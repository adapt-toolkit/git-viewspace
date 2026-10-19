import errno
from pathlib import Path
import sys


root = Path(sys.argv[1])
source = root / "source"
destination = root / "destination"
try:
    source.rename(destination)
except OSError as exc:
    if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
        raise
else:
    raise AssertionError("rename over a nonempty directory unexpectedly succeeded")

assert (source / "source.txt").read_text(encoding="utf-8") == "source child\n"
assert (destination / "existing.txt").read_text(encoding="utf-8") == "destination child\n"
assert (destination / "nested" / "inside.txt").read_text(encoding="utf-8") == "nested destination child\n"
