"""Explicit acquisition and immutable Git-object access for top-level code modules."""

import hashlib
import os
import re
import signal
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from .errors import GitViewspaceError
from .git import Git


def exact_oid(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise GitViewspaceError("INVALID_COMMIT", "submodule commit must be an exact SHA-1 object ID")
    return value


def entries(repository, commit):
    """Read registrations from this snapshot, never mutable local Git configuration."""
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
        # Immutable metadata only. Missing stores/objects are checked separately, every use.
        return {
            path: dict(values)
            for path, values in _entries_at(str(Path(repository).resolve()), commit)
        }
    return _read_entries(repository, commit)  # Symbolic refs must never become cache keys.


@lru_cache(maxsize=128)
def _entries_at(repository, commit):
    return tuple(
        (path, tuple(values.items())) for path, values in _read_entries(repository, commit).items()
    )


def _read_entries(repository, commit):
    git = Git()
    links = {}
    for record in git._repo(repository, ("ls-tree", "-rz", commit)).stdout.split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            mode, kind, oid = meta.split()
            if mode == b"160000":
                if kind != b"commit":
                    raise GitViewspaceError("INVALID_SUBMODULE", "gitlink must name a commit")
                links[path.decode()] = oid.decode()
    if not links:
        return {}
    config = git._repo(
        repository,
        (
            "config",
            "--null",
            "--blob",
            f"{commit}:.gitmodules",
            "--get-regexp",
            r"^submodule\..*\.(path|url)$",
        ),
        check=False,
    )
    if config.returncode:
        raise GitViewspaceError("INVALID_SUBMODULE", "gitlinks require a valid committed .gitmodules")
    registrations = {}
    for record in config.stdout.split(b"\0"):
        if record:
            key, value = record.decode().split("\n", 1)
            name, _, field = key.rpartition(".")
            section = registrations.setdefault(name, {})
            if field in section:
                raise GitViewspaceError("INVALID_SUBMODULE", "duplicate module configuration")
            section[field] = value
    result = {}
    for item in registrations.values():
        path, url = item.get("path", ""), item.get("url", "")
        if path not in links:
            continue
        if (
            path in result
            or path.startswith("/")
            or any(p in {"", ".", "..", ".git"} for p in path.split("/"))
        ):
            raise GitViewspaceError("INVALID_SUBMODULE", "invalid or duplicate module path")
        if not url or url.startswith("-") or not (Path(url).is_absolute() or "://" in url):
            raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "module URL must be absolute")
        result[path] = {"path": path, "url": url, "commit": links[path]}
    if result.keys() != links.keys():
        raise GitViewspaceError("INVALID_SUBMODULE", "every gitlink requires path and URL registration")
    return result


def store_path(repository, url):
    directory = Git()._repo(repository, ("rev-parse", "--absolute-git-dir")).stdout.decode().strip()
    return Path(directory) / "viewspace-submodules" / hashlib.sha256(url.encode()).hexdigest()


def validate_store(store, commit):
    exact_oid(commit)
    if not store.is_dir():
        raise GitViewspaceError("SUBMODULE_NOT_PREPARED", "prepare the module before starting its view")
    git = Git()
    found = git._repo(store, ("cat-file", "-t", commit), check=False)
    if found.returncode or found.stdout.strip() != b"commit":
        raise GitViewspaceError("SUBMODULE_NOT_PREPARED", "exact module commit is unavailable locally")
    tree = git._repo(store, ("ls-tree", "-rz", commit)).stdout
    if any(row.startswith(b"160000 ") for row in tree.split(b"\0")):
        raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "nested submodules are not supported")
    git._validate_commit_tree(store, commit)


def require_available(repository, commit):
    modules = entries(repository, commit)
    for item in modules.values():
        validate_store(store_path(repository, item["url"]), item["commit"])
    return modules


