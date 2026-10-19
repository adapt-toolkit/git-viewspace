#!/usr/bin/env python3
"""Small mount-enabled Stage 4 writable-view lifecycle check."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(arguments, *, cwd=None, timeout=40, check=True):
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
        (
            sys.executable,
            "-m",
            "git_viewspace",
            "--root",
            root,
            "--json",
            *arguments,
        ),
        check=False,
    )
    payload = json.loads(result.stdout) if result.stdout else None
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(arguments)} failed: {payload} {result.stderr}")
    return result, payload


def make_repository(path):
    path.mkdir()
    run(("git", "init", "--initial-branch", "main", "."), cwd=path)
    run(("git", "config", "user.name", "Stage 4 Host Test"), cwd=path)
    run(("git", "config", "user.email", "stage4@example.invalid"), cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / "keep.txt").write_text("keep\n", encoding="utf-8")
    run(("git", "add", "tracked.txt", "keep.txt"), cwd=path)
    run(("git", "commit", "-m", "base"), cwd=path)
    return run(("git", "rev-parse", "HEAD"), cwd=path).stdout.strip()


def assert_changed(mountpoint):
    if (mountpoint / "tracked.txt").read_text(encoding="utf-8") != "changed\n":
        raise RuntimeError("copied-up file did not persist")
    if (mountpoint / "created.txt").read_text(encoding="utf-8") != "created\n":
        raise RuntimeError("created file did not persist")
    if (mountpoint / "keep.txt").exists():
        raise RuntimeError("whiteout did not persist")


def assert_clean(mountpoint):
    if (mountpoint / "tracked.txt").read_text(encoding="utf-8") != "base\n":
        raise RuntimeError("clean did not restore lower file")
    if (mountpoint / "created.txt").exists():
        raise RuntimeError("clean retained an upper-only file")
    if (mountpoint / "keep.txt").read_text(encoding="utf-8") != "keep\n":
        raise RuntimeError("clean retained a whiteout")


def wait_recovery(root, prior_source, timeout=20.0):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        result, latest = command(root, "status", check=False)
        if (
            result.returncode == 0
            and latest["healthy"]
            and latest["viewfs"]["source"] != prior_source
            and latest["views"][0]["actual"] == "overlay"
        ):
            return latest
        time.sleep(0.1)
    raise RuntimeError(f"writable recovery timed out: {latest}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    arguments = parser.parse_args()
    scratch = Path(arguments.scratch).resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="stage4-", dir=scratch))
    source = workspace / "source"
    root = workspace / "root"
    daemon_started = False
    try:
        commit = make_repository(source)
        command(
            root,
            "init",
            "--git-name",
            "Stage 4 Host Test",
            "--git-email",
            "stage4@example.invalid",
            "--copy",
            source,
        )
        command(root, "start")
        daemon_started = True
        command(root, "view", "init", "readonly", "work", "--at", commit)
        _, overlay = command(root, "view", "init", "overlay", "work", "--branch", "main")
        first_generation = overlay["view"]["generation"]
        command(root, "view", "start", "work")
        mountpoint = root / "views/work/mnt"
        (mountpoint / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (mountpoint / "created.txt").write_text("created\n", encoding="utf-8")
        (mountpoint / "keep.txt").unlink()
        assert_changed(mountpoint)

        command(root, "view", "stop", "work")
        _, stopped = command(root, "view", "status", "work")
        if stopped["view"]["dirty"] is not True:
            raise RuntimeError("stopped writable view was not reported dirty")
        command(root, "view", "start", "work")
        assert_changed(mountpoint)

        _, before_recovery = command(root, "status")
        run(
            (
                "/usr/bin/sudo",
                "-n",
                "--",
                "/usr/bin/umount",
                "--",
                mountpoint,
                root / "git-mnt",
            )
        )
        recovered = wait_recovery(root, before_recovery["viewfs"]["source"])
        if recovered["viewfs"]["restart_count"] <= before_recovery["viewfs"]["restart_count"]:
            raise RuntimeError("git-viewspace-fs replacement was not recorded")
        if recovered["views"][0]["generation"] != first_generation:
            raise RuntimeError("writable recovery changed the active generation")
        assert_changed(mountpoint)

        command(root, "stop")
        daemon_started = False
        _, offline = command(root, "view", "status", "work")
        if offline["view"]["kind"] != "overlay" or offline["view"]["dirty"] is not True:
            raise RuntimeError("offline writable status lost overlay state")
        command(root, "start")
        daemon_started = True
        assert_changed(mountpoint)
        command(root, "view", "stop", "work")
        _, cleaned = command(root, "view", "clean", "work")
        if cleaned["view"]["generation"] == first_generation:
            raise RuntimeError("clean did not rotate the overlay generation")
        command(root, "view", "start", "work")
        assert_clean(mountpoint)
        command(root, "view", "stop", "work")
        command(root, "view", "deinit", "overlay", "work")
        command(root, "view", "start", "work")
        assert_clean(mountpoint)
        try:
            (mountpoint / "forbidden").write_text("no\n", encoding="utf-8")
        except OSError:
            pass
        else:
            raise RuntimeError("deinitialized view is not read-only")
        command(root, "view", "stop", "work")
        command(root, "view", "destroy", "work")
        command(root, "stop")
        daemon_started = False
        if any((root / "views").iterdir()) or any((root / "run").iterdir()):
            raise RuntimeError("Stage 4 cleanup left managed state")
        print(json.dumps({"ok": True, "commit": commit}))
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
