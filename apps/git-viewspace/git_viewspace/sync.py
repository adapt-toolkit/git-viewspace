"""Narrow privileged boundary for stopped-upper inspection and replay."""

import json
import shutil
import subprocess
from pathlib import Path

from .errors import GitViewspaceError

MAX_REPORT_BYTES = 64 * 1024 * 1024

_COUNT_KEYS = {
    "directories",
    "regulars",
    "symlinks",
    "whiteouts",
    "opaque",
    "origin",
    "impure",
}
_REPORT_KEYS = {
    "mode",
    "workload_uid",
    "workload_gid",
    "upper_fingerprint",
    "git_pathspecs",
    "counts",
    "redirects",
    "whiteouts",
    "opaque",
    "cleanup_pending",
}


def _digest(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", f"invalid {label}")


def _path(value, *, allow_root=False):
    if not isinstance(value, str):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync report contains a non-string path")
    if value == "." and allow_root:
        return
    if not value or value.startswith("/") or "\0" in value:
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync report contains an invalid path")
    components = value.split("/")
    if any(component in {"", ".", "..", ".git"} for component in components):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync report contains an invalid path")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync report path is not UTF-8") from error


def validate_report(value, expected_mode):
    if not isinstance(value, dict) or set(value) != _REPORT_KEYS:
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned an invalid report")
    if value["mode"] != expected_mode:
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned the wrong operation mode")
    for field in ("workload_uid", "workload_gid"):
        if type(value[field]) is not int or value[field] < 0:
            raise GitViewspaceError("SYNC_PROTOCOL_ERROR", f"sync helper returned an invalid {field}")
    _digest(value["upper_fingerprint"], "upper fingerprint")
    manifest = value["git_pathspecs"]
    if not isinstance(manifest, list) or manifest != sorted(set(manifest)):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned an invalid Git manifest")
    for path in manifest:
        _path(path, allow_root=True)
    counts = value["counts"]
    if not isinstance(counts, dict) or set(counts) != _COUNT_KEYS:
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned invalid artifact counts")
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned invalid artifact counts")
    for field in ("whiteouts", "opaque"):
        paths = value[field]
        if not isinstance(paths, list) or paths != sorted(set(paths)):
            raise GitViewspaceError("SYNC_PROTOCOL_ERROR", f"sync helper returned invalid {field}")
        for path in paths:
            _path(path, allow_root=field == "opaque")
    redirects = value["redirects"]
    if not isinstance(redirects, list):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned invalid redirects")
    for redirect in redirects:
        if not isinstance(redirect, dict) or set(redirect) != {"source", "destination"}:
            raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned invalid redirects")
        _path(redirect["source"])
        _path(redirect["destination"])
    if not isinstance(value["cleanup_pending"], bool):
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned invalid cleanup state")
    if expected_mode != "atomic-swap" and value["cleanup_pending"]:
        raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "direct sync reported pending cleanup")
    return value


def comparable_report(value):
    return {
        key: value[key]
        for key in _REPORT_KEYS
        if key not in {"mode", "cleanup_pending"}
    }


class OverlaySync:
    """Invoke the qualified helper with exactly the two allowed operations."""

    def __init__(
        self,
        *,
        script=None,
        sudo=None,
        python="/usr/bin/python3",
        run=subprocess.run,
        timeout=120.0,
    ):
        self.script = Path(
            script
            or Path(__file__).resolve().with_name("overlay_sync.py")
        ).resolve()
        self.sudo = str(Path(sudo or shutil.which("sudo") or "/usr/bin/sudo").resolve())
        self.python = str(Path(python).resolve())
        self._run = run
        self.timeout = timeout

    def _invoke(self, arguments, mode):
        command = (
            self.sudo,
            "-n",
            "--",
            self.python,
            "-I",
            str(self.script),
            *map(str, arguments),
        )
        try:
            result = self._run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitViewspaceError(
                "OVERLAY_SYNC_FAILED",
                "could not execute OverlaySync helper",
                detail=str(error),
            ) from error
        detail = result.stderr.decode("utf-8", "replace").strip()
        if result.returncode != 0:
            code = "UNSUPPORTED_OVERLAY_ARTIFACT" if result.returncode == 1 else "OVERLAY_SYNC_FAILED"
            raise GitViewspaceError(code, "OverlaySync helper rejected the stopped upper", detail=detail)
        # Whole-publication manifests can contain hundreds of thousands of
        # paths. Keep a finite bound without rejecting ordinary corpus sizes.
        if len(result.stdout) > MAX_REPORT_BYTES:
            raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper response is too large")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as error:
            raise GitViewspaceError("SYNC_PROTOCOL_ERROR", "sync helper returned invalid JSON") from error
        return validate_report(value, mode)

    def inspect(self, upper):
        return self._invoke(("--inspect-only", Path(upper)), "inspect")

    def replay(self, upper, checkout):
        return self._invoke((Path(upper), Path(checkout)), "direct")
