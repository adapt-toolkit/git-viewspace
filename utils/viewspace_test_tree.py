#!/usr/bin/env python3
"""Driver-neutral exact filesystem-tree support for VIEWSPACE test drivers."""

from __future__ import annotations

import ctypes
import dataclasses
import difflib
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
from typing import Callable
import zipfile
import zlib


MAX_TREE_ENTRIES = 131_072
MAX_TREE_ENTRY_BYTES = 256 * 1024 * 1024
MAX_TREE_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_SYMLINK_TARGET_BYTES = 4096


class TreeError(Exception):
    """A malformed tree, unsafe archive, or tree I/O failure."""


@dataclasses.dataclass(frozen=True)
class TreeEntry:
    path: bytes
    kind: str
    contents: bytes = b""
    executable: bool = False


TreeSnapshot = tuple[TreeEntry, ...]


def display_path(path: bytes) -> str:
    """Render arbitrary Linux path bytes without losing or interpreting bytes."""

    return repr(path)


def snapshot_directory(root: Path) -> TreeSnapshot:
    """Capture exact paths, node types, and file bytes below a stable directory.

    Traversal is descriptor-relative and every open uses ``O_NOFOLLOW``.  Identity and
    mutation metadata are checked around reads so a concurrent path/type swap fails the
    snapshot instead of being followed or producing a silently mixed tree.
    """

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

    def identity(node_stat: os.stat_result) -> tuple[int, int, int]:
        return (node_stat.st_dev, node_stat.st_ino, stat.S_IFMT(node_stat.st_mode))

    def stability(node_stat: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        return (
            node_stat.st_dev,
            node_stat.st_ino,
            node_stat.st_mode,
            node_stat.st_size,
            node_stat.st_mtime_ns,
            node_stat.st_ctime_ns,
            node_stat.st_nlink,
        )

    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as error:
        raise TreeError(f"could not open fixture tree {root} without following links: {error}") from error

    entries: list[TreeEntry] = []
    captured_bytes = 0

    def visit(directory_descriptor: int, prefix: bytes) -> None:
        nonlocal captured_bytes
        try:
            before_directory = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(before_directory.st_mode):
                raise TreeError(
                    f"fixture entry changed type during snapshot: {display_path(prefix)}"
                )
            with os.scandir(directory_descriptor) as scanned:
                names = sorted((os.fsencode(entry.name) for entry in scanned))
        except TreeError:
            raise
        except OSError as error:
            rendered = display_path(prefix) if prefix else repr(os.fspath(root))
            raise TreeError(f"could not inspect fixture directory {rendered}: {error}") from error

        for name in names:
            if len(entries) >= MAX_TREE_ENTRIES:
                raise TreeError(f"fixture tree exceeds {MAX_TREE_ENTRIES} entries")
            relative = name if not prefix else prefix + b"/" + name
            try:
                before_entry = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise TreeError(
                    f"fixture entry changed during snapshot {display_path(relative)}: {error}"
                ) from error

            if stat.S_ISDIR(before_entry.st_mode):
                try:
                    child_descriptor = os.open(
                        name,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise TreeError(
                        f"could not safely open fixture directory {display_path(relative)}: {error}"
                    ) from error
                try:
                    opened_entry = os.fstat(child_descriptor)
                    if identity(opened_entry) != identity(before_entry):
                        raise TreeError(
                            f"fixture directory changed during snapshot: {display_path(relative)}"
                        )
                    entries.append(TreeEntry(relative, "directory"))
                    visit(child_descriptor, relative)
                    if stability(os.fstat(child_descriptor)) != stability(opened_entry):
                        raise TreeError(
                            f"fixture directory changed during snapshot: {display_path(relative)}"
                        )
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(before_entry.st_mode):
                if before_entry.st_size > MAX_TREE_ENTRY_BYTES:
                    raise TreeError(
                        f"fixture file exceeds {MAX_TREE_ENTRY_BYTES} bytes: "
                        f"{display_path(relative)}"
                    )
                if captured_bytes + before_entry.st_size > MAX_TREE_TOTAL_BYTES:
                    raise TreeError(f"fixture tree exceeds {MAX_TREE_TOTAL_BYTES} bytes")
                try:
                    file_descriptor = os.open(
                        name,
                        file_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise TreeError(
                        f"could not safely open fixture file {display_path(relative)}: {error}"
                    ) from error
                try:
                    opened_entry = os.fstat(file_descriptor)
                    if (
                        not stat.S_ISREG(opened_entry.st_mode)
                        or identity(opened_entry) != identity(before_entry)
                    ):
                        raise TreeError(
                            f"fixture file changed during snapshot: {display_path(relative)}"
                        )
                    chunks = []
                    while True:
                        chunk = os.read(file_descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    after_entry = os.fstat(file_descriptor)
                    if stability(after_entry) != stability(opened_entry):
                        raise TreeError(
                            f"fixture file changed during snapshot: {display_path(relative)}"
                        )
                    contents = b"".join(chunks)
                    if len(contents) != opened_entry.st_size:
                        raise TreeError(
                            f"fixture file size changed during snapshot: {display_path(relative)}"
                        )
                    entries.append(
                        TreeEntry(
                            relative,
                            "file",
                            contents,
                            bool(opened_entry.st_mode & 0o111),
                        )
                    )
                    captured_bytes += len(contents)
                except OSError as error:
                    raise TreeError(
                        f"could not read fixture file {display_path(relative)}: {error}"
                    ) from error
                finally:
                    os.close(file_descriptor)
            elif stat.S_ISLNK(before_entry.st_mode):
                try:
                    target = os.readlink(name, dir_fd=directory_descriptor)
                    after_entry = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise TreeError(
                        f"could not safely read fixture symlink {display_path(relative)}: {error}"
                    ) from error
                if stability(after_entry) != stability(before_entry):
                    raise TreeError(
                        f"fixture symlink changed during snapshot: {display_path(relative)}"
                    )
                encoded_target = os.fsencode(target)
                if len(encoded_target) > MAX_SYMLINK_TARGET_BYTES:
                    raise TreeError(
                        f"fixture symlink target exceeds {MAX_SYMLINK_TARGET_BYTES} bytes: "
                        f"{display_path(relative)}"
                    )
                entries.append(TreeEntry(relative, "symlink", encoded_target))
            else:
                raise TreeError(
                    "fixture trees support only regular files, directories, and symlinks: "
                    + display_path(relative)
                )

        try:
            after_directory = os.fstat(directory_descriptor)
        except OSError as error:
            raise TreeError("fixture directory disappeared during snapshot") from error
        if stability(after_directory) != stability(before_directory):
            rendered = display_path(prefix) if prefix else repr(os.fspath(root))
            raise TreeError(f"fixture directory changed during snapshot: {rendered}")

    try:
        opened_root = os.fstat(root_descriptor)
        visit(root_descriptor, b"")
        try:
            current_root = root.lstat()
        except OSError as error:
            raise TreeError(f"fixture tree changed during snapshot {root}: {error}") from error
        if identity(current_root) != identity(opened_root):
            raise TreeError(f"fixture tree changed during snapshot: {root}")
    finally:
        os.close(root_descriptor)
    return tuple(entries)


def _zip_entry_parts(info: zipfile.ZipInfo) -> tuple[tuple[bytes, ...], bool]:
    encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
    try:
        name = info.orig_filename.encode(encoding)
    except UnicodeEncodeError as error:
        raise TreeError(f"ZIP entry name disagrees with its encoding flags: {info.filename!r}") from error
    if not name or b"\x00" in name:
        raise TreeError("ZIP entry has an invalid name")
    if name.startswith(b"/"):
        raise TreeError(f"ZIP entry is absolute: {name!r}")
    is_directory = name.endswith(b"/")
    stripped = name[:-1] if is_directory else name
    parts = tuple(stripped.split(b"/"))
    if not stripped or any(part in (b"", b".", b"..") for part in parts):
        raise TreeError(f"ZIP entry has an unsafe path: {name!r}")
    return parts, is_directory


def _ensure_empty_directory(root: Path) -> None:
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise TreeError(f"destination is not a directory: {root}")
        with os.scandir(root) as scanned:
            if next(scanned, None) is not None:
                raise TreeError(f"destination is not empty: {root}")
    except TreeError:
        raise
    except OSError as error:
        raise TreeError(f"could not inspect destination {root}: {error}") from error


def _clear_directory(root: Path) -> None:
    try:
        with os.scandir(root) as scanned:
            entries = list(scanned)
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
    except OSError as error:
        raise TreeError(f"could not clean failed tree materialization in {root}: {error}") from error


def _remove_path(path: Path) -> None:
    try:
        path_stat = path.lstat()
        if stat.S_ISDIR(path_stat.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise TreeError(f"could not remove temporary replacement {path}: {error}") from error


def extract_zip_safely(archive_path: Path, destination: Path) -> None:
    """Extract files, directories, and symlinks without trusting ZIP paths."""

    _ensure_empty_directory(destination)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            planned: dict[tuple[bytes, ...], tuple[str, bool, zipfile.ZipInfo]] = {}
            total_bytes = 0
            for info in archive.infolist():
                if len(planned) >= MAX_TREE_ENTRIES:
                    raise TreeError(f"ZIP tree exceeds {MAX_TREE_ENTRIES} entries")
                if info.flag_bits & 0x1:
                    raise TreeError(f"encrypted ZIP entries are unsupported: {info.filename!r}")
                parts, name_says_directory = _zip_entry_parts(info)
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                    raise TreeError(
                        f"ZIP entry has an unsupported type: {info.filename!r}"
                    )
                mode_says_directory = file_type == stat.S_IFDIR
                if file_type and mode_says_directory != name_says_directory:
                    raise TreeError(f"ZIP entry type disagrees with its name: {info.filename!r}")
                if file_type == stat.S_IFLNK and name_says_directory:
                    raise TreeError(f"ZIP symlink name ends with a slash: {info.filename!r}")
                if name_says_directory and info.file_size != 0:
                    raise TreeError(f"ZIP directory entry contains a payload: {info.filename!r}")
                kind = (
                    "directory"
                    if name_says_directory
                    else "symlink"
                    if file_type == stat.S_IFLNK
                    else "file"
                )
                if info.file_size > MAX_TREE_ENTRY_BYTES:
                    raise TreeError(
                        f"ZIP entry exceeds {MAX_TREE_ENTRY_BYTES} bytes: {info.filename!r}"
                    )
                total_bytes += info.file_size
                if total_bytes > MAX_TREE_TOTAL_BYTES:
                    raise TreeError(f"ZIP tree exceeds {MAX_TREE_TOTAL_BYTES} bytes")
                if kind == "symlink" and info.file_size > MAX_SYMLINK_TARGET_BYTES:
                    raise TreeError(
                        f"ZIP symlink target exceeds {MAX_SYMLINK_TARGET_BYTES} bytes: "
                        f"{info.filename!r}"
                    )
                permissions = stat.S_IMODE(mode)
                if kind == "file" and permissions & ~0o777:
                    raise TreeError(
                        f"ZIP regular file has unsupported permissions: {info.filename!r}"
                    )
                executable = kind == "file" and bool(permissions & 0o111)
                if parts in planned:
                    raise TreeError(f"duplicate ZIP entry: {info.filename!r}")
                planned[parts] = (kind, executable, info)

            for parts, (kind, _, info) in planned.items():
                for length in range(1, len(parts)):
                    parent = planned.get(parts[:length])
                    if parent is not None and parent[0] != "directory":
                        raise TreeError(
                            f"ZIP non-directory is an ancestor of another entry: {info.filename!r}"
                        )
                if kind != "directory" and any(
                    len(other) > len(parts) and other[: len(parts)] == parts for other in planned
                ):
                    raise TreeError(
                        f"ZIP non-directory is an ancestor of another entry: {info.filename!r}"
                    )

            destination_bytes = os.fsencode(destination)
            for parts, (kind, _, _) in sorted(
                planned.items(), key=lambda item: (len(item[0]), item[0])
            ):
                if kind == "directory":
                    os.makedirs(os.path.join(destination_bytes, *parts), exist_ok=True)
            for parts, (kind, executable, info) in sorted(planned.items()):
                if kind == "directory":
                    with archive.open(info, "r") as input_file:
                        if input_file.read(1):
                            raise TreeError(
                                f"ZIP directory entry contains a payload: {info.filename!r}"
                            )
                    continue
                output_path = os.path.join(destination_bytes, *parts)
                parent = os.path.dirname(output_path)
                os.makedirs(parent, exist_ok=True)
                if kind == "symlink":
                    with archive.open(info, "r") as input_file:
                        target = input_file.read()
                    if not target or b"\0" in target:
                        raise TreeError(f"ZIP symlink has an invalid target: {info.filename!r}")
                    os.symlink(target, output_path)
                    continue
                with archive.open(info, "r") as input_file, open(output_path, "xb") as output_file:
                    shutil.copyfileobj(input_file, output_file)
                os.chmod(output_path, 0o755 if executable else 0o644)
    except BaseException as original_error:
        try:
            _clear_directory(destination)
        except TreeError as cleanup_error:
            raise TreeError(f"{original_error}; {cleanup_error}") from original_error
        if isinstance(original_error, TreeError):
            raise
        if isinstance(original_error, Exception):
            raise TreeError(
                f"could not read fixture ZIP {archive_path}: {original_error}"
            ) from original_error
        raise


def snapshot_source(source: Path, *, temporary_root: Path | None = None) -> TreeSnapshot:
    """Snapshot a directory or ZIP archive as an equivalent logical tree."""

    try:
        source_stat = source.lstat()
    except OSError as error:
        raise TreeError(f"could not inspect tree source {source}: {error}") from error
    if stat.S_ISDIR(source_stat.st_mode):
        return snapshot_directory(source)
    if not stat.S_ISREG(source_stat.st_mode):
        raise TreeError(f"tree source is not a regular file or directory: {source}")

    with tempfile.TemporaryDirectory(prefix="z-", dir=temporary_root) as temporary_name:
        temporary = Path(temporary_name)
        extract_zip_safely(source, temporary)
        return snapshot_directory(temporary)


def _validate_snapshot_path(path: bytes) -> tuple[bytes, ...]:
    if not path or path.startswith(b"/"):
        raise TreeError(f"invalid snapshot path: {display_path(path)}")
    parts = tuple(path.split(b"/"))
    if any(part in (b"", b".", b"..") for part in parts):
        raise TreeError(f"invalid snapshot path: {display_path(path)}")
    return parts


def _validate_snapshot(snapshot: TreeSnapshot) -> list[tuple[TreeEntry, tuple[bytes, ...]]]:
    if len(snapshot) > MAX_TREE_ENTRIES:
        raise TreeError(f"snapshot exceeds {MAX_TREE_ENTRIES} entries")
    seen: set[bytes] = set()
    validated: list[tuple[TreeEntry, tuple[bytes, ...]]] = []
    total_bytes = 0
    for entry in snapshot:
        if not isinstance(entry, TreeEntry):
            raise TreeError(f"invalid snapshot entry: {entry!r}")
        if type(entry.path) is not bytes or type(entry.contents) is not bytes:
            raise TreeError("snapshot paths and contents must be bytes")
        parts = _validate_snapshot_path(entry.path)
        if entry.path in seen:
            raise TreeError(f"duplicate snapshot path: {display_path(entry.path)}")
        seen.add(entry.path)
        if type(entry.executable) is not bool:
            raise TreeError("snapshot executable flag must be boolean")
        if entry.kind not in ("directory", "file", "symlink"):
            raise TreeError(f"invalid snapshot node kind: {entry.kind!r}")
        if entry.kind == "directory" and entry.contents:
            raise TreeError(f"snapshot directory has contents: {display_path(entry.path)}")
        if entry.kind != "file" and entry.executable:
            raise TreeError(
                f"snapshot non-file is marked executable: {display_path(entry.path)}"
            )
        if entry.kind == "symlink" and (not entry.contents or b"\0" in entry.contents):
            raise TreeError(f"snapshot symlink has an invalid target: {display_path(entry.path)}")
        if len(entry.contents) > (
            MAX_SYMLINK_TARGET_BYTES if entry.kind == "symlink" else MAX_TREE_ENTRY_BYTES
        ):
            raise TreeError(f"snapshot entry is too large: {display_path(entry.path)}")
        total_bytes += len(entry.contents)
        if total_bytes > MAX_TREE_TOTAL_BYTES:
            raise TreeError(f"snapshot exceeds {MAX_TREE_TOTAL_BYTES} bytes")
        validated.append((entry, parts))

    kinds = {entry.path: entry.kind for entry, _ in validated}
    for entry, parts in validated:
        for length in range(1, len(parts)):
            parent = b"/".join(parts[:length])
            parent_kind = kinds.get(parent)
            if parent_kind in ("file", "symlink"):
                raise TreeError(
                    f"snapshot non-directory is an ancestor of another entry: {display_path(entry.path)}"
                )
            if parent_kind is None:
                raise TreeError(
                    f"snapshot omits parent directory of {display_path(entry.path)}"
                )
    return validated


def materialize_snapshot(snapshot: TreeSnapshot, root: Path) -> None:
    """Write a validated snapshot into an existing empty directory."""

    _ensure_empty_directory(root)
    validated = _validate_snapshot(snapshot)

    root_bytes = os.fsencode(root)
    try:
        for entry, parts in sorted(validated, key=lambda item: (len(item[1]), item[0].path)):
            if entry.kind == "directory":
                os.mkdir(os.path.join(root_bytes, *parts))
        for entry, parts in sorted(validated, key=lambda item: item[0].path):
            if entry.kind == "symlink":
                os.symlink(entry.contents, os.path.join(root_bytes, *parts))
                continue
            if entry.kind != "file":
                continue
            output_path = os.path.join(root_bytes, *parts)
            with open(output_path, "xb") as output_file:
                output_file.write(entry.contents)
            os.chmod(output_path, 0o755 if entry.executable else 0o644)
    except BaseException as original_error:
        try:
            _clear_directory(root)
        except TreeError as cleanup_error:
            raise TreeError(f"{original_error}; {cleanup_error}") from original_error
        if isinstance(original_error, TreeError):
            raise
        if isinstance(original_error, OSError):
            raise TreeError(f"could not materialize snapshot in {root}: {original_error}") from original_error
        raise


def snapshot_zip_bytes(snapshot: TreeSnapshot) -> bytes:
    """Encode a tree as one deterministic, uncompressed, path-byte-exact ZIP archive."""

    validated = _validate_snapshot(snapshot)
    local_records: list[bytes] = []
    central_records: list[bytes] = []
    offset = 0
    dos_date_1980_01_01 = 0x21
    for entry, _ in sorted(validated, key=lambda item: item[0].path):
        name = entry.path + (b"/" if entry.kind == "directory" else b"")
        if len(name) > 0xFFFF:
            raise TreeError(f"ZIP entry name is too long: {display_path(entry.path)}")
        contents = entry.contents
        if len(contents) > 0xFFFFFFFF:
            raise TreeError(f"ZIP entry is too large: {display_path(entry.path)}")
        checksum = zlib.crc32(contents) & 0xFFFFFFFF
        local = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            0,
            0,
            dos_date_1980_01_01,
            checksum,
            len(contents),
            len(contents),
            len(name),
            0,
        ) + name + contents
        mode = (
            0o040755
            if entry.kind == "directory"
            else 0o120777
            if entry.kind == "symlink"
            else 0o100755
            if entry.executable
            else 0o100644
        )
        central = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            (3 << 8) | 20,
            20,
            0,
            0,
            0,
            dos_date_1980_01_01,
            checksum,
            len(contents),
            len(contents),
            len(name),
            0,
            0,
            0,
            0,
            mode << 16,
            offset,
        ) + name
        local_records.append(local)
        central_records.append(central)
        offset += len(local)
        if offset > 0xFFFFFFFF:
            raise TreeError("ZIP archive is too large")

    central_offset = offset
    central_directory = b"".join(central_records)
    if len(central_directory) > 0xFFFFFFFF or len(validated) > 0xFFFF:
        raise TreeError("ZIP archive requires unsupported ZIP64 encoding")
    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        len(validated),
        len(validated),
        len(central_directory),
        central_offset,
        0,
    )
    return b"".join(local_records) + central_directory + end


def diff_snapshots(expected: TreeSnapshot, actual: TreeSnapshot) -> str:
    """Describe exact tree differences; equality itself never relies on hashes."""

    _validate_snapshot(expected)
    _validate_snapshot(actual)
    expected_by_path = {entry.path: entry for entry in expected}
    actual_by_path = {entry.path: entry for entry in actual}
    lines = ["--- expected filesystem\n", "+++ actual filesystem\n"]
    for path in sorted(set(expected_by_path) | set(actual_by_path)):
        expected_entry = expected_by_path.get(path)
        actual_entry = actual_by_path.get(path)
        rendered = display_path(path)
        if expected_entry is None:
            lines.append(f"+ {actual_entry.kind} {rendered}\n")
        elif actual_entry is None:
            lines.append(f"- {expected_entry.kind} {rendered}\n")
        elif expected_entry.kind != actual_entry.kind:
            lines.append(f"- {expected_entry.kind} {rendered}\n")
            lines.append(f"+ {actual_entry.kind} {rendered}\n")
        elif expected_entry.executable != actual_entry.executable:
            lines.append(
                f"- {'executable' if expected_entry.executable else 'non-executable'} "
                f"file {rendered}\n"
            )
            lines.append(
                f"+ {'executable' if actual_entry.executable else 'non-executable'} "
                f"file {rendered}\n"
            )
        elif expected_entry.contents != actual_entry.contents:
            expected_digest = hashlib.sha256(expected_entry.contents).hexdigest()
            actual_digest = hashlib.sha256(actual_entry.contents).hexdigest()
            try:
                expected_text = expected_entry.contents.decode("utf-8")
                actual_text = actual_entry.contents.decode("utf-8")
                if any(
                    ord(character) < 0x20 and character not in "\t\n\r"
                    for character in expected_text + actual_text
                ):
                    raise ValueError("control bytes require binary diagnostics")
            except (UnicodeDecodeError, ValueError):
                shared = min(len(expected_entry.contents), len(actual_entry.contents))
                first = next(
                    (
                        index
                        for index in range(shared)
                        if expected_entry.contents[index] != actual_entry.contents[index]
                    ),
                    shared,
                )
                start = max(0, first - 8)
                end = first + 9
                lines.append(
                    f"- binary {rendered} {len(expected_entry.contents)} bytes "
                    f"sha256={expected_digest}\n"
                )
                lines.append(
                    f"+ binary {rendered} {len(actual_entry.contents)} bytes "
                    f"sha256={actual_digest}\n"
                )
                lines.append(f"! first differing offset: {first}\n")
                lines.append(
                    f"- bytes[{start}:{end}] {expected_entry.contents[start:end].hex()}\n"
                )
                lines.append(
                    f"+ bytes[{start}:{end}] {actual_entry.contents[start:end].hex()}\n"
                )
            else:
                lines.extend(
                    difflib.unified_diff(
                        expected_text.splitlines(keepends=True),
                        actual_text.splitlines(keepends=True),
                        fromfile=f"expected/{rendered}",
                        tofile=f"actual/{rendered}",
                    )
                )
    return "".join(lines)


def _rename_exchange(first: Path, second: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise TreeError("atomic directory exchange is unavailable on this Linux system")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    if renameat2(
        at_fdcwd,
        os.fsencode(first),
        at_fdcwd,
        os.fsencode(second),
        rename_exchange,
    ) != 0:
        error_number = ctypes.get_errno()
        if error_number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise TreeError("atomic directory exchange is unsupported by this filesystem")
        raise TreeError(
            f"could not atomically exchange {first} and {second}: {os.strerror(error_number)}"
        )


def atomic_replace_directory(target: Path, populate: Callable[[Path], None]) -> None:
    """Populate then atomically install a complete directory on Linux."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        try:
            target_stat = target.lstat()
        except OSError as error:
            raise TreeError(f"could not inspect replacement target {target}: {error}") from error
        if not stat.S_ISDIR(target_stat.st_mode):
            raise TreeError(f"atomic directory replacement target is not a directory: {target}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    installed = False
    try:
        populate(temporary)
        try:
            temporary_stat = temporary.lstat()
        except OSError as error:
            raise TreeError(f"could not inspect populated replacement directory: {error}") from error
        if not stat.S_ISDIR(temporary_stat.st_mode):
            raise TreeError("replacement population did not leave a directory")
        if os.path.lexists(target):
            _rename_exchange(temporary, target)
        else:
            os.replace(temporary, target)
            installed = True
    except BaseException:
        if os.path.lexists(temporary):
            _remove_path(temporary)
        raise
    if not installed:
        try:
            _remove_path(temporary)
        except TreeError as error:
            raise TreeError(
                f"new directory was installed but old directory cleanup failed: {error}"
            ) from error
