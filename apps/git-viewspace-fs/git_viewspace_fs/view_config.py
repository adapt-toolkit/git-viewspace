# SPDX-License-Identifier: Apache-2.0
"""Hot-loaded configuration and process-local pinning for commit views."""

import json
import os
import threading
from stat import S_ISREG
from types import MappingProxyType

from .gitoper import GitOperError


class _InvalidConfiguration(Exception):
    pass


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidConfiguration("duplicate JSON object key")
        result[key] = value
    return result


class ViewRegistry:
    """Load unpinned views afresh and retain successful resolutions forever."""

    def __init__(self, config_path, git_operations):
        self.config_path = os.path.abspath(config_path)
        self._git = git_operations
        self._pinned = {}
        self._lock = threading.RLock()

    @staticmethod
    def _valid_alias(alias):
        if (
            not isinstance(alias, str)
            or alias in ("", ".", "..")
            or "/" in alias
            or "\0" in alias
        ):
            return False
        try:
            return len(alias.encode("utf-8")) <= 255
        except UnicodeEncodeError:
            return False

    def _load_snapshot(self):
        descriptor = None
        try:
            descriptor = os.open(
                self.config_path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK,
            )
            if not S_ISREG(os.fstat(descriptor).st_mode):
                raise _InvalidConfiguration("configuration must be a regular file")

            with os.fdopen(descriptor, "r", encoding="utf-8") as config_file:
                descriptor = None
                raw = json.load(
                    config_file,
                    object_pairs_hook=_object_without_duplicate_keys,
                )

            if not isinstance(raw, dict):
                raise _InvalidConfiguration("configuration must be an object")

            snapshot = {}
            for alias, parameters in raw.items():
                if not self._valid_alias(alias):
                    raise _InvalidConfiguration("invalid view name")
                if not isinstance(parameters, dict) or set(parameters) != {"commit"}:
                    raise _InvalidConfiguration("invalid view parameters")
                if not isinstance(parameters["commit"], str):
                    raise _InvalidConfiguration("commit must be a string")

                commit = self._git.resolve_commit(parameters["commit"])
                # A fresh mapping makes the accepted snapshot independent of
                # the JSON decoder, and the proxy keeps pinned parameters fixed.
                snapshot[alias] = MappingProxyType({"commit": commit})

            return snapshot
        except (
            GitOperError,
            OSError,
            ValueError,
            RecursionError,
            OverflowError,
            _InvalidConfiguration,
        ):
            return None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def resolve(self, alias):
        """Return immutable parameters for *alias*, pinning on first success."""

        with self._lock:
            pinned = self._pinned.get(alias)
            if pinned is not None:
                return pinned

            snapshot = self._load_snapshot()
            if snapshot is None or alias not in snapshot:
                return None

            self._pinned[alias] = snapshot[alias]
            return self._pinned[alias]

    def names(self):
        """Return the sorted union of pinned and currently configured names."""

        with self._lock:
            snapshot = self._load_snapshot()
            names = set(self._pinned)
            if snapshot is not None:
                names.update(snapshot)
            return sorted(names)
