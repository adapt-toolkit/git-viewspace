#!/usr/bin/python3
"""Delete one existing lower regular file."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "victim.txt").unlink()
