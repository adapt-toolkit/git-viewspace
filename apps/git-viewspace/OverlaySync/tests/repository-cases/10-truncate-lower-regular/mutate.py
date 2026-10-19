#!/usr/bin/python3
"""Truncate an existing lower regular file to zero bytes."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "target.txt").write_bytes(b"")
