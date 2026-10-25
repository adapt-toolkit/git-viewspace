"""Durable view definitions, overlay generations, and public mounts."""

import errno
import os
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import GitViewspaceError
from .host import mount_at
from .schemas import (
    GENERATION_FORMAT,
    REPOSITORY_OPERATION_FORMAT,
    SCHEMA_VERSION,
    STATE_FORMAT,
    VIEW_CONFIG_FORMAT,
    VIEW_STATE_FORMAT,
    validate_generation,
    validate_view_config,
    validate_view_name,
    validate_view_state,
)
from .storage import atomic_json, fsync_directory, read_json, validate_stable_file


UNMOUNT_RETRY_INTERVAL_SECONDS = 5.0
UNMOUNT_RETRY_LIMIT = 6


@dataclass(frozen=True)
class View:
    name: str
    view_id: str
    commit: str
    alias: str
    desired_started: bool
    branch: str | None
    overlay_base: str | None
    generation_id: str | None
    sync: dict | None
    stage: dict | None
    next: dict | None
    path: Path
    config_path: Path
    state_path: Path
    mountpoint: Path
    routing: dict | None = None

    @property
    def kind(self):
        return "overlay" if self.branch is not None else "readonly"

    @property
    def overlay_path(self):
        return self.path / "overlay"

    @property
    def generation_path(self):
        if self.generation_id is None:
            return None
        return self.overlay_path / "generations" / self.generation_id

    @property
    def upper_path(self):
        return None if self.generation_path is None else self.generation_path / "upper"

    @property
    def work_path(self):
        return None if self.generation_path is None else self.generation_path / "work"


def _directory(path, uid, gid, modes):
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise GitViewspaceError("INVALID_STATE", f"managed view directory is missing: {path}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) not in modes
    ):
        raise GitViewspaceError("INVALID_STATE", f"managed view directory has invalid identity: {path}")


def _new_directory(path, uid, gid, mode=0o700):
    path.mkdir(mode=mode)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def viewfs_config(views):
    return {
        view.alias: {"commit": view.commit}
        for view in sorted(views.values(), key=lambda item: item.alias)
    }


