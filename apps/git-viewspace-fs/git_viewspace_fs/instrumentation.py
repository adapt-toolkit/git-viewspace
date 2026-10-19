# SPDX-License-Identifier: Apache-2.0
"""Optional append-only JSONL instrumentation for Stage 0 measurements."""

import json
import os
import stat
import threading


class JsonlInstrumentation:
    """Write ordered operation counters to a caller-owned regular file."""

    def __init__(self, path, filesystem_source, owner_uid, owner_gid):
        self.path = os.path.abspath(path)
        self.filesystem_source = filesystem_source
        self._lock = threading.Lock()
        self._sequence = 0
        self._counts = {}
        self._descriptor = None

        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CLOEXEC
            | os.O_NONBLOCK
            | os.O_NOCTTY
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("instrumentation log must be a regular file")
            if metadata.st_nlink != 1:
                raise ValueError("instrumentation log must have exactly one link")
            if metadata.st_size != 0:
                raise ValueError("instrumentation log must start empty")
            if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
                raise ValueError(
                    "instrumentation log must be owned by the workload UID/GID"
                )
            if not metadata.st_mode & stat.S_IWUSR:
                raise ValueError("instrumentation log must be owner-writable")
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def emit(self, event, **fields):
        """Append one event with a total ordering and cumulative event count."""

        with self._lock:
            if self._descriptor is None:
                raise ValueError("instrumentation log is closed")
            self._sequence += 1
            event_count = self._counts.get(event, 0) + 1
            self._counts[event] = event_count
            record = {
                **fields,
                "event": event,
                "event_count": event_count,
                "filesystem_source": self.filesystem_source,
                "sequence": self._sequence,
            }
            encoded = (
                json.dumps(
                    record,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(self._descriptor, remaining)
                if written == 0:
                    raise OSError("instrumentation log write made no progress")
                remaining = remaining[written:]

    def close(self):
        with self._lock:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