def fetch_exact(git, store, url, commit, *, timeout=120):
    """Acquisition must release the single daemon control loop before RPC timeout."""
    command = [
        git.executable,
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(store),
        "fetch",
        "--no-tags",
        "--",
        url,
        commit,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git._environment(authentication=True),
            start_new_session=True,
        )
    except OSError as error:
        raise GitViewspaceError("GIT_EXEC_FAILED", "could not start module fetch") from error
    try:
        process.communicate(timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise GitViewspaceError(
                "SUBMODULE_FETCH_TIMEOUT", "module fetch exceeded its deadline"
            ) from error
        raise
    if process.returncode:
        raise GitViewspaceError("SUBMODULE_FETCH_FAILED", "could not fetch exact module commit")


def prepare(repository, snapshot, path, commit=""):
    git = Git()
    snapshot = git.resolve_commit(repository, snapshot)
    item = entries(repository, snapshot).get(path)
    if item is None:
        raise GitViewspaceError(
            "SUBMODULE_NOT_FOUND", "path is not a registered gitlink in this snapshot"
        )
    commit = exact_oid(commit or item["commit"])
    store = store_path(repository, item["url"])
    store.parent.mkdir(parents=True, exist_ok=True)
    if not store.exists():
        git.run(("init", "--bare", store))
    fetch_exact(git, store, item["url"], commit)
    validate_store(store, commit)
    git._repo(store, ("update-ref", "refs/pins/" + commit, commit))
    return {"path": path, "commit": commit, "prepared": True}


def pin(repository, branch, path, commit, message, identity):
    """Caller holds repository ownership and has excluded writable branch claims."""
    git = Git()
    base = git.resolve_local_branch(repository, branch)
    if git.checkout_branch(repository) == branch:
        raise GitViewspaceError("BRANCH_CHECKED_OUT", "pin on an unclaimed, non-checked-out branch")
    item = entries(repository, base).get(path)
    if item is None:
        raise GitViewspaceError("SUBMODULE_NOT_FOUND", "path is not a registered gitlink")
    validate_store(store_path(repository, item["url"]), commit)
    if not isinstance(message, str) or not message.strip():
        raise GitViewspaceError("INVALID_MESSAGE", "pin update requires a commit message")
    if item["commit"] == commit:
        return {"branch": branch, "commit": base, "idempotent": True}
    gitdir = Path(repository) / ".git"
    fd, index = tempfile.mkstemp(prefix="submodule-index-", dir=gitdir)
    os.close(fd)
    os.unlink(index)  # Git requires an absent index, not an empty file.
    environment = {"GIT_INDEX_FILE": index}
    try:
        git._repo_with_environment(
            repository, ("read-tree", base), environment_overrides=environment
        )
        git._repo_with_environment(
            repository,
            ("update-index", "--add", "--cacheinfo", f"160000,{commit},{path}"),
            environment_overrides=environment,
        )
        tree = (
            git._repo_with_environment(
                repository, ("write-tree",), environment_overrides=environment
            )
            .stdout.decode()
            .strip()
        )
        new = (
            git.run(
                ("-C", repository, "commit-tree", tree, "-p", base, "-m", message),
                identity=identity,
            )
            .stdout.decode()
            .strip()
        )
        git._repo(repository, ("update-ref", "refs/heads/" + branch, new, base))
        return {"branch": branch, "commit": new, "idempotent": False}
    finally:
        Path(index).unlink(missing_ok=True)


def protect_sync(repository, base, paths):
    """Gitlinks/config are modified only through explicit pin operations, not overlay replay."""
    modules = entries(repository, base)
    if not modules:
        return
    for path in paths:
        if path in {".", ".gitmodules"} or any(
            path == module or path.startswith(module + "/") or module.startswith(path + "/")
            for module in modules
        ):
            raise GitViewspaceError(
                "SUBMODULE_READ_ONLY",
                "overlay sync cannot modify code module paths or registrations",
            )
