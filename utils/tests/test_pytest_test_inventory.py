from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from utils import pytest_test_inventory


class PytestTestInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pytest-test-inventory-")
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "repository"
        self.source_root = self.repository / "viewspace-intake/tests"
        self.test_root = self.repository / "tests"
        self.source_root.mkdir(parents=True)
        self.test_root.mkdir()
        pyproject = self.repository / "viewspace-intake/pyproject.toml"
        pyproject.write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\ntestpaths = ['tests']\n",
            encoding="utf-8",
        )

    def write_source(self, relative: str, contents: str) -> None:
        path = self.source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def write_descriptor(self, name: str, arguments: list[str]) -> None:
        path = self.test_root / f"viewspace-intake/python/{name}=exec+sr/spec.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "driver": "exec",
                    "prerequisites": ["command:python3", "python-module:pytest"],
                    "config": {
                        "argv": arguments,
                        "environment": {
                            "inherit": ["PATH"],
                            "set": {
                                "PYTHONDONTWRITEBYTECODE": "1",
                                "PYTHONPATH": "viewspace-intake/src",
                                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                                "TMPDIR": "{workspace}",
                            },
                        },
                        "cwd": {"anchor": "repository", "path": "."},
                        "compare": ["status"],
                    },
                }
            ),
            encoding="utf-8",
        )
        golden = path.parent / "golden/status"
        golden.parent.mkdir()
        golden.write_text("0\n", encoding="utf-8")

    def test_exact_functions_are_discovered_without_importing(self) -> None:
        self.write_source(
            "test_example.py",
            "raise RuntimeError('must not import')\n"
            "import pytest\n"
            "@pytest.mark.parametrize('value', [1, 2])\n"
            "def test_parameterized(value): pass\n"
            "def test_second(): pass\n"
            "class TestGroup:\n"
            "    def test_method(self): pass\n",
        )
        selectors = [
            "viewspace-intake/tests/test_example.py::test_parameterized",
            "viewspace-intake/tests/test_example.py::test_second",
            "viewspace-intake/tests/test_example.py::TestGroup::test_method",
        ]
        self.write_descriptor(
            "exact",
            [
                "python3",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "utils.pytest_no_skips",
                *selectors,
            ],
        )
        self.assertEqual(
            pytest_test_inventory.audit(self.repository, self.test_root),
            3,
        )

    def test_missing_duplicate_stale_and_broad_selectors_are_reported(self) -> None:
        self.write_source(
            "test_example.py",
            "def test_first(): pass\ndef test_second(): pass\n",
        )
        exact = "viewspace-intake/tests/test_example.py::test_first"
        runner = [
            "python3",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "utils.pytest_no_skips",
        ]
        self.write_descriptor("first", [*runner, exact])
        self.write_descriptor("duplicate", [*runner, exact])
        self.write_descriptor(
            "stale",
            [*runner, "viewspace-intake/tests/test_example.py::test_gone"],
        )
        self.write_descriptor(
            "broad",
            [*runner, "viewspace-intake/tests/test_example.py"],
        )
        self.write_source("nested/test_nested.py", "def test_nested(): pass\n")
        self.write_descriptor(
            "broad-directory",
            [*runner, "viewspace-intake/tests/nested"],
        )

        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        message = str(caught.exception)
        self.assertIn("missing registrations", message)
        self.assertIn("test_second", message)
        self.assertIn("duplicate registrations", message)
        self.assertIn("stale registrations", message)
        self.assertIn("broad pytest selectors are forbidden", message)
        self.assertIn("viewspace-intake/tests/nested", message)

    def test_dynamic_and_inherited_test_shapes_fail_closed(self) -> None:
        self.write_source("test_control.py", "def test_control(): pass\n")
        invalid = (
            (
                "if True:\n    def test_conditional(): pass\n",
                "conditional or nested pytest tests",
            ),
            (
                "def helper(): pass\ntest_generated = helper\n",
                "dynamically assigned pytest tests",
            ),
            (
                "class Base: pass\nclass TestDerived(Base):\n    def test_method(self): pass\n",
                "inherited or decorated pytest class",
            ),
            (
                "class TestAssigned:\n    test_method = lambda self: None\n",
                "dynamically assigned pytest class tests",
            ),
            (
                "class TestConditional:\n    if True:\n        def test_method(self): pass\n",
                "conditional or nested pytest class tests",
            ),
            (
                "from helpers import test_external\n",
                "imported test bindings are unsupported",
            ),
            (
                "from helpers import *\n",
                "module-scope wildcard imports are unsupported",
            ),
            (
                "import pytest\n@pytest.mark.skip\ndef test_skipped(): pass\n",
                "pytest skip/xfail constructs are unsupported",
            ),
            (
                "import pytest as pt\ndef test_skipped(): pt.skip('later')\n",
                "pytest skip/xfail constructs are unsupported",
            ),
            (
                "from pytest import mark\n@mark.xfail\ndef test_xfail(): pass\n",
                "pytest skip/xfail constructs are unsupported",
            ),
            (
                "async def test_async(): pass\n",
                "async pytest tests are unsupported",
            ),
            (
                "def test_yield():\n    yield 1\n",
                "yield pytest tests are unsupported",
            ),
            (
                "class Hidden:\n    def test_method(self): pass\nTestAlias = Hidden\n",
                "dynamically assigned pytest tests are unsupported",
            ),
            (
                "from helpers import Hidden as TestAlias\n",
                "imported test bindings are unsupported",
            ),
            (
                "from helpers import PLUGINS as pytest_plugins\n",
                "pytest plugin bindings are unsupported",
            ),
            (
                "def helper(): pass\nhelper.__test__ = True\n",
                "dynamic pytest collection overrides are unsupported",
            ),
            (
                "def helper(): pass\nif True:\n    test_alias = helper\n",
                "dynamically assigned pytest tests are unsupported",
            ),
            (
                "def helper(): pass\nglobals()['test_alias'] = helper\n",
                "dynamically assigned pytest tests are unsupported",
            ),
            (
                "def helper(self): pass\nclass TestGroup:\n"
                "    def test_control(self): pass\n"
                "    if True:\n        test_alias = helper\n",
                "dynamically assigned pytest class tests are unsupported",
            ),
        )
        for index, (source, diagnostic) in enumerate(invalid):
            with self.subTest(index=index):
                path = self.source_root / f"test_invalid_{index}.py"
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
                    pytest_test_inventory.discover_functions(self.repository)
                self.assertIn(diagnostic, str(caught.exception))
                path.unlink()

    def test_owned_selector_outside_exact_pytest_runner_cannot_spoof_registration(self) -> None:
        self.write_source("test_example.py", "def test_first(): pass\n")
        selector = "viewspace-intake/tests/test_example.py::test_first"
        self.write_descriptor("spoof", ["python3", "helper.py", selector])

        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("outside the exact runner", str(caught.exception))

    def test_registration_contract_and_collection_configuration_fail_closed(self) -> None:
        self.write_source("test_example.py", "def test_first(): pass\n")
        selector = "viewspace-intake/tests/test_example.py::test_first"
        arguments = [
            "python3",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "utils.pytest_no_skips",
            selector,
        ]
        self.write_descriptor("exact", arguments)
        case = self.test_root / "viewspace-intake/python/exact=exec+sr"

        (case / "golden/status").write_text("1\n", encoding="utf-8")
        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("must expect status 0", str(caught.exception))
        (case / "golden/status").write_text("0\n", encoding="utf-8")

        spec_path = case / "spec.json"
        descriptor = json.loads(spec_path.read_text(encoding="utf-8"))
        descriptor["config"]["environment"]["set"]["PYTEST_ADDOPTS"] = "-k missing"
        spec_path.write_text(json.dumps(descriptor), encoding="utf-8")
        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("exact isolated environment", str(caught.exception))
        del descriptor["config"]["environment"]["set"]["PYTEST_ADDOPTS"]
        spec_path.write_text(json.dumps(descriptor), encoding="utf-8")

        descriptor["config"]["argv"].insert(-1, "other-tests")
        spec_path.write_text(json.dumps(descriptor), encoding="utf-8")
        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("unrelated selectors", str(caught.exception))
        descriptor["config"]["argv"].remove("other-tests")
        spec_path.write_text(json.dumps(descriptor), encoding="utf-8")

        pyproject = self.repository / "viewspace-intake/pyproject.toml"
        pyproject.write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\ntestpaths = ['tests']\n"
            "python_functions = ['test_*', 'check_*']\n",
            encoding="utf-8",
        )
        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("unsupported viewspace-intake pytest configuration", str(caught.exception))

        pyproject.write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\ntestpaths = ['tests']\n",
            encoding="utf-8",
        )
        (self.repository / "viewspace-intake/pytest.ini").write_text(
            "[pytest]\npython_functions = test_* check_*\n",
            encoding="utf-8",
        )
        with self.assertRaises(pytest_test_inventory.InventoryError) as caught:
            pytest_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("shadowing viewspace-intake pytest configuration", str(caught.exception))

    def test_runtime_plugin_rejects_nonexecuted_and_xfail_outcomes(self) -> None:
        scenarios = {
            "pass": "def test_passes(): pass\n",
            "skip-marker": "import pytest\n@pytest.mark.skip\ndef test_skipped(): pass\n",
            "runtime-skip": "import pytest\ndef test_skipped(): pytest.skip('later')\n",
            "xfail": "import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False\n",
            "empty-parametrize": (
                "import pytest\n@pytest.mark.parametrize('value', [])\n"
                "def test_empty(value): pass\n"
            ),
            "async": "async def test_async(): pass\n",
            "yield": "def test_yield():\n    yield 1\n",
            "deselected": "def test_deselected(): pass\n",
            "exit-zero": (
                "import pytest\n"
                "def test_aborts(): pytest.exit('stop', returncode=0)\n"
                "def test_never_runs(): pass\n"
            ),
        }
        plugin_root = Path(__file__).resolve().parents[2]
        for name, source in scenarios.items():
            with self.subTest(name=name):
                scenario = Path(self.temporary.name) / name
                scenario.mkdir()
                (scenario / "test_outcome.py").write_text(source, encoding="utf-8")
                if name == "deselected":
                    (scenario / "conftest.py").write_text(
                        "def pytest_collection_modifyitems(config, items):\n"
                        "    removed = list(items)\n"
                        "    items.clear()\n"
                        "    config.hook.pytest_deselected(items=removed)\n",
                        encoding="utf-8",
                    )
                environment = os.environ.copy()
                for key in list(environment):
                    if key.startswith("PYTEST_"):
                        del environment[key]
                environment["PYTHONPATH"] = os.fspath(plugin_root)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
                environment["TMPDIR"] = os.fspath(scenario)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "-p",
                        "utils.pytest_no_skips",
                        "-o",
                        "addopts=",
                        "test_outcome.py",
                    ],
                    cwd=scenario,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                expected_status = 0 if name == "pass" else 1
                self.assertEqual(
                    result.returncode,
                    expected_status,
                    msg=result.stdout + result.stderr,
                )
                if name != "pass":
                    self.assertIn(
                        "VIEWSPACE forbids skipped/xfail/nonexecuted pytest outcomes",
                        result.stdout + result.stderr,
                    )
