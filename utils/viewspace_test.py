#!/usr/bin/env python3
"""Unified, fail-closed test harness for VIEWSPACE repository tests."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import dataclasses
import difflib
import fcntl
import fnmatch
import functools
import hashlib
import html
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Sequence

# The public harness may be invoked directly from the checkout. Keep Python's import cache out of
# the repository; all legitimate runtime artifacts belong beneath the configured scratch root.
sys.dont_write_bytecode = True

if __package__:
    from .viewspace_test_tree import (
        TreeError,
        TreeEntry,
        TreeSnapshot,
        atomic_replace_directory,
        diff_snapshots,
        materialize_snapshot,
        snapshot_source,
        snapshot_zip_bytes,
    )
else:
    from viewspace_test_tree import (
        TreeError,
        TreeEntry,
        TreeSnapshot,
        atomic_replace_directory,
        diff_snapshots,
        materialize_snapshot,
        snapshot_source,
        snapshot_zip_bytes,
    )


DISCOVERY_PROTOCOL_VERSION = 2
REPORT_PROTOCOL_VERSION = 7
RESULT_PROTOCOL_VERSION = 1
CASE_SPEC_VERSION = 2
UNIT_DRIVERS = frozenset({"udir", "urun"})
DRIVERS = frozenset({*UNIT_DRIVERS, "cargo", "exec"})
VALID_EXPECTATIONS = frozenset({"fr", "sr"})
VALID_LABELS = frozenset({"host", "package", "performance", "privileged", "slow"})
SCOPE_RANK = {"basic": 0, "full": 1, "exhaustive": 2}
PROFILE_LABELS = {
    "default": frozenset(),
    "host": frozenset({"host"}),
    "full": VALID_LABELS,
}
EXEC_COMPARISONS = frozenset(
    {"filesystem", "filesystem-script", "status", "stderr", "stdout"}
)
TOP_LEVEL_TEST_DOCUMENTS = frozenset({"external_tests.md", "how_to_write_tests.md"})
SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
ARGUMENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
CASE_DIRECTORY_PATTERN = re.compile(
    r"^(?P<case>[a-z][a-z0-9_-]*)="
    r"(?P<driver>[a-z][a-z0-9_]*)\+(?P<expectation>[a-z]+)"
    r"$"
)
RESULT_PATTERN = re.compile(
    rf"^VIEWSPACE-TEST-RESULT {RESULT_PROTOCOL_VERSION} "
    r"(?P<id>[a-z][a-z0-9_-]*(?:::[a-z][a-z0-9_-]*)+) "
    r"(?P<outcome>PASS|FAIL)$"
)
CARGO_SELECTOR_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"
)

class HarnessError(Exception):
    """An infrastructure or suite-configuration failure."""


_PROCESS_HARNESS_LOCK = threading.RLock()
_HARNESS_LOCK_STATE = threading.local()


@contextmanager
def _harness_operation_lock(root: Path, *, create_parent: bool = False):
    """Serialize cooperating harness operations without adding a test-tree file."""

    parent = root.absolute().parent
    if create_parent:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise HarnessError(f"could not create test-root parent {parent}: {error}") from error
    with _PROCESS_HARNESS_LOCK:
        process_id = os.getpid()
        active = getattr(_HARNESS_LOCK_STATE, "active", None)
        if active is not None and active[0] == process_id:
            if active[1] != parent:
                raise HarnessError(
                    "one harness operation cannot nest a different test-root lock"
                )
            _HARNESS_LOCK_STATE.active = (process_id, parent, active[2] + 1)
            try:
                yield
            finally:
                _HARNESS_LOCK_STATE.active = (process_id, parent, active[2])
            return

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(parent, flags)
        except OSError as error:
            raise HarnessError(
                f"could not open test-root parent for locking {parent}: {error}"
            ) from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as error:
                raise HarnessError(
                    f"could not lock test-root parent {parent}: {error}"
                ) from error
            _HARNESS_LOCK_STATE.active = (process_id, parent, 1)
            yield
        finally:
            _HARNESS_LOCK_STATE.active = None
            os.close(descriptor)


@contextmanager
def _report_operation_lock(report_path: Path):
    """Claim a report destination for the complete run without lock-order deadlocks."""

    parent = report_path.resolve(strict=False).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HarnessError(f"could not create report parent {parent}: {error}") from error
    active = getattr(_HARNESS_LOCK_STATE, "active", None)
    if active is not None and active[0] == os.getpid() and active[1] == parent:
        yield
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        raise HarnessError(f"could not open report parent for locking {parent}: {error}") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HarnessError(f"report destination is already in use: {report_path}") from error
        except OSError as error:
            raise HarnessError(f"could not lock report destination {report_path}: {error}") from error
        yield
    finally:
        os.close(descriptor)


def _serialized_harness_operation(*, create_parent: bool = False):
    def decorate(function):
        @functools.wraps(function)
        def locked(root: Path, *args, **kwargs):
            try:
                canonical_root = root.resolve(strict=False)
            except OSError as error:
                raise HarnessError(f"could not resolve test root {root}: {error}") from error
            with _harness_operation_lock(canonical_root, create_parent=create_parent):
                return function(canonical_root, *args, **kwargs)

        return locked

    return decorate


_SUBREAPER_PID: int | None = None


def _enable_child_subreaper() -> None:
    """Make detached case descendants observable and reapable by this harness."""

    global _SUBREAPER_PID
    process_id = os.getpid()
    if _SUBREAPER_PID == process_id:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        raise HarnessError("Linux child-subreaper support is unavailable")
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    pr_set_child_subreaper = 36
    if prctl(pr_set_child_subreaper, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise HarnessError(
            f"could not enable child-subreaper supervision: {os.strerror(error_number)}"
        )
    _SUBREAPER_PID = process_id


def _process_parent_map() -> dict[int, int]:
    parents: dict[int, int] = {}
    try:
        process_entries = list(Path("/proc").iterdir())
    except OSError as error:
        raise HarnessError(f"could not inspect Linux process table: {error}") from error
    for entry in process_entries:
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        try:
            fields = (entry / "status").read_text(encoding="ascii").splitlines()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except (OSError, UnicodeDecodeError) as error:
            raise HarnessError(f"could not inspect process {entry.name}: {error}") from error
        parent_field = next((field for field in fields if field.startswith("PPid:\t")), None)
        if parent_field is None:
            raise HarnessError(f"process {entry.name} has no PPid in /proc")
        try:
            parents[int(entry.name)] = int(parent_field.removeprefix("PPid:\t"))
        except ValueError as error:
            raise HarnessError(f"process {entry.name} has an invalid PPid") from error

    return parents


def _descendant_processes(
    parent_pid: int,
) -> set[int]:
    parents = _process_parent_map()

    descendants: set[int] = set()
    frontier = {parent_pid}
    while frontier:
        children = {pid for pid, ppid in parents.items() if ppid in frontier}
        children -= descendants
        if not children:
            break
        descendants.update(children)
        frontier = children

    return descendants


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_adopted_children(process_ids: set[int]) -> None:
    for process_id in process_ids:
        try:
            os.waitpid(process_id, os.WNOHANG)
        except ChildProcessError:
            continue


def _reap_exited_adopted_children(controlled_pid: int) -> None:
    """Reap exited orphans while leaving the Popen-owned leader untouched."""

    while True:
        try:
            status = os.waitid(
                os.P_ALL,
                0,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return
        if status is None or status.si_pid == controlled_pid:
            return
        try:
            os.waitpid(status.si_pid, 0)
        except ChildProcessError:
            continue


def _terminate_case_descendants(
    group_id: int,
) -> str | None:
    descendants = _descendant_processes(os.getpid())
    _reap_adopted_children(descendants)
    descendants = _descendant_processes(os.getpid())
    group_exists = _process_group_exists(group_id)
    if not descendants and not group_exists:
        return None

    deadline = time.monotonic() + 2.0
    while True:
        if _process_group_exists(group_id):
            try:
                os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        candidates = _descendant_processes(os.getpid())
        for process_id in candidates:
            try:
                os.kill(process_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _reap_adopted_children(candidates)
        descendants = _descendant_processes(os.getpid())
        group_exists = _process_group_exists(group_id)
        if not descendants and not group_exists:
            return "controlled process left descendant processes running"
        if time.monotonic() >= deadline:
            remaining = ", ".join(str(pid) for pid in sorted(descendants)) or "unknown"
            return (
                "controlled process left descendant processes running and cleanup "
                f"could not prove quiescence (remaining: {remaining})"
            )
        time.sleep(0.01)


def _abort_controlled_process(
    process: subprocess.Popen[bytes],
) -> None:
    cleanup_errors = []
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        cleanup_errors.append(f"could not kill controlled process group: {error}")
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_errors.append(f"could not kill controlled process: {error}")
    try:
        process.wait()
    except OSError as error:
        cleanup_errors.append(f"could not reap controlled process: {error}")
    try:
        descendant_error = _terminate_case_descendants(process.pid)
        if descendant_error is not None and "could not prove quiescence" in descendant_error:
            cleanup_errors.append(descendant_error)
    except HarnessError as error:
        cleanup_errors.append(str(error))
    if cleanup_errors:
        raise HarnessError("; ".join(cleanup_errors))


class _SupervisorTermination(BaseException):
    """Private signal escape used by the dedicated process supervisor."""


def _set_parent_death_signal(expected_parent: int) -> None:
    """Kill the controlled leader if its dedicated supervisor disappears."""

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        os._exit(127)
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    pr_set_pdeathsig = 1
    if prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(127)
    if os.getppid() != expected_parent:
        os._exit(127)


def _arm_supervisor_parent_death(expected_parent: int) -> None:
    """Arrange graceful owned-tree cleanup when the outer harness disappears."""

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        raise HarnessError("Linux parent-death supervision is unavailable")
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    pr_set_pdeathsig = 1
    if prctl(pr_set_pdeathsig, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise HarnessError(
            f"could not arm dedicated supervisor parent-death handling: "
            f"{os.strerror(error_number)}"
        )
    if os.getppid() != expected_parent:
        raise _SupervisorTermination(signal.SIGTERM)


def _supervise_owned_process(
    arguments: Sequence[str],
    *,
    cwd: Path | None,
    environment: dict[str, str] | None,
    pass_fds: tuple[int, ...],
    stdout_descriptor: int,
    stderr_descriptor: int,
) -> tuple[int, str | None]:
    """Run a target in a fresh helper whose complete child tree is owned by the run."""

    _enable_child_subreaper()
    if _descendant_processes(os.getpid()):
        raise HarnessError("dedicated process supervisor did not start with an empty child tree")

    expected_parent = os.getpid()
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_descriptor,
            stderr=stderr_descriptor,
            start_new_session=True,
            pass_fds=pass_fds,
            preexec_fn=lambda: _set_parent_death_signal(expected_parent),
        )
    except OSError as error:
        raise HarnessError(f"could not start controlled process: {error}") from error
    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            _reap_exited_adopted_children(process.pid)
            time.sleep(0.01)
        descendant_error = _terminate_case_descendants(process.pid)
    except BaseException as original_error:
        try:
            _abort_controlled_process(process)
        except HarnessError as cleanup_error:
            raise HarnessError(
                f"controlled process was interrupted and cleanup failed: {cleanup_error}"
            ) from original_error
        raise
    return return_code, descendant_error


def _parse_supervisor_request(
    raw_request: bytes,
) -> tuple[list[str], Path | None, dict[str, str] | None, tuple[int, ...], int, int]:
    try:
        request = json.loads(raw_request.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("dedicated process supervisor received a malformed request") from error
    if not isinstance(request, dict) or set(request) != {
        "arguments",
        "cwd",
        "environment",
        "pass_fds",
        "stderr_fd",
        "stdout_fd",
    }:
        raise HarnessError("dedicated process supervisor received an invalid request shape")
    arguments = request["arguments"]
    cwd = request["cwd"]
    environment = request["environment"]
    pass_fds = request["pass_fds"]
    stdout_descriptor = request["stdout_fd"]
    stderr_descriptor = request["stderr_fd"]
    if (
        not isinstance(arguments, list)
        or not arguments
        or any(not isinstance(argument, str) or "\0" in argument for argument in arguments)
    ):
        raise HarnessError("dedicated process supervisor received invalid arguments")
    if cwd is not None and (not isinstance(cwd, str) or "\0" in cwd):
        raise HarnessError("dedicated process supervisor received an invalid working directory")
    if environment is not None and (
        not isinstance(environment, dict)
        or any(
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\0" in name
            or not isinstance(value, str)
            or "\0" in value
            for name, value in environment.items()
        )
    ):
        raise HarnessError("dedicated process supervisor received an invalid environment")
    if (
        not isinstance(pass_fds, list)
        or any(type(descriptor) is not int or descriptor < 3 for descriptor in pass_fds)
        or len(pass_fds) != len(set(pass_fds))
    ):
        raise HarnessError("dedicated process supervisor received invalid inherited descriptors")
    if (
        type(stdout_descriptor) is not int
        or type(stderr_descriptor) is not int
        or stdout_descriptor < 3
        or stderr_descriptor < 3
        or stdout_descriptor == stderr_descriptor
    ):
        raise HarnessError("dedicated process supervisor received invalid output descriptors")
    for descriptor in (*pass_fds, stdout_descriptor, stderr_descriptor):
        try:
            os.fstat(descriptor)
        except OSError as error:
            raise HarnessError(
                "dedicated process supervisor did not inherit every requested descriptor"
            ) from error
    return (
        arguments,
        None if cwd is None else Path(cwd),
        environment,
        tuple(pass_fds),
        stdout_descriptor,
        stderr_descriptor,
    )


def _internal_supervisor_main(expected_parent: int) -> int:
    def terminate(signum: int, _frame: object) -> None:
        raise _SupervisorTermination(signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        _arm_supervisor_parent_death(expected_parent)
        request = _parse_supervisor_request(sys.stdin.buffer.read())
        return_code, descendant_error = _supervise_owned_process(
            request[0],
            cwd=request[1],
            environment=request[2],
            pass_fds=request[3],
            stdout_descriptor=request[4],
            stderr_descriptor=request[5],
        )
        response = {
            "descendant_error": descendant_error,
            "returncode": return_code,
        }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except _SupervisorTermination as interrupted:
        return 128 + int(interrupted.args[0])
    except (HarnessError, OSError) as error:
        encoded_error = str(error).encode("utf-8", errors="backslashreplace")
        suffix = b"... [truncated]"
        if len(encoded_error) > 4096:
            encoded_error = encoded_error[: 4096 - len(suffix)] + suffix
        response = {"error": encoded_error.decode("utf-8", errors="ignore")}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0


def _stop_dedicated_supervisor(supervisor: subprocess.Popen[bytes]) -> None:
    try:
        supervisor.terminate()
    except ProcessLookupError:
        pass
    try:
        supervisor.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        supervisor.kill()
    except ProcessLookupError:
        pass
    try:
        supervisor.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise HarnessError("could not stop dedicated process supervisor") from error


def _run_quiescent_process(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    timeout_seconds: float | None = None,
    temporary_root: Path | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], str | None]:
    """Run a process under a dedicated subreaper and prove its tree is quiescent."""

    normalized_arguments = [os.fspath(argument) for argument in arguments]
    if not normalized_arguments:
        raise HarnessError("controlled process arguments must not be empty")
    if len(pass_fds) != len(set(pass_fds)):
        raise HarnessError("controlled process descriptors must be unique")
    with tempfile.TemporaryFile(dir=temporary_root) as stdout_file, tempfile.TemporaryFile(
        dir=temporary_root
    ) as stderr_file:
        inherited_descriptors = tuple(
            dict.fromkeys((*pass_fds, stdout_file.fileno(), stderr_file.fileno()))
        )
        request = {
            "arguments": normalized_arguments,
            "cwd": None if cwd is None else os.fspath(cwd),
            "environment": environment,
            "pass_fds": list(pass_fds),
            "stderr_fd": stderr_file.fileno(),
            "stdout_fd": stdout_file.fileno(),
        }
        try:
            supervisor = subprocess.Popen(
                [
                    sys.executable,
                    os.path.abspath(__file__),
                    "--_internal-supervisor",
                    str(os.getpid()),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=inherited_descriptors,
            )
        except OSError as error:
            raise HarnessError(f"could not start dedicated process supervisor: {error}") from error
        try:
            supervisor_stdout, supervisor_stderr = supervisor.communicate(
                json.dumps(request, separators=(",", ":")).encode("utf-8"),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            try:
                _stop_dedicated_supervisor(supervisor)
            except HarnessError as cleanup_error:
                raise HarnessError(
                    f"controlled process timed out and cleanup failed: {cleanup_error}"
                ) from error
            finally:
                if supervisor.stdout is not None:
                    supervisor.stdout.close()
                if supervisor.stderr is not None:
                    supervisor.stderr.close()
            raise HarnessError(
                f"controlled process timed out after {timeout_seconds:g} seconds"
            ) from error
        except BaseException as original_error:
            try:
                _stop_dedicated_supervisor(supervisor)
            except HarnessError as cleanup_error:
                raise HarnessError(
                    f"process supervision was interrupted and cleanup failed: {cleanup_error}"
                ) from original_error
            finally:
                if supervisor.stdout is not None:
                    supervisor.stdout.close()
                if supervisor.stderr is not None:
                    supervisor.stderr.close()
            raise

        if supervisor.stdout is not None:
            supervisor.stdout.close()
        if supervisor.stderr is not None:
            supervisor.stderr.close()
        if supervisor.returncode != 0:
            detail = supervisor_stderr.decode("utf-8", errors="backslashreplace").strip()
            suffix = f": {detail}" if detail else ""
            raise HarnessError(
                f"dedicated process supervisor exited with status {supervisor.returncode}{suffix}"
            )
        if supervisor_stderr:
            raise HarnessError(
                "dedicated process supervisor wrote to stderr: "
                + supervisor_stderr.decode("utf-8", errors="backslashreplace")
            )
        try:
            response = json.loads(supervisor_stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HarnessError("dedicated process supervisor returned malformed status") from error
        if isinstance(response, dict) and set(response) == {"error"}:
            if not isinstance(response["error"], str):
                raise HarnessError("dedicated process supervisor returned invalid error status")
            raise HarnessError(response["error"])
        if (
            not isinstance(response, dict)
            or set(response) != {"descendant_error", "returncode"}
            or type(response["returncode"]) is not int
            or (
                response["descendant_error"] is not None
                and not isinstance(response["descendant_error"], str)
            )
        ):
            raise HarnessError("dedicated process supervisor returned invalid status")

        stdout_file.seek(0)
        stderr_file.seek(0)
        completed = subprocess.CompletedProcess(
            args=normalized_arguments,
            returncode=response["returncode"],
            stdout=stdout_file.read(),
            stderr=stderr_file.read(),
        )
    return completed, response["descendant_error"]


@dataclasses.dataclass(frozen=True)
class RegisteredTest:
    test_id: str
    driver: str


@dataclasses.dataclass
class PinnedBinary:
    requested_path: Path
    source_path: Path
    descriptor: int
    sha256: str
    source_signature: tuple[int, int, int, int, int]

    @property
    def executable(self) -> str:
        return f"/proc/self/fd/{self.descriptor}"

    def verify_source(self) -> None:
        try:
            if self.requested_path.resolve(strict=True) != self.source_path:
                raise HarnessError("unit-test binary path changed during the operation")
            descriptor = os.open(
                self.source_path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except HarnessError:
            raise
        except OSError as error:
            raise HarnessError(f"could not re-open unit-test binary: {error}") from error
        try:
            before_digest = os.fstat(descriptor)
            if _binary_signature(before_digest) != self.source_signature:
                raise HarnessError("unit-test binary changed during the operation")
            if _descriptor_digest(descriptor) != self.sha256:
                raise HarnessError("unit-test binary changed during the operation")
            after_digest = os.fstat(descriptor)
            if _binary_signature(after_digest) != _binary_signature(before_digest):
                raise HarnessError("unit-test binary changed during verification")
            try:
                path_stat = self.source_path.lstat()
                final_requested_path = self.requested_path.resolve(strict=True)
            except OSError as error:
                raise HarnessError(
                    f"could not revalidate unit-test binary path: {error}"
                ) from error
            if _binary_signature(path_stat) != _binary_signature(after_digest):
                raise HarnessError("unit-test binary path changed during verification")
            if final_requested_path != self.source_path:
                raise HarnessError("unit-test binary path changed during the operation")
        finally:
            os.close(descriptor)


def _binary_signature(binary_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        binary_stat.st_dev,
        binary_stat.st_ino,
        binary_stat.st_size,
        binary_stat.st_mtime_ns,
        binary_stat.st_ctime_ns,
    )


def _descriptor_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        try:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
        except OSError as error:
            raise HarnessError(f"could not fingerprint unit-test binary: {error}") from error
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


@contextmanager
def _pin_binary(binary: Path):
    requested = binary.absolute()
    try:
        source = requested.resolve(strict=True)
        source_descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise HarnessError(f"could not open unit-test binary {binary}: {error}") from error
    pinned_descriptor = -1
    try:
        try:
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise HarnessError(f"unit-test binary is not a regular file: {source}")
            if before.st_mode & 0o111 == 0:
                raise HarnessError(f"unit-test binary is not executable: {source}")

            pinned_descriptor = os.memfd_create(
                "viewspace-unit-tests",
                os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
            )
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                copied += len(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(pinned_descriptor, remaining)
                    remaining = remaining[written:]
            after = os.fstat(source_descriptor)
            if _binary_signature(after) != _binary_signature(before) or copied != before.st_size:
                raise HarnessError("unit-test binary changed while it was being pinned")
            os.fchmod(pinned_descriptor, 0o500)
            seals = (
                fcntl.F_SEAL_SEAL
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_WRITE
            )
            fcntl.fcntl(pinned_descriptor, fcntl.F_ADD_SEALS, seals)
            pinned = PinnedBinary(
                requested_path=requested,
                source_path=source,
                descriptor=pinned_descriptor,
                sha256=digest.hexdigest(),
                source_signature=_binary_signature(before),
            )
        except HarnessError:
            raise
        except OSError as error:
            raise HarnessError(f"could not pin unit-test binary {binary}: {error}") from error
        yield pinned
    finally:
        os.close(source_descriptor)
        if pinned_descriptor >= 0:
            os.close(pinned_descriptor)


@contextmanager
def _using_pinned_binary(binary: Path | PinnedBinary):
    if isinstance(binary, PinnedBinary):
        yield binary
        return
    with _pin_binary(binary) as pinned:
        yield pinned



@dataclasses.dataclass(frozen=True)
class UdirSpec:
    cwd: str
    args: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class ExecSpec:
    """Validated direct-process invocation and comparison policy."""

    argv: tuple[str, ...]
    cwd_anchor: str
    cwd: str
    environment_inherit: tuple[str, ...]
    environment_set: tuple[tuple[str, str], ...]
    timeout_seconds: float
    comparisons: frozenset[str]
    filesystem: str | None


@dataclasses.dataclass(frozen=True)
class CargoSpec:
    """One exact libtest case selected from one shared Cargo build."""

    package: str
    target_kind: str
    target_name: str
    timeout_seconds: float


@dataclasses.dataclass(frozen=True)
class GoldenExpectation:
    comparisons: frozenset[str]
    stdout: str | None = None
    stderr: str | None = None
    status: int | None = None
    filesystem: TreeSnapshot | None = None
    filesystem_source: str | None = None
    filesystem_archive_sha256: str | None = None


@dataclasses.dataclass(frozen=True)
class CaseDefinition:
    case_id: str
    path: Path
    selector: str
    driver: str
    expectation: str
    scope: str | None
    labels: tuple[str, ...]
    prerequisites: tuple[str, ...]
    input_tree: TreeSnapshot
    udir_spec: UdirSpec | None
    exec_spec: ExecSpec | None
    cargo_spec: CargoSpec | None
    golden: GoldenExpectation
    comparator_sha256: str | None = None

    @property
    def source_name(self) -> str:
        return "/".join(self.path.parts[-3:])

    @property
    def golden_path(self) -> Path:
        return self.path / "golden"

    @property
    def comparator_path(self) -> Path:
        return self.path / "compare.py"


@dataclasses.dataclass(frozen=True)
class DriverResult:
    outcome: str
    stdout: str
    stderr: str
    error: str | None = None
    filesystem: TreeSnapshot | None = None
    status: int | None = None
    comparison_outcome: str | None = None
    comparison_stdout: str = ""
    comparison_stderr: str = ""


@dataclasses.dataclass(frozen=True)
class CaseResult:
    case_id: str
    selector: str
    driver: str
    source: str
    expectation: str
    scope: str | None
    labels: tuple[str, ...]
    prerequisites: tuple[str, ...]
    driver_outcome: str
    status: str
    duration_ms: int
    stdout: str
    stderr: str
    comparison_stdout: str = ""
    comparison_stderr: str = ""
    message: str | None = None
    diff: str | None = None
    golden_updated: bool = False


@dataclasses.dataclass(frozen=True)
class Selection:
    cases: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    globs: tuple[str, ...] = ()
    scope: str = "exhaustive"
    profile: str = "default"
    explicit_all: bool = False

    @property
    def has_filter(self) -> bool:
        return bool(self.cases or self.segments or self.tags or self.globs)


def is_valid_test_id(test_id: str) -> bool:
    segments = test_id.split("::")
    return len(segments) >= 2 and all(SEGMENT_PATTERN.fullmatch(segment) for segment in segments)


def case_path_for(
    root: Path,
    case_id: str,
    driver: str = "urun",
    expectation: str = "sr",
) -> Path:
    segments = case_id.split("::")
    if len(segments) != 3 or not all(SEGMENT_PATTERN.fullmatch(value) for value in segments):
        raise HarnessError(f"invalid case ID: {case_id}")
    if driver not in DRIVERS:
        raise HarnessError(f"invalid test driver: {driver}")
    if expectation not in VALID_EXPECTATIONS:
        raise HarnessError(f"invalid expectation: {expectation}")
    component, topic, case = segments
    return root / component / topic / f"{case}={driver}+{expectation}"


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _load_json_object(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise HarnessError(f"duplicate JSON field in {path.name}: {name!r}")
            result[name] = value
        return result

    try:
        contents = path.read_text(encoding="utf-8")
        loaded = json.loads(contents, object_pairs_hook=reject_duplicates)
    except HarnessError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"could not read strict JSON from {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise HarnessError(f"{path.name} must contain a JSON object")
    return loaded


def _strict_directory_entries(
    directory: Path,
    expected: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Path]:
    try:
        with os.scandir(directory) as scanned:
            entries = list(scanned)
    except OSError as error:
        raise HarnessError(f"could not inspect test directory {directory}: {error}") from error
    names = {entry.name for entry in entries}
    if not expected <= names or not names <= expected | optional:
        missing = sorted(expected - names)
        unexpected = sorted(names - expected - optional, key=os.fsencode)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append(
                "unexpected " + ", ".join(repr(os.fsencode(name)) for name in unexpected)
            )
        raise HarnessError(f"invalid layout in {directory}: " + "; ".join(details))
    paths: dict[str, Path] = {}
    for entry in entries:
        if entry.is_symlink():
            raise HarnessError(f"symlinks are not allowed in test layout: {entry.path}")
        paths[entry.name] = Path(entry.path)
    return paths


def _validate_udir_config(path: Path, loaded: dict[str, Any], tree: TreeSnapshot) -> UdirSpec:
    expected = {"args", "cwd"}
    if set(loaded) != expected:
        missing = sorted(expected - set(loaded))
        unknown = sorted(set(loaded) - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(repr(value) for value in missing))
        if unknown:
            details.append("unknown " + ", ".join(repr(value) for value in unknown))
        raise HarnessError(f"{path.name} udir config has invalid fields: " + "; ".join(details))
    cwd = loaded["cwd"]
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd or _contains_surrogate(cwd):
        raise HarnessError("spec.json udir field 'cwd' must be a nonempty valid string")
    if os.path.isabs(cwd):
        raise HarnessError("spec.json udir field 'cwd' must be relative to files/")
    entries = {entry.path: entry.kind for entry in tree}
    resolved_parts: list[bytes] = []
    for component in cwd.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not resolved_parts:
                raise HarnessError("spec.json udir field 'cwd' escapes files/")
            resolved_parts.pop()
            continue
        resolved_parts.append(os.fsencode(component))
        if entries.get(b"/".join(resolved_parts)) != "directory":
            raise HarnessError("spec.json udir field 'cwd' does not name a fixture directory")
    normalized_cwd = os.fsdecode(b"/".join(resolved_parts)) if resolved_parts else "."
    raw_args = loaded["args"]
    if not isinstance(raw_args, dict):
        raise HarnessError("spec.json udir field 'args' must be an object")
    args: list[tuple[str, str]] = []
    for name, value in raw_args.items():
        if not isinstance(name, str) or ARGUMENT_NAME_PATTERN.fullmatch(name) is None:
            raise HarnessError(f"invalid named unit-test argument: {name!r}")
        if not isinstance(value, str) or "\x00" in value or _contains_surrogate(value):
            raise HarnessError(f"unit-test argument {name!r} must be a valid string")
        args.append((name, value))
    args.sort()
    return UdirSpec(normalized_cwd, tuple(args))


def _validate_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or _contains_surrogate(value):
        raise HarnessError(f"spec.json field {field!r} must be a nonempty valid string")
    if os.path.isabs(value):
        raise HarnessError(f"spec.json field {field!r} must be relative")
    parts: list[str] = []
    for component in value.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not parts:
                raise HarnessError(f"spec.json field {field!r} escapes its anchor")
            parts.pop()
        else:
            parts.append(component)
    return "/".join(parts) or "."


def _validate_timeout(value: object) -> float:
    if type(value) not in (int, float) or not 0.05 <= value <= 3600:
        raise HarnessError("spec.json timeout_seconds must be between 0.05 and 3600")
    return float(value)


def _validate_exec_config(loaded: dict[str, Any], tree: TreeSnapshot) -> ExecSpec:
    expected = {"argv", "compare", "cwd", "environment", "filesystem", "timeout_seconds"}
    if set(loaded) != expected:
        raise HarnessError(
            "spec.json exec config has invalid fields: "
            + ", ".join(sorted(set(loaded) ^ expected))
        )
    argv = loaded["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(value, str) or not value or "\0" in value for value in argv)
    ):
        raise HarnessError("spec.json exec argv must be a nonempty array of valid strings")
    cwd = loaded["cwd"]
    if not isinstance(cwd, dict) or set(cwd) != {"anchor", "path"}:
        raise HarnessError("spec.json exec cwd must contain exactly anchor and path")
    anchor = cwd["anchor"]
    if anchor not in ("repository", "workspace"):
        raise HarnessError("spec.json exec cwd anchor must be repository or workspace")
    relative_cwd = _validate_relative_path(cwd["path"], "config.cwd.path")
    if anchor == "workspace" and relative_cwd != ".":
        entries = {entry.path: entry.kind for entry in tree}
        if entries.get(os.fsencode(relative_cwd)) != "directory":
            raise HarnessError("spec.json exec cwd does not name a fixture directory")
    environment = loaded["environment"]
    if not isinstance(environment, dict) or set(environment) != {"inherit", "set"}:
        raise HarnessError("spec.json exec environment must contain exactly inherit and set")
    inherited = environment["inherit"]
    assigned = environment["set"]
    variable_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    if (
        not isinstance(inherited, list)
        or any(not isinstance(name, str) or variable_pattern.fullmatch(name) is None for name in inherited)
        or len(inherited) != len(set(inherited))
    ):
        raise HarnessError("spec.json exec environment inherit must contain unique names")
    if not isinstance(assigned, dict) or any(
        variable_pattern.fullmatch(name) is None
        or not isinstance(value, str)
        or "\0" in value
        or _contains_surrogate(value)
        for name, value in assigned.items()
    ):
        raise HarnessError("spec.json exec environment set must contain valid string variables")
    if set(inherited) & set(assigned):
        raise HarnessError("spec.json exec environment cannot both inherit and set a variable")
    compare = loaded["compare"]
    if (
        not isinstance(compare, list)
        or not compare
        or any(value not in EXEC_COMPARISONS for value in compare)
        or len(compare) != len(set(compare))
    ):
        raise HarnessError("spec.json exec compare contains an invalid or duplicate field")
    comparisons = frozenset(compare)
    if {"filesystem", "filesystem-script"} <= comparisons:
        raise HarnessError(
            "spec.json exec compare cannot select both filesystem comparison modes"
        )
    filesystem = loaded["filesystem"]
    if comparisons & {"filesystem", "filesystem-script"}:
        filesystem = _validate_relative_path(filesystem, "config.filesystem")
    elif filesystem is not None:
        raise HarnessError("spec.json exec filesystem must be null unless compared")
    return ExecSpec(
        tuple(argv),
        anchor,
        relative_cwd,
        tuple(sorted(inherited)),
        tuple(sorted(assigned.items())),
        _validate_timeout(loaded["timeout_seconds"]),
        comparisons,
        filesystem,
    )


def _validate_cargo_config(loaded: dict[str, Any], tree: TreeSnapshot) -> CargoSpec:
    expected = {"package", "target", "timeout_seconds"}
    if set(loaded) != expected:
        raise HarnessError(
            "spec.json cargo config has invalid fields: "
            + ", ".join(sorted(set(loaded) ^ expected))
        )
    if tree:
        raise HarnessError("cargo files/ must be empty")
    package = loaded["package"]
    target = loaded["target"]
    if not isinstance(package, str) or SEGMENT_PATTERN.fullmatch(package) is None:
        raise HarnessError("spec.json cargo package is invalid")
    if not isinstance(target, dict) or set(target) != {"kind", "name"}:
        raise HarnessError("spec.json cargo target must contain exactly kind and name")
    kind = target["kind"]
    name = target["name"]
    if kind not in ("bin", "lib", "test"):
        raise HarnessError("spec.json cargo target kind must be bin, lib, or test")
    if not isinstance(name, str) or SEGMENT_PATTERN.fullmatch(name.replace("_", "-")) is None:
        raise HarnessError("spec.json cargo target name is invalid")
    return CargoSpec(package, kind, name, _validate_timeout(loaded["timeout_seconds"]))


def _validate_string_array(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
    ):
        raise HarnessError(f"spec.json field {field!r} must be an array of unique strings")
    return tuple(sorted(value))


def _load_case_spec(
    path: Path,
    directory_driver: str,
    input_tree: TreeSnapshot,
) -> tuple[
    str,
    str | None,
    tuple[str, ...],
    tuple[str, ...],
    UdirSpec | None,
    ExecSpec | None,
    CargoSpec | None,
]:
    loaded = _load_json_object(path)
    required = {"config", "driver", "labels", "prerequisites", "schema_version", "selector"}
    allowed = required | {"scope"}
    if not required <= set(loaded) or not set(loaded) <= allowed:
        missing = sorted(required - set(loaded))
        unknown = sorted(set(loaded) - allowed)
        details = []
        if missing:
            details.append("missing " + ", ".join(repr(value) for value in missing))
        if unknown:
            details.append("unknown " + ", ".join(repr(value) for value in unknown))
        raise HarnessError("spec.json has invalid fields: " + "; ".join(details))
    if type(loaded["schema_version"]) is not int or loaded["schema_version"] != CASE_SPEC_VERSION:
        raise HarnessError(f"unsupported case schema version: {loaded['schema_version']!r}")
    driver = loaded["driver"]
    if not isinstance(driver, str) or driver not in DRIVERS:
        raise HarnessError(f"spec.json has invalid driver: {driver!r}")
    if driver != directory_driver:
        raise HarnessError(
            f"case directory driver {directory_driver!r} disagrees with spec.json {driver!r}"
        )
    selector = loaded["selector"]
    selector_is_valid = (
        isinstance(selector, str)
        and (
            CARGO_SELECTOR_PATTERN.fullmatch(selector) is not None
            if driver == "cargo"
            else is_valid_test_id(selector)
        )
    )
    if not selector_is_valid:
        raise HarnessError(f"spec.json has invalid test selector: {selector!r}")
    scope = None
    if "scope" in loaded:
        scope = loaded["scope"]
        if not isinstance(scope, str) or scope not in SCOPE_RANK:
            raise HarnessError(f"spec.json has invalid scope: {scope!r}")
    config = loaded["config"]
    if not isinstance(config, dict):
        raise HarnessError("spec.json field 'config' must be an object")
    labels = _validate_string_array(loaded["labels"], "labels")
    unknown_labels = sorted(set(labels) - VALID_LABELS)
    if unknown_labels:
        raise HarnessError("spec.json contains unknown labels: " + ", ".join(unknown_labels))
    if scope == "basic" and labels:
        raise HarnessError("spec.json basic scope cannot use scheduling labels")
    prerequisites = _validate_string_array(loaded["prerequisites"], "prerequisites")
    for prerequisite in prerequisites:
        if prerequisite in ("linux", "root", "scratch-ext4", "unix-seqpacket"):
            continue
        prefix, separator, name = prerequisite.partition(":")
        if separator != ":" or prefix not in ("command", "env", "python-module") or not name:
            raise HarnessError(f"invalid prerequisite: {prerequisite!r}")
    if driver == "urun":
        if config:
            raise HarnessError("urun spec.json config must be empty")
        if input_tree:
            raise HarnessError("urun files/ must be empty")
        return selector, scope, labels, prerequisites, None, None, None
    if driver == "udir":
        return (
            selector,
            scope,
            labels,
            prerequisites,
            _validate_udir_config(path, config, input_tree),
            None,
            None,
        )
    if driver == "exec":
        return (
            selector,
            scope,
            labels,
            prerequisites,
            None,
            _validate_exec_config(config, input_tree),
            None,
        )
    return (
        selector,
        scope,
        labels,
        prerequisites,
        None,
        None,
        _validate_cargo_config(config, input_tree),
    )


def _load_input_tree(files_path: Path, *, temporary_root: Path | None) -> TreeSnapshot:
    try:
        with os.scandir(files_path) as scanned:
            children = list(scanned)
    except OSError as error:
        raise HarnessError(f"could not inspect files/ fixture {files_path}: {error}") from error
    if len(children) == 1 and children[0].name == "source.zip":
        source = Path(children[0].path)
        if children[0].is_symlink() or not children[0].is_file(follow_symlinks=False):
            raise HarnessError("files/source.zip must be a real regular archive")
    elif any(child.name == "source.zip" for child in children):
        raise HarnessError("files/source.zip must be the only entry in files/")
    else:
        source = files_path
    try:
        tree = snapshot_source(source, temporary_root=temporary_root)
    except TreeError as error:
        raise HarnessError(str(error)) from error
    if not tree:
        raise HarnessError("empty files/ is redundant; omit it when the case has no fixture")
    if any(entry.path.rsplit(b"/", 1)[-1] == b".gitkeep" for entry in tree):
        raise HarnessError("files/.gitkeep is not a fixture; omit files/ when it would be empty")
    return tree


def _split_udir_archive(snapshot: TreeSnapshot, path: Path) -> tuple[str, TreeSnapshot]:
    entries = {entry.path: entry for entry in snapshot}
    stdout_entry = entries.get(b"stdout")
    files_entry = entries.get(b"files")
    if stdout_entry is None or stdout_entry.kind != "file":
        raise HarnessError(f"{path} must contain a regular stdout file")
    if files_entry is None or files_entry.kind != "directory":
        raise HarnessError(f"{path} must contain a files/ directory")
    unexpected = sorted(
        entry.path
        for entry in snapshot
        if entry.path not in (b"stdout", b"files") and not entry.path.startswith(b"files/")
    )
    if unexpected:
        raise HarnessError(f"{path} contains an unexpected archive path: {unexpected[0]!r}")
    try:
        stdout = stdout_entry.contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HarnessError(f"{path} stdout is not UTF-8") from error
    tree = tuple(
        dataclasses.replace(entry, path=entry.path.removeprefix(b"files/"))
        for entry in snapshot
        if entry.path.startswith(b"files/")
    )
    return stdout, tree


def _load_golden(
    golden_path: Path,
    driver: str,
    exec_spec: ExecSpec | None,
    *,
    allow_missing: bool,
    temporary_root: Path | None,
) -> GoldenExpectation:
    try:
        with os.scandir(golden_path) as scanned:
            entries = list(scanned)
    except OSError as error:
        raise HarnessError(f"could not inspect golden directory {golden_path}: {error}") from error
    if any(entry.is_symlink() for entry in entries):
        raise HarnessError(f"symlinks are not allowed in golden/: {golden_path}")
    names = {entry.name for entry in entries}
    if not names:
        if allow_missing:
            comparisons = (
                exec_spec.comparisons
                if exec_spec is not None
                else frozenset({"status"}) if driver == "cargo" else frozenset({"stdout"})
            )
            if driver == "udir":
                comparisons = frozenset({"filesystem", "stdout"})
            return GoldenExpectation(comparisons)
        raise HarnessError(f"missing golden for case: {golden_path.parent}")
    if driver == "urun":
        paths = _strict_directory_entries(golden_path, frozenset({"stdout"}))
        if not paths["stdout"].is_file():
            raise HarnessError(f"urun golden stdout is not a regular file: {paths['stdout']}")
        try:
            return GoldenExpectation(
                frozenset({"stdout"}), stdout=paths["stdout"].read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError) as error:
            raise HarnessError(f"urun golden stdout is not readable UTF-8: {error}") from error
    if driver == "udir" and names == {"expected.zip"}:
        archive = golden_path / "expected.zip"
        if not archive.is_file():
            raise HarnessError(f"udir golden archive is not a regular file: {archive}")
        try:
            snapshot = snapshot_source(archive, temporary_root=temporary_root)
        except TreeError as error:
            raise HarnessError(str(error)) from error
        stdout, tree = _split_udir_archive(snapshot, archive)
        return GoldenExpectation(
            frozenset({"filesystem", "stdout"}),
            stdout=stdout,
            filesystem=tree,
            filesystem_source="archive",
        )
    if driver == "udir" and names == {"files", "stdout"}:
        paths = _strict_directory_entries(golden_path, frozenset({"files", "stdout"}))
        if not paths["stdout"].is_file() or not paths["files"].is_dir():
            raise HarnessError(f"udir expanded golden has invalid types: {golden_path}")
        try:
            stdout = paths["stdout"].read_text(encoding="utf-8")
            tree = snapshot_source(paths["files"], temporary_root=temporary_root)
        except (OSError, UnicodeDecodeError, TreeError) as error:
            raise HarnessError(f"could not read expanded udir golden {golden_path}: {error}") from error
        return GoldenExpectation(
            frozenset({"filesystem", "stdout"}),
            stdout=stdout,
            filesystem=tree,
            filesystem_source="expanded",
        )
    if driver == "udir":
        raise HarnessError(
            f"udir golden must contain only expected.zip or stdout plus files/: {golden_path}"
        )

    comparisons = frozenset({"status"}) if driver == "cargo" else exec_spec.comparisons
    expected_names = set(comparisons) - {"filesystem", "filesystem-script"}
    if comparisons & {"filesystem", "filesystem-script"}:
        if "expected.zip" in names:
            expected_names.add("expected.zip")
            filesystem_source = (
                "script" if "filesystem-script" in comparisons else "archive"
            )
        elif "filesystem-script" in comparisons:
            expected_names.add("expected.zip")
            filesystem_source = "script"
        else:
            expected_names.add("files")
            filesystem_source = "expanded"
    else:
        filesystem_source = None
    if names != expected_names:
        raise HarnessError(
            f"{driver} golden has invalid entries: expected {sorted(expected_names)!r}, "
            f"found {sorted(names)!r}"
        )
    stdout = None
    stderr = None
    status_value = None
    filesystem_tree = None
    try:
        if "stdout" in comparisons:
            stdout = (golden_path / "stdout").read_text(encoding="utf-8")
        if "stderr" in comparisons:
            stderr = (golden_path / "stderr").read_text(encoding="utf-8")
        if "status" in comparisons:
            raw_status = (golden_path / "status").read_text(encoding="ascii")
            if not re.fullmatch(r"-?[0-9]+\n", raw_status):
                raise HarnessError("golden/status must be one decimal integer plus LF")
            status_value = int(raw_status)
        if "filesystem" in comparisons:
            filesystem_tree = snapshot_source(
                golden_path / ("expected.zip" if filesystem_source == "archive" else "files"),
                temporary_root=temporary_root,
            )
        elif "filesystem-script" in comparisons:
            archive = golden_path / "expected.zip"
            archive_stat = archive.lstat()
            if not stat.S_ISREG(archive_stat.st_mode):
                raise HarnessError(
                    f"semantic filesystem golden is not a regular ZIP archive: {archive}"
                )
    except HarnessError:
        raise
    except (OSError, UnicodeDecodeError, TreeError) as error:
        raise HarnessError(f"could not read {driver} golden {golden_path}: {error}") from error
    archive_sha256 = None
    if "filesystem-script" in comparisons:
        try:
            archive_sha256 = hashlib.sha256(
                (golden_path / "expected.zip").read_bytes()
            ).hexdigest()
        except OSError as error:
            raise HarnessError(
                f"could not hash semantic filesystem golden {golden_path}: {error}"
            ) from error
    return GoldenExpectation(
        comparisons,
        stdout=stdout,
        stderr=stderr,
        status=status_value,
        filesystem=filesystem_tree,
        filesystem_source=filesystem_source,
        filesystem_archive_sha256=archive_sha256,
    )


def _load_case(
    root: Path,
    path: Path,
    *,
    allow_missing_goldens: bool,
    temporary_root: Path | None,
) -> CaseDefinition:
    relative = path.relative_to(root)
    if len(relative.parts) != 3:
        raise HarnessError(f"test case must have component/topic/case depth: {relative}")
    component, topic, directory_name = relative.parts
    if SEGMENT_PATTERN.fullmatch(component) is None or SEGMENT_PATTERN.fullmatch(topic) is None:
        raise HarnessError(f"invalid component/topic path: {relative.parent}")
    match = CASE_DIRECTORY_PATTERN.fullmatch(directory_name)
    if match is None:
        raise HarnessError(f"malformed test case directory: {relative}")
    driver = match.group("driver")
    expectation = match.group("expectation")
    if driver not in DRIVERS:
        raise HarnessError(f"unknown test driver {driver!r}: {relative}")
    if expectation not in VALID_EXPECTATIONS:
        raise HarnessError(f"invalid expectation {expectation!r}: {relative}")
    case_id = "::".join((component, topic, match.group("case")))
    entries = _strict_directory_entries(
        path,
        frozenset({"golden", "spec.json"}),
        frozenset({"compare.py", "files"}),
    )
    if not entries["spec.json"].is_file():
        raise HarnessError(f"spec.json is not a regular file: {entries['spec.json']}")
    if not entries["golden"].is_dir():
        raise HarnessError(f"golden/ must be a real directory: {path}")
    files_path = entries.get("files")
    if files_path is None:
        input_tree = ()
    else:
        if not files_path.is_dir():
            raise HarnessError(f"files/ must be a real directory: {path}")
        try:
            input_tree = _load_input_tree(files_path, temporary_root=temporary_root)
        except HarnessError as error:
            raise HarnessError(f"invalid files/ for {case_id}: {error}") from error
    (
        selector,
        scope,
        labels,
        prerequisites,
        udir_spec,
        exec_spec,
        cargo_spec,
    ) = _load_case_spec(entries["spec.json"], driver, input_tree)
    comparator = entries.get("compare.py")
    needs_comparator = (
        exec_spec is not None and "filesystem-script" in exec_spec.comparisons
    )
    if needs_comparator != (comparator is not None):
        requirement = "requires" if needs_comparator else "does not permit"
        raise HarnessError(
            f"case {case_id} {requirement} compare.py for its selected comparison mode"
        )
    comparator_sha256 = None
    if comparator is not None:
        if not comparator.is_file():
            raise HarnessError(f"compare.py is not a regular file: {comparator}")
        try:
            comparator_bytes = comparator.read_bytes()
        except OSError as error:
            raise HarnessError(f"could not read compare.py for {case_id}: {error}") from error
        if not comparator_bytes or len(comparator_bytes) > 1024 * 1024:
            raise HarnessError("compare.py must contain between 1 byte and 1 MiB")
        comparator_sha256 = hashlib.sha256(comparator_bytes).hexdigest()
    golden = _load_golden(
        entries["golden"],
        driver,
        exec_spec,
        allow_missing=allow_missing_goldens,
        temporary_root=temporary_root,
    )
    return CaseDefinition(
        case_id,
        path,
        selector,
        driver,
        expectation,
        scope,
        labels,
        prerequisites,
        input_tree,
        udir_spec,
        exec_spec,
        cargo_spec,
        golden,
        comparator_sha256,
    )


def scan_case_tree(
    root: Path,
    *,
    allow_missing_goldens: bool = False,
    temporary_root: Path | None = None,
) -> dict[str, CaseDefinition]:
    if root.is_symlink() or not root.is_dir():
        raise HarnessError(f"test root is not a directory: {root}")
    cases: dict[str, CaseDefinition] = {}
    try:
        with os.scandir(root) as scanned:
            components = sorted(scanned, key=lambda entry: os.fsencode(entry.name))
        for component in components:
            if component.is_symlink():
                raise HarnessError(f"symlinks are not allowed in the test tree: {component.path}")
            if component.is_file(follow_symlinks=False):
                if component.name in TOP_LEVEL_TEST_DOCUMENTS:
                    continue
                raise HarnessError(f"unrecognized file in test root: {component.name}")
            if not component.is_dir(follow_symlinks=False):
                raise HarnessError(f"unsupported test-root entry: {component.name}")
            if SEGMENT_PATTERN.fullmatch(component.name) is None:
                raise HarnessError(f"invalid component directory: {component.name}")
            with os.scandir(component.path) as topics_scanned:
                topics = sorted(topics_scanned, key=lambda entry: os.fsencode(entry.name))
            for topic in topics:
                if topic.is_symlink() or not topic.is_dir(follow_symlinks=False):
                    raise HarnessError(f"invalid topic entry: {topic.path}")
                if SEGMENT_PATTERN.fullmatch(topic.name) is None:
                    raise HarnessError(f"invalid topic directory: {topic.path}")
                with os.scandir(topic.path) as cases_scanned:
                    case_entries = sorted(cases_scanned, key=lambda entry: os.fsencode(entry.name))
                if not case_entries:
                    raise HarnessError(f"empty topic directory: {topic.path}")
                for entry in case_entries:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        raise HarnessError(f"test case must be a real directory: {entry.path}")
                    case = _load_case(
                        root,
                        Path(entry.path),
                        allow_missing_goldens=allow_missing_goldens,
                        temporary_root=temporary_root,
                    )
                    if case.case_id in cases:
                        raise HarnessError(f"duplicate case ID: {case.case_id}")
                    cases[case.case_id] = case
    except HarnessError:
        raise
    except OSError as error:
        raise HarnessError(f"could not inspect test tree {root}: {error}") from error
    if not cases:
        raise HarnessError("test tree contains no cases")
    return dict(sorted(cases.items()))


def list_registered_tests(
    binary: Path | PinnedBinary, *, temporary_root: Path | None = None
) -> list[RegisteredTest]:
    with _using_pinned_binary(binary) as pinned:
        return _list_registered_tests(pinned, temporary_root=temporary_root)


def _list_registered_tests(
    binary: PinnedBinary, *, temporary_root: Path | None
) -> list[RegisteredTest]:
    try:
        completed, descendant_error = _run_quiescent_process(
            [binary.executable, "--list-tests"],
            pass_fds=(binary.descriptor,),
            temporary_root=temporary_root,
        )
    except HarnessError as error:
        raise HarnessError(f"could not execute unit-test binary {binary}: {error}") from error
    if descendant_error is not None:
        raise HarnessError(f"unit-test discovery {descendant_error}")
    if completed.returncode != 0:
        raise HarnessError(
            f"unit-test discovery failed with exit {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', errors='backslashreplace')}"
        )
    if completed.stderr:
        raise HarnessError(
            "unit-test discovery wrote to stderr: "
            + completed.stderr.decode("utf-8", errors="backslashreplace")
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise HarnessError(f"unit-test discovery repeated JSON field: {name!r}")
            result[name] = value
        return result

    try:
        manifest = json.loads(
            completed.stdout.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except HarnessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("unit-test discovery returned malformed JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != {"protocol", "tests"}:
        raise HarnessError("unit-test discovery returned an invalid manifest shape")
    if type(manifest["protocol"]) is not int or manifest["protocol"] != DISCOVERY_PROTOCOL_VERSION:
        raise HarnessError(f"unsupported unit-test protocol: {manifest['protocol']!r}")
    raw_tests = manifest["tests"]
    if not isinstance(raw_tests, list):
        raise HarnessError("unit-test manifest tests must be an array")
    tests: list[RegisteredTest] = []
    for raw_test in raw_tests:
        if not isinstance(raw_test, dict) or set(raw_test) != {"id", "driver"}:
            raise HarnessError("unit-test manifest entry has an invalid shape")
        test_id = raw_test["id"]
        driver = raw_test["driver"]
        if not isinstance(test_id, str) or not is_valid_test_id(test_id):
            raise HarnessError(f"unit-test manifest contains invalid ID: {test_id!r}")
        if not isinstance(driver, str) or driver not in UNIT_DRIVERS:
            raise HarnessError(f"unit-test manifest contains invalid driver: {driver!r}")
        tests.append(RegisteredTest(test_id, driver))
    if tests != sorted(tests, key=lambda test: test.test_id):
        raise HarnessError("unit-test manifest is not sorted")
    ids = [test.test_id for test in tests]
    if len(ids) != len(set(ids)):
        raise HarnessError("unit-test manifest contains duplicate IDs")
    return tests


def check_inventory(
    cases: dict[str, CaseDefinition],
    registered: Sequence[RegisteredTest],
) -> None:
    registered_by_id = {test.test_id: test for test in registered}
    cases_by_selector: dict[str, list[CaseDefinition]] = {}
    for case in cases.values():
        if case.driver not in UNIT_DRIVERS:
            continue
        cases_by_selector.setdefault(case.selector, []).append(case)
    details = []
    missing = sorted(set(registered_by_id) - set(cases_by_selector))
    stale = sorted(set(cases_by_selector) - set(registered_by_id))
    if missing:
        details.append("registered tests without cases: " + ", ".join(missing))
    if stale:
        details.append("cases with stale selectors: " + ", ".join(stale))
    mismatches = sorted(
        case.case_id
        for case in cases.values()
        if case.selector in registered_by_id
        and case.driver != registered_by_id[case.selector].driver
    )
    if mismatches:
        details.append("driver mismatches: " + ", ".join(mismatches))
    duplicate_urun = sorted(
        selector
        for selector, selected in cases_by_selector.items()
        if selector in registered_by_id
        and registered_by_id[selector].driver == "urun"
        and len(selected) != 1
    )
    if duplicate_urun:
        details.append("urun selectors must have exactly one case: " + ", ".join(duplicate_urun))
    if details:
        raise HarnessError("; ".join(details))


def _select_cases(
    cases: dict[str, CaseDefinition],
    selection: Selection,
    *,
    update_golden: bool,
) -> list[str]:
    if selection.explicit_all and selection.has_filter:
        raise HarnessError("--all cannot be combined with case filters")
    if selection.explicit_all and not update_golden:
        raise HarnessError("--all is only needed with --update-golden")
    if update_golden and not selection.explicit_all and not selection.has_filter:
        raise HarnessError("--update-golden requires a selector or explicit --all")
    unknown = sorted(set(selection.cases) - set(cases))
    if unknown:
        raise HarnessError("unknown selected cases: " + ", ".join(unknown))
    for segment in selection.segments:
        if SEGMENT_PATTERN.fullmatch(segment) is None:
            raise HarnessError(f"invalid selected segment: {segment!r}")
    if selection.profile not in PROFILE_LABELS:
        raise HarnessError(f"unknown profile: {selection.profile!r}")
    if selection.scope not in SCOPE_RANK:
        raise HarnessError(f"unknown scope: {selection.scope!r}")
    for tag in selection.tags:
        if tag not in DRIVERS:
            raise HarnessError(f"unknown selected tag: {tag!r}")
    allowed_labels = PROFILE_LABELS[selection.profile]
    selected = []
    for case_id, case in cases.items():
        segments = case_id.split("::")
        slash_id = "/".join(segments)
        if selection.cases and case_id not in selection.cases:
            continue
        if selection.segments and not all(segment in segments for segment in selection.segments):
            continue
        if selection.tags and case.driver not in selection.tags:
            continue
        if (
            case.scope is not None
            and SCOPE_RANK[case.scope] > SCOPE_RANK[selection.scope]
        ):
            continue
        if not set(case.labels) <= allowed_labels:
            continue
        if selection.globs and not any(
            fnmatch.fnmatchcase(slash_id, pattern) for pattern in selection.globs
        ):
            continue
        selected.append(case_id)
    if not selected:
        raise HarnessError("test selection matched no cases")
    return selected


def invoke_unit_test(
    binary: Path | PinnedBinary,
    case: CaseDefinition,
    *,
    temporary_root: Path | None = None,
) -> DriverResult:
    with _using_pinned_binary(binary) as pinned:
        return _invoke_unit_test(pinned, case, temporary_root=temporary_root)


def _invoke_unit_test(
    binary: PinnedBinary, case: CaseDefinition, *, temporary_root: Path | None
) -> DriverResult:
    try:
        with tempfile.TemporaryDirectory(prefix="u-", dir=temporary_root) as temporary_name:
            fixture_root = Path(temporary_name)
            arguments = [binary.executable, "--run-test", case.selector]
            filesystem = None
            working_directory = fixture_root
            if case.driver == "udir":
                if case.udir_spec is None:
                    return DriverResult(
                        "infrastructure", "", "", "udir case lacks validated configuration"
                    )
                try:
                    materialize_snapshot(case.input_tree, fixture_root)
                except TreeError as error:
                    return DriverResult(
                        "infrastructure", "", "", f"could not stage udir fixture: {error}"
                    )
                working_directory = fixture_root / case.udir_spec.cwd
                for name, value in case.udir_spec.args:
                    arguments.extend(("--arg", name, value))
            elif case.udir_spec is not None or case.input_tree:
                return DriverResult(
                    "infrastructure", "", "", "urun case contains directory-driver state"
                )
            try:
                completed, descendant_error = _run_quiescent_process(
                    arguments,
                    cwd=working_directory,
                    pass_fds=(binary.descriptor,),
                    temporary_root=temporary_root,
                )
            except HarnessError as error:
                return DriverResult("infrastructure", "", "", f"could not start driver: {error}")
            if descendant_error is not None:
                return DriverResult(
                    "infrastructure",
                    completed.stdout.decode("utf-8", errors="backslashreplace"),
                    completed.stderr.decode("utf-8", errors="backslashreplace"),
                    descendant_error,
                )
            if case.driver == "udir":
                try:
                    filesystem = snapshot_source(
                        fixture_root, temporary_root=temporary_root
                    )
                except TreeError as error:
                    return DriverResult(
                        "infrastructure",
                        completed.stdout.decode("utf-8", errors="backslashreplace"),
                        completed.stderr.decode("utf-8", errors="backslashreplace"),
                        f"could not capture final udir filesystem: {error}",
                    )
    except OSError as error:
        return DriverResult("infrastructure", "", "", f"could not manage fixture: {error}")
    try:
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
    except UnicodeDecodeError:
        return DriverResult(
            "infrastructure",
            completed.stdout.decode("utf-8", errors="backslashreplace"),
            completed.stderr.decode("utf-8", errors="backslashreplace"),
            "driver output is not UTF-8",
            filesystem,
        )
    if completed.returncode not in (0, 1):
        message = f"driver exited with infrastructure status {completed.returncode}"
        if stderr:
            message += ": " + stderr.rstrip("\n")
        return DriverResult("infrastructure", stdout, stderr, message, filesystem)
    if stderr:
        return DriverResult(
            "infrastructure",
            stdout,
            stderr,
            "controlled test execution wrote to stderr: " + stderr.rstrip("\n"),
            filesystem,
        )
    if not stdout.endswith("\n"):
        return DriverResult("infrastructure", stdout, stderr, "missing final result line", filesystem)
    protocol_lines = [line for line in stdout.splitlines() if line.startswith("VIEWSPACE-TEST-RESULT ")]
    if len(protocol_lines) != 1 or protocol_lines[0] != stdout.splitlines()[-1]:
        return DriverResult(
            "infrastructure", stdout, stderr, "result record must occur exactly once and be last", filesystem
        )
    match = RESULT_PATTERN.fullmatch(protocol_lines[0])
    if match is None:
        return DriverResult("infrastructure", stdout, stderr, "malformed final result record", filesystem)
    if match.group("id") != case.selector:
        return DriverResult("infrastructure", stdout, stderr, "result selector mismatch", filesystem)
    outcome = "passed" if match.group("outcome") == "PASS" else "failed"
    if completed.returncode != (0 if outcome == "passed" else 1):
        return DriverResult(
            "infrastructure", stdout, stderr, "result record disagrees with exit code", filesystem
        )
    return DriverResult(
        outcome, stdout, stderr, filesystem=filesystem, status=completed.returncode
    )


def _missing_prerequisites(
    case: CaseDefinition, *, temporary_root: Path | None = None
) -> tuple[str, ...]:
    missing = []
    for prerequisite in case.prerequisites:
        if prerequisite == "linux" and sys.platform != "linux":
            missing.append(prerequisite)
        elif prerequisite == "root" and os.geteuid() != 0:
            missing.append(prerequisite)
        elif prerequisite == "scratch-ext4" and (
            temporary_root is None or _mount_filesystem_type(temporary_root) != "ext4"
        ):
            missing.append(prerequisite)
        elif prerequisite == "unix-seqpacket" and not _unix_seqpacket_available(
            temporary_root=temporary_root
        ):
            missing.append(prerequisite)
        elif prerequisite.startswith("command:") and shutil.which(prerequisite[8:]) is None:
            missing.append(prerequisite)
        elif prerequisite.startswith("env:") and prerequisite[4:] not in os.environ:
            missing.append(prerequisite)
        elif prerequisite.startswith("python-module:") and not _python_module_available(
            prerequisite[14:]
        ):
            missing.append(prerequisite)
    return tuple(missing)


def _unix_seqpacket_available(*, temporary_root: Path | None = None) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="s-", dir=temporary_root) as temporary:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.bind(os.fspath(Path(temporary) / "probe.sock"))
        return True
    except OSError:
        return False


def _mount_filesystem_type(path: Path) -> str | None:
    """Return Linux mountinfo's filesystem type for the deepest mount containing path."""

    def decode_mount_field(value: str) -> str:
        for encoded, decoded in (
            ("\\040", " "),
            ("\\011", "\t"),
            ("\\012", "\n"),
            ("\\134", "\\"),
        ):
            value = value.replace(encoded, decoded)
        return value

    try:
        resolved = path.resolve(strict=True)
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    selected: tuple[int, str] | None = None
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mountpoint = Path(decode_mount_field(fields[4]))
            filesystem = fields[separator + 1]
            common = os.path.commonpath((resolved, mountpoint))
        except (ValueError, IndexError):
            continue
        if common != os.fspath(mountpoint):
            continue
        depth = len(mountpoint.parts)
        if selected is None or depth > selected[0]:
            selected = (depth, filesystem)
    return None if selected is None else selected[1]


