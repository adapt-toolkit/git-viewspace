#!/usr/bin/python3
"""Rename a populated lower directory over an empty lower directory."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "empty-destination" / "sentinel.txt").unlink()
(root / "source").rename(root / "empty-destination")
