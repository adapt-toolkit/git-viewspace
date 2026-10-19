#!/usr/bin/python3
"""Replace a lower symbolic link with a regular file."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
node = root / "node"
node.unlink()
node.write_text("replacement regular file\n", encoding="utf-8")
