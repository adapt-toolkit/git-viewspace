"""Strict durable-record schemas."""

import hashlib
import json

from .errors import GitViewspaceError

SCHEMA_VERSION = 1
CONFIG_FORMAT = "git-viewspace-manager-config-v1"
STATE_FORMAT = "git-viewspace-manager-state-v1"
OWNER_FORMAT = "git-viewspace-repository-owner-v1"
TRANSACTION_FORMAT = "git-viewspace-init-transaction-v1"
RUNTIME_FORMAT = "git-viewspace-runtime-v1"
VIEW_CONFIG_FORMAT = "git-viewspace-view-config-v1"
VIEW_STATE_FORMAT = "git-viewspace-view-state-v1"
GENERATION_FORMAT = "git-viewspace-overlay-generation-v1"
SYNC_FORMAT = "git-viewspace-view-sync-v2"
STAGE_FORMAT = "git-viewspace-view-stage-v1"
NEXT_FORMAT = "git-viewspace-view-next-v1"
REPOSITORY_OPERATION_FORMAT = "git-viewspace-repository-operation-v1"


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise GitViewspaceError("INVALID_STATE", f"invalid {label} schema")


def _integer(value, label):
    if type(value) is not int or value < 0:
        raise GitViewspaceError("INVALID_STATE", f"invalid {label}")


def _string(value, label):
    if not isinstance(value, str) or not value:
        raise GitViewspaceError("INVALID_STATE", f"invalid {label}")


def _digest(value, label):
    _string(value, label)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise GitViewspaceError("INVALID_STATE", f"invalid {label}")


def _boolean(value, label):
    if not isinstance(value, bool):
        raise GitViewspaceError("INVALID_STATE", f"invalid {label}")


def _oid(value, label):
    _string(value, label)
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise GitViewspaceError("INVALID_STATE", f"invalid {label}")


def _identifier(value, label):
    _string(value, label)
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise GitViewspaceError("INVALID_STATE", f"invalid {label}")


def _manifest_path(value):
    _string(value, "Git manifest path")
    if value == ".":
        return
    if value.startswith("/") or "\0" in value:
        raise GitViewspaceError("INVALID_STATE", "invalid Git manifest path")
    components = value.split("/")
    if any(component in {"", ".", "..", ".git"} for component in components):
        raise GitViewspaceError("INVALID_STATE", "invalid Git manifest path")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise GitViewspaceError("INVALID_STATE", "invalid Git manifest path") from error


def _canonical(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sync_manifest_digest(manifest):
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def sync_identifier(value):
    identity = {
        key: value[key]
        for key in (
            "root_uuid",
            "view_id",
            "generation_id",
            "branch",
            "base_commit",
            "upper_fingerprint",
            "manifest_sha256",
            "base_tree",
            "counts",
        )
    }
    return hashlib.sha256(_canonical(identity)).hexdigest()[:32]


def validate_sync(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "view_id",
            "sync_id",
            "generation_id",
            "branch",
            "base_commit",
            "upper_fingerprint",
            "manifest",
            "manifest_sha256",
            "base_tree",
            "counts",
        ),
        "view sync",
    )
    if value["format"] != SYNC_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported view sync")
    _string(value["root_uuid"], "root UUID")
    _identifier(value["view_id"], "view ID")
    _identifier(value["sync_id"], "sync ID")
    _identifier(value["generation_id"], "generation ID")
    _string(value["branch"], "owned branch")
    _oid(value["base_commit"], "sync base")
    _digest(value["upper_fingerprint"], "upper fingerprint")
    _digest(value["manifest_sha256"], "manifest digest")
    _oid(value["base_tree"], "base tree")
    manifest = value["manifest"]
    if not isinstance(manifest, list) or manifest != sorted(set(manifest)):
        raise GitViewspaceError("INVALID_STATE", "invalid Git path manifest")
    for path in manifest:
        _manifest_path(path)
    if value["manifest_sha256"] != sync_manifest_digest(manifest):
        raise GitViewspaceError("INVALID_STATE", "Git path manifest digest does not match")
    counts = value["counts"]
    expected_counts = {
        "directories",
        "regulars",
        "symlinks",
        "whiteouts",
        "opaque",
        "origin",
        "impure",
    }
    if not isinstance(counts, dict) or set(counts) != expected_counts:
        raise GitViewspaceError("INVALID_STATE", "invalid sync artifact counts")
    for count in counts.values():
        _integer(count, "sync artifact count")
    if value["sync_id"] != sync_identifier(value):
        raise GitViewspaceError("INVALID_STATE", "view sync identity does not match")
    return value


