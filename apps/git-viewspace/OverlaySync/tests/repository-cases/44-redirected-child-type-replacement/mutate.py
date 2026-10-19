#!/usr/bin/python3
"""Replace a regular child beneath a redirected directory with a symlink."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
redirected = root / "collection" / "renamed"
(root / "collection" / "source").rename(redirected)
(redirected / "node.txt").unlink()
os.symlink("keep.txt", redirected / "node.txt")
