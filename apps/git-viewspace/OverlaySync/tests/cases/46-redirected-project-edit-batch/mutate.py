#!/usr/bin/python3
"""Move a lower project and apply several small edits below its redirect."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
project = root / "workspace" / "project"
(root / "incoming" / "project").rename(project)
(project / "change.txt").write_text("new change\n", encoding="utf-8")
(project / "delete.txt").unlink()
(project / "added.txt").write_text("added child\n", encoding="utf-8")
(project / "nested").mkdir()
(project / "nested" / "new.txt").write_text("nested child\n", encoding="utf-8")
os.symlink("change.txt", project / "current")
