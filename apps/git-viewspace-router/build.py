#!/usr/bin/env python3
"""Build the optional router; requires C++17, pkg-config and libfuse >=3.17 headers."""

import argparse
import os
from pathlib import Path
import shlex
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--cflags", help="override pkg-config cflags for an isolated SDK")
p.add_argument("--libs", help="override pkg-config libs for an isolated SDK")
a = p.parse_args()
cflags = (
    shlex.split(a.cflags)
    if a.cflags is not None
    else shlex.split(
        subprocess.check_output(["pkg-config", "--cflags", "fuse3"], text=True)
    )
)
libs = (
    shlex.split(a.libs)
    if a.libs is not None
    else shlex.split(
        subprocess.check_output(["pkg-config", "--libs", "fuse3"], text=True)
    )
)
a.output.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        os.environ.get("CXX", "c++"),
        "-std=c++17",
        "-O2",
        "-pthread",
        "-Wall",
        "-Wextra",
        *cflags,
        str(Path(__file__).with_name("router.cc")),
        *libs,
        "-lcrypto",
        "-o",
        str(a.output),
    ],
    check=True,
)
