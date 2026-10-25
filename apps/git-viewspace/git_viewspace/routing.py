"""Optional native router lifecycle; no external contents enter the Git checkout."""

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from dataclasses import replace

from .errors import GitViewspaceError
from .host import filesystem_at, mount_at, process_matches, process_start_time_ticks
from .storage import (
    atomic_json,
    atomic_write,
    fsync_directory,
    read_json,
    validate_stable_file,
)

PREFIX = ".git-viewspace-#SF#-"


def private_view(view):
    return replace(view, mountpoint=view.path / "routing" / "source", routing=None)


def facts(view):
    directory = view.path / "routing"
    running = (
        read_json(directory / "running.json")
        if (directory / "running.json").exists()
        else None
    )
    from .runtime_mounts import status

    return {
        "runtime": status(view),
        "enabled": view.routing is not None,
        "private_mounted": mount_at(directory / "source") is not None,
        "process_alive": bool(
            running and process_matches(running["pid"], running["start_time_ticks"])
        ),
        "faults": sorted(p.stem for p in directory.glob("*.fault")),
        "requires_rebind": (directory / "uncertain").exists(),
    }


def _public(mounts, view, observed=None):
    actual = mount_at(view.mountpoint, observed)
    if actual and not (
        actual.filesystem_type == "fuse.git-viewspace-router"
        and actual.source == "gvr-" + view.view_id
        and actual.root == "/"
        and {"nosuid", "nodev", "ro" if view.kind == "readonly" else "rw"}.issubset(
            actual.mount_options
        )
    ):
        raise GitViewspaceError("FOREIGN_MOUNT", "unexpected router mount")
    return actual


def observe(mounts, view, source, observed=None):
    actual = _public(mounts, view, observed)
    lower = mounts.observe(private_view(view), source, observed)
    if actual is not None and lower is None:
        raise GitViewspaceError(
            "ROUTER_PARTIAL_STATE", "public router has no underlying mount"
        )
    if actual is None and lower is not None:
        raise GitViewspaceError(
            "ROUTER_PARTIAL_STATE", "private mount remains; stop the view to recover"
        )
    if actual is not None:
        from .runtime_mounts import verify

        owned = verify(view)
        if len(owned) != len(view.routing.get("runtime_directories", {})):
            raise GitViewspaceError(
                "ROUTER_PARTIAL_STATE", "runtime mount set incomplete"
            )
    return actual


def _bindings(mounts, view, base, previous=None):
    from .submodules import entries

    modules = tuple(entries(mounts.context.repository, view.commit))
    found = {}
    root = mounts.context.root.resolve()
    for parent, dirs, files in os.walk(base):
        # Git paths do not contain literal .git administrative directories.
        if any(n.startswith(PREFIX) for n in dirs):
            raise GitViewspaceError(
                "INVALID_ROUTING_DESCRIPTOR", "descriptor must be a regular file"
            )
        dirs[:] = [n for n in dirs if n != ".git"]
        for filename in files:
            if not filename.startswith(PREFIX):
                continue
            descriptor = Path(parent) / filename
            relative = descriptor.relative_to(base).as_posix()
            leaf = filename[len(PREFIX) :]
            if not leaf or leaf in (".", "..") or leaf.startswith(PREFIX):
                raise GitViewspaceError(
                    "INVALID_ROUTING_DESCRIPTOR", "invalid routed basename"
                )
            if any(relative == m or relative.startswith(m + "/") for m in modules):
                raise GitViewspaceError(
                    "INVALID_ROUTING_DESCRIPTOR",
                    "bindings inside submodules are unsupported",
                )
            if not stat.S_ISREG(descriptor.lstat().st_mode):
                raise GitViewspaceError(
                    "INVALID_ROUTING_DESCRIPTOR", "descriptor must be a regular file"
                )
            with descriptor.open("rb") as stream:
                raw = stream.read(4097)
            try:
                target_text = raw.decode("utf-8").removesuffix("\n")
            except UnicodeError as error:
                raise GitViewspaceError(
                    "INVALID_ROUTING_DESCRIPTOR", "descriptor must be UTF-8"
                ) from error
            if (
                len(raw) > 4096
                or "\n" in target_text
                or "\0" in target_text
                or not Path(target_text).is_absolute()
            ):
                raise GitViewspaceError(
                    "INVALID_ROUTING_DESCRIPTOR",
                    "descriptor must contain one absolute path",
                )
            target = Path(target_text)
            visible = descriptor.with_name(leaf)
            if os.path.lexists(visible):
                raise GitViewspaceError(
                    "ROUTING_COLLISION", "ordinary path conflicts with routed name"
                )
            key = "/" + visible.relative_to(base).as_posix()
            if (
                previous is not None
                and key in previous
                and previous[key]["target"] == target_text
            ):
                # Preserve admitted identity even when its pathname disappears or changes.
                # The native router faults only this binding; ordinary files stay usable.
                found[key] = previous[key]
                continue
            if target.is_symlink() or target.resolve().is_relative_to(root):
                raise GitViewspaceError(
                    "INVALID_ROUTING_TARGET",
                    "backing file must be external and not a symlink",
                )
            try:
                metadata = target.stat()
            except OSError as error:
                raise GitViewspaceError(
                    "INVALID_ROUTING_TARGET",
                    "backing file unavailable",
                    detail=str(error),
                ) from error
            if not stat.S_ISREG(metadata.st_mode):
                raise GitViewspaceError(
                    "INVALID_ROUTING_TARGET", "backing file must be regular"
                )
            fs = filesystem_at(target).filesystem_type
            if fs.startswith("fuse") or fs == "overlay":
                raise GitViewspaceError(
                    "INVALID_ROUTING_TARGET",
                    "recursive/stacked backing files are unsupported",
                )
            found[key] = {
                "target": str(target),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "fault": hashlib.sha256(key.encode()).hexdigest() + ".fault",
            }
    return found


