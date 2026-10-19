from pathlib import Path
import sys


root = Path(sys.argv[1])
redirected = root / "collection" / "renamed"
(root / "collection" / "source").rename(redirected)
(redirected / "delete.txt").unlink()
