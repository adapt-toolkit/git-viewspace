#!/usr/bin/python3
"""Create a dangling symbolic link with a relative target."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
os.symlink("missing/viewspace-overlay-target", root / "dangling-link")
