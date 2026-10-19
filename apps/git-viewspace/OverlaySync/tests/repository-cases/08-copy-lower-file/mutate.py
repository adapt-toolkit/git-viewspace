#!/usr/bin/python3
"""Copy an existing lower regular file to a new path."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
shutil.copy2(root / "source.txt", root / "copied.txt")
