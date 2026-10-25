#!/usr/bin/env python3
"""Narrow routed namespace with full-repository checkpoint preservation."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

base = Path(tempfile.mkdtemp(prefix="gvsub-", dir=os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"]))
seed = base / "seed"
seed.mkdir()
(seed / "session/runtime").mkdir(parents=True)
(seed / "session/runtime/.git-viewspace-runtime").touch()
(seed / "session/history").write_text("before")
(seed / "data").write_text("outside unchanged")
external = base / "external"
external.write_text("not in git")
runtime = base / "runtime"
runtime.mkdir()
root = base / "vm"
exe = shutil.which("git-viewspace")

def run(*args):
    r = subprocess.run(list(map(str,args)), capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError((args,r.stdout,r.stderr))
    return r.stdout

def vm(*args):
    return run(exe,"--root",root,*args)

run("git","init","-b","main",seed)
run("git","-C",seed,"config","user.name","Test")
run("git","-C",seed,"config","user.email","test@example.invalid")
run("git","-C",seed,"add",".")
run("git","-C",seed,"commit","-m","fixture")
vm("init","--copy",seed,"--git-name","Test","--git-email","test@example.invalid")
try:
    vm("start")
    vm("view","init","readonly","session","--at","main")
    vm("view","init","overlay","session","--branch","main")
    vm("view","start","session")
    mount=root/"views/session/mnt"
    (mount/"uncommitted-outside").write_text("retain outside overlay")
    vm("view","stop","session")
    vm("view","routing","enable","session","--executable",os.environ["VIEWSPACE_ROUTER_BIN"],
       "--subtree","session","--runtime-dir",f"runtime={runtime}")
    vm("view","start","session")
    assert not (mount/"data").exists()
    assert (mount/"history").read_text()=="before"
    (mount/"history").write_text("after")
    pending=mount/"new-descriptor"
    pending.write_text(str(external)+"\n")
    pending.rename(mount/".git-viewspace-#SF#-auth")
    assert (mount/"auth").read_text()=="not in git"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(mount/"runtime/socket"))
        assert (runtime/"socket").is_socket()
    (runtime/"socket").unlink()
    vm("view","advance","session","--message","narrow changes")
    assert run("git","-C",root/"repo","show","main:data")=="outside unchanged"
    assert run("git","-C",root/"repo","show","main:uncommitted-outside")=="retain outside overlay"
    assert run("git","-C",root/"repo","show","main:session/history")=="after"
    names=run("git","-C",root/"repo","ls-tree","-r","--name-only","main").splitlines()
    assert "session/.git-viewspace-#SF#-auth" in names
    assert "session/auth" not in names
    assert not any(n.endswith("socket") for n in names)
    vm("view","init","readonly","data","--at","main")
    vm("view","start","data")
    assert (root/"views/data/mnt/data").read_text()=="outside unchanged"
    assert (mount/"history").read_text()=="after"
finally:
    vm("stop")
from git_viewspace.host import mounts
assert not any(m.mountpoint.is_relative_to(root) for m in mounts())
print(json.dumps({"narrow_routing":True,"full_tree_preserved":True,"runtime_prefix":True,"no_remaining_mounts":True}))
