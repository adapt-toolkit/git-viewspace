from pathlib import Path
import sys


root = Path(sys.argv[1])
redirected = root / "workspace" / "project"
(root / "incoming" / "project").rename(redirected)
(redirected / "added.txt").write_text("new child\n", encoding="utf-8")
(redirected / "change.txt").write_text("new contents\n", encoding="utf-8")
(redirected / "nested" / "delete.txt").unlink()
redirected.rename(root / "archive" / "final-project")
