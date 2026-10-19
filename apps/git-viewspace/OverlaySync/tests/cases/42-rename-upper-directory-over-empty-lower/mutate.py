#!/usr/bin/python3
"""Rename a new populated upper directory over an empty lower directory."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "empty-destination" / "sentinel.txt").unlink()
created = root / "created"
created.mkdir()
(created / "child.txt").write_text("upper child\n", encoding="utf-8")
created.rename(root / "empty-destination")