def _table(mounts, view, base):
    directory = view.path / "routing"
    record = directory / "bindings.json"
    previous = None
    if record.exists():
        validate_stable_file(record, mounts.context.uid, mounts.context.gid)
        previous = read_json(record)
    current = _bindings(mounts, view, base, previous)
    if previous is not None:
        if set(previous) != set(current) or any(
            previous[k]["target"] != current[k]["target"] for k in current
        ):
            raise GitViewspaceError(
                "ROUTER_REBIND_REQUIRED",
                "bindings changed; explicitly rebind the stopped view",
            )
        current = (
            previous  # Keep the originally admitted identity; the router validates it.
        )
    else:
        atomic_json(record, current)
    payload = bytearray()
    for key, value in sorted(current.items()):
        for token in (
            key,
            value["target"],
            str(value["device"]),
            str(value["inode"]),
            value["fault"],
        ):
            payload.extend(token.encode() + b"\0")
    table = directory / "bindings.nul"
    table.write_bytes(payload)
    table.chmod(0o600)
    return table


def configure(daemon, name, action, executable, runtime_directories=None):
    view = daemon.store.get(name)
    if (
        view.desired_started
        or mount_at(view.mountpoint)
        or mount_at(view.path / "routing/source")
    ):
        raise GitViewspaceError(
            "VIEW_STARTED", "stop the view before configuring its router"
        )
    directory = view.path / "routing"
    if directory.exists() and (
        facts(view)["process_alive"]
        or facts(view)["runtime"]["record_present"]
        or facts(view)["runtime"]["mounted_paths"]
    ):
        raise GitViewspaceError(
            "ROUTER_PARTIAL_STATE", "router process remains; stop the view first"
        )
    if action == "enable":
        binary = Path(executable).resolve()
        try:
            version = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise GitViewspaceError(
                "INVALID_ROUTER", "cannot execute router", detail=str(error)
            ) from error
        if version.stdout.strip() != "git-viewspace-router 1":
            raise GitViewspaceError("INVALID_ROUTER", "unsupported router protocol")
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "source").mkdir(mode=0o700, exist_ok=True)
        from .runtime_mounts import validate_mapping

        mapping = validate_mapping(runtime_directories or {})
        view = replace(
            view, routing={"executable": str(binary), "runtime_directories": mapping}
        )
    elif action == "disable":
        view = replace(view, routing=None)
    elif action == "rebind":
        if view.routing is None:
            raise GitViewspaceError(
                "ROUTER_DISABLED", "enable routing before rebinding"
            )
        for path in [
            directory / "bindings.json",
            directory / "bindings.nul",
            directory / "uncertain",
            *directory.glob("*.fault"),
        ]:
            path.unlink(missing_ok=True)
    else:
        raise GitViewspaceError("INVALID_REQUEST", "unknown routing operation")
    atomic_json(view.config_path, daemon.store._config(view, daemon.context.root_uuid))
    daemon.store.views[name] = view
    return {
        "ok": True,
        "command": "view routing " + action,
        "view": {"name": name, "routing": view.routing},
        "idempotent": False,
    }


