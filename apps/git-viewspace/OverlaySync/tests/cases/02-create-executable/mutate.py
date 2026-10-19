#!/usr/bin/python3
"""Create a new executable regular file."""

from pathlib import Path
import sys


root = Path(sys.argv[1])
created = root / "created-tool.sh"
created.write_text("#!/bin/sh\nprintf 'created executable\\n'\n", encoding="utf-8")
created.chmod(0o755)
