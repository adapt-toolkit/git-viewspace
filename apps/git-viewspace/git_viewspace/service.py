"""CLI-side daemon lifecycle operations."""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from .daemon import run as run_daemon
from .errors import GitViewspaceError
from .git import Git
from .host import mount_at, process_matches
from .protocol import call
from .root import OnlineRoot, RootContext
from .storage import exclusive_lock
from .views import ViewStore

START_TIMEOUT_SECONDS = 20.0
STATUS_TIMEOUT_SECONDS = 30.0


def _daemon_response_error(response, fallback="DAEMON_FAILED"):
    error = response.get("error")
    if not isinstance(error, dict):
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon returned an invalid error")
    code = error.get("code")
    message = error.get("message")
    detail = error.get("detail")
    data = error.get("data")
    partial_state = response.get("partial_state")
    if not isinstance(code, str) or not isinstance(message, str):
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon returned an invalid error")
    if partial_state is not None and not isinstance(partial_state, dict):
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon returned invalid partial state")
    if data is not None and not isinstance(data, dict):
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon returned invalid error data")
    raise GitViewspaceError(
        code or fallback,
        message,
        detail=detail if isinstance(detail, str) else None,
        data=data,
        partial_state=partial_state,
    )


def _validate_daemon_identity(context, response):
    if response.get("root") != str(context.root) or response.get("root_uuid") != context.root_uuid:
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon response does not match the managed root")


def _running_status(context):
    if not context.socket_path.exists():
        return None
    try:
        response = call(context.socket_path, "status", timeout=STATUS_TIMEOUT_SECONDS)
    except GitViewspaceError as error:
        if error.code == "DAEMON_UNAVAILABLE":
            return None
        raise
    if not response["ok"]:
        _daemon_response_error(response)
    _validate_daemon_identity(context, response)
    return response


