from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "dirs" / "source").rename(root / "dirs" / "renamed")
