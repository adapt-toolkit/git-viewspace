#!/usr/bin/env python
#
# Copyright 2017-2021 Vitalis Salis and Diomidis Spinellis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import errno
import os
from stat import S_IFDIR, S_IFLNK, S_IFREG
from time import time

from fuse import FuseOSError, Operations

from .gitoper import DEFAULT_CONTENT_CACHE_BYTES, GitOperations, GitOperError, NodeType
from .handlers.root import RootHandler
from .handlers.view import ViewHandler
from .view_config import ViewRegistry

MAX_WORKLOAD_ID = (1 << 32) - 2


class GitViewspaceFS(Operations):
    DIRECTORY_MODE = 0o755
    REGULAR_MODE = 0o644
    EXECUTABLE_MODE = 0o755

    def __init__(
        self,
        repo,
        config,
        *,
        workload_uid,
        workload_gid,
        instrumentation=None,
        content_cache_bytes=DEFAULT_CONTENT_CACHE_BYTES,
    ):
        self._validate_workload_id("UID", workload_uid)
        self._validate_workload_id("GID", workload_gid)

        self.repo = os.path.abspath(repo)
        self.config = os.path.abspath(config)
        self.workload_uid = workload_uid
        self.workload_gid = workload_gid
        self._instrumentation = instrumentation
        self._git = GitOperations(
            self.repo, instrumentation=instrumentation, content_cache_bytes=content_cache_bytes
        )
        self._views = ViewRegistry(self.config, self._git)
        self._trace("filesystem_ready")

    @staticmethod
    def _validate_workload_id(name, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 < value <= MAX_WORKLOAD_ID
        ):
            raise ValueError(f"workload {name} must be a non-root Linux numeric ID")

    def _trace(self, event, **fields):
        if self._instrumentation is not None:
            self._instrumentation.emit(event, **fields)

    def _get_handler(self, path):
        if path == "/":
            return RootHandler()
        if path in ("/views", "/views/"):
            return ViewHandler("", self._views, self._git)
        if path.startswith("/views/"):
            return ViewHandler(path[len("/views/") :], self._views, self._git)
        raise FuseOSError(errno.ENOENT)

    @staticmethod
    def _raise_fuse_error(error):
        raise FuseOSError(error.errno) from error

    def get_commit_time(self, commit):
        return self._git.get_commit_time(commit)

    def get_author_time(self, commit):
        return self._git.get_author_time(commit)

    def getattr(self, path, fh=None):
        del fh
        self._trace("fuse_getattr", path=path)
        stat = {
            "st_uid": self.workload_uid,
            "st_gid": self.workload_gid,
        }

        try:
            handler = self._get_handler(path)
            node_type = handler.node_type()

            if node_type is NodeType.DIRECTORY:
                stat["st_mode"] = S_IFDIR | self.DIRECTORY_MODE
                stat["st_nlink"] = 2
            elif node_type is NodeType.SYMLINK:
                stat["st_mode"] = S_IFLNK | 0o777
                stat["st_nlink"] = 1
                stat["st_size"] = len(handler.symlink_target().encode("utf-8"))
            elif node_type in (NodeType.REGULAR, NodeType.EXECUTABLE):
                permissions = (
                    self.REGULAR_MODE if node_type is NodeType.REGULAR else self.EXECUTABLE_MODE
                )
                stat["st_mode"] = S_IFREG | permissions
                stat["st_nlink"] = 1
                stat["st_size"] = handler.file_size()
            else:
                raise FuseOSError(errno.EIO)

            current_time = time()
            stat["st_atime"] = current_time
            stat["st_ctime"] = current_time
            stat["st_mtime"] = current_time

            commit = handler.get_commit()
            if commit:
                stat["st_mtime"] = self.get_commit_time(commit)
                stat["st_ctime"] = self.get_author_time(commit)
            return stat
        except GitOperError as error:
            self._raise_fuse_error(error)

    def readdir(self, path, fh):
        del fh
        self._trace("fuse_readdir", path=path)
        try:
            entries = self._get_handler(path).readdir()
        except GitOperError as error:
            self._raise_fuse_error(error)
        return [".", "..", *entries]

    def read(self, path, size, offset, fh):
        del fh
        self._trace("fuse_read", offset=offset, path=path, size=size)
        try:
            contents = self._get_handler(path).file_contents()
        except GitOperError as error:
            self._raise_fuse_error(error)
        return contents[offset : offset + size]

    def readlink(self, path):
        self._trace("fuse_readlink", path=path)
        try:
            return self._get_handler(path).symlink_target()
        except GitOperError as error:
            self._raise_fuse_error(error)

    statfs = None

    access = None
    chmod = None
    chown = None
    mknod = None
    rmdir = None
    mkdir = None
    unlink = None
    symlink = None
    rename = None
    link = None
    utimens = None