def stage_identifier(value):
    identity = {
        key: value[key]
        for key in (
            "root_uuid",
            "view_id",
            "sync_id",
            "generation_id",
            "branch",
            "base_commit",
            "upper_fingerprint",
            "manifest_sha256",
            "base_tree",
            "checkout_tree",
            "index_tree",
        )
    }
    return hashlib.sha256(_canonical(identity)).hexdigest()[:32]


def validate_stage(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "view_id",
            "stage_id",
            "sync_id",
            "generation_id",
            "branch",
            "base_commit",
            "upper_fingerprint",
            "manifest_sha256",
            "base_tree",
            "checkout_tree",
            "index_tree",
        ),
        "view stage",
    )
    if value["format"] != STAGE_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported view stage")
    _string(value["root_uuid"], "root UUID")
    for field in ("view_id", "stage_id", "sync_id", "generation_id"):
        _identifier(value[field], field.replace("_", " "))
    _string(value["branch"], "owned branch")
    _oid(value["base_commit"], "stage base")
    _digest(value["upper_fingerprint"], "upper fingerprint")
    _digest(value["manifest_sha256"], "manifest digest")
    for field in ("base_tree", "checkout_tree", "index_tree"):
        _oid(value[field], field.replace("_", " "))
    if value["checkout_tree"] != value["index_tree"]:
        raise GitViewspaceError("INVALID_STATE", "staged index differs from synchronized checkout")
    if value["stage_id"] != stage_identifier(value):
        raise GitViewspaceError("INVALID_STATE", "view stage identity does not match")
    return value


def next_identifier(value):
    identity = {
        key: value[key]
        for key in (
            "root_uuid",
            "view_id",
            "sync_id",
            "stage_id",
            "generation_id",
            "branch",
            "base_commit",
            "commit",
            "tree",
            "changed",
        )
    }
    return hashlib.sha256(_canonical(identity)).hexdigest()[:32]


def validate_next(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "view_id",
            "next_id",
            "sync_id",
            "stage_id",
            "generation_id",
            "branch",
            "base_commit",
            "commit",
            "tree",
            "changed",
        ),
        "view next transition",
    )
    if value["format"] != NEXT_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported view next transition")
    _string(value["root_uuid"], "root UUID")
    for field in ("view_id", "next_id", "sync_id", "stage_id", "generation_id"):
        _identifier(value[field], field.replace("_", " "))
    _string(value["branch"], "owned branch")
    for field in ("base_commit", "commit", "tree"):
        _oid(value[field], field.replace("_", " "))
    _boolean(value["changed"], "next changed state")
    if value["changed"] != (value["commit"] != value["base_commit"]):
        raise GitViewspaceError("INVALID_STATE", "next transition changed state is inconsistent")
    if value["next_id"] != next_identifier(value):
        raise GitViewspaceError("INVALID_STATE", "view next identity does not match")
    return value


def validate_repository_operation(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "operation",
            "view_id",
            "sync_id",
        ),
        "repository operation",
    )
    if (
        value["format"] != REPOSITORY_OPERATION_FORMAT
        or value["schema_version"] != SCHEMA_VERSION
        or value["operation"] not in {"sync", "stage", "next"}
    ):
        raise GitViewspaceError("INVALID_STATE", "unsupported repository operation")
    _string(value["root_uuid"], "root UUID")
    _identifier(value["view_id"], "view ID")
    _identifier(value["sync_id"], "sync ID")
    return value


