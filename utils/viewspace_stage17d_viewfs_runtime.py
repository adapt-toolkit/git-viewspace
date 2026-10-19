#!/usr/bin/env python3
"""Exercise git-viewspace-fs source and installed-wheel runtime under one Python."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap


def run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed with status {completed.returncode}: {arguments!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def isolated_environment(workspace: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": os.fspath(workspace),
        }
    )
    return environment


def copy_source(repository: Path, destination: Path) -> None:
    shutil.copytree(
        repository,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".scratch",
            "*.egg-info",
            "__pycache__",
            "*.pyc",
            "build",
            "dist",
            "test_repo",
        ),
    )


def make_fixture(
    source: Path, environment: dict[str, str]
) -> None:
    run(
        [os.fspath(source / "git_viewspace_fs/tests/make_test_repo.sh")],
        cwd=source,
        environment=environment,
    )


def run_suite(
    python: str,
    source: Path,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    run(
        [
            python,
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            os.fspath(source / "git_viewspace_fs/tests"),
            "-p",
            "*_test.py",
        ],
        cwd=cwd,
        environment=environment,
    )


def run_installed_suite(
    python: str,
    site: Path,
    source: Path,
    neutral: Path,
    environment: dict[str, str],
) -> None:
    script = textwrap.dedent(
        f"""
        import sys
        import unittest

        sys.path.insert(0, {os.fspath(site)!r})
        suite = unittest.defaultTestLoader.discover(
            {os.fspath(source / "git_viewspace_fs/tests")!r},
            pattern="*_test.py",
        )
        result = unittest.TextTestRunner().run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
        """
    )
    run(
        [python, "-I", "-B", "-c", script],
        cwd=neutral,
        environment=environment,
    )


def build_wheel(
    python: str,
    source: Path,
    destination: Path,
    environment: dict[str, str],
) -> Path:
    script = textwrap.dedent(
        """
        import os
        import setuptools.build_meta
        import sys

        setuptools.build_meta.build_wheel(os.path.abspath(sys.argv[1]))
        """
    )
    build_environment = dict(environment)
    run(
        [python, "-B", "-c", script, os.fspath(destination)],
        cwd=source,
        environment=build_environment,
    )
    wheels = list(destination.glob("*.whl"))
    if len(wheels) != 1:
        raise AssertionError(f"expected one git-viewspace-fs wheel, found {wheels!r}")
    return wheels[0]


def verify_installed(
    python: str,
    site: Path,
    neutral: Path,
    environment: dict[str, str],
) -> None:
    probe = textwrap.dedent(
        f"""
        import importlib.metadata
        import importlib.util
        from pathlib import Path
        import sys

        site = Path({os.fspath(site)!r}).resolve(strict=True)
        sys.path.insert(0, str(site))
        import git_viewspace_fs
        from git_viewspace_fs.filesystem import GitViewspaceFS

        distribution = importlib.metadata.distribution("git-viewspace")
        assert distribution.metadata["Name"] == "git-viewspace"
        assert distribution.version == "0.1.0"
        console_entries = [
            entry
            for entry in distribution.entry_points
            if entry.group == "console_scripts" and entry.name == "git-viewspace-fs"
        ]
        assert len(console_entries) == 1
        assert console_entries[0].value == "git_viewspace_fs.__main__:console_main"
        assert GitViewspaceFS.__name__ == "GitViewspaceFS"
        module = Path(git_viewspace_fs.__file__).resolve(strict=True)
        assert module.is_relative_to(site), module
        direct = distribution.read_text("direct_url.json")
        assert direct is None or not __import__("json").loads(direct).get(
            "dir_info", {{}}
        ).get("editable", False)
        assert importlib.util.find_spec("repofs") is None
        """
    )
    run([python, "-I", "-B", "-c", probe], cwd=neutral, environment=environment)
    binary = site / "bin/git-viewspace-fs"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise AssertionError("installed git-viewspace-fs console entry is absent or not executable")
    if (binary.parent / "repofs").exists():
        raise AssertionError("obsolete repofs console entry remains installed")
    console_environment = dict(environment)
    console_environment["PYTHONPATH"] = os.fspath(site)
    run(
        [os.fspath(binary), "--help"],
        cwd=neutral,
        environment=console_environment,
    )
    module_probe = textwrap.dedent(
        f"""
        import runpy
        import sys

        sys.path.insert(0, {os.fspath(site)!r})
        sys.argv = ["python -m git_viewspace_fs", "--help"]
        runpy.run_module("git_viewspace_fs", run_name="__main__")
        """
    )
    run(
        [python, "-I", "-B", "-c", module_probe],
        cwd=neutral,
        environment=environment,
    )


def main() -> None:
    if len(sys.argv) != 4:
        raise AssertionError("expected repository, target interpreter, and major.minor")
    repository = Path(sys.argv[1]).resolve(strict=True)
    target_python = shutil.which(sys.argv[2])
    if target_python is None:
        raise AssertionError(f"target interpreter is unavailable: {sys.argv[2]}")
    target_version = tuple(map(int, sys.argv[3].split(".")))
    if len(target_version) != 2:
        raise AssertionError(f"invalid target version: {sys.argv[3]!r}")
    workspace = Path.cwd().resolve(strict=True)
    if workspace == repository or workspace.is_relative_to(repository):
        raise AssertionError("runtime workspace must be outside the source repository")

    environment = isolated_environment(workspace)
    actual_version = run(
        [
            target_python,
            "-I",
            "-B",
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        cwd=workspace,
        environment=environment,
    ).stdout.strip()
    requested_version = ".".join(map(str, target_version))
    if actual_version != requested_version:
        raise AssertionError(
            f"target interpreter is Python {actual_version}, expected {requested_version}"
        )
    source = workspace / "source"
    wheelhouse = workspace / "wheelhouse"
    neutral = workspace / "neutral"
    copy_source(repository, source)
    wheelhouse.mkdir()
    neutral.mkdir()
    fs_source = source / "apps/git-viewspace-fs"
    make_fixture(fs_source, environment)

    source_environment = dict(environment)
    source_environment["PYTHONPATH"] = os.pathsep.join((
        os.fspath(source / "apps/git-viewspace"), os.fspath(fs_source),
    ))
    run_suite(target_python, fs_source, fs_source, source_environment)

    wheel = build_wheel(
        target_python, source, wheelhouse, environment
    )
    installed = workspace / "installed"
    run(
        [
            target_python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            os.fspath(installed),
            os.fspath(wheel),
        ],
        cwd=workspace,
        environment=environment,
    )
    site = installed.resolve(strict=True)
    verify_installed(target_python, site, neutral, environment)
    run_installed_suite(
        target_python,
        site,
        fs_source,
        neutral,
        environment,
    )

    version = requested_version
    print(f"Python {version}: isolated git-viewspace-fs source suite: ok")
    print(f"Python {version}: offline wheel build and target install: ok")
    print(f"Python {version}: installed metadata and import isolation: ok")
    print(f"Python {version}: installed console and module help: ok")
    print(f"Python {version}: installed-wheel suite from neutral directory: ok")


if __name__ == "__main__":
    main()
