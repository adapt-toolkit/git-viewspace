#!/usr/bin/env python3
"""Mounted origin-reset regression: daemon restart with changed lookup order.

Includes lower copyups, redirects with untouched children, whiteouts, hardlinks,
symlinks, mode changes, and user xattrs. Optional router alternates on/off.
Uses only a newly created disposable repository under --scratch.
"""

import subprocess, os, json, random, hashlib
from pathlib import Path
import argparse, sys, tempfile, shutil

parser = argparse.ArgumentParser()
parser.add_argument("--scratch", type=Path)
parser.add_argument("--router", type=Path)
parser.add_argument("--cycles", type=int, default=6)
args = parser.parse_args()
r = (
    args.scratch
    or Path(
        tempfile.mkdtemp(
            prefix="origin-", dir=os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"]
        )
    )
).resolve()
r.mkdir(parents=True, exist_ok=True)
if any(r.iterdir()):
    raise RuntimeError("scratch directory must be empty")
root = r / "vm"
repo = r / "seed"
exe = shutil.which("git-viewspace")
if exe is None:
    raise RuntimeError("git-viewspace must be available on PATH")


def run(a, **kw):
    return subprocess.run(
        list(map(str, a)), check=True, capture_output=True, text=True, timeout=60, **kw
    ).stdout


def vm(*a):
    return run([exe, "--root", root, *a])


if not repo.exists():
    repo.mkdir()
    run(["git", "init", "-b", "main", repo])
    run(["git", "-C", repo, "config", "user.name", "Test"])
    run(["git", "-C", repo, "config", "user.email", "test@example.invalid"])
    for i in range(20):
        (repo / f"dir{i}").mkdir()
        (repo / f"dir{i}/file").write_text(f"base-{i}")
    os.symlink("dir2", repo / "dir-symlink")
    os.symlink("mode", repo / "file-symlink")
    (repo / "dir0/untouched").write_text("lower-only")
    (repo / "dir0/nested").mkdir()
    (repo / "dir0/nested/leaf").write_text("nested-lower-only")
    (repo / "delete").write_text("gone")
    (repo / "mode").write_text("mode")
    (repo / "opaque").mkdir()
    (repo / "opaque/hidden").write_text("hidden")
    run(["git", "-C", repo, "add", "."])
    run(["git", "-C", repo, "commit", "-m", "fixture"])
    vm(
        "init",
        "--copy",
        repo,
        "--git-name",
        "Test",
        "--git-email",
        "test@example.invalid",
    )
    vm("start")
    vm("view", "init", "readonly", "test", "--at", "main")
    vm("view", "init", "overlay", "test", "--branch", "main")
    vm("view", "start", "test")
    m = root / "views/test/mnt"
    for i in range(10):
        (m / f"dir{i}/file").write_text(f"changed-{i}")
    os.link(m / "dir1/file", m / "copied-link")
    os.chown(m / "dir-symlink", os.getuid(), os.getgid(), follow_symlinks=False)
    os.chown(m / "file-symlink", os.getuid(), os.getgid(), follow_symlinks=False)
    (m / "dir0").rename(m / "renamed")
    (m / "renamed/nested").rename(m / "nested-moved")
    (m / "delete").unlink()
    (m / "mode").chmod(0o755)
    (m / "new").write_text("new")
    os.link(m / "new", m / "new-link")
    os.symlink("new", m / "sym")
    (m / "opaque/hidden").unlink()
    (m / "opaque").rmdir()
    (m / "opaque").mkdir()
    (m / "opaque/visible").write_text("visible")
    os.setxattr(m / "new", "user.test", b"keep")
    vm("view", "stop", "test")
    vm("stop")
m = root / "views/test/mnt"
rows = []
expected = {i: f'{"changed" if i<10 else "base"}-{i}' for i in range(20)}
try:
    for cycle in range(args.cycles):
        upper = next((root / "views/test/overlay/generations").iterdir()) / "upper"
        removed = []  # Origin reset is invoked automatically by manager start.
        vm("start")
        routed = bool(args.router and cycle % 2)
        vm(
            "view",
            "routing",
            "enable" if routed else "disable",
            "test",
            *(["--executable", args.router] if routed else []),
        )
        vm("view", "start", "test")
        indices = list(range(20))
        random.Random(cycle).shuffle(indices)
        for i in indices:
            folder = "renamed" if i == 0 else f"dir{i}"
            assert (m / folder / "file").read_text() == expected[i]
        assert not (m / "dir0").exists() and not (m / "delete").exists()
        assert list(p.name for p in (m / "opaque").iterdir()) == ["visible"]
        assert (m / "sym").read_text() == "new" and (
            m / "new-link"
        ).read_text() == "new"
        underlying = root / "views/test/routing/source" if routed else m
        assert (underlying / "new").stat().st_ino == (
            underlying / "new-link"
        ).stat().st_ino
        assert (m / "mode").stat().st_mode & 0o777 == 0o755
        assert os.getxattr(m / "new", "user.test") == b"keep"
        assert (underlying / "copied-link").stat().st_ino == (
            underlying / "dir1/file"
        ).stat().st_ino
        assert (m / "copied-link").read_text() == expected[1]
        assert (underlying / "copied-link").stat().st_nlink == 2
        assert (m / "renamed/untouched").read_text() == "lower-only"
        assert (m / "nested-moved/leaf").read_text() == "nested-lower-only"
        expected[1] = f"hardlink-cycle-{cycle}"
        (m / ("copied-link" if cycle % 2 else "dir1/file")).write_text(expected[1])
        assert (m / "dir1/file").read_text() == expected[1] and (
            m / "copied-link"
        ).read_text() == expected[1]
        assert (
            os.readlink(m / "dir-symlink") == "dir2"
            and os.readlink(m / "file-symlink") == "mode"
        )
        i = 10 + cycle % 10
        expected[i] = f"cycle-{cycle}"
        (m / f"dir{i}/file").write_text(expected[i])
        epoch = upper.parent / "origin-epoch.json"
        before_epoch = epoch.stat().st_mtime_ns
        for remount in range(3):
            vm("view", "stop", "test")
            vm("view", "start", "test")
            assert epoch.stat().st_mtime_ns == before_epoch, "same lower rescanned"
            # Change lookup order and exercise fresh lower lookups between mounts.
            for j in reversed(indices):
                folder = "renamed" if j == 0 else f"dir{j}"
                assert (m / folder / "file").read_text() == expected[j]
            assert (m / "nested-moved/leaf").read_text() == "nested-lower-only"
        vm("view", "stop", "test")
        vm("stop")
        rows.append(
            {"cycle": cycle, "routing": routed, "removed": removed, "result": "pass"}
        )
        print("cycle", cycle, "passed", flush=True)
        (r / "receipt.json").write_text(json.dumps(rows, indent=2) + "\n")
finally:
    vm("stop")