def start_background(root, *, popen=subprocess.Popen, timeout=START_TIMEOUT_SECONDS):
    context = OnlineRoot.load(root, require_runtime=False)
    running = _running_status(context)
    if running is not None:
        return {
            "ok": True,
            "command": "start",
            "root": str(context.root),
            "root_uuid": context.root_uuid,
            "started": True,
            "idempotent": True,
            "daemon": running["daemon"],
            "viewfs": running["viewfs"],
        }

    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    os.set_inheritable(write_fd, True)
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        package_root if not prior_pythonpath else package_root + os.pathsep + prior_pythonpath
    )
    try:
        process = popen(
            (
                sys.executable,
                "-m",
                "git_viewspace.daemon",
                "--root",
                str(context.root),
                "--ready-fd",
                str(write_fd),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(write_fd,),
            start_new_session=True,
            cwd="/",
            env=environment,
        )
    except OSError as error:
        os.close(read_fd)
        os.close(write_fd)
        raise GitViewspaceError(
            "DAEMON_START_FAILED", "could not launch git-viewspace daemon", detail=str(error)
        ) from error
    os.close(write_fd)
    ready = False
    try:
        deadline = time.monotonic() + timeout
        payload = bytearray()
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select((read_fd,), (), (), remaining)
            if not readable:
                break
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > 1024 * 1024:
                raise GitViewspaceError("DAEMON_START_FAILED", "daemon readiness response is too large")
            if b"\n" in payload:
                break
        if b"\n" not in payload:
            raise GitViewspaceError("DAEMON_START_FAILED", "timed out waiting for daemon readiness")
        try:
            response = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise GitViewspaceError(
                "DAEMON_START_FAILED", "daemon returned invalid readiness data"
            ) from error
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise GitViewspaceError("DAEMON_START_FAILED", "daemon returned invalid readiness data")
        if not response["ok"]:
            _daemon_response_error(response, "DAEMON_START_FAILED")
        ready = True
    finally:
        os.close(read_fd)
        if not ready and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

    running = _running_status(context)
    if running is None:
        raise GitViewspaceError("DAEMON_START_FAILED", "daemon reported ready without a control socket")
    return {
        "ok": True,
        "command": "start",
        "root": str(context.root),
        "root_uuid": context.root_uuid,
        "started": True,
        "idempotent": False,
        "daemon": running["daemon"],
        "viewfs": running["viewfs"],
    }


def start_foreground(root):
    return run_daemon(root)


def offline_status(context):
    issues = list(context.runtime_entries())
    runtime = context.runtime()
    mounted = mount_at(context.mountpoint)
    daemon_alive = False
    supervisor_alive = False
    if runtime is not None:
        daemon_alive = process_matches(
            runtime["daemon"]["pid"], runtime["daemon"]["start_time_ticks"]
        )
        supervisor_alive = process_matches(
            runtime["viewfs"]["supervisor_pid"],
            runtime["viewfs"]["supervisor_start_time_ticks"],
        )
        issues.append("runtime record remains while daemon is unavailable")
    if context.socket_path.exists() or context.socket_path.is_symlink():
        issues.append("control socket is unavailable or stale")
    if mounted is not None:
        issues.append(f"mount remains at {context.mountpoint}")
    public_mounts = {view.name: mount_at(view.mountpoint) for view in context.views.values()}
    issues.extend(
        f"public view mount remains: {name}"
        for name, observation in sorted(public_mounts.items())
        if observation is not None
    )
    if daemon_alive:
        issues.append("recorded daemon process is alive but unreachable")
    if supervisor_alive:
        issues.append("recorded git-viewspace-fs supervisor is alive")
    facts = context.repository_facts
    if not facts.clean and context.manager_state["repository_operation"] is None:
        issues.append("repository checkout is dirty without a current manager operation")
    git = Git()
    views = []
    for view in sorted(context.views.values(), key=lambda item: item.name):
        observation = public_mounts[view.name]
        value = {
            "name": view.name,
            "kind": view.kind,
            "view_id": view.view_id,
            "commit": view.commit,
            "alias": view.alias,
            "desired_started": view.desired_started,
            "actual": "mounted" if observation is not None else "stopped",
            "healthy": observation is None,
            "mountpoint": str(view.mountpoint),
        }
        if view.routing is not None or (view.path / "routing").exists():
            from .routing import facts as routing_facts
            value["routing"] = routing_facts(view)
            if value["routing"]["private_mounted"] or value["routing"]["process_alive"] or value["routing"]["runtime"]["mounted_paths"]:
                value["healthy"] = False
                issues.append(f"router runtime remains: {view.name}")
            if value["routing"]["faults"] or value["routing"]["requires_rebind"]:
                value["healthy"] = False
                issues.append(f"router requires attention: {view.name}")
        if view.kind == "overlay":
            tip = git.resolve_local_branch(context.repository, view.branch)
            value.update(
                {
                    "branch": view.branch,
                    "branch_tip": tip,
                    "overlay_base": view.overlay_base,
                    "generation": view.generation_id,
                    "dirty": ViewStore.dirty(view),
                    "sync": None
                    if view.sync is None
                    else {
                        "sync_id": view.sync["sync_id"],
                        "generation": view.sync["generation_id"],
                        "base_commit": view.sync["base_commit"],
                        "upper_fingerprint": view.sync["upper_fingerprint"],
                        "manifest_count": len(view.sync["manifest"]),
                    },
                    "stage": None
                    if view.stage is None
                    else {
                        "stage_id": view.stage["stage_id"],
                        "sync_id": view.stage["sync_id"],
                        "index_tree": view.stage["index_tree"],
                    },
                    "next": None
                    if view.next is None
                    else {
                        "next_id": view.next["next_id"],
                        "commit": view.next["commit"],
                        "tree": view.next["tree"],
                        "changed": view.next["changed"],
                    },
                }
            )
            expected_tip = view.next["commit"] if view.next is not None else view.overlay_base
            if tip != expected_tip:
                value["healthy"] = False
                issues.append(f"owned branch differs from expected transition: {view.name}")
        views.append(value)
    return {
        "ok": True,
        "command": "status",
        "root": str(context.root),
        "root_uuid": context.root_uuid,
        "initialized": True,
        "healthy": not issues,
        "repository": {
            "healthy": True,
            "head": facts.head,
            "branch": facts.branch,
            "object_format": "sha1",
            "clean": facts.clean,
            "operation_in_progress": context.manager_state["repository_operation"] is not None,
        },
        "daemon": {"running": False},
        "viewfs": {
            "healthy": False,
            "mounted": mounted is not None,
            "supervisor_alive": supervisor_alive,
        },
        "views": views,
        "pending_transactions": [],
        "issues": issues,
    }


def _offline_status(online):
    # Never read a multi-file transition outside its existing ownership lock.
    with exclusive_lock(online.root / "state/lock"):
        return offline_status(RootContext.load(online.root))


def status(root):
    online = OnlineRoot.load(root, require_runtime=False)
    if online.socket_path.exists():
        online = OnlineRoot.load(root, allow_unready=True)
        running = _running_status(online)
        if running is not None:
            return running
    return _offline_status(online)


def stop(root):
    online = OnlineRoot.load(root, require_runtime=False)
    if online.socket_path.exists():
        context = OnlineRoot.load(root)
        running = _running_status(context)
    else:
        context = online
        running = None
    if running is None:
        observed = _offline_status(online)
        if not observed["healthy"]:
            raise GitViewspaceError(
                "DAEMON_NOT_RUNNING",
                "daemon is stopped but manager-owned runtime residue remains; "
                "run start to reconcile it",
                detail="; ".join(observed["issues"]),
            )
        return {
            "ok": True,
            "command": "stop",
            "root": str(context.root),
            "stopped": True,
            "idempotent": True,
            "mounts_remaining": 0,
            "children_remaining": 0,
        }
    response = call(context.socket_path, "stop", timeout=30.0)
    if not response["ok"]:
        _daemon_response_error(response)
    _validate_daemon_identity(context, response)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not context.socket_path.exists() and mount_at(context.root / "git-mnt") is None:
            break
        time.sleep(0.05)
    if context.socket_path.exists() or mount_at(context.root / "git-mnt") is not None:
        raise GitViewspaceError("SHUTDOWN_INCOMPLETE", "daemon shutdown left runtime state")
    return response


def branch_merge(
    root,
    attempt_branch,
    parent_branch,
    ensure_attempt_tip,
    message,
    checked_out_destination="refuse",
):
    """Request one repository-only attempt-to-parent merge."""

    context = OnlineRoot.load(root)
    try:
        response = call(
            context.socket_path,
            "branch-merge",
            timeout=180.0,
            attempt_branch=attempt_branch,
            parent_branch=parent_branch,
            ensure_attempt_tip=ensure_attempt_tip,
            message=message,
            checked_out_destination=checked_out_destination,
        )
    except GitViewspaceError as error:
        if error.code == "DAEMON_UNAVAILABLE":
            raise GitViewspaceError(
                "DAEMON_NOT_RUNNING",
                "branch merge requires a running git-viewspace daemon",
                detail=error.detail,
            ) from error
        raise
    if not response["ok"]:
        _daemon_response_error(response)
    _validate_daemon_identity(context, response)
    return response


def branch_pin(root, branch, commit):
    """Request one repository-only exact commit pin."""

    context = OnlineRoot.load(root)
    try:
        response = call(
            context.socket_path,
            "branch-pin",
            branch=branch,
            commit=commit,
        )
    except GitViewspaceError as error:
        if error.code == "DAEMON_UNAVAILABLE":
            raise GitViewspaceError(
                "DAEMON_NOT_RUNNING",
                "branch pin requires a running git-viewspace daemon",
                detail=error.detail,
            ) from error
        raise
    if not response["ok"]:
        _daemon_response_error(response)
    _validate_daemon_identity(context, response)
    return response


def submodule_operation(root, operation, **arguments):
    context = OnlineRoot.load(root)
    response = call(context.socket_path, "submodule-" + operation, timeout=180.0, **arguments)
    if not response["ok"]:
        _daemon_response_error(response)
    _validate_daemon_identity(context, response)
    return response


def _view_mutation(root, command, *, timeout=30.0, **arguments):
    name = arguments.get("view")
    context = OnlineRoot.load(
        root,
        view=name,
        allow_missing_view=command in {"view-init-readonly", "view-init-and-start"},
    )
    try:
        response = call(context.socket_path, command, timeout=timeout, **arguments)
    except GitViewspaceError as error:
        if error.code == "DAEMON_UNAVAILABLE":
            raise GitViewspaceError(
                "DAEMON_NOT_RUNNING",
                "view operation requires a running git-viewspace daemon",
                detail=error.detail,
            ) from error
        raise
    if not response["ok"]:
        _daemon_response_error(response)
    _validate_daemon_identity(context, response)
    return response


def _view_identity_arguments(name, ensure_view_id):
    arguments = {"view": name}
    if ensure_view_id is not None:
        if (
            not isinstance(ensure_view_id, str)
            or len(ensure_view_id) != 32
            or any(character not in "0123456789abcdef" for character in ensure_view_id)
        ):
            raise GitViewspaceError(
                "INVALID_ARGUMENT",
                "view ID must be exactly 32 lowercase hexadecimal characters",
            )
        arguments["ensure_view_id"] = ensure_view_id
    return arguments


def _existing_view_mutation(
    root,
    command,
    name,
    *,
    ensure_view_id=None,
    timeout=30.0,
    **arguments,
):
    return _view_mutation(
        root,
        command,
        timeout=timeout,
        **_view_identity_arguments(name, ensure_view_id),
        **arguments,
    )


def view_init_readonly(root, name, commitish):
    return _view_mutation(root, "view-init-readonly", view=name, at=commitish)


def view_init_overlay(root, name, branch, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-init-overlay",
        name,
        ensure_view_id=ensure_view_id,
        branch=branch,
    )


def view_init_and_start(
    root,
    name,
    kind,
    *,
    at=None,
    from_branch=None,
    new_branch=None,
):
    arguments = {"view": name, "kind": kind}
    if kind == "readonly":
        arguments["at"] = at
    else:
        arguments["from_branch"] = from_branch
        arguments["new_branch"] = new_branch
    return _view_mutation(root, "view-init-and-start", **arguments)


def view_start(root, name, *, ensure_view_id=None):
    return _existing_view_mutation(root, "view-start", name, ensure_view_id=ensure_view_id)


def view_stop(root, name, *, ensure_view_id=None):
    return _existing_view_mutation(root, "view-stop", name, ensure_view_id=ensure_view_id)


def view_sync(root, name, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-sync",
        name,
        ensure_view_id=ensure_view_id,
        timeout=180.0,
    )


def view_stage(root, name, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-stage",
        name,
        ensure_view_id=ensure_view_id,
        timeout=180.0,
    )


def view_commit(root, name, message, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-commit",
        name,
        ensure_view_id=ensure_view_id,
        timeout=180.0,
        message=message,
    )


def view_advance(root, name, message, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-advance",
        name,
        ensure_view_id=ensure_view_id,
        timeout=300.0,
        message=message,
    )


def view_switch(root, name, commitish=None, *, use_next=False, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-switch",
        name,
        ensure_view_id=ensure_view_id,
        to=commitish,
        next=use_next,
    )


def view_destroy(root, name, discard=False, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-destroy",
        name,
        ensure_view_id=ensure_view_id,
        timeout=300.0,
        discard=discard,
    )


def view_clean(root, name, *, ensure_view_id=None):
    return _existing_view_mutation(root, "view-clean", name, ensure_view_id=ensure_view_id)


def view_deinit_overlay(root, name, *, ensure_view_id=None):
    return _existing_view_mutation(
        root,
        "view-deinit-overlay",
        name,
        ensure_view_id=ensure_view_id,
    )


def view_status(root, name=None, *, ensure_view_id=None):
    if name is not None:
        from .views import validate_view_name
        validate_view_name(name)
    if ensure_view_id is not None and name is None:
        raise GitViewspaceError(
            "INVALID_ARGUMENT",
            "--ensure-view-id requires one view name",
        )
    identity_arguments = _view_identity_arguments(name, ensure_view_id)
    online = OnlineRoot.load(root, require_runtime=False)
    if online.socket_path.exists():
        context = OnlineRoot.load(root, allow_unready=True)
        response = call(
            context.socket_path,
            "view-status",
            timeout=STATUS_TIMEOUT_SECONDS,
            **identity_arguments,
        )
        if not response["ok"]:
            _daemon_response_error(response)
        _validate_daemon_identity(context, response)
        return response
    views = [
        item for item in _offline_status(online)["views"] if name is None or item["name"] == name
    ]
    if name is not None and not views:
        raise GitViewspaceError("VIEW_NOT_FOUND", f"view does not exist: {name}")
    if ensure_view_id is not None and views[0]["view_id"] != ensure_view_id:
        raise GitViewspaceError(
            "VIEW_ID_MISMATCH",
            f"view {name} has ID {views[0]['view_id']}, not expected ID {ensure_view_id}",
        )
    return {
        "ok": True,
        "command": "view status",
        "root": str(online.root),
        "views": views,
        "view": views[0] if name is not None else None,
    }


def view_routing(root, name, action, executable=None, *, runtime_directories=None, subtree=None, ensure_view_id=None):
    extra = {} if runtime_directories is None else {"runtime_directories": runtime_directories}
    if subtree is not None:
        extra["subtree"] = subtree
    return _existing_view_mutation(root, "view-routing", name, ensure_view_id=ensure_view_id, action=action, executable=executable, **extra)
