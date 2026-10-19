#!/usr/bin/python3
"""Create a regular file containing non-text bytes."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "binary.dat").write_bytes(b"\x00VIEWSPACE\xff\n\x80payload\x00")
