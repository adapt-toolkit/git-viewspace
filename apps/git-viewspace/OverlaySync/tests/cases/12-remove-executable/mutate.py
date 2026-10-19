#!/usr/bin/python3
"""Remove the executable class from an existing lower regular file."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "tool.sh").chmod(0o644)
