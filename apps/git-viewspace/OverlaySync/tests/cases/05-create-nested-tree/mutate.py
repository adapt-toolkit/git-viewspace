#!/usr/bin/python3
"""Create a small populated tree several directories deep."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
deep = root / "new-tree" / "branch" / "deep"
deep.mkdir(parents=True)
(deep / "leaf.txt").write_text("deep new leaf\n", encoding="utf-8")
(root / "new-tree" / "root-leaf.txt").write_text("shallow new leaf\n", encoding="utf-8")
