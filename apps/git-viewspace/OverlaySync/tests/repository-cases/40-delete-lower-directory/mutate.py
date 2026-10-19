#!/usr/bin/python3
"""Delete a populated lower directory and its complete Git subtree."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
shutil.rmtree(root / "remove-me")