def _python_module_available(name: str) -> bool:
    command, separator, module = name.partition(":")
    if separator:
        executable = shutil.which(command)
        if executable is None or not module:
            return False
    else:
        executable = sys.executable
        module = name
    success_token = os.urandom(24).hex()
    probe = (
        "import contextlib,importlib,os,sys;"
        "sink=open(os.devnull,'w');"
        "stdout=contextlib.redirect_stdout(sink);"
        "stderr=contextlib.redirect_stderr(sink);"
        "stdout.__enter__();stderr.__enter__();"
        "importlib.import_module(sys.argv[1]);"
        "stderr.__exit__(None,None,None);stdout.__exit__(None,None,None);"
        f"sink.close();sys.stdout.write({success_token!r})"
    )
    try:
        completed = subprocess.run(
            [executable, "-I", "-B", "-c", probe, module],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout == success_token.encode("ascii")


def _expand_exec_value(value: str, repository: Path, workspace: Path) -> str:
    return value.replace("{repository}", os.fspath(repository)).replace(
        "{workspace}", os.fspath(workspace)
    )


def _exec_environment(
    spec: ExecSpec, repository: Path, workspace: Path
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in spec.environment_inherit
        if name in os.environ
    }
    environment.update(
        (name, _expand_exec_value(value, repository, workspace))
        for name, value in spec.environment_set
    )
    return environment


def invoke_exec(
    repository: Path,
    case: CaseDefinition,
    *,
    temporary_root: Path | None = None,
    update_golden: bool = False,
) -> DriverResult:
    spec = case.exec_spec
    if spec is None:
        return DriverResult("infrastructure", "", "", "exec case lacks validated configuration")
    completed: subprocess.CompletedProcess[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="e-", dir=temporary_root) as temporary_name:
            workspace = Path(temporary_name)
            materialize_snapshot(case.input_tree, workspace)
            anchor = repository if spec.cwd_anchor == "repository" else workspace
            cwd = anchor if spec.cwd == "." else anchor / spec.cwd
            arguments = [
                _expand_exec_value(value, repository, workspace) for value in spec.argv
            ]
            completed, descendant_error = _run_quiescent_process(
                arguments,
                cwd=cwd,
                environment=_exec_environment(spec, repository, workspace),
                timeout_seconds=spec.timeout_seconds,
                temporary_root=temporary_root,
            )
            if descendant_error is not None:
                return DriverResult(
                    "infrastructure",
                    completed.stdout.decode("utf-8", errors="backslashreplace"),
                    completed.stderr.decode("utf-8", errors="backslashreplace"),
                    descendant_error,
                    status=completed.returncode,
                )
            filesystem = None
            comparison_outcome = None
            comparison_stdout = ""
            comparison_stderr = ""
            if spec.filesystem is not None:
                target = workspace if spec.filesystem == "." else workspace / spec.filesystem
                if "filesystem" in spec.comparisons or update_golden:
                    filesystem = snapshot_source(target, temporary_root=temporary_root)
                elif (
                    "filesystem-script" in spec.comparisons
                    and completed.returncode >= 0
                ):
                    try:
                        target_stat = target.lstat()
                    except OSError as error:
                        raise HarnessError(
                            f"semantic comparison target is unavailable: {error}"
                        ) from error
                    if not stat.S_ISDIR(target_stat.st_mode):
                        raise HarnessError(
                            "semantic comparison target must be a real directory"
                        )
                    comparison, comparison_descendant_error = _run_quiescent_process(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            os.fspath(case.comparator_path),
                            os.fspath(case.golden_path / "expected.zip"),
                            os.fspath(target),
                        ],
                        cwd=workspace,
                        environment=_exec_environment(spec, repository, workspace),
                        timeout_seconds=spec.timeout_seconds,
                        temporary_root=temporary_root,
                    )
                    comparison_stdout = comparison.stdout.decode("utf-8")
                    comparison_stderr = comparison.stderr.decode("utf-8")
                    if comparison_descendant_error is not None:
                        return DriverResult(
                            "infrastructure",
                            completed.stdout.decode("utf-8", errors="backslashreplace"),
                            completed.stderr.decode("utf-8", errors="backslashreplace"),
                            "semantic comparator " + comparison_descendant_error,
                            status=completed.returncode,
                            comparison_stdout=comparison_stdout,
                            comparison_stderr=comparison_stderr,
                        )
                    if comparison.returncode not in (0, 1):
                        return DriverResult(
                            "infrastructure",
                            completed.stdout.decode("utf-8", errors="backslashreplace"),
                            completed.stderr.decode("utf-8", errors="backslashreplace"),
                            "semantic comparator exited with unsupported status "
                            f"{comparison.returncode}",
                            status=completed.returncode,
                            comparison_stdout=comparison_stdout,
                            comparison_stderr=comparison_stderr,
                        )
                    comparison_outcome = (
                        "matched" if comparison.returncode == 0 else "mismatched"
                    )
    except (HarnessError, OSError, TreeError, UnicodeDecodeError) as error:
        return DriverResult(
            "infrastructure",
            ""
            if completed is None
            else completed.stdout.decode("utf-8", errors="backslashreplace"),
            ""
            if completed is None
            else completed.stderr.decode("utf-8", errors="backslashreplace"),
            f"could not execute case: {error}",
            status=None if completed is None else completed.returncode,
        )
    try:
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
    except UnicodeDecodeError:
        return DriverResult(
            "infrastructure",
            completed.stdout.decode("utf-8", errors="backslashreplace"),
            completed.stderr.decode("utf-8", errors="backslashreplace"),
            "exec output is not UTF-8",
            filesystem,
            completed.returncode,
            comparison_outcome,
            comparison_stdout,
            comparison_stderr,
        )
    if completed.returncode < 0:
        return DriverResult(
            "infrastructure",
            stdout,
            stderr,
            f"exec process terminated by signal {-completed.returncode}",
            filesystem,
            completed.returncode,
            comparison_outcome,
            comparison_stdout,
            comparison_stderr,
        )
    return DriverResult(
        "passed" if completed.returncode == 0 else "failed",
        stdout,
        stderr,
        filesystem=filesystem,
        status=completed.returncode,
        comparison_outcome=comparison_outcome,
        comparison_stdout=comparison_stdout,
        comparison_stderr=comparison_stderr,
    )


