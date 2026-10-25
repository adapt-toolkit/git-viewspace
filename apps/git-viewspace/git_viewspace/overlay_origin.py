#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Discard nonpersistent lower handles in an exclusively owned, stopped upper.

Only for the manager's index=off,metacopy=off,nfs_export=off,xino=off profile.
This does not change file data or directory redirects. Removing an xattr changes
ctime. The manager must quiesce writers and hold its mutation exclusion throughout
this operation and the following mount.
"""

import argparse
import errno
import json
import os
from pathlib import Path
import sys

ORIGIN = "trusted.overlay.origin"


def unescape_mount(value):
    for source, target in (
        (r"\040", " "),
        (r"\011", "\t"),
        (r"\012", "\n"),
        (r"\134", "\\"),
    ):
        value = value.replace(source, target)
    return value


def require_unmounted(upper, mountinfo):
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            raise ValueError("invalid mount information")
        fields = left.split()
        tail = right.split()
        if len(fields) < 6 or len(tail) < 3:
            raise ValueError("invalid mount information")
        mountpoint = Path(unescape_mount(fields[4]))
        if mountpoint == upper or upper in mountpoint.parents:
            raise ValueError("upper contains an active mount")
        if tail[0] == "overlay":
            for option in tail[2].split(","):
                if (
                    option.startswith("upperdir=")
                    and Path(unescape_mount(option[9:])).resolve() == upper
                ):
                    raise ValueError("upper is in use by an active overlay")


def reset_origins(upper):
    upper = Path(upper)
    if upper.is_symlink() or not upper.is_dir() or upper != upper.resolve():
        raise ValueError("upper must be a canonical real directory")
    require_unmounted(upper, Path("/proc/self/mountinfo").read_text())
    count = 0
    visited = 0

    def fail(error):
        raise error

    for parent, dirs, files in os.walk(upper, followlinks=False, onerror=fail):
        # Directory symlinks occur in dirs but must not be traversed or omitted.
        entries = [
            Path(parent),
            *(Path(parent) / name for name in files),
            *(
                Path(parent) / name
                for name in dirs
                if (Path(parent) / name).is_symlink()
            ),
        ]
        for path in entries:
            visited += 1
            try:
                os.removexattr(path, ORIGIN, follow_symlinks=False)
            except OSError as error:
                if error.errno != errno.ENODATA:
                    raise
            else:
                count += 1
    return {"visited": visited, "origins_removed": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upper", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(reset_origins(args.upper)))
    except (OSError, ValueError) as error:
        print(f"overlay-origin: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
