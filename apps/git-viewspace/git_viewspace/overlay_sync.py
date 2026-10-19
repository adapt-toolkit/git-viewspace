#!/usr/bin/python3
"""Replay a stopped OverlayFS upper directory onto a copy of its lower tree.

This is the Stage-5 algorithm prototype for the qualified git-viewspace
OverlayFS profile.  It deliberately understands only the closed positive
grammar documented in ../spec/OverlayFS-artifact-format.md.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PRIVATE_PREFIX = "trusted.overlay."
ORIGIN = PRIVATE_PREFIX + "origin"
IMPURE = PRIVATE_PREFIX + "impure"
OPAQUE = PRIVATE_PREFIX + "opaque"
REDIRECT = PRIVATE_PREFIX + "redirect"
RECOGNIZED_PRIVATE = frozenset({ORIGIN, IMPURE, OPAQUE, REDIRECT})
IGNORED_XATTRS = frozenset(
    {"system.posix_acl_access", "system.posix_acl_default"}
)


class SyncError(RuntimeError):
    """A stopped upper or target is outside the supported contract."""


@dataclass(frozen=True)
class Entry:
    parts: tuple[str, ...]
    kind: str
    mode: int
    uid: int
    gid: int
    nlink: int
    rdev: int
    xattrs: dict[str, bytes]
    symlink_target: str | None = None
    children: tuple["Entry", ...] = ()
    redirect_source: tuple[str, ...] | None = None

    @property
    def display_path(self) -> str:
        return "." if not self.parts else "/".join(self.parts)

    @property
    def opaque(self) -> bool:
        return OPAQUE in self.xattrs


@dataclass(frozen=True)
class Inventory:
    root: Entry
    by_path: dict[tuple[str, ...], Entry]
    redirects: tuple[Entry, ...]
    ordinary_uid: int
    ordinary_gid: int


def _strict_utf8(value: str, description: str) -> None:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise SyncError(f"{description} is not valid UTF-8") from exc


def _validate_component(name: str) -> None:
    _strict_utf8(name, "path component")
    if name in {"", ".", ".."} or "/" in name or "\x00" in name:
        raise SyncError(f"invalid path component: {name!r}")
    if name == ".git":
        raise SyncError("a .git path component is outside the replay data tree")


def _path(root: Path, parts: tuple[str, ...]) -> Path:
    result = root
    for part in parts:
        _validate_component(part)
        result = result / part
    return result


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise SyncError(f"cannot lstat {path}: {exc}") from exc


def _read_xattrs(path: Path) -> dict[str, bytes]:
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise SyncError(f"cannot list xattrs on {path}: {exc}") from exc
    result: dict[str, bytes] = {}
    for name in sorted(names):
        _strict_utf8(name, f"xattr name on {path}")
        try:
            result[name] = os.getxattr(path, name, follow_symlinks=False)
        except OSError as exc:
            raise SyncError(f"cannot read xattr {name!r} on {path}: {exc}") from exc
    return result


def _validate_origin(value: bytes, where: str) -> None:
    # Exactly two profiles are qualified: the 33-byte git-viewspace-fs/FUSE handle
    # frozen by Stage 0 and the 29-byte ext4 handle observed in this requested
    # plain-directory-lower suite. Handle payloads remain opaque and are never
    # used as replay identity.
    fuse_prefix = bytes.fromhex(
        "00fb2100810000000000000000000000000000000000000000"
    )
    if len(value) == 33 and value[:25] == fuse_prefix and any(value[25:]):
        return
    if (
        len(value) == 29
        and value[:5] == bytes.fromhex("00fb1d0001")
        and any(value[5:21])
        and any(value[21:])
    ):
        return
    raise SyncError(f"{where}: origin is not a qualified FUSE or ext4 file handle")


def _parse_redirect(value: bytes, destination: tuple[str, ...], where: str) -> tuple[str, ...]:
    try:
        text = value.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SyncError(f"{where}: redirect is not valid UTF-8") from exc
    if not text or text == "/" or "\x00" in text or "//" in text:
        raise SyncError(f"{where}: malformed redirect {text!r}")
    absolute = text.startswith("/")
    raw_parts = text[1:].split("/") if absolute else text.split("/")
    for part in raw_parts:
        _validate_component(part)
    source = tuple(raw_parts) if absolute else destination[:-1] + tuple(raw_parts)
    if not source:
        raise SyncError(f"{where}: redirect resolves to the overlay root")
    return source


def _validate_xattrs(kind: str, xattrs: dict[str, bytes], where: str) -> None:
    for name in xattrs:
        if name in RECOGNIZED_PRIVATE or name in IGNORED_XATTRS or name.startswith("user."):
            continue
        if name.startswith(PRIVATE_PREFIX):
            raise SyncError(f"{where}: unknown OverlayFS xattr {name!r}")
        raise SyncError(f"{where}: unsupported xattr {name!r}")

    private = frozenset(name for name in xattrs if name in RECOGNIZED_PRIVATE)
    if kind == "directory":
        allowed = {
            frozenset(),
            frozenset({IMPURE}),
            frozenset({ORIGIN}),
            frozenset({ORIGIN, IMPURE}),
            frozenset({OPAQUE}),
            frozenset({IMPURE, OPAQUE}),
            frozenset({ORIGIN, REDIRECT}),
            frozenset({ORIGIN, IMPURE, REDIRECT}),
        }
    elif kind == "regular":
        allowed = {frozenset(), frozenset({ORIGIN})}
    elif kind == "symlink":
        # A lower symlink rename on the requested plain-ext4 test profile
        # copies the symlink up with an origin handle.  Stage 0's FUSE lower
        # did not produce this form, but it is ordinary OverlayFS origin
        # bookkeeping and has no replay semantics of its own.
        allowed = {frozenset(), frozenset({ORIGIN})}
    else:
        allowed = {frozenset()}
    if private not in allowed:
        raise SyncError(f"{where}: unsupported OverlayFS xattr combination {sorted(private)!r}")
    if IMPURE in xattrs and xattrs[IMPURE] != b"y":
        raise SyncError(f"{where}: impure marker is not exactly b'y'")
    if OPAQUE in xattrs and xattrs[OPAQUE] != b"y":
        raise SyncError(f"{where}: opaque marker is not exactly b'y'")
    if ORIGIN in xattrs:
        _validate_origin(xattrs[ORIGIN], where)


def _scan_entry(
    root: Path,
    parts: tuple[str, ...],
    ordinary_uid: int,
    ordinary_gid: int,
) -> Entry:
    path = _path(root, parts)
    info = _lstat(path)
    mode = info.st_mode
    xattrs = _read_xattrs(path)
    where = "." if not parts else "/".join(parts)

    if stat.S_ISCHR(mode):
        if (
            stat.S_IMODE(mode) != 0
            or info.st_rdev != 0
            or info.st_uid != 0
            or info.st_gid != 0
            or xattrs
        ):
            raise SyncError(f"{where}: unsupported character device; not a qualified whiteout")
        return Entry(
            parts,
            "whiteout",
            mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_rdev,
            xattrs,
        )

    if info.st_uid != ordinary_uid or info.st_gid != ordinary_gid:
        raise SyncError(
            f"{where}: ordinary entry ownership {info.st_uid}:{info.st_gid} does not match "
            f"upper root {ordinary_uid}:{ordinary_gid}"
        )
    special = stat.S_IMODE(mode) & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    if special:
        raise SyncError(f"{where}: special permission bits are unsupported")

    symlink_target = None
    if stat.S_ISDIR(mode):
        kind = "directory"
        if stat.S_IMODE(mode) & (stat.S_IRUSR | stat.S_IXUSR) != (
            stat.S_IRUSR | stat.S_IXUSR
        ):
            raise SyncError(f"{where}: directory must be owner-readable and searchable")
    elif stat.S_ISREG(mode):
        kind = "regular"
        if info.st_nlink != 1:
            raise SyncError(f"{where}: hard-linked regular files are unsupported")
        if not mode & stat.S_IRUSR:
            raise SyncError(f"{where}: regular file must be owner-readable")
    elif stat.S_ISLNK(mode):
        kind = "symlink"
        if info.st_nlink != 1:
            raise SyncError(f"{where}: hard-linked symlinks are unsupported")
        try:
            symlink_target = os.readlink(path)
        except OSError as exc:
            raise SyncError(f"{where}: cannot read symlink target: {exc}") from exc
        _strict_utf8(symlink_target, f"symlink target at {where}")
        if not symlink_target or "\x00" in symlink_target:
            raise SyncError(f"{where}: symlink target is empty or contains NUL")
    else:
        raise SyncError(f"{where}: unsupported inode type")

    _validate_xattrs(kind, xattrs, where)
    redirect_source = None
    if REDIRECT in xattrs:
        redirect_source = _parse_redirect(xattrs[REDIRECT], parts, where)

    children: tuple[Entry, ...] = ()
    if kind == "directory":
        try:
            names = sorted(item.name for item in os.scandir(path))
        except OSError as exc:
            raise SyncError(f"{where}: cannot scan directory: {exc}") from exc
        for name in names:
            _validate_component(name)
        children = tuple(
            _scan_entry(root, parts + (name,), ordinary_uid, ordinary_gid)
            for name in names
        )
    return Entry(
        parts,
        kind,
        mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_rdev,
        xattrs,
        symlink_target,
        children,
        redirect_source,
    )


def inventory_upper(upper: Path) -> Inventory:
    info = _lstat(upper)
    if not stat.S_ISDIR(info.st_mode):
        raise SyncError("upper-dir must be a real directory, not a symlink")
    root = _scan_entry(upper, (), info.st_uid, info.st_gid)
    by_path: dict[tuple[str, ...], Entry] = {}

    def collect(entry: Entry) -> None:
        if entry.parts in by_path:
            raise SyncError(f"duplicate upper path: {entry.display_path}")
        by_path[entry.parts] = entry
        for child in entry.children:
            collect(child)

    collect(root)
    redirects = tuple(entry for entry in by_path.values() if entry.redirect_source is not None)
    _validate_redirect_set(by_path, redirects)
    return Inventory(root, by_path, redirects, info.st_uid, info.st_gid)


def _fingerprint_field(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def inventory_fingerprint(upper: Path, inventory: Inventory) -> str:
    """Bind a replay plan to exact Git-relevant bytes and OverlayFS metadata."""

    digest = hashlib.sha256()
    digest.update(b"viewspace-overlay-upper-v1\0")
    for entry in sorted(inventory.by_path.values(), key=lambda item: item.parts):
        _fingerprint_field(digest, "/".join(entry.parts).encode("utf-8"))
        _fingerprint_field(digest, entry.kind.encode("ascii"))
        for number in (
            stat.S_IMODE(entry.mode),
            entry.uid,
            entry.gid,
            entry.nlink,
            entry.rdev,
        ):
            _fingerprint_field(digest, str(number).encode("ascii"))
        for name, value in sorted(entry.xattrs.items()):
            _fingerprint_field(digest, name.encode("utf-8"))
            _fingerprint_field(digest, value)
        if entry.symlink_target is not None:
            _fingerprint_field(digest, entry.symlink_target.encode("utf-8"))
        if entry.kind == "regular":
            descriptor = os.open(
                _path(upper, entry.parts),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                content = hashlib.sha256()
                size = 0
                while True:
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    content.update(block)
                    size += len(block)
            finally:
                os.close(descriptor)
            _fingerprint_field(digest, str(size).encode("ascii"))
            _fingerprint_field(digest, content.digest())
    return digest.hexdigest()


def _drop_to_workload(uid: int, gid: int) -> None:
    """Irreversibly become the ordinary owner recorded on the upper root."""

    if uid == 0:
        raise SyncError("upper root must be owned by a non-root workload user")
    effective_uid = os.geteuid()
    effective_gid = os.getegid()
    if effective_uid != 0:
        if effective_uid == uid and effective_gid == gid:
            return
        raise SyncError(
            f"cannot become upper owner {uid}:{gid} from "
            f"unprivileged identity {effective_uid}:{effective_gid}"
        )
    try:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    except OSError as exc:
        raise SyncError(f"cannot drop privileges to upper owner {uid}:{gid}: {exc}") from exc


def _is_nested(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return (
        left == right
        or left[: len(right)] == right
        or right[: len(left)] == left
    )


def _validate_redirect_set(
    by_path: dict[tuple[str, ...], Entry], redirects: tuple[Entry, ...]
) -> None:
    sources: set[tuple[str, ...]] = set()
    for entry in redirects:
        source = entry.redirect_source
        assert source is not None
        if source in sources:
            raise SyncError(f"multiple redirects use source {'/'.join(source)}")
        sources.add(source)
        source_entry = by_path.get(source)
        if source_entry is None or source_entry.kind != "whiteout":
            raise SyncError(
                f"redirect {entry.display_path} lacks its matching source whiteout at {'/'.join(source)}"
            )
        if _is_nested(source, entry.parts):
            raise SyncError(f"redirect source and destination overlap at {entry.display_path}")
    for index, left in enumerate(redirects):
        assert left.redirect_source is not None
        for right in redirects[index + 1 :]:
            assert right.redirect_source is not None
            pairs = (
                (left.parts, right.parts),
                (left.redirect_source, right.redirect_source),
                (left.parts, right.redirect_source),
                (left.redirect_source, right.parts),
            )
            if any(_is_nested(first, second) for first, second in pairs):
                raise SyncError("nested, chained, or overlapping redirects are unsupported")


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove(path: Path) -> None:
    if not _lexists(path):
        return
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        for item in os.scandir(path):
            _remove(path / item.name)
        path.rmdir()
    else:
        path.unlink()


def _copy_git_tree(source: Path, destination: Path, relative: tuple[str, ...] = ()) -> None:
    info = _lstat(source)
    if any(part == ".git" for part in relative):
        raise SyncError(".git is outside the replay data tree")
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        for item in sorted(os.scandir(source), key=lambda child: child.name):
            _validate_component(item.name)
            _copy_git_tree(Path(item.path), destination / item.name, relative + (item.name,))
        return
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise SyncError(f"target lower copy contains a hard link at {'/'.join(relative)}")
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with os.fdopen(source_fd, "rb", closefd=True) as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
        except Exception:
            if source_fd >= 0:
                try:
                    os.close(source_fd)
                except OSError:
                    pass
            raise
        destination.chmod(0o755 if info.st_mode & stat.S_IXUSR else 0o644)
        return
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(source)
        _strict_utf8(target, f"lower symlink target at {'/'.join(relative)}")
        os.symlink(target, destination)
        return
    raise SyncError(f"target lower copy contains unsupported inode at {'/'.join(relative)}")


def _ensure_directory(
    path: Path,
    clear: bool = False,
    preserve: frozenset[str] = frozenset(),
) -> None:
    if _lexists(path):
        if not stat.S_ISDIR(path.lstat().st_mode):
            _remove(path)
            path.mkdir(mode=0o755)
        elif clear:
            for item in os.scandir(path):
                if item.name not in preserve:
                    _remove(path / item.name)
    else:
        path.mkdir(mode=0o755)
    path.chmod(0o755)


def _write_regular(
    source: Path,
    destination: Path,
    executable: bool,
) -> None:
    _remove(destination)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        dest_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    written = os.write(dest_fd, view)
                    view = view[written:]
        finally:
            os.close(dest_fd)
    finally:
        os.close(source_fd)
    destination.chmod(0o755 if executable else 0o644)


def _apply_entry(
    entry: Entry,
    upper: Path,
    result: Path,
    redirect_snapshots: dict[tuple[str, ...], Path],
    *,
    move_redirects: bool = False,
    preserve_root_git: bool = False,
) -> None:
    destination = _path(result, entry.parts)
    source = _path(upper, entry.parts)
    if entry.kind == "whiteout":
        _remove(destination)
        return
    if entry.kind == "regular":
        _write_regular(
            source,
            destination,
            bool(entry.mode & stat.S_IXUSR),
        )
        return
    if entry.kind == "symlink":
        _remove(destination)
        assert entry.symlink_target is not None
        os.symlink(entry.symlink_target, destination)
        return

    if entry.redirect_source is not None:
        _remove(destination)
        snapshot = redirect_snapshots[entry.parts]
        if move_redirects:
            os.rename(snapshot, destination)
        else:
            _copy_git_tree(snapshot, destination, entry.parts)
        if entry.opaque:
            _ensure_directory(destination, clear=True)
    else:
        preserve = (
            frozenset({".git"})
            if preserve_root_git and not entry.parts
            else frozenset()
        )
        _ensure_directory(destination, clear=entry.opaque, preserve=preserve)
    for child in entry.children:
        _apply_entry(
            child,
            upper,
            result,
            redirect_snapshots,
            move_redirects=move_redirects,
            preserve_root_git=preserve_root_git,
        )


def _validate_roots(upper: Path, target: Path) -> tuple[Path, Path]:
    if not upper.is_absolute() or not target.is_absolute():
        raise SyncError("upper-dir and copy-of-lower-dir must be absolute paths")
    if upper == Path("/") or target == Path("/"):
        raise SyncError("refusing to operate on filesystem root")
    upper_info = _lstat(upper)
    target_info = _lstat(target)
    if not stat.S_ISDIR(upper_info.st_mode) or not stat.S_ISDIR(target_info.st_mode):
        raise SyncError("both arguments must be real directories")
    upper_real = Path(os.path.realpath(upper))
    target_real = Path(os.path.realpath(target))
    if upper_real == target_real:
        raise SyncError("upper-dir and target are the same directory")
    common = Path(os.path.commonpath([upper_real, target_real]))
    if common in {upper_real, target_real}:
        raise SyncError("upper-dir and target must not contain one another")
    if os.path.ismount(target_real):
        raise SyncError("copy-of-lower-dir must not itself be a mountpoint")
    _assert_upper_stopped(upper_real)
    _assert_no_mounts_within((upper_real, target_real))
    return upper_real, target_real


def _validate_upper(upper: Path) -> Path:
    if not upper.is_absolute() or upper == Path("/"):
        raise SyncError("upper-dir must be an absolute non-root path")
    info = _lstat(upper)
    if not stat.S_ISDIR(info.st_mode) or upper.is_symlink():
        raise SyncError("upper-dir must be a real directory, not a symlink")
    upper_real = Path(os.path.realpath(upper))
    _assert_upper_stopped(upper_real)
    _assert_no_mounts_within((upper_real,))
    return upper_real


def _mountinfo_unescape(value: str) -> str:
    result = bytearray()
    encoded = value.encode("utf-8", "surrogateescape")
    index = 0
    while index < len(encoded):
        if (
            encoded[index] == 0x5C
            and index + 3 < len(encoded)
            and all(0x30 <= item <= 0x37 for item in encoded[index + 1 : index + 4])
        ):
            result.append(int(encoded[index + 1 : index + 4], 8))
            index += 4
        else:
            result.append(encoded[index])
            index += 1
    return result.decode("utf-8", "surrogateescape")


def _assert_upper_stopped(upper: Path) -> None:
    """Refuse an upper currently named by an OverlayFS mount in this namespace."""

    try:
        records = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SyncError(f"cannot verify stopped-upper state from mountinfo: {exc}") from exc
    for record in records:
        before, separator, after = record.partition(" - ")
        if not separator:
            raise SyncError("malformed /proc/self/mountinfo record")
        fields = after.split()
        if len(fields) < 3 or fields[0] != "overlay":
            continue
        for option in fields[2].split(","):
            if not option.startswith("upperdir="):
                continue
            mounted_upper = Path(_mountinfo_unescape(option.removeprefix("upperdir=")))
            if Path(os.path.realpath(mounted_upper)) == upper:
                raise SyncError(f"upper-dir is still mounted by OverlayFS: {upper}")


def _assert_no_mounts_within(roots: tuple[Path, ...]) -> None:
    try:
        records = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SyncError(f"cannot inspect mount topology: {exc}") from exc
    for record in records:
        fields = record.split()
        if len(fields) < 6:
            raise SyncError("malformed /proc/self/mountinfo record")
        mountpoint = Path(os.path.realpath(_mountinfo_unescape(fields[4])))
        for root in roots:
            if mountpoint == root or root in mountpoint.parents:
                raise SyncError(f"managed tree contains a mountpoint: {mountpoint}")


def _rename_exchange(left: Path, right: Path) -> None:
    """Atomically exchange two names so the target path never disappears."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SyncError("renameat2(RENAME_EXCHANGE) is required")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    if renameat2(
        at_fdcwd,
        os.fsencode(left),
        at_fdcwd,
        os.fsencode(right),
        rename_exchange,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{left} <-> {right}")


