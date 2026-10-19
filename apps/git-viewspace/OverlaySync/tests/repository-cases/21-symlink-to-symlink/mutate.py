#!/usr/bin/python3
"""Replace a lower symbolic link with a differently targeted link."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
node = root / "node"
node.unlink()
os.symlink("second.txt", node)
