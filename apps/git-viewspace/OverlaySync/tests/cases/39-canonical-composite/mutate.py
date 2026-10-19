from pathlib import Path
import sys


root = Path(sys.argv[1])
(root / "upper-only.txt").write_text("pure upper\n", encoding="utf-8")
(root / "files" / "plain.txt").write_text("copied up\n", encoding="utf-8")
(root / "files" / "remove.txt").unlink()
recreated = root / "dirs" / "recreate"
(recreated / "old.txt").unlink()
recreated.rmdir()
recreated.mkdir()
(recreated / "new.txt").write_text("opaque replacement\n", encoding="utf-8")
(root / "dirs" / "move").rename(root / "redirected")
