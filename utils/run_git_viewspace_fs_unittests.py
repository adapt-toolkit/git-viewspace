#!/usr/bin/env python3
"""Run exact git-viewspace-fs unittest IDs from an isolated source copy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from python_test_inventory import InventoryError, validate_exact_methods


def _copy_ignores(source: Path):
    """Ignore generated app-root artifacts without hiding nested test packages."""

    def ignored(directory: str, names: list[str]) -> set[str]:
        result = {name for name in names if name == "__pycache__"}
        if Path(directory).resolve() == source:
            result.update(
                name
                for name in names
                if name in {"build", "test_repo"} or name.endswith(".egg-info")
            )
        return result

    return ignored


def main() -> int:
    if len(sys.argv) < 5:
        print(
            "usage: run_git_viewspace_fs_unittests.py REPOSITORY WORKSPACE "
            "[--fixture] -- TEST_ID...",
            file=sys.stderr,
        )
        return 2
    repository = Path(sys.argv[1]).resolve(strict=True)
    workspace = Path(sys.argv[2]).resolve(strict=True)
    arguments = sys.argv[3:]
    fixture = bool(arguments and arguments[0] == "--fixture")
    if fixture:
        arguments = arguments[1:]
    if not arguments or arguments[0] != "--" or len(arguments) == 1:
        print("a -- delimiter and at least one exact test ID are required", file=sys.stderr)
        return 2
    test_ids = arguments[1:]
    try:
        validate_exact_methods(
            repository,
            test_ids,
            prefix="git_viewspace_fs.tests.",
        )
    except InventoryError as error:
        print(f"invalid git-viewspace-fs unittest selection: {error}", file=sys.stderr)
        return 2
    source = repository / "apps/git-viewspace-fs"
    isolated = workspace / "git-viewspace-fs"
    shutil.copytree(
        source,
        isolated,
        symlinks=True,
        ignore=_copy_ignores(source),
    )
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(isolated),
            "TMPDIR": os.fspath(workspace),
        }
    )
    if fixture:
        fixture_result = subprocess.run(
            [os.fspath(isolated / "git_viewspace_fs/tests/make_test_repo.sh")],
            cwd=isolated,
            env=environment,
            check=False,
        )
        if fixture_result.returncode:
            return fixture_result.returncode
    return subprocess.run(
        [sys.executable, "-m", "unittest", *test_ids],
        cwd=isolated,
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