@dataclasses.dataclass(frozen=True)
class CargoExecutable:
    path: Path
    tests: frozenset[str]
    ignored_tests: frozenset[str]


def prepare_cargo(
    repository: Path, *, temporary_root: Path | None = None
) -> dict[tuple[str, str, str], CargoExecutable]:
    metadata_result, metadata_descendants = _run_quiescent_process(
        ["cargo", "metadata", "--no-deps", "--format-version=1", "--locked"],
        cwd=repository,
        timeout_seconds=60,
        temporary_root=temporary_root,
    )
    if metadata_descendants is not None or metadata_result.returncode != 0:
        raise HarnessError("Cargo metadata discovery failed")
    try:
        metadata = json.loads(metadata_result.stdout.decode("utf-8"))
        package_names = {
            os.fspath(Path(package["manifest_path"]).resolve()): package["name"]
            for package in metadata["packages"]
        }
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise HarnessError("Cargo metadata discovery returned malformed JSON") from error
    command = [
        "cargo",
        "test",
        "--workspace",
        "--all-features",
        "--locked",
        "--no-run",
        "--message-format=json-render-diagnostics",
    ]
    completed, descendant_error = _run_quiescent_process(
        command,
        cwd=repository,
        timeout_seconds=900,
        temporary_root=temporary_root,
    )
    if descendant_error is not None:
        raise HarnessError(f"Cargo build {descendant_error}")
    if completed.returncode != 0:
        raise HarnessError(
            "Cargo test build failed: "
            + completed.stderr.decode("utf-8", errors="backslashreplace").strip()
        )
    candidates: dict[tuple[str, str, str], Path] = {}
    for raw_line in completed.stdout.splitlines():
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if message.get("reason") != "compiler-artifact" or message.get("executable") is None:
            continue
        manifest_path = message.get("manifest_path")
        package = package_names.get(
            os.fspath(Path(manifest_path).resolve()) if isinstance(manifest_path, str) else ""
        )
        target = message.get("target")
        if package is None or not isinstance(target, dict):
            continue
        kinds = target.get("kind")
        name = target.get("name")
        executable = message.get("executable")
        if not isinstance(kinds, list) or not isinstance(name, str) or not isinstance(executable, str):
            continue
        for kind in ("lib", "bin", "test"):
            if kind in kinds:
                candidates[(package, kind, name)] = Path(executable)

    prepared: dict[tuple[str, str, str], CargoExecutable] = {}
    for identity, executable in candidates.items():
        listed, descendant_error = _run_quiescent_process(
            [os.fspath(executable), "--list", "--format", "terse"],
            cwd=repository,
            timeout_seconds=60,
            temporary_root=temporary_root,
        )
        if descendant_error is not None or listed.returncode != 0:
            continue
        try:
            lines = listed.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        tests = frozenset(
            line.removesuffix(": test") for line in lines if line.endswith(": test")
        )
        ignored, ignored_descendant_error = _run_quiescent_process(
            [os.fspath(executable), "--list", "--format", "terse", "--ignored"],
            cwd=repository,
            timeout_seconds=60,
            temporary_root=temporary_root,
        )
        if ignored_descendant_error is not None or ignored.returncode != 0:
            continue
        try:
            ignored_lines = ignored.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        ignored_tests = frozenset(
            line.removesuffix(": test")
            for line in ignored_lines
            if line.endswith(": test")
        )
        if tests:
            prepared[identity] = CargoExecutable(executable, tests, ignored_tests)
    return prepared


