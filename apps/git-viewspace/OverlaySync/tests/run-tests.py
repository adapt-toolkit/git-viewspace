#!/usr/bin/python3
"""Mount-enabled end-to-end tests for the installed OverlaySync helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
SYNC = HERE.parents[1] / "git_viewspace/overlay_sync.py"
SUDO = Path("/usr/bin/sudo")
MOUNT = Path("/usr/bin/mount")
UMOUNT = Path("/usr/bin/umount")
FINDMNT = Path("/usr/bin/findmnt")
PYTHON = Path("/usr/bin/python3")
SCRATCH = HERE.parents[4] / ".scratch"
MOUNT_OPTIONS = (
    "redirect_dir=on",
    "metacopy=off",
    "index=off",
    "nfs_export=off",
    "xino=off",
    "uuid=null",
    "verity=off",
)
NO_GIT_CHANGE = {
    "15-create-delete-upper",
    "38-rename-over-nonempty-directory-fails",
}
EXPECTED_WHITEOUTS: dict[str, list[str]] = {
    "14-delete-lower-regular": ["victim.txt"],
    "16-delete-lower-symlink": ["link-to-delete"],
    "26-rename-lower-file-same-parent": ["docs/old.txt"],
    "27-rename-lower-file-cross-parent": ["from/moving.txt"],
    "28-rename-lower-file-over-lower": ["source.txt"],
    "29-rename-lower-file-twice": ["old.txt"],
    "30-rename-lower-symlink": ["old-link"],
    "32-rename-lower-directory-same-parent": ["dirs/source"],
    "33-rename-lower-directory-cross-parent": ["from/source"],
    "34-redirected-directory-add-child": ["collection/source"],
    "35-redirected-directory-modify-child": ["collection/source"],
    "36-redirected-directory-delete-child": ["collection/renamed/delete.txt", "collection/source"],
    "37-redirected-directory-second-rename": ["archive/final-project/nested/delete.txt", "incoming/project"],
    "39-canonical-composite": ["dirs/move", "files/remove.txt"],
    "40-delete-lower-directory": ["remove-me"],
    "41-rename-lower-directory-over-empty": ["source"],
    "43-two-independent-redirects": ["left/source", "right/source"],
    "44-redirected-child-type-replacement": ["collection/source"],
    "45-mixed-working-tree-edit": ["docs/old.txt", "src/draft.txt"],
    "46-redirected-project-edit-batch": [
        "incoming/project",
        "workspace/project/delete.txt",
    ],
    "48-opaque-recreate-with-move": ["inbox/move.txt", "remove.txt"],
    "49-literal-pathnames-batch": [
        "names/-leading.txt",
        "names/[draft].txt",
        "names/café.txt",
    ],
    "50-directory-lifecycle-batch": [
        "archive/alpha/remove.txt",
        "obsolete",
        "projects/alpha",
        "projects/beta/keep.txt",
    ],
}
EXPECTED_OPAQUE: dict[str, list[str]] = {
    "05-create-nested-tree": ["new-tree"],
    "17-delete-recreate-directory": ["replaced"],
    "19-regular-to-directory": ["node"],
    "22-symlink-to-directory": ["node"],
    "31-rename-upper-directory-cross-parent": ["destination/created-tree"],
    "39-canonical-composite": ["dirs/recreate"],
    "42-rename-upper-directory-over-empty-lower": ["empty-destination"],
    "46-redirected-project-edit-batch": ["workspace/project/nested"],
    "47-multiple-type-replacements": ["second-file"],
    "48-opaque-recreate-with-move": ["cache"],
    "49-literal-pathnames-batch": ["names/sub dir"],
    "50-directory-lifecycle-batch": ["projects/gamma"],
}
EXPECTED_REDIRECTS: dict[str, list[dict[str, str]]] = {
    "32-rename-lower-directory-same-parent": [
        {"destination": "dirs/renamed", "source": "dirs/source"}
    ],
    "33-rename-lower-directory-cross-parent": [
        {"destination": "to/moved-source", "source": "from/source"}
    ],
    "34-redirected-directory-add-child": [
        {"destination": "collection/renamed", "source": "collection/source"}
    ],
    "35-redirected-directory-modify-child": [
        {"destination": "collection/renamed", "source": "collection/source"}
    ],
    "36-redirected-directory-delete-child": [
        {"destination": "collection/renamed", "source": "collection/source"}
    ],
    "37-redirected-directory-second-rename": [
        {"destination": "archive/final-project", "source": "incoming/project"}
    ],
    "39-canonical-composite": [
        {"destination": "redirected", "source": "dirs/move"}
    ],
    "41-rename-lower-directory-over-empty": [
        {"destination": "empty-destination", "source": "source"}
    ],
    "43-two-independent-redirects": [
        {"destination": "destinations/left-moved", "source": "left/source"},
        {"destination": "destinations/right-moved", "source": "right/source"},
    ],
    "44-redirected-child-type-replacement": [
        {"destination": "collection/renamed", "source": "collection/source"}
    ],
    "46-redirected-project-edit-batch": [
        {"destination": "workspace/project", "source": "incoming/project"}
    ],
    "50-directory-lifecycle-batch": [
        {"destination": "archive/alpha", "source": "projects/alpha"}
    ],
}


class TestFailure(RuntimeError):
    pass


def run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv, text=True, capture_output=True, env=env, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        raise TestFailure(f"command timed out: {argv!r}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no command output"
        raise TestFailure(
            f"command failed ({completed.returncode}): {argv!r}\n{detail}"
        )
    return completed


def copy_fixture(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)


def git_manifest(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise TestFailure(f"cannot lstat manifest root {root}: {exc}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise TestFailure(f"manifest root is not a real directory: {root}")

    def visit(directory: Path, prefix: tuple[str, ...]) -> bool:
        has_leaf = False
        for item in sorted(os.scandir(directory), key=lambda value: value.name):
            try:
                item.name.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise TestFailure(f"non-UTF-8 name in result: {item.path}") from exc
            parts = prefix + (item.name,)
            relative = "/".join(parts)
            info = item.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if visit(Path(item.path), parts):
                    has_leaf = True
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise TestFailure(f"hard link in result: {relative}")
                data = Path(item.path).read_bytes()
                result[relative] = {
                    "kind": "regular",
                    "executable": bool(info.st_mode & stat.S_IXUSR),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                has_leaf = True
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(item.path)
                try:
                    target.encode("utf-8", "strict")
                except UnicodeEncodeError as exc:
                    raise TestFailure(f"non-UTF-8 symlink target: {relative}") from exc
                result[relative] = {"kind": "symlink", "target": target}
                has_leaf = True
            else:
                raise TestFailure(f"unsupported inode in result: {relative}")
        return has_leaf

    visit(root, ())
    return result


def manifest_diff(expected: dict[str, object], actual: dict[str, object]) -> str:
    paths = sorted(set(expected) | set(actual))
    differences = []
    for path in paths:
        if expected.get(path) != actual.get(path):
            differences.append(
                {"path": path, "expected": expected.get(path), "actual": actual.get(path)}
            )
    return json.dumps(differences, indent=2, sort_keys=True)


def validate_case(case: Path) -> None:
    names = {item.name for item in case.iterdir()}
    if names != {"A", "mutate.py"}:
        raise TestFailure(f"{case.name}: case directory must contain exactly A and mutate.py; got {sorted(names)}")
    if not (case / "A").is_dir() or (case / "A").is_symlink():
        raise TestFailure(f"{case.name}: A must be a real directory")
    if not (case / "mutate.py").is_file() or (case / "mutate.py").is_symlink():
        raise TestFailure(f"{case.name}: mutate.py must be a real file")


def mutate(script: Path, tree: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    run([str(PYTHON), "-I", str(script), str(tree)], env=environment)


def mount_overlay(lower: Path, upper: Path, work: Path, merged: Path) -> None:
    for path in (lower, upper, work, merged):
        if any(character in os.fspath(path) for character in (",", ":", "\n", "\r")):
            raise TestFailure(f"mount path contains an unsafe option delimiter: {path}")
    option = ",".join(
        (f"lowerdir={lower}", f"upperdir={upper}", f"workdir={work}", *MOUNT_OPTIONS)
    )
    run([str(SUDO), "-n", str(MOUNT), "-t", "overlay", "overlay", "-o", option, str(merged)])
    profile = run(
        [str(FINDMNT), "-n", "-T", str(merged), "-o", "FSTYPE,OPTIONS"]
    ).stdout.strip()
    words = set(profile.replace(",", " ").split())
    if "overlay" not in words or "redirect_dir=on" not in words or "uuid=null" not in words:
        raise TestFailure(f"effective OverlayFS profile is not qualified: {profile}")
    forbidden = {"metacopy=on", "index=on", "nfs_export=on", "xino=on", "verity=on", "userxattr", "volatile"}
    present = forbidden & words
    if present:
        raise TestFailure(f"forbidden effective OverlayFS options: {sorted(present)}")


def unmount_overlay(merged: Path) -> None:
    run([str(SUDO), "-n", str(UMOUNT), "--", str(merged)])


def sync_upper(upper: Path, target: Path) -> dict[str, object]:
    completed = run(
        [str(SUDO), "-n", str(PYTHON), "-I", str(SYNC), str(upper), str(target)]
    )
    try:
        report = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TestFailure(f"overlay-sync returned invalid JSON: {completed.stdout!r}") from exc
    if not isinstance(report, dict):
        raise TestFailure("overlay-sync report is not an object")
    return report


def assert_live_sync_refused(upper: Path, target: Path) -> None:
    before = git_manifest(target)
    completed = subprocess.run(
        [str(SUDO), "-n", str(PYTHON), "-I", str(SYNC), str(upper), str(target)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode == 0 or "still mounted" not in completed.stderr:
        raise TestFailure(
            "overlay-sync did not reject its live upper: "
            f"rc={completed.returncode}, stderr={completed.stderr!r}"
        )
    if git_manifest(target) != before:
        raise TestFailure("live-upper refusal changed the target")


def validate_sync_report(case_name: str, report: dict[str, object]) -> None:
    if report.get("cleanup_pending") is not False:
        raise TestFailure(f"{case_name}: sync left cleanup pending: {report!r}")
    counts = report.get("counts")
    redirects = report.get("redirects")
    whiteouts = report.get("whiteouts")
    opaque = report.get("opaque")
    if not all(
        (
            isinstance(counts, dict),
            isinstance(redirects, list),
            isinstance(whiteouts, list),
            isinstance(opaque, list),
        )
    ):
        raise TestFailure(f"{case_name}: malformed sync report: {report!r}")
    expected_redirects = EXPECTED_REDIRECTS.get(case_name, [])
    if redirects != expected_redirects:
        raise TestFailure(
            f"{case_name}: redirect plan mismatch; expected {expected_redirects!r}, got {redirects!r}"
        )
    expected_whiteouts = EXPECTED_WHITEOUTS.get(case_name, [])
    if whiteouts != expected_whiteouts:
        raise TestFailure(
            f"{case_name}: whiteout paths mismatch; expected {expected_whiteouts!r}, got {whiteouts!r}"
        )
    expected_opaque = EXPECTED_OPAQUE.get(case_name, [])
    if opaque != expected_opaque:
        raise TestFailure(
            f"{case_name}: opaque paths mismatch; expected {expected_opaque!r}, got {opaque!r}"
        )
    if case_name in NO_GIT_CHANGE:
        expected = {
            "directories": 1,
            "regulars": 0,
            "symlinks": 0,
            "whiteouts": 0,
            "opaque": 0,
            "origin": 0,
            "impure": 0,
        }
        if counts != expected:
            raise TestFailure(f"{case_name}: expected a pristine final upper; got {counts!r}")


def run_case(case: Path, run_root: Path, *, probe_live_refusal: bool) -> None:
    validate_case(case)
    root = run_root / case.name
    root.mkdir()
    lower = root / "A"
    expected = root / "B"
    actual = root / "C"
    upper = root / "upper"
    work = root / "work"
    merged = root / "Aprime"
    copy_fixture(case / "A", lower)
    copy_fixture(lower, expected)
    copy_fixture(lower, actual)
    upper.mkdir()
    work.mkdir()
    merged.mkdir()
    lower_before = git_manifest(lower)
    mounted = False
    primary_error: BaseException | None = None
    try:
        mount_overlay(lower, upper, work, merged)
        mounted = True
        if probe_live_refusal:
            assert_live_sync_refused(upper, actual)
        mutate(case / "mutate.py", merged)
        mutate(case / "mutate.py", expected)
    except BaseException as exc:
        primary_error = exc
    finally:
        if mounted or os.path.ismount(merged):
            try:
                unmount_overlay(merged)
            except BaseException as cleanup_error:
                if primary_error is not None:
                    raise TestFailure(
                        f"case failed ({primary_error}) and unmount also failed ({cleanup_error})"
                    ) from cleanup_error
                raise
            mounted = False
    if primary_error is not None:
        raise primary_error
    if git_manifest(lower) != lower_before:
        raise TestFailure(f"{case.name}: lower A changed during the test")
    expected_manifest = git_manifest(expected)
    should_change = case.name not in NO_GIT_CHANGE
    if (expected_manifest != lower_before) != should_change:
        raise TestFailure(
            f"{case.name}: mutation did not produce its declared Git-change disposition"
        )

    # Prove that the stopped upper/work pair remains a valid OverlayFS state
    # before interpreting it offline.
    mounted = False
    try:
        mount_overlay(lower, upper, work, merged)
        mounted = True
        remounted_manifest = git_manifest(merged)
        if remounted_manifest != expected_manifest:
            raise TestFailure(
                f"{case.name}: remounted OverlayFS differs from oracle\n"
                f"{manifest_diff(expected_manifest, remounted_manifest)}"
            )
    finally:
        if mounted or os.path.ismount(merged):
            unmount_overlay(merged)
            mounted = False

    report = sync_upper(upper, actual)
    validate_sync_report(case.name, report)
    actual_manifest = git_manifest(actual)
    if actual_manifest != expected_manifest:
        raise TestFailure(f"{case.name}: replay mismatch\n{manifest_diff(expected_manifest, actual_manifest)}")
    residue = list(root.glob(".C.overlay-sync-*"))
    if residue:
        raise TestFailure(f"{case.name}: sync left staging residue: {residue!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", default=[], help="run only this case (repeatable)")
    parser.add_argument(
        "--repository-roundtrip",
        action="store_true",
        help="run the repository commit/delete/restore suite instead of the ABC suite",
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=SCRATCH / "git-viewspace-OverlaySync",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repository_roundtrip:
        command = [str(PYTHON), "-B", str(HERE / "run-commit-tests.py")]
        for case in args.case:
            command.extend(("--case", case))
        command.extend(("--scratch-root", os.fspath(args.scratch_root)))
        return subprocess.run(command, check=False).returncode
    os.umask(0o022)
    if os.geteuid() == 0:
        print("the harness must run as the ordinary workload user", file=sys.stderr)
        return 2
    for executable in (SUDO, MOUNT, UMOUNT, FINDMNT, PYTHON, SYNC):
        if not executable.is_file():
            print(f"missing required executable: {executable}", file=sys.stderr)
            return 2
    all_cases = sorted(path for path in CASES.iterdir() if path.is_dir())
    wanted = set(args.case)
    if wanted:
        unknown = wanted - {path.name for path in all_cases}
        if unknown:
            print(f"unknown cases: {sorted(unknown)}", file=sys.stderr)
            return 2
        all_cases = [path for path in all_cases if path.name in wanted]
    if not all_cases:
        print("no test cases selected", file=sys.stderr)
        return 2

    required_scratch = SCRATCH.resolve()
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    scratch_root = args.scratch_root.resolve()
    if scratch_root != required_scratch and required_scratch not in scratch_root.parents:
        print(f"scratch root must remain beneath {required_scratch}: {scratch_root}", file=sys.stderr)
        return 2
    run_root = scratch_root / f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    run_root.mkdir()
    failures: list[tuple[str, str]] = []
    print(f"scratch: {run_root}")
    for index, case in enumerate(all_cases):
        started = time.monotonic()
        try:
            run_case(case, run_root, probe_live_refusal=index == 0)
        except (TestFailure, OSError, subprocess.CalledProcessError) as exc:
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
