"""VM/ViewFS composed cache contract; real Git objects, no FUSE mount or network."""

import errno
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest import TestCase, mock

import pygit2
from git_viewspace_fs.filesystem import GitViewspaceFS
from git_viewspace_fs.gitoper import GitOperations, GitOperError, NodeType


class _ODBProbe:
    def __init__(self, repository, reads, headers):
        self.repository = repository
        self.reads = reads
        self.headers = headers
        self.fail_next = False

    def read(self, oid):
        self.reads[(self.repository.path, str(oid))] += 1
        if self.fail_next:
            self.fail_next = False
            raise pygit2.GitError("synthetic read failure")
        return self.repository.odb.read(oid)

    def read_header(self, oid):
        self.headers[(self.repository.path, str(oid))] += 1
        return self.repository.odb.read_header(oid)


class _ObjectProbe:
    """Keep real lazy tree entries, but observe accidental payload attribute access."""

    def __init__(self, value, repository_path, blobs):
        self.value = value
        self.repository_path = repository_path
        self.blobs = blobs

    @property
    def __class__(self):
        return type(self.value)

    def _wrap(self, value):
        if isinstance(value, (pygit2.Commit, pygit2.Tree, pygit2.Blob)):
            return _ObjectProbe(value, self.repository_path, self.blobs)
        return value

    def __getattr__(self, name):
        # pygit2 1.20.1 tree entries are lazy. These entry fields do not load data.
        if isinstance(self.value, pygit2.Blob) and name not in {
            "id",
            "name",
            "raw_name",
            "filemode",
            "type",
            "type_str",
        }:
            self.blobs[(self.repository_path, str(self.value.id))] += 1
        return self._wrap(getattr(self.value, name))

    def __getitem__(self, key):
        return self._wrap(self.value[key])

    def __iter__(self):
        return (self._wrap(value) for value in self.value)


class _RepositoryProbe:
    """Also trap Blob construction: counting only odb.read misses repo[oid]."""

    def __init__(self, path, reads, headers, blobs):
        self.repository = pygit2.Repository(path)
        self.odb = _ODBProbe(self.repository, reads, headers)
        self.blobs = blobs

    def __getitem__(self, oid):
        result = self.repository[oid]
        if isinstance(result, pygit2.Blob):
            self.blobs[(self.repository.path, str(result.id))] += 1
        if isinstance(result, (pygit2.Commit, pygit2.Tree)):
            return _ObjectProbe(result, self.repository.path, self.blobs)
        return result

    def __getattr__(self, name):
        return getattr(self.repository, name)


def _snapshot(repository, files, *, links=None):
    tree = repository.TreeBuilder()
    for name, data in files.items():
        mode = (
            pygit2.GIT_FILEMODE_BLOB_EXECUTABLE
            if name == "executable"
            else pygit2.GIT_FILEMODE_BLOB
        )
        tree.insert(name, repository.create_blob(data), mode)
    for name, oid in (links or {}).items():
        tree.insert(name, oid, pygit2.GIT_FILEMODE_COMMIT)
    signature = pygit2.Signature("Cache fixture", "cache@example.invalid", 1000, 0)
    return str(repository.create_commit(None, signature, signature, "fixture", tree.write(), []))


class ReadCacheTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="viewfs-cache-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repository = pygit2.init_repository(self.root / "repository", bare=True)
        self.payload = bytes(range(256)) * 256
        self.files = {
            "payload": self.payload,
            "executable": b"exec",
            "a": b"aaaa",
            "b": b"bbbb",
            "c": b"cccc",
            "d": b"dddddd",
            "large": b"123456789",
            "empty": b"",
        }
        self.commit = _snapshot(self.repository, self.files)
        self.changed = _snapshot(self.repository, {**self.files, "payload": b"new\x00bytes"})
        self.reads, self.headers, self.blobs = Counter(), Counter(), Counter()
        self.probes = []

        def open_repository(path):
            probe = _RepositoryProbe(path, self.reads, self.headers, self.blobs)
            self.probes.append(probe)
            return probe

        patcher = mock.patch("git_viewspace_fs.gitoper.Repository", side_effect=open_repository)
        patcher.start()
        self.addCleanup(patcher.stop)

    def operations(self, **kwargs):
        return GitOperations(self.repository.path, **kwargs)

    def filesystem(self):
        config = self.root / "views.json"
        config.write_text(
            json.dumps(
                {
                    name: {"commit": commit}
                    for name, commit in [
                        ("first", self.commit),
                        ("alias", self.commit),
                        ("new", self.changed),
                    ]
                }
            )
        )
        return GitViewspaceFS(self.repository.path, str(config), workload_uid=1234, workload_gid=5678)

    def test_partial_reads_share_content_across_views_and_preserve_versions(self):
        fs = self.filesystem()
        path = "/views/first/payload"
        handle = fs.open(path, 0)
        data = b"".join(
            fs.read(path, 4096, offset, handle) for offset in range(0, len(self.payload), 4096)
        )
        fs.release(path, handle)
        self.assertEqual(data, self.payload)
        self.assertEqual(fs.read(path, 10, len(data), fs.open(path, 0)), b"")
        self.assertEqual(fs.read("/views/alias/payload", len(data), 0, 0), data)
        self.assertEqual(fs.read("/views/new/a", 4, 0, 0), b"aaaa")
        self.assertEqual(fs.read("/views/first/a", 4, 0, 0), b"aaaa")
        self.assertEqual(fs.read("/views/new/payload", 100, 0, 0), b"new\x00bytes")
        self.assertEqual(fs.read(path, 256, 0, 0), self.payload[:256])
        self.assertEqual(sum(self.blobs.values()), 0, "no hidden Repository[blob] loads")
        self.assertEqual(sorted(self.reads.values()), [1, 1, 1])

    def test_regular_metadata_uses_headers_without_loading_contents(self):
        fs = self.filesystem()
        for _ in range(3):
            for name, kind in (("payload", NodeType.REGULAR), ("executable", NodeType.EXECUTABLE)):
                self.assertEqual(fs._git.node_type(self.commit, name), kind)
                self.assertEqual(
                    fs.getattr("/views/first/" + name)["st_size"], len(self.files[name])
                )
        self.assertEqual(sum(self.blobs.values()), 0)
        self.assertEqual(sum(self.reads.values()), 0)
        self.assertEqual(sorted(self.headers.values()), [1, 1])

    def test_byte_budget_evicts_lru_not_fifo(self):
        ops = self.operations(content_cache_bytes=8)
        for name in ("a", "b", "a", "c", "a", "b"):
            self.assertEqual(ops.file_contents(self.commit, name), self.files[name])
            self.assertLessEqual(ops.cache_stats()["resident_bytes"], 8)
        self.assertEqual(sum(self.blobs.values()), 0)
        self.assertEqual(sorted(self.reads.values()), [1, 1, 2])
        stats = ops.cache_stats()
        self.assertEqual(
            {
                key: stats[key]
                for key in (
                    "limit_bytes",
                    "resident_bytes",
                    "entries",
                    "hits",
                    "misses",
                    "evictions",
                    "decoded_bytes",
                )
            },
            dict(
                limit_bytes=8,
                resident_bytes=8,
                entries=2,
                hits=2,
                misses=4,
                evictions=2,
                decoded_bytes=16,
            ),
        )
        # A caller cannot alter the live counters through the returned snapshot.
        stats["resident_bytes"] = -1
        self.assertEqual(ops.cache_stats()["resident_bytes"], 8)
        # Six bytes fit the budget but require evicting BOTH four-byte entries.
        self.assertEqual(ops.file_contents(self.commit, "d"), b"dddddd")
        stats = ops.cache_stats()
        self.assertEqual((stats["resident_bytes"], stats["entries"], stats["evictions"]), (6, 1, 4))
        for name in ("a", "b"):
            self.assertEqual(ops.file_contents(self.commit, name), self.files[name])
        expected = {"a": 2, "b": 3, "c": 1, "d": 1}
        self.assertEqual(
            self.reads,
            Counter(
                {
                    (
                        self.repository.path,
                        str(self.repository.create_blob(self.files[name])),
                    ): count
                    for name, count in expected.items()
                }
            ),
        )
        self.assertEqual(sum(self.blobs.values()), 0)

    def test_oversized_and_empty_reads_do_not_displace_fitting_content(self):
        ops = self.operations(content_cache_bytes=8)
        for name in ("a", "b", "large", "large", "empty", "a", "b"):
            self.assertEqual(ops.file_contents(self.commit, name), self.files[name])
        self.assertEqual(sum(self.blobs.values()), 0)
        self.assertEqual(sorted(self.reads.values()), [1, 1, 1, 2])
        stats = ops.cache_stats()
        self.assertEqual((stats["resident_bytes"], stats["entries"], stats["evictions"]), (8, 2, 0))
        self.assertEqual(stats["decoded_bytes"], 26)

    def test_zero_disables_retention_and_exact_limit_fits(self):
        for limit, expected_reads in ((0, 2), (4, 1)):
            with self.subTest(limit=limit):
                self.reads.clear()
                ops = self.operations(content_cache_bytes=limit)
                for _ in range(2):
                    self.assertEqual(ops.file_contents(self.commit, "a"), b"aaaa")
                self.assertEqual(sum(self.reads.values()), expected_reads)
                self.assertEqual(ops.cache_stats()["resident_bytes"], limit)
        self.assertEqual(sum(self.blobs.values()), 0)

    def test_failed_content_read_is_not_cached(self):
        ops = self.operations()
        self.probes[0].odb.fail_next = True
        with self.assertRaises(GitOperError) as caught:
            ops.file_contents(self.commit, "a")
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(ops.cache_stats()["entries"], 0)
        for _ in range(2):
            self.assertEqual(ops.file_contents(self.commit, "a"), b"aaaa")
        self.assertEqual(sum(self.reads.values()), 2)
        self.assertEqual(sum(self.blobs.values()), 0)
        self.assertEqual(ops.cache_stats()["decoded_bytes"], 4)

    def test_prepared_module_uses_own_repository_even_for_identical_blob(self):
        from git_viewspace.submodules import store_path

        url = str(self.root / "synthetic-module-origin")
        store = store_path(self.repository.path, url)
        store.parent.mkdir(parents=True)
        module = pygit2.init_repository(store, bare=True)
        module_commit = _snapshot(module, {"payload": self.payload})
        registration = f'[submodule "code"]\npath = code\nurl = {url}\n'.encode()
        commit = _snapshot(
            self.repository,
            {"payload": self.payload, ".gitmodules": registration},
            links={"code": pygit2.Oid(hex=module_commit)},
        )
        ops = self.operations()
        for _ in range(2):
            self.assertEqual(ops.file_contents(commit, "payload"), self.payload)
            self.assertEqual(ops.file_contents(commit, "code/payload"), self.payload)
        oid = str(self.repository.create_blob(self.payload))
        self.assertEqual(sum(self.blobs.values()), 0)
        self.assertEqual(
            self.reads, Counter({(self.repository.path, oid): 1, (module.path, oid): 1})
        )

    def test_new_instance_starts_cold(self):
        for _ in range(2):
            ops = self.operations()
            self.assertEqual(ops.file_contents(self.commit, "a"), b"aaaa")
            self.assertEqual(ops.file_contents(self.commit, "a"), b"aaaa")
        self.assertEqual(sum(self.blobs.values()), 0)
        self.assertEqual(list(self.reads.values()), [2])

    def test_cli_budget_precedence_reaches_real_filesystem(self):
        from git_viewspace_fs import __main__ as cli

        config = self.root / "views.json"
        config.write_text(json.dumps({"test": {"commit": self.commit}}))
        argv = [
            "git-viewspace-fs",
            self.repository.path,
            str(self.root / "mount"),
            "--config",
            str(config),
            "--workload-uid",
            "1234",
            "--workload-gid",
            "5678",
            "--root-uuid",
            "fixture",
            "--attempt-token",
            "attempt",
        ]
        for env, option, expected in (
            ({}, [], 268435456),
            ({"GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES": "4"}, [], 4),
            ({"GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES": "invalid"}, ["--content-cache-bytes", "0"], 0),
        ):
            with (
                self.subTest(expected=expected),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(sys, "argv", argv + option),
                mock.patch.object(cli, "FUSE") as mount,
                mock.patch.object(sys, "stderr"),
            ):
                cli.main()
                fs = mount.call_args.args[0]
                self.assertEqual(fs._git.cache_stats()["limit_bytes"], expected)
                self.assertEqual(fs.read("/views/test/a", 4, 0, 0), b"aaaa")
        with (
            mock.patch.dict(os.environ, {"GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES": "-1"}),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(cli, "FUSE") as mount,
            mock.patch.object(sys, "stderr"),
            self.assertRaises(SystemExit) as caught,
        ):
            cli.main()
        self.assertEqual(caught.exception.code, 2)
        mount.assert_not_called()

    def test_cache_diagnostics_and_python_limit_validation(self):
        for value in (-1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.operations(content_cache_bytes=value)
        sink = mock.Mock()
        ops = self.operations(content_cache_bytes=4, instrumentation=sink)
        for name in ("a", "a", "b"):
            self.assertEqual(ops.file_contents(self.commit, name), self.files[name])
        events = [call.args[0] for call in sink.emit.call_args_list]
        self.assertEqual(events.count("git_content_cache_hit"), 1)
        self.assertEqual(events.count("git_content_cache_eviction"), 1)
        record = sink.emit.call_args.kwargs
        self.assertEqual(record, ops.cache_stats())
        self.assertNotIn("aaaa", repr(sink.emit.call_args_list))

    def test_supervisor_snapshots_budget_and_forwards_explicit_argument(self):
        from types import SimpleNamespace

        from git_viewspace.viewfs import ViewFS

        context = SimpleNamespace(
            root=self.root,
            repository=Path(self.repository.path),
            mountpoint=self.root / "mount",
            config_path=self.root / "views.json",
            uid=1234,
            gid=5678,
            root_uuid="fixture",
        )
        with mock.patch.dict(os.environ, {"GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES": "8"}):
            supervisor = ViewFS(context, executable="/bin/true")
            os.environ["GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES"] = "16"
            for attempt in ("first", "restarted"):
                command = supervisor._command(attempt)
                self.assertEqual(command[command.index("--content-cache-bytes") + 1], "8")
                self.assertEqual(command[1:3], ("-n", "--"))
            fresh = ViewFS(context, executable="/bin/true")
            self.assertEqual(fresh.content_cache_bytes, 16)
