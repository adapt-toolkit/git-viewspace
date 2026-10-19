#!/usr/bin/python3
"""Delete one existing lower symbolic link."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "link-to-delete").unlink()
