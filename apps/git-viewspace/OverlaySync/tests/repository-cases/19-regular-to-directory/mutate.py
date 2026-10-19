#!/usr/bin/python3
"""Replace a lower regular file with a populated directory."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
node = root / "node"
node.unlink()
node.mkdir()
(node / "child.txt").write_text("leaf in replacement directory\n", encoding="utf-8")
