"""Per-root git-viewspace daemon and git-viewspace-fs supervisor."""

import argparse
import contextlib
import os
import signal
import socket
import stat
import sys
import time
from pathlib import Path

from .errors import GitViewspaceError
from .git import Git
from .host import (
    filesystem_at,
    indexed_mounts,
    mount_at,
    process_matches,
    process_start_time_ticks,
    validate_socket_path,
)
from .protocol import (
    VIEW_ID_GUARDED_COMMANDS,
    receive_message,
    send_message,
    validate_request,
)
from .root import RootContext
from .schemas import (
    NEXT_FORMAT,
    RUNTIME_FORMAT,
    SCHEMA_VERSION,
    STAGE_FORMAT,
    SYNC_FORMAT,
    next_identifier,
    stage_identifier,
    sync_identifier,
    sync_manifest_digest,
    validate_next,
    validate_runtime,
    validate_stage,
    validate_state,
    validate_sync,
    validate_view_config,
    validate_view_name,
    validate_view_state,
)
from .storage import (
    atomic_json,
    canonical_json_bytes,
    exclusive_lock,
    fsync_directory,
    read_json,
    validate_stable_file,
)
from .sync import OverlaySync
from .viewfs import ViewFS
from .views import ViewMounts, ViewStore


CONTROL_SOCKET_BACKLOG = 128
REQUEST_READ_TIMEOUT_SECONDS = 5.0


