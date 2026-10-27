#!/usr/bin/env python3
"""Real routed-file lifecycle and ordinary-I/O acceptance in a disposable root."""

import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import time
import subprocess
import tempfile

root = Path(
    tempfile.mkdtemp(prefix="gvr-", dir=os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"])
)
seed = root / "seed"
seed.mkdir()
external = root / "external"
external.write_text("external baseline")
(seed / ".git-viewspace-#SF#-credential").write_text(str(external) + "\n")
(seed / "ordinary").write_text("baseline")
name_limit = os.pathconf(seed, "PC_NAME_MAX")
long_name = "l" * name_limit
(seed / long_name).write_text("long ordinary baseline")
exe = shutil.which("git-viewspace")
if exe is None:
    raise RuntimeError("git-viewspace must be on PATH")
vmroot = root / "vm"
receipt = {}


def run(*args):
    result = subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError((args, result.stdout, result.stderr))
    return result.stdout


def vm(*args):
    return run(exe, "--root", vmroot, *args)


run("git", "init", "-b", "main", seed)
run("git", "-C", seed, "config", "user.name", "Test")
run("git", "-C", seed, "config", "user.email", "test@example.invalid")
run("git", "-C", seed, "add", ".")
run("git", "-C", seed, "commit", "-m", "fixture")
vm("init", "--copy", seed, "--git-name", "Test", "--git-email", "test@example.invalid")
try:
    vm("start")
    vm("view", "init", "readonly", "test", "--at", "main")
    vm("view", "init", "overlay", "test", "--branch", "main")
    vm(
        "view",
        "routing",
        "enable",
        "test",
        "--executable",
        os.environ["VIEWSPACE_ROUTER_BIN"],
    )
    vm("view", "start", "test")
    mount = vmroot / "views/test/mnt"
    # A valid ordinary basename can be too long to have a descriptor sibling.
    # Synthetic lookup failure must not hide it or prevent ordinary mutations.
    long_file = mount / long_name
    assert long_file.stat().st_size == len("long ordinary baseline")
    assert long_file.read_text() == "long ordinary baseline"
    long_file.write_text("updated long name")
    assert long_file.read_text() == "updated long name"
    moved = mount / ("m" * name_limit)
    long_file.rename(moved)
    assert moved.read_text() == "updated long name"
    moved.unlink()
    long_file.write_text("new long file")
    assert long_file.read_text() == "new long file"
    long_file.unlink()
    long_directory = mount / ("d" * name_limit)
    long_directory.mkdir()
    (long_directory / "child").write_text("nested")
    assert (long_directory / "child").read_text() == "nested"
    (long_directory / "child").unlink()
    long_directory.rmdir()
    try:
        (mount / ("x" * (name_limit + 1))).write_text("invalid basename")
    except OSError as error:
        assert error.errno == errno.ENAMETOOLONG
    else:
        raise AssertionError("invalid ordinary basename unexpectedly accepted")
    receipt["ordinary_name_max_preserved"] = True
    # Dynamic declarations: both names visible; only atomic install is allowed.
    descriptor = mount / ".git-viewspace-#SF#-dynamic"
    second = root / "second-external"
    second.write_text("second backing")
    def install(text):
        pending = mount / "pending-descriptor"
        pending.write_text(text)
        pending.rename(descriptor)
    def rejected(action, codes):
        try:
            action()
        except OSError as error:
            assert error.errno in codes, error
        else:
            raise AssertionError("operation unexpectedly accepted")
    install(str(external) + "\n")
    assert {descriptor.name, "dynamic"} <= set(os.listdir(mount))
    assert descriptor.read_text() == str(external) + "\n"
    rejected(lambda: descriptor.write_text(str(second)), {errno.EPERM, errno.EACCES})
    with (mount / "dynamic").open() as old:
        install(str(second) + "\n")
        assert (mount / "dynamic").read_text() == "second backing"
        assert old.read() == "external baseline"
    rejected(lambda: install("not an absolute target"), {errno.EINVAL})
    assert (mount / "dynamic").read_text() == "second backing"
    (mount / "pending-descriptor").unlink()
    pending = mount / "pending-descriptor"
    pending.write_text(str(external) + "\n")
    with pending.open("r+"):
        rejected(lambda: pending.rename(descriptor), {errno.EBUSY})
    os.link(pending, mount / "alias")
    rejected(lambda: pending.rename(descriptor), {errno.EBUSY})
    (mount / "alias").unlink()
    pending.unlink()
    with (mount / "dynamic").open() as old:
        (mount / "dynamic").unlink()
        assert "deleted" in descriptor.read_text()
        assert not (mount / "dynamic").exists()
        assert "dynamic" not in os.listdir(mount)
        assert old.read() == "second backing"
        assert second.read_text() == "second backing"
    vm("view", "stop", "test")
    vm("view", "start", "test")
    assert not (mount / "dynamic").exists()
    assert descriptor.exists()
    install(str(second) + "\n")
    assert (mount / "dynamic").read_text() == "second backing"
    descriptor.unlink()
    assert not (mount / "dynamic").exists()
    assert second.read_text() == "second backing"
    receipt["dynamic_descriptor_lifecycle"] = True
    pages = mount / "pages"
    pages.mkdir()
    expected = set()
    for i in range(700):
        leaf = f"item-{i:04d}"
        temp = pages / "pending"
        temp.write_text(str(second) + "\n")
        desc = ".git-viewspace-#SF#-" + leaf
        temp.rename(pages / desc)
        expected.update((leaf, desc))
    assert set(os.listdir(pages)) == expected
    # Readdir must not admit/open external targets. All declarations point to the
    # same target, which is deliberately absent during enumeration.
    second.rename(root / "second-moved")
    assert set(os.listdir(pages)) == expected
    for item in pages.iterdir():
        if item.name.startswith(".git-viewspace-#SF#-"):
            item.unlink()
    pages.rmdir()
    receipt["paginated_listing_without_external_access"] = True
    before = external.stat().st_ino
    (mount / "credential").write_text("external update")
    assert external.read_text() == "external update"
    assert external.stat().st_ino == before
    # Distinct read/write handles, copy-up, rename and unlink must retain the
    # original open object's identity after the pathname has been reused.
    with (
        (mount / "ordinary").open("rb") as reader,
        (mount / "ordinary").open("r+b") as writer,
    ):
        writer.write(b"changed!")
        writer.flush()
        assert reader.read() == b"changed!"
        (mount / "ordinary").rename(mount / "renamed")
        (mount / "renamed").unlink()
        (mount / "ordinary").write_text("new inode")
        reader.seek(0)
        assert reader.read() == b"changed!"
    receipt["copy_up_and_open_handle_identity"] = True
    # POSIX record locks are intentionally FUSE-local, not backing-file locks.
    with (mount / "credential").open("r+") as routed:
        fcntl.lockf(routed, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(
            "python3",
            "-c",
            'import fcntl,sys; f=open(sys.argv[1],"r+"); fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
            external,
        )
        try:
            fcntl.flock(routed, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            assert error.errno == errno.EOPNOTSUPP
        else:
            raise AssertionError("special-file flock unexpectedly supported")
    receipt["external_posix_lock_boundary"] = True
    vm("view", "advance", "test", "--message", "accepted ordinary output")
    names = run(
        "git", "-C", vmroot / "repo", "ls-tree", "-r", "--name-only", "main"
    ).splitlines()
    assert names == [".git-viewspace-#SF#-credential", "ordinary"], names
    receipt["external_bytes_excluded"] = True
    external.rename(root / "old-external")
    external.write_text("replacement")
    for restart in (False, True):
        if restart:
            vm("view", "stop", "test")
            vm("view", "start", "test")
        try:
            (mount / "credential").read_text()
        except OSError as error:
            assert error.errno == errno.ESTALE
        else:
            raise AssertionError("identity replacement did not remain faulted")
    vm("view", "stop", "test")
    vm("view", "routing", "rebind", "test")
    vm("view", "start", "test")
    assert (mount / "credential").read_text() == "replacement"
    receipt["persistent_fault_and_explicit_rebind"] = True
    # Prevent creation of the fault latch, then replace the backing inode. A
    # failed durable latch must terminate the helper, not silently lose the fault.
    state = vmroot / "views/test/routing"
    state.chmod(0o500)
    try:
        external.rename(root / "second-old-external")
        external.write_text("replacement after failed latch")
        try:
            (mount / "credential").read_text()
        except OSError as error:
            assert error.errno in (errno.ENOTCONN, errno.ECONNABORTED, errno.EIO), error
        else:
            raise AssertionError("failed latch did not stop router")
    finally:
        state.chmod(0o700)
    # Kill only this disposable manager, leaving its disconnected mount for
    # startup reconciliation. No live manager is available to clean it first.
    runtime = json.loads((vmroot / "run/runtime.json").read_text())
    pid = runtime["daemon"]["pid"]
    from git_viewspace.host import process_matches

    assert process_matches(pid, runtime["daemon"]["start_time_ticks"])
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while (
        process_matches(pid, runtime["daemon"]["start_time_ticks"])
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    vm("start")
    assert (state / "uncertain").exists()
    rejected = subprocess.run(
        [exe, "--root", str(vmroot), "view", "start", "test"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        rejected.returncode != 0 and "ROUTER_RECOVERY_REQUIRED" in rejected.stderr
    ), rejected.stderr
    vm("view", "routing", "rebind", "test")
    vm("view", "start", "test")
    assert (mount / "credential").read_text() == "replacement after failed latch"
    receipt["failed_fault_persistence_requires_rebind"] = True
finally:
    vm("stop")
from git_viewspace.host import mounts as host_mounts

assert not any(m.mountpoint.is_relative_to(vmroot) for m in host_mounts())
receipt["no_remaining_mounts"] = True
receipt["scratch"] = str(root)
(root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt))
