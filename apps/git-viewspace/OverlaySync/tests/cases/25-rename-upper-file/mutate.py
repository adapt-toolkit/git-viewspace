from pathlib import Path
import sys


root = Path(sys.argv[1])
draft = root / "draft.txt"
draft.write_text("created in upper\n", encoding="utf-8")
draft.rename(root / "FINAL.txt")
