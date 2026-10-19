#!/usr/bin/python3
"""Combine a redirected tree, subtree deletion, new tree, and file rename."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
alpha = root / "archive" / "alpha"
(root / "projects" / "alpha").rename(alpha)
(alpha / "edit.txt").write_text("new edit\n", encoding="utf-8")
(alpha / "remove.txt").unlink()
(alpha / "added.txt").write_text("added child\n", encoding="utf-8")
shutil.rmtree(root / "obsolete")
gamma = root / "projects" / "gamma" / "nested"
gamma.mkdir(parents=True)
(gamma / "one.txt").write_text("new one\n", encoding="utf-8")
(gamma / "two.txt").write_text("new two\n", encoding="utf-8")
(root / "projects" / "beta" / "keep.txt").rename(
    root / "projects" / "beta" / "renamed.txt"
)
