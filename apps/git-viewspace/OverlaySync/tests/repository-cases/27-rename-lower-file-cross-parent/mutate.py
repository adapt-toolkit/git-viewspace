from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "from" / "moving.txt").rename(root / "to" / "moved.txt")
