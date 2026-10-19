from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "old-link").rename(root / "moved-link")