def validate_view_name(value):
    if (
        not isinstance(value, str)
        or value in {"", ".", "..", ".git"}
        or value.startswith(".new-")
        or "/" in value
        or "\0" in value
    ):
        raise GitViewspaceError("INVALID_VIEW_NAME", "view name must be one supported filename component")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GitViewspaceError("INVALID_VIEW_NAME", "view name must be valid UTF-8") from error
    if len(encoded) > 255:
        raise GitViewspaceError("INVALID_VIEW_NAME", "view name exceeds the filesystem component limit")
    return value


def validate_owner(value):
    _object(
        value,
        ("format", "schema_version", "root_uuid", "root_path", "owner"),
        "repository owner",
    )
    if value["format"] != OWNER_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported repository-owner schema")
    _string(value["root_uuid"], "root UUID")
    _string(value["root_path"], "root path")
    _object(value["owner"], ("uid", "gid"), "owner identity")
    _integer(value["owner"]["uid"], "owner UID")
    _integer(value["owner"]["gid"], "owner GID")
    return value


def validate_config(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "root_path",
            "owner",
            "git_identity",
            "repository",
            "initialization",
            "mount_policy",
        ),
        "manager configuration",
    )
    if value["format"] != CONFIG_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported manager configuration schema")
    _string(value["root_uuid"], "root UUID")
    _string(value["root_path"], "root path")
    _object(value["owner"], ("uid", "gid"), "owner identity")
    _integer(value["owner"]["uid"], "owner UID")
    _integer(value["owner"]["gid"], "owner GID")
    _object(value["git_identity"], ("name", "email"), "Git identity")
    _string(value["git_identity"]["name"], "Git name")
    _string(value["git_identity"]["email"], "Git email")
    _object(value["repository"], ("object_format",), "repository policy")
    if value["repository"]["object_format"] != "sha1":
        raise GitViewspaceError("INVALID_STATE", "unsupported repository object format")
    _object(
        value["initialization"],
        ("source_mode", "arguments_sha256"),
        "initialization identity",
    )
    if value["initialization"]["source_mode"] not in {"clone", "copy", "move", "empty"}:
        raise GitViewspaceError("INVALID_STATE", "invalid initialization source mode")
    _digest(value["initialization"]["arguments_sha256"], "initialization argument digest")
    _object(value["mount_policy"], ("fuse", "overlay"), "mount policy")
    _object(
        value["mount_policy"]["fuse"],
        ("allow_other", "default_permissions", "negative_timeout", "read_only"),
        "FUSE policy",
    )
    if value["mount_policy"]["fuse"] != {
        "allow_other": True,
        "default_permissions": True,
        "negative_timeout": 0,
        "read_only": True,
    }:
        raise GitViewspaceError("INVALID_STATE", "invalid FUSE policy")
    _object(
        value["mount_policy"]["overlay"],
        ("index", "metacopy", "nfs_export", "redirect_dir", "uuid", "verity", "xino"),
        "OverlayFS policy",
    )
    if value["mount_policy"]["overlay"] != {
        "index": "off",
        "metacopy": "off",
        "nfs_export": "off",
        "redirect_dir": "on",
        "uuid": "null",
        "verity": "off",
        "xino": "off",
    }:
        raise GitViewspaceError("INVALID_STATE", "invalid OverlayFS policy")
    return value


def validate_state(value):
    _object(
        value,
        ("format", "schema_version", "root_uuid", "views", "repository_operation"),
        "manager state",
    )
    if value["format"] != STATE_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported manager state schema")
    _string(value["root_uuid"], "root UUID")
    if not isinstance(value["views"], dict):
        raise GitViewspaceError("INVALID_STATE", "invalid view registry")
    for name, reference in value["views"].items():
        validate_view_name(name)
        _object(reference, ("view_id",), "view reference")
        _identifier(reference["view_id"], "view ID")
    if value["repository_operation"] is not None:
        validate_repository_operation(value["repository_operation"])
    return value


