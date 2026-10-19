#!/usr/bin/python3
"""Replace a populated lower directory with a symbolic link."""

import os
from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
node = root / "node"
shutil.rmtree(node)
os.symlink("destination.txt", node)
