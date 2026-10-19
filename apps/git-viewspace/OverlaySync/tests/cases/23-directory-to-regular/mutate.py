#!/usr/bin/python3
"""Replace a populated lower directory with a regular file."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
node = root / "node"
shutil.rmtree(node)
node.write_text("regular file replacing directory\n", encoding="utf-8")
