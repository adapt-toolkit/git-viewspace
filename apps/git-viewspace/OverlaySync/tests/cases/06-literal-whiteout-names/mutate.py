#!/usr/bin/python3
"""Create ordinary files whose names resemble OCI whiteout markers."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / ".wh.visible").write_text("ordinary dot-wh file\n", encoding="utf-8")
(root / ".wh..wh..opq").write_text("ordinary opaque-like file\n", encoding="utf-8")
