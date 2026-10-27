"""Explicit acquisition and immutable Git-object access for pinned code modules."""

import hashlib
import os
import json
import time
from urllib.parse import urlsplit, urlunsplit
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


def entries(repository, commit, *, git=None):
    """Read registrations from this snapshot, never mutable local Git configuration."""
    if git is not None:
        return _read_entries(repository, commit, git=git)
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


def _read_entries(repository, commit, *, git=None):
    git = git or Git()
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
        validate_url(url)
        result[path] = {"path": path, "url": url, "commit": links[path]}
    if result.keys() != links.keys():
        raise GitViewspaceError("INVALID_SUBMODULE", "every gitlink requires path and URL registration")
    return result


def store_path(repository, url):
    directory = Git()._repo(repository, ("rev-parse", "--absolute-git-dir")).stdout.decode().strip()
    return Path(directory) / "viewspace-submodules" / hashlib.sha256(url.encode()).hexdigest()


MAX_DEPTH = 32
MAX_OCCURRENCES = 4096
PREPARE_SECONDS = 120


def validate_url(url):
    """Supported Git locations, without invoking shell helpers or local config."""
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "invalid module URL")
    if url.startswith(("/", "./", "../")):
        return
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError as error:
            raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "invalid module URL") from error
        if parts.scheme not in {"https", "http", "ssh", "git", "file"} or parts.query or parts.fragment:
            raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "unsupported module URL form")
        if parts.scheme != "file" and not parts.netloc:
            raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "module URL requires a host")
        return
    if re.fullmatch(r"[^/\s:]+:[^\s]+", url) and not url.startswith("-") and "::" not in url:
        return
    raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "unsupported module URL form")


def resolve_url(url, base=None):
    validate_url(url)
    if not url.startswith(("./", "../")):
        return url
    if base is None:
        raise GitViewspaceError("SUBMODULE_NOT_PREPARED", "prepare relative module URLs first")
    validate_url(base)
    if "://" in base:
        parts = urlsplit(base)
        path, prefix = parts.path.rstrip("/"), "url"
    elif base.startswith("/"):
        path, prefix = base.rstrip("/"), "local"
    else:
        host, path = base.split(":", 1)
        path, prefix = path.rstrip("/"), "scp"
    absolute = prefix in {"url", "local"} or path.startswith("/")
    # Git consumes leading ./ and ../ only; interior components must retain
    # their spelling (notably for symlink traversal in local/SSH locations).
    while url.startswith(("./", "../")):
        if url.startswith("./"):
            url = url[2:]
        else:
            if not path or path == "/":
                raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "relative URL traverses above repository URL root")
            path = path.rpartition("/")[0]
            url = url[3:]
    combined = path + "/" + url if path else ("/" if absolute else "") + url
    if prefix == "url":
        return urlunsplit((parts.scheme, parts.netloc, combined, "", ""))
    return combined if prefix == "local" else host + ":" + combined


def store_root(repository, *, git=None):
    git = git or Git()
    directory = git._repo(repository, ("rev-parse", "--absolute-git-dir")).stdout.decode().strip()
    return Path(directory) / "viewspace-submodules"


def root_url(repository, *, create=False, git=None):
    """Once-only local binding: mutable origin never reinterprets a snapshot."""
    from .storage import atomic_json
    git = git or Git()
    record = store_root(repository, git=git) / "source.json"
    if record.exists():
        try:
            value = json.loads(record.read_text())["url"]
            validate_url(value)
            if value.startswith(("./", "../")):
                raise ValueError("relative source")
            return value
        except (ValueError, KeyError, TypeError) as error:
            raise GitViewspaceError("INVALID_SUBMODULE", "invalid frozen module URL base") from error
    if not create:
        return None
    configured = git._repo(repository, ("config", "--get", "remote.origin.url"), check=False)
    value = configured.stdout.decode().strip() if configured.returncode == 0 else str(Path(repository).resolve())
    # Git permits a relative local origin. Freeze its absolute spelling.
    if ":" not in value and not value.startswith("/"):
        value = str((Path(repository) / value).resolve())
    validate_url(value)
    record.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(record, {"url": value})  # atomic_json creates mode 0600
    return value


def resolved_entries(repository, commit, base=None, *, git=None):
    return {path: {**item, "url": resolve_url(item["url"], base)}
            for path, item in entries(repository, commit, git=git).items()}


def validate_store(store, commit, *, git=None):
    exact_oid(commit)
    if not store.is_dir():
        raise GitViewspaceError("SUBMODULE_NOT_PREPARED", "prepare the module before starting its view")
    git = git or Git()
    found = git._repo(store, ("cat-file", "-t", commit), check=False)
    if found.returncode or found.stdout.strip() != b"commit":
        raise GitViewspaceError("SUBMODULE_NOT_PREPARED", "exact module commit is unavailable locally")
    git._validate_commit_tree(store, commit, require_blobs=True)


