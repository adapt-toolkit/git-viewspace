from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "from" / "source").rename(root / "to" / "moved-source")
