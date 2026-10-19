import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from git_viewspace.errors import GitViewspaceError
from git_viewspace.git import Git
from git_viewspace.submodules import entries, fetch_exact, pin, prepare, protect_sync, require_available


class SubmoduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sm-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.code = self.base / "code"
        self.repo = self.base / "data"
        for repo in (self.code, self.repo):
            self.git(repo, "init", "-b", "main", repo)
            self.git(repo, "config", "user.name", "Test")
            self.git(repo, "config", "user.email", "test@example.invalid")
        (self.code / "script.py").write_text("first\n")
        self.commit(self.code)
        self.first = self.git(self.code, "rev-parse", "HEAD")
        (self.code / "script.py").write_text("second\n")
        self.commit(self.code)
        self.second = self.git(self.code, "rev-parse", "HEAD")
        (self.repo / ".gitmodules").write_text(
            f'[submodule "code"]\npath = code/library\nurl = {self.code}\n'
        )
        self.git(self.repo, "add", ".gitmodules")
        self.git(
            self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.first},code/library"
        )
        self.git(self.repo, "commit", "-m", "source")
        self.git(self.repo, "checkout", "--", ".")
        # Gitlink placeholders need not contain a nested worktree.
        (self.repo / "code/library").mkdir(parents=True, exist_ok=True)
        self.snapshot = self.git(self.repo, "rev-parse", "HEAD")

    @staticmethod
    def git(repo, *args):
        command = (
            ["git", *map(str, args)]
            if args[0] == "init"
            else ["git", "-C", str(repo), *map(str, args)]
        )
        return subprocess.check_output(command, stderr=subprocess.PIPE, text=True).strip()

    def commit(self, repo):
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "version")

    def test_explicit_acquisition_pin_and_old_snapshot(self):
        from git_viewspace_fs.gitoper import GitOperations

        Git().validate_repository(self.repo, owner_uid=os.getuid())
        with self.assertRaises(GitViewspaceError) as error:
            require_available(self.repo, self.snapshot)
        self.assertEqual(error.exception.code, "SUBMODULE_NOT_PREPARED")
        prepare(self.repo, self.snapshot, "code/library")
        self.git(self.repo, "branch", "next")
        prepare(self.repo, self.snapshot, "code/library", self.second)
        result = pin(
            self.repo,
            "next",
            "code/library",
            self.second,
            "new code",
            ("Test", "test@example.invalid"),
        )
        reader = GitOperations(str(self.repo))
        self.assertEqual(reader.file_contents(self.snapshot, "code/library/script.py"), b"first\n")
        self.assertEqual(
            reader.file_contents(result["commit"], "code/library/script.py"), b"second\n"
        )
        self.assertEqual(entries(self.repo, self.snapshot)["code/library"]["commit"], self.first)
        self.assertEqual(
            reader.directory_contents(result["commit"], "code/library"), ["script.py"]
        )
        # A normal data-only commit preserves the gitlink instead of flattening the module.
        (self.repo / "result.txt").write_text("target\n")
        self.commit(self.repo)
        self.assertEqual(entries(self.repo, "HEAD")["code/library"]["commit"], self.first)
        self.git(self.repo, "merge", "next", "-m", "merge code")
        self.assertEqual(entries(self.repo, "HEAD")["code/library"]["commit"], self.second)

    def test_pin_and_overlay_ownership_boundaries(self):
        prepare(self.repo, self.snapshot, "code/library")
        with self.assertRaises(GitViewspaceError) as error:
            pin(
                self.repo,
                "main",
                "code/library",
                self.first,
                "pin",
                ("Test", "test@example.invalid"),
            )
        self.assertEqual(error.exception.code, "BRANCH_CHECKED_OUT")
        protect_sync(self.repo, self.snapshot, ["datasets/result.json"])
        with self.assertRaises(GitViewspaceError) as error:
            protect_sync(self.repo, self.snapshot, ["code/library/script.py"])
        self.assertEqual(error.exception.code, "SUBMODULE_READ_ONLY")

    def test_fetch_deadline_reaps_owned_process(self):
        helper = self.base / "fake-git"
        pidfile = self.base / "fetch-pid"
        helper.write_text(
            f"#!{sys.executable}\nimport os,time\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
        )
        helper.chmod(0o755)
        with self.assertRaises(GitViewspaceError) as error:
            fetch_exact(Git(executable=helper), self.repo, str(self.code), self.first, timeout=1)
        self.assertEqual(error.exception.code, "SUBMODULE_FETCH_TIMEOUT")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