def start(mounts, view, source):
    if observe(mounts, view, source) is not None:
        return True
    directory = view.path / "routing"
    if (directory / "running.json").exists() or (directory / "uncertain").exists():
        raise GitViewspaceError(
            "ROUTER_RECOVERY_REQUIRED",
            "stop/rebind the view after an uncertain router exit",
        )
    base = private_view(view)
    mounts.start(base, source)
    process = None
    try:
        table = _table(mounts, view, base.mountpoint)
        from . import runtime_mounts

        prepared = runtime_mounts.prepare(
            mounts, view, base.mountpoint, read_json(directory / "bindings.json")
        )
        protected = directory / "protected.nul"
        protected.write_bytes(
            b"".join(("/" + item["path"]).encode() + b"\0" for item in prepared)
        )
        protected.chmod(0o600)
        argv = [
            mounts.sudo,
            "-n",
            "--",
            view.routing["executable"],
            str(base.mountpoint),
            str(view.mountpoint),
            str(table),
            str(directory),
            str(mounts.context.uid),
            str(mounts.context.gid),
            "ro" if view.kind == "readonly" else "rw",
            "gvr-" + view.view_id,
            str(protected),
        ]
        with (directory / "stderr.log").open("wb") as log:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
        atomic_json(
            directory / "running.json",
            {
                "pid": process.pid,
                "start_time_ticks": process_start_time_ticks(process.pid),
            },
        )
        if not hasattr(mounts, "router_processes"):
            mounts.router_processes = {}
        mounts.router_processes[view.view_id] = process
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise GitViewspaceError(
                    "ROUTER_START_FAILED", "router exited; inspect per-view stderr.log"
                )
            if _public(mounts, view) is not None:
                # getattr forces INIT completion; a mere mount-table entry is not readiness.
                view.mountpoint.stat()
                runtime_mounts.start(mounts, view, prepared)
                return False
            time.sleep(0.05)
        raise GitViewspaceError(
            "ROUTER_START_FAILED", "router mount did not become ready"
        )
    except BaseException:
        stop(mounts, view, source, force=True)
        raise


def stop(mounts, view, source, *, force=False):
    directory = view.path / "routing"
    public = _public(mounts, view)
    record = directory / "running.json"
    running = read_json(record) if record.exists() else None
    alive = bool(
        running and process_matches(running["pid"], running["start_time_ticks"])
    )
    from . import runtime_mounts

    runtime_mounts.stop(mounts, view, force)
    if public is not None:
        mounts._unmount(view.mountpoint, lazy=force, retry=not force)
    process = getattr(mounts, "router_processes", {}).get(view.view_id)
    if process is not None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if not force:
                raise GitViewspaceError(
                    "ROUTER_STOP_FAILED", "router did not exit after unmount"
                )
    if running and process_matches(running["pid"], running["start_time_ticks"]):
        mounts._command(
            (mounts.sudo, "-n", "--", "kill", "-TERM", "--", "-" + str(running["pid"])),
            "ROUTER_STOP_FAILED",
            "cannot stop owned router group",
        )
        deadline = time.monotonic() + 5
        while (
            process_matches(running["pid"], running["start_time_ticks"])
            and time.monotonic() < deadline
        ):
            if process is not None:
                process.poll()
            time.sleep(0.05)
        if process_matches(running["pid"], running["start_time_ticks"]):
            raise GitViewspaceError("ROUTER_STOP_FAILED", "router process remains")
    if process is not None:
        process.wait()
        mounts.router_processes.pop(view.view_id, None)
    if running and (
        not alive or public is None or (process is not None and process.returncode != 0)
    ):
        atomic_write(
            directory / "uncertain",
            b"Explicit rebind required after uncertain router exit\n",
        )
    record.unlink(missing_ok=True)
    fsync_directory(directory)
    mounts.stop(private_view(view), source, force=force)
    return public is None and running is None
