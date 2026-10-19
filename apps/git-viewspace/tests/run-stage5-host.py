#!/usr/bin/env python3
"""Compact mount-enabled Stage 5 sync lifecycle check."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(arguments, *, cwd=None, timeout=120, check=True):
    return subprocess.run(
        tuple(map(str, arguments)),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        text=True,
        timeout=timeout,
    )


def command(root, *arguments, check=True):
    result = run(
        (sys.executable, "-m", "git_viewspace", "--root", root, "--json", *arguments),
        check=False,
    )
    payload = json.loads(result.stdout) if result.stdout else None
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(arguments)} failed: {payload} {result.stderr}")
    return result, payload


def make_repository(path):
    path.mkdir()
    run(("git", "init", "--initial-branch", "main", "."), cwd=path)
    run(("git", "config", "user.name", "Stage 5 Host Test"), cwd=path)
    run(("git", "config", "user.email", "stage5@example.invalid"), cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (path / "source").mkdir()
    (path / "source/kept.txt").write_text("kept\n", encoding="utf-8")
    (path / "source/changed.txt").write_text("old\n", encoding="utf-8")
    run(("git", "add", "."), cwd=path)
    run(("git", "commit", "-m", "base"), cwd=path)
    return run(("git", "rev-parse", "HEAD"), cwd=path).stdout.strip()


def assert_synced(checkout):
    if (checkout / "tracked.txt").read_text(encoding="utf-8") != "changed\n":
        raise RuntimeError("copied-up file was not synchronized")
    if (checkout / "created.txt").read_text(encoding="utf-8") != "created\n":
        raise RuntimeError("upper-only file was not synchronized")
    if (checkout / "deleted.txt").exists() or (checkout / "source").exists():
        raise RuntimeError("whiteout or redirect source was not synchronized")
    if (checkout / "moved/kept.txt").read_text(encoding="utf-8") != "kept\n":
        raise RuntimeError("redirected lower child was not synchronized")
    if (checkout / "moved/changed.txt").read_text(encoding="utf-8") != "changed child\n":
        raise RuntimeError("redirected modified child was not synchronized")
    if (checkout / "moved/new.txt").read_text(encoding="utf-8") != "new child\n":
        raise RuntimeError("redirected new child was not synchronized")
    for relative in ("created.txt", "moved/new.txt"):
        if (checkout / relative).lstat().st_uid != os.getuid():
            raise RuntimeError(f"replayed path is not workload-owned: {relative}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    arguments = parser.parse_args()
    scratch = Path(arguments.scratch).resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="stage5-", dir=scratch))
    source = workspace / "source-repository"
    root = workspace / "root"
    daemon_started = False
    try:
        commit = make_repository(source)
        command(
            root,
            "init",
            "--git-name",
            "Stage 5 Host Test",
            "--git-email",
            "stage5@example.invalid",
            "--copy",
            source,
        )
        command(root, "start")
        daemon_started = True
        command(root, "view", "init", "readonly", "work", "--at", commit)
        command(root, "view", "init", "overlay", "work", "--branch", "main")
        command(root, "view", "start", "work")
        mountpoint = root / "views/work/mnt"
        (mountpoint / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (mountpoint / "created.txt").write_text("created\n", encoding="utf-8")
        (mountpoint / "deleted.txt").unlink()
        (mountpoint / "source").rename(mountpoint / "moved")
        (mountpoint / "moved/changed.txt").write_text("changed child\n", encoding="utf-8")
        (mountpoint / "moved/new.txt").write_text("new child\n", encoding="utf-8")
        command(root, "view", "stop", "work")

        _, first = command(root, "view", "sync", "work")
        if first["sync"]["base_commit"] != commit or not first["sync"]["manifest"]:
            raise RuntimeError("sync did not publish its base-bound Git manifest")
        if first["sync"]["counts"]["whiteouts"] < 2:
            raise RuntimeError("sync did not classify expected whiteouts")
        assert_synced(root / "repo")
        if run(("git", "diff", "--cached", "--quiet"), cwd=root / "repo", check=False).returncode:
            raise RuntimeError("sync changed the real Git index")
        if run(("git", "rev-parse", "HEAD"), cwd=root / "repo").stdout.strip() != commit:
            raise RuntimeError("sync advanced the branch or checkout HEAD")

        command(root, "stop")
        daemon_started = False
        _, offline = command(root, "view", "status", "work")
        if offline["view"]["sync"]["sync_id"] != first["sync"]["sync_id"]:
            raise RuntimeError("offline status lost durable sync state")
        command(root, "start")
        daemon_started = True

        (root / "repo/created.txt").write_text("corrupted checkout\n", encoding="utf-8")
        (root / "repo/untracked.tmp").write_text("remove me\n", encoding="utf-8")
        _, repeated = command(root, "view", "sync", "work")
        if not repeated["idempotent"] or repeated["sync"] != first["sync"]:
            raise RuntimeError("repeat sync was not deterministic")
        assert_synced(root / "repo")
        if (root / "repo/untracked.tmp").exists():
            raise RuntimeError("repeat sync did not reset the checkout")

        _, cleaned = command(root, "view", "clean", "work")
        if cleaned["view"]["sync"] is not None:
            raise RuntimeError("clean retained a stale sync record")
        if run(("git", "status", "--porcelain"), cwd=root / "repo").stdout:
            raise RuntimeError("clean did not restore the singular checkout")
        command(root, "view", "destroy", "work")
        command(root, "stop")
        daemon_started = False
        print(json.dumps({"ok": True, "commit": commit, "manifest": first["sync"]["manifest"]}))
    finally:
        if daemon_started and root.exists():
            command(root, "stop", check=False)
        mounted = False
        if root.exists():
            prefix = str(root) + os.sep
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                target = line.split()[4].replace("\\040", " ")
                if target == str(root) or target.startswith(prefix):
                    mounted = True
                    break
        if not mounted:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
