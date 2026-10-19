from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "source.txt").rename(root / "destination.txt")
