#!/usr/bin/python3
"""Replace a lower file using a same-directory editor temporary file."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
temporary = root / ".document.txt.editor-tmp"
temporary.write_text("new document written by an atomic editor\n", encoding="utf-8")
os.replace(temporary, root / "document.txt")
