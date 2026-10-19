"""Durable managed-root initialization."""

import hashlib
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import GitViewspaceError
from .git import Git
from .host import mount_at, validate_control_socket_location
from .schemas import (
    CONFIG_FORMAT,
    OWNER_FORMAT,
    SCHEMA_VERSION,
    STATE_FORMAT,
    TRANSACTION_FORMAT,
    validate_config,
    validate_owner,
    validate_state,
    validate_transaction,
)
from .storage import (
    atomic_json,
    canonical_json_bytes,
    create_stable_file,
    ensure_directory,
    exclusive_lock,
    fsync_directory,
    fsync_tree,
    read_json,
    remove_tree,
    validate_stable_file,
)
from .views import load_views, viewfs_config


@dataclass(frozen=True)
class InitRequest:
    root: Path
    git_name: str
    git_email: str
    source_mode: str
    source: str | None = None
    clone_branch: str | None = None
    initial_branch: str | None = None


@dataclass(frozen=True)
class InitResult:
    root: Path
    root_uuid: str
    repository: Path
    source_mode: str
    idempotent: bool


class Initializer:
    """Initialize or resume exactly one managed root."""

    TRANSACTION_NAME = "init.json"
    PREPARED_REPOSITORY = "init-repository"

    def __init__(self, git=None, fault=None):
        self.git = git or Git()
        self.fault = fault

    def _checkpoint(self, name):
        if self.fault is not None:
            self.fault(name)

    @staticmethod
    def _identity():
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0:
            raise GitViewspaceError("ROOT_CALLER_FORBIDDEN", "git-viewspace init must run as a non-root user")
        if uid != os.geteuid() or gid != os.getegid():
            raise GitViewspaceError("SETID_CALLER_FORBIDDEN", "real and effective UID/GID must match")
        return uid, gid

    @staticmethod
    def _validate_text(value, label, *, email=False):
        if not isinstance(value, str) or not value or value != value.strip():
            raise GitViewspaceError("INVALID_ARGUMENT", f"{label} must be non-empty without surrounding whitespace")
        if any(character in value for character in ("\0", "\r", "\n", "<", ">")):
            raise GitViewspaceError("INVALID_ARGUMENT", f"{label} contains an unsupported character")
        if email and "@" not in value:
            raise GitViewspaceError("INVALID_ARGUMENT", "Git email must contain @")

    @staticmethod
    def _canonical_root(root):
        root = Path(root)
        if not root.is_absolute():
            raise GitViewspaceError("INVALID_ROOT", "--root must be an absolute path")
        normalized = Path(os.path.abspath(root))
        resolved = Path(os.path.realpath(normalized))
        if normalized != resolved:
            raise GitViewspaceError("INVALID_ROOT", "--root must not contain symlink components")
        if normalized == Path(normalized.anchor):
            raise GitViewspaceError("INVALID_ROOT", "filesystem root cannot be a managed root")
        parent = normalized.parent
        if not parent.exists() or not parent.is_dir() or parent.is_symlink():
            raise GitViewspaceError("INVALID_ROOT", "managed-root parent must be a real directory")
        return normalized

    @staticmethod
    def _canonical_source(source):
        path = Path(source)
        normalized = Path(os.path.abspath(path))
        if Path(os.path.realpath(normalized)) != normalized:
            raise GitViewspaceError("INVALID_SOURCE", "repository source must not contain symlink components")
        return normalized

    @staticmethod
    def _disjoint(left, right):
        try:
            left.relative_to(right)
            return False
        except ValueError:
            pass
        try:
            right.relative_to(left)
            return False
        except ValueError:
            return True

    def _validate_request(self, request, uid):
        root = self._canonical_root(request.root)
        validate_control_socket_location(root)
        self._validate_text(request.git_name, "Git name")
        self._validate_text(request.git_email, "Git email", email=True)
        if request.source_mode not in {"clone", "copy", "move", "empty"}:
            raise GitViewspaceError("INVALID_ARGUMENT", "exactly one initialization source mode is required")
        if request.source_mode == "clone":
            if not request.source:
                raise GitViewspaceError("INVALID_ARGUMENT", "--clone requires a repository")
            if request.initial_branch is not None:
                raise GitViewspaceError("INVALID_ARGUMENT", "--initial-branch is valid only with --empty")
            if request.clone_branch is not None:
                self.git.validate_branch_name(request.clone_branch)
            local = Path(request.source)
            if local.exists():
                local = self._canonical_source(local)
                if not self._disjoint(root, local):
                    raise GitViewspaceError("INVALID_TOPOLOGY", "source repository and managed root must be disjoint")
        elif request.source_mode in {"copy", "move"}:
            if not request.source:
                raise GitViewspaceError("INVALID_ARGUMENT", f"--{request.source_mode} requires a repository")
            if request.clone_branch is not None or request.initial_branch is not None:
                raise GitViewspaceError("INVALID_ARGUMENT", "Git branch options do not apply to copy or move")
            source = self._canonical_source(request.source)
            if not self._disjoint(root, source):
                raise GitViewspaceError("INVALID_TOPOLOGY", "source repository and managed root must be disjoint")
        else:
            if request.source is not None or request.clone_branch is not None:
                raise GitViewspaceError("INVALID_ARGUMENT", "--empty does not accept a repository or clone branch")
            self.git.validate_branch_name(request.initial_branch or "main")
        return root

    @staticmethod
    def _argument_digest(request, root, uid, gid):
        source = request.source
        if request.source_mode in {"copy", "move"} and source is not None:
            source = str(Path(os.path.abspath(source)))
        value = {
            "root": str(root),
            "uid": uid,
            "gid": gid,
            "git_name": request.git_name,
            "git_email": request.git_email,
            "source_mode": request.source_mode,
            "source": source,
            "clone_branch": request.clone_branch,
            "initial_branch": request.initial_branch,
        }
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    @staticmethod
    def _root_paths(root):
        transactions = root / "state/transactions"
        return {
            "config": root / "config",
            "state": root / "state",
            "transactions": transactions,
            "transaction": transactions / Initializer.TRANSACTION_NAME,
            "prepared": transactions / Initializer.PREPARED_REPOSITORY,
            "run": root / "run",
            "git_mount": root / "git-mnt",
            "views": root / "views",
            "repository": root / "repo",
        }

    @staticmethod
    def _set_directory_policy(path, uid, gid):
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise GitViewspaceError("INVALID_PATH", f"expected a real directory: {path}")
        if metadata.st_uid != uid:
            raise GitViewspaceError("OWNER_MISMATCH", f"directory is not owned by UID {uid}: {path}")
        if metadata.st_gid != gid:
            os.chown(path, -1, gid)
        os.chmod(path, 0o700)

    def _recognize_pretransaction_root(self, root):
        entries = set(root.iterdir())
        state = root / "state"
        allowed = {state}
        if not entries:
            return
        if entries != allowed or state.is_symlink() or not state.is_dir():
            raise GitViewspaceError("ROOT_NOT_EMPTY", "managed root is not empty or resumable")
        state_entries = set(state.iterdir())
        transactions = state / "transactions"
        if not state_entries:
            return
        lock = state / "lock"
        if state_entries not in ({transactions}, {transactions, lock}):
            raise GitViewspaceError("ROOT_NOT_EMPTY", "managed root is not empty or resumable")
        if transactions.is_symlink() or not transactions.is_dir():
            raise GitViewspaceError("ROOT_NOT_EMPTY", "managed root is not empty or resumable")
        if lock in state_entries and (lock.is_symlink() or not lock.is_file()):
            raise GitViewspaceError("ROOT_NOT_EMPTY", "managed-root lock is invalid")
        if any(transactions.iterdir()):
            raise GitViewspaceError("INVALID_TRANSACTION", "unrecognized initialization residue")

    def _ensure_root_scaffold(self, root, uid, gid):
        created = ensure_directory(root)
        self._set_directory_policy(root, uid, gid)
        paths = self._root_paths(root)
        if not (paths["transaction"].exists() or (root / "config/manager.json").exists()):
            self._recognize_pretransaction_root(root)
        ensure_directory(paths["state"])
        self._set_directory_policy(paths["state"], uid, gid)
        ensure_directory(paths["transactions"])
        self._set_directory_policy(paths["transactions"], uid, gid)
        return created, paths

    @staticmethod
    def _transaction(request, root, uid, gid, digest):
        return {
            "format": TRANSACTION_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "operation": "init",
            "phase": "started",
            "root_uuid": str(uuid.uuid4()),
            "root_path": str(root),
            "owner": {"uid": uid, "gid": gid},
            "git_identity": {"name": request.git_name, "email": request.git_email},
            "source_mode": request.source_mode,
            "arguments_sha256": digest,
        }

    @staticmethod
    def _write_transaction(path, transaction, phase=None):
        value = dict(transaction)
        if phase is not None:
            value["phase"] = phase
        validate_transaction(value)
        atomic_json(path, value)
        return value

    def _load_or_create_transaction(self, request, root, paths, uid, gid, digest):
        if paths["transaction"].exists():
            transaction = validate_transaction(read_json(paths["transaction"]))
            mismatch = (
                transaction["root_path"] != str(root)
                or transaction["owner"] != {"uid": uid, "gid": gid}
                or transaction["git_identity"]
                != {"name": request.git_name, "email": request.git_email}
                or transaction["source_mode"] != request.source_mode
                or transaction["arguments_sha256"] != digest
            )
            if mismatch:
                if (
                    transaction["phase"] == "started"
                    and not paths["prepared"].exists()
                    and not paths["repository"].exists()
                ):
                    self._prevalidate_source(request, uid)
                    transaction = self._transaction(request, root, uid, gid, digest)
                    self._write_transaction(paths["transaction"], transaction)
                    return transaction
                raise GitViewspaceError(
                    "TRANSACTION_MISMATCH",
                    "initialization arguments do not match retained transaction",
                )
            return transaction
        transaction = self._transaction(request, root, uid, gid, digest)
        self._write_transaction(paths["transaction"], transaction)
        self._checkpoint("transaction-created")
        return transaction

    @staticmethod
    def _validate_transaction_facts(paths, transaction):
        phase = transaction["phase"]
        prepared = paths["prepared"].exists()
        published = paths["repository"].exists()
        if prepared and published:
            raise GitViewspaceError("INVALID_TRANSACTION", "both prepared and published repositories exist")
        if phase in {"started", "repository_prepared"} and published:
            raise GitViewspaceError("INVALID_TRANSACTION", "repository was published ahead of its transaction")
        if phase in {"repository_prepared", "repository_claimed"} and not (
            prepared or published
        ):
            raise GitViewspaceError("INVALID_TRANSACTION", "transaction repository is missing")
        if phase in {"repository_published", "root_published"} and not published:
            raise GitViewspaceError("INVALID_TRANSACTION", "published repository is missing")
        if phase == "root_published":
            for path in (
                paths["config"] / "manager.json",
                paths["config"] / "viewfs.json",
                paths["state"] / "manager.json",
            ):
                if not path.exists():
                    raise GitViewspaceError("INVALID_TRANSACTION", "published root record is missing")

    def _prevalidate_source(self, request, uid):
        if request.source_mode not in {"copy", "move"}:
            return
        source = self._canonical_source(request.source)
        self.git.validate_repository(source, owner_uid=uid)

    def _prepare_repository(self, request, paths, uid):
        prepared = paths["prepared"]
        expected_branch = request.clone_branch if request.source_mode == "clone" else None
        if prepared.exists():
            try:
                self.git.validate_repository(
                    prepared,
                    expected_branch=expected_branch,
                    check_claim=False,
                    owner_uid=uid,
                )
                fsync_tree(prepared)
                return
            except GitViewspaceError:
                if request.source_mode == "move" and not Path(request.source).exists():
                    raise GitViewspaceError("INVALID_TRANSACTION", "moved repository cannot be validated during recovery")
                remove_tree(prepared)

        if request.source_mode == "clone":
            try:
                self.git.clone(request.source, prepared, request.clone_branch)
            except GitViewspaceError:
                if prepared.exists():
                    remove_tree(prepared)
                raise
        elif request.source_mode == "copy":
            source = self._canonical_source(request.source)
            self.git.validate_repository(source, owner_uid=uid)
            shutil.copytree(source, prepared, symlinks=True, copy_function=shutil.copy2)
            fsync_directory(prepared.parent)
        elif request.source_mode == "move":
            source = self._canonical_source(request.source)
            self.git.validate_repository(source, owner_uid=uid)
            try:
                os.rename(source, prepared)
            except OSError as error:
                if error.errno == getattr(os, "EXDEV", 18):
                    raise GitViewspaceError("MOVE_CROSS_FILESYSTEM", "--move requires source and root on one filesystem") from error
                raise GitViewspaceError("MOVE_FAILED", "could not move repository into initialization transaction", detail=str(error)) from error
            fsync_directory(source.parent)
            fsync_directory(prepared.parent)
        else:
            self.git.create_empty(
                prepared,
                request.initial_branch or "main",
                (request.git_name, request.git_email),
            )

        self.git.validate_repository(
            prepared,
            expected_branch=expected_branch,
            owner_uid=uid,
        )
        fsync_tree(prepared)

    @staticmethod
    def _claim(paths, transaction):
        git_directory = paths["prepared"] / ".git"
        owner_path = git_directory / "git-viewspace-owner.json"
        owner = {
            "format": OWNER_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": transaction["root_uuid"],
            "root_path": transaction["root_path"],
            "owner": transaction["owner"],
        }
        if owner_path.exists():
            if validate_owner(read_json(owner_path)) != owner:
                raise GitViewspaceError("REPOSITORY_CLAIMED", "repository has a conflicting git-viewspace claim")
        else:
            atomic_json(owner_path, owner)
        create_stable_file(git_directory / "git-viewspace.lock")

    @staticmethod
    def _validate_published_claim(repository, transaction):
        owner_path = repository / ".git/git-viewspace-owner.json"
        owner = validate_owner(read_json(owner_path))
        expected = {
            "format": OWNER_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": transaction["root_uuid"],
            "root_path": transaction["root_path"],
            "owner": transaction["owner"],
        }
        if owner != expected:
            raise GitViewspaceError("REPOSITORY_CLAIMED", "published repository claim does not match transaction")
        lock = repository / ".git/git-viewspace.lock"
        validate_stable_file(
            lock,
            transaction["owner"]["uid"],
            transaction["owner"]["gid"],
        )

    @staticmethod
    def _manager_config(transaction):
        return {
            "format": CONFIG_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": transaction["root_uuid"],
            "root_path": transaction["root_path"],
            "owner": transaction["owner"],
            "git_identity": transaction["git_identity"],
            "repository": {"object_format": "sha1"},
            "initialization": {
                "source_mode": transaction["source_mode"],
                "arguments_sha256": transaction["arguments_sha256"],
            },
            "mount_policy": {
                "fuse": {
                    "allow_other": True,
                    "default_permissions": True,
                    "negative_timeout": 0,
                    "read_only": True,
                },
                "overlay": {
                    "index": "off",
                    "metacopy": "off",
                    "nfs_export": "off",
                    "redirect_dir": "on",
                    "uuid": "null",
                    "verity": "off",
                    "xino": "off",
                },
            },
        }

    @staticmethod
    def _manager_state(transaction):
        return {
            "format": STATE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": transaction["root_uuid"],
            "views": {},
            "repository_operation": None,
        }

    def _publish_root_records(self, root, paths, transaction, uid, gid):
        for key in ("config", "run", "git_mount", "views"):
            ensure_directory(paths[key])
            self._set_directory_policy(paths[key], uid, gid)
        atomic_json(paths["config"] / "manager.json", self._manager_config(transaction))
        atomic_json(paths["config"] / "viewfs.json", {})
        atomic_json(paths["state"] / "manager.json", self._manager_state(transaction))
        create_stable_file(paths["state"] / "lock")
        fsync_directory(root)

    def _validate_completed(self, request, root, uid, gid):
        config_path = root / "config/manager.json"
        if not config_path.exists():
            return None
        config = validate_config(read_json(config_path))
        state = validate_state(read_json(root / "state/manager.json"))
        if config["root_path"] != str(root) or config["root_uuid"] != state["root_uuid"]:
            raise GitViewspaceError("INVALID_STATE", "managed-root identity is inconsistent")
        if config["owner"] != {"uid": uid, "gid": gid}:
            raise GitViewspaceError("OWNER_MISMATCH", "managed root belongs to a different identity")
        if config["git_identity"] != {"name": request.git_name, "email": request.git_email}:
            raise GitViewspaceError("INITIALIZATION_MISMATCH", "Git identity differs from completed initialization")
        digest = self._argument_digest(request, root, uid, gid)
        if config["initialization"] != {
            "source_mode": request.source_mode,
            "arguments_sha256": digest,
        }:
            raise GitViewspaceError(
                "INITIALIZATION_MISMATCH",
                "source arguments differ from completed initialization",
            )
        transaction = {
            "root_uuid": config["root_uuid"],
            "root_path": str(root),
            "owner": config["owner"],
        }
        repository = root / "repo"
        self._validate_published_claim(repository, transaction)
        expected_branch = None
        if request.source_mode == "clone":
            expected_branch = request.clone_branch
        elif request.source_mode == "empty":
            expected_branch = request.initial_branch or "main"
        self.git.validate_repository(
            repository,
            expected_branch=expected_branch,
            check_claim=False,
            owner_uid=uid,
            require_clean=False,
        )
        views = load_views(root, config["root_uuid"], state, uid, gid)
        if read_json(root / "config/viewfs.json") != viewfs_config(views):
            raise GitViewspaceError("INVALID_STATE", "git-viewspace-fs configuration differs from durable views")
        required_directories = ("run", "git-mnt", "views", "state/transactions")
        for relative in required_directories:
            path = root / relative
            if path.is_symlink() or not path.is_dir():
                raise GitViewspaceError("INVALID_STATE", f"managed-root directory is missing: {relative}")
        validate_stable_file(root / "state/lock", uid, gid)
        expected_root = {"config", "state", "run", "git-mnt", "views", "repo"}
        if {entry.name for entry in root.iterdir()} != expected_root:
            raise GitViewspaceError("INVALID_STATE", "managed root contains an unexplained entry")
        if {entry.name for entry in (root / "config").iterdir()} != {
            "manager.json",
            "viewfs.json",
        }:
            raise GitViewspaceError("INVALID_STATE", "config directory contains an unexplained entry")
        if {entry.name for entry in (root / "state").iterdir()} != {
            "manager.json",
            "lock",
            "transactions",
        }:
            raise GitViewspaceError("INVALID_STATE", "state directory contains an unexplained entry")
        for relative in ("run", "git-mnt"):
            if any((root / relative).iterdir()):
                raise GitViewspaceError("INVALID_STATE", f"stopped managed directory must be empty: {relative}")
        if mount_at(root / "git-mnt") is not None or any(
            mount_at(view.mountpoint) is not None for view in views.values()
        ):
            raise GitViewspaceError("ROOT_BUSY", "managed mounts exist during initialization validation")
        if any((root / "state/transactions").iterdir()):
            raise GitViewspaceError("INVALID_TRANSACTION", "completed root has a pending transaction")
        return InitResult(root, config["root_uuid"], repository, request.source_mode, True)

    def _initialize_locked(self, request, root, uid, gid, paths):
        if not paths["transaction"].exists():
            completed = self._validate_completed(request, root, uid, gid)
            if completed is not None:
                return completed
        digest = self._argument_digest(request, root, uid, gid)
        transaction = self._load_or_create_transaction(request, root, paths, uid, gid, digest)
        self._validate_transaction_facts(paths, transaction)

        if not paths["repository"].exists():
            self._prepare_repository(request, paths, uid)
            transaction = self._write_transaction(
                paths["transaction"], transaction, "repository_prepared"
            )
            self._checkpoint("repository-prepared")
            self._claim(paths, transaction)
            transaction = self._write_transaction(
                paths["transaction"], transaction, "repository_claimed"
            )
            self._checkpoint("repository-claimed")
            os.rename(paths["prepared"], paths["repository"])
            fsync_directory(root)
            transaction = self._write_transaction(
                paths["transaction"], transaction, "repository_published"
            )
            self._checkpoint("repository-published")

        if transaction["phase"] == "repository_claimed":
            transaction = self._write_transaction(
                paths["transaction"], transaction, "repository_published"
            )

        self._validate_published_claim(paths["repository"], transaction)
        self.git.validate_repository(
            paths["repository"],
            expected_branch=request.clone_branch if request.source_mode == "clone" else None,
            check_claim=False,
            owner_uid=uid,
        )
        self._publish_root_records(root, paths, transaction, uid, gid)
        transaction = self._write_transaction(paths["transaction"], transaction, "root_published")
        self._checkpoint("root-published")
        paths["transaction"].unlink()
        fsync_directory(paths["transactions"])
        self._checkpoint("transaction-removed")

        result = self._validate_completed(request, root, uid, gid)
        if result is None:
            raise GitViewspaceError("INVALID_STATE", "initialization did not publish a complete root")
        return InitResult(
            result.root,
            result.root_uuid,
            result.repository,
            request.source_mode,
            False,
        )

    def initialize(self, request):
        uid, gid = self._identity()
        root = self._validate_request(request, uid)
        transaction_path = root / "state/transactions" / self.TRANSACTION_NAME
        config_path = root / "config/manager.json"
        if config_path.exists() and not transaction_path.exists():
            self._validate_completed(request, root, uid, gid)
            with exclusive_lock(root / "state/lock"):
                return self._validate_completed(request, root, uid, gid)
        if not transaction_path.exists() and not config_path.exists():
            self._prevalidate_source(request, uid)

        _, paths = self._ensure_root_scaffold(root, uid, gid)
        lock_path = paths["state"] / "lock"
        create_stable_file(lock_path)
        with exclusive_lock(lock_path):
            return self._initialize_locked(request, root, uid, gid, paths)