def invoke_cargo(
    repository: Path,
    case: CaseDefinition,
    executables: dict[tuple[str, str, str], CargoExecutable],
    *,
    temporary_root: Path | None = None,
) -> DriverResult:
    spec = case.cargo_spec
    if spec is None:
        return DriverResult("infrastructure", "", "", "cargo case lacks validated configuration")
    identity = (spec.package, spec.target_kind, spec.target_name)
    executable = executables.get(identity)
    if executable is None:
        return DriverResult("infrastructure", "", "", f"Cargo test target was not built: {identity}")
    if case.selector not in executable.tests:
        return DriverResult(
            "infrastructure", "", "", f"Cargo target does not contain exact test {case.selector!r}"
        )
    try:
        completed, descendant_error = _run_quiescent_process(
            [
                os.fspath(executable.path),
                "--exact",
                case.selector,
                "--nocapture",
                "--test-threads=1",
            ],
            cwd=repository,
            timeout_seconds=spec.timeout_seconds,
            temporary_root=temporary_root,
        )
    except HarnessError as error:
        return DriverResult("infrastructure", "", "", f"could not run Cargo test: {error}")
    stdout = completed.stdout.decode("utf-8", errors="backslashreplace")
    stderr = completed.stderr.decode("utf-8", errors="backslashreplace")
    if descendant_error is not None:
        return DriverResult(
            "infrastructure", stdout, stderr, descendant_error, status=completed.returncode
        )
    if completed.returncode not in (0, 101):
        return DriverResult(
            "infrastructure",
            stdout,
            stderr,
            f"Cargo test exited with infrastructure status {completed.returncode}",
            status=completed.returncode,
        )
    if "running 1 test" not in stdout:
        return DriverResult(
            "infrastructure",
            stdout,
            stderr,
            "Cargo exact selector did not execute exactly one test",
            status=completed.returncode,
        )
    return DriverResult(
        "passed" if completed.returncode == 0 else "failed",
        stdout,
        stderr,
        status=completed.returncode,
    )


