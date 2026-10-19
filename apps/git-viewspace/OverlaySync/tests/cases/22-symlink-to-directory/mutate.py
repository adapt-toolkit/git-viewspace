#!/usr/bin/python3
"""Replace a lower symbolic link with a populated directory."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
node = root / "node"
node.unlink()
node.mkdir()
(node / "child.txt").write_text("leaf in directory replacing symlink\n", encoding="utf-8")
