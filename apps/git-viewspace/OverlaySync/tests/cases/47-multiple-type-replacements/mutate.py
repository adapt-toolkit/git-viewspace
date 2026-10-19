#!/usr/bin/python3
"""Apply four independent Git-relevant inode type transitions."""

import os
from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
regular_node = root / "regular-node"
regular_node.unlink()
os.symlink("target.txt", regular_node)

link_node = root / "link-node"
link_node.unlink()
link_node.write_text("link becoming regular\n", encoding="utf-8")

directory_node = root / "directory-node"
shutil.rmtree(directory_node)
directory_node.write_text("directory becoming regular\n", encoding="utf-8")

second_file = root / "second-file"
second_file.unlink()
second_file.mkdir()
(second_file / "child.txt").write_text("regular becoming directory\n", encoding="utf-8")
