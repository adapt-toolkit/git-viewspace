# SPDX-License-Identifier: Apache-2.0
"""Handler for the configured ``/views`` namespace."""

import errno

from ..gitoper import GitOperError, NodeType


class ViewHandler:
    def __init__(self, path, registry, git_operations):
        self._registry = registry
        self._git = git_operations
        self._commit = None
        self._tree_path = ""

        if not path:
            return

        alias, separator, tree_path = path.partition("/")
        parameters = self._registry.resolve(alias)
        if parameters is None:
            raise GitOperError(errno.ENOENT, "view does not exist")

        self._commit = parameters["commit"]
        self._tree_path = tree_path if separator else ""

    def get_commit(self):
        return self._commit

    def node_type(self):
        if self._commit is None:
            return NodeType.DIRECTORY
        return self._git.node_type(self._commit, self._tree_path)

    def readdir(self):
        if self._commit is None:
            return self._registry.names()
        return self._git.directory_contents(self._commit, self._tree_path)

    def file_contents(self):
        if self._commit is None:
            raise GitOperError(errno.EISDIR, "path is a directory")
        return self._git.file_contents(self._commit, self._tree_path)

    def file_size(self):
        if self._commit is None:
            raise GitOperError(errno.EISDIR, "path is a directory")
        return self._git.file_size(self._commit, self._tree_path)

    def symlink_target(self):
        if self._commit is None:
            raise GitOperError(errno.EINVAL, "path is not a symbolic link")
        return self._git.symlink_target(self._commit, self._tree_path)
