"""Small synchronous Unix-socket protocol used by ``git-viewspace``."""

import errno
import json
import math
import socket
import time

from .errors import GitViewspaceError
from .storage import canonical_json_bytes

PROTOCOL_VERSION = 4
MAX_MESSAGE_BYTES = 1024 * 1024
CONNECT_RETRY_INTERVAL_SECONDS = 5.0
CONNECT_RETRY_LIMIT = 6

VIEW_ID_GUARDED_COMMANDS = frozenset(
    {
        "view-init-overlay",
        "view-start",
        "view-routing",
        "view-stop",
        "view-sync",
        "view-stage",
        "view-commit",
        "view-advance",
        "view-status",
        "view-switch",
        "view-clean",
        "view-deinit-overlay",
        "view-destroy",
    }
)


def request(command, **arguments):
    return {"protocol": PROTOCOL_VERSION, "command": command, **arguments}


def validate_request(value):
    if not isinstance(value, dict) or type(value.get("protocol")) is not int:
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if value["protocol"] != PROTOCOL_VERSION or not isinstance(value.get("command"), str):
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    command = value["command"]
    expected = {
        "status": {"protocol", "command"},
        "stop": {"protocol", "command"},
        "branch-merge": {
            "protocol",
            "command",
            "attempt_branch",
            "parent_branch",
            "ensure_attempt_tip",
            "message",
            "checked_out_destination",
        },
        "branch-pin": {"protocol", "command", "branch", "commit"},
        "submodule-prepare": {"protocol", "command", "at", "path", "commit"},
        "submodule-pin": {"protocol", "command", "branch", "path", "commit", "message"},
        "view-init-readonly": {"protocol", "command", "view", "at"},
        "view-init-overlay": {"protocol", "command", "view", "branch"},
        "view-init-and-start": None,
        "view-start": {"protocol", "command", "view"},
        "view-routing": {"protocol", "command", "view", "action", "executable"},
        "view-stop": {"protocol", "command", "view"},
        "view-sync": {"protocol", "command", "view"},
        "view-stage": {"protocol", "command", "view"},
        "view-commit": {"protocol", "command", "view", "message"},
        "view-advance": {"protocol", "command", "view", "message"},
        "view-status": {"protocol", "command", "view"},
        "view-switch": {"protocol", "command", "view", "to", "next"},
        "view-clean": {"protocol", "command", "view"},
        "view-deinit-overlay": {"protocol", "command", "view"},
        "view-destroy": {"protocol", "command", "view", "discard"},
    }
    if command == "view-routing":
        if value.get("action") not in {"enable", "disable", "rebind"}:
            raise GitViewspaceError("INVALID_REQUEST", "invalid routing action")
        executable = value.get("executable")
        if value["action"] == "enable":
            if not isinstance(executable, str) or not executable.startswith("/") or "\0" in executable:
                raise GitViewspaceError("INVALID_REQUEST", "enable requires an absolute router executable")
        elif executable is not None:
            raise GitViewspaceError("INVALID_REQUEST", "executable applies only to enable")
    actual_fields = set(value)
    if command == "view-routing" and "runtime_directories" in value:
        from .runtime_mounts import validate_mapping
        validate_mapping(value["runtime_directories"])
        if value["action"] != "enable":
            raise GitViewspaceError("INVALID_REQUEST", "runtime directories apply only to enable")
        actual_fields.discard("runtime_directories")
    if "queue_timeout_seconds" in value:
        budget = value["queue_timeout_seconds"]
        if (
            command in {"status", "view-status"}
            or type(budget) not in (int, float)
            or not 0 < budget <= 86400
            or not math.isfinite(budget)
        ):
            raise GitViewspaceError("INVALID_REQUEST", "invalid queue budget")
        actual_fields.discard("queue_timeout_seconds")
    expected_fields = expected.get(command)
    if command == "view-init-and-start":
        kind = value.get("kind")
        if kind == "readonly":
            expected_fields = {"protocol", "command", "view", "kind", "at"}
        elif kind == "overlay":
            expected_fields = {
                "protocol",
                "command",
                "view",
                "kind",
                "from_branch",
                "new_branch",
            }
    if "ensure_view_id" in value:
        if command not in VIEW_ID_GUARDED_COMMANDS or value.get("view") is None:
            raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
        actual_fields.discard("ensure_view_id")
    if expected_fields is None or actual_fields != expected_fields:
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    # JSON escapes can decode to lone surrogates, which cannot be echoed as UTF-8.
    try:
        for item in value.values():
            if isinstance(item, str):
                item.encode("utf-8")
    except UnicodeError as error:
        raise GitViewspaceError("INVALID_REQUEST", "request text must be valid UTF-8") from error
    for field in (
        "view",
        "path",
        "at",
        "branch",
        "commit",
        "attempt_branch",
        "parent_branch",
        "ensure_attempt_tip",
        "from_branch",
        "new_branch",
        "message",
        "kind",
        "checked_out_destination",
    ):
        if (
            field in value
            and not (command == "view-status" and field == "view" and value[field] is None)
            and not isinstance(value[field], str)
        ):
            raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if (
        command == "view-status"
        and value["view"] is not None
        and not isinstance(value["view"], str)
    ):
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if "ensure_view_id" in value:
        ensure_view_id = value.get("ensure_view_id")
        if (
            not isinstance(ensure_view_id, str)
            or len(ensure_view_id) != 32
            or any(character not in "0123456789abcdef" for character in ensure_view_id)
        ):
            raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if command == "view-destroy" and not isinstance(value["discard"], bool):
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if command == "branch-merge" and value["checked_out_destination"] not in {
        "refuse",
        "detach_if_clean",
    }:
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if command == "view-init-and-start" and value.get("kind") not in {"readonly", "overlay"}:
        raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    if command == "view-switch":
        if (
            not isinstance(value["next"], bool)
            or ((value["to"] is None) == (not value["next"]))
            or (value["to"] is not None and not isinstance(value["to"], str))
        ):
            raise GitViewspaceError("INVALID_REQUEST", "invalid daemon request")
    return value


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("control request deadline exceeded")
    return remaining


