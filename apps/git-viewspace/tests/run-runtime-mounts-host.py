#!/usr/bin/env python3
"""Manager-owned runtime mount acceptance using a disposable repository."""

from pathlib import Path
import subprocess, tempfile, os, socket, json, errno
import shutil

r = Path(
    tempfile.mkdtemp(prefix="gvrt-", dir=os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"])
)
root = r / "vm"
seed = r / "seed"
external = r / "external"
seed.mkdir()
external.mkdir()
(seed / "session/runtime").mkdir(parents=True)
(seed / "session/runtime/.git-viewspace-runtime").touch()
(seed / "file").write_text("base")
exe = shutil.which("git-viewspace")
if exe is None:
    raise RuntimeError("git-viewspace must be available on PATH")
router = os.environ["VIEWSPACE_ROUTER_BIN"]


def run(a):
    p = subprocess.run(list(map(str, a)), capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError((a, p.stdout, p.stderr))
    return p.stdout


def vm(*a):
    return run([exe, "--root", root, *a])


run(["git", "init", "-b", "main", seed])
run(["git", "-C", seed, "config", "user.name", "Test"])
run(["git", "-C", seed, "config", "user.email", "test@example.invalid"])
run(["git", "-C", seed, "add", "."])
run(["git", "-C", seed, "commit", "-m", "runtime fixture"])
vm("init", "--copy", seed, "--git-name", "Test", "--git-email", "test@example.invalid")
rows = {}
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
        router,
        "--runtime-dir",
        f"session/runtime={external}",
    )
    vm("view", "start", "test")
    m = root / "views/test/mnt"
    (m / "session/runtime/ephemeral").write_text("never-git")
    assert (external / "ephemeral").read_text() == "never-git"
    for bound, connected in [
        (m / "session/runtime/a.sock", external / "a.sock"),
        (external / "b.sock", m / "session/runtime/b.sock"),
    ]:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(bound))
            server.listen(1)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(str(connected))
                conn, _ = server.accept()
                with conn:
                    client.sendall(b"test")
                    assert conn.recv(4) == b"test"
        bound.unlink()
    rows["sockets_both_directions"] = True
    try:
        (m / "session/runtime").rename(m / "moved")
    except OSError as e:
        assert e.errno in (errno.EPERM, errno.EBUSY)
    else:
        raise AssertionError("runtime directory rename allowed")
    try:
        (m / "session").rename(m / "moved-session")
    except OSError as e:
        assert e.errno == errno.EPERM
    else:
        raise AssertionError("runtime ancestor rename allowed")
    rows["ancestor_rename_rejected"] = True
    directory_fd = os.open(m, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    rows["directory_fsync"] = True
    (m / "file").write_text("changed")
    vm("view", "advance", "test", "--message", "runtime exclusion")
    names = run(
        ["git", "-C", root / "repo", "ls-tree", "-r", "--name-only", "main"]
    ).splitlines()
    assert names == ["file", "session/runtime/.git-viewspace-runtime"], names
    rows["runtime_excluded_from_commit"] = True
    vm("view", "stop", "test")
    vm("stop")
    vm("start")
    vm("view", "start", "test")
    assert (m / "session/runtime/ephemeral").read_text() == "never-git"
    rows["restart"] = True
    vm("view", "stop", "test")
    vm("view", "init", "readonly", "ro", "--at", "main")
    vm(
        "view",
        "routing",
        "enable",
        "ro",
        "--executable",
        router,
        "--runtime-dir",
        f"session/runtime={external}",
    )
    vm("view", "start", "ro")
    ro = root / "views/ro/mnt"
    try:
        (ro / "session/runtime/ephemeral").write_text("bad")
    except OSError as e:
        assert e.errno == errno.EROFS
    else:
        raise AssertionError("readonly runtime writable")
    assert (external / "ephemeral").read_text() == "never-git"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(external / "ro.sock"))
        server.listen(1)
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(ro / "session/runtime/ro.sock"))
            conn, _ = server.accept()
            conn.close()
    (external / "ro.sock").unlink()
    rows["readonly_filesystem_and_socket_connect"] = True
    vm("view", "stop", "ro")
finally:
    vm("stop")
from git_viewspace.host import mounts as host_mounts

assert not any(m.mountpoint.is_relative_to(root) for m in host_mounts())
rows["no_remaining_mounts"] = True
rows["scratch"] = str(r)
(r / "receipt.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps(rows))
