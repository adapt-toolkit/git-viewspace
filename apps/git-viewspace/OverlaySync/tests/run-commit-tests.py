#!/usr/bin/python3
"""Commit-and-restore integration tests for the installed OverlaySync helper."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


sys.dont_write_bytecode = True


HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parents[4] / ".scratch"
CASES = HERE / "cases"
REPOSITORY_CASES = HERE / "repository-cases"
GIT = Path("/usr/bin/git")
PYTHON = Path("/usr/bin/python3")


def load_abc_harness():
    specification = importlib.util.spec_from_file_location(
        "viewspace_overlay_sync_abc_harness", HERE / "run-tests.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


ABC = load_abc_harness()
TestFailure = ABC.TestFailure


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            input=data,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise TestFailure(f"command timed out: {argv!r}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"no command output").decode(
            "utf-8", "replace"
        )
        raise TestFailure(
            f"command failed ({completed.returncode}): {argv!r}\n{detail.strip()}"
        )
    return completed


def git(
    git_dir: Path,
    worktree: Path,
    arguments: list[str],
    *,
    data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return run(
        [
            os.fspath(GIT),
            f"--git-dir={git_dir}",
            f"--work-tree={worktree}",
            *arguments,
        ],
        cwd=worktree,
        data=data,
    )


def output(completed: subprocess.CompletedProcess[bytes]) -> str:
    return completed.stdout.decode("utf-8", "strict").strip()


def materialize_repository(
    bundle: Path,
    git_dir: Path,
    worktree: Path,
    branch: str,
    hooks: Path,
) -> str:
    run([os.fspath(GIT), "init", "--quiet", "--bare", os.fspath(git_dir)])
    run(
        [
            os.fspath(GIT),
            f"--git-dir={git_dir}",
            "fetch",
            "--quiet",
            "--no-tags",
            os.fspath(bundle),
            "refs/heads/base:refs/heads/base",
        ]
    )
    settings = {
        "commit.gpgsign": "false",
        "core.autocrlf": "false",
        "core.bare": "false",
        "core.filemode": "true",
        "core.hooksPath": os.fspath(hooks),
        "core.worktree": os.fspath(worktree),
        "user.email": "overlay-sync-tests@example.invalid",
        "user.name": "OverlaySync tests",
    }
    for key, value in settings.items():
        run(
            [
                os.fspath(GIT),
                f"--git-dir={git_dir}",
                "config",
                key,
                value,
            ]
        )
    worktree.mkdir()
    git(git_dir, worktree, ["checkout", "--quiet", "--force", "-B", branch, "refs/heads/base"])
    return output(git(git_dir, worktree, ["rev-parse", "refs/heads/base^{commit}"]))


def commit(git_dir: Path, worktree: Path, message: str) -> str:
    git(
        git_dir,
        worktree,
        ["commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", message],
    )
    return output(git(git_dir, worktree, ["rev-parse", "HEAD^{commit}"]))


def require_clean(git_dir: Path, worktree: Path, where: str) -> None:
    status = git(
        git_dir,
        worktree,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    ).stdout
    if status:
        raise TestFailure(f"{where} is not clean after commit/restore: {status!r}")


def require_single_parent(
    git_dir: Path,
    worktree: Path,
    revision: str,
    base: str,
    where: str,
) -> None:
    fields = output(
        git(git_dir, worktree, ["rev-list", "--parents", "-n", "1", revision])
    ).split()
    if fields != [revision, base]:
        raise TestFailure(f"{where} commit parent mismatch: {fields!r}, expected {[revision, base]!r}")


def validate_fixture_sets() -> list[Path]:
    ordinary = {path.name: path for path in CASES.iterdir() if path.is_dir()}
    repositories = {
        path.name: path for path in REPOSITORY_CASES.iterdir() if path.is_dir()
    }
    if ordinary.keys() != repositories.keys():
        missing = sorted(ordinary.keys() - repositories.keys())
        extra = sorted(repositories.keys() - ordinary.keys())
        raise TestFailure(
            f"repository fixture set differs from ABC cases; missing={missing}, extra={extra}"
        )
    for name, repository_case in repositories.items():
        names = {item.name for item in repository_case.iterdir()}
        if names != {"repository.bundle", "mutate.py"}:
            raise TestFailure(
                f"{name}: repository case must contain exactly repository.bundle and mutate.py"
            )
        bundle = repository_case / "repository.bundle"
        mutation = repository_case / "mutate.py"
        if not bundle.is_file() or bundle.is_symlink():
            raise TestFailure(f"{name}: repository.bundle must be a real file")
        if not mutation.is_file() or mutation.is_symlink():
            raise TestFailure(f"{name}: mutate.py must be a real file")
        if mutation.read_bytes() != (ordinary[name] / "mutate.py").read_bytes():
            raise TestFailure(f"{name}: repository mutate.py differs from canonical case")
    return [repositories[name] for name in sorted(repositories)]


def validate_pathspecs(report: dict[str, object], case_name: str) -> list[str]:
    if report.get("mode") != "direct":
        raise TestFailure(f"{case_name}: commit suite requires direct sync mode")
    value = report.get("git_pathspecs")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TestFailure(f"{case_name}: malformed git_pathspecs: {value!r}")
    if value != sorted(set(value)):
        raise TestFailure(f"{case_name}: git_pathspecs are not unique and sorted: {value!r}")
    for item in value:
        if not item or "\0" in item or any(part == ".git" for part in item.split("/")):
            raise TestFailure(f"{case_name}: invalid Git pathspec {item!r}")
        item.encode("utf-8", "strict")
    return value


def run_case(repository_case: Path, run_root: Path) -> None:
    name = repository_case.name
    root = run_root / name
    root.mkdir()
    hooks = root / "hooks"
    hooks.mkdir()
    git_dirs = {label: root / f"{label}.git" for label in ("A", "B", "C")}
    trees = {label: root / label for label in ("A", "B", "C")}
    bases = {
        label: materialize_repository(
            repository_case / "repository.bundle",
            git_dirs[label],
            trees[label],
            {"A": "lower", "B": "model", "C": "actual"}[label],
            hooks,
        )
        for label in ("A", "B", "C")
    }
    if len(set(bases.values())) != 1:
        raise TestFailure(f"{name}: materialized repositories disagree on BASE: {bases!r}")
    base = bases["A"]
    canonical = CASES / name
    ABC.validate_case(canonical)
    base_manifest = ABC.git_manifest(trees["A"])
    canonical_manifest = ABC.git_manifest(canonical / "A")
    if base_manifest != canonical_manifest:
        raise TestFailure(
            f"{name}: bundled BASE differs from canonical A\n"
            f"{ABC.manifest_diff(canonical_manifest, base_manifest)}"
        )
    for tree in trees.values():
        if os.path.lexists(tree / ".git"):
            raise TestFailure(f"{name}: external-worktree fixture unexpectedly contains .git")

    ABC.mutate(repository_case / "mutate.py", trees["B"])
    git(git_dirs["B"], trees["B"], ["add", "-A"])
    model_commit = commit(git_dirs["B"], trees["B"], f"model {name}")
    require_single_parent(git_dirs["B"], trees["B"], model_commit, base, "model")
    require_clean(git_dirs["B"], trees["B"], "model worktree")

    upper = root / "upper"
    work = root / "work"
    merged = root / "Aprime"
    upper.mkdir()
    work.mkdir()
    merged.mkdir()
    mounted = False
    primary_error: BaseException | None = None
    try:
        ABC.mount_overlay(trees["A"], upper, work, merged)
        mounted = True
        ABC.mutate(repository_case / "mutate.py", merged)
    except BaseException as exc:
        primary_error = exc
    finally:
        if mounted or os.path.ismount(merged):
            try:
                ABC.unmount_overlay(merged)
            except BaseException as cleanup_error:
                if primary_error is not None:
                    raise TestFailure(
                        f"case failed ({primary_error}) and unmount failed ({cleanup_error})"
                    ) from cleanup_error
                raise
            mounted = False
    if primary_error is not None:
        raise primary_error
    if ABC.git_manifest(trees["A"]) != base_manifest:
        raise TestFailure(f"{name}: immutable lower changed")
    require_clean(git_dirs["A"], trees["A"], "lower worktree")

    report = ABC.sync_upper(upper, trees["C"])
    ABC.validate_sync_report(name, report)
    pathspecs = validate_pathspecs(report, name)
    if pathspecs:
        payload = b"".join(item.encode("utf-8", "strict") + b"\0" for item in pathspecs)
        git(
            git_dirs["C"],
            trees["C"],
            [
                "--literal-pathspecs",
                "add",
                "-A",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            data=payload,
        )
    actual_commit = commit(git_dirs["C"], trees["C"], f"actual {name}")
    require_single_parent(git_dirs["C"], trees["C"], actual_commit, base, "actual")
    require_clean(git_dirs["C"], trees["C"], "actual worktree before restore")

    shutil.rmtree(trees["C"])
    index = git_dirs["C"] / "index"
    if index.exists() or index.is_symlink():
        index.unlink()
    trees["C"].mkdir()
    git(git_dirs["C"], trees["C"], ["reset", "--quiet", "--hard", actual_commit])
    require_clean(git_dirs["C"], trees["C"], "actual worktree after restore")

    model_tree = output(git(git_dirs["B"], trees["B"], ["rev-parse", f"{model_commit}^{{tree}}"] ))
    actual_tree = output(git(git_dirs["C"], trees["C"], ["rev-parse", f"{actual_commit}^{{tree}}"] ))
    if model_tree != actual_tree:
        raise TestFailure(
            f"{name}: model and actual commit trees differ: {model_tree} != {actual_tree}"
        )
    model_manifest = ABC.git_manifest(trees["B"])
    restored_manifest = ABC.git_manifest(trees["C"])
    if model_manifest != restored_manifest:
        raise TestFailure(
            f"{name}: Git-restored checkout differs from model\n"
            f"{ABC.manifest_diff(model_manifest, restored_manifest)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=SCRATCH / "git-viewspace-OverlaySync-commit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.umask(0o022)
    if os.geteuid() == 0:
        print("the harness must run as the ordinary workload user", file=sys.stderr)
        return 2
    if not GIT.is_file():
        print(f"missing Git executable: {GIT}", file=sys.stderr)
        return 2
    try:
        all_cases = validate_fixture_sets()
    except (TestFailure, OSError) as exc:
        print(f"invalid repository fixtures: {exc}", file=sys.stderr)
        return 2
    wanted = set(args.case)
    if wanted:
        unknown = wanted - {case.name for case in all_cases}
        if unknown:
            print(f"unknown cases: {sorted(unknown)}", file=sys.stderr)
            return 2
        all_cases = [case for case in all_cases if case.name in wanted]
    required_scratch = SCRATCH.resolve()
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = args.scratch_root.resolve()
    if scratch != required_scratch and required_scratch not in scratch.parents:
        print(f"scratch root must remain beneath {required_scratch}: {scratch}", file=sys.stderr)
        return 2
    run_root = scratch / f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    run_root.mkdir()
    failures: list[tuple[str, str]] = []
    print(f"scratch: {run_root}")
    for case in all_cases:
        started = time.monotonic()
        try:
            run_case(case, run_root)
        except (TestFailure, OSError) as exc:
            failures.append((case.name, str(exc)))
            print(f"FAIL {case.name}: {exc}")
            if os.path.ismount(run_root / case.name / "Aprime"):
                print("ABORT: failed case left an OverlayFS mount active")
                return 1
        else:
            print(f"PASS {case.name} ({time.monotonic() - started:.3f}s)")
    print(f"summary: {len(all_cases) - len(failures)}/{len(all_cases)} passed")
    if failures:
        for name, error in failures:
            print(f"  {name}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
