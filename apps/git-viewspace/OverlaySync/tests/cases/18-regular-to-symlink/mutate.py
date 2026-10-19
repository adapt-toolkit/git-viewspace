#!/usr/bin/python3
"""Replace a lower regular file with a symbolic link."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "node").unlink()
os.symlink("unchanged.txt", root / "node")
