#!/usr/bin/python3
"""Create and then delete a pure-upper regular file."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
temporary = root / "short-lived.txt"
temporary.write_text("this pure-upper file is removed immediately\n", encoding="utf-8")
temporary.unlink()
