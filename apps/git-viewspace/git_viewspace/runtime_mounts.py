"""Manager-owned external runtime directories above the optional router."""

import os
import subprocess
from pathlib import Path

from .errors import GitViewspaceError
from .host import filesystem_at, mount_at, mounts as host_mounts
from .storage import atomic_json, read_json, validate_stable_file

MARKER = ".git-viewspace-runtime"


def validate_mapping(value):
    if not isinstance(value, dict):
        raise GitViewspaceError(
            "INVALID_ROUTING", "runtime directories must be a mapping"
        )
    for destination, source in value.items():
        if (
            not isinstance(destination, str)
            or not destination
            or destination.startswith("/")
            or any(p in ("", ".", "..", ".git") for p in destination.split("/"))
            or not isinstance(source, str)
            or not source.startswith("/")
            or "\0" in destination
            or "\0" in source
        ):
            raise GitViewspaceError(
                "INVALID_ROUTING", "invalid runtime directory mapping"
            )
        try:
            (destination + source).encode("utf-8")
        except UnicodeError as error:
            raise GitViewspaceError(
                "INVALID_ROUTING", "runtime paths must be UTF-8"
            ) from error
    paths = sorted(value)
    for b in paths:
        if any(b.startswith(a + "/") for a in paths if a != b):
            raise GitViewspaceError(
                "INVALID_ROUTING", "runtime destinations cannot overlap"
            )
    return value


