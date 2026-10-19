import contextlib
import fcntl
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_viewspace.cli import run
from git_viewspace.errors import GitViewspaceError, InjectedFailure
from git_viewspace.init import InitRequest, Initializer
from git_viewspace.storage import atomic_json, read_json


class InitTest(unittest.TestCase):
    def setUp(self):
        # Keep managed-root control sockets below the Linux AF_UNIX path limit even when the
        # harness routes TMPDIR through its private scratch workspace.
        self.temporary = tempfile.TemporaryDirectory(prefix="v-")
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)

    def git(self, *arguments, cwd=None, check=True):
        return subprocess.run(
            ("git", *map(str, arguments)),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            check=check,
        )

    def repository(self, name="source", *, branch="main", empty=False):
        path = self.base / name
        self.git("init", "--initial-branch", branch, path)
        self.git("config", "user.name", "Source User", cwd=path)
        self.git("config", "user.email", "source@example.invalid", cwd=path)
        self.git("config", "core.autocrlf", "false", cwd=path)
        if not empty:
            (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            self.git("add", "tracked.txt", cwd=path)
        self.git("commit", "--allow-empty", "-m", "base", cwd=path)
        return path

    def request(self, root, mode, source=None, **changes):
        values = {
            "root": root,
            "git_name": "Managed User",
            "git_email": "managed@example.invalid",
            "source_mode": mode,
            "source": None if source is None else str(source),
            "clone_branch": None,
            "initial_branch": None,
        }
        values.update(changes)
        return InitRequest(**values)

    def assert_layout(self, root):
        self.assertTrue((root / "repo/.git").is_dir())
        for relative in (
            "config/manager.json",
            "config/viewfs.json",
            "state/manager.json",
            "state/lock",
            "state/transactions",
            "run",
            "git-mnt",
            "views",
            "repo/.git/git-viewspace-owner.json",
            "repo/.git/git-viewspace.lock",
        ):
            self.assertTrue((root / relative).exists(), relative)
        self.assertEqual(list((root / "state/transactions").iterdir()), [])
        self.assertEqual(read_json(root / "config/viewfs.json"), {})
        self.assertEqual(read_json(root / "state/manager.json")["views"], {})

    def test_empty_creates_root_commit_and_is_idempotent(self):
        root = self.base / "root"
        request = self.request(root, "empty")
        first = Initializer().initialize(request)
        second = Initializer().initialize(request)
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.root_uuid, second.root_uuid)
        self.assert_layout(root)
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=root / "repo").stdout.strip(),
            b"main",
        )
        self.assertEqual(self.git("ls-tree", "-r", "HEAD", cwd=root / "repo").stdout, b"")
        parents = self.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=root / "repo").stdout.split()
        self.assertEqual(len(parents), 1)

    def test_empty_honors_initial_branch(self):
        root = self.base / "root"
        request = self.request(root, "empty", initial_branch="trunk")
        Initializer().initialize(request)
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=root / "repo").stdout.strip(),
            b"trunk",
        )

    def test_init_rejects_overlong_control_socket_path_before_creating_root(self):
        suffix = "x"
        while True:
            root = self.base / suffix
            socket_path = root / "run/manager.sock"
            if len(os.fsencode(socket_path)) > 103:
                break
            suffix += "x"
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(self.request(root, "empty"))
        self.assertEqual(caught.exception.code, "SOCKET_PATH_TOO_LONG")
        self.assertIn(f"socket path is {len(os.fsencode(socket_path))} bytes", caught.exception.detail)
        self.assertIn("maximum is 103", caught.exception.detail)
        self.assertIn(str(socket_path), caught.exception.detail)
        self.assertFalse(root.exists())

    def test_copy_preserves_source_and_publishes_equivalent_repository(self):
        source = self.repository()
        source_head = self.git("rev-parse", "HEAD", cwd=source).stdout
        root = self.base / "root"
        Initializer().initialize(self.request(root, "copy", source))
        self.assertTrue(source.exists())
        self.assertFalse((source / ".git/git-viewspace-owner.json").exists())
        self.assertEqual(self.git("status", "--porcelain", cwd=source).stdout, b"")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=root / "repo").stdout, source_head)
        self.assertEqual((root / "repo/tracked.txt").read_bytes(), b"tracked\n")
        self.assert_layout(root)

    def test_move_removes_source_and_preserves_commit(self):
        source = self.repository()
        source_head = self.git("rev-parse", "HEAD", cwd=source).stdout
        root = self.base / "root"
        Initializer().initialize(self.request(root, "move", source))
        self.assertFalse(source.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=root / "repo").stdout, source_head)
        self.assert_layout(root)

    def test_clone_and_reviewed_branch_argument(self):
        source = self.repository()
        self.git("checkout", "-b", "feature", cwd=source)
        (source / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.git("add", "feature.txt", cwd=source)
        self.git("commit", "-m", "feature", cwd=source)
        feature = self.git("rev-parse", "HEAD", cwd=source).stdout
        self.git("checkout", "main", cwd=source)

        root = self.base / "root"
        request = self.request(root, "clone", source, clone_branch="feature")
        Initializer().initialize(request)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=root / "repo").stdout, feature)
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=root / "repo").stdout.strip(),
            b"feature",
        )

    def test_clone_failure_has_actionable_diagnostic_and_retryable_transaction(self):
        root = self.base / "root"
        request = self.request(root, "clone", self.base / "missing")
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(request)
        self.assertEqual(caught.exception.code, "GIT_CLONE_FAILED")
        self.assertIn("use --move", caught.exception.message)
        self.assertTrue((root / "state/transactions/init.json").is_file())
        self.assertFalse((root / "state/transactions/init-repository").exists())

        source = self.repository("fallback")
        result = Initializer().initialize(self.request(root, "move", source))
        self.assertFalse(result.idempotent)
        self.assertFalse(source.exists())
        self.assert_layout(root)

    def test_dirty_copy_source_is_rejected_before_root_transaction(self):
        source = self.repository()
        (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        root = self.base / "root"
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(self.request(root, "copy", source))
        self.assertEqual(caught.exception.code, "DIRTY_REPOSITORY")
        self.assertFalse(root.exists())

    def test_claimed_source_is_rejected(self):
        source = self.repository()
        (source / ".git/git-viewspace-owner.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(self.request(self.base / "root", "copy", source))
        self.assertEqual(caught.exception.code, "REPOSITORY_CLAIMED")

    def test_transforming_attributes_are_rejected(self):
        source = self.repository()
        (source / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
        self.git("add", ".gitattributes", cwd=source)
        self.git("commit", "-m", "attributes", cwd=source)
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(self.request(self.base / "root", "copy", source))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_GIT_ATTRIBUTES")

    def test_non_utf8_symlink_target_is_rejected(self):
        source = self.repository()
        os.symlink(b"\xff", os.fsencode(source / "bad-link"))
        self.git("add", "bad-link", cwd=source)
        self.git("commit", "-m", "invalid symlink", cwd=source)
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(self.request(self.base / "root", "copy", source))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_REPOSITORY")

    def test_shallow_bare_and_in_progress_repositories_are_rejected(self):
        source = self.repository("history")
        (source / "tracked.txt").write_text("second\n", encoding="utf-8")
        self.git("commit", "-am", "second", cwd=source)
        shallow = self.base / "shallow"
        self.git("clone", "--depth", "1", f"file://{source}", shallow)
        with self.assertRaises(GitViewspaceError) as shallow_error:
            Initializer().initialize(self.request(self.base / "shallow-root", "copy", shallow))
        self.assertEqual(shallow_error.exception.code, "UNSUPPORTED_REPOSITORY")

        bare = self.base / "bare.git"
        self.git("init", "--bare", bare)
        with self.assertRaises(GitViewspaceError) as bare_error:
            Initializer().initialize(self.request(self.base / "bare-root", "copy", bare))
        self.assertEqual(bare_error.exception.code, "INVALID_REPOSITORY")

        active = self.repository("active")
        (active / ".git/BISECT_LOG").write_text("active\n", encoding="utf-8")
        with self.assertRaises(GitViewspaceError) as active_error:
            Initializer().initialize(self.request(self.base / "active-root", "copy", active))
        self.assertEqual(active_error.exception.code, "GIT_OPERATION_IN_PROGRESS")

    def test_clone_branch_rejects_tag(self):
        source = self.repository()
        self.git("tag", "release", cwd=source)
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(
                self.request(
                    self.base / "root",
                    "clone",
                    source,
                    clone_branch="release",
                )
            )
        self.assertEqual(caught.exception.code, "CLONE_BRANCH_MISMATCH")

    def test_root_caller_is_rejected(self):
        with mock.patch("git_viewspace.init.os.getuid", return_value=0):
            with self.assertRaises(GitViewspaceError) as caught:
                Initializer().initialize(self.request(self.base / "root", "empty"))
        self.assertEqual(caught.exception.code, "ROOT_CALLER_FORBIDDEN")

    def test_busy_root_lock_is_rejected(self):
        root = self.base / "root"
        request = self.request(root, "empty")
        Initializer().initialize(request)
        descriptor = os.open(root / "state/lock", os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(GitViewspaceError) as caught:
                Initializer().initialize(request)
            self.assertEqual(caught.exception.code, "ROOT_BUSY")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_unexplained_completed_root_entry_is_rejected(self):
        root = self.base / "root"
        request = self.request(root, "empty")
        Initializer().initialize(request)
        (root / "unexpected").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(request)
        self.assertEqual(caught.exception.code, "INVALID_STATE")

    def test_relative_root_and_misapplied_branch_options_are_rejected(self):
        source = self.repository()
        with self.assertRaises(GitViewspaceError) as relative:
            Initializer().initialize(self.request(Path("relative"), "copy", source))
        self.assertEqual(relative.exception.code, "INVALID_ROOT")
        with self.assertRaises(GitViewspaceError) as branch:
            Initializer().initialize(
                self.request(self.base / "root", "copy", source, clone_branch="main")
            )
        self.assertEqual(branch.exception.code, "INVALID_ARGUMENT")

    def test_completed_initialization_rejects_changed_arguments(self):
        root = self.base / "root"
        Initializer().initialize(self.request(root, "empty"))
        with self.assertRaises(GitViewspaceError) as identity:
            Initializer().initialize(
                self.request(root, "empty", git_email="different@example.invalid")
            )
        self.assertEqual(identity.exception.code, "INITIALIZATION_MISMATCH")
        with self.assertRaises(GitViewspaceError) as source:
            Initializer().initialize(self.request(root, "empty", initial_branch="trunk"))
        self.assertEqual(source.exception.code, "INITIALIZATION_MISMATCH")

    def test_strict_config_schema_blocks_idempotent_adoption(self):
        root = self.base / "root"
        request = self.request(root, "empty")
        Initializer().initialize(request)
        config_path = root / "config/manager.json"
        config = read_json(config_path)
        config["unknown"] = True
        atomic_json(config_path, config)
        with self.assertRaises(GitViewspaceError) as caught:
            Initializer().initialize(request)
        self.assertEqual(caught.exception.code, "INVALID_STATE")

    def test_every_durable_boundary_is_retryable(self):
        boundaries = (
            "transaction-created",
            "repository-prepared",
            "repository-claimed",
            "repository-published",
            "root-published",
            "transaction-removed",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                source = self.repository(f"s-{index}")
                root = self.base / f"r-{index}"
                request = self.request(root, "copy", source)

                def fault(name):
                    if name == boundary:
                        raise InjectedFailure(boundary)

                with self.assertRaises(InjectedFailure):
                    Initializer(fault=fault).initialize(request)
                result = Initializer().initialize(request)
                self.assert_layout(root)
                self.assertIn(result.idempotent, (True, False))

    def test_move_resumes_after_repository_preparation(self):
        source = self.repository()
        root = self.base / "root"
        request = self.request(root, "move", source)

        def fault(name):
            if name == "repository-prepared":
                raise InjectedFailure(name)

        with self.assertRaises(InjectedFailure):
            Initializer(fault=fault).initialize(request)
        self.assertFalse(source.exists())
        Initializer().initialize(request)
        self.assert_layout(root)

    def test_cli_json_success_and_usage_error(self):
        root = self.base / "root"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run(
                (
                    "--root",
                    str(root),
                    "--json",
                    "init",
                    "--git-name",
                    "Managed User",
                    "--git-email",
                    "managed@example.invalid",
                    "--empty",
                )
            )
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run(("--root", str(self.base / "other"), "--json", "init"))
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
