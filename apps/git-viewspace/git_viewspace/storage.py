"""Small durable filesystem boundary used by Stage 1."""

import json
import os
import shutil
import stat
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path

from .errors import GitViewspaceError


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root):
    """Durably flush a validated regular-file/directory/symlink tree."""

    root = Path(root)
    for base, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(base) / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise GitViewspaceError("INVALID_REPOSITORY", f"unsupported repository entry: {path}")
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for name in directories:
            path = Path(base) / name
            if not path.is_symlink():
                fsync_directory(path)
        fsync_directory(base)


def ensure_directory(path, mode=0o700):
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=mode)
        fsync_directory(path.parent)
        return True
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise GitViewspaceError("INVALID_PATH", f"expected a real directory: {path}")
    return False


def atomic_write(path, payload, mode=0o600):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.fchown(descriptor, -1, os.getgid())
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path, value, mode=0o600):
    atomic_write(path, canonical_json_bytes(value), mode)


def read_json(path):
    path = Path(path)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise GitViewspaceError("INVALID_STATE", f"state is not a regular file: {path}")
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_object_without_duplicates)
    except GitViewspaceError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise GitViewspaceError("INVALID_STATE", f"cannot read state file: {path}") from error


def create_stable_file(path, mode=0o600):
    path = Path(path)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
    except FileExistsError:
        validate_stable_file(path, os.getuid(), os.getgid(), mode)
        return False
    try:
        os.fchown(descriptor, -1, os.getgid())
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)
    return True


def validate_stable_file(path, uid, gid, mode=0o600):
    path = Path(path)
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise GitViewspaceError("INVALID_STATE", f"stable file has invalid identity or mode: {path}")


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GitViewspaceError("ROOT_BUSY", "another git-viewspace process owns the managed root") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def remove_tree(path):
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise GitViewspaceError("INVALID_TRANSACTION", f"refusing to remove unexpected path: {path}")
    shutil.rmtree(path)
    fsync_directory(path.parent)
