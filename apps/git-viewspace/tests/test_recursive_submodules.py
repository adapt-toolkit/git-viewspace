"""Recursive pinned modules: real Git objects, no network dependencies."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_viewspace import submodules as sm
from git_viewspace.errors import GitViewspaceError
from git_viewspace.git import Git
from git_viewspace.protocol import request, validate_request
from git_viewspace_fs.gitoper import GitOperations
from tests.test_submodules import SubmoduleTests


class RecursiveSubmoduleTests(unittest.TestCase):
    git = staticmethod(SubmoduleTests.git)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recursive-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.new_repo("root")
        self.leaf = self.new_repo("leaf")
        (self.leaf / "payload").write_text("old leaf\n")
        (self.leaf / "exec").write_text("#!/bin/sh\n")
        (self.leaf / "exec").chmod(0o755)
        (self.leaf / "link").symlink_to("payload")
        self.git(self.leaf, "add", ".")
        self.old_leaf = self.commit(self.leaf)
        (self.leaf / "payload").write_text("new leaf\n")
        self.git(self.leaf, "add", ".")
        self.new_leaf = self.commit(self.leaf)
        self.middle = self.new_repo("middle")
        self.old_middle = self.link(self.middle, "deps/leaf", "../leaf", self.old_leaf)
        self.new_middle = self.link(self.middle, "deps/leaf", "../leaf", self.new_leaf)
        self.outer = self.new_repo("outer")
        self.old_outer = self.link(self.outer, "lib/middle", "../middle", self.old_middle)
        self.new_outer = self.link(self.outer, "lib/middle", "../middle", self.new_middle)
        self.snapshot = self.link(self.repo, "code/library", str(self.outer), self.old_outer)
        self.payload = "code/library/lib/middle/deps/leaf/payload"

    def new_repo(self, name):
        repo = self.base / name
        self.git(repo, "init", "-b", "main", repo)
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        return repo

    def commit(self, repo):
        self.git(repo, "commit", "--allow-empty", "-m", "fixture")
        return self.git(repo, "rev-parse", "HEAD")

    def link(self, repo, path, url, commit):
        self.git(repo, "config", "--file", ".gitmodules", "submodule." + path + ".path", path)
        self.git(repo, "config", "--file", ".gitmodules", "submodule." + path + ".url", url)
        self.git(repo, "add", ".gitmodules")
        self.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},{path}")
        (repo / path).mkdir(parents=True, exist_ok=True)
        return self.commit(repo)

    def prepare(self, **kwargs):
        return sm.prepare(self.repo, self.snapshot, "code/library", recursive=True, **kwargs)

    def assert_code(self, code, operation):
        with self.assertRaises(GitViewspaceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_three_levels_pins_offline_reads_and_cache(self):
        self.assert_code("SUBMODULE_NOT_PREPARED", lambda: sm.require_available(self.repo, self.snapshot))
        result = self.prepare()
        self.assertEqual((result["fetched"], result["occurrences"]), (3, 3))
        reader = GitOperations(str(self.repo))
        with mock.patch.object(sm, "fetch_exact", side_effect=AssertionError("implicit fetch")):
            sm.require_available(self.repo, self.snapshot)
            self.assertEqual(reader.file_contents(self.snapshot, self.payload), b"old leaf\n")
            leafpath = self.payload.rsplit("/", 1)[0]
            self.assertEqual(sorted(reader.directory_contents(self.snapshot, leafpath)), ["exec", "link", "payload"])
            self.assertEqual(reader.node_type(self.snapshot, leafpath + "/exec").value, "executable")
            self.assertEqual(reader.node_type(self.snapshot, leafpath + "/link").value, "symlink")
            reused = self.prepare()
            self.assertEqual((reused["fetched"], reused["reused"]), (0, 3))
        self.git(self.repo, "branch", "next")
        self.prepare(commit=self.new_outer)
        changed = sm.pin(self.repo, "next", "code/library", self.new_outer, "update", ("Test", "test@example.invalid"))
        self.assertEqual(reader.file_contents(changed["commit"], self.payload), b"new leaf\n")
        self.assertEqual(reader.file_contents(self.snapshot, self.payload), b"old leaf\n")
        # Warm lookup must not revalidate entire module trees or invoke Git.
        with mock.patch.object(Git, "run", side_effect=AssertionError("warm read ran Git")):
            self.assertEqual(reader.file_contents(self.snapshot, self.payload), b"old leaf\n")
        self.assert_code("SUBMODULE_READ_ONLY", lambda: sm.protect_sync(self.repo, self.snapshot, [self.payload]))
        self.assert_code("UNSUPPORTED_SUBMODULE", lambda: sm.prepare(self.repo, self.snapshot, "code/library"))

    def test_partial_failure_retry_retains_objects_and_branch(self):
        shutil.move(self.leaf, self.base / "absent-leaf")
        reports = []
        self.assert_code("SUBMODULE_FETCH_FAILED", lambda: self.prepare(progress=reports.append))
        self.assertEqual(reports[-1]["phase"], "failed")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.snapshot)
        outerstore = sm.store_path(self.repo, str(self.outer))
        self.assertEqual(self.git(outerstore, "rev-parse", "refs/pins/" + self.old_outer), self.old_outer)
        self.git(self.repo, "branch", "next")
        self.assert_code("SUBMODULE_NOT_PREPARED", lambda: sm.pin(self.repo, "next", "code/library", self.old_outer, "pin", ("T", "t@t")))
        shutil.move(self.base / "absent-leaf", self.leaf)
        result = self.prepare()
        self.assertEqual((result["fetched"], result["reused"]), (1, 2))
        # Availability checks must not retain a stale success after object loss.
        shutil.rmtree(sm.store_path(self.repo, str(self.leaf)))
        self.assert_code("SUBMODULE_NOT_PREPARED", lambda: sm.require_available(self.repo, self.snapshot))

    def test_diamond_deduplicates_fetch_not_occurrences(self):
        self.link(self.outer, "lib/middle", "../middle", self.old_middle)
        outer = self.link(self.outer, "second", "../middle", self.old_middle)
        self.snapshot = self.link(self.repo, "code/library", str(self.outer), outer)
        result = self.prepare()
        self.assertEqual((result["fetched"], result["occurrences"]), (3, 5))
        with mock.patch.object(sm, "MAX_OCCURRENCES", 4):
            self.assert_code("SUBMODULE_LIMIT", lambda: sm.require_available(self.repo, self.snapshot))
        with mock.patch.object(sm, "MAX_DEPTH", 2):
            self.assert_code("SUBMODULE_LIMIT", self.prepare)

    def test_same_dependency_at_two_commits(self):
        self.link(self.outer, "lib/middle", "../middle", self.old_middle)
        outer = self.link(self.outer, "second", "../middle", self.new_middle)
        self.snapshot = self.link(self.repo, "code/library", str(self.outer), outer)
        result = self.prepare()
        self.assertEqual((result["fetched"], result["occurrences"]), (5, 5))
        reader = GitOperations(str(self.repo))
        self.assertEqual(reader.file_contents(self.snapshot, self.payload), b"old leaf\n")
        self.assertEqual(reader.file_contents(self.snapshot, "code/library/second/deps/leaf/payload"), b"new leaf\n")

    def test_relative_root_base_survives_origin_change_and_move(self):
        self.git(self.repo, "remote", "add", "origin", str(self.base / "root-origin"))
        self.snapshot = self.link(self.repo, "code/library", "../outer", self.old_outer)
        self.prepare()
        record = sm.store_root(self.repo) / "source.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.git(self.repo, "remote", "set-url", "origin", str(self.base / "different/root"))
        moved = self.base / "moved-root"
        shutil.move(self.repo, moved)
        sm.require_available(moved, self.snapshot)
        self.assertEqual(GitOperations(str(moved)).file_contents(self.snapshot, self.payload), b"old leaf\n")

    def test_originless_relative_base_is_frozen(self):
        self.snapshot = self.link(self.repo, "code/library", "../outer", self.old_outer)
        self.prepare()
        self.assertEqual(sm.root_url(self.repo), str(self.repo))
        self.git(self.repo, "remote", "add", "origin", "https://invalid.example/elsewhere/root")
        sm.require_available(self.repo, self.snapshot)
        self.assertEqual(GitOperations(str(self.repo)).file_contents(self.snapshot, self.payload), b"old leaf\n")

    def test_same_commit_different_parent_urls(self):
        # Identical parent objects, relative child URLs: lookup must use each URL.
        other = self.base / "other"
        other.mkdir()
        shutil.copytree(self.outer, other / "outer")
        shutil.copytree(self.middle, other / "middle")
        shutil.copytree(self.leaf, other / "leaf")
        snapshot = self.link(self.repo, "other", str(other / "outer"), self.old_outer)
        sm.prepare(self.repo, snapshot, "other", recursive=True)
        self.prepare()
        reader = GitOperations(str(self.repo))
        self.assertEqual(reader.file_contents(snapshot, self.payload), b"old leaf\n")
        otherpath = "other/lib/middle/deps/leaf/payload"
        self.assertEqual(reader.file_contents(snapshot, otherpath), b"old leaf\n")
        self.assertNotEqual(reader._entry(snapshot, self.payload).content_key,
                            reader._entry(snapshot, otherpath).content_key)

    def test_invalid_registration_and_cycle(self):
        self.git(self.outer, "rm", ".gitmodules")
        invalid = self.commit(self.outer)
        self.assert_code("INVALID_SUBMODULE", lambda: self.prepare(commit=invalid))
        fake = {"loop": {"path": "loop", "url": str(self.outer), "commit": self.old_outer}}
        with mock.patch.object(sm, "validate_store"), mock.patch.object(sm, "resolved_entries", return_value=fake):
            self.assert_code("SUBMODULE_CYCLE", lambda: sm._Closure(self.repo).walk(fake))

    def test_missing_blob_refuses_preparation_pin_and_mount_preflight(self):
        self.prepare()
        store = sm.store_path(self.repo, str(self.outer))
        missing = "1" * 40
        tree = subprocess.check_output(
            ["git", "-C", str(store), "mktree", "--missing"],
            input=f"100644 blob {missing}\tpayload\n".encode(),
        ).decode().strip()
        bad = Git().run(("-C", store, "commit-tree", tree, "-m", "missing blob"),
                        identity=("Test", "test@example.invalid")).stdout.decode().strip()
        self.assert_code("SUBMODULE_NOT_PREPARED", lambda: self.prepare(commit=bad))
        self.git(self.repo, "branch", "next")
        self.assert_code("SUBMODULE_NOT_PREPARED", lambda: sm.pin(
            self.repo, "next", "code/library", bad, "invalid", ("Test", "test@example.invalid")))
        self.assertEqual(self.git(self.repo, "rev-parse", "next"), self.snapshot)
        snapshot = self.link(self.repo, "code/library", str(self.outer), bad)
        self.assert_code("SUBMODULE_NOT_PREPARED", lambda: sm.require_available(self.repo, snapshot))

    def test_metadata_deadline_and_progress(self):
        reports = []
        with mock.patch.object(Git, "run", side_effect=subprocess.TimeoutExpired("git", 0.1)):
            self.assert_code("SUBMODULE_FETCH_TIMEOUT", lambda: self.prepare(progress=reports.append))
        self.assertEqual(reports[-1]["phase"], "failed")
        with mock.patch.object(sm, "PREPARE_SECONDS", -1):
            self.assert_code("SUBMODULE_FETCH_TIMEOUT", self.prepare)
        # A real local Git subprocess receives the timeout, not just fetch.
        helper = self.base / "slow-git"
        helper.write_text("#!/bin/sh\nexec sleep 60\n")
        helper.chmod(0o755)
        import time
        git = sm._BudgetGit(time.monotonic() + 0.1)
        git.executable = str(helper)
        self.assert_code("SUBMODULE_FETCH_TIMEOUT", lambda: git.run(("version",)))


class RecursivePublicContractTests(unittest.TestCase):
    def test_protocol_old_new_and_invalid(self):
        value = request("submodule-prepare", at="main", path="code", commit="")
        self.assertEqual(validate_request(value), value)
        for recursive in (True, False):
            self.assertEqual(validate_request({**value, "recursive": recursive})["recursive"], recursive)
        for recursive in (0, 1, "yes", None):
            with self.assertRaises(GitViewspaceError):
                validate_request({**value, "recursive": recursive})

    def test_url_resolution(self):
        cases = [
            ("../leaf.git", "https://example.org/team/parent.git", "https://example.org/team/leaf.git"),
            ("./leaf", "ssh://git@example.org/team/parent.git", "ssh://git@example.org/team/parent.git/leaf"),
            ("../leaf", "git@example.org:team/parent.git", "git@example.org:team/leaf"),
            ("../leaf", "/tmp/team/parent", "/tmp/team/leaf"),
            ("../leaf", "file:///tmp/team/parent", "file:///tmp/team/leaf"),
        ]
        for relative, base, expected in cases:
            self.assertEqual(sm.resolve_url(relative, base), expected)
        for base in ("https://host/repo", "git@host:/repo", "/repo", "git@host:repo"):
            with self.assertRaises(GitViewspaceError):
                sm.resolve_url("../../child", base)
        for url in ("-bad", "ext::command", "https://example.org/a?secret=yes", "plain-relative", "a\nsecret", "https://[broken"):
            with self.assertRaises(GitViewspaceError):
                sm.resolve_url(url)

    def test_relative_resolution_matches_git_init(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git = SubmoduleTests.git
            git(repo, "init", "-b", "main", repo)
            (repo / ".gitmodules").write_text('[submodule "child"]\npath = child\nurl = ../child.git\n')
            git(repo, "add", ".gitmodules")
            git(repo, "update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",child")
            for base in ("https://example.org/team/parent.git", "git@example.org:team/parent.git", str(repo / "parent.git")):
                git(repo, "config", "remote.origin.url", base)
                for relative in ("../child.git", "./child", "../child/../x", "./../child.git"):
                    git(repo, "config", "--file", ".gitmodules", "submodule.child.url", relative)
                    subprocess.run(["git", "-C", str(repo), "config", "--remove-section", "submodule.child"], capture_output=True)
                    git(repo, "submodule", "init")
                    self.assertEqual(git(repo, "config", "--get", "submodule.child.url"), sm.resolve_url(relative, base))