def _receive_line(stream, *, deadline=None):
    payload = bytearray()
    while True:
        if deadline is not None:
            stream.settimeout(_remaining(deadline))
        chunk = stream.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(payload)))
        if not chunk:
            raise GitViewspaceError("PROTOCOL_ERROR", "daemon closed the connection")
        payload.extend(chunk)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise GitViewspaceError("PROTOCOL_ERROR", "daemon message is too large")
        newline = payload.find(b"\n")
        if newline >= 0:
            if newline != len(payload) - 1:
                raise GitViewspaceError("PROTOCOL_ERROR", "daemon sent trailing data")
            return bytes(payload[:-1])


def decode_message(payload, label):
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise GitViewspaceError("PROTOCOL_ERROR", f"invalid {label} JSON") from error
    if not isinstance(value, dict):
        raise GitViewspaceError("PROTOCOL_ERROR", f"invalid {label} object")
    return value


def send_message(stream, value):
    payload = canonical_json_bytes(value)
    if len(payload) > MAX_MESSAGE_BYTES:
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon message is too large")
    stream.sendall(payload)


def receive_message(stream, label, *, deadline=None):
    return decode_message(_receive_line(stream, deadline=deadline), label)


def call(socket_path, command, *, timeout=5.0, **arguments):
    deadline = time.monotonic() + timeout if command in {"status", "view-status"} else None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        for retry in range(CONNECT_RETRY_LIMIT + 1):
            try:
                if deadline is not None:
                    client.settimeout(_remaining(deadline))
                client.connect(str(socket_path))
                break
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                    raise GitViewspaceError(
                        "DAEMON_UNAVAILABLE", "git-viewspace control socket is absent", detail=str(error)
                    ) from error
                if error.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise
                if retry == CONNECT_RETRY_LIMIT:
                    raise GitViewspaceError(
                        "DAEMON_BUSY", "git-viewspace control socket is busy", detail=str(error)
                    ) from error
                delay = CONNECT_RETRY_INTERVAL_SECONDS
                if deadline is not None:
                    delay = min(delay, _remaining(deadline))
                time.sleep(delay)
        if deadline is not None:
            client.settimeout(_remaining(deadline))
        if command not in {"status", "view-status"}:
            arguments.setdefault("queue_timeout_seconds", min(timeout, 86400.0))
        send_message(client, request(command, **arguments))
        response = receive_message(client, "daemon response", deadline=deadline)
    except GitViewspaceError:
        raise
    except TimeoutError as error:
        raise GitViewspaceError(
            "DAEMON_TIMEOUT",
            "git-viewspace response timed out; inspect state before retrying a mutation",
            detail=str(error),
        ) from error
    except OSError as error:
        raise GitViewspaceError(
            "DAEMON_UNREACHABLE",
            "git-viewspace is unreachable; daemon absence is not established",
            detail=str(error),
        ) from error
    finally:
        try:
            client.close()
        except (OSError, UnboundLocalError):
            pass
    if not isinstance(response.get("ok"), bool):
        raise GitViewspaceError("PROTOCOL_ERROR", "daemon response has no result status")
    return response
