#!/usr/bin/env python3
"""Real mount qualification, using disposable data and the public VM CLI only."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_submodules import SubmoduleTests


def main():
    fixture = SubmoduleTests()
    fixture.setUp()
    root = fixture.base / "r"

    def vm(*args, ok=True):
        result = subprocess.run(
            [sys.executable, "-m", "git_viewspace", "--root", str(root), "--json", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        value = json.loads(result.stdout)
        if ok and result.returncode:
            raise RuntimeError(f"{args}: {value}")
        return value

    started = False
    try:
        vm(
            "init",
            "--copy",
            str(fixture.repo),
            "--git-name",
            "Test",
            "--git-email",
            "test@example.invalid",
        )
        vm("start")
        started = True
        vm("submodule", "prepare", "--at", "main", "--path", "code/library")
        vm(
            "view",
            "init-and-start",
            "overlay",
            "one",
            "--from-branch",
            "main",
            "--new-branch",
            "out-one",
        )
        vm("branch-pin", "--branch", "next", "--commit", fixture.snapshot)
        vm(
            "submodule",
            "prepare",
            "--at",
            "main",
            "--path",
            "code/library",
            "--commit",
            fixture.second,
        )
        vm(
            "submodule",
            "pin",
            "--branch",
            "next",
            "--path",
            "code/library",
            "--commit",
            fixture.second,
            "--message",
            "new code",
        )
        vm(
            "view",
            "init-and-start",
            "overlay",
            "two",
            "--from-branch",
            "next",
            "--new-branch",
            "out-two",
        )
        for name, expected in (("one", "first\n"), ("two", "second\n")):
            path = root / "views" / name / "mnt"
            assert (path / "code/library/script.py").read_text() == expected
            try:
                (path / "code/library/script.py").write_text("bad")
            except OSError:
                pass
            else:
                raise AssertionError("code was writable")
            (path / "result.txt").write_text(name)
            vm("view", "advance", name, "--message", "target result")
            assert (path / "code/library/script.py").read_text() == expected
        refused = vm(
            "submodule",
            "pin",
            "--branch",
            "out-one",
            "--path",
            "code/library",
            "--commit",
            fixture.second,
            "--message",
            "wrong owner",
            ok=False,
        )
        assert refused["error"]["code"] == "BRANCH_CLAIMED"
        for name in ("one", "two"):
            vm("view", "stop", name)
            vm("view", "destroy", name, "--discard")
        result = vm("stop")
        started = False
        assert result["mounts_remaining"] == result["children_remaining"] == 0
        print(
            "submodule host smoke passed: two immutable pins, read-only code, "
            "target commits, ownership, teardown"
        )
    finally:
        if started:
            vm("stop")
        # Do not erase diagnostic evidence while any mount remains.
        mountinfo = Path("/proc/self/mountinfo").read_text()
        if str(root) not in mountinfo:
            fixture.doCleanups()
        else:
            raise RuntimeError(f"test mounts remain beneath {root}")


if __name__ == "__main__":
    # Short per-workstream TMPDIR avoids Linux's Unix-socket path length ceiling.
    os.environ["TMPDIR"] = os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"]
    main()
