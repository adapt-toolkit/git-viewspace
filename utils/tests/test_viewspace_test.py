from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from utils import viewspace_test
from utils.viewspace_test_tree import TreeEntry, snapshot_source, snapshot_zip_bytes


FAKE_BINARY = r"""
#!/usr/bin/env python3
import base64
import json
from pathlib import Path
import subprocess
import sys

configuration = json.loads(base64.b64decode("@@CONFIGURATION@@"))

if sys.argv[1:] == ["--list-tests"]:
    tests = [
        {"id": selector, "driver": configuration["behaviors"][selector].get("driver", "urun")}
        for selector in sorted(configuration["behaviors"])
    ]
    print(json.dumps({"protocol": 2, "tests": tests}, separators=(",", ":")))
    raise SystemExit(0)

if len(sys.argv) >= 3 and sys.argv[1] == "--run-test":
    selector = sys.argv[2]
    behavior = configuration["behaviors"].get(selector)
    if behavior is None:
        raise SystemExit(2)
    remaining = sys.argv[3:]
    arguments = []
    while remaining:
        if len(remaining) < 3 or remaining[0] != "--arg":
            raise SystemExit(2)
        arguments.append([remaining[1], remaining[2]])
        remaining = remaining[3:]
    for relative in behavior.get("directories", []):
        Path(relative).mkdir(parents=True, exist_ok=True)
    for relative, encoded in behavior.get("writes", {}).items():
        path = Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes.fromhex(encoded))
    if behavior.get("emit_context"):
        print(json.dumps({"args": arguments, "cwd": Path.cwd().name}, separators=(",", ":")))
    if behavior.get("output"):
        print(behavior["output"])
    outcome = behavior["outcome"]
    if outcome == "crash":
        raise SystemExit(1)
    if outcome == "malformed":
        print("VIEWSPACE-TEST-RESULT 1 wrong::selector PASS")
        raise SystemExit(0)
    result = "PASS" if outcome == "pass" else "FAIL"
    print(f"VIEWSPACE-TEST-RESULT 1 {selector} {result}")
    raise SystemExit(0 if outcome == "pass" else 1)

raise SystemExit(2)
"""