def _redirect_snapshots(
    inventory: Inventory,
    source_root: Path,
    stage: Path,
) -> dict[tuple[str, ...], Path]:
    snapshots: dict[tuple[str, ...], Path] = {}
    for index, entry in enumerate(inventory.redirects):
        source_parts = entry.redirect_source
        assert source_parts is not None
        source = _path(source_root, source_parts)
        info = _lstat(source)
        if not stat.S_ISDIR(info.st_mode):
            raise SyncError(
                f"redirect source {'/'.join(source_parts)} is not a directory "
                "in the lower copy"
            )
        snapshot = stage / f"redirect-{index:04d}"
        _copy_git_tree(source, snapshot, source_parts)
        snapshots[entry.parts] = snapshot
    return snapshots


def _sync_direct(upper: Path, target: Path, inventory: Inventory) -> bool:
    """Apply only upper-described changes directly to the target checkout."""

    # Redirect sources must be captured before their paired source whiteouts
    # are applied. Moving those disjoint source directories into a temporary
    # same-filesystem holding directory is O(1); they are moved into their
    # final destinations during direct replay. No file content is copied.
    redirect_stage: Path | None = None
    succeeded = False
    try:
        snapshots: dict[tuple[str, ...], Path] = {}
        if inventory.redirects:
            redirect_stage = Path(
                tempfile.mkdtemp(
                    prefix=f".{target.name}.overlay-sync-redirect-",
                    dir=target.parent,
                )
            )
            for index, entry in enumerate(inventory.redirects):
                source_parts = entry.redirect_source
                assert source_parts is not None
                source = _path(target, source_parts)
                if not stat.S_ISDIR(_lstat(source).st_mode):
                    raise SyncError(
                        f"redirect source {'/'.join(source_parts)} is not a "
                        "directory in the lower copy"
                    )
                snapshot = redirect_stage / f"redirect-{index:04d}"
                os.rename(source, snapshot)
                snapshots[entry.parts] = snapshot
        _apply_entry(
            inventory.root,
            upper,
            target,
            snapshots,
            move_redirects=True,
            preserve_root_git=True,
        )
        succeeded = True
    finally:
        if succeeded and redirect_stage is not None:
            redirect_stage.rmdir()
    return False


