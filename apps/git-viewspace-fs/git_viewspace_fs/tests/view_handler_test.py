# SPDX-License-Identifier: Apache-2.0

import errno
from unittest import TestCase, main
from unittest.mock import Mock

from git_viewspace_fs.gitoper import GitOperError, NodeType
from git_viewspace_fs.handlers.view import ViewHandler


class ViewHandlerTestCase(TestCase):
    def assert_git_error(self, expected_errno, function):
        with self.assertRaises(GitOperError) as caught:
            function()
        self.assertEqual(caught.exception.errno, expected_errno)

    def test_namespace_handler_lists_names_without_resolving_an_alias(self):
        registry = Mock()
        registry.names.return_value = ["alpha", "beta"]
        git_operations = Mock()
        handler = ViewHandler("", registry, git_operations)

        self.assertEqual(handler.node_type(), NodeType.DIRECTORY)
        self.assertEqual(handler.readdir(), ["alpha", "beta"])
        self.assertIsNone(handler.get_commit())
        registry.resolve.assert_not_called()
        self.assertEqual(git_operations.method_calls, [])
        self.assert_git_error(errno.EISDIR, handler.file_contents)
        self.assert_git_error(errno.EISDIR, handler.file_size)
        self.assert_git_error(errno.EINVAL, handler.symlink_target)

    def test_alias_and_nested_tree_path_delegate_to_git_operations(self):
        registry = Mock()
        registry.resolve.return_value = {"commit": "commit-id"}
        git_operations = Mock()
        git_operations.node_type.return_value = NodeType.REGULAR
        git_operations.directory_contents.return_value = ["entry"]
        git_operations.file_contents.return_value = b"contents"
        git_operations.file_size.return_value = 8
        git_operations.symlink_target.return_value = "target"

        handler = ViewHandler("alpha/dir/file", registry, git_operations)
        self.assertEqual(handler.get_commit(), "commit-id")
        self.assertEqual(handler.node_type(), NodeType.REGULAR)
        self.assertEqual(handler.readdir(), ["entry"])
        self.assertEqual(handler.file_contents(), b"contents")
        self.assertEqual(handler.file_size(), 8)
        self.assertEqual(handler.symlink_target(), "target")

        registry.resolve.assert_called_once_with("alpha")
        git_operations.node_type.assert_called_once_with("commit-id", "dir/file")
        git_operations.directory_contents.assert_called_once_with(
            "commit-id",
            "dir/file",
        )
        git_operations.file_contents.assert_called_once_with("commit-id", "dir/file")
        git_operations.file_size.assert_called_once_with("commit-id", "dir/file")
        git_operations.symlink_target.assert_called_once_with("commit-id", "dir/file")

    def test_alias_root_with_trailing_separator_uses_empty_tree_path(self):
        registry = Mock()
        registry.resolve.return_value = {"commit": "commit-id"}
        git_operations = Mock()
        git_operations.node_type.return_value = NodeType.DIRECTORY

        handler = ViewHandler("alpha/", registry, git_operations)
        self.assertEqual(handler.node_type(), NodeType.DIRECTORY)
        git_operations.node_type.assert_called_once_with("commit-id", "")

    def test_unknown_alias_is_not_a_view(self):
        registry = Mock()
        registry.resolve.return_value = None

        with self.assertRaises(GitOperError) as caught:
            ViewHandler("missing/path", registry, Mock())
        self.assertEqual(caught.exception.errno, errno.ENOENT)
        registry.resolve.assert_called_once_with("missing")


if __name__ == "__main__":
    main()