def load_views(root, root_uuid, manager_state, uid, gid):
    views_root = root / "views"
    registry = manager_state["views"]
    actual_names = {entry.name for entry in views_root.iterdir()}
    if actual_names != set(registry):
        raise GitViewspaceError("INVALID_STATE", "view registry and managed view directories differ")

    result = {}
    aliases = set()
    view_ids = set()
    branches = set()
    for name, reference in registry.items():
        validate_view_name(name)
        view_path = views_root / name
        config_directory = view_path / "config"
        state_directory = view_path / "state"
        mountpoint = view_path / "mnt"
        _directory(view_path, uid, gid, {0o700})
        view_entries = {entry.name for entry in view_path.iterdir()}
        if view_entries - {"routing"} not in ({"config", "state", "mnt"}, {"config", "state", "mnt", "overlay"}):
            raise GitViewspaceError("INVALID_STATE", f"view contains an unexplained entry: {name}")
        if "routing" in view_entries:
            _directory(view_path / "routing", uid, gid, {0o700})
        _directory(config_directory, uid, gid, {0o700})
        _directory(state_directory, uid, gid, {0o700})
        mounted = mount_at(mountpoint)
        if {entry.name for entry in config_directory.iterdir()} != {"view.json"}:
            raise GitViewspaceError("INVALID_STATE", f"view configuration directory is invalid: {name}")
        if {entry.name for entry in state_directory.iterdir()} != {"state.json"}:
            raise GitViewspaceError("INVALID_STATE", f"view state directory is invalid: {name}")
        config_path = config_directory / "view.json"
        state_path = state_directory / "state.json"
        validate_stable_file(config_path, uid, gid)
        validate_stable_file(state_path, uid, gid)
        config = validate_view_config(read_json(config_path))
        state = validate_view_state(read_json(state_path))
        if (
            config["root_uuid"] != root_uuid
            or state["root_uuid"] != root_uuid
            or config["name"] != name
            or config["view_id"] != reference["view_id"]
            or state["view_id"] != config["view_id"]
        ):
            raise GitViewspaceError("INVALID_STATE", f"view identity is inconsistent: {name}")
        try:
            _directory(mountpoint, uid, gid, {0o755} if mounted is not None else {0o700})
        except OSError as error:
            # A dead router cannot answer lstat. Defer only this known owned
            # mount to reconciliation; it validates the runtime record and
            # unmounts before the underlying directory is checked again.
            if not (
                error.errno == errno.ENOTCONN
                and config.get("routing") is not None
                and mounted is not None
                and mounted.filesystem_type == "fuse.git-viewspace-router"
                and mounted.source == "gvr-" + config["view_id"]
                and mounted.root == "/"
            ):
                raise
        writable = config["kind"] == "overlay"
        if writable != ("overlay_base" in state) or writable != ("overlay" in view_entries):
            raise GitViewspaceError("INVALID_STATE", f"view writable state is inconsistent: {name}")
        branch = config.get("branch")
        overlay_base = state.get("overlay_base")
        generation_id = state.get("active_generation")
        sync = state.get("sync")
        stage = state.get("stage")
        next_transition = state.get("next")
        if writable:
            if branch in branches:
                raise GitViewspaceError("INVALID_STATE", f"branch is owned by multiple views: {branch}")
            branches.add(branch)
            if overlay_base != config["commit"]:
                raise GitViewspaceError("INVALID_STATE", f"overlay base differs from pinned commit: {name}")
            overlay = view_path / "overlay"
            generations = overlay / "generations"
            _directory(overlay, uid, gid, {0o700})
            _directory(generations, uid, gid, {0o700})
            if {entry.name for entry in overlay.iterdir()} != {"generations"}:
                raise GitViewspaceError("INVALID_STATE", f"overlay directory is invalid: {name}")
            if {entry.name for entry in generations.iterdir()} != {generation_id}:
                raise GitViewspaceError("INVALID_STATE", f"overlay generation set is invalid: {name}")
            generation = generations / generation_id
            _directory(generation, uid, gid, {0o700})
            if {entry.name for entry in generation.iterdir()} != {"generation.json", "upper", "work"}:
                raise GitViewspaceError("INVALID_STATE", f"overlay generation is invalid: {name}")
            upper = generation / "upper"
            work = generation / "work"
            _directory(upper, uid, gid, {0o755})
            _directory(work, uid, gid, {0o755})
            marker_path = generation / "generation.json"
            validate_stable_file(marker_path, uid, gid)
            marker = validate_generation(read_json(marker_path))
            if marker != {
                "format": GENERATION_FORMAT,
                "schema_version": SCHEMA_VERSION,
                "root_uuid": root_uuid,
                "view_id": config["view_id"],
                "generation_id": generation_id,
                "base_commit": overlay_base,
            }:
                raise GitViewspaceError("INVALID_STATE", f"overlay generation marker is inconsistent: {name}")
            if sync is not None and (
                sync["root_uuid"] != root_uuid
                or sync["view_id"] != config["view_id"]
                or sync["generation_id"] != generation_id
                or sync["branch"] != branch
                or sync["base_commit"] != overlay_base
            ):
                raise GitViewspaceError("INVALID_STATE", f"view sync identity is inconsistent: {name}")
            if stage is not None and (
                stage["root_uuid"] != root_uuid
                or stage["view_id"] != config["view_id"]
                or stage["generation_id"] != generation_id
                or stage["branch"] != branch
                or stage["base_commit"] != overlay_base
                or stage["sync_id"] != sync["sync_id"]
            ):
                raise GitViewspaceError("INVALID_STATE", f"view stage identity is inconsistent: {name}")
            if next_transition is not None and (
                next_transition["root_uuid"] != root_uuid
                or next_transition["view_id"] != config["view_id"]
                or next_transition["branch"] != branch
                or next_transition["base_commit"] != overlay_base
                or (stage is not None and next_transition["stage_id"] != stage["stage_id"])
            ):
                raise GitViewspaceError("INVALID_STATE", f"view next identity is inconsistent: {name}")
        elif sync is not None or stage is not None or next_transition is not None:
            raise GitViewspaceError("INVALID_STATE", f"read-only view has repository transition state: {name}")
        if config["alias"] in aliases or config["view_id"] in view_ids:
            raise GitViewspaceError("INVALID_STATE", "view aliases and IDs must be unique")
        aliases.add(config["alias"])
        view_ids.add(config["view_id"])
        result[name] = View(
            name=name,
            view_id=config["view_id"],
            commit=config["commit"],
            alias=config["alias"],
            desired_started=state["desired_started"],
            branch=branch,
            overlay_base=overlay_base,
            generation_id=generation_id,
            sync=sync,
            stage=stage,
            next=next_transition,
            path=view_path,
            config_path=config_path,
            state_path=state_path,
            mountpoint=mountpoint,
            routing=config.get("routing"),
        )
    transitioning = [
        view
        for view in result.values()
        if view.sync is not None or view.stage is not None or view.next is not None
    ]
    operation = manager_state["repository_operation"]
    if operation is None:
        if transitioning:
            raise GitViewspaceError("INVALID_STATE", "view transition exists without repository operation")
    else:
        if operation["root_uuid"] != root_uuid or len(transitioning) != 1:
            raise GitViewspaceError("INVALID_STATE", "repository operation is inconsistent")
        view = transitioning[0]
        phase = "next" if view.next is not None else "stage" if view.stage is not None else "sync"
        record = view.next or view.stage or view.sync
        if (
            operation["view_id"] != view.view_id
            or operation["operation"] != phase
            or operation["sync_id"] != record["sync_id"]
        ):
            raise GitViewspaceError("INVALID_STATE", "repository operation does not match view transition")
    return result