def _sync_atomic_swap(upper: Path, target: Path, inventory: Inventory) -> bool:
    """Build and atomically publish a complete replacement target tree."""

    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.overlay-sync-", dir=target.parent))
    result = stage / "result"
    committed = False
    cleanup_pending = False
    try:
        _copy_git_tree(target, result)
        redirect_snapshots = _redirect_snapshots(inventory, result, stage)

        _apply_entry(inventory.root, upper, result, redirect_snapshots)
        _rename_exchange(result, target)
        committed = True
        try:
            _remove(stage)
        except OSError:
            # Publication is already complete and atomic. Retain the old tree
            # for controller-owned cleanup rather than report a false sync
            # failure after the target has changed.
            cleanup_pending = True
    finally:
        if not committed and _lexists(stage):
            _remove(stage)
    return cleanup_pending


def git_pathspecs(inventory: Inventory) -> tuple[str, ...]:
    """Return minimal semantic roots for a targeted ``git add -A``."""

    candidates: set[tuple[str, ...]] = set()
    for entry in inventory.by_path.values():
        if not entry.parts:
            if entry.opaque:
                candidates.add(())
            continue
        if entry.kind != "directory" or entry.opaque:
            candidates.add(entry.parts)
        if entry.redirect_source is not None:
            candidates.add(entry.parts)
            candidates.add(entry.redirect_source)

    # Only ancestors can subsume a path. Comparing every previously retained
    # sibling makes a large publication quadratic in its changed-file count.
    roots = [
        candidate
        for candidate in candidates
        if not any(candidate[:length] in candidates for length in range(len(candidate)))
    ]
    return tuple(sorted("." if not parts else "/".join(parts) for parts in roots))


