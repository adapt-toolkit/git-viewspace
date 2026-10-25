"""Linux process and mount observations for git-viewspace."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import GitViewspaceError


CONTROL_SOCKET_MAX_PATH_BYTES = 103


@dataclass(frozen=True)
class Mount:
    mountpoint: Path
    root: str
    filesystem_type: str
    source: str
    mount_options: frozenset[str]
    super_options: frozenset[str]
    mount_id: int | None = None


def process_start_time_ticks(pid=None):
    selected = os.getpid() if pid is None else int(pid)
    try:
        value = Path(f"/proc/{selected}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError, ValueError) as error:
        raise GitViewspaceError("PROCESS_IDENTITY_UNAVAILABLE", "cannot read process identity") from error
    _, separator, fields = value.rpartition(")")
    components = fields.split()
    if not separator or len(components) <= 19:
        raise GitViewspaceError("PROCESS_IDENTITY_UNAVAILABLE", "invalid process identity")
    try:
        start_time = int(components[19], 10)
    except ValueError as error:
        raise GitViewspaceError("PROCESS_IDENTITY_UNAVAILABLE", "invalid process start time") from error
    if start_time <= 0:
        raise GitViewspaceError("PROCESS_IDENTITY_UNAVAILABLE", "invalid process start time")
    return start_time


def process_matches(pid, start_time_ticks):
    try:
        return process_start_time_ticks(pid) == start_time_ticks
    except GitViewspaceError:
        return False


def _unescape_mount(value):
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def mounts(path="/proc/self/mountinfo"):
    result = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise GitViewspaceError("MOUNTINFO_UNAVAILABLE", "cannot inspect Linux mount table") from error
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mountpoint = Path(_unescape_mount(fields[4]))
            root = _unescape_mount(fields[3])
            filesystem_type = fields[separator + 1]
            source = _unescape_mount(fields[separator + 2])
            mount_options = frozenset(fields[5].split(","))
            super_options = frozenset(fields[separator + 3].split(","))
        except (ValueError, IndexError) as error:
            raise GitViewspaceError("MOUNTINFO_UNAVAILABLE", "invalid Linux mount table") from error
        result.append(Mount(mountpoint, root, filesystem_type, source, mount_options, super_options, int(fields[0])))
    return tuple(result)


def indexed_mounts(path="/proc/self/mountinfo"):
    result = {}
    for entry in mounts(path):
        result.setdefault(entry.mountpoint, []).append(entry)
    return {mountpoint: tuple(entries) for mountpoint, entries in result.items()}


def mount_at(path, observed=None):
    selected = Path(path).resolve()
    if observed is not None:
        matching = observed.get(selected, ())
        if len(matching) > 1:
            raise GitViewspaceError("AMBIGUOUS_MOUNT", f"multiple mounts exist at {selected}")
        return matching[0] if matching else None
    matching = [entry for entry in mounts() if entry.mountpoint == selected]
    if len(matching) > 1:
        raise GitViewspaceError("AMBIGUOUS_MOUNT", f"multiple mounts exist at {selected}")
    return matching[0] if matching else None


def filesystem_at(path):
    selected = Path(path).resolve()
    matching = []
    for entry in mounts():
        try:
            selected.relative_to(entry.mountpoint)
        except ValueError:
            continue
        matching.append(entry)
    if not matching:
        raise GitViewspaceError("MOUNTINFO_UNAVAILABLE", f"cannot identify filesystem for {selected}")
    return max(matching, key=lambda entry: len(entry.mountpoint.parts))


def validate_private_mount(path, expected_source, observed=None):
    mounted = mount_at(path, observed)
    if mounted is None:
        return None
    if (
        not mounted.filesystem_type.startswith("fuse")
        or mounted.source != expected_source
        or "ro" not in mounted.mount_options
    ):
        raise GitViewspaceError(
            "FOREIGN_MOUNT",
            f"unexpected mount at {Path(path)}",
            detail=f"type={mounted.filesystem_type} source={mounted.source}",
        )
    return mounted


def validate_socket_path(path, uid, gid):
    path = Path(path)
    metadata = path.lstat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise GitViewspaceError("INVALID_RUNTIME", "control socket has invalid identity or mode")


def validate_control_socket_location(root):
    path = Path(root) / "run/manager.sock"
    length = len(os.fsencode(path))
    if length > CONTROL_SOCKET_MAX_PATH_BYTES:
        raise GitViewspaceError(
            "SOCKET_PATH_TOO_LONG",
            "managed-root path is too long for its control socket",
            detail=(
                f"socket path is {length} bytes; maximum is "
                f"{CONTROL_SOCKET_MAX_PATH_BYTES}: {path}"
            ),
        )
    return path
