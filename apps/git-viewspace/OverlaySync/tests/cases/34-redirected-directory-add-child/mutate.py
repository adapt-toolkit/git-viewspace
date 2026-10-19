from pathlib import Path
import sys


root = Path(sys.argv[1])
redirected = root / "collection" / "renamed"
(root / "collection" / "source").rename(redirected)
(redirected / "added.txt").write_text("new child\n", encoding="utf-8")
