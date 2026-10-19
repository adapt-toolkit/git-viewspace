# SPDX-License-Identifier: Apache-2.0

import errno
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

from git_viewspace_fs.gitoper import GitOperError, GitOperations, NodeType
from pygit2 import GitError


PROJECT_DIR = Path(__file__).resolve().parents[2]
TEST_REPO = PROJECT_DIR / "test_repo"


def git_output(*arguments, repository=TEST_REPO):
    return subprocess.check_output(
        ["git", "-C", os.fspath(repository), *arguments],
        text=True,
    ).strip()


class GitOperationsTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = GitOperations(os.fspath(TEST_REPO))
        cls.commit = git_output("rev-parse", "HEAD")
        cls.previous_commit = git_output("rev-parse", "HEAD^")

    def assert_git_error(self, expected_errno, function, *arguments):
        with self.assertRaises(GitOperError) as caught:
            function(*arguments)
        self.assertEqual(caught.exception.errno, expected_errno)
        return caught.exception

    def test_resolve_commit_accepts_only_an_exact_full_commit_id(self):
        self.assertEqual(self.git.resolve_commit(self.commit), self.commit)

        rejected = (
            "",
            self.commit[:12],
            self.commit.upper(),
            f" {self.commit}",
            f"{self.commit}\n",
            "0" * 40,
            "f" * 64,
            "not-an-object-id",
            "HEAD",
            "refs/heads/master",
            git_output("rev-parse", "HEAD^{tree}"),
            git_output("rev-parse", "HEAD:file_a"),
            git_output("rev-parse", "tannotated^{tag}"),
            None,
            b"not-a-string",
            17,
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assert_git_error(errno.ENOENT, self.git.resolve_commit, value)

    def test_unreachable_commit_object_is_still_a_valid_commit(self):
        commit = git_output("config", "--get", "viewspace-test.danglingCommit")
        self.assertEqual(self.git.resolve_commit(commit), commit)
        self.assertEqual(self.git.node_type(commit, ""), NodeType.DIRECTORY)

    def test_missing_pinned_commit_is_an_io_error(self):
        self.assert_git_error(
            errno.EIO,
            self.git.node_type,
            "0" * 40,
            "",
        )

    def test_pygit_repository_errors_are_translated_to_io_errors(self):
        class BrokenRepository:
            def __getitem__(self, _object_id):
                raise GitError("simulated object database failure")

        operations = GitOperations(os.fspath(TEST_REPO))
        with patch.object(operations, "_pygit", BrokenRepository()):
            resolve_error = self.assert_git_error(
                errno.EIO,
                operations.resolve_commit,
                self.commit,
            )
            pinned_error = self.assert_git_error(
                errno.EIO,
                operations.node_type,
                self.commit,
                "",
            )

        self.assertIsInstance(resolve_error.__cause__, GitError)
        self.assertIsInstance(pinned_error.__cause__, GitError)

    def test_corrupt_commit_tree_is_reported_as_an_io_error(self):
        class CorruptCommit:
            tree = object()

        operations = GitOperations(os.fspath(TEST_REPO))
        with patch.object(operations, "_commit", return_value=CorruptCommit()):
            self.assert_git_error(
                errno.EIO,
                operations.node_type,
                self.commit,
                "",
            )

    def test_tree_entry_lookup_and_type_corruption_are_io_errors(self):
        operations = GitOperations(os.fspath(TEST_REPO))
        real_repository = operations._pygit
        commit = self.commit

        class BrokenEntryRepository:
            @property
            def odb(self):
                return self

            def read_header(self, object_id):
                raise GitError("simulated tree entry lookup failure")

            def __getitem__(self, object_id):
                if str(object_id) == commit:
                    return real_repository[object_id]
                raise GitError("simulated tree entry lookup failure")

        with patch.object(operations, "_pygit", BrokenEntryRepository()):
            lookup_error = self.assert_git_error(
                errno.EIO,
                operations.node_type,
                self.commit,
                "file_a",
            )
        self.assertIsInstance(lookup_error.__cause__, GitError)

        operations = GitOperations(os.fspath(TEST_REPO))
        real_repository = operations._pygit

        class WrongEntryTypeRepository:
            @property
            def odb(self):
                return self

            def read_header(self, object_id):
                return real_repository.odb.read_header(real_repository[commit].id)

            def __getitem__(self, object_id):
                if str(object_id) == commit:
                    return real_repository[object_id]
                return real_repository[commit]

        with patch.object(operations, "_pygit", WrongEntryTypeRepository()):
            self.assert_git_error(
                errno.EIO,
                operations.node_type,
                self.commit,
                "file_a",
            )

    def test_node_types_cover_all_supported_git_modes(self):
        expected = {
            "": NodeType.DIRECTORY,
            "dir_a": NodeType.DIRECTORY,
            "file_a": NodeType.REGULAR,
            "empty": NodeType.REGULAR,
            "executable.sh": NodeType.EXECUTABLE,
            "link_relative": NodeType.SYMLINK,
        }
        for path, node_type in expected.items():
            with self.subTest(path=path):
                self.assertEqual(self.git.node_type(self.commit, path), node_type)

    def test_directory_contents_cover_root_nested_and_literal_metadata(self):
        root = set(self.git.directory_contents(self.commit, ""))
        self.assertTrue(
            {
                "file_a",
                "empty",
                "executable.sh",
                "dir_a",
                "gitlink",
                ".author",
                ".author-email",
                ".git-descendants",
                ".git-names",
                ".git-parents",
            }.issubset(root)
        )
        self.assertEqual(
            set(self.git.directory_contents(self.commit, "dir_a")),
            {"dir_b", "file_aa", "link_parent"},
        )
        self.assertEqual(
            self.git.directory_contents(self.commit, "dir_a/dir_b/dir_c"),
            ["file_ca"],
        )
        self.assertEqual(
            self.git.directory_contents(self.commit, ".git-parents"),
            ["literal-parent"],
        )

    def test_regular_and_executable_file_contents_and_sizes(self):
        expected = {
            "file_a": b"Contents\n",
            "empty": b"",
            "executable.sh": b"#!/bin/sh\necho executable\n",
            "dir_a/dir_b/dir_c/file_ca": b"nested\n",
            ".author": b"literal author file\n",
            ".git-parents/literal-parent": b"repository content\n",
        }
        for path, contents in expected.items():
            with self.subTest(path=path):
                self.assertEqual(self.git.file_contents(self.commit, path), contents)
                self.assertEqual(self.git.file_size(self.commit, path), len(contents))

    def test_successful_commit_and_entry_lookups_are_cached(self):
        operations = GitOperations(os.fspath(TEST_REPO))
        self.assertEqual(operations.node_type(self.commit, "file_a"), NodeType.REGULAR)
        self.assertEqual(operations.file_size(self.commit, "file_a"), 9)
        self.assertEqual(operations.file_contents(self.commit, "file_a"), b"Contents\n")
        self.assertEqual(operations._cached_commit.cache_info().misses, 1)
        # One miss for the root tree and one for file_a; later operations hit.
        self.assertEqual(operations._cached_entry.cache_info().misses, 2)
        self.assertGreaterEqual(operations._cached_entry.cache_info().hits, 2)

    def test_failed_entry_lookup_is_not_cached(self):
        operations = GitOperations(os.fspath(TEST_REPO))
        for _ in range(2):
            self.assert_git_error(
                errno.ENOENT,
                operations.node_type,
                self.commit,
                "missing",
            )
        self.assertEqual(operations._cached_entry.cache_info().misses, 3)

    def test_sibling_entries_share_the_cached_parent_tree(self):
        operations = GitOperations(os.fspath(TEST_REPO))
        operations.node_type(self.commit, "dir_a/file_aa")
        misses = operations._cached_entry.cache_info().misses
        operations.node_type(self.commit, "dir_a/link_parent")
        self.assertEqual(operations._cached_entry.cache_info().misses, misses + 1)

    def test_symlink_targets_are_returned_verbatim(self):
        expected = {
            "link_relative": "file_a",
            "link_absolute": "/absolute/target",
            "link_parent": "../outside-the-view",
            "link_dangling": "does-not-exist",
            "dir_a/link_parent": "../file_a",
        }
        for path, target in expected.items():
            with self.subTest(path=path):
                self.assertEqual(self.git.symlink_target(self.commit, path), target)
                self.assertEqual(
                    self.git.file_size(self.commit, path),
                    len(target.encode("utf-8")),
                )

    def test_invalid_symlink_target_encodings_are_io_errors(self):
        self.assert_git_error(
            errno.EIO,
            self.git.symlink_target,
            self.commit,
            "link_invalid_utf8",
        )
        self.assert_git_error(
            errno.EIO,
            self.git.symlink_target,
            self.commit,
            "link_nul",
        )

    def test_gitlinks_are_rejected_as_unsupported_entries(self):
        self.assert_git_error(
            errno.EIO,
            self.git.node_type,
            self.commit,
            "gitlink",
        )
        self.assert_git_error(
            errno.EIO,
            self.git.file_size,
            self.commit,
            "gitlink",
        )

    def test_path_validation_rejects_absolute_and_redundant_paths(self):
        rejected = (
            "/file_a",
            "./file_a",
            "dir_a//file_aa",
            "dir_a/./file_aa",
            "dir_a/../file_a",
            "dir_a/",
            None,
            b"file_a",
        )
        for path in rejected:
            with self.subTest(path=path):
                self.assert_git_error(
                    errno.ENOENT,
                    self.git.node_type,
                    self.commit,
                    path,
                )

    def test_path_and_operation_failures_preserve_errno_distinctions(self):
        cases = (
            (errno.ENOENT, self.git.node_type, (self.commit, "missing")),
            (errno.ENOENT, self.git.node_type, (self.commit, "dir_a/missing")),
            (errno.ENOTDIR, self.git.node_type, (self.commit, "file_a/child")),
            (errno.ENOTDIR, self.git.directory_contents, (self.commit, "file_a")),
            (
                errno.ENOTDIR,
                self.git.directory_contents,
                (self.commit, "link_relative"),
            ),
            (errno.EISDIR, self.git.file_contents, (self.commit, "dir_a")),
            (errno.EISDIR, self.git.file_size, (self.commit, "dir_a")),
            (errno.EINVAL, self.git.file_contents, (self.commit, "link_relative")),
            (errno.EINVAL, self.git.symlink_target, (self.commit, "file_a")),
            (errno.EINVAL, self.git.symlink_target, (self.commit, "dir_a")),
        )
        for expected_errno, function, arguments in cases:
            with self.subTest(function=function.__name__, arguments=arguments):
                self.assert_git_error(expected_errno, function, *arguments)

    def test_commit_and_author_timestamps_are_exact_and_distinct(self):
        expected_commit_time = int(git_output("show", "-s", "--format=%ct", "HEAD"))
        expected_author_time = int(git_output("show", "-s", "--format=%at", "HEAD"))
        self.assertNotEqual(expected_commit_time, expected_author_time)
        self.assertEqual(self.git.get_commit_time(self.commit), expected_commit_time)
        self.assertEqual(self.git.get_author_time(self.commit), expected_author_time)

    def test_empty_repository_can_be_opened_without_enumerating_commits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            empty_repo = Path(temporary_directory) / "empty.git"
            subprocess.run(
                ["git", "init", "--bare", os.fspath(empty_repo)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            operations = GitOperations(os.fspath(empty_repo))
            self.assert_git_error(
                errno.ENOENT,
                operations.resolve_commit,
                "0" * 40,
            )

    def test_bare_repository_exposes_exact_commits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bare_repo = Path(temporary_directory) / "bare.git"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--bare",
                    os.fspath(TEST_REPO),
                    os.fspath(bare_repo),
                ],
                check=True,
            )
            operations = GitOperations(os.fspath(bare_repo))
            self.assertEqual(operations.resolve_commit(self.commit), self.commit)
            self.assertEqual(
                operations.file_contents(self.commit, "version.txt"),
                b"new\n",
            )

    def test_linked_worktree_can_be_opened(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            clone = Path(temporary_directory) / "clone"
            worktree = Path(temporary_directory) / "worktree"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    os.fspath(TEST_REPO),
                    os.fspath(clone),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    os.fspath(clone),
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    os.fspath(worktree),
                    self.previous_commit,
                ],
                check=True,
            )
            operations = GitOperations(os.fspath(worktree))
            self.assertEqual(
                operations.resolve_commit(self.previous_commit),
                self.previous_commit,
            )
            self.assertEqual(
                operations.file_contents(self.previous_commit, "version.txt"),
                b"old\n",
            )


if __name__ == "__main__":
    main()
