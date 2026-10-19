#!/usr/bin/python3
"""Combine ordinary edits using names that Git pathspecs must treat literally."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
names = root / "names"
(names / "space name.txt").write_text("new spaced contents\n", encoding="utf-8")
subdirectory = names / "sub dir"
subdirectory.mkdir()
(names / "[draft].txt").rename(subdirectory / "final*.txt")
(names / "-leading.txt").unlink()
(names / "café.txt").rename(names / "renamed café.txt")
(names / "雪 report.txt").write_text("unicode report\n", encoding="utf-8")
os.symlink("雪 report.txt", names / "latest")
