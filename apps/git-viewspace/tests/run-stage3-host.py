#!/usr/bin/env python3
"""Small mount-enabled Stage 3 read-only-view lifecycle check."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(arguments, *, cwd=None, timeout=40):
    return subprocess.run(
        tuple(map(str, arguments)),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
        timeout=timeout,
    )


def command(root, *arguments, check=True):
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "git_viewspace",
            "--root",
            str(root),
            "--json",
            *arguments,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        timeout=40,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(arguments)} failed: {result.stdout} {result.stderr}")
    return result, json.loads(result.stdout) if result.stdout else None


def make_repository(path):
    path.mkdir()
    run(("git", "init", "--initial-branch", "main", "."), cwd=path)
    run(("git", "config", "user.name", "Stage 3 Host Test"), cwd=path)
    run(("git", "config", "user.email", "stage3@example.invalid"), cwd=path)
    (path / "version.txt").write_text("version one\n", encoding="utf-8")
    run(("git", "add", "version.txt"), cwd=path)
    run(("git", "commit", "-m", "version one"), cwd=path)
    first = run(("git", "rev-parse", "HEAD"), cwd=path).stdout.strip()
    (path / "version.txt").write_text("version two\n", encoding="utf-8")
    (path / "second.txt").write_text("second commit\n", encoding="utf-8")
    run(("git", "add", "version.txt", "second.txt"), cwd=path)
    run(("git", "commit", "-m", "version two"), cwd=path)
    second = run(("git", "rev-parse", "HEAD"), cwd=path).stdout.strip()
    return first, second


def assert_view(root, name, version, second_present):
    mountpoint = root / "views" / name / "mnt"
    if (mountpoint / "version.txt").read_text(encoding="utf-8") != version:
        raise RuntimeError(f"{name} exposed the wrong commit")
    if (mountpoint / "second.txt").exists() != second_present:
        raise RuntimeError(f"{name} second-file visibility is wrong")
    try:
        (mountpoint / "forbidden").write_text("must fail\n", encoding="utf-8")
    except OSError:
        pass
    else:
        raise RuntimeError(f"{name} public mount is writable")


def wait_recovery(root, view_name, prior_source, timeout=20.0):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        result, latest = command(root, "status", check=False)
        if (
            result.returncode == 0
            and latest["healthy"]
            and latest["viewfs"]["source"] == prior_source
            and next(view for view in latest["views"] if view["name"] == view_name)["actual"]
            == "readonly"
            and all(view["actual"] == "readonly" for view in latest["views"])
        ):
            return latest
        time.sleep(0.1)
    raise RuntimeError(f"view recovery timed out: {latest}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    arguments = parser.parse_args()
    scratch = Path(arguments.scratch).resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="stage3-", dir=scratch))
    source = workspace / "source"
    root = workspace / "root"
    daemon_started = False
    try:
        first, second = make_repository(source)
        command(
            root,
            "init",
            "--git-name",
            "Stage 3 Host Test",
            "--git-email",
            "stage3@example.invalid",
            "--copy",
            str(source),
        )
        command(root, "start")
        daemon_started = True
        command(root, "view", "init", "readonly", "one", "--at", first)
        _, two_created = command(root, "view", "init", "readonly", "two", "--at", first)
        command(root, "view", "start", "one")
        command(root, "view", "start", "two")
        assert_view(root, "one", "version one\n", False)
        assert_view(root, "two", "version one\n", False)

        _, before_recovery = command(root, "status")
        run(
            (
                "/usr/bin/sudo",
                "-n",
                "--",
                "/usr/bin/umount",
                "--",
                root / "views/one/mnt",
            )
        )
        recovered = wait_recovery(root, "one", before_recovery["viewfs"]["source"])
        if recovered["viewfs"]["restart_count"] != before_recovery["viewfs"]["restart_count"]:
            raise RuntimeError("public-bind recovery unnecessarily restarted git-viewspace-fs")
        assert_view(root, "one", "version one\n", False)
        assert_view(root, "two", "version one\n", False)

        command(root, "stop")
        daemon_started = False
        command(root, "start")
        daemon_started = True
        assert_view(root, "one", "version one\n", False)
        assert_view(root, "two", "version one\n", False)

        command(root, "view", "stop", "one")
        _, switched = command(root, "view", "switch", "one", "--to", second)
        command(root, "view", "start", "one")
        assert_view(root, "one", "version two\n", True)
        assert_view(root, "two", "version one\n", False)
        # git-viewspace-fs pins aliases after first resolution for the lifetime of
        # its process.  Retired aliases may therefore remain in this private
        # namespace, but the two currently configured aliases must exist and
        # public behavior above must reflect their exact commits.
        private_aliases = set(os.listdir(root / "git-mnt/views"))
        configured_aliases = {switched["view"]["alias"], two_created["view"]["alias"]}
        if not configured_aliases.issubset(private_aliases):
            raise RuntimeError("current aliases are missing from git-viewspace-fs")

        _, listed = command(root, "view", "status")
        if [view["name"] for view in listed["views"]] != ["one", "two"]:
            raise RuntimeError("view status did not list both views")
        command(root, "view", "stop", "one")
        command(root, "view", "destroy", "one")
        command(root, "view", "stop", "two")
        command(root, "view", "destroy", "two")
        if json.loads((root / "config/viewfs.json").read_text(encoding="utf-8")):
            raise RuntimeError("destroyed views remain in durable git-viewspace-fs configuration")
        command(root, "stop")
        daemon_started = False
        if any((root / "views").iterdir()) or any((root / "run").iterdir()):
            raise RuntimeError("Stage 3 cleanup left managed runtime or view state")
        print(json.dumps({"ok": True, "first": first, "second": second}))
    finally:
        if daemon_started and root.exists():
            command(root, "stop", check=False)
        mounted = False
        if root.exists():
            prefix = str(root) + os.sep
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                target = fields[4].replace("\\040", " ")
                if target == str(root) or target.startswith(prefix):
                    mounted = True
                    break
        if not mounted:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
