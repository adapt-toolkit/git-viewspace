#!/usr/bin/python3
"""Create a regular file whose valid UTF-8 name is non-ASCII."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "café-雪.txt").write_text("Unicode filename payload\n", encoding="utf-8")
