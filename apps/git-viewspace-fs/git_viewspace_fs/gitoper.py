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
import re
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from git_viewspace.cache_config import DEFAULT_CONTENT_CACHE_BYTES
from pygit2 import Blob, Commit, GitError, Repository, Tree
from pygit2.enums import FileMode, ObjectType


class NodeType(Enum):
    """The Git tree entry types git-viewspace-fs can expose."""

    DIRECTORY = "directory"
    REGULAR = "regular"
    EXECUTABLE = "executable"
    SYMLINK = "symlink"


@dataclass(frozen=True)
class _ResolvedEntry:
    """Lightweight cached tree-entry metadata and an optional directory tree."""

    filemode: FileMode
    object_id: object
    tree: Tree | None = None
    repository: Repository | None = None
    size: int | None = None
    content_key: tuple[str, str] | None = None


class GitOperations:
    """Read objects from a Git repository without walking its references."""

    def __init__(
        self, repo, instrumentation=None, *, content_cache_bytes=DEFAULT_CONTENT_CACHE_BYTES
    ):
        if type(content_cache_bytes) is not int or content_cache_bytes < 0:
            raise ValueError("content_cache_bytes must be a nonnegative integer")
        self._content_limit = content_cache_bytes
        self._content = OrderedDict()
        self._content_bytes = 0
        self._content_counts = dict(hits=0, misses=0, evictions=0, decoded_bytes=0)
        self._instrumentation = instrumentation
        self._pygit = Repository(repo)
        self._cached_commit = lru_cache(maxsize=128)(self._load_commit)
        self._cached_entry = lru_cache(maxsize=4096)(self._load_entry)
        self._module_registrations = lru_cache(maxsize=128)(self._load_modules)
        self._trace("git_repository_open")

    def cache_stats(self):
        """Snapshot of retained payload and content-read counters (not process RSS)."""
        return dict(
            limit_bytes=self._content_limit,
            resident_bytes=self._content_bytes,
            entries=len(self._content),
            **self._content_counts,
        )

    def _cache_trace(self, event):
        if self._instrumentation is not None:
            self._trace(event, **self.cache_stats())

    def _trace(self, event, **fields):
        if self._instrumentation is not None:
            self._instrumentation.emit(event, **fields)

    def resolve_commit(self, object_id):
        """Return *object_id* iff it names one exact, full commit object ID."""

        self._trace("git_resolve_commit", object_id=object_id)

        if not isinstance(object_id, str) or re.fullmatch(r"[0-9a-f]{40}", object_id) is None:
            raise GitOperError(errno.ENOENT, "commit does not exist")

        try:
            commit = self._pygit[object_id]
        except GitError as error:
            raise GitOperError(errno.EIO, "commit object cannot be read") from error
        except (KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.ENOENT, "commit does not exist") from error

        # Repository.__getitem__ accepts abbreviated object IDs.  Comparing the
        # canonical spelling prevents an alias from silently depending on that
        # abbreviation continuing to be unique.
        if not isinstance(commit, Commit) or str(commit.id) != object_id:
            raise GitOperError(errno.ENOENT, "object is not an exact commit ID")

        return object_id

    def _load_commit(self, object_id):
        self._trace("git_commit_lookup", object_id=object_id)
        try:
            commit = self._pygit[object_id]
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "commit object is unavailable") from error
        if not isinstance(commit, Commit) or str(commit.id) != object_id:
            raise GitOperError(errno.EIO, "pinned object is no longer an exact commit")
        return commit

    def _commit(self, object_id):
        return self._cached_commit(object_id)

    @staticmethod
    def _validate_path(path):
        if not isinstance(path, str) or path.startswith("/"):
            raise GitOperError(errno.ENOENT, "tree path does not exist")
        if path and any(part in ("", ".", "..") for part in path.split("/")):
            raise GitOperError(errno.ENOENT, "tree path does not exist")

    def _load_entry(self, commit, path):
        self._trace(
            "git_tree_path_lookup",
            commit=commit,
            depth=len(path.split("/")) if path else 0,
            path=path,
        )
        if not path:
            commit_object = self._commit(commit)
            try:
                tree = commit_object.tree
            except (GitError, KeyError, TypeError, ValueError) as error:
                raise GitOperError(errno.EIO, "commit tree is unavailable") from error
            if not isinstance(tree, Tree):
                raise GitOperError(errno.EIO, "commit has an invalid tree")
            return _ResolvedEntry(FileMode.TREE, tree.id, tree, self._pygit)

        parent_path, _, name = path.rpartition("/")
        parent = self._cached_entry(commit, parent_path)
        tree = parent.tree
        if not isinstance(tree, Tree):
            raise GitOperError(errno.ENOTDIR, "path prefix is not a directory")
        try:
            entry = tree[name]
        except KeyError as error:
            raise GitOperError(errno.ENOENT, "tree path does not exist") from error
        except (GitError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "Git tree cannot be read") from error

        repository = parent.repository or self._pygit
        if entry.filemode in (FileMode.BLOB, FileMode.BLOB_EXECUTABLE):
            try:
                object_type, size = repository.odb.read_header(entry.id)
            except (GitError, KeyError, TypeError, ValueError) as error:
                raise GitOperError(errno.EIO, "Git blob header is unavailable") from error
            if object_type != ObjectType.BLOB:
                raise GitOperError(errno.EIO, "Git tree entry has an invalid object type")
            return _ResolvedEntry(
                entry.filemode,
                entry.id,
                repository=repository,
                size=size,
                content_key=(str(Path(repository.path).resolve()), str(entry.id)),
            )
        if entry.filemode == FileMode.COMMIT:
            from git_viewspace.errors import GitViewspaceError
            from git_viewspace.submodules import store_path, validate_store

            try:
                item = self._module_registrations(commit)[path]
                store = store_path(self._pygit.path, item["url"])
                validate_store(store, str(entry.id))
                module = Repository(str(store))
                tree = module[entry.id].tree
                return _ResolvedEntry(FileMode.TREE, tree.id, tree, module)
            except (KeyError, ValueError, GitError, GitViewspaceError) as error:
                raise GitOperError(
                    errno.EIO, "pinned module is unavailable or unsupported"
                ) from error
        expected_object_type = {
            FileMode.TREE: Tree,
            FileMode.BLOB: Blob,
            FileMode.BLOB_EXECUTABLE: Blob,
            FileMode.LINK: Blob,
        }.get(entry.filemode)
        if expected_object_type is None:
            raise GitOperError(errno.EIO, "unsupported Git tree entry")
        try:
            obj = repository[entry.id]
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(
                errno.EIO,
                "Git tree entry object is unavailable",
            ) from error
        if not isinstance(obj, expected_object_type):
            raise GitOperError(errno.EIO, "Git tree entry has an invalid object type")
        return _ResolvedEntry(
            entry.filemode,
            entry.id,
            obj if isinstance(obj, Tree) else None,
            repository,
        )

    def _load_modules(self, commit):
        from git_viewspace.submodules import entries

        return entries(Path(self._pygit.path), commit)

    def _entry(self, commit, path):
        self._validate_path(path)
        return self._cached_entry(commit, path)

    def node_type(self, commit, path):
        """Return a supported node type, rejecting gitlinks and corrupt entries."""

        entry = self._entry(commit, path)
        if not path:
            return NodeType.DIRECTORY

        if entry.filemode == FileMode.TREE:
            return NodeType.DIRECTORY
        if entry.filemode == FileMode.BLOB:
            return NodeType.REGULAR
        if entry.filemode == FileMode.BLOB_EXECUTABLE:
            return NodeType.EXECUTABLE
        if entry.filemode == FileMode.LINK:
            return NodeType.SYMLINK

        # Gitlinks can legitimately refer to objects absent from this object
        # database, and unknown modes cannot be represented safely through FUSE.
        raise GitOperError(errno.EIO, "unsupported Git tree entry")

    def directory_contents(self, commit, path):
        self._trace("git_directory_contents", commit=commit, path=path)
        if self.node_type(commit, path) is not NodeType.DIRECTORY:
            raise GitOperError(errno.ENOTDIR, "path is not a directory")

        tree = self._entry(commit, path).tree
        if not isinstance(tree, Tree):
            raise GitOperError(errno.EIO, "Git tree cannot be read")
        try:
            return [entry.name for entry in tree]
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "Git tree cannot be read") from error

    def file_contents(self, commit, path):
        self._trace("git_file_contents", commit=commit, path=path)
        node_type = self.node_type(commit, path)
        if node_type is NodeType.DIRECTORY:
            raise GitOperError(errno.EISDIR, "path is a directory")
        if node_type is NodeType.SYMLINK:
            raise GitOperError(errno.EINVAL, "path is a symbolic link")

        entry = self._entry(commit, path)
        key = entry.content_key
        if key in self._content:
            self._content.move_to_end(key)
            self._content_counts["hits"] += 1
            self._cache_trace("git_content_cache_hit")
            return self._content[key]
        self._content_counts["misses"] += 1
        self._cache_trace("git_content_cache_miss")
        try:
            object_type, contents = entry.repository.odb.read(entry.object_id)
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "Git blob cannot be read") from error
        if object_type != ObjectType.BLOB:
            raise GitOperError(errno.EIO, "Git tree entry has an invalid object type")
        size = len(contents)
        self._content_counts["decoded_bytes"] += size
        if 0 < size <= self._content_limit:
            while self._content_bytes + size > self._content_limit:
                _, evicted = self._content.popitem(last=False)
                self._content_bytes -= len(evicted)
                self._content_counts["evictions"] += 1
                self._cache_trace("git_content_cache_eviction")
            self._content[key] = contents
            self._content_bytes += size
        self._cache_trace("git_content_cache_read")
        return contents

    def file_size(self, commit, path):
        self._trace("git_file_size", commit=commit, path=path)
        node_type = self.node_type(commit, path)
        if node_type is NodeType.DIRECTORY:
            raise GitOperError(errno.EISDIR, "path is a directory")

        entry = self._entry(commit, path)
        if entry.size is not None:
            return entry.size

        blob = self._blob(entry)
        try:
            return blob.size
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "Git blob cannot be read") from error

    def symlink_target(self, commit, path):
        self._trace("git_symlink_target", commit=commit, path=path)
        if self.node_type(commit, path) is not NodeType.SYMLINK:
            raise GitOperError(errno.EINVAL, "path is not a symbolic link")

        try:
            blob = self._blob(self._entry(commit, path))
            target = blob.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GitOperError(errno.EIO, "symbolic-link target is not UTF-8") from error
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "symbolic-link target cannot be read") from error

        if "\0" in target:
            raise GitOperError(errno.EIO, "symbolic-link target contains NUL")
        return target

    def _blob(self, entry):
        try:
            blob = (entry.repository or self._pygit)[entry.object_id]
        except (GitError, KeyError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "Git blob cannot be read") from error
        if not isinstance(blob, Blob):
            raise GitOperError(errno.EIO, "Git tree entry has an invalid object type")
        return blob

    def get_commit_time(self, commit):
        self._trace("git_commit_time", commit=commit)
        try:
            return self._commit(commit).commit_time
        except GitOperError:
            raise
        except (GitError, AttributeError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "commit timestamp cannot be read") from error

    def get_author_time(self, commit):
        self._trace("git_author_time", commit=commit)
        try:
            return self._commit(commit).author.time
        except GitOperError:
            raise
        except (GitError, AttributeError, TypeError, ValueError) as error:
            raise GitOperError(errno.EIO, "author timestamp cannot be read") from error


class GitOperError(Exception):
    """A Git lookup failure carrying the errno exposed through FUSE."""

    def __init__(self, error_number, message):
        super().__init__(message)
        self.errno = error_number