def validate_view_config(value):
    if not isinstance(value, dict) or value.get("kind") not in {"readonly", "overlay"}:
        raise GitViewspaceError("INVALID_STATE", "invalid view configuration schema")
    keys = (
        "format",
        "schema_version",
        "root_uuid",
        "view_id",
        "name",
        "kind",
        "commit",
        "alias",
    )
    if value["kind"] == "overlay":
        keys = (*keys, "branch")
    if "routing" in value:
        keys = (*keys, "routing")
        routing_keys = ("executable", "runtime_directories") if "runtime_directories" in value["routing"] else ("executable",)
        if "subtree" in value["routing"]:
            routing_keys = (*routing_keys, "subtree")
            from .routing import validate_subtree
            validate_subtree(value["routing"]["subtree"])
        _object(value["routing"], routing_keys, "routing configuration")
        from .runtime_mounts import validate_mapping
        validate_mapping(value["routing"].get("runtime_directories", {}))
        binary = value["routing"]["executable"]
        _string(binary, "router executable")
        if not binary.startswith("/") or "\0" in binary:
            raise GitViewspaceError("INVALID_STATE", "router executable must be an absolute path")
    _object(value, keys, "view configuration")
    if value["format"] != VIEW_CONFIG_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported view configuration")
    _string(value["root_uuid"], "root UUID")
    _identifier(value["view_id"], "view ID")
    validate_view_name(value["name"])
    _oid(value["commit"], "view commit")
    _string(value["alias"], "view alias")
    if (
        not value["alias"].startswith("v-")
        or len(value["alias"]) != 34
        or any(character not in "0123456789abcdef" for character in value["alias"][2:])
    ):
        raise GitViewspaceError("INVALID_STATE", "invalid view alias")
    if value["kind"] == "overlay":
        _string(value["branch"], "owned branch")
    return value


def validate_view_state(value):
    if not isinstance(value, dict):
        raise GitViewspaceError("INVALID_STATE", "invalid view state schema")
    keys = (
        "format",
        "schema_version",
        "root_uuid",
        "view_id",
        "desired_started",
    )
    if "overlay_base" in value or "active_generation" in value:
        keys = (*keys, "overlay_base", "active_generation")
    if "sync" in value:
        keys = (*keys, "sync")
    if "stage" in value:
        keys = (*keys, "stage")
    if "next" in value:
        keys = (*keys, "next")
    _object(value, keys, "view state")
    if value["format"] != VIEW_STATE_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported view state")
    _string(value["root_uuid"], "root UUID")
    _identifier(value["view_id"], "view ID")
    _boolean(value["desired_started"], "desired-started state")
    if "overlay_base" in value:
        _oid(value["overlay_base"], "overlay base")
        _identifier(value["active_generation"], "active generation")
    if "sync" in value:
        if "overlay_base" not in value:
            raise GitViewspaceError("INVALID_STATE", "read-only view cannot have sync state")
        validate_sync(value["sync"])
    if "stage" in value:
        if "sync" not in value:
            raise GitViewspaceError("INVALID_STATE", "view stage requires sync state")
        validate_stage(value["stage"])
        if value["stage"]["sync_id"] != value["sync"]["sync_id"]:
            raise GitViewspaceError("INVALID_STATE", "view stage does not match sync state")
    if "next" in value:
        if "overlay_base" not in value:
            raise GitViewspaceError("INVALID_STATE", "read-only view cannot have next state")
        validate_next(value["next"])
        if "stage" in value and value["next"]["stage_id"] != value["stage"]["stage_id"]:
            raise GitViewspaceError("INVALID_STATE", "view next state does not match stage state")
    return value


