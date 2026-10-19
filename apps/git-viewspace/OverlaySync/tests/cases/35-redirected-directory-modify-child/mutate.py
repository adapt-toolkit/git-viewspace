from pathlib import Path
import sys


root = Path(sys.argv[1])
redirected = root / "collection" / "renamed"
(root / "collection" / "source").rename(redirected)
(redirected / "change.txt").write_text("new contents\n", encoding="utf-8")
