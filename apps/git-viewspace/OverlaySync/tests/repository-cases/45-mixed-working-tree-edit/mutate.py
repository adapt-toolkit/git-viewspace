#!/usr/bin/python3
"""Apply a compact batch of ordinary working-tree edits."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "README.txt").write_text("new readme\n", encoding="utf-8")
(root / "src" / "draft.txt").rename(root / "src" / "final.txt")
(root / "docs" / "old.txt").unlink()
(root / "docs" / "new.txt").write_text("new docs\n", encoding="utf-8")
(root / "bin" / "tool.sh").chmod(0o755)
(root / "current").unlink()
os.symlink("docs/new.txt", root / "current")
