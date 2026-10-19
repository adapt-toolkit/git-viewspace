# SPDX-License-Identifier: Apache-2.0

import errno
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase, main

from fuse import FuseOSError

from git_viewspace_fs.filesystem import GitViewspaceFS
from git_viewspace_fs.handlers.root import RootHandler
from git_viewspace_fs.handlers.view import ViewHandler
from git_viewspace_fs.instrumentation import JsonlInstrumentation


PROJECT_DIR = Path(__file__).resolve().parents[2]
TEST_REPO = PROJECT_DIR / "test_repo"


def git_output(*arguments):
    return subprocess.check_output(
        ["git", "-C", os.fspath(TEST_REPO), *arguments],
        text=True,
    ).strip()


class GitViewspaceFSTestCase(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "views.json"
        self.commit = git_output("rev-parse", "HEAD")
        self.previous_commit = git_output("rev-parse", "HEAD^")
        self.write_config(
            {
                "current": {"commit": self.commit},
                "older": {"commit": self.previous_commit},
            }
        )
        self.filesystem = GitViewspaceFS(
            os.fspath(TEST_REPO),
            os.fspath(self.config_path),
            workload_uid=1234,
            workload_gid=5678,
        )

    def write_config(self, configuration):
        self.config_path.write_text(json.dumps(configuration), encoding="utf-8")

    def assert_fuse_error(self, expected_errno, function, *arguments):
        with self.assertRaises(FuseOSError) as caught:
            function(*arguments)
        self.assertEqual(caught.exception.errno, expected_errno)

    def test_constructor_uses_absolute_paths_and_fixed_identity_and_modes(self):
        self.assertEqual(self.filesystem.repo, os.fspath(TEST_REPO.resolve()))
        self.assertEqual(self.filesystem.config, os.fspath(self.config_path.resolve()))
        self.assertEqual(self.filesystem.workload_uid, 1234)
        self.assertEqual(self.filesystem.workload_gid, 5678)
        self.assertEqual(self.filesystem.DIRECTORY_MODE, 0o755)
        self.assertEqual(self.filesystem.REGULAR_MODE, 0o644)
        self.assertEqual(self.filesystem.EXECUTABLE_MODE, 0o755)

    def test_constructor_rejects_root_invalid_and_reserved_identity_values(self):
        invalid_values = (True, None, "123", -1, 0, 1 << 32)
        for parameter in ("workload_uid", "workload_gid"):
            for value in invalid_values:
                arguments = {"workload_uid": 1234, "workload_gid": 5678}
                arguments[parameter] = value
                with self.subTest(parameter=parameter, value=value), \
                        self.assertRaises(ValueError):
                    GitViewspaceFS(
                        os.fspath(TEST_REPO),
                        os.fspath(self.config_path),
                        **arguments,
                    )

    def test_optional_instrumentation_separates_startup_from_demanded_tree_io(self):
        log_path = Path(self.temporary_directory.name) / "instrumentation.jsonl"
        log_path.touch(mode=0o600)
        instrumentation = JsonlInstrumentation(
            log_path,
            "GitViewspaceFS:root-1:attempt-1",
            os.getuid(),
            os.getgid(),
        )
        self.addCleanup(instrumentation.close)
        filesystem = GitViewspaceFS(
            os.fspath(TEST_REPO),
            os.fspath(self.config_path),
            workload_uid=1234,
            workload_gid=5678,
            instrumentation=instrumentation,
        )

        startup_records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["event"] for record in startup_records],
            ["git_repository_open", "filesystem_ready"],
        )

        self.assertEqual(
            filesystem.read(
                "/views/current/dir_a/dir_b/dir_c/file_ca",
                100,
                0,
                None,
            ),
            b"nested\n",
        )
        demanded_records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        demanded_events = [record["event"] for record in demanded_records]
        self.assertIn("fuse_read", demanded_events)
        self.assertIn("git_resolve_commit", demanded_events)
        self.assertIn("git_tree_path_lookup", demanded_events)
        self.assertNotIn("git_directory_contents", demanded_events)
        self.assertTrue(
            any(
                record["event"] == "git_tree_path_lookup"
                and record["depth"] == 4
                and record["path"] == "dir_a/dir_b/dir_c/file_ca"
                for record in demanded_records
            )
        )

    def test_root_and_views_directory_listings_have_only_current_namespaces(self):
        self.assertEqual(self.filesystem.readdir("/", None), [".", "..", "views"])
        self.assertEqual(
            self.filesystem.readdir("/views", None),
            [".", "..", "current", "older"],
        )
        self.assertEqual(
            self.filesystem.readdir("/views/", None),
            [".", "..", "current", "older"],
        )

    def test_handler_routing_is_boundary_aware(self):
        self.assertIsInstance(self.filesystem._get_handler("/"), RootHandler)
        self.assertIsInstance(self.filesystem._get_handler("/views"), ViewHandler)
        self.assertIsInstance(
            self.filesystem._get_handler("/views/current"),
            ViewHandler,
        )

        for path in (
            "",
            "/viewsfoo",
            "/commits-by-hash",
            "/commits-by-date",
            "/branches",
            "/tags",
        ):
            with self.subTest(path=path):
                self.assert_fuse_error(errno.ENOENT, self.filesystem.getattr, path)

    def test_all_legacy_namespaces_and_their_descendants_are_absent(self):
        for namespace in (
            "commits-by-hash",
            "commits-by-date",
            "branches",
            "tags",
        ):
            for suffix in ("", "/anything"):
                path = f"/{namespace}{suffix}"
                with self.subTest(path=path):
                    self.assert_fuse_error(
                        errno.ENOENT,
                        self.filesystem.getattr,
                        path,
                    )

    def test_commit_hash_is_not_an_implicit_view_name(self):
        self.assert_fuse_error(
            errno.ENOENT,
            self.filesystem.getattr,
            f"/views/{self.commit}",
        )
        self.assertEqual(
            self.filesystem.read("/views/current/version.txt", 100, 0, None),
            b"new\n",
        )

    def test_getattr_reports_types_fixed_modes_ownership_and_sizes(self):
        root = self.filesystem.getattr("/")
        views = self.filesystem.getattr("/views")
        alias = self.filesystem.getattr("/views/current")
        regular = self.filesystem.getattr("/views/current/file_a")
        executable = self.filesystem.getattr("/views/current/executable.sh")
        empty = self.filesystem.getattr("/views/current/empty")
        symlink = self.filesystem.getattr("/views/current/link_unicode")

        for directory in (root, views, alias):
            self.assertTrue(stat.S_ISDIR(directory["st_mode"]))
            self.assertEqual(directory["st_nlink"], 2)
            self.assertEqual(stat.S_IMODE(directory["st_mode"]), 0o755)

        self.assertTrue(stat.S_ISREG(regular["st_mode"]))
        self.assertEqual(regular["st_nlink"], 1)
        self.assertEqual(regular["st_size"], len(b"Contents\n"))
        self.assertEqual(
            stat.S_IMODE(regular["st_mode"]),
            0o644,
        )

        self.assertTrue(stat.S_ISREG(executable["st_mode"]))
        self.assertEqual(
            stat.S_IMODE(executable["st_mode"]),
            0o755,
        )
        self.assertEqual(empty["st_size"], 0)

        self.assertTrue(stat.S_ISLNK(symlink["st_mode"]))
        self.assertEqual(stat.S_IMODE(symlink["st_mode"]), 0o777)
        self.assertEqual(symlink["st_nlink"], 1)
        self.assertEqual(symlink["st_size"], len("café".encode("utf-8")))

        for attributes in (root, views, alias, regular, executable, empty, symlink):
            self.assertEqual(attributes["st_uid"], 1234)
            self.assertEqual(attributes["st_gid"], 5678)

    def test_commit_entries_use_commit_and_author_timestamps(self):
        commit_time = int(git_output("show", "-s", "--format=%ct", "HEAD"))
        author_time = int(git_output("show", "-s", "--format=%at", "HEAD"))
        self.assertNotEqual(commit_time, author_time)

        for path in (
            "/views/current",
            "/views/current/dir_a",
            "/views/current/file_a",
            "/views/current/link_relative",
        ):
            with self.subTest(path=path):
                attributes = self.filesystem.getattr(path)
                self.assertEqual(attributes["st_mtime"], commit_time)
                self.assertEqual(attributes["st_ctime"], author_time)

    def test_read_returns_binary_content_with_offset_and_size(self):
        path = "/views/current/binary.bin"
        self.assertEqual(self.filesystem.read(path, 99, 0, None), b"a\0b\n")
        self.assertEqual(self.filesystem.read(path, 2, 1, None), b"\0b")
        self.assertEqual(self.filesystem.read(path, 0, 0, None), b"")
        self.assertEqual(self.filesystem.read(path, 10, 999, None), b"")

    def test_nested_paths_and_alias_root_trailing_slash_work(self):
        self.assertEqual(
            self.filesystem.read(
                "/views/current/dir_a/dir_b/dir_c/file_ca",
                100,
                0,
                None,
            ),
            b"nested\n",
        )
        self.assertEqual(
            set(self.filesystem.readdir("/views/current/", None)),
            set(self.filesystem.readdir("/views/current", None)),
        )
        self.assert_fuse_error(
            errno.ENOENT,
            self.filesystem.getattr,
            "/views/current/dir_a/",
        )

    def test_repository_files_with_former_metadata_names_are_literal_content(self):
        entries = set(self.filesystem.readdir("/views/current", None))
        literal_files = {
            ".author": b"literal author file\n",
            ".author-email": b"literal author email file\n",
            ".git-descendants": b"literal descendants file\n",
            ".git-names": b"literal names file\n",
        }
        self.assertTrue(set(literal_files).issubset(entries))
        self.assertIn(".git-parents", entries)
        self.assertNotIn(".git-log", entries)

        for name, contents in literal_files.items():
            path = f"/views/current/{name}"
            with self.subTest(name=name):
                attributes = self.filesystem.getattr(path)
                self.assertTrue(stat.S_ISREG(attributes["st_mode"]))
                self.assertEqual(attributes["st_size"], len(contents))
                self.assertEqual(
                    self.filesystem.read(path, 100, 0, None),
                    contents,
                )

        parent_directory = self.filesystem.getattr("/views/current/.git-parents")
        self.assertTrue(stat.S_ISDIR(parent_directory["st_mode"]))
        self.assertEqual(
            self.filesystem.readdir("/views/current/.git-parents", None),
            [".", "..", "literal-parent"],
        )
        self.assertEqual(
            self.filesystem.read(
                "/views/current/.git-parents/literal-parent",
                100,
                0,
                None,
            ),
            b"repository content\n",
        )

    def test_early_commit_has_no_synthetic_metadata_entries(self):
        early_commit = git_output("rev-parse", "t20050607")
        self.write_config({"early": {"commit": early_commit}})

        self.assertEqual(
            self.filesystem.readdir("/views/early", None),
            [".", "..", "empty", "file_a"],
        )
        for name in (
            ".git-parents",
            ".git-descendants",
            ".git-names",
            ".author",
            ".author-email",
        ):
            with self.subTest(name=name):
                self.assert_fuse_error(
                    errno.ENOENT,
                    self.filesystem.getattr,
                    f"/views/early/{name}",
                )

    def test_readlink_preserves_relative_absolute_parent_and_dangling_targets(self):
        expected = {
            "link_relative": "file_a",
            "link_absolute": "/absolute/target",
            "link_parent": "../outside-the-view",
            "link_dangling": "does-not-exist",
            "dir_a/link_parent": "../file_a",
            "link_unicode": "café",
        }
        for path, target in expected.items():
            with self.subTest(path=path):
                self.assertEqual(
                    self.filesystem.readlink(f"/views/current/{path}"),
                    target,
                )

    def test_invalid_symlinks_and_gitlinks_are_io_errors(self):
        for path in (
            "link_invalid_utf8",
            "link_nul",
            "gitlink",
            "gitlink/child",
        ):
            with self.subTest(path=path):
                self.assert_fuse_error(
                    errno.EIO,
                    self.filesystem.getattr,
                    f"/views/current/{path}",
                )
        for path in ("link_invalid_utf8", "link_nul"):
            with self.subTest(readlink=path):
                self.assert_fuse_error(
                    errno.EIO,
                    self.filesystem.readlink,
                    f"/views/current/{path}",
                )

    def test_wrong_operations_and_missing_paths_preserve_errno(self):
        cases = (
            (errno.ENOENT, self.filesystem.getattr, ("/views/missing",)),
            (
                errno.ENOENT,
                self.filesystem.getattr,
                ("/views/current/does-not-exist",),
            ),
            (
                errno.ENOTDIR,
                self.filesystem.getattr,
                ("/views/current/file_a/child",),
            ),
            (
                errno.ENOTDIR,
                self.filesystem.readdir,
                ("/views/current/file_a", None),
            ),
            (
                errno.EISDIR,
                self.filesystem.read,
                ("/views/current/dir_a", 10, 0, None),
            ),
            (errno.EISDIR, self.filesystem.read, ("/", 10, 0, None)),
            (
                errno.EINVAL,
                self.filesystem.read,
                ("/views/current/link_relative", 10, 0, None),
            ),
            (
                errno.EINVAL,
                self.filesystem.readlink,
                ("/views/current/file_a",),
            ),
            (errno.EINVAL, self.filesystem.readlink, ("/views",)),
        )
        for expected_errno, function, arguments in cases:
            with self.subTest(function=function.__name__, arguments=arguments):
                self.assert_fuse_error(expected_errno, function, *arguments)

    def test_successful_first_access_pins_an_alias_across_config_changes(self):
        self.write_config({"pinned": {"commit": self.previous_commit}})
        self.assertEqual(
            self.filesystem.read("/views/pinned/version.txt", 100, 0, None),
            b"old\n",
        )

        self.write_config(
            {
                "pinned": {"commit": self.commit},
                "new": {"commit": self.commit},
            }
        )
        self.assertEqual(
            self.filesystem.read("/views/pinned/version.txt", 100, 0, None),
            b"old\n",
        )
        self.assertEqual(
            self.filesystem.read("/views/new/version.txt", 100, 0, None),
            b"new\n",
        )

        self.config_path.write_text("{", encoding="utf-8")
        self.assertEqual(
            self.filesystem.read("/views/pinned/version.txt", 100, 0, None),
            b"old\n",
        )
        self.assertEqual(
            self.filesystem.read("/views/new/version.txt", 100, 0, None),
            b"new\n",
        )
        self.assertEqual(
            self.filesystem.readdir("/views", None),
            [".", "..", "new", "pinned"],
        )

    def test_failed_unpinned_access_is_retried_after_config_becomes_valid(self):
        self.config_path.write_text("{", encoding="utf-8")
        self.assert_fuse_error(
            errno.ENOENT,
            self.filesystem.getattr,
            "/views/future",
        )

        self.write_config({"future": {"commit": self.commit}})
        self.assertEqual(
            self.filesystem.read("/views/future/version.txt", 100, 0, None),
            b"new\n",
        )

    def test_valid_alias_is_pinned_even_when_the_requested_child_is_missing(self):
        self.write_config({"pinned": {"commit": self.previous_commit}})
        self.assert_fuse_error(
            errno.ENOENT,
            self.filesystem.getattr,
            "/views/pinned/missing-child",
        )

        self.write_config({"pinned": {"commit": self.commit}})
        self.assertEqual(
            self.filesystem.read("/views/pinned/version.txt", 100, 0, None),
            b"old\n",
        )

    def test_empty_alias_and_repeated_separators_do_not_escape_views(self):
        for path in (
            "//",
            "/views//",
            "/views//current",
            "/views/current//file_a",
            "/views/current/./file_a",
            "/views/current/../file_a",
        ):
            with self.subTest(path=path):
                self.assert_fuse_error(errno.ENOENT, self.filesystem.getattr, path)


if __name__ == "__main__":
    main()