def check_cargo_inventory(
    cases: dict[str, CaseDefinition],
    executables: dict[tuple[str, str, str], CargoExecutable],
) -> None:
    seen: dict[tuple[tuple[str, str, str], str], str] = {}
    errors = []
    for case in cases.values():
        spec = case.cargo_spec
        if spec is None:
            continue
        identity = (spec.package, spec.target_kind, spec.target_name)
        executable = executables.get(identity)
        if executable is None:
            errors.append(f"{case.case_id}: target was not built {identity!r}")
            continue
        if case.selector not in executable.tests:
            errors.append(f"{case.case_id}: target lacks selector {case.selector!r}")
        elif case.selector in executable.ignored_tests:
            errors.append(
                f"{case.case_id}: ignored Cargo selector must remain an explicit external test"
            )
        key = (identity, case.selector)
        if key in seen:
            errors.append(
                f"{case.case_id}: duplicates Cargo selector already owned by {seen[key]}"
            )
        else:
            seen[key] = case.case_id
    for identity, executable in sorted(executables.items()):
        represented = {
            selector
            for (represented_identity, selector), _case_id in seen.items()
            if represented_identity == identity
        }
        missing = sorted(executable.tests - executable.ignored_tests - represented)
        if missing:
            errors.append(
                f"{identity!r}: non-ignored Cargo tests without cases: " + ", ".join(missing)
            )
    if errors:
        raise HarnessError("invalid Cargo case inventory: " + "; ".join(errors))


