# SPDX-License-Identifier: Apache-2.0

import errno
from unittest import TestCase, main

from git_viewspace_fs.gitoper import GitOperError, NodeType
from git_viewspace_fs.handlers.root import RootHandler


class RootHandlerTestCase(TestCase):
    def setUp(self):
        self.handler = RootHandler()

    def assert_git_error(self, expected_errno, function):
        with self.assertRaises(GitOperError) as caught:
            function()
        self.assertEqual(caught.exception.errno, expected_errno)

    def test_root_contains_only_the_views_namespace(self):
        self.assertEqual(self.handler.node_type(), NodeType.DIRECTORY)
        self.assertEqual(self.handler.readdir(), ["views"])
        self.assertIsNone(self.handler.get_commit())

    def test_root_rejects_file_and_symlink_operations(self):
        self.assert_git_error(errno.EISDIR, self.handler.file_contents)
        self.assert_git_error(errno.EINVAL, self.handler.symlink_target)


if __name__ == "__main__":
    main()