class HarnessTests(unittest.TestCase):
    def test_default_inventory_never_builds_engine(self):
        from unittest import mock

        root = Path(__file__).resolve().parents[2]
        with mock.patch.object(viewspace_test.subprocess, "Popen", side_effect=AssertionError("build")):
            binary = viewspace_test.build_unit_test_binary(root)
        self.assertEqual(binary.name, "empty_unit_inventory.py")
        self.assertEqual(viewspace_test.list_registered_tests(binary), [])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "tests"
        self.root.mkdir()
        self.binary = self.base / "fake-unit-tests"
        self.report = self.base / "report.json"
        self.html = self.base / "report.html"
        self.scratch = self.base / "scratch"

    def configure(self, behaviors: dict[str, dict[str, object]]) -> None:
        configuration = base64.b64encode(
            json.dumps({"behaviors": behaviors}).encode("utf-8")
        ).decode("ascii")
        self.binary.write_text(
            textwrap.dedent(FAKE_BINARY).lstrip().replace("@@CONFIGURATION@@", configuration),
            encoding="utf-8",
        )
        self.binary.chmod(0o755)

    def write_case(
        self,
        case_id: str,
        selector: str,
        *,
        driver: str = "urun",
        expectation: str = "sr",
        files: dict[str, bytes] | None = None,
        directories: tuple[str, ...] = (),
        config: dict[str, object] | None = None,
        scope: str | None = None,
        labels: tuple[str, ...] = (),
        prerequisites: tuple[str, ...] = (),
        golden_stdout: str | None = None,
        golden_stderr: str | None = None,
        golden_status: int | None = None,
        golden_tree: tuple[TreeEntry, ...] = (),
        expanded_golden: bool = False,
        compare_script: str | None = None,
    ) -> Path:
        case = viewspace_test.case_path_for(self.root, case_id, driver, expectation)
        case.mkdir(parents=True)
        if files or directories:
            (case / "files").mkdir()
            for directory in directories:
                (case / "files" / directory).mkdir(parents=True)
            for relative, contents in (files or {}).items():
                path = case / "files" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
        (case / "golden").mkdir()
        if compare_script is not None:
            (case / "compare.py").write_text(compare_script, encoding="utf-8")
        if config is None:
            config = {} if driver == "urun" else {"cwd": ".", "args": {}}
        manifest = {
            "schema_version": 2,
            "driver": driver,
            "selector": selector,
            "labels": list(labels),
            "prerequisites": list(prerequisites),
            "config": config,
        }
        if scope is not None:
            manifest["scope"] = scope
        (case / "spec.json").write_text(
            json.dumps(manifest, indent=2)
            + "\n",
            encoding="utf-8",
        )
        if golden_stdout is not None and driver == "urun":
            (case / "golden" / "stdout").write_text(golden_stdout, encoding="utf-8")
        elif driver in ("cargo", "exec"):
            if golden_stdout is not None:
                (case / "golden" / "stdout").write_text(golden_stdout, encoding="utf-8")
            if golden_stderr is not None:
                (case / "golden" / "stderr").write_text(golden_stderr, encoding="utf-8")
            if golden_status is not None:
                (case / "golden" / "status").write_text(
                    f"{golden_status}\n", encoding="ascii"
                )
            if driver == "exec" and {
                "filesystem",
                "filesystem-script",
            } & set((config or {}).get("compare", [])):
                (case / "golden" / "expected.zip").write_bytes(
                    snapshot_zip_bytes(golden_tree)
                )
        elif golden_stdout is not None and expanded_golden:
            (case / "golden" / "stdout").write_text(golden_stdout, encoding="utf-8")
            (case / "golden" / "files").mkdir()
            for entry in golden_tree:
                path = case / "golden" / "files" / entry.path.decode("utf-8")
                if entry.kind == "directory":
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(entry.contents)
        elif golden_stdout is not None:
            archive = (
                TreeEntry(b"stdout", "file", golden_stdout.encode("utf-8")),
                TreeEntry(b"files", "directory"),
                *(TreeEntry(b"files/" + entry.path, entry.kind, entry.contents) for entry in golden_tree),
            )
            (case / "golden" / "expected.zip").write_bytes(snapshot_zip_bytes(archive))
        return case

    @staticmethod
    def output(selector: str, prefix: str = "") -> str:
        return f"{prefix}VIEWSPACE-TEST-RESULT 1 {selector} PASS\n"

    def run_harness(
        self, selection: viewspace_test.Selection | None = None, *, update: bool = False
    ) -> int:
        return viewspace_test.run_suite(
            self.root,
            self.binary,
            self.report,
            self.html,
            selection,
            update_golden=update,
            scratch_root=self.scratch,
        )

    def test_pass_run_writes_matching_json_and_visible_html(self) -> None:
        selector = "component::passes"
        self.configure({selector: {"outcome": "pass", "output": "visible output"}})
        self.write_case(
            "component::topic::visible",
            selector,
            scope="basic",
            golden_stdout=self.output(selector, "visible output\n"),
        )
        self.assertEqual(self.run_harness(), 0)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["summary"]["unscoped"], 0)
        self.assertEqual(report["cases"][0]["case_id"], "component::topic::visible")
        self.assertEqual(report["cases"][0]["scope"], "basic")
        html = self.html.read_text(encoding="utf-8")
        self.assertIn("visible output", html)
        self.assertIn("<dd>basic</dd>", html)
        self.assertNotIn("<details", html)

    def test_selection_supports_exact_segment_tag_and_glob_and_rejects_empty(self) -> None:
        behaviors = {
            "a::one": {"outcome": "pass"},
            "b::two": {"outcome": "pass"},
        }
        self.configure(behaviors)
        self.write_case("a::topic::first", "a::one", golden_stdout=self.output("a::one"))
        self.write_case("b::other::second", "b::two", golden_stdout=self.output("b::two"))
        selection = viewspace_test.Selection(segments=("topic",), tags=("urun",), globs=("a/*",))
        self.assertEqual(self.run_harness(selection), 0)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual([case["case_id"] for case in report["cases"]], ["a::topic::first"])
        self.assertEqual(self.run_harness(viewspace_test.Selection(globs=("missing/*",))), 2)

    def test_scope_selection_is_cumulative_and_unscoped_is_always_included(self) -> None:
        behaviors = {
            "scope::basic": {"outcome": "pass"},
            "scope::full": {"outcome": "pass"},
            "scope::exhaustive": {"outcome": "pass"},
            "scope::legacy": {"outcome": "pass"},
        }
        self.configure(behaviors)
        self.write_case(
            "component::scope::basic",
            "scope::basic",
            scope="basic",
            golden_stdout=self.output("scope::basic"),
        )
        self.write_case(
            "component::scope::full",
            "scope::full",
            scope="full",
            golden_stdout=self.output("scope::full"),
        )
        self.write_case(
            "component::scope::exhaustive",
            "scope::exhaustive",
            scope="exhaustive",
            golden_stdout=self.output("scope::exhaustive"),
        )
        self.write_case(
            "component::scope::legacy",
            "scope::legacy",
            golden_stdout=self.output("scope::legacy"),
        )

        expected = {
            "basic": ["component::scope::basic", "component::scope::legacy"],
            "full": [
                "component::scope::basic",
                "component::scope::full",
                "component::scope::legacy",
            ],
            "exhaustive": [
                "component::scope::basic",
                "component::scope::exhaustive",
                "component::scope::full",
                "component::scope::legacy",
            ],
        }
        for scope, selected in expected.items():
            with self.subTest(scope=scope):
                self.assertEqual(self.run_harness(viewspace_test.Selection(scope=scope)), 0)
                report = json.loads(self.report.read_text(encoding="utf-8"))
                self.assertEqual(report["summary"]["unscoped"], 1)
                self.assertEqual(
                    [case["case_id"] for case in report["cases"]],
                    selected,
                )

    def test_manifest_is_strict_and_directory_driver_must_agree(self) -> None:
        selector = "component::case"
        self.configure({selector: {"outcome": "pass"}})
        case = self.write_case(
            "component::topic::case", selector, golden_stdout=self.output(selector)
        )
        spec = json.loads((case / "spec.json").read_text(encoding="utf-8"))
        spec["unknown"] = True
        (case / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(viewspace_test.HarnessError, "unknown"):
            viewspace_test.scan_case_tree(self.root)

        spec.pop("unknown")
        spec["scope"] = "daily"
        (case / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(viewspace_test.HarnessError, "invalid scope"):
            viewspace_test.scan_case_tree(self.root)

        spec["scope"] = "basic"
        spec["labels"] = ["host"]
        (case / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(viewspace_test.HarnessError, "basic scope cannot use"):
            viewspace_test.scan_case_tree(self.root)

    def test_files_directory_is_optional_but_must_not_be_empty_or_a_placeholder(self) -> None:
        selector = "component::case"
        self.configure({selector: {"outcome": "pass"}})
        case = self.write_case(
            "component::topic::case", selector, golden_stdout=self.output(selector)
        )
        loaded = viewspace_test.scan_case_tree(self.root, allow_missing_goldens=True)
        self.assertEqual(loaded["component::topic::case"].input_tree, ())

        (case / "files").mkdir()
        with self.assertRaisesRegex(viewspace_test.HarnessError, "empty files/ is redundant"):
            viewspace_test.scan_case_tree(self.root)

        (case / "files" / ".gitkeep").touch()
        with self.assertRaisesRegex(viewspace_test.HarnessError, "is not a fixture"):
            viewspace_test.scan_case_tree(self.root)

        (case / "files" / ".gitkeep").unlink()
        (case / "files" / "real").write_text("content", encoding="utf-8")
        with self.assertRaisesRegex(viewspace_test.HarnessError, "urun files/ must be empty"):
            viewspace_test.scan_case_tree(self.root)

    def test_source_archive_is_the_complete_fixture_tree(self) -> None:
        selector = "component::directory"
        self.configure({selector: {"driver": "udir", "outcome": "pass"}})
        case = self.write_case(
            "component::topic::archive",
            selector,
            driver="udir",
            config={"cwd": ".", "args": {}},
        )
        files = case / "files"
        files.mkdir()
        expected = (
            TreeEntry(b"bin", "directory"),
            TreeEntry(b"bin/tool", "file", b"#!/bin/sh\n", True),
            TreeEntry(b"tool", "symlink", b"bin/tool"),
        )
        (files / "source.zip").write_bytes(snapshot_zip_bytes(expected))

        loaded = viewspace_test.scan_case_tree(self.root, allow_missing_goldens=True)

        self.assertEqual(loaded["component::topic::archive"].input_tree, expected)

        (files / "extra").write_bytes(b"not allowed")
        with self.assertRaisesRegex(viewspace_test.HarnessError, "only entry"):
            viewspace_test.scan_case_tree(self.root, allow_missing_goldens=True)

    def test_controlled_failure_is_expected_but_crash_is_infrastructure(self) -> None:
        failed = "component::failed"
        crashed = "component::crashed"
        self.configure(
            {
                failed: {"outcome": "fail"},
                crashed: {"outcome": "crash"},
            }
        )
        self.write_case(
            "component::topic::failed",
            failed,
            expectation="fr",
            golden_stdout=f"VIEWSPACE-TEST-RESULT 1 {failed} FAIL\n",
        )
        crash_case = self.write_case(
            "component::topic::crashed", crashed, expectation="fr"
        )
        self.assertEqual(self.run_harness(update=False), 2)
        before = list((crash_case / "golden").iterdir())
        self.assertEqual(
            self.run_harness(
                viewspace_test.Selection(cases=("component::topic::crashed",)), update=True
            ),
            2,
        )
        self.assertEqual(list((crash_case / "golden").iterdir()), before)

    def test_golden_update_requires_selection_and_creates_urun_golden(self) -> None:
        selector = "component::case"
        self.configure({selector: {"outcome": "pass", "output": "new"}})
        case = self.write_case("component::topic::case", selector)
        self.assertEqual(self.run_harness(update=True), 2)
        selection = viewspace_test.Selection(cases=("component::topic::case",))
        self.assertEqual(self.run_harness(selection, update=True), 0)
        self.assertEqual(
            (case / "golden" / "stdout").read_text(encoding="utf-8"),
            self.output(selector, "new\n"),
        )

    def test_udir_update_writes_canonical_archive_and_normal_run_reads_it(self) -> None:
        selector = "component::directory"
        self.configure(
            {
                selector: {
                    "driver": "udir",
                    "outcome": "pass",
                    "writes": {"result.txt": b"done\n".hex()},
                    "emit_context": True,
                }
            }
        )
        case = self.write_case(
            "component::directory::mutates",
            selector,
            driver="udir",
            directories=("work",),
            config={"cwd": "work", "args": {"name": "value"}},
        )
        selection = viewspace_test.Selection(cases=("component::directory::mutates",))
        self.assertEqual(self.run_harness(selection, update=True), 0)
        self.assertEqual([path.name for path in (case / "golden").iterdir()], ["expected.zip"])
        snapshot = snapshot_source(case / "golden" / "expected.zip")
        self.assertIn(TreeEntry(b"files/work/result.txt", "file", b"done\n"), snapshot)
        self.assertEqual(self.run_harness(), 0)

    def test_expanded_udir_golden_is_read_but_update_normalizes_it(self) -> None:
        selector = "component::directory"
        stdout = self.output(selector)
        self.configure({selector: {"driver": "udir", "outcome": "pass"}})
        case = self.write_case(
            "component::directory::expanded",
            selector,
            driver="udir",
            golden_stdout=stdout,
            expanded_golden=True,
        )
        self.assertEqual(self.run_harness(), 0)
        selection = viewspace_test.Selection(cases=("component::directory::expanded",))
        self.assertEqual(self.run_harness(selection, update=True), 0)
        self.assertEqual([path.name for path in (case / "golden").iterdir()], ["expected.zip"])

    def test_inventory_rejects_missing_and_stale_registered_selectors(self) -> None:
        self.configure({"component::registered": {"outcome": "pass"}})
        self.write_case(
            "component::topic::stale",
            "component::stale",
            golden_stdout=self.output("component::stale"),
        )
        self.assertEqual(self.run_harness(), 2)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        error = report["global_errors"][0]
        self.assertIn("without cases", error)
        self.assertIn("stale selectors", error)

    def test_report_targets_cannot_enter_case_tree(self) -> None:
        selector = "component::case"
        self.configure({selector: {"outcome": "pass"}})
        self.write_case(
            "component::topic::case", selector, golden_stdout=self.output(selector)
        )
        nested_report = self.root / "report.json"
        self.assertEqual(
            viewspace_test.run_suite(
                self.root,
                self.binary,
                nested_report,
                self.html,
                viewspace_test.Selection(),
                scratch_root=self.scratch,
            ),
            2,
        )

    def test_quiescent_process_reports_and_kills_detached_descendants(self) -> None:
        completed, descendant_error = viewspace_test._run_quiescent_process(
            [
                sys.executable,
                "-c",
                "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], start_new_session=True)",
            ]
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIsNotNone(descendant_error)

    def test_binary_path_swap_during_digest_is_detected(self) -> None:
        self.configure({"component::passes": {"outcome": "pass"}})
        replacement = self.base / "replacement"
        replacement.write_bytes(b"replacement executable")
        replacement.chmod(0o755)
        real_digest = viewspace_test._descriptor_digest
        swapped = False

        def digest_then_swap(descriptor: int) -> str:
            nonlocal swapped
            digest = real_digest(descriptor)
            if not swapped:
                os.replace(replacement, self.binary)
                swapped = True
            return digest

        with viewspace_test._pin_binary(self.binary) as pinned:
            with mock.patch.object(
                viewspace_test, "_descriptor_digest", side_effect=digest_then_swap
            ):
                with self.assertRaisesRegex(
                    viewspace_test.HarnessError, r"binary (?:path )?changed"
                ):
                    pinned.verify_source()
        self.assertTrue(swapped)

    def test_harness_operations_take_one_exclusive_root_lock(self) -> None:
        ready = self.base / "lock-ready"
        acquired = self.base / "lock-acquired"
        with viewspace_test._harness_operation_lock(self.root):
            with viewspace_test._harness_operation_lock(self.root):
                pass

        child_code = """
from pathlib import Path
import sys
from utils import viewspace_test
root, ready, acquired = map(Path, sys.argv[1:])
ready.write_text("ready")
with viewspace_test._harness_operation_lock(root):
    acquired.write_text("acquired")
"""
        with viewspace_test._harness_operation_lock(self.root):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(child_code),
                    os.fspath(self.root),
                    os.fspath(ready),
                    os.fspath(acquired),
                ],
                cwd=Path(__file__).resolve().parents[2],
            )
            self.addCleanup(lambda: child.poll() is None and child.kill())
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            self.assertFalse(acquired.exists())
        self.assertEqual(child.wait(timeout=2), 0)
        self.assertTrue(acquired.exists())

    def test_report_destination_is_exclusively_claimed_without_waiting(self) -> None:
        child_code = """
from pathlib import Path
import sys
from utils import viewspace_test
try:
    with viewspace_test._report_operation_lock(Path(sys.argv[1])):
        raise SystemExit(1)
except viewspace_test.HarnessError:
    raise SystemExit(0)
"""
        with viewspace_test._report_operation_lock(self.report):
            completed = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(child_code), os.fspath(self.report)],
                cwd=Path(__file__).resolve().parents[2],
                check=False,
            )
        self.assertEqual(completed.returncode, 0)

    def test_subreaper_is_reenabled_after_fork(self) -> None:
        viewspace_test._enable_child_subreaper()
        child = os.fork()
        if child == 0:
            try:
                leader = [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess; "
                        "subprocess.Popen(['/bin/sleep','10'], start_new_session=True)"
                    ),
                ]
                _, descendant_error = viewspace_test._run_quiescent_process(leader)
                os._exit(0 if descendant_error is not None else 1)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)

    def test_large_supervisor_start_error_is_bounded(self) -> None:
        started = time.monotonic()
        with self.assertRaises(viewspace_test.HarnessError) as raised:
            viewspace_test._run_quiescent_process(["x" * 1_000_000])
        self.assertLess(time.monotonic() - started, 5)
        self.assertLessEqual(len(str(raised.exception).encode("utf-8")), 4096)

    def test_interrupted_wait_kills_and_reaps_the_controlled_process(self) -> None:
        existing_descendants = viewspace_test._descendant_processes(os.getpid())
        interrupter = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
        interrupter.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                viewspace_test._run_quiescent_process(["/bin/sleep", "30"])
        finally:
            interrupter.cancel()
            interrupter.join()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if viewspace_test._descendant_processes(os.getpid()) == existing_descendants:
                break
            time.sleep(0.01)
        self.assertEqual(
            viewspace_test._descendant_processes(os.getpid()), existing_descendants
        )

    def test_process_supervision_does_not_touch_unrelated_children(self) -> None:
        unrelated = subprocess.Popen(["/bin/sleep", "30"])
        try:
            completed, descendant_error = viewspace_test._run_quiescent_process(["/bin/true"])
            self.assertEqual(completed.returncode, 0)
            self.assertIsNone(descendant_error)
            self.assertIsNone(unrelated.poll())
        finally:
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait()

    def test_supervisor_reaps_adopted_orphans_without_changing_leader_status(
        self,
    ) -> None:
        leader = """
import subprocess
import sys
import time
subprocess.Popen([
    sys.executable,
    '-c',
    "import subprocess,sys; subprocess.Popen([sys.executable,'-c','pass']); raise SystemExit(0)",
])
time.sleep(0.25)
raise SystemExit(37)
"""
        completed, descendant_error = viewspace_test._run_quiescent_process(
            [sys.executable, "-c", textwrap.dedent(leader)],
            timeout_seconds=10,
            temporary_root=self.base,
        )
        self.assertEqual(completed.returncode, 37)
        self.assertIsNone(descendant_error)

        completed, descendant_error = viewspace_test._run_quiescent_process(
            [
                sys.executable,
                "-c",
                "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
            ],
            timeout_seconds=10,
            temporary_root=self.base,
        )
        self.assertEqual(completed.returncode, -signal.SIGTERM)
        self.assertIsNone(descendant_error)

    def test_abrupt_outer_harness_death_stops_owned_processes(self) -> None:
        wrapper = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from utils import viewspace_test; viewspace_test._run_quiescent_process(['/bin/sleep','30'])",
            ],
            cwd=Path(__file__).resolve().parents[2],
        )
        owned: set[int] = set()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                owned = viewspace_test._descendant_processes(wrapper.pid)
                if len(owned) >= 2:
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(len(owned), 2)
            wrapper.kill()
            wrapper.wait(timeout=2)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all(not Path(f"/proc/{process_id}").exists() for process_id in owned):
                    break
                for process_id in owned:
                    try:
                        os.waitpid(process_id, os.WNOHANG)
                    except ChildProcessError:
                        pass
                time.sleep(0.01)
            self.assertTrue(
                all(not Path(f"/proc/{process_id}").exists() for process_id in owned)
            )
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
            wrapper.wait()
            for process_id in owned:
                try:
                    os.kill(process_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(process_id, 0)
                except ChildProcessError:
                    pass

    def test_cli_parser_exposes_golden_and_selection_controls(self) -> None:
        options = viewspace_test.build_argument_parser(self.base).parse_args(
            [
                "run",
                "--update-golden",
                "--case",
                "component::topic::case",
                "--segment",
                "topic",
                "--tag",
                "urun",
                "--glob",
                "component/*",
                "--scope",
                "full",
                "--profile",
                "full",
                "--scratch-root",
                os.fspath(self.scratch),
            ]
        )
        self.assertTrue(options.update_golden)
        self.assertEqual(options.case, ["component::topic::case"])
        self.assertEqual(options.segment, ["topic"])
        self.assertEqual(options.tag, ["urun"])
        self.assertEqual(options.globs, ["component/*"])
        self.assertEqual(options.scope, "full")
        self.assertEqual(options.profile, "full")
        self.assertEqual(options.scratch_root, self.scratch)
        default_options = viewspace_test.build_argument_parser(self.base).parse_args(["run"])
        self.assertEqual(default_options.scope, "exhaustive")

    def test_scratch_root_is_private_external_and_reported(self) -> None:
        repository = self.base / "repository"
        repository.mkdir()
        with self.assertRaisesRegex(viewspace_test.HarnessError, "outside"):
            viewspace_test._prepare_scratch_root(repository / "scratch", repository)

        permissive = self.base / "permissive"
        permissive.mkdir(mode=0o755)
        with self.assertRaisesRegex(viewspace_test.HarnessError, "group or other"):
            viewspace_test._prepare_scratch_root(permissive, repository)

        selector = "component::passes"
        self.configure({selector: {"outcome": "pass"}})
        self.write_case(
            "component::topic::scratch",
            selector,
            golden_stdout=self.output(selector),
        )
        self.assertEqual(self.run_harness(), 0)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["scratch_root"], os.fspath(self.scratch.resolve()))
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_exec_uses_direct_argv_declared_environment_and_fresh_fixture(self) -> None:
        script = textwrap.dedent(
            """
            import os
            from pathlib import Path
            import sys
            if Path("marker").exists():
                raise SystemExit(9)
            Path("marker").write_text("created\\n", encoding="utf-8")
            print(sys.argv[1])
            print(os.environ.get("ONLY", "missing"))
            """
        ).lstrip().encode("utf-8")
        config = {
            "argv": [sys.executable, "case.py", "$(touch should-not-exist)"],
            "cwd": {"anchor": "workspace", "path": "."},
            "environment": {"inherit": [], "set": {"ONLY": "declared"}},
            "timeout_seconds": 2,
            "compare": ["status", "stdout", "stderr"],
            "filesystem": None,
        }
        self.write_case(
            "component::exec::direct",
            "exec::direct",
            driver="exec",
            files={"case.py": script},
            config=config,
            golden_stdout="$(touch should-not-exist)\ndeclared\n",
            golden_stderr="",
            golden_status=0,
        )
        self.configure({"unit::placeholder": {"outcome": "pass"}})
        self.write_case(
            "unit::placeholder::case",
            "unit::placeholder",
            golden_stdout=self.output("unit::placeholder"),
        )
        selection = viewspace_test.Selection(cases=("component::exec::direct",))
        self.assertEqual(self.run_harness(selection), 0)
        self.assertEqual(self.run_harness(selection), 0)
        self.assertFalse((self.base / "should-not-exist").exists())

    def test_exec_timeout_is_infrastructure_not_expected_failure(self) -> None:
        config = {
            "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
            "cwd": {"anchor": "workspace", "path": "."},
            "environment": {"inherit": [], "set": {}},
            "timeout_seconds": 0.05,
            "compare": ["status"],
            "filesystem": None,
        }
        self.write_case(
            "component::exec::timeout",
            "exec::timeout",
            driver="exec",
            expectation="fr",
            files={"placeholder": b"fixture\n"},
            config=config,
            golden_status=1,
        )
        self.configure({"unit::placeholder": {"outcome": "pass"}})
        self.write_case(
            "unit::placeholder::case",
            "unit::placeholder",
            golden_stdout=self.output("unit::placeholder"),
        )
        self.assertEqual(
            self.run_harness(viewspace_test.Selection(cases=("component::exec::timeout",))),
            2,
        )
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["cases"][0]["status"], "infrastructure_failed")
        self.assertIn("timed out", report["cases"][0]["message"])

    def test_exec_filesystem_comparison_reports_file_diff(self) -> None:
        config = {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('result').mkdir(); Path('result/out').write_text('actual\\n')",
            ],
            "cwd": {"anchor": "workspace", "path": "."},
            "environment": {"inherit": [], "set": {}},
            "timeout_seconds": 2,
            "compare": ["status", "filesystem"],
            "filesystem": "result",
        }
        self.write_case(
            "component::exec::filesystem",
            "exec::filesystem",
            driver="exec",
            config=config,
            golden_status=0,
            golden_tree=(TreeEntry(b"out", "file", b"expected\n"),),
        )
        self.configure({"unit::placeholder": {"outcome": "pass"}})
        self.write_case(
            "unit::placeholder::case",
            "unit::placeholder",
            golden_stdout=self.output("unit::placeholder"),
        )
        selection = viewspace_test.Selection(cases=("component::exec::filesystem",))
        self.assertEqual(self.run_harness(selection), 1)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertIn("-expected", report["cases"][0]["diff"])
        self.assertIn("+actual", report["cases"][0]["diff"])

    def test_exec_semantic_comparator_ignores_volatile_files_and_reports_output(self) -> None:
        config = {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('result').mkdir(); Path('result/stable').write_text('same\\n'); Path('result/volatile').write_text('runtime-value\\n')",
            ],
            "cwd": {"anchor": "workspace", "path": "."},
            "environment": {"inherit": [], "set": {}},
            "timeout_seconds": 2,
            "compare": ["status", "filesystem-script"],
            "filesystem": "result",
        }
        comparator = textwrap.dedent(
            """
            from pathlib import Path
            import sys
            import zipfile

            with zipfile.ZipFile(sys.argv[1]) as archive:
                expected = archive.read("stable")
            actual = (Path(sys.argv[2]) / "stable").read_bytes()
            if actual != expected:
                print(f"stable mismatch: expected={expected!r} actual={actual!r}")
                raise SystemExit(1)
            print("semantic comparison inspected stable and ignored volatile")
            """
        ).lstrip()
        self.write_case(
            "component::exec::semantic",
            "exec::semantic",
            driver="exec",
            config=config,
            compare_script=comparator,
            golden_status=0,
            golden_tree=(
                TreeEntry(b"stable", "file", b"same\n"),
                TreeEntry(b"volatile", "file", b"different-golden-value\n"),
            ),
        )
        self.configure({"unit::placeholder": {"outcome": "pass"}})
        self.write_case(
            "unit::placeholder::case",
            "unit::placeholder",
            golden_stdout=self.output("unit::placeholder"),
        )

        selection = viewspace_test.Selection(cases=("component::exec::semantic",))
        self.assertEqual(self.run_harness(selection), 0)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertIn("ignored volatile", report["cases"][0]["comparison_stdout"])
        self.assertIn("ignored volatile", self.html.read_text(encoding="utf-8"))

        config["argv"][-1] = config["argv"][-1].replace("same", "changed")
        case = viewspace_test.case_path_for(
            self.root, "component::exec::semantic", "exec", "sr"
        )
        spec = json.loads((case / "spec.json").read_text(encoding="utf-8"))
        spec["config"] = config
        (case / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        self.assertEqual(self.run_harness(selection), 1)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertIn("stable mismatch", report["cases"][0]["diff"])

    def test_semantic_comparator_failure_is_infrastructure_but_golden_update_bypasses_it(
        self,
    ) -> None:
        config = {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('result').mkdir(); Path('result/value').write_text('new\\n')",
            ],
            "cwd": {"anchor": "workspace", "path": "."},
            "environment": {"inherit": [], "set": {}},
            "timeout_seconds": 2,
            "compare": ["status", "filesystem-script"],
            "filesystem": "result",
        }
        case = self.write_case(
            "component::exec::broken-comparator",
            "exec::broken_comparator",
            driver="exec",
            config=config,
            compare_script="raise SystemExit(2)\n",
            golden_status=0,
            golden_tree=(TreeEntry(b"value", "file", b"old\n"),),
        )
        self.configure({"unit::placeholder": {"outcome": "pass"}})
        self.write_case(
            "unit::placeholder::case",
            "unit::placeholder",
            golden_stdout=self.output("unit::placeholder"),
        )
        selection = viewspace_test.Selection(
            cases=("component::exec::broken-comparator",)
        )

        self.assertEqual(self.run_harness(selection), 2)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertIn("unsupported status 2", report["cases"][0]["message"])

        self.assertEqual(self.run_harness(selection, update=True), 0)
        updated = snapshot_source(case / "golden/expected.zip")
        self.assertEqual(updated, (TreeEntry(b"value", "file", b"new\n"),))

    def test_missing_prerequisite_is_visible_skip_in_selected_profile(self) -> None:
        config = {
            "argv": [sys.executable, "-c", "raise SystemExit(99)"],
            "cwd": {"anchor": "workspace", "path": "."},
            "environment": {"inherit": [], "set": {}},
            "timeout_seconds": 1,
            "compare": ["status"],
            "filesystem": None,
        }
        self.write_case(
            "component::host::unavailable",
            "host::unavailable",
            driver="exec",
            config=config,
            labels=("host",),
            prerequisites=("command:viewspace-command-that-does-not-exist",),
            golden_status=0,
        )
        self.configure({"unit::placeholder": {"outcome": "pass"}})
        self.write_case(
            "unit::placeholder::case",
            "unit::placeholder",
            golden_stdout=self.output("unit::placeholder"),
        )
        selection = viewspace_test.Selection(
            cases=("component::host::unavailable",), profile="full"
        )
        self.assertEqual(self.run_harness(selection), 0)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertIn("missing prerequisites", report["cases"][0]["message"])

        basic_selection = viewspace_test.Selection(
            cases=("component::host::unavailable",), scope="basic", profile="full"
        )
        self.assertEqual(self.run_harness(basic_selection), 2)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(report["summary"]["infrastructure_failed"], 1)
        self.assertIn(
            "Basic regression does not permit skipped cases",
            report["global_errors"],
        )

    def test_python_module_prerequisite_can_name_an_exact_interpreter(self) -> None:
        self.assertTrue(
            viewspace_test._python_module_available(f"{sys.executable}:json")
        )
        self.assertFalse(
            viewspace_test._python_module_available(
                f"{sys.executable}:viewspace_module_that_does_not_exist"
            )
        )
        self.assertFalse(
            viewspace_test._python_module_available("viewspace-python-that-does-not-exist:json")
        )
        self.assertFalse(viewspace_test._python_module_available("true:json"))
        self.assertTrue(
            viewspace_test._python_module_available(f"{sys.executable}:this")
        )
        failed_import = subprocess.CompletedProcess([], 1)
        with mock.patch.object(
            viewspace_test.subprocess,
            "run",
            return_value=failed_import,
        ) as run:
            self.assertFalse(viewspace_test._python_module_available("json"))
        self.assertIn("import_module", run.call_args.args[0][4])

    def test_cargo_adapter_attributes_two_tests_from_one_built_target(self) -> None:
        executable = self.base / "fake-cargo-test"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if sys.argv[1:3] == ['--list', '--format']:\n"
            " print('module::first: test')\n print('module::second: test')\n"
            " raise SystemExit(0)\n"
            "print('running 1 test')\nprint(sys.argv[2])\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        config = {
            "package": "component",
            "target": {"kind": "lib", "name": "component"},
            "timeout_seconds": 2,
        }
        cases = []
        for name in ("first", "second"):
            self.write_case(
                f"component::cargo::{name}",
                f"module::{name}",
                driver="cargo",
                config=config,
                golden_status=0,
            )
        loaded = viewspace_test.scan_case_tree(self.root)
        target = viewspace_test.CargoExecutable(
            executable,
            frozenset({"module::first", "module::second", "module::ignored"}),
            frozenset({"module::ignored"}),
        )
        prepared = {("component", "lib", "component"): target}
        viewspace_test.check_cargo_inventory(loaded, prepared)
        for name in ("first", "second"):
            case = loaded[f"component::cargo::{name}"]
            result = viewspace_test.invoke_cargo(self.base, case, prepared)
            self.assertEqual(result.outcome, "passed")
            self.assertIn(f"module::{name}", result.stdout)

        incomplete = viewspace_test.CargoExecutable(
            executable,
            frozenset({"module::first", "module::second", "module::third"}),
            frozenset(),
        )
        with self.assertRaisesRegex(
            viewspace_test.HarnessError, "non-ignored Cargo tests without cases"
        ):
            viewspace_test.check_cargo_inventory(
                loaded, {("component", "lib", "component"): incomplete}
            )


if __name__ == "__main__":
    unittest.main()
