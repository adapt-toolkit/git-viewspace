#!/usr/bin/python3
"""Create a relative symbolic link."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
os.symlink("target.txt", root / "relative-link")
