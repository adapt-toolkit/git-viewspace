#!/usr/bin/env python3
"""Real recursive modules through installed CLI, FUSE, overlay and routing."""
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from tests.test_recursive_submodules import RecursiveSubmoduleTests
from git_viewspace.submodules import store_path


def main():
    fixture = RecursiveSubmoduleTests()
    fixture.setUp()
    root = fixture.base / "vm"
    exe = shutil.which("git-viewspace")
    if not exe:
        raise RuntimeError("installed git-viewspace required")
    receipt = {}

    def vm(*args, ok=True):
        result = subprocess.run([exe, "--root", str(root), "--json", *map(str, args)],
                                capture_output=True, text=True, timeout=180)
        value = json.loads(result.stdout)
        if ok and result.returncode:
            raise RuntimeError((args, value, result.stderr))
        if not ok and not result.returncode:
            raise AssertionError(("unexpected success", args, value))
        return value

    def protected(path):
        operations = [lambda: path.write_text("bad"), path.unlink,
                      lambda: path.rename(path.with_name("renamed"))]
        for operation in operations:
            try:
                operation()
            except OSError as error:
                assert error.errno in {errno.EROFS, errno.EACCES, errno.EPERM}, error
            else:
                raise AssertionError("nested code mutation succeeded")

    def started_view(name):
        return root / "views" / name / "mnt"

    live = False
    try:
        vm("init", "--copy", fixture.repo, "--git-name", "Test", "--git-email", "test@example.invalid")
        vm("start")
        live = True
        denied = vm("submodule", "prepare", "--at", "main", "--path", "code/library", ok=False)
        assert denied["error"]["code"] == "UNSUPPORTED_SUBMODULE"
        vm("submodule", "prepare", "--at", "main", "--path", "code/library", "--recursive")
        progress = json.loads((root / "run/submodule-prepare.json").read_text())
        assert progress["phase"] == "complete" and progress["occurrences"] == 3
        assert "url" not in json.dumps(progress)
        vm("view", "init-and-start", "overlay", "old", "--from-branch", "main", "--new-branch", "old-results")
        vm("branch-pin", "--branch", "next", "--commit", fixture.snapshot)
        vm("submodule", "prepare", "--at", "main", "--path", "code/library", "--commit", fixture.new_outer, "--recursive")
        vm("submodule", "pin", "--branch", "next", "--path", "code/library", "--commit", fixture.new_outer, "--message", "new recursive pins")
        vm("view", "init-and-start", "overlay", "new", "--from-branch", "next", "--new-branch", "new-results")
        vm("view", "init-and-start", "readonly", "readonly", "--at", "main")
        # Narrow routing still protects descendants of a top-level module.
        vm("view", "init", "readonly", "routed", "--at", "main")
        vm("branch-pin", "--branch", "routed-results", "--commit", fixture.snapshot)
        vm("view", "init", "overlay", "routed", "--branch", "routed-results")
        vm("view", "routing", "enable", "routed", "--executable", os.environ["VIEWSPACE_ROUTER_BIN"], "--subtree", "code")
        vm("view", "start", "routed")
        for name, expected in (("old", "old leaf\n"), ("new", "new leaf\n"), ("readonly", "old leaf\n"), ("routed", "old leaf\n")):
            relative = fixture.payload.removeprefix("code/") if name == "routed" else fixture.payload
            payload = started_view(name) / relative
            assert payload.read_text() == expected
            assert (payload.parent / "link").read_text() == expected
            assert (payload.parent / "exec").stat().st_mode & 0o111
            protected(payload)
            if name != "readonly":
                (started_view(name) / "result.txt").write_text(name)
                vm("view", "advance", name, "--message", "data only")
                assert payload.read_text() == expected
                branch = {"old": "old-results", "new": "new-results", "routed": "routed-results"}[name]
                listing = fixture.git(root / "repo", "ls-tree", "-r", branch)
                assert "160000 commit" in listing
                assert "deps/leaf/payload" not in listing
        receipt["recursive_old_new_readonly_routed_and_advance"] = True
        # Sources disappear: restart must use only acquired objects.
        for repo in (fixture.outer, fixture.middle, fixture.leaf):
            shutil.move(repo, repo.with_name(repo.name + "-offline"))
        stopped = vm("stop")
        live = False
        assert stopped["mounts_remaining"] == stopped["children_remaining"] == 0
        vm("start")
        live = True
        for name in ("old", "new", "readonly", "routed"):
            vm("view", "start", name)
        assert (started_view("old") / fixture.payload).read_text() == "old leaf\n"
        assert (started_view("new") / fixture.payload).read_text() == "new leaf\n"
        receipt["offline_restart_preserves_pins"] = True
        for name in ("old", "new", "readonly", "routed"):
            vm("view", "stop", name)
            vm("view", "destroy", name, "--discard")
        vm("view", "init", "readonly", "missing", "--at", "main")
        stopped = vm("stop")
        live = False
        leafstore = store_path(root / "repo", str(fixture.leaf))
        shutil.rmtree(leafstore)
        vm("start")
        live = True
        failed = vm("view", "start", "missing", ok=False)
        assert failed["error"]["code"] == "SUBMODULE_NOT_PREPARED", failed
        assert str(started_view("missing")) not in Path("/proc/self/mountinfo").read_text()
        receipt["missing_descendant_fails_before_mount"] = True
        vm("view", "destroy", "missing", "--discard")
    finally:
        if live:
            stopped = vm("stop")
            assert stopped["mounts_remaining"] == stopped["children_remaining"] == 0
        if str(root) in Path("/proc/self/mountinfo").read_text():
            raise RuntimeError(f"test mounts remain under {root}")
        receipt["no_remaining_mounts"] = True
        fixture.doCleanups()
    target = Path(os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"]) / "recursive-host-receipt.json"
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    tempfile.tempdir = os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"]
    main()
