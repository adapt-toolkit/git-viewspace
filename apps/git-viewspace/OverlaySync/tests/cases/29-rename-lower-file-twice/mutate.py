from pathlib import Path
import sys


root = Path(sys.argv[1])
middle = root / "middle.txt"
(root / "old.txt").rename(middle)
middle.rename(root / "final.txt")