class ViewStore:
    """The daemon-owned view registry."""

    def __init__(self, context):
        self.context = context
        self.views = dict(context.views)
        self.repository_operation = context.manager_state["repository_operation"]

    def _manager_state(self):
        return {
            "format": STATE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "views": {
                name: {"view_id": view.view_id}
                for name, view in sorted(self.views.items())
            },
            "repository_operation": self.repository_operation,
        }

    def publish_config(self):
        atomic_json(self.context.config_path, viewfs_config(self.views))

    def _publish_registry(self):
        atomic_json(self.context.state_path, self._manager_state())

    @staticmethod
    def _config(view, root_uuid):
        value = {
            "format": VIEW_CONFIG_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": root_uuid,
            "view_id": view.view_id,
            "name": view.name,
            "kind": view.kind,
            "commit": view.commit,
            "alias": view.alias,
        }
        if view.routing is not None:
            value["routing"] = view.routing
        if view.branch is not None:
            value["branch"] = view.branch
        return value

    @staticmethod
    def _state(view, root_uuid):
        value = {
            "format": VIEW_STATE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": root_uuid,
            "view_id": view.view_id,
            "desired_started": view.desired_started,
        }
        if view.overlay_base is not None:
            value["overlay_base"] = view.overlay_base
            value["active_generation"] = view.generation_id
        if view.sync is not None:
            value["sync"] = view.sync
        if view.stage is not None:
            value["stage"] = view.stage
        if view.next is not None:
            value["next"] = view.next
        return value

    def get(self, name):
        validate_view_name(name)
        try:
            return self.views[name]
        except KeyError as error:
            raise GitViewspaceError("VIEW_NOT_FOUND", f"view does not exist: {name}") from error

    def branch_owner(self, branch):
        return next(
            (view for view in self.views.values() if view.branch == branch),
            None,
        )

    def sync_owner(self):
        if self.repository_operation is None:
            return None
        return next(
            (
                view
                for view in self.views.values()
                if view.view_id == self.repository_operation["view_id"]
            ),
            None,
        )

    def record_sync(self, name, record):
        view = self.get(name)
        owner = self.sync_owner()
        if owner is not None and owner.name != name:
            raise GitViewspaceError(
                "REPOSITORY_BUSY",
                f"checkout is synchronized for view {owner.name}; clean it first",
            )
        changed = replace(view, sync=record, stage=None, next=None)
        operation = {
            "format": REPOSITORY_OPERATION_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "operation": "sync",
            "view_id": changed.view_id,
            "sync_id": record["sync_id"],
        }
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        self.repository_operation = operation
        self._publish_registry()
        return changed

    def _record_phase(self, name, field, record, operation):
        view = self.get(name)
        owner = self.sync_owner()
        if owner is None or owner.name != name or view.sync is None:
            raise GitViewspaceError("INVALID_STATE", "view does not own the repository transition")
        changed = replace(view, **{field: record})
        repository_operation = {
            "format": REPOSITORY_OPERATION_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": self.context.root_uuid,
            "operation": operation,
            "view_id": changed.view_id,
            "sync_id": record["sync_id"],
        }
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        self.repository_operation = repository_operation
        self._publish_registry()
        return changed

    def record_stage(self, name, record):
        return self._record_phase(name, "stage", record, "stage")

    def record_next(self, name, record):
        return self._record_phase(name, "next", record, "next")

    def clear_sync(self, name):
        view = self.get(name)
        if view.sync is None and view.stage is None:
            return view, True
        if view.next is not None:
            raise GitViewspaceError("NEXT_EXISTS", "committed transition must be preserved during cleanup")
        owner = self.sync_owner()
        if owner is None or owner.name != name:
            raise GitViewspaceError("INVALID_STATE", "view sync does not own repository operation")
        changed = replace(view, sync=None, stage=None)
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        self.repository_operation = None
        self._publish_registry()
        return changed, False

    def clear_generation_records(self, name):
        view = self.get(name)
        if view.sync is None and view.stage is None:
            return view, True
        owner = self.sync_owner()
        if owner is None or owner.name != name:
            raise GitViewspaceError("INVALID_STATE", "view does not own repository operation")
        changed = replace(view, sync=None, stage=None)
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        if changed.next is None:
            self.repository_operation = None
        else:
            self.repository_operation = {
                "format": REPOSITORY_OPERATION_FORMAT,
                "schema_version": SCHEMA_VERSION,
                "root_uuid": self.context.root_uuid,
                "operation": "next",
                "view_id": changed.view_id,
                "sync_id": changed.next["sync_id"],
            }
        self._publish_registry()
        return changed, False

    def discard_transition(self, name):
        view = self.get(name)
        if view.sync is None and view.stage is None and view.next is None:
            return view, True
        owner = self.sync_owner()
        if owner is None or owner.name != name:
            raise GitViewspaceError("INVALID_STATE", "view does not own repository operation")
        changed = replace(view, sync=None, stage=None, next=None)
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        self.repository_operation = None
        self._publish_registry()
        return changed, False

    def _create_generation(self, view, base_commit, *, create_overlay=False):
        overlay = view.overlay_path
        generations = overlay / "generations"
        if create_overlay:
            _new_directory(overlay, self.context.uid, self.context.gid)
            _new_directory(generations, self.context.uid, self.context.gid)
        generation_id = uuid.uuid4().hex
        generation = generations / generation_id
        _new_directory(generation, self.context.uid, self.context.gid)
        try:
            _new_directory(generation / "upper", self.context.uid, self.context.gid, 0o755)
            _new_directory(generation / "work", self.context.uid, self.context.gid, 0o755)
            marker = {
                "format": GENERATION_FORMAT,
                "schema_version": SCHEMA_VERSION,
                "root_uuid": self.context.root_uuid,
                "view_id": view.view_id,
                "generation_id": generation_id,
                "base_commit": base_commit,
            }
            atomic_json(generation / "generation.json", marker)
            fsync_directory(generations)
            return generation_id
        except BaseException:
            shutil.rmtree(generation, ignore_errors=True)
            if create_overlay:
                shutil.rmtree(overlay, ignore_errors=True)
            raise

    @staticmethod
    def _remove_generation(generation):
        generation = Path(generation)
        work = generation / "work"
        private_work = work / "work"
        try:
            if private_work.exists() or private_work.is_symlink():
                os.rmdir(private_work)
            shutil.rmtree(generation)
            fsync_directory(generation.parent)
        except OSError as error:
            raise GitViewspaceError(
                "OVERLAY_CLEANUP_FAILED",
                "could not retire stopped OverlayFS generation",
                detail=str(error),
            ) from error

    def _remove_overlay(self, view):
        if view.kind != "overlay":
            return
        self._remove_generation(view.generation_path)
        try:
            os.rmdir(view.overlay_path / "generations")
            os.rmdir(view.overlay_path)
            fsync_directory(view.path)
        except OSError as error:
            raise GitViewspaceError(
                "OVERLAY_CLEANUP_FAILED",
                "could not remove retired OverlayFS storage",
                detail=str(error),
            ) from error

    @staticmethod
    def dirty(view):
        if view.kind != "overlay":
            return False
        try:
            with os.scandir(view.upper_path) as entries:
                if next(entries, None) is not None:
                    return True
            return bool(os.listxattr(view.upper_path, follow_symlinks=False))
        except OSError as error:
            raise GitViewspaceError(
                "OVERLAY_INSPECTION_FAILED",
                f"cannot inspect stopped upper directory: {view.name}",
                detail=str(error),
            ) from error

    def init_overlay(self, name, branch):
        view = self.get(name)
        if view.kind == "overlay":
            if view.branch != branch:
                raise GitViewspaceError("OVERLAY_EXISTS", f"view already owns another branch: {name}")
            return view, True
        if view.desired_started:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before writable initialization")
        owner = self.branch_owner(branch)
        if owner is not None:
            raise GitViewspaceError("BRANCH_CLAIMED", f"branch is already owned by view {owner.name}")
        generation_id = self._create_generation(view, view.commit, create_overlay=True)
        changed = replace(
            view,
            branch=branch,
            overlay_base=view.commit,
            generation_id=generation_id,
            sync=None,
            stage=None,
            next=None,
        )
        try:
            atomic_json(changed.config_path, self._config(changed, self.context.root_uuid))
            atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
            self.views[name] = changed
            return changed, False
        except BaseException:
            shutil.rmtree(view.overlay_path, ignore_errors=True)
            atomic_json(view.config_path, self._config(view, self.context.root_uuid))
            atomic_json(view.state_path, self._state(view, self.context.root_uuid))
            raise

    def clean(self, name, *, force=False):
        view = self.get(name)
        if view.kind == "readonly":
            return view, True
        if view.desired_started:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before cleaning")
        if view.sync is not None or view.stage is not None:
            raise GitViewspaceError("TRANSITION_EXISTS", "view sync and stage must be cleared before generation cleanup")
        if not force and not self.dirty(view):
            return view, True
        old_generation = view.generation_path
        generation_id = self._create_generation(view, view.commit)
        changed = replace(view, generation_id=generation_id)
        try:
            self._remove_generation(old_generation)
        except BaseException:
            if changed.generation_path.exists():
                self._remove_generation(changed.generation_path)
            raise
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        return changed, False

    def deinit_overlay(self, name):
        view = self.get(name)
        if view.kind == "readonly":
            return view, True
        if view.desired_started:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before writable deinitialization")
        if view.sync is not None or view.stage is not None or view.next is not None:
            raise GitViewspaceError("TRANSITION_EXISTS", "view transition must be cleared before writable deinitialization")
        if self.dirty(view):
            raise GitViewspaceError("VIEW_DIRTY", "view must be clean before writable deinitialization")
        changed = replace(
            view,
            branch=None,
            overlay_base=None,
            generation_id=None,
            sync=None,
            stage=None,
            next=None,
        )
        self._remove_overlay(view)
        atomic_json(changed.config_path, self._config(changed, self.context.root_uuid))
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        return changed, False

    def initialize(self, name, commit, probe):
        validate_view_name(name)
        existing = self.views.get(name)
        if existing is not None:
            if existing.commit != commit:
                raise GitViewspaceError("VIEW_EXISTS", f"view already exists with another commit: {name}")
            return existing, True

        view_id = uuid.uuid4().hex
        alias = f"v-{uuid.uuid4().hex}"
        final = self.context.root / "views" / name
        temporary = self.context.root / "views" / f".new-{view_id}"
        if final.exists() or final.is_symlink() or temporary.exists() or temporary.is_symlink():
            raise GitViewspaceError("INVALID_STATE", "view path already exists")
        _new_directory(temporary, self.context.uid, self.context.gid)
        try:
            for child in ("config", "state", "mnt"):
                _new_directory(temporary / child, self.context.uid, self.context.gid)
            view = View(
                name=name,
                view_id=view_id,
                commit=commit,
                alias=alias,
                desired_started=False,
                branch=None,
                overlay_base=None,
                generation_id=None,
                sync=None,
                stage=None,
                next=None,
                path=final,
                config_path=final / "config/view.json",
                state_path=final / "state/state.json",
                mountpoint=final / "mnt",
            )
            atomic_json(temporary / "config/view.json", self._config(view, self.context.root_uuid))
            atomic_json(temporary / "state/state.json", self._state(view, self.context.root_uuid))
            os.rename(temporary, final)
            fsync_directory(final.parent)
            self.views[name] = view
            try:
                self._publish_registry()
                self.publish_config()
                probe(view)
            except BaseException:
                self.views.pop(name, None)
                self._publish_registry()
                self.publish_config()
                shutil.rmtree(final, ignore_errors=True)
                fsync_directory(final.parent)
                raise
            return view, False
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    def set_desired(self, name, desired):
        view = self.get(name)
        if desired and (view.sync is not None or view.stage is not None or view.next is not None):
            code = "SYNC_EXISTS" if view.stage is None and view.next is None else "TRANSITION_EXISTS"
            raise GitViewspaceError(code, "view transition must be cleaned and switched before start")
        if view.desired_started == desired:
            return view, True
        changed = replace(view, desired_started=desired)
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        return changed, False

    def switch(self, name, commit, probe, *, consume_next=False):
        view = self.get(name)
        if view.desired_started:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before switching")
        if view.sync is not None or view.stage is not None:
            raise GitViewspaceError("TRANSITION_EXISTS", "view sync and stage must be cleared before switching")
        if consume_next:
            if view.next is None or view.next["commit"] != commit:
                raise GitViewspaceError("NEXT_NOT_FOUND", "view has no matching committed next transition")
        elif view.next is not None:
            raise GitViewspaceError("NEXT_EXISTS", "consume the committed next transition first")
        if view.commit == commit and not consume_next:
            return view, True
        old_generation = view.generation_path
        generation_id = None
        if view.kind == "overlay":
            generation_id = self._create_generation(view, commit)
        changed = replace(
            view,
            commit=commit,
            alias=f"v-{uuid.uuid4().hex}",
            overlay_base=commit if view.kind == "overlay" else None,
            generation_id=generation_id,
            sync=None,
            stage=None,
            next=None,
        )
        atomic_json(changed.config_path, self._config(changed, self.context.root_uuid))
        atomic_json(changed.state_path, self._state(changed, self.context.root_uuid))
        self.views[name] = changed
        try:
            self.publish_config()
            probe(changed)
        except BaseException:
            self.views[name] = view
            atomic_json(view.config_path, self._config(view, self.context.root_uuid))
            atomic_json(view.state_path, self._state(view, self.context.root_uuid))
            self.publish_config()
            if generation_id is not None:
                shutil.rmtree(changed.generation_path, ignore_errors=True)
            raise
        if old_generation is not None:
            self._remove_generation(old_generation)
        if consume_next:
            self.repository_operation = None
            self._publish_registry()
        return changed, False

    def destroy(self, name):
        view = self.get(name)
        if view.desired_started:
            raise GitViewspaceError("VIEW_STARTED", "view must be stopped before destruction")
        if view.sync is not None or view.stage is not None or view.next is not None:
            raise GitViewspaceError("TRANSITION_EXISTS", "view transition must be cleared before destruction")
        if view.kind == "overlay":
            self._remove_overlay(view)
        self.views.pop(name)
        try:
            self._publish_registry()
            self.publish_config()
            shutil.rmtree(view.path)
            fsync_directory(view.path.parent)
        except BaseException:
            self.views[name] = view
            raise
        return view


