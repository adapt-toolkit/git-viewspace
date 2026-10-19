#!/usr/bin/python3
"""Delete a populated lower directory and recreate it with new contents."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
replaced = root / "replaced"
shutil.rmtree(replaced)
(replaced / "fresh").mkdir(parents=True)
(replaced / "new.txt").write_text("new root-level leaf\n", encoding="utf-8")
(replaced / "fresh" / "new.txt").write_text("new nested leaf\n", encoding="utf-8")