class Daemon:
    def __init__(
        self,
        context,
        *,
        viewfs_factory=ViewFS,
        mount_factory=ViewMounts,
        git=None,
        filesystem=filesystem_at,
        sync_factory=OverlaySync,
    ):
        self.context = context
        self.viewfs = viewfs_factory(context)
        self.mounts = mount_factory(context)
        self.store = ViewStore(context)
        self.git = git or Git()
        self.filesystem = filesystem
        self.sync = sync_factory()
        self.restart_count = 0
        self.server = None
        self.stopping = False
        self.signal_stop = False

    def _ensure_view_identity(self, name, expected_view_id):
        view = self.store.get(name)
        if view.view_id != expected_view_id:
            raise GitViewspaceError(
                "VIEW_ID_MISMATCH",
                f"view {name} has ID {view.view_id}, not expected ID {expected_view_id}",
            )
        return view

    def _validate_durable_state(self, command, requested_view):
        """Match cheap durable state to the daemon's authoritative memory.

        Repository qualification belongs to daemon startup.  Routine requests
        re-read only the small state-machine records that could change the
        meaning of a command.
        """

        validate_stable_file(
            self.context.state_path,
            self.context.uid,
            self.context.gid,
        )
        manager_state = validate_state(read_json(self.context.state_path))
        if manager_state != self.store._manager_state():
            raise GitViewspaceError(
                "STALE_DURABLE_STATE",
                "durable manager state differs from the running daemon",
            )
        validate_stable_file(
            self.context.config_path,
            self.context.uid,
            self.context.gid,
        )
        if read_json(self.context.config_path) != {
            view.alias: {"commit": view.commit}
            for view in sorted(self.store.views.values(), key=lambda item: item.alias)
        }:
            raise GitViewspaceError(
                "STALE_DURABLE_STATE",
                "durable git-viewspace-fs configuration differs from the running daemon",
            )
        try:
            actual_names = {entry.name for entry in (self.context.root / "views").iterdir()}
        except OSError as error:
            raise GitViewspaceError("INVALID_STATE", "cannot inspect durable view registry") from error
        if actual_names != set(self.store.views):
            raise GitViewspaceError(
                "STALE_DURABLE_STATE",
                "durable view directories differ from the running daemon",
            )

        selected = (
            tuple(self.store.views.values())
            if command in {"status", "stop", "view-status", "branch-merge"}
            and requested_view is None
            else (() if requested_view not in self.store.views else (self.store.views[requested_view],))
        )
        for view in selected:
            validate_stable_file(view.config_path, self.context.uid, self.context.gid)
            validate_stable_file(view.state_path, self.context.uid, self.context.gid)
            config = validate_view_config(read_json(view.config_path))
            state = validate_view_state(read_json(view.state_path))
            if config != self.store._config(view, self.context.root_uuid) or state != self.store._state(
                view, self.context.root_uuid
            ):
                raise GitViewspaceError(
                    "STALE_DURABLE_STATE",
                    f"durable view state differs from the running daemon: {view.name}",
                )

    def _runtime(self, *, ready):
        return {
            "format": RUNTIME_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "daemon": {
                "pid": os.getpid(),
                "start_time_ticks": process_start_time_ticks(),
            },
            "viewfs": self.viewfs.runtime(),
            "restart_count": self.restart_count,
            "ready": ready,
        }

    def _write_runtime(self, *, ready):
        value = self._runtime(ready=ready)
        validate_runtime(value)
        atomic_json(self.context.runtime_path, value)
        return value

    def _remove_runtime_file(self, path):
        try:
            path.unlink()
            fsync_directory(path.parent)
        except FileNotFoundError:
            pass

    def _wait_stale_supervisor(self, runtime):
        identity = runtime["viewfs"]
        deadline = time.monotonic() + self.viewfs.timeout
        while time.monotonic() < deadline:
            if not process_matches(
                identity["supervisor_pid"],
                identity["supervisor_start_time_ticks"],
            ):
                return
            time.sleep(0.05)
        raise GitViewspaceError(
            "VIEWFS_PROCESS_REMAINS",
            "stale git-viewspace-fs supervisor remains after unmount",
        )

    def reconcile(self):
        unexplained = self.context.runtime_entries()
        if unexplained:
            raise GitViewspaceError(
                "FOREIGN_RUNTIME",
                "managed runtime directory contains unexplained entries",
                detail=", ".join(unexplained),
            )
        previous = self.context.runtime()
        if previous is not None and process_matches(
            previous["daemon"]["pid"], previous["daemon"]["start_time_ticks"]
        ):
            raise GitViewspaceError("DAEMON_CONFLICT", "recorded daemon is alive without its locks")

        mounted = mount_at(self.context.mountpoint)
        public_mounted = [
            view for view in self.store.views.values() if mount_at(view.mountpoint) is not None
        ]
        routed_residue = []
        from .routing import facts as routing_facts
        for view in self.store.views.values():
            if view.routing is not None or (view.path / "routing").exists():
                routing = routing_facts(view)
                if (routing["private_mounted"] or routing["process_alive"]
                    or routing["runtime"]["record_present"] or routing["runtime"]["mounted_paths"]
                    or (view.path / "routing/running.json").exists()):
                    routed_residue.append(view)
        if (public_mounted or routed_residue) and previous is None:
            raise GitViewspaceError("FOREIGN_MOUNT", "public view mount has no matching runtime record")
        if public_mounted or routed_residue:
            self.mounts.cleanup_all(
                self.store.views,
                previous["viewfs"]["source"],
                force=True,
            )
        # Recovery requiring operator rebind must not prevent the control
        # daemon from starting. Preserve the fault and leave that view stopped.
        for view in list(self.store.views.values()):
            if view.routing is not None and (view.path / "routing/uncertain").exists():
                self.store.set_desired(view.name, False)
        if mounted is not None:
            if previous is None:
                raise GitViewspaceError("FOREIGN_MOUNT", "private mount has no matching runtime record")
            self.viewfs.cleanup_mount(previous["viewfs"]["source"])
            self._wait_stale_supervisor(previous)
        elif previous is not None and process_matches(
            previous["viewfs"]["supervisor_pid"],
            previous["viewfs"]["supervisor_start_time_ticks"],
        ):
            raise GitViewspaceError(
                "VIEWFS_PROCESS_REMAINS",
                "stale git-viewspace-fs supervisor exists without its mount",
            )

        if self.context.socket_path.exists() or self.context.socket_path.is_symlink():
            metadata = self.context.socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode):
                raise GitViewspaceError("FOREIGN_RUNTIME", "control-socket path is not a socket")
            self._remove_runtime_file(self.context.socket_path)
        self._remove_runtime_file(self.context.runtime_path)
        self.viewfs.remove_diagnostics()

    def _bind_socket(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.context.socket_path))
            os.chmod(self.context.socket_path, 0o600)
            validate_socket_path(self.context.socket_path, self.context.uid, self.context.gid)
            server.listen(CONTROL_SOCKET_BACKLOG)
            server.settimeout(0.20)
        except BaseException:
            server.close()
            self._remove_runtime_file(self.context.socket_path)
            raise
        self.server = server

    def start(self):
        self.store.publish_config()
        self.viewfs.start()
        try:
            # Publish the exact child/mount identity before restoring public
            # views.  A replacement daemon can then reconcile an interrupted
            # startup without guessing which private mount it owns.
            self._write_runtime(ready=False)
            self._restore_views()
            self._bind_socket()
            return self._write_runtime(ready=True)
        except BaseException:
            cleanup_complete = False
            try:
                if self.viewfs.source is not None:
                    self.mounts.cleanup_all(self.store.views, self.viewfs.source, force=True)
                self.viewfs.stop(force=True)
                cleanup_complete = True
            finally:
                if self.server is not None:
                    self.server.close()
                    self.server = None
                self._remove_runtime_file(self.context.socket_path)
                if cleanup_complete:
                    self._remove_runtime_file(self.context.runtime_path)
                    self.viewfs.remove_diagnostics()
            raise

    def _repository_status(self):
        head, branch = self.git.checkout_position(self.context.repository)
        return {
            "healthy": True,
            "head": head,
            "branch": branch,
            "object_format": "sha1",
            "clean": not self.git.checkout_status(self.context.repository),
            "operation_in_progress": self.store.repository_operation is not None,
        }

    def _require_parent_merge_available(self, parent_branch):
        if self.store.repository_operation is not None:
            raise GitViewspaceError(
                "GIT_OPERATION_IN_PROGRESS",
                "a managed view transition currently owns the repository checkout",
            )
        owner = next(
            (
                view
                for view in self.store.views.values()
                if view.kind == "overlay" and view.branch == parent_branch
            ),
            None,
        )
        if owner is not None:
            raise GitViewspaceError(
                "BRANCH_CLAIMED",
                "parent branch is claimed by a writable view",
                data={
                    "parent_branch": parent_branch,
                    "view_name": owner.name,
                    "view_id": owner.view_id,
                },
            )

    def branch_merge(
        self,
        attempt_branch,
        parent_branch,
        expected_attempt_tip,
        message,
        checked_out_destination="refuse",
    ):
        """Merge an exact attempt tip under one checked-out-destination policy."""

        result = self.git.merge_local_branches(
            self.context.repository,
            attempt_branch,
            parent_branch,
            expected_attempt_tip,
            message,
            self.context.git_identity,
            lambda: self._require_parent_merge_available(parent_branch),
            checked_out_destination,
        )
        return {
            "ok": True,
            "command": "branch merge",
            "root": str(self.context.root),
            **result,
        }

    def branch_pin(self, branch, commit):
        """Pin one exact commit on an immutable repository-only source branch."""

        result = self.git.pin_local_branch(
            self.context.repository,
            branch,
            commit,
        )
        return {
            "ok": True,
            "command": "branch pin",
            "root": str(self.context.root),
            **result,
        }

    def submodule_prepare(self, at, path, commit):
        from .submodules import prepare
        return {"ok": True, "command": "submodule prepare", "root": str(self.context.root),
                **prepare(self.context.repository, at, path, commit)}

    def submodule_pin(self, branch, path, commit, message):
        from .submodules import pin
        self._require_parent_merge_available(branch)
        return {"ok": True, "command": "submodule pin", "root": str(self.context.root),
                **pin(self.context.repository, branch, path, commit, message, self.context.git_identity)}

    def status(self):
        observed = indexed_mounts()
        mounted = mount_at(self.context.mountpoint, observed)
        viewfs_healthy = self.viewfs.healthy(observed)
        views = self._view_statuses(observed)
        repository = self._repository_status()
        issues = []
        if not viewfs_healthy:
            issues.append("git-viewspace-fs is not healthy")
        issues.extend(
            f"view desired and actual states differ: {view['name']}"
            for view in views
            if not view["healthy"]
        )
        if not repository["clean"] and not repository["operation_in_progress"]:
            issues.append("repository checkout is dirty without a current manager operation")
        return {
            "ok": True,
            "command": "status",
            "root": str(self.context.root),
            "root_uuid": self.context.root_uuid,
            "initialized": True,
            "healthy": not issues,
            "repository": repository,
            "daemon": {
                "running": True,
                "pid": os.getpid(),
                "start_time_ticks": process_start_time_ticks(),
                "socket": str(self.context.socket_path),
            },
            "viewfs": {
                **self.viewfs.runtime(),
                "healthy": viewfs_healthy,
                "mounted": mounted is not None,
                "restart_count": self.restart_count,
            },
            "views": views,
            "pending_transactions": [],
            "issues": issues,
        }

    def _recover_viewfs(self):
        try:
            self._write_runtime(ready=False)
        except GitViewspaceError:
            pass
        if self.viewfs.source is not None:
            self.mounts.cleanup_all(self.store.views, self.viewfs.source, force=True)
        self.viewfs.stop(force=True)
        self.viewfs.remove_diagnostics()
        self.restart_count += 1
        self.viewfs.start()
        # The preceding record names the retired child.  Replace it before
        # restoring public mounts so another daemon can identify the new one
        # if this recovery attempt is interrupted.
        self._write_runtime(ready=False)
        self._restore_views()
        self._write_runtime(ready=True)

    def supervise(self):
        observed = indexed_mounts()
        if not self.viewfs.healthy(observed):
            self._recover_viewfs()
            return
        for view in sorted(self.store.views.values(), key=lambda item: item.name):
            mounted = self.mounts.observe(view, self.viewfs.source, observed)
            if view.desired_started and mounted is None:
                self.mounts.start(view, self.viewfs.source)
            elif not view.desired_started and mounted is not None:
                self.mounts.stop(view, self.viewfs.source, force=True)

    def _shutdown(self):
        self.stopping = True
        try:
            self._write_runtime(ready=False)
        except (GitViewspaceError, OSError):
            # This record only closes the readiness window for observers.  A
            # failed advisory write must not prevent the forceful cleanup that
            # gives root-level stop its meaning.
            pass
        if self.viewfs.source is not None:
            self.mounts.cleanup_all(self.store.views, self.viewfs.source, force=True)
        self.viewfs.stop(force=True)
        self.viewfs.remove_diagnostics()
        if self.server is not None:
            self.server.close()
            self.server = None
        self._remove_runtime_file(self.context.socket_path)
        self._remove_runtime_file(self.context.runtime_path)
        if mount_at(self.context.mountpoint) is not None:
            raise GitViewspaceError("SHUTDOWN_INCOMPLETE", "manager-owned mount remains after shutdown")

    def _restore_views(self):
        for view in sorted(self.store.views.values(), key=lambda item: item.name):
            if view.desired_started:
                self._validate_overlay_branch(view)
                self.mounts.start(view, self.viewfs.source)

    def _validate_overlay_branch(self, view):
        if view.kind == "readonly":
            return
        tip = self.git.resolve_local_branch(self.context.repository, view.branch)
        expected = view.next["commit"] if view.next is not None else view.overlay_base
        if tip != expected:
            raise GitViewspaceError(
                "BRANCH_DIVERGED",
                f"owned branch tip differs from the expected transition commit: {view.branch}",
            )

    def _normalize_stopped_work(self, view):
        """Let OverlayFS consume transient work entries using only mount/unmount."""
        self._validate_overlay_branch(view)
        self.mounts.start(view, self.viewfs.source)
        self.mounts.stop(view, self.viewfs.source, force=False)

    def _view_status(self, view, observed=None):
        mounted = self.mounts.observe(view, self.viewfs.source, observed)
        value = {
            "name": view.name,
            "kind": view.kind,
            "view_id": view.view_id,
            "commit": view.commit,
            "alias": view.alias,
            "desired_started": view.desired_started,
            "actual": view.kind if mounted is not None else "stopped",
            "healthy": (mounted is not None) == view.desired_started,
            "mountpoint": str(view.mountpoint),
        }
        if view.routing is not None:
            from .routing import facts
            value["routing"] = facts(view)
            if value["routing"]["faults"] or value["routing"]["requires_rebind"]:
                value["healthy"] = False
        if view.kind == "overlay":
            value.update(
                {
                    "branch": view.branch,
                    "branch_tip": self.git.resolve_local_branch(
                        self.context.repository,
                        view.branch,
                    ),
                    "overlay_base": view.overlay_base,
                    "generation": view.generation_id,
                    "dirty": None if mounted is not None else self.store.dirty(view),
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
            if value["branch_tip"] != expected_tip:
                value["healthy"] = False
        return value

    def _view_statuses(self, observed=None):
        return [
            self._view_status(view, observed)
            for view in sorted(self.store.views.values(), key=lambda item: item.name)
        ]

    def view_init_readonly(self, name, commitish, *, report=True):
        commit = self.git.resolve_commit(self.context.repository, commitish)
        view, idempotent = self.store.initialize(name, commit, self.mounts.probe)
        return {
            "ok": True,
            "command": "view init readonly",
            "root": str(self.context.root),
            "view": self._view_status(view) if report else None,
            "idempotent": idempotent,
        }

    def view_init_overlay(self, name, branch, *, report=True):
        view = self.store.get(name)
        if self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before writable initialization")
        if self.filesystem(view.path).filesystem_type != "ext4":
            raise GitViewspaceError("UNSUPPORTED_FILESYSTEM", "writable overlay storage requires ext4")
        tip = self.git.resolve_direct_local_branch(self.context.repository, branch)
        if tip != view.commit:
            raise GitViewspaceError("BRANCH_DIVERGED", "branch tip must equal the pinned view commit")
        changed, idempotent = self.store.init_overlay(name, branch)
        return {
            "ok": True,
            "command": "view init overlay",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "idempotent": idempotent,
        }

    def view_start(self, name, *, report=True):
        original = self.store.get(name)
        self._validate_overlay_branch(original)
        view, desired_idempotent = self.store.set_desired(name, True)
        try:
            mount_idempotent = self.mounts.start(view, self.viewfs.source)
        except BaseException:
            if not original.desired_started:
                self.store.set_desired(name, False)
            raise
        return {
            "ok": True,
            "command": "view start",
            "root": str(self.context.root),
            "view": self._view_status(view) if report else None,
            "idempotent": desired_idempotent and mount_idempotent,
        }

    def view_sync(self, name, *, report=True):
        view = self.store.get(name)
        if view.kind != "overlay":
            raise GitViewspaceError("VIEW_NOT_WRITABLE", "view sync requires a writable view")
        if view.desired_started or self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before sync")
        owner = self.store.sync_owner()
        if owner is not None and owner.name != name:
            raise GitViewspaceError(
                "REPOSITORY_BUSY",
                f"checkout is synchronized for view {owner.name}; clean it first",
            )

        upper = view.upper_path.lstat()
        if (
            not stat.S_ISDIR(upper.st_mode)
            or upper.st_uid != self.context.uid
            or upper.st_gid != self.context.gid
            or stat.S_IMODE(upper.st_mode) != 0o755
        ):
            raise GitViewspaceError(
                "OVERLAY_OWNER_MISMATCH",
                "stopped upper does not belong to the managed workload identity",
            )

        previous = view.sync
        if previous is not None:
            self.store.clear_sync(name)
            view = self.store.get(name)
        base_tree = self.git.prepare_sync_checkout(
            self.context.repository,
            view.branch,
            view.overlay_base,
        )
        from .submodules import entries, protect_sync
        if entries(self.context.repository, view.overlay_base):
            inspected = self.sync.inspect(view.upper_path)
            protect_sync(self.context.repository, view.overlay_base, inspected["git_pathspecs"])
        replay = self.sync.replay(view.upper_path, self.context.repository)
        if (
            replay["workload_uid"] != self.context.uid
            or replay["workload_gid"] != self.context.gid
        ):
            raise GitViewspaceError(
                "OVERLAY_OWNER_MISMATCH",
                "stopped upper does not belong to the managed workload identity",
            )
        manifest = list(replay["git_pathspecs"])
        manifest_sha256 = sync_manifest_digest(manifest)
        record = {
            "format": SYNC_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "view_id": view.view_id,
            "sync_id": "0" * 32,
            "generation_id": view.generation_id,
            "branch": view.branch,
            "base_commit": view.overlay_base,
            "upper_fingerprint": replay["upper_fingerprint"],
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "base_tree": base_tree,
            "counts": replay["counts"],
        }
        record["sync_id"] = sync_identifier(record)
        validate_sync(record)
        changed = self.store.record_sync(name, record)
        return {
            "ok": True,
            "command": "view sync",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "sync": record,
            "idempotent": previous == record,
        }

    def _validate_current_sync(
        self, view, *, expected_index=None, verify_checkout=True, verify_index=True,
    ):
        if view.kind != "overlay" or view.sync is None:
            raise GitViewspaceError("SYNC_NOT_FOUND", "view has no current sync record")
        if view.desired_started or self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before repository transition")
        if view.next is not None:
            raise GitViewspaceError("NEXT_EXISTS", "view already has a committed next transition")
        owner = self.store.sync_owner()
        if owner is None or owner.name != view.name:
            raise GitViewspaceError("INVALID_STATE", "view does not own the repository operation")
        sync = view.sync
        if not verify_checkout:
            return sync
        head, branch = self.git.checkout_position(self.context.repository)
        if head != sync["base_commit"] or branch != sync["branch"]:
            raise GitViewspaceError("STALE_SYNC", "checkout no longer matches the sync base")
        allowed_indexes = (
            set(expected_index)
            if isinstance(expected_index, (tuple, list, set, frozenset))
            else {expected_index}
        )
        if verify_index and self.git.index_tree(self.context.repository) not in allowed_indexes:
            raise GitViewspaceError("STALE_INDEX", "Git index no longer matches the expected transition state")
        return sync

    def view_stage(self, name, *, report=True, _fresh_sync=False):
        view = self.store.get(name)
        prior = view.stage
        expected_index = prior["index_tree"] if prior is not None else (
            view.sync["base_tree"]
            if view.sync is not None
            else ""
        )
        sync = self._validate_current_sync(
            view, expected_index=expected_index, verify_index=not _fresh_sync,
        )
        index_tree = self.git.stage_manifest(
            self.context.repository,
            sync["base_commit"],
            sync["manifest"],
            reset_index=not _fresh_sync,
        )
        remaining = self.git.unstaged_paths(self.context.repository)
        if remaining:
            raise GitViewspaceError(
                "INCOMPLETE_SYNC_MANIFEST",
                "changed or untracked paths remain after targeted staging",
                detail=", ".join(remaining[:20]),
            )
        record = {
            "format": STAGE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "view_id": view.view_id,
            "stage_id": "0" * 32,
            "sync_id": sync["sync_id"],
            "generation_id": sync["generation_id"],
            "branch": sync["branch"],
            "base_commit": sync["base_commit"],
            "upper_fingerprint": sync["upper_fingerprint"],
            "manifest_sha256": sync["manifest_sha256"],
            "base_tree": sync["base_tree"],
            "checkout_tree": index_tree,
            "index_tree": index_tree,
        }
        record["stage_id"] = stage_identifier(record)
        validate_stage(record)
        changed = self.store.record_stage(name, record)
        return {
            "ok": True,
            "command": "view stage",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "stage": record,
            "idempotent": prior == record,
        }

    def view_commit(
        self, name, message, *, report=True, verify_worktree=True, _fresh_stage=False,
    ):
        view = self.store.get(name)
        if not isinstance(message, str) or not message or "\0" in message:
            raise GitViewspaceError("INVALID_MESSAGE", "commit message must be nonempty and contain no NUL")
        if view.next is not None:
            return {
                "ok": True,
                "command": "view commit",
                "root": str(self.context.root),
                "view": self._view_status(view) if report else None,
                "next": view.next,
                "idempotent": True,
            }
        if view.stage is None:
            raise GitViewspaceError("STAGE_NOT_FOUND", "view has no current stage record")
        stage = view.stage
        sync = self._validate_current_sync(view, verify_checkout=False)
        if verify_worktree and self.git.unstaged_paths(self.context.repository):
            raise GitViewspaceError("STALE_CHECKOUT", "checkout changed after staging")
        commit, changed_commit = self.git.commit_index(
            self.context.repository,
            stage["branch"],
            stage["base_commit"],
            stage["base_tree"],
            stage["index_tree"],
            message,
            self.context.git_identity,
            verify_index=not _fresh_stage,
        )
        record = {
            "format": NEXT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "view_id": view.view_id,
            "next_id": "0" * 32,
            "sync_id": sync["sync_id"],
            "stage_id": stage["stage_id"],
            "generation_id": stage["generation_id"],
            "branch": stage["branch"],
            "base_commit": stage["base_commit"],
            "commit": commit,
            "tree": stage["index_tree"],
            "changed": changed_commit,
        }
        record["next_id"] = next_identifier(record)
        validate_next(record)
        changed = self.store.record_next(name, record)
        return {
            "ok": True,
            "command": "view commit",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "next": record,
            "idempotent": False,
        }

    def view_stop(self, name, *, report=True):
        original = self.store.get(name)
        view, desired_idempotent = self.store.set_desired(name, False)
        try:
            mount_idempotent = self.mounts.stop(view, self.viewfs.source, force=False)
        except BaseException:
            if original.desired_started:
                self.store.set_desired(name, True)
            raise
        return {
            "ok": True,
            "command": "view stop",
            "root": str(self.context.root),
            "view": self._view_status(view) if report else None,
            "idempotent": desired_idempotent and mount_idempotent,
        }

    def view_status(self, name):
        selected = (
            [self.store.get(name)]
            if name is not None
            else sorted(self.store.views.values(), key=lambda item: item.name)
        )
        views = [self._view_status(view) for view in selected]
        return {
            "ok": True,
            "command": "view status",
            "root": str(self.context.root),
            "views": views,
            "view": views[0] if name is not None else None,
        }

    def view_switch(self, name, commitish=None, *, use_next=False, report=True):
        view = self.store.get(name)
        if view.desired_started or self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before switching")
        if use_next:
            if commitish is not None or view.next is None:
                raise GitViewspaceError("NEXT_NOT_FOUND", "view has no committed next transition")
            if view.sync is not None or view.stage is not None:
                raise GitViewspaceError("TRANSITION_EXISTS", "clean the committed view before switching")
            commit = view.next["commit"]
        else:
            if commitish is None:
                raise GitViewspaceError("INVALID_COMMIT", "switch requires --to or --next")
            commit = self.git.resolve_commit(self.context.repository, commitish)
        if view.kind == "overlay":
            if self.store.dirty(view):
                raise GitViewspaceError("VIEW_DIRTY", "writable view must be clean before switching")
            tip = self.git.resolve_local_branch(self.context.repository, view.branch)
            if tip != commit:
                raise GitViewspaceError("BRANCH_DIVERGED", "writable view may switch only to its branch tip")
        changed, idempotent = self.store.switch(
            name,
            commit,
            self.mounts.probe,
            consume_next=use_next,
        )
        return {
            "ok": True,
            "command": "view switch",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "idempotent": idempotent,
        }

    def view_destroy(self, name, discard, *, ensure_view_id=None):
        view = (
            self.store.get(name)
            if ensure_view_id is None
            else self._ensure_view_identity(name, ensure_view_id)
        )
        destroyed_view_id = view.view_id
        if self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before destruction")
        if (view.sync is not None or view.stage is not None or view.next is not None) and not discard:
            raise GitViewspaceError("TRANSITION_EXISTS", "view has a repository transition; use --discard to destroy it")
        if view.kind == "overlay" and self.store.dirty(view) and not discard:
            raise GitViewspaceError("VIEW_DIRTY", "writable view is dirty; use --discard to destroy it")
        if discard and view.kind == "overlay":
            owner = self.store.sync_owner()
            if owner is not None and owner.name != name:
                raise GitViewspaceError(
                    "REPOSITORY_BUSY",
                    f"checkout is synchronized for view {owner.name}; clean it first",
                )
            target = view.next["commit"] if view.next is not None else view.overlay_base
            self.git.prepare_sync_checkout(
                self.context.repository,
                view.branch,
                target,
            )
            if view.sync is not None or view.stage is not None or view.next is not None:
                self.store.discard_transition(name)
                view = self.store.get(name)
        try:
            self.store.destroy(name)
        except GitViewspaceError as error:
            if error.code != "OVERLAY_CLEANUP_FAILED" or view.kind != "overlay":
                raise
            self._normalize_stopped_work(view)
            self.store.destroy(name)
        return {
            "ok": True,
            "command": "view destroy",
            "root": str(self.context.root),
            "name": name,
            "view_id": destroyed_view_id,
            "destroyed": True,
            "idempotent": False,
        }

    def view_clean(self, name, *, report=True, prepare_checkout=True):
        view = self.store.get(name)
        transition_cleared = False
        if view.desired_started or self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before cleaning")
        if view.kind == "readonly":
            changed, idempotent = self.store.clean(name)
            return {
                "ok": True,
                "command": "view clean",
                "root": str(self.context.root),
                "view": self._view_status(changed) if report else None,
                "idempotent": idempotent,
            }
        owner = self.store.sync_owner()
        if owner is not None and owner.name != name:
            raise GitViewspaceError(
                "REPOSITORY_BUSY",
                f"checkout is synchronized for view {owner.name}; clean it first",
            )
        checkout_commit = view.next["commit"] if view.next is not None else view.overlay_base
        if prepare_checkout:
            self.git.prepare_sync_checkout(
                self.context.repository,
                view.branch,
                checkout_commit,
            )
        force_generation = (
            view.sync is not None
            or view.stage is not None
            or (
                view.next is not None
                and view.next["generation_id"] == view.generation_id
            )
        )
        if view.sync is not None or view.stage is not None:
            self.store.clear_generation_records(name)
            view = self.store.get(name)
            transition_cleared = True
        try:
            changed, idempotent = self.store.clean(name, force=force_generation)
        except GitViewspaceError as error:
            if error.code != "OVERLAY_CLEANUP_FAILED" or view.kind != "overlay":
                raise
            self._normalize_stopped_work(view)
            changed, idempotent = self.store.clean(name, force=force_generation)
        return {
            "ok": True,
            "command": "view clean",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "idempotent": idempotent and not transition_cleared,
        }

    def view_deinit_overlay(self, name, *, report=True):
        view = self.store.get(name)
        if self.mounts.observe(view, self.viewfs.source) is not None:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before writable deinitialization")
        try:
            changed, idempotent = self.store.deinit_overlay(name)
        except GitViewspaceError as error:
            if error.code != "OVERLAY_CLEANUP_FAILED" or view.kind != "overlay":
                raise
            self._normalize_stopped_work(view)
            changed, idempotent = self.store.deinit_overlay(name)
        return {
            "ok": True,
            "command": "view deinit overlay",
            "root": str(self.context.root),
            "view": self._view_status(changed) if report else None,
            "idempotent": idempotent,
        }

    def _timed_step(self, name, operation):
        started = time.perf_counter_ns()
        control = getattr(self, '_control', None)
        if control is not None:
            control.phase(name)
        try:
            result = operation()
        finally:
            duration = time.perf_counter_ns() - started
            if control is not None:
                control.phase(name, duration)
        return result, {
            "name": name,
            "duration_ns": duration,
            "idempotent": bool(result.get("idempotent", False)),
        }

    def view_init_and_start(
        self,
        name,
        kind,
        *,
        at=None,
        from_branch=None,
        new_branch=None,
    ):
        if kind not in {"readonly", "overlay"}:
            raise GitViewspaceError("INVALID_VIEW_KIND", "view kind must be readonly or overlay")
        if kind == "readonly":
            if not isinstance(at, str) or from_branch is not None or new_branch is not None:
                raise GitViewspaceError(
                    "INVALID_BRANCH",
                    "read-only initialization requires only an exact commit",
                )
            commitish = at
            branch = None
        elif (
            at is not None
            or not isinstance(from_branch, str)
            or not isinstance(new_branch, str)
        ):
            raise GitViewspaceError(
                "INVALID_BRANCH",
                "writable initialization requires parent and new branch names",
            )
        else:
            branch = new_branch

        branch_creation = None
        if kind == "overlay":
            # Runner creation is deliberately not idempotent: its branch is a new ownership
            # claim. Validate the view name and its absence before the first repository effect.
            validate_view_name(name)
            if name in self.store.views:
                raise GitViewspaceError("VIEW_EXISTS", f"view already exists: {name}")
            commitish = self.git.create_local_branch(
                self.context.repository,
                branch,
                from_branch,
            )
            branch_creation = {
                "parent_branch": from_branch,
                "parent_commit": commitish,
                "new_branch": branch,
                "new_branch_commit": commitish,
            }
        try:
            steps = []
            readonly, timing = self._timed_step(
                "init-readonly",
                lambda: self.view_init_readonly(name, commitish, report=False),
            )
            steps.append(timing)
            overlay = None
            if kind == "overlay":
                existing = self.store.get(name)
                if existing.kind == "overlay":
                    if existing.branch != branch:
                        raise GitViewspaceError(
                            "OVERLAY_EXISTS",
                            f"view already has writable state for branch {existing.branch}",
                        )
                    overlay, timing = self._timed_step(
                        "init-overlay",
                        lambda: {
                            "ok": True,
                            "command": "view init overlay",
                            "idempotent": True,
                        },
                    )
                else:
                    overlay, timing = self._timed_step(
                        "init-overlay",
                        lambda: self.view_init_overlay(name, branch, report=False),
                    )
                steps.append(timing)
            elif self.store.get(name).kind != "readonly":
                raise GitViewspaceError(
                    "OVERLAY_EXISTS",
                    "an existing writable view cannot be initialized as read-only",
                )
            started, timing = self._timed_step(
                "start",
                lambda: self.view_start(name),
            )
            steps.append(timing)
            response = {
                "ok": True,
                "command": "view init-and-start",
                "root": str(self.context.root),
                "view": started["view"],
                "steps": steps,
                "idempotent": readonly["idempotent"]
                and (overlay is None or overlay["idempotent"])
                and started["idempotent"],
            }
            if branch_creation is not None:
                response["branch_creation"] = branch_creation
            return response
        except GitViewspaceError as error:
            if branch_creation is None:
                raise
            detail = f"{error.code}: {error.message}"
            if error.detail:
                detail = f"{detail}; {error.detail}"
            raise GitViewspaceError(
                "VIEW_INIT_PARTIAL",
                "attempt branch was created but writable view initialization did not complete",
                detail=detail,
                partial_state={
                    "view_name": name,
                    "parent_branch": from_branch,
                    "branch": branch,
                    "branch_commit": commitish,
                    "view_created": name in self.store.views,
                },
            ) from error

    def view_advance(self, name, message):
        if not isinstance(message, str) or not message or "\0" in message:
            raise GitViewspaceError("INVALID_MESSAGE", "commit message must be nonempty and contain no NUL")
        initial = self.store.get(name)
        if initial.kind != "overlay":
            raise GitViewspaceError("VIEW_NOT_WRITABLE", "view advance requires a writable view")

        steps = []
        _result, timing = self._timed_step(
            "stop",
            lambda: self.view_stop(name, report=False),
        )
        steps.append(timing)
        committed_now = False
        staged_now = False
        synced_now = False
        current = self.store.get(name)
        if current.next is None:
            if current.sync is None:
                _result, timing = self._timed_step(
                    "sync",
                    lambda: self.view_sync(name, report=False),
                )
                steps.append(timing)
                synced_now = True
                current = self.store.get(name)
            if current.stage is None:
                _result, timing = self._timed_step(
                    "stage",
                    lambda: self.view_stage(
                        name,
                        report=False,
                        _fresh_sync=synced_now,
                    ),
                )
                steps.append(timing)
                staged_now = True
                current = self.store.get(name)
            _result, timing = self._timed_step(
                "commit",
                lambda: self.view_commit(
                    name,
                    message,
                    report=False,
                    verify_worktree=not staged_now,
                    _fresh_stage=staged_now,
                ),
            )
            steps.append(timing)
            committed_now = True
        _result, timing = self._timed_step(
            "clean",
            lambda: self.view_clean(
                name,
                report=False,
                prepare_checkout=not committed_now,
            ),
        )
        steps.append(timing)
        _result, timing = self._timed_step(
            "switch-next",
            lambda: self.view_switch(name, use_next=True, report=False),
        )
        steps.append(timing)
        started, timing = self._timed_step(
            "start",
            lambda: self.view_start(name),
        )
        steps.append(timing)
        return {
            "ok": True,
            "command": "view advance",
            "root": str(self.context.root),
            "view": started["view"],
            "steps": steps,
            "idempotent": False,
        }

    @staticmethod
    def _error_response(command, error):
        payload = {
            "ok": False,
            "command": command,
            "error": {"code": error.code, "message": error.message},
        }
        if error.detail:
            payload["error"]["detail"] = error.detail
        if error.data is not None:
            payload["error"]["data"] = error.data
        if error.partial_state is not None:
            payload["partial_state"] = error.partial_state
        return payload

    def _serve_connection(self, connection):
        try:
            value = validate_request(
                receive_message(
                    connection, "daemon request",
                    deadline=time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS,
                )
            )
        except (OSError, GitViewspaceError) as error:
            if isinstance(error, OSError):
                return
            response = self._error_response(None, error)
        else:
            response = self._dispatch(value)
        try:
            send_message(connection, response)
        except (GitViewspaceError, OSError):
            pass

    def _dispatch(self, value):
        """Run one validated request on the sole state-owning execution thread."""
        command = None
        try:
            command = value["command"]
            self._validate_durable_state(command, value.get("view"))
            if command in VIEW_ID_GUARDED_COMMANDS and "ensure_view_id" in value:
                self._ensure_view_identity(value["view"], value["ensure_view_id"])
            if command == "status":
                response = self.status()
            elif command == "stop":
                self._shutdown()
                response = {
                    "ok": True,
                    "command": "stop",
                    "root": str(self.context.root),
                    "stopped": True,
                    "idempotent": False,
                    "mounts_remaining": 0,
                    "children_remaining": 0,
                }
            elif command == "branch-merge":
                response = self.branch_merge(
                    value["attempt_branch"],
                    value["parent_branch"],
                    value["ensure_attempt_tip"],
                    value["message"],
                    value["checked_out_destination"],
                )
            elif command == "branch-pin":
                response = self.branch_pin(value["branch"], value["commit"])
            elif command == "submodule-prepare":
                response = self.submodule_prepare(value["at"], value["path"], value["commit"])
            elif command == "submodule-pin":
                response = self.submodule_pin(value["branch"], value["path"], value["commit"], value["message"])
            elif command == "view-init-readonly":
                response = self.view_init_readonly(value["view"], value["at"])
            elif command == "view-init-overlay":
                response = self.view_init_overlay(value["view"], value["branch"])
            elif command == "view-init-and-start":
                response = self.view_init_and_start(
                    value["view"],
                    value["kind"],
                    at=value.get("at"),
                    from_branch=value.get("from_branch"),
                    new_branch=value.get("new_branch"),
                )
            elif command == "view-routing":
                from .routing import configure
                response = configure(self, value["view"], value["action"], value["executable"], value.get("runtime_directories"))
            elif command == "view-start":
                response = self.view_start(value["view"])
            elif command == "view-sync":
                response = self.view_sync(value["view"])
            elif command == "view-stage":
                response = self.view_stage(value["view"])
            elif command == "view-commit":
                response = self.view_commit(value["view"], value["message"])
            elif command == "view-advance":
                response = self.view_advance(value["view"], value["message"])
            elif command == "view-stop":
                response = self.view_stop(value["view"])
            elif command == "view-status":
                response = self.view_status(value["view"])
            elif command == "view-switch":
                response = self.view_switch(
                    value["view"],
                    value["to"],
                    use_next=value["next"],
                )
            elif command == "view-clean":
                response = self.view_clean(value["view"])
            elif command == "view-deinit-overlay":
                response = self.view_deinit_overlay(value["view"])
            else:
                response = self.view_destroy(
                    value["view"],
                    value["discard"],
                )
        except GitViewspaceError as error:
            response = self._error_response(command, error)
            if command == "stop":
                self.stopping = False
        response.setdefault("root", str(self.context.root))
        response.setdefault("root_uuid", self.context.root_uuid)
        return response

    def serve(self):
        from .control import Control
        Control(self).run()


def _ready(fd, value):
    if fd is None:
        return
    try:
        payload = canonical_json_bytes(value)
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def run(root, *, ready_fd=None, daemon_factory=Daemon):
    daemon = None
    started = False

    def cleanup():
        if daemon is not None and started and not daemon.stopping:
            try:
                daemon._shutdown()
            except (GitViewspaceError, OSError) as error:
                print(
                    f"git-viewspace shutdown incomplete: {type(error).__name__}",
                    file=sys.stderr, flush=True,
                )
    try:
        context = RootContext.load(root)
        with contextlib.ExitStack() as stack:
            stack.enter_context(exclusive_lock(context.root_lock))
            stack.enter_context(exclusive_lock(context.repository_lock))
            daemon = daemon_factory(context)
            daemon.reconcile()

            def stop_signal(_number, _frame):
                daemon.signal_stop = True

            signal.signal(signal.SIGTERM, stop_signal)
            signal.signal(signal.SIGINT, stop_signal)
            runtime = daemon.start()
            started = True
            # ExitStack callbacks are LIFO: cleanup completes before either lock releases.
            stack.callback(cleanup)
            _ready(ready_fd, {"ok": True, "pid": os.getpid(), "runtime": runtime})
            ready_fd = None
            daemon.serve()
        return 0
    except GitViewspaceError as error:
        print(f'git-viewspace daemon failed: {error.code}', file=sys.stderr, flush=True)
        _ready(
            ready_fd,
            {
                "ok": False,
                "error": {"code": error.code, "message": error.message, "detail": error.detail},
            },
        )
        return 1
    except BaseException as error:
        print(f'git-viewspace daemon failed: {type(error).__name__}', file=sys.stderr, flush=True)
        _ready(
            ready_fd,
            {
                "ok": False,
                "error": {"code": "DAEMON_FAILED", "message": "daemon failed", "detail": str(error)},
            },
        )
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m git_viewspace.daemon")
    parser.add_argument("--root", required=True)
    parser.add_argument("--ready-fd", type=int)
    arguments = parser.parse_args(argv)
    raise SystemExit(run(arguments.root, ready_fd=arguments.ready_fd))


if __name__ == "__main__":
    main()
