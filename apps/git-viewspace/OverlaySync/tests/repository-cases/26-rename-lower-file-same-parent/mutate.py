from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "docs" / "old.txt").rename(root / "docs" / "new.txt")
