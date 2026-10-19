#!/usr/bin/env python3
"""Compact mount-enabled Stage 6/7 transition lifecycle check."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(arguments, *, cwd=None, check=True, timeout=180):
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
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(arguments)} failed: {payload} {result.stderr}")
    return payload


def make_repository(path):
    path.mkdir()
    run(("git", "init", "--initial-branch", "main", "."), cwd=path)
    run(("git", "config", "user.name", "Unmanaged Identity"), cwd=path)
    run(("git", "config", "user.email", "unmanaged@example.invalid"), cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / "deleted.txt").write_text("delete\n", encoding="utf-8")
    (path / "tree").mkdir()
    (path / "tree/kept.txt").write_text("kept\n", encoding="utf-8")
    run(("git", "add", "."), cwd=path)
    run(("git", "commit", "-m", "base"), cwd=path)
    return run(("git", "rev-parse", "HEAD"), cwd=path).stdout.strip()


def assert_no_mounts(root):
    prefix = str(root) + os.sep
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        target = line.split()[4].replace("\\040", " ")
        if target == str(root) or target.startswith(prefix):
            raise RuntimeError(f"manager mount remains: {target}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    arguments = parser.parse_args()
    scratch = Path(arguments.scratch).resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="stage7-", dir=scratch))
    source = workspace / "source"
    root = workspace / "root"
    daemon_started = False
    try:
        base = make_repository(source)
        command(
            root,
            "init",
            "--git-name",
            "Managed Stage 7",
            "--git-email",
            "managed-stage7@example.invalid",
            "--copy",
            source,
        )
        command(root, "start")
        daemon_started = True
        command(root, "view", "init", "readonly", "work", "--at", base)
        command(root, "view", "init", "overlay", "work", "--branch", "main")
        command(root, "view", "start", "work")
        mountpoint = root / "views/work/mnt"
        (mountpoint / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (mountpoint / "created.txt").write_text("created\n", encoding="utf-8")
        (mountpoint / "deleted.txt").unlink()
        (mountpoint / "tree").rename(mountpoint / "moved")
        (mountpoint / "moved/new.txt").write_text("new\n", encoding="utf-8")
        command(root, "view", "stop", "work")

        synchronized = command(root, "view", "sync", "work")["sync"]
        staged = command(root, "view", "stage", "work")["stage"]
        if staged["sync_id"] != synchronized["sync_id"]:
            raise RuntimeError("stage was not bound to sync")
        if run(("git", "write-tree"), cwd=root / "repo").stdout.strip() != staged["index_tree"]:
            raise RuntimeError("real index differs from durable stage")
        full_index = workspace / "full-stage.index"
        full_environment = {**os.environ, "GIT_INDEX_FILE": str(full_index)}
        subprocess.run(
            ("git", "read-tree", base),
            cwd=root / "repo",
            env=full_environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ("git", "--literal-pathspecs", "add", "-A", "--force", "--", "."),
            cwd=root / "repo",
            env=full_environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        full_tree = subprocess.run(
            ("git", "write-tree"),
            cwd=root / "repo",
            env=full_environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        if full_tree != staged["index_tree"]:
            raise RuntimeError("targeted staging differs from independent full staging")

        command(root, "stop")
        daemon_started = False
        offline = command(root, "view", "status", "work")["view"]
        if offline["stage"]["stage_id"] != staged["stage_id"]:
            raise RuntimeError("offline status lost stage")
        command(root, "start")
        daemon_started = True

        transition = command(
            root,
            "view",
            "commit",
            "work",
            "--message",
            "managed transition",
        )["next"]
        if not transition["changed"] or transition["base_commit"] != base:
            raise RuntimeError("changed commit transition was not recorded")
        commit = transition["commit"]
        ancestry = run(("git", "rev-list", "--parents", "-n", "1", commit), cwd=root / "repo").stdout.split()
        if ancestry != [commit, base]:
            raise RuntimeError("commit does not have the expected sole parent")
        identity = run(("git", "show", "-s", "--format=%an%n%ae", commit), cwd=root / "repo").stdout.splitlines()
        if identity != ["Managed Stage 7", "managed-stage7@example.invalid"]:
            raise RuntimeError("commit did not use the manager identity")

        command(root, "view", "clean", "work")
        pending = command(root, "view", "status", "work")["view"]
        if pending["sync"] is not None or pending["stage"] is not None or pending["next"]["commit"] != commit:
            raise RuntimeError("clean did not preserve only next")
        command(root, "view", "switch", "work", "--next")
        command(root, "view", "start", "work")
        if (mountpoint / "tracked.txt").read_text(encoding="utf-8") != "changed\n":
            raise RuntimeError("switched view does not expose committed content")
        if (mountpoint / "deleted.txt").exists() or not (mountpoint / "moved/kept.txt").is_file():
            raise RuntimeError("switched view tree is incorrect")
        command(root, "view", "stop", "work")

        before_count = int(run(("git", "rev-list", "--count", "main"), cwd=root / "repo").stdout)
        command(root, "view", "sync", "work")
        command(root, "view", "stage", "work")
        no_op = command(root, "view", "commit", "work", "--message", "no-op")["next"]
        if no_op["changed"] or no_op["commit"] != commit:
            raise RuntimeError("Git no-op created or selected another commit")
        command(root, "view", "clean", "work")
        command(root, "view", "switch", "work", "--next")
        after_count = int(run(("git", "rev-list", "--count", "main"), cwd=root / "repo").stdout)
        if after_count != before_count:
            raise RuntimeError("Git no-op created an empty commit")

        command(root, "view", "destroy", "work")
        command(root, "stop")
        daemon_started = False
        assert_no_mounts(root)
        print(json.dumps({"ok": True, "base": base, "commit": commit}))
    finally:
        if daemon_started and root.exists():
            command(root, "stop", check=False)
        try:
            assert_no_mounts(root)
        except RuntimeError:
            pass
        else:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