def _golden_diff(expected: str, actual: str, golden_path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=os.fspath(golden_path),
            tofile="actual",
        )
    )


def _expected_driver_outcome(expectation: str) -> str:
    if expectation == "sr":
        return "passed"
    if expectation == "fr":
        return "failed"
    raise HarnessError(f"invalid case expectation: {expectation!r}")


def _case_result_common(
    case: CaseDefinition, driver: DriverResult, duration_ms: int
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "selector": case.selector,
        "driver": case.driver,
        "source": case.source_name,
        "expectation": case.expectation,
        "scope": case.scope,
        "labels": case.labels,
        "prerequisites": case.prerequisites,
        "duration_ms": duration_ms,
        "stdout": driver.stdout,
        "stderr": driver.stderr,
        "comparison_stdout": driver.comparison_stdout,
        "comparison_stderr": driver.comparison_stderr,
    }


def evaluate_case(
    case: CaseDefinition, driver: DriverResult, duration_ms: int
) -> CaseResult:
    common = _case_result_common(case, driver, duration_ms)
    if driver.outcome == "skipped":
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="skipped",
            message=driver.error,
        )
    if driver.outcome == "infrastructure":
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="infrastructure_failed",
            message=driver.error,
        )
    expected_outcome = _expected_driver_outcome(case.expectation)
    if driver.outcome != expected_outcome:
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="expectation_failed",
            message=f"expected controlled {expected_outcome}, got {driver.outcome}",
        )
    differences = []
    difference = ""
    golden = case.golden
    if "stdout" in golden.comparisons and golden.stdout is None:
        return CaseResult(
            **common,
            driver_outcome="infrastructure",
            status="infrastructure_failed",
            message="stdout golden was absent from the preflight case snapshot",
        )
    if "stderr" in golden.comparisons and golden.stderr is None:
        return CaseResult(
            **common,
            driver_outcome="infrastructure",
            status="infrastructure_failed",
            message="stderr golden was absent from the preflight case snapshot",
        )
    if "status" in golden.comparisons and golden.status is None:
        return CaseResult(
            **common,
            driver_outcome="infrastructure",
            status="infrastructure_failed",
            message="status golden was absent from the preflight case snapshot",
        )
    if "filesystem" in golden.comparisons and golden.filesystem is None:
        return CaseResult(
            **common,
            driver_outcome="infrastructure",
            status="infrastructure_failed",
            message="filesystem golden was absent from the preflight case snapshot",
        )
    if "filesystem-script" in golden.comparisons:
        if golden.filesystem_archive_sha256 is None:
            return CaseResult(
                **common,
                driver_outcome="infrastructure",
                status="infrastructure_failed",
                message="semantic filesystem golden was absent from the preflight case snapshot",
            )
        if driver.comparison_outcome is None:
            return CaseResult(
                **common,
                driver_outcome="infrastructure",
                status="infrastructure_failed",
                message="semantic filesystem comparator did not return an outcome",
            )
    if "stdout" in golden.comparisons and golden.stdout != driver.stdout:
        differences.append("stdout differs")
        difference += _golden_diff(
            golden.stdout or "", driver.stdout, case.golden_path / "stdout"
        )
    if "stderr" in golden.comparisons and golden.stderr != driver.stderr:
        differences.append("stderr differs")
        difference += _golden_diff(
            golden.stderr or "", driver.stderr, case.golden_path / "stderr"
        )
    if "status" in golden.comparisons and golden.status != driver.status:
        differences.append("status differs")
        difference += _golden_diff(
            f"{golden.status}\n", f"{driver.status}\n", case.golden_path / "status"
        )
    if "filesystem" in golden.comparisons:
        if driver.filesystem is None:
            return CaseResult(
                **common,
                driver_outcome="infrastructure",
                status="infrastructure_failed",
                message=f"{case.driver} execution did not return a filesystem snapshot",
            )
        if golden.filesystem != driver.filesystem:
            differences.append("filesystem differs")
            difference += diff_snapshots(golden.filesystem or (), driver.filesystem)
    if (
        "filesystem-script" in golden.comparisons
        and driver.comparison_outcome == "mismatched"
    ):
        differences.append("semantic filesystem comparison failed")
        difference += "--- semantic comparison\n+++ comparator diagnostics\n"
        if driver.comparison_stdout:
            difference += "stdout:\n" + driver.comparison_stdout
            if not difference.endswith("\n"):
                difference += "\n"
        if driver.comparison_stderr:
            difference += "stderr:\n" + driver.comparison_stderr
            if not difference.endswith("\n"):
                difference += "\n"
        if not driver.comparison_stdout and not driver.comparison_stderr:
            difference += "comparator reported a mismatch without details\n"
    if differences:
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="expectation_failed",
            message=" and ".join(differences),
            diff=difference,
        )
    return CaseResult(**common, driver_outcome=driver.outcome, status="passed")