def prepare(mounts, view, base, bindings):
    from .submodules import entries

    mapping = validate_mapping(view.routing.get("runtime_directories", {}))
    modules = tuple(entries(mounts.context.repository, view.commit))
    prepared = []
    for relative, source in sorted(mapping.items()):
        if any(
            relative == m
            or relative.startswith(m + "/")
            or m.startswith(relative + "/")
            for m in modules
        ):
            raise GitViewspaceError(
                "INVALID_ROUTING", "runtime destination overlaps a submodule"
            )
        if any(
            relative == b.lstrip("/")
            or relative.startswith(b.lstrip("/") + "/")
            or b.lstrip("/").startswith(relative + "/")
            for b in bindings
        ):
            raise GitViewspaceError(
                "INVALID_ROUTING", "runtime destination overlaps a routed file"
            )
        path = base
        for part in relative.split("/"):
            path = path / part
            if path.is_symlink() or not path.is_dir():
                raise GitViewspaceError(
                    "INVALID_ROUTING",
                    "runtime destination must have real directory components",
                )
        if (
            set(p.name for p in path.iterdir()) != {MARKER}
            or (path / MARKER).is_symlink()
            or not (path / MARKER).is_file()
            or (path / MARKER).stat().st_size
        ):
            raise GitViewspaceError(
                "INVALID_ROUTING",
                "runtime destination requires only an empty marker file",
            )
        tracked = subprocess.run(
            [
                "git",
                "-C",
                str(mounts.context.repository),
                "cat-file",
                "-s",
                f"{view.commit}:{relative}/{MARKER}",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if tracked.returncode or tracked.stdout.strip() != b"0":
            raise GitViewspaceError(
                "INVALID_ROUTING",
                "runtime marker must be committed in the pinned source",
            )
        external = Path(source)
        if (
            external != external.resolve()
            or not external.is_dir()
            or external.is_relative_to(mounts.context.root.resolve())
        ):
            raise GitViewspaceError(
                "INVALID_ROUTING",
                "runtime source must be a canonical external directory",
            )
        fs = filesystem_at(external)
        if fs.filesystem_type.startswith("fuse") or fs.filesystem_type == "overlay":
            raise GitViewspaceError(
                "INVALID_ROUTING", "stacked runtime sources are unsupported"
            )
        if any(
            m.mountpoint != external and m.mountpoint.is_relative_to(external)
            for m in host_mounts()
        ):
            raise GitViewspaceError(
                "INVALID_ROUTING", "runtime source contains a subordinate mount"
            )
        st = external.stat()
        prepared.append(
            dict(
                path=relative,
                source=source,
                device=st.st_dev,
                inode=st.st_ino,
                filesystem=fs.filesystem_type,
                root=str(Path(fs.root) / external.relative_to(fs.mountpoint)),
            )
        )
    return prepared


def record_path(view):
    return view.path / "routing/runtime-mounts.json"


def read_record(view):
    path = record_path(view)
    if not path.exists():
        return []
    owner = view.path.stat()
    validate_stable_file(path, owner.st_uid, owner.st_gid)
    value = read_json(path)
    if not isinstance(value, list):
        raise GitViewspaceError("INVALID_STATE", "invalid runtime mount record")
    expected = {"path", "source", "device", "inode", "filesystem", "root", "mount_id"}
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != expected:
            raise GitViewspaceError("INVALID_STATE", "invalid runtime mount item")
        validate_mapping({item["path"]: item["source"]})
        if item["path"] in seen or any(
            type(item[k]) is not int or item[k] < 0
            for k in ("device", "inode", "mount_id")
        ):
            raise GitViewspaceError("INVALID_STATE", "invalid runtime mount identity")
        if (
            not isinstance(item["filesystem"], str)
            or not isinstance(item["root"], str)
            or not item["root"].startswith("/")
        ):
            raise GitViewspaceError(
                "INVALID_STATE", "invalid runtime filesystem identity"
            )
        seen.add(item["path"])
    return value


def status(view):
    owned = read_record(view)
    configured = view.routing.get("runtime_directories", {}) if view.routing else {}
    paths = sorted(set(configured) | {item["path"] for item in owned})
    return {
        "record_present": record_path(view).exists(),
        "mounted_paths": [
            p for p in paths if mount_at(view.mountpoint / p) is not None
        ],
    }


def identity(view, item):
    path = view.mountpoint / item["path"]
    observed = mount_at(path)
    if observed is None:
        return None
    if (
        observed.filesystem_type != item["filesystem"]
        or observed.root != item["root"]
        or ("mount_id" in item and observed.mount_id != item["mount_id"])
    ):
        raise GitViewspaceError("FOREIGN_MOUNT", "unexpected runtime directory mount")
    st = path.stat()
    if st.st_dev != item["device"] or st.st_ino != item["inode"]:
        raise GitViewspaceError("FOREIGN_MOUNT", "runtime directory identity differs")
    return observed


def start(mounts, view, prepared):
    if not prepared:
        return
    path = record_path(view)
    if path.exists():
        raise GitViewspaceError(
            "ROUTER_RECOVERY_REQUIRED", "runtime mount record remains"
        )
    owned = []
    atomic_json(path, owned)
    for item in prepared:
        target = view.mountpoint / item["path"]
        if mount_at(target):
            raise GitViewspaceError(
                "FOREIGN_MOUNT", "runtime destination is already mounted"
            )
        mounts._command(
            (
                mounts.sudo,
                "-n",
                "--",
                mounts.mount,
                "--bind",
                "--",
                item["source"],
                str(target),
            ),
            "RUNTIME_MOUNT_FAILED",
            "cannot bind runtime directory",
        )
        actual = identity(view, item)
        if actual is None:
            raise GitViewspaceError("RUNTIME_MOUNT_FAILED", "runtime mount missing")
        owned.append(dict(item, mount_id=actual.mount_id))
        atomic_json(path, owned)
        flags = "remount,bind,nosuid,nodev," + (
            "ro" if view.kind == "readonly" else "rw"
        )
        mounts._command(
            (mounts.sudo, "-n", "--", mounts.mount, "-o", flags, "--", str(target)),
            "RUNTIME_MOUNT_FAILED",
            "cannot set runtime mount flags",
        )
        verify(view)


def verify(view):
    path = record_path(view)
    owned = read_record(view)
    for item in owned:
        actual = identity(view, item)
        required = {"nosuid", "nodev", "ro" if view.kind == "readonly" else "rw"}
        if actual is None or not required.issubset(actual.mount_options):
            raise GitViewspaceError(
                "ROUTER_PARTIAL_STATE", "runtime mount missing or flags differ"
            )
    return owned


def stop(mounts, view, force=False):
    path = record_path(view)
    owned = read_record(view)
    configured = view.routing.get("runtime_directories", {}) if view.routing else {}
    known = {item["path"] for item in owned}
    for relative in configured:
        if relative not in known and mount_at(view.mountpoint / relative):
            raise GitViewspaceError(
                "FOREIGN_MOUNT",
                "unrecorded mount at runtime destination; operator recovery required",
            )
    while owned:
        item = owned[-1]
        if identity(view, item) is not None:
            mounts._unmount(view.mountpoint / item["path"], lazy=force, retry=not force)
        owned.pop()
        atomic_json(path, owned)
    path.unlink(missing_ok=True)