def _inventory_report(
    upper: Path,
    inventory: Inventory,
    *,
    mode: str,
    cleanup_pending: bool,
) -> dict[str, object]:
    counts = {
        "directories": 0,
        "regulars": 0,
        "symlinks": 0,
        "whiteouts": 0,
        "opaque": 0,
        "origin": 0,
        "impure": 0,
    }
    for entry in inventory.by_path.values():
        key = {
            "directory": "directories",
            "regular": "regulars",
            "symlink": "symlinks",
            "whiteout": "whiteouts",
        }[entry.kind]
        counts[key] += 1
        counts["opaque"] += int(OPAQUE in entry.xattrs)
        counts["origin"] += int(ORIGIN in entry.xattrs)
        counts["impure"] += int(IMPURE in entry.xattrs)
    return {
        "mode": mode,
        "workload_uid": inventory.ordinary_uid,
        "workload_gid": inventory.ordinary_gid,
        "upper_fingerprint": inventory_fingerprint(upper, inventory),
        "git_pathspecs": git_pathspecs(inventory),
        "counts": counts,
        "redirects": [
            {
                "destination": entry.display_path,
                "source": "/".join(entry.redirect_source or ()),
            }
            for entry in inventory.redirects
        ],
        "whiteouts": sorted(
            entry.display_path
            for entry in inventory.by_path.values()
            if entry.kind == "whiteout"
        ),
        "opaque": sorted(
            entry.display_path
            for entry in inventory.by_path.values()
            if entry.opaque
        ),
        "cleanup_pending": cleanup_pending,
    }


