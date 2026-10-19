"""Supervise the one private git-viewspace-fs mount owned by a daemon."""

import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .cache_config import content_cache_budget
from .errors import GitViewspaceError
from .host import mount_at, process_start_time_ticks, validate_private_mount


class ViewFS:
    def __init__(
        self,
        context,
        *,
        executable=None,
        sudo=None,
        umount=None,
        popen=subprocess.Popen,
        run=subprocess.run,
        timeout=12.0,
    ):
        self.context = context
        # Snapshot at daemon construction; supervised restarts reuse this value.
        try:
            self.content_cache_bytes = content_cache_budget()
        except ValueError as error:
            raise GitViewspaceError("INVALID_CONFIGURATION", str(error)) from error
        # Preserve the virtual-environment path here: resolving the Python
        # symlink first would look beside /usr/bin/python instead.
        sibling = Path(sys.executable).parent / "git-viewspace-fs"
        selected = executable or (
            str(sibling) if sibling.is_file() else shutil.which("git-viewspace-fs")
        )
        if selected is None:
            raise GitViewspaceError(
                "VIEWFS_NOT_INSTALLED",
                "git-viewspace-fs is not installed beside git-viewspace or on PATH",
            )
        self.executable = str(Path(selected).resolve())
        self.sudo = str(Path(sudo or shutil.which("sudo") or "/usr/bin/sudo").resolve())
        self.umount = str(Path(umount or shutil.which("umount") or "/usr/bin/umount").resolve())
        self._popen = popen
        self._run = run
        self.timeout = timeout
        self.process = None
        self.supervisor_start_time_ticks = None
        self.attempt_token = None
        self.source = None
        self.stderr_path = context.root / "run/viewfs.stderr"

    def _stderr_stream(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.stderr_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != self.context.uid
            or metadata.st_gid != self.context.gid
        ):
            os.close(descriptor)
            raise GitViewspaceError("INVALID_RUNTIME", "git-viewspace-fs diagnostic file has invalid identity")
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "wb", buffering=0)

    def _diagnostic(self):
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-8192:].strip()
        except OSError:
            return ""

    def _command(self, token):
        return (
            self.sudo,
            "-n",
            "--",
            self.executable,
            str(self.context.repository),
            str(self.context.mountpoint),
            "--config",
            str(self.context.config_path),
            "--workload-uid",
            str(self.context.uid),
            "--workload-gid",
            str(self.context.gid),
            "--content-cache-bytes",
            str(self.content_cache_bytes),
            "--root-uuid",
            self.context.root_uuid,
            "--attempt-token",
            token,
        )

    def _validate_exposed_root(self):
        for path in (self.context.mountpoint, self.context.mountpoint / "views"):
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self.context.uid
                or metadata.st_gid != self.context.gid
                or stat.S_IMODE(metadata.st_mode) != 0o755
            ):
                raise GitViewspaceError(
                    "VIEWFS_PROFILE_MISMATCH", "git-viewspace-fs exposed an invalid root profile"
                )

    def start(self):
        if self.process is not None:
            raise GitViewspaceError("INVALID_RUNTIME", "git-viewspace-fs is already supervised")
        if mount_at(self.context.mountpoint) is not None:
            raise GitViewspaceError("FOREIGN_MOUNT", "private mountpoint is already mounted")
        token = uuid.uuid4().hex
        source = f"GitViewspaceFS:{self.context.root_uuid}:{token}"
        with self._stderr_stream() as error_stream:
            try:
                process = self._popen(
                    self._command(token),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_stream,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as error:
                raise GitViewspaceError(
                    "VIEWFS_START_FAILED", "could not launch git-viewspace-fs", detail=str(error)
                ) from error
        try:
            supervisor_start_time_ticks = process_start_time_ticks(process.pid)
        except GitViewspaceError:
            self._terminate_after_failed_start(process, source)
            raise
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise GitViewspaceError(
                    "VIEWFS_START_FAILED",
                    "git-viewspace-fs exited before becoming ready",
                    detail=self._diagnostic(),
                )
            try:
                mounted = validate_private_mount(self.context.mountpoint, source)
            except GitViewspaceError:
                self._terminate_after_failed_start(process, source)
                raise
            if mounted is not None:
                try:
                    self._validate_exposed_root()
                except (OSError, GitViewspaceError):
                    self._terminate_after_failed_start(process, source)
                    raise
                self.process = process
                self.supervisor_start_time_ticks = supervisor_start_time_ticks
                self.attempt_token = token
                self.source = source
                return self.runtime()
            time.sleep(0.05)
        self._terminate_after_failed_start(process, source)
        raise GitViewspaceError(
            "VIEWFS_START_FAILED",
            "timed out waiting for git-viewspace-fs mount",
            detail=self._diagnostic(),
        )

    def _unmount(self, source, *, lazy=False):
        mounted = validate_private_mount(self.context.mountpoint, source)
        if mounted is None:
            return
        arguments = [self.sudo, "-n", "--", self.umount]
        if lazy:
            arguments.append("-l")
        arguments.extend(("--", str(self.context.mountpoint)))
        try:
            result = self._run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitViewspaceError(
                "UNMOUNT_FAILED", "could not unmount git-viewspace-fs", detail=str(error)
            ) from error
        if result.returncode != 0:
            raise GitViewspaceError(
                "UNMOUNT_FAILED",
                "could not unmount git-viewspace-fs",
                detail=result.stderr.decode("utf-8", "replace").strip(),
            )

    def cleanup_mount(self, source, *, force=True):
        try:
            self._unmount(source)
        except GitViewspaceError:
            if not force:
                raise
            self._unmount(source, lazy=True)
        if mount_at(self.context.mountpoint) is not None:
            raise GitViewspaceError("UNMOUNT_FAILED", "git-viewspace-fs mount remains after unmount")

    def _wait_process(self, process):
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                raise GitViewspaceError(
                    "VIEWFS_PROCESS_REMAINS",
                    "git-viewspace-fs supervisor remains after unmount",
                ) from error

    def _terminate_after_failed_start(self, process, source):
        try:
            if validate_private_mount(self.context.mountpoint, source) is not None:
                self.cleanup_mount(source)
        finally:
            if process.poll() is None:
                self._wait_process(process)

    def stop(self, *, force=True):
        if self.source is not None:
            self.cleanup_mount(self.source, force=force)
        if self.process is not None and self.process.poll() is None:
            self._wait_process(self.process)
        self.process = None
        self.supervisor_start_time_ticks = None
        self.attempt_token = None
        self.source = None

    def healthy(self, observed=None):
        if self.process is None or self.process.poll() is not None or self.source is None:
            return False
        try:
            return (
                validate_private_mount(
                    self.context.mountpoint,
                    self.source,
                    observed,
                )
                is not None
            )
        except GitViewspaceError:
            return False

    def runtime(self):
        if (
            self.process is None
            or self.supervisor_start_time_ticks is None
            or self.attempt_token is None
            or self.source is None
        ):
            raise GitViewspaceError("INVALID_RUNTIME", "git-viewspace-fs is not running")
        return {
            "attempt_token": self.attempt_token,
            "source": self.source,
            "supervisor_pid": self.process.pid,
            "supervisor_start_time_ticks": self.supervisor_start_time_ticks,
        }

    def remove_diagnostics(self):
        try:
            self.stderr_path.unlink()
        except FileNotFoundError:
            pass