def validate_generation(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "view_id",
            "generation_id",
            "base_commit",
        ),
        "overlay generation",
    )
    if value["format"] != GENERATION_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_STATE", "unsupported overlay generation")
    _string(value["root_uuid"], "root UUID")
    _identifier(value["view_id"], "view ID")
    _identifier(value["generation_id"], "generation ID")
    _oid(value["base_commit"], "generation base")
    return value


def validate_transaction(value):
    keys = (
        "format",
        "schema_version",
        "operation",
        "phase",
        "root_uuid",
        "root_path",
        "owner",
        "git_identity",
        "source_mode",
        "arguments_sha256",
    )
    _object(value, keys, "initialization transaction")
    if value["format"] != TRANSACTION_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_TRANSACTION", "unsupported initialization transaction")
    if value["operation"] != "init":
        raise GitViewspaceError("INVALID_TRANSACTION", "unexpected transaction operation")
    if value["phase"] not in {
        "started",
        "repository_prepared",
        "repository_claimed",
        "repository_published",
        "root_published",
    }:
        raise GitViewspaceError("INVALID_TRANSACTION", "invalid initialization phase")
    _string(value["root_uuid"], "root UUID")
    _string(value["root_path"], "root path")
    _object(value["owner"], ("uid", "gid"), "owner identity")
    _integer(value["owner"]["uid"], "owner UID")
    _integer(value["owner"]["gid"], "owner GID")
    _object(value["git_identity"], ("name", "email"), "Git identity")
    _string(value["git_identity"]["name"], "Git name")
    _string(value["git_identity"]["email"], "Git email")
    if value["source_mode"] not in {"clone", "copy", "move", "empty"}:
        raise GitViewspaceError("INVALID_TRANSACTION", "invalid initialization source mode")
    _digest(value["arguments_sha256"], "argument digest")
    return value


def validate_runtime(value):
    _object(
        value,
        (
            "format",
            "schema_version",
            "root_uuid",
            "daemon",
            "viewfs",
            "restart_count",
            "ready",
        ),
        "runtime record",
    )
    if value["format"] != RUNTIME_FORMAT or value["schema_version"] != SCHEMA_VERSION:
        raise GitViewspaceError("INVALID_RUNTIME", "unsupported runtime record")
    _string(value["root_uuid"], "root UUID")
    _object(value["daemon"], ("pid", "start_time_ticks"), "daemon identity")
    _integer(value["daemon"]["pid"], "daemon PID")
    _integer(value["daemon"]["start_time_ticks"], "daemon start time")
    if value["daemon"]["pid"] <= 0 or value["daemon"]["start_time_ticks"] <= 0:
        raise GitViewspaceError("INVALID_RUNTIME", "invalid daemon identity")
    _object(
        value["viewfs"],
        ("attempt_token", "source", "supervisor_pid", "supervisor_start_time_ticks"),
        "git-viewspace-fs runtime",
    )
    _string(value["viewfs"]["attempt_token"], "git-viewspace-fs attempt token")
    _string(value["viewfs"]["source"], "git-viewspace-fs source")
    _integer(value["viewfs"]["supervisor_pid"], "git-viewspace-fs supervisor PID")
    if value["viewfs"]["supervisor_pid"] <= 0:
        raise GitViewspaceError("INVALID_RUNTIME", "invalid git-viewspace-fs supervisor PID")
    _integer(
        value["viewfs"]["supervisor_start_time_ticks"],
        "git-viewspace-fs supervisor start time",
    )
    if value["viewfs"]["supervisor_start_time_ticks"] <= 0:
        raise GitViewspaceError("INVALID_RUNTIME", "invalid git-viewspace-fs supervisor identity")
    _integer(value["restart_count"], "git-viewspace-fs restart count")
    if not isinstance(value["ready"], bool):
        raise GitViewspaceError("INVALID_RUNTIME", "invalid runtime readiness")
    return value
