#!/usr/bin/python3
"""Overwrite the bytes of an existing lower regular file."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "target.txt").write_bytes(b"new replacement bytes\nwith a second line\n")
