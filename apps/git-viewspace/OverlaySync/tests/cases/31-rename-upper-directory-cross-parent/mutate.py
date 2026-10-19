from pathlib import Path
import sys


root = Path(sys.argv[1])
staging = root / "staging"
staging.mkdir()
(staging / "one.txt").write_text("first upper child\n", encoding="utf-8")
(staging / "nested").mkdir()
(staging / "nested" / "two.txt").write_text("second upper child\n", encoding="utf-8")
staging.rename(root / "destination" / "created-tree")
