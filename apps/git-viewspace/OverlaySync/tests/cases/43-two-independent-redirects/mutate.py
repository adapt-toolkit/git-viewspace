#!/usr/bin/python3
"""Rename two unrelated lower directories in one stopped upper."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "left" / "source").rename(root / "destinations" / "left-moved")
(root / "right" / "source").rename(root / "destinations" / "right-moved")