def inspect_upper(upper: Path) -> dict[str, object]:
    upper = _validate_upper(upper)
    inventory = inventory_upper(upper)
    return _inventory_report(upper, inventory, mode="inspect", cleanup_pending=False)


def sync_overlay(
    upper: Path,
    target: Path,
    *,
    atomic_swap: bool = False,
) -> dict[str, object]:
    upper, target = _validate_roots(upper, target)
    inventory = inventory_upper(upper)
    # All privileged OverlayFS inspection is now complete. Replay runs as the
    # workload identity so target and staging objects are naturally owned by
    # the caller that will subsequently stage, commit, delete, and restore the
    # checkout. Nothing may mutate the target before this irreversible drop.
    _drop_to_workload(inventory.ordinary_uid, inventory.ordinary_gid)
    if atomic_swap:
        cleanup_pending = _sync_atomic_swap(upper, target, inventory)
        mode = "atomic-swap"
    else:
        cleanup_pending = _sync_direct(upper, target, inventory)
        mode = "direct"

    return _inventory_report(
        upper,
        inventory,
        mode=mode,
        cleanup_pending=cleanup_pending,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay a stopped qualified OverlayFS upper onto a copy of its lower tree."
    )
    parser.add_argument("upper_dir", type=Path)
    parser.add_argument("copy_of_lower_dir", type=Path, nargs="?")
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="validate and fingerprint a stopped upper without replaying it",
    )
    parser.add_argument(
        "--atomic-swap",
        action="store_true",
        help=(
            "build and atomically exchange a complete staged checkout; "
            "the default applies only upper-described changes directly"
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print("overlay_sync.py must run as root so trusted OverlayFS metadata is visible", file=sys.stderr)
        return 2
    try:
        if args.inspect_only:
            if args.copy_of_lower_dir is not None or args.atomic_swap:
                raise SyncError("--inspect-only accepts only upper-dir")
            counts = inspect_upper(args.upper_dir)
        else:
            if args.copy_of_lower_dir is None:
                raise SyncError("copy-of-lower-dir is required for replay")
            counts = sync_overlay(
                args.upper_dir,
                args.copy_of_lower_dir,
                atomic_swap=args.atomic_swap,
            )
    except (SyncError, OSError) as exc:
        print(f"overlay-sync: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(counts, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
