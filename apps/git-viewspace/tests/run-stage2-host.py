#!/usr/bin/env python3
"""Small mount-enabled Stage 2 lifecycle check for a supported Linux host."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def command(root, operation, *arguments, check=True):
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "git_viewspace",
            "--root",
            str(root),
            "--json",
            operation,
            *arguments,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        timeout=40,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{operation} failed: {result.stdout} {result.stderr}")
    return result, json.loads(result.stdout) if result.stdout else None


def mount_source(mountpoint):
    selected = str(Path(mountpoint).resolve())
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        separator = fields.index("-")
        if fields[4].replace("\\040", " ") == selected:
            return fields[separator + 2].replace("\\040", " ")
    return None


def wait_status(root, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        result, observed = command(root, "status", check=False)
        latest = observed
        if result.returncode == 0 and predicate(observed):
            return latest
        time.sleep(0.1)
    raise RuntimeError(f"status condition timed out: {latest}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    arguments = parser.parse_args()
    scratch = Path(arguments.scratch).resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="stage2-", dir=scratch))
    root = workspace / "root"
    stopped = False
    try:
        command(
            root,
            "init",
            "--git-name",
            "Stage 2 Host Test",
            "--git-email",
            "stage2@example.invalid",
            "--empty",
        )
        _, started = command(root, "start")
        if started["idempotent"]:
            raise RuntimeError("first start was unexpectedly idempotent")
        initial = wait_status(root, lambda value: value["healthy"])
        initial_source = initial["viewfs"]["source"]
        if mount_source(root / "git-mnt") != initial_source:
            raise RuntimeError("private mount source does not match daemon status")
        if sorted(os.listdir(root / "git-mnt")) != ["views"]:
            raise RuntimeError("git-viewspace-fs private root is not ready")

        _, repeated = command(root, "start")
        if not repeated["idempotent"]:
            raise RuntimeError("second start was not idempotent")

        subprocess.run(
            ("/usr/bin/sudo", "-n", "--", "/usr/bin/umount", "--", str(root / "git-mnt")),
            check=True,
            timeout=20,
        )
        recovered = wait_status(
            root,
            lambda value: value["healthy"]
            and value["viewfs"]["restart_count"] >= 1
            and value["viewfs"]["source"] != initial_source,
        )
        if mount_source(root / "git-mnt") != recovered["viewfs"]["source"]:
            raise RuntimeError("recovered mount source does not match daemon status")

        _, stopped_result = command(root, "stop")
        stopped = True
        if stopped_result["idempotent"]:
            raise RuntimeError("running daemon stop was unexpectedly idempotent")
        _, offline = command(root, "status")
        if not offline["healthy"] or offline["daemon"]["running"]:
            raise RuntimeError("root is not cleanly stopped")
        if mount_source(root / "git-mnt") is not None:
            raise RuntimeError("private mount remains after stop")
        if any((root / "run").iterdir()):
            raise RuntimeError("runtime directory is not empty after stop")
        _, repeated_stop = command(root, "stop")
        if not repeated_stop["idempotent"]:
            raise RuntimeError("second stop was not idempotent")
        print(json.dumps({"ok": True, "root": str(root), "restart_count": recovered["viewfs"]["restart_count"]}))
    finally:
        if not stopped and root.exists():
            command(root, "stop", check=False)
        if mount_source(root / "git-mnt") is None:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
