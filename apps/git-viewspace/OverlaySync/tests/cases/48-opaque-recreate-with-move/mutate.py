#!/usr/bin/python3
"""Recreate a lower subtree and move another lower file into it."""

import os
from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
cache = root / "cache"
shutil.rmtree(cache)
fresh = cache / "fresh"
fresh.mkdir(parents=True)
(fresh / "new.txt").write_text("fresh child\n", encoding="utf-8")
(root / "inbox" / "move.txt").rename(fresh / "moved.txt")
(root / "remove.txt").unlink()
(root / "archive" / "added.txt").write_text("added archive file\n", encoding="utf-8")
os.symlink("fresh/moved.txt", cache / "latest")