def _udir_archive_snapshot(stdout: str, filesystem: TreeSnapshot) -> TreeSnapshot:
    entries = [
        TreeEntry(b"stdout", "file", stdout.encode("utf-8")),
        TreeEntry(b"files", "directory"),
    ]
    entries.extend(
        dataclasses.replace(entry, path=b"files/" + entry.path) for entry in filesystem
    )
    return tuple(entries)


def _update_golden(
    case: CaseDefinition, driver: DriverResult, duration_ms: int
) -> CaseResult:
    common = _case_result_common(case, driver, duration_ms)
    if driver.outcome == "skipped":
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="skipped",
            message="golden preserved: " + (driver.error or "prerequisite unavailable"),
        )
    if driver.outcome == "infrastructure":
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="infrastructure_failed",
            message=driver.error,
        )
    expected_outcome = _expected_driver_outcome(case.expectation)
    if driver.outcome != expected_outcome:
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="expectation_failed",
            message=f"golden preserved: expected controlled {expected_outcome}, got {driver.outcome}",
        )
    if (
        case.golden.comparisons & {"filesystem", "filesystem-script"}
        and driver.filesystem is None
    ):
        return CaseResult(
            **common,
            driver_outcome="infrastructure",
            status="infrastructure_failed",
            message=f"golden preserved: {case.driver} execution returned no filesystem snapshot",
        )
    golden = case.golden
    encoded_filesystem = (
        None if driver.filesystem is None else snapshot_zip_bytes(driver.filesystem)
    )
    unchanged = (
        ("stdout" not in golden.comparisons or golden.stdout == driver.stdout)
        and ("stderr" not in golden.comparisons or golden.stderr == driver.stderr)
        and ("status" not in golden.comparisons or golden.status == driver.status)
        and (
            "filesystem" not in golden.comparisons
            or (
                golden.filesystem_source == "archive"
                and golden.filesystem == driver.filesystem
            )
        )
        and (
            "filesystem-script" not in golden.comparisons
            or (
                encoded_filesystem is not None
                and golden.filesystem_archive_sha256
                == hashlib.sha256(encoded_filesystem).hexdigest()
            )
        )
    )
    if unchanged:
        return CaseResult(
            **common,
            driver_outcome=driver.outcome,
            status="passed",
            message=f"unchanged {case.source_name}/golden",
        )

    def populate(directory: Path) -> None:
        if case.driver == "udir":
            filesystem = driver.filesystem
            if filesystem is None:
                raise AssertionError("filesystem checked before golden population")
            snapshot = _udir_archive_snapshot(driver.stdout, filesystem)
            _atomic_write(directory / "expected.zip", snapshot_zip_bytes(snapshot))
            return
        if "stdout" in golden.comparisons:
            _atomic_write(directory / "stdout", driver.stdout.encode("utf-8"))
        if "stderr" in golden.comparisons:
            _atomic_write(directory / "stderr", driver.stderr.encode("utf-8"))
        if "status" in golden.comparisons:
            _atomic_write(directory / "status", f"{driver.status}\n".encode("ascii"))
        if golden.comparisons & {"filesystem", "filesystem-script"}:
            filesystem = driver.filesystem
            if filesystem is None:
                raise AssertionError("filesystem checked before golden population")
            _atomic_write(
                directory / "expected.zip",
                encoded_filesystem or snapshot_zip_bytes(filesystem),
            )

    try:
        atomic_replace_directory(case.golden_path, populate)
    except TreeError as error:
        raise HarnessError(str(error)) from error
    return CaseResult(
        **common,
        driver_outcome=driver.outcome,
        status="passed",
        message=f"UPDATED {case.source_name}/golden",
        golden_updated=True,
    )


def _report_for(
    results: Sequence[CaseResult],
    global_errors: Sequence[str] = (),
    binary_sha256: str | None = None,
    scratch_root: Path | str | None = None,
) -> dict[str, Any]:
    counts = {
        "selected": len(results),
        "passed": sum(result.status == "passed" for result in results),
        "skipped": sum(result.status == "skipped" for result in results),
        "expectation_failed": sum(result.status == "expectation_failed" for result in results),
        "infrastructure_failed": sum(
            result.status == "infrastructure_failed" for result in results
        ) + len(global_errors),
        "goldens_updated": sum(result.golden_updated for result in results),
        "unscoped": sum(result.scope is None for result in results),
    }
    cases = []
    for result in results:
        encoded = dataclasses.asdict(result)
        encoded["labels"] = list(encoded["labels"])
        encoded["prerequisites"] = list(encoded["prerequisites"])
        cases.append(encoded)
    return {
        "protocol": REPORT_PROTOCOL_VERSION,
        "binary_sha256": binary_sha256,
        "scratch_root": None if scratch_root is None else os.fspath(scratch_root),
        "cases": cases,
        "global_errors": list(global_errors),
        "summary": counts,
    }


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_json_report(path: Path, report: dict[str, Any]) -> None:
    serialized = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _atomic_write(path, serialized)
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessError(f"could not write and validate report {path}: {error}") from error
    if loaded != report:
        raise HarnessError(f"serialized report did not round-trip: {path}")


def _html_report(report: dict[str, Any]) -> str:
    def escaped(value: object) -> str:
        return html.escape(str(value), quote=True)

    sections = []
    for case in report["cases"]:
        css_class = (
            "pass" if case["status"] == "passed" else "skip" if case["status"] == "skipped" else "fail"
        )
        message = (
            ""
            if case["message"] is None
            else f"<h3>Message</h3><pre>{escaped(case['message'])}</pre>"
        )
        difference = (
            ""
            if case["diff"] is None
            else f"<h3>Comparison</h3><pre>{escaped(case['diff'])}</pre>"
        )
        comparator_output = ""
        if case["comparison_stdout"] or case["comparison_stderr"]:
            comparator_output = (
                f"<h3>comparator stdout</h3><pre>{escaped(case['comparison_stdout'])}</pre>"
                f"<h3>comparator stderr</h3><pre>{escaped(case['comparison_stderr'])}</pre>"
            )
        sections.append(
            f"<section class=\"case {css_class}\">"
            f"<h2>{escaped(case['case_id'])}: {escaped(case['status'])}</h2><dl>"
            f"<dt>Driver</dt><dd>{escaped(case['driver'])}</dd>"
            f"<dt>Selector</dt><dd>{escaped(case['selector'])}</dd>"
            f"<dt>Source</dt><dd>{escaped(case['source'])}</dd>"
            f"<dt>Expected</dt><dd>{escaped(case['expectation'])}</dd>"
            f"<dt>Scope</dt><dd>{escaped(case['scope'] or 'unscoped (all)')}</dd>"
            f"<dt>Labels</dt><dd>{escaped(', '.join(case['labels']) or 'none')}</dd>"
            f"<dt>Prerequisites</dt><dd>{escaped(', '.join(case['prerequisites']) or 'none')}</dd>"
            f"<dt>Observed</dt><dd>{escaped(case['driver_outcome'])}</dd>"
            f"<dt>Duration</dt><dd>{escaped(case['duration_ms'])} ms</dd></dl>"
            f"{message}<h3>stdout</h3><pre>{escaped(case['stdout'])}</pre>"
            f"<h3>stderr</h3><pre>{escaped(case['stderr'])}</pre>"
            f"{comparator_output}{difference}</section>"
        )
    global_errors = "".join(
        f"<li>{escaped(error)}</li>" for error in report["global_errors"]
    )
    summary = report["summary"]
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>VIEWSPACE test report</title><style>"
        "body{font-family:system-ui,sans-serif;margin:2rem;background:#f5f5f5;color:#222}"
        "header,.case{background:white;border:1px solid #bbb;padding:1rem;margin:1rem 0}"
        ".case.pass{border-left:8px solid #198754}.case.skip{border-left:8px solid #d39e00}"
        ".case.fail{border-left:8px solid #dc3545}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eee;padding:.75rem}"
        "dt{font-weight:700;float:left;clear:left;width:8rem}dd{margin-left:9rem}"
        "</style></head><body><header><h1>VIEWSPACE test report</h1>"
        f"<p>Scratch root: <code>{escaped(report['scratch_root'])}</code></p>"
        f"<p>{summary['passed']} passed; {summary['skipped']} skipped; "
        f"{summary['expectation_failed']} failed; "
        f"{summary['infrastructure_failed']} infrastructure errors; "
        f"{summary['goldens_updated']} goldens updated; "
        f"{summary['unscoped']} unscoped compatibility cases.</p>"
        f"<ul>{global_errors}</ul></header>{''.join(sections)}</body></html>\n"
    )


def write_html_report(path: Path, report: dict[str, Any]) -> None:
    try:
        _atomic_write(path, _html_report(report).encode("utf-8"))
    except OSError as error:
        raise HarnessError(f"could not write HTML report {path}: {error}") from error


def write_reports(json_path: Path, html_path: Path, report: dict[str, Any]) -> None:
    write_json_report(json_path, report)
    write_html_report(html_path, report)


