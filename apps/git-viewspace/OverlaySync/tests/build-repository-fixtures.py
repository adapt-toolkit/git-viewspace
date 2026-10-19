#!/usr/bin/python3
"""Build deterministic Git bundle fixtures from the canonical ABC cases."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
GIT = Path("/usr/bin/git")
FIXED_DATE = "2000-01-01T00:00:00Z"


def run(argv: list[str], *, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        argv,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {argv!r}\n{detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "destination",
        type=Path,
        help="new directory to populate; it must not already exist",
    )
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists():
        parser.error(f"destination already exists: {destination}")
    if not GIT.is_file():
        parser.error(f"missing Git executable: {GIT}")

    cases = sorted(path for path in CASES.iterdir() if path.is_dir())
    if not cases:
        parser.error("no canonical cases found")
    destination.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="viewspace-overlay-repository-fixtures-") as temporary:
        build_root = Path(temporary)
        hooks = build_root / "hooks"
        hooks.mkdir()
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_AUTHOR_DATE": FIXED_DATE,
                "GIT_AUTHOR_EMAIL": "overlay-sync@example.invalid",
                "GIT_AUTHOR_NAME": "OverlaySync fixture",
                "GIT_COMMITTER_DATE": FIXED_DATE,
                "GIT_COMMITTER_EMAIL": "overlay-sync@example.invalid",
                "GIT_COMMITTER_NAME": "OverlaySync fixture",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "LC_ALL": "C.UTF-8",
            }
        )
        for case in cases:
            names = {item.name for item in case.iterdir()}
            if names != {"A", "mutate.py"}:
                raise RuntimeError(
                    f"{case.name}: expected exactly A and mutate.py; got {sorted(names)}"
                )
            repository = build_root / case.name
            shutil.copytree(
                case / "A",
                repository,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            run(
                [
                    os.fspath(GIT),
                    "-c",
                    f"core.hooksPath={hooks}",
                    "-C",
                    os.fspath(repository),
                    "init",
                    "--quiet",
                    "--initial-branch=base",
                ],
                environment=environment,
            )
            run(
                [os.fspath(GIT), "-C", os.fspath(repository), "config", "core.autocrlf", "false"],
                environment=environment,
            )
            run(
                [os.fspath(GIT), "-C", os.fspath(repository), "config", "core.filemode", "true"],
                environment=environment,
            )
            run(
                [os.fspath(GIT), "-C", os.fspath(repository), "add", "-A"],
                environment=environment,
            )
            run(
                [
                    os.fspath(GIT),
                    "-c",
                    f"core.hooksPath={hooks}",
                    "-C",
                    os.fspath(repository),
                    "commit",
                    "--quiet",
                    "--no-gpg-sign",
                    "-m",
                    f"base fixture for {case.name}",
                ],
                environment=environment,
            )
            output = destination / case.name
            output.mkdir()
            run(
                [
                    os.fspath(GIT),
                    "-C",
                    os.fspath(repository),
                    "bundle",
                    "create",
                    os.fspath(output / "repository.bundle"),
                    "refs/heads/base",
                ],
                environment=environment,
            )
            shutil.copy2(case / "mutate.py", output / "mutate.py")
            (output / "repository.bundle").chmod(0o644)
            (output / "mutate.py").chmod(0o644)
            shutil.rmtree(repository)

    print(f"built {len(cases)} repository fixtures in {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