class _Closure:
    """One operation's traversal; shared stores but distinct mounted occurrences."""

    def __init__(self, repository, *, git=None, acquire=None, recursive=True):
        self.git = git or Git()
        self.stores = store_root(repository, git=self.git)
        self.acquire = acquire
        self.recursive = recursive
        self.loaded = {}
        self.active = set()
        self.count = 0

    def walk(self, roots):
        for path, item in sorted(roots.items()):
            self._visit(path, item, 1)

    def _visit(self, path, item, depth):
        if self.acquire:
            self.git.remaining()
        self.count += 1
        if depth > MAX_DEPTH or self.count > MAX_OCCURRENCES:
            raise GitViewspaceError("SUBMODULE_LIMIT", "module traversal limit exceeded")
        key = (item["url"], item["commit"])
        if key in self.active:
            raise GitViewspaceError("SUBMODULE_CYCLE", "recursive module cycle")
        if key not in self.loaded:
            store = self.stores / hashlib.sha256(item["url"].encode()).hexdigest()
            if self.acquire:
                self.acquire(store, item, path)
            else:
                validate_store(store, item["commit"], git=self.git)
            children = resolved_entries(store, item["commit"], item["url"], git=self.git)
            if children and not self.recursive:
                raise GitViewspaceError("UNSUPPORTED_SUBMODULE", "nested modules require --recursive")
            self.loaded[key] = children
        self.active.add(key)
        try:
            for child, target in sorted(self.loaded[key].items()):
                self._visit(path + "/" + child, target, depth + 1)
        finally:
            self.active.remove(key)


def require_available(repository, commit):
    modules = resolved_entries(repository, commit, root_url(repository))
    _Closure(repository).walk(modules)
    return modules


class _BudgetGit(Git):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GitViewspaceError("SUBMODULE_FETCH_TIMEOUT", "module preparation deadline exceeded")
        return remaining

    def run(self, arguments, **kwargs):
        try:
            return super().run(("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *arguments),
                               timeout=self.remaining(), **kwargs)
        except subprocess.TimeoutExpired as error:
            raise GitViewspaceError("SUBMODULE_FETCH_TIMEOUT", "module preparation deadline exceeded") from error


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


def prepare(repository, snapshot, path, commit="", *, recursive=False, progress=None):
    """Acquire explicitly; partial success retains objects but never changes a branch."""
    git = _BudgetGit(time.monotonic() + PREPARE_SECONDS)
    totals = {"fetched": 0, "reused": 0}

    def report(phase, **fields):
        if progress:
            progress({"phase": phase, **totals, **fields})

    def acquire(store, item, occurrence):
        git.remaining()
        report("checking", path=occurrence, commit=item["commit"])
        store.parent.mkdir(parents=True, exist_ok=True)
        if not store.exists():
            git.run(("init", "--bare", store))
        present = git._repo(store, ("cat-file", "-t", item["commit"]), check=False)
        if present.returncode or present.stdout.strip() != b"commit":
            report("fetching", path=occurrence, commit=item["commit"])
            fetch_exact(git, store, item["url"], item["commit"], timeout=git.remaining())
            totals["fetched"] += 1
        else:
            totals["reused"] += 1
        validate_store(store, item["commit"], git=git)
        git._repo(store, ("update-ref", "refs/pins/" + item["commit"], item["commit"]))
        report("prepared", path=occurrence, commit=item["commit"])

    report("running", path=path)
    try:
        snapshot = git.resolve_commit(repository, snapshot)
        raw = entries(repository, snapshot, git=git)
        item = raw.get(path)
        if item is None:
            raise GitViewspaceError("SUBMODULE_NOT_FOUND", "path is not a registered gitlink in this snapshot")
        base = root_url(repository, create=True, git=git) if item["url"].startswith(("./", "../")) else None
        item = {**item, "url": resolve_url(item["url"], base), "commit": exact_oid(commit or item["commit"])}
        closure = _Closure(repository, git=git, acquire=acquire, recursive=recursive)
        closure.walk({path: item})
        git.remaining()
        report("complete", occurrences=closure.count)
        return {"path": path, "commit": item["commit"], "prepared": True,
                "recursive": recursive, "occurrences": closure.count, **totals}
    except BaseException as error:
        report("failed", error=error.code if isinstance(error, GitViewspaceError) else "PREPARATION_FAILED")
        raise


def pin(repository, branch, path, commit, message, identity):
    """Caller holds repository ownership and has excluded writable branch claims."""
    git = Git()
    base = git.resolve_local_branch(repository, branch)
    if git.checkout_branch(repository) == branch:
        raise GitViewspaceError("BRANCH_CHECKED_OUT", "pin on an unclaimed, non-checked-out branch")
    item = resolved_entries(repository, base, root_url(repository)).get(path)
    if item is None:
        raise GitViewspaceError("SUBMODULE_NOT_FOUND", "path is not a registered gitlink")
    _Closure(repository).walk({path: {**item, "commit": exact_oid(commit)}})
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