class ViewMounts:
    """Create and remove public read-only and writable view mounts."""

    def __init__(
        self,
        context,
        *,
        sudo=None,
        mount=None,
        umount=None,
        run=subprocess.run,
        timeout=12.0,
    ):
        self.context = context
        self.sudo = str(Path(sudo or shutil.which("sudo") or "/usr/bin/sudo").resolve())
        self.mount = str(Path(mount or shutil.which("mount") or "/usr/bin/mount").resolve())
        self.umount = str(Path(umount or shutil.which("umount") or "/usr/bin/umount").resolve())
        self._run = run
        self.timeout = timeout

    def _command(self, arguments, code, message):
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
            raise GitViewspaceError(code, message, detail=str(error)) from error
        if result.returncode != 0:
            raise GitViewspaceError(
                code,
                message,
                detail=result.stderr.decode("utf-8", "replace").strip(),
            )

    @staticmethod
    def _expected_root(view):
        return f"/views/{view.alias}"

    def observe(self, view, source, observed=None):
        if view.routing is not None:
            from .routing import observe
            return observe(self, view, source, observed)
        mounted = (
            mount_at(view.mountpoint)
            if observed is None
            else mount_at(view.mountpoint, observed)
        )
        if mounted is None:
            return None
        if view.kind == "readonly":
            valid = (
                mounted.filesystem_type.startswith("fuse")
                and mounted.source == source
                and mounted.root == self._expected_root(view)
                and {"ro", "nosuid", "nodev"}.issubset(mounted.mount_options)
            )
        else:
            expected = {
                f"lowerdir={self.context.mountpoint / 'views' / view.alias}",
                f"upperdir={view.upper_path}",
                f"workdir={view.work_path}",
                "uuid=null",
            }
            valid = (
                mounted.filesystem_type == "overlay"
                and mounted.source == "overlay"
                and mounted.root == "/"
                and "rw" in mounted.mount_options
                and expected.issubset(mounted.super_options)
                # Linux may omit redirect_dir=on when it is the default.
                # start() requests it; match mount identity, not a verbatim echo.
                and not any(
                    option.startswith("redirect_dir=") and option != "redirect_dir=on"
                    for option in mounted.super_options
                )
                and "volatile" not in mounted.super_options
                and "userxattr" not in mounted.super_options
            )
        if not valid:
            raise GitViewspaceError(
                "FOREIGN_MOUNT",
                f"unexpected public mount at {view.mountpoint}",
                detail=f"type={mounted.filesystem_type} source={mounted.source} root={mounted.root}",
            )
        return mounted

    def _source(self, view):
        source = self.context.mountpoint / "views" / view.alias
        try:
            metadata = source.lstat()
        except FileNotFoundError as error:
            raise GitViewspaceError(
                "VIEWFS_ALIAS_UNAVAILABLE",
                f"git-viewspace-fs alias is unavailable: {view.name}",
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.context.uid
            or metadata.st_gid != self.context.gid
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise GitViewspaceError("VIEWFS_ALIAS_UNAVAILABLE", f"git-viewspace-fs alias is unavailable: {view.name}")
        return source

    def probe(self, view):
        self._source(view)

    def start(self, view, source):
        if view.routing is not None:
            from .routing import start
            return start(self, view, source)
        if self.observe(view, source) is not None:
            return True
        from .submodules import require_available
        modules = require_available(self.context.repository, view.commit)
        _directory(view.mountpoint, self.context.uid, self.context.gid, {0o700})
        alias_path = self._source(view)
        if view.kind == "readonly":
            self._command(
                (self.sudo, "-n", "--", self.mount, "--bind", "--", str(alias_path), str(view.mountpoint)),
                "VIEW_MOUNT_FAILED",
                f"could not bind-mount read-only view: {view.name}",
            )
        else:
            _directory(view.upper_path, self.context.uid, self.context.gid, {0o755})
            _directory(view.work_path, self.context.uid, self.context.gid, {0o755})
            if view.upper_path.stat().st_dev != view.work_path.stat().st_dev:
                raise GitViewspaceError("INVALID_OVERLAY", "upper and work must share one filesystem")
            paths = (alias_path, view.upper_path, view.work_path, view.mountpoint)
            if any("," in str(path) or ":" in str(path) for path in paths):
                raise GitViewspaceError("INVALID_OVERLAY", "OverlayFS paths must not contain comma or colon")
            # libfuse2 lower handles can be reused after daemon restart. With
            # indexing/metacopy/export disabled, origins are optional identity
            # metadata; preserve the upper contents and redirects, not old handles.
            self._command(
                (self.sudo, "-n", "--", "/usr/bin/python3", "-I",
                 str(Path(__file__).with_name("overlay_origin.py").resolve()),
                 str(view.upper_path)),
                "OVERLAY_ORIGIN_RESET_FAILED", "cannot reset stopped overlay origins",
            )
            options = ",".join(
                (
                    f"lowerdir={alias_path}",
                    f"upperdir={view.upper_path}",
                    f"workdir={view.work_path}",
                    "redirect_dir=on",
                    "metacopy=off",
                    "index=off",
                    "nfs_export=off",
                    "xino=off",
                    "uuid=null",
                    "verity=off",
                )
            )
            self._command(
                (
                    self.sudo,
                    "-n",
                    "--",
                    self.mount,
                    "-t",
                    "overlay",
                    "overlay",
                    "-o",
                    options,
                    "--",
                    str(view.mountpoint),
                ),
                "VIEW_MOUNT_FAILED",
                f"could not mount writable view: {view.name}",
            )
        try:
            if view.kind == "readonly":
                self._command(
                    (
                        self.sudo,
                        "-n",
                        "--",
                        self.mount,
                        "-o",
                        "remount,bind,ro,nosuid,nodev",
                        "--",
                        str(view.mountpoint),
                    ),
                    "VIEW_MOUNT_FAILED",
                    f"could not make view mount read-only: {view.name}",
                )
            if view.kind == "overlay":
                for path in modules:
                    target = view.mountpoint / path
                    self._command(
                        (self.sudo, "-n", "--", self.mount, "--bind", "--",
                         str(alias_path / path), str(target)),
                        "VIEW_MOUNT_FAILED", "could not mount pinned code module",
                    )
                    self._command(
                        (self.sudo, "-n", "--", self.mount, "-o", "remount,bind,ro,nosuid,nodev",
                         "--", str(target)),
                        "VIEW_MOUNT_FAILED", "could not protect pinned code module",
                    )
            self.observe(view, source)
        except BaseException:
            try:
                self._stop_modules(view, source, force=True)
                self._unmount(view.mountpoint, lazy=True)
            except GitViewspaceError:
                pass
            raise
        return False

    def _unmount(self, mountpoint, *, lazy=False, retry=False):
        arguments = [self.sudo, "-n", "--", self.umount]
        if lazy:
            arguments.append("-l")
        arguments.extend(("--", str(mountpoint)))
        retries = UNMOUNT_RETRY_LIMIT if retry and not lazy else 0
        for retry in range(retries + 1):
            try:
                self._command(
                    arguments,
                    "VIEW_UNMOUNT_FAILED",
                    f"could not unmount view: {mountpoint}",
                )
                return
            except GitViewspaceError:
                if mount_at(mountpoint) is None:
                    return
                if retry == retries:
                    raise
                time.sleep(UNMOUNT_RETRY_INTERVAL_SECONDS)

    def stop(self, view, source, *, force=False):
        if view.routing is not None:
            from .routing import stop
            return stop(self, view, source, force=force)
        if self.observe(view, source) is None:
            return True
        self._stop_modules(view, source, force=force)
        try:
            self._unmount(view.mountpoint, retry=not force)
        except GitViewspaceError:
            if not force:
                raise
            self._unmount(view.mountpoint, lazy=True)
        if mount_at(view.mountpoint) is not None:
            raise GitViewspaceError("VIEW_UNMOUNT_FAILED", f"view mount remains: {view.name}")
        _directory(view.mountpoint, self.context.uid, self.context.gid, {0o700})
        return False

    def _stop_modules(self, view, source, *, force=False):
        if view.kind != "overlay":
            return
        from .submodules import entries
        for path in reversed(sorted(entries(self.context.repository, view.commit))):
            target = view.mountpoint / path
            mounted = mount_at(target)
            if mounted is None:
                continue
            if mounted.source != source or mounted.root != self._expected_root(view) + "/" + path:
                raise GitViewspaceError("FOREIGN_MOUNT", "unexpected mount over code module")
            self._unmount(target, lazy=force, retry=not force)

    def cleanup_all(self, views, source, *, force):
        for view in sorted(views.values(), key=lambda item: item.name, reverse=True):
            self.stop(view, source, force=force)


# Compatibility name retained for Stage 3 callers/tests.
ReadonlyMounts = ViewMounts
