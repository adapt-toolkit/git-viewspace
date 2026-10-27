"""Managed-root validation shared by commands and the daemon."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import GitViewspaceError
from .git import Git, RepositoryFacts
from .host import (
    mount_at,
    process_matches,
    validate_control_socket_location,
    validate_socket_path,
)
from .schemas import (
    validate_config,
    validate_owner,
    validate_runtime,
    validate_state,
    validate_view_config,
    validate_view_name,
    validate_view_state,
)
from .storage import read_json, validate_stable_file
from .views import load_views, viewfs_config


def _caller_and_root(root):
    """Validate the cheap, immutable identity boundary around a managed root."""

    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        raise GitViewspaceError("ROOT_CALLER_FORBIDDEN", "git-viewspace must run as a non-root user")
    if uid != os.geteuid() or gid != os.getegid():
        raise GitViewspaceError("SETID_CALLER_FORBIDDEN", "real and effective UID/GID must match")
    root = Path(root)
    if not root.is_absolute() or Path(os.path.abspath(root)) != Path(os.path.realpath(root)):
        raise GitViewspaceError("INVALID_ROOT", "managed root must be an absolute symlink-free path")
    try:
        metadata = root.lstat()
    except FileNotFoundError as error:
        raise GitViewspaceError("ROOT_NOT_INITIALIZED", "managed root is not initialized") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GitViewspaceError("INVALID_ROOT", "managed root has invalid identity or mode")
    return root, uid, gid


@dataclass(frozen=True)
class OnlineRoot:
    """Constant-cost client view of a root already owned by its daemon.

    This intentionally validates durable state and runtime identity without
    re-qualifying the repository or walking the managed checkout.  The daemon
    performs the matching in-memory/durable-state check before each request.
    """

    root: Path
    root_uuid: str
    uid: int
    gid: int
    socket_path: Path
    runtime_path: Path
    manager_state: dict

    @classmethod
    def load(cls, root, *, view=None, allow_missing_view=False, require_runtime=True,
             allow_unready=False):
        root, uid, gid = _caller_and_root(root)
        config_path = root / "config/manager.json"
        state_path = root / "state/manager.json"
        validate_stable_file(config_path, uid, gid)
        validate_stable_file(state_path, uid, gid)
        config = validate_config(read_json(config_path))
        manager_state = validate_state(read_json(state_path))
        if (
            config["root_path"] != str(root)
            or config["root_uuid"] != manager_state["root_uuid"]
            or config["owner"] != {"uid": uid, "gid": gid}
        ):
            raise GitViewspaceError("INVALID_STATE", "managed-root identity is inconsistent")

        if view is not None:
            validate_view_name(view)
            reference = manager_state["views"].get(view)
            if reference is None:
                if not allow_missing_view:
                    raise GitViewspaceError("VIEW_NOT_FOUND", f"view does not exist: {view}")
            else:
                config_file = root / "views" / view / "config/view.json"
                state_file = root / "views" / view / "state/state.json"
                try:
                    validate_stable_file(config_file, uid, gid)
                    validate_stable_file(state_file, uid, gid)
                    view_config = validate_view_config(read_json(config_file))
                    view_state = validate_view_state(read_json(state_file))
                except FileNotFoundError as error:
                    raise GitViewspaceError(
                        "INVALID_STATE", f"view durable state is incomplete: {view}"
                    ) from error
                if (
                    view_config["root_uuid"] != config["root_uuid"]
                    or view_state["root_uuid"] != config["root_uuid"]
                    or view_config["name"] != view
                    or view_config["view_id"] != reference["view_id"]
                    or view_state["view_id"] != reference["view_id"]
                ):
                    raise GitViewspaceError("INVALID_STATE", f"view identity is inconsistent: {view}")

        socket_path = validate_control_socket_location(root)
        runtime_path = root / "run/runtime.json"
        if require_runtime:
            try:
                validate_stable_file(runtime_path, uid, gid)
                runtime = validate_runtime(read_json(runtime_path))
            except FileNotFoundError as error:
                raise GitViewspaceError("DAEMON_UNAVAILABLE", "git-viewspace daemon is unavailable") from error
            if runtime["root_uuid"] != config["root_uuid"] or (
                not runtime["ready"] and not allow_unready
            ):
                raise GitViewspaceError("INVALID_RUNTIME", "daemon runtime does not match the managed root")
            expected_source = (
                f"GitViewspaceFS:{config['root_uuid']}:{runtime['viewfs']['attempt_token']}"
            )
            if runtime["viewfs"]["source"] != expected_source:
                raise GitViewspaceError("INVALID_RUNTIME", "runtime git-viewspace-fs source does not match")
            if not process_matches(
                runtime["daemon"]["pid"], runtime["daemon"]["start_time_ticks"]
            ):
                raise GitViewspaceError("DAEMON_UNAVAILABLE", "recorded git-viewspace daemon is not running")
            try:
                validate_socket_path(socket_path, uid, gid)
            except (FileNotFoundError, OSError) as error:
                raise GitViewspaceError("DAEMON_UNAVAILABLE", "git-viewspace daemon is unavailable") from error
        return cls(
            root=root,
            root_uuid=config["root_uuid"],
            uid=uid,
            gid=gid,
            socket_path=socket_path,
            runtime_path=runtime_path,
            manager_state=manager_state,
        )


@dataclass(frozen=True)
class RootContext:
    root: Path
    root_uuid: str
    uid: int
    gid: int
    git_identity: tuple[str, str]
    repository: Path
    repository_facts: RepositoryFacts
    config_path: Path
    state_path: Path
    runtime_path: Path
    socket_path: Path
    mountpoint: Path
    root_lock: Path
    repository_lock: Path
    manager_state: dict
    views: dict

    @classmethod
    def load(cls, root, *, git=None):
        root, uid, gid = _caller_and_root(root)

        config_path = root / "config/manager.json"
        state_path = root / "state/manager.json"
        config = validate_config(read_json(config_path))
        manager_state = validate_state(read_json(state_path))
        if config["root_path"] != str(root) or config["root_uuid"] != manager_state["root_uuid"]:
            raise GitViewspaceError("INVALID_STATE", "managed-root identity is inconsistent")
        if config["owner"] != {"uid": uid, "gid": gid}:
            raise GitViewspaceError("OWNER_MISMATCH", "managed root belongs to a different identity")

        expected_root_entries = {"config", "state", "run", "git-mnt", "views", "repo"}
        if {entry.name for entry in root.iterdir()} != expected_root_entries:
            raise GitViewspaceError("INVALID_STATE", "managed root contains an unexplained entry")
        if {entry.name for entry in (root / "config").iterdir()} != {
            "manager.json",
            "viewfs.json",
        }:
            raise GitViewspaceError("INVALID_STATE", "configuration directory contains an unexplained entry")
        if {entry.name for entry in (root / "state").iterdir()} != {
            "manager.json",
            "lock",
            "transactions",
        }:
            raise GitViewspaceError("INVALID_STATE", "state directory contains an unexplained entry")

        expected_directories = (
            (root, {0o700}),
            (root / "config", {0o700}),
            (root / "state", {0o700}),
            (root / "state/transactions", {0o700}),
            (root / "run", {0o700}),
            # The unmounted backing directory is private 0700. git-viewspace-fs
            # exposes its mounted root as workload-owned 0755.
            (root / "git-mnt", {0o755} if mount_at(root / "git-mnt") else {0o700}),
            (root / "views", {0o700}),
        )
        for directory, allowed_modes in expected_directories:
            entry = directory.lstat()
            if (
                not stat.S_ISDIR(entry.st_mode)
                or directory.is_symlink()
                or entry.st_uid != uid
                or entry.st_gid != gid
                or stat.S_IMODE(entry.st_mode) not in allowed_modes
            ):
                raise GitViewspaceError("INVALID_STATE", f"managed directory has invalid identity: {directory}")
        if any((root / "state/transactions").iterdir()):
            raise GitViewspaceError("PENDING_TRANSACTION", "managed root has a pending transaction")

        root_lock = root / "state/lock"
        repository = root / "repo"
        repository_lock = repository / ".git/git-viewspace.lock"
        validate_stable_file(root_lock, uid, gid)
        validate_stable_file(repository_lock, uid, gid)
        for managed_file in (
            config_path,
            root / "config/viewfs.json",
            state_path,
            repository / ".git/git-viewspace-owner.json",
        ):
            validate_stable_file(managed_file, uid, gid)
        owner = validate_owner(read_json(repository / ".git/git-viewspace-owner.json"))
        expected_owner = {
            "format": owner["format"],
            "schema_version": owner["schema_version"],
            "root_uuid": config["root_uuid"],
            "root_path": str(root),
            "owner": {"uid": uid, "gid": gid},
        }
        if owner != expected_owner:
            raise GitViewspaceError("REPOSITORY_CLAIMED", "repository claim does not match managed root")
        repository_facts = (git or Git()).validate_repository(
            repository,
            check_claim=False,
            owner_uid=uid,
            require_clean=False,
        )

        views = load_views(root, config["root_uuid"], manager_state, uid, gid)
        operation = manager_state["repository_operation"]
        if operation is not None:
            transitioning = next(
                view for view in views.values() if view.view_id == operation["view_id"]
            )
            expected = transitioning.next["commit"] if transitioning.next is not None else transitioning.overlay_base
            if repository_facts.head != expected or repository_facts.branch != transitioning.branch:
                raise GitViewspaceError(
                    "STALE_TRANSITION",
                    "repository checkout no longer matches the recorded view transition",
                )
        configured_aliases = read_json(root / "config/viewfs.json")
        if configured_aliases != viewfs_config(views):
            raise GitViewspaceError("INVALID_STATE", "git-viewspace-fs configuration differs from durable views")
        socket_path = validate_control_socket_location(root)
        return cls(
            root=root,
            root_uuid=config["root_uuid"],
            uid=uid,
            gid=gid,
            git_identity=(config["git_identity"]["name"], config["git_identity"]["email"]),
            repository=repository,
            repository_facts=repository_facts,
            config_path=root / "config/viewfs.json",
            state_path=state_path,
            runtime_path=root / "run/runtime.json",
            socket_path=socket_path,
            mountpoint=root / "git-mnt",
            root_lock=root_lock,
            repository_lock=repository_lock,
            manager_state=manager_state,
            views=views,
        )

    def runtime(self):
        if not self.runtime_path.exists():
            return None
        value = validate_runtime(read_json(self.runtime_path))
        if value["root_uuid"] != self.root_uuid:
            raise GitViewspaceError("INVALID_RUNTIME", "runtime root UUID does not match")
        expected_source = (
            f"GitViewspaceFS:{self.root_uuid}:{value['viewfs']['attempt_token']}"
        )
        if value["viewfs"]["source"] != expected_source:
            raise GitViewspaceError("INVALID_RUNTIME", "runtime git-viewspace-fs source does not match")
        validate_stable_file(self.runtime_path, self.uid, self.gid)
        return value

    def runtime_entries(self):
        allowed = {"manager.sock", "runtime.json", "viewfs.stderr", "submodule-prepare.json"}
        progress = self.root / "run/submodule-prepare.json"
        if progress.exists() or progress.is_symlink():
            validate_stable_file(progress, self.uid, self.gid)
        return tuple(sorted(entry.name for entry in (self.root / "run").iterdir() if entry.name not in allowed))