def verify_report(report: dict[str, Any], selected_cases: Sequence[str]) -> None:
    if report.get("protocol") != REPORT_PROTOCOL_VERSION:
        raise HarnessError("report protocol does not match the harness report protocol")
    try:
        case_ids = [case["case_id"] for case in report["cases"]]
    except (KeyError, TypeError) as error:
        raise HarnessError("report cases have an invalid identity") from error
    if case_ids != list(selected_cases) or len(case_ids) != len(set(case_ids)):
        raise HarnessError("report cases do not exactly match the selected plan")
    if not isinstance(report.get("scratch_root"), str) or not os.path.isabs(
        report["scratch_root"]
    ):
        raise HarnessError("report scratch_root is not an absolute path")
    recomputed = _report_for(
        [CaseResult(**case) for case in report["cases"]],
        report["global_errors"],
        scratch_root=report["scratch_root"],
    )["summary"]
    if recomputed != report["summary"]:
        raise HarnessError("report summary does not match its case details")


def print_results(results: Sequence[CaseResult]) -> None:
    labels = {
        "passed": "PASS",
        "expectation_failed": "FAIL",
        "infrastructure_failed": "ERROR",
        "skipped": "SKIP",
    }
    for result in results:
        print(f"[{labels[result.status]}] {result.case_id}")
        if result.message:
            print(f"  {result.message}")
        if result.diff:
            print(result.diff, end="" if result.diff.endswith("\n") else "\n")


def _definition_fingerprint(cases: dict[str, CaseDefinition]) -> tuple[object, ...]:
    return tuple(
        (
            case.case_id,
            case.selector,
            case.driver,
            case.expectation,
            case.scope,
            case.labels,
            case.prerequisites,
            case.udir_spec,
            case.exec_spec,
            case.cargo_spec,
            case.input_tree,
            case.comparator_sha256,
        )
        for case in cases.values()
    )


def _golden_is_complete(case: CaseDefinition) -> bool:
    golden = case.golden
    return not (
        ("stdout" in golden.comparisons and golden.stdout is None)
        or ("stderr" in golden.comparisons and golden.stderr is None)
        or ("status" in golden.comparisons and golden.status is None)
        or ("filesystem" in golden.comparisons and golden.filesystem is None)
        or (
            "filesystem-script" in golden.comparisons
            and golden.filesystem_archive_sha256 is None
        )
    )


def _validate_report_targets(
    root: Path, binary: PinnedBinary, json_report: Path, html_report: Path
) -> None:
    resolved_root = root.resolve()
    targets = [json_report.resolve(), html_report.resolve()]
    if targets[0] == targets[1]:
        raise HarnessError("JSON and HTML reports must use different paths")
    for target in targets:
        if target == resolved_root or resolved_root in target.parents:
            raise HarnessError("report paths must be outside the test tree")
        if target == binary.source_path:
            raise HarnessError("report path must not replace the unit-test binary")


def _prepare_scratch_root(requested: Path, repository: Path) -> Path:
    """Create or validate one private scratch boundary outside the source repository."""

    try:
        expanded = requested.expanduser()
        resolved = expanded.resolve(strict=False)
        source_repository = repository.resolve(strict=True)
    except OSError as error:
        raise HarnessError(f"could not resolve scratch boundary: {error}") from error
    if resolved == source_repository or source_repository in resolved.parents:
        raise HarnessError(f"scratch root must be outside the source repository: {resolved}")
    existed = os.path.lexists(resolved)
    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed:
            resolved.chmod(0o700)
        root_stat = resolved.lstat()
    except OSError as error:
        raise HarnessError(f"could not establish scratch root {resolved}: {error}") from error
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise HarnessError(f"scratch root is not a real directory: {resolved}")
    if root_stat.st_uid != os.geteuid():
        raise HarnessError(f"scratch root is not owned by the current user: {resolved}")
    if stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise HarnessError(f"scratch root permits group or other access: {resolved}")
    return resolved


def _run_suite(
    root: Path,
    repository: Path,
    binary: PinnedBinary,
    json_report: Path,
    html_report: Path,
    selection: Selection,
    update_golden: bool,
    scratch_root: Path,
    temporary_root: Path,
) -> int:
    initial_binary_digest = binary.sha256
    try:
        _validate_report_targets(root, binary, json_report, html_report)
        cases = scan_case_tree(
            root,
            allow_missing_goldens=update_golden,
            temporary_root=temporary_root,
        )
        registered = list_registered_tests(binary, temporary_root=temporary_root)
        binary.verify_source()
        check_inventory(cases, registered)
        selected = _select_cases(cases, selection, update_golden=update_golden)
        missing_unselected = [
            case_id
            for case_id, case in cases.items()
            if not _golden_is_complete(case) and case_id not in selected
        ]
        if missing_unselected:
            raise HarnessError(
                "unselected cases have missing goldens: " + ", ".join(missing_unselected)
            )
        initial_definitions = _definition_fingerprint(cases)
    except (HarnessError, OSError) as error:
        report = _report_for(
            [], [str(error)], initial_binary_digest, scratch_root=scratch_root
        )
        try:
            write_reports(json_report, html_report, report)
        except HarnessError as report_error:
            print(f"viewspace-test: {report_error}", file=sys.stderr)
        print(f"viewspace-test: {error}", file=sys.stderr)
        return 2

    cargo_executables: dict[tuple[str, str, str], CargoExecutable] = {}
    cargo_error: str | None = None
    if any(
        cases[case_id].driver == "cargo"
        and not _missing_prerequisites(cases[case_id], temporary_root=temporary_root)
        for case_id in selected
    ):
        try:
            cargo_executables = prepare_cargo(repository, temporary_root=temporary_root)
            check_cargo_inventory(cases, cargo_executables)
        except HarnessError as error:
            cargo_error = str(error)

    results = []
    for case_id in selected:
        case = cases[case_id]
        started = time.monotonic_ns()
        missing = _missing_prerequisites(case, temporary_root=temporary_root)
        if missing:
            driver = DriverResult(
                "skipped", "", "", "missing prerequisites: " + ", ".join(missing)
            )
        elif case.driver in UNIT_DRIVERS:
            driver = invoke_unit_test(binary, case, temporary_root=temporary_root)
        elif case.driver == "exec":
            driver = invoke_exec(
                repository,
                case,
                temporary_root=temporary_root,
                update_golden=update_golden,
            )
        elif cargo_error is not None:
            driver = DriverResult("infrastructure", "", "", cargo_error)
        else:
            driver = invoke_cargo(
                repository,
                case,
                cargo_executables,
                temporary_root=temporary_root,
            )
        duration_ms = (time.monotonic_ns() - started) // 1_000_000
        results.append(
            _update_golden(case, driver, duration_ms)
            if update_golden
            else evaluate_case(case, driver, duration_ms)
        )

    global_errors: list[str] = []
    try:
        final_cases = scan_case_tree(
            root,
            allow_missing_goldens=update_golden,
            temporary_root=temporary_root,
        )
        final_registered = list_registered_tests(binary, temporary_root=temporary_root)
        check_inventory(final_cases, final_registered)
        if final_registered != registered:
            raise HarnessError("unit-test binary inventory changed during the run")
        if _definition_fingerprint(final_cases) != initial_definitions:
            raise HarnessError("test definitions or input fixtures changed during the run")
        binary.verify_source()
        if not update_golden and final_cases != cases:
            raise HarnessError("test tree changed during the run")
    except HarnessError as error:
        global_errors.append(f"post-run validation failed: {error}")

    if selection.scope == "basic" and any(result.status == "skipped" for result in results):
        global_errors.append("Basic regression does not permit skipped cases")

    report = _report_for(
        results, global_errors, initial_binary_digest, scratch_root=scratch_root
    )
    print_results(results)
    for error in global_errors:
        print(f"[ERROR] suite: {error}")
    try:
        verify_report(report, selected)
        write_reports(json_report, html_report, report)
    except (HarnessError, OSError) as error:
        print(f"viewspace-test: {error}", file=sys.stderr)
        return 2
    print(
        "summary: "
        f"{report['summary']['passed']} passed, "
        f"{report['summary']['skipped']} skipped, "
        f"{report['summary']['expectation_failed']} failed, "
        f"{report['summary']['infrastructure_failed']} infrastructure errors, "
        f"{report['summary']['goldens_updated']} goldens updated, "
        f"{report['summary']['unscoped']} unscoped compatibility cases"
    )
    if report["summary"]["infrastructure_failed"]:
        return 2
    if report["summary"]["expectation_failed"]:
        return 1
    return 0


@_serialized_harness_operation()
def run_suite(
    root: Path,
    binary: Path,
    json_report: Path,
    html_report: Path,
    selection: Selection | None = None,
    *,
    repository: Path | None = None,
    update_golden: bool = False,
    scratch_root: Path | None = None,
) -> int:
    repository = repository or Path(__file__).resolve().parent.parent
    requested_scratch = scratch_root or Path.home() / ".scratch" / "viewspace-test"
    try:
        resolved_scratch = _prepare_scratch_root(requested_scratch, repository)
        with _pin_binary(binary) as pinned:
            with _report_operation_lock(json_report):
                with tempfile.TemporaryDirectory(prefix="r-", dir=resolved_scratch) as temporary_name:
                    return _run_suite(
                        root,
                        repository,
                        pinned,
                        json_report,
                        html_report,
                        selection or Selection(),
                        update_golden,
                        resolved_scratch,
                        Path(temporary_name),
                    )
    except HarnessError as error:
        try:
            reported_scratch = requested_scratch.expanduser().resolve(strict=False)
        except OSError:
            reported_scratch = requested_scratch.absolute()
        report = _report_for([], [str(error)], scratch_root=reported_scratch)
        try:
            write_reports(json_report, html_report, report)
        except HarnessError as report_error:
            print(f"viewspace-test: {report_error}", file=sys.stderr)
        print(f"viewspace-test: {error}", file=sys.stderr)
        return 2


def build_unit_test_binary(repository: Path) -> Path:
    """Pipeline fork: never build or invoke an VIEWSPACE engine component."""
    return repository / "utils" / "empty_unit_inventory.py"


def build_argument_parser(repository: Path | None = None) -> argparse.ArgumentParser:
    repository = repository or Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(prog="viewspace-test")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run VIEWSPACE harness-managed tests")
    run_parser.add_argument("--root", type=Path, default=repository / "tests")
    run_parser.add_argument(
        "--scratch-root",
        type=Path,
        default=Path.home() / ".scratch" / "viewspace-test",
        help="private out-of-repository root for all harness runtime scratch",
    )
    run_parser.add_argument("--binary", type=Path)
    run_parser.add_argument(
        "--report",
        type=Path,
        default=repository / "target" / "viewspace-test-report.json",
    )
    run_parser.add_argument(
        "--html-report",
        type=Path,
        default=repository / "target" / "viewspace-test-report.html",
    )
    run_parser.add_argument("--case", action="append", default=[], help="exact case ID")
    run_parser.add_argument("--segment", action="append", default=[])
    run_parser.add_argument("--tag", action="append", default=[])
    run_parser.add_argument("--glob", action="append", default=[], dest="globs")
    run_parser.add_argument(
        "--scope",
        choices=tuple(SCOPE_RANK),
        default="exhaustive",
        help="cumulative regression scope (default: exhaustive for backward compatibility)",
    )
    run_parser.add_argument("--profile", choices=sorted(PROFILE_LABELS), default="default")
    run_parser.add_argument("--update-golden", action="store_true")
    run_parser.add_argument("--all", action="store_true", dest="explicit_all")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    normalized_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(normalized_arguments) == 2 and normalized_arguments[0] == "--_internal-supervisor":
        try:
            expected_parent = int(normalized_arguments[1])
        except ValueError:
            return 2
        if expected_parent <= 1:
            return 2
        return _internal_supervisor_main(expected_parent)
    repository = Path(__file__).resolve().parent.parent
    options = build_argument_parser(repository).parse_args(normalized_arguments)
    try:
        if options.command == "run":
            if options.binary is None:
                try:
                    binary = build_unit_test_binary(repository)
                except HarnessError as error:
                    try:
                        report = _report_for(
                            [],
                            [str(error)],
                            scratch_root=options.scratch_root.expanduser().absolute(),
                        )
                        write_reports(options.report, options.html_report, report)
                    except HarnessError as report_error:
                        print(f"viewspace-test: {report_error}", file=sys.stderr)
                    raise
            else:
                binary = options.binary
            selection = Selection(
                cases=tuple(options.case),
                segments=tuple(options.segment),
                tags=tuple(options.tag),
                globs=tuple(options.globs),
                scope=options.scope,
                profile=options.profile,
                explicit_all=options.explicit_all,
            )
            return run_suite(
                options.root,
                binary,
                options.report,
                options.html_report,
                selection,
                repository=repository,
                update_golden=options.update_golden,
                scratch_root=options.scratch_root,
            )
    except (HarnessError, OSError) as error:
        print(f"viewspace-test: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command dispatch")


if __name__ == "__main__":
    raise SystemExit(main())
