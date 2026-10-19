from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from utils import python_test_inventory


class PythonTestInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="python-test-inventory-")
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "repository"
        self.test_root = self.repository / "tests"
        for family in python_test_inventory.SOURCE_FAMILIES:
            (self.repository / family.directory).mkdir(parents=True, exist_ok=True)

    def write_source(self, relative: str, contents: str) -> None:
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def write_descriptor(self, name: str, arguments: list[str]) -> None:
        path = self.test_root / f"component/python/{name}=exec+sr/spec.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"config": {"argv": arguments}}), encoding="utf-8")

    def test_exact_ids_are_discovered_and_registered_without_importing(self) -> None:
        self.write_source(
            "utils/tests/test_example.py",
            "raise RuntimeError('must not import')\n"
            "from unittest import TestCase as Case\n"
            "class Helper:\n"
            "    def test_not_a_unittest(self): pass\n"
            "class Namespace:\n"
            "    class TestCase: pass\n"
            "class FakeTests(Namespace.TestCase):\n"
            "    def test_not_a_unittest_either(self): pass\n"
            "class ExampleTests(Case):\n"
            "    def test_first(self): pass\n"
            "    async def test_second(self): pass\n"
            "    def testCamel(self): pass\n"
            "class DerivedTests(ExampleTests):\n"
            "    def test_third(self): pass\n"
            "class Mixin:\n"
            "    def test_from_mixin(self): pass\n"
            "class MixedTests(Mixin, Case):\n"
            "    def test_own(self): pass\n",
        )
        self.write_source(
            "utils/tests/nested/test_nested.py",
            "import unittest as unit\n"
            "class NestedTests(unit.TestCase):\n"
            "    def test_nested(self): pass\n"
            "AssignedCase = unit.TestCase\n"
            "class AssignedAliasTests(AssignedCase):\n"
            "    def test_assigned_alias(self): pass\n",
        )
        self.write_source(
            "apps/git-viewspace/OverlaySync/tests/test_added.py",
            "from unittest import case as unit_case\n"
            "class AddedTests(unit_case.TestCase):\n"
            "    def test_added(self): pass\n",
        )
        self.write_source(
            "apps/git-viewspace-fs/git_viewspace_fs/tests/test_added.py",
            "from unittest import async_case\n"
            "class AddedAsyncTests(async_case.IsolatedAsyncioTestCase):\n"
            "    async def test_added_async(self): pass\n",
        )
        self.write_source(
            "utils/tests/test_dialogue_added.py",
            "import unittest\n"
            "class DialogueAddedTests(unittest.TestCase):\n"
            "    def test_dialogue_added(self): pass\n",
        )
        ids = [
            "utils.tests.test_example.ExampleTests.test_first",
            "utils.tests.test_example.ExampleTests.test_second",
            "utils.tests.test_example.ExampleTests.testCamel",
            "utils.tests.test_example.DerivedTests.test_first",
            "utils.tests.test_example.DerivedTests.test_second",
            "utils.tests.test_example.DerivedTests.test_third",
            "utils.tests.test_example.DerivedTests.testCamel",
            "utils.tests.test_example.MixedTests.test_from_mixin",
            "utils.tests.test_example.MixedTests.test_own",
            "utils.tests.nested.test_nested.NestedTests.test_nested",
            "utils.tests.nested.test_nested.AssignedAliasTests.test_assigned_alias",
            "test_added.AddedTests.test_added",
            "utils.tests.test_dialogue_added.DialogueAddedTests.test_dialogue_added",
        ]
        self.write_descriptor("exact", ["python3", "-m", "unittest", *ids])
        self.write_descriptor(
            "viewfs",
            [
                "python3",
                "{repository}/utils/run_git_viewspace_fs_unittests.py",
                "{repository}",
                "{workspace}",
                "--",
                "git_viewspace_fs.tests.test_added.AddedAsyncTests.test_added_async",
            ],
        )
        self.assertEqual(python_test_inventory.audit(self.repository, self.test_root), 14)

        invalid_sources = (
            (
                "unittest aliases are rebound: unittest",
                (
                    "import unittest\n"
                    "class Faux: pass\n"
                    "if True:\n"
                    "    unittest = Faux\n"
                    "class FakeTests(unittest.TestCase):\n"
                    "    def test_fake(self): pass\n"
                ),
            ),
            (
                "unittest aliases are rebound: unittest",
                "import unittest\nimport faux as unittest\n",
            ),
            (
                "unresolved top-level class base: RelativeTests",
                (
                    "from .unittest import TestCase\n"
                    "class RelativeTests(TestCase):\n"
                    "    def test_spoof(self): pass\n"
                ),
            ),
            (
                "unittest aliases have global rebinding declarations: unittest",
                (
                    "import unittest\n"
                    "class Rebind:\n"
                    "    global unittest\n"
                    "    unittest = object\n"
                ),
            ),
            (
                "module-scope test method mutation is unsupported",
                (
                    "import unittest\n"
                    "class DynamicTests(unittest.TestCase): pass\n"
                    "def test_added(self): pass\n"
                    "DynamicTests.test_added = test_added\n"
                ),
            ),
            (
                "TestCase ancestry class is rebound or aliased: Mixin",
                (
                    "import unittest\n"
                    "class Mixin:\n"
                    "    def test_original(self): pass\n"
                    "class MixedTests(Mixin, unittest.TestCase): pass\n"
                    "Mixin = object\n"
                ),
            ),
            (
                "dynamically assigned class test methods are unsupported",
                (
                    "import unittest\n"
                    "class Suppressor:\n"
                    "    test_hidden = None\n"
                    "class SuppressedTests(Suppressor, unittest.TestCase): pass\n"
                ),
            ),
        )
        for message, source in invalid_sources:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    python_test_inventory.InventoryError,
                    message,
                ):
                    python_test_inventory._test_case_classes(ast.parse(source))

        empty_module = "apps/git-viewspace/OverlaySync/tests/__init__.py"
        self.write_source(
            empty_module,
            "import unittest\n"
            "class RootTests(unittest.TestCase):\n"
            "    def test_root(self): pass\n",
        )
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "test-bearing module has no importable name",
        ):
            python_test_inventory.discover_methods(self.repository)
        (self.repository / empty_module).unlink()

        self.write_source(
            "utils/tests/collision.py",
            "import unittest\n"
            "class ModuleTests(unittest.TestCase):\n"
            "    def test_module(self): pass\n",
        )
        self.write_source(
            "utils/tests/collision/__init__.py",
            "import unittest\n"
            "class PackageTests(unittest.TestCase):\n"
            "    def test_package(self): pass\n",
        )
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "owned Python module name collision",
        ):
            python_test_inventory.discover_methods(self.repository)
        (self.repository / "utils/tests/collision.py").unlink()
        (self.repository / "utils/tests/collision/__init__.py").unlink()
        (self.repository / "utils/tests/collision").rmdir()

        self.write_source(
            "utils/tests/parent.py",
            "import unittest\n"
            "class ParentTests(unittest.TestCase):\n"
            "    def test_parent(self): pass\n",
        )
        self.write_source(
            "utils/tests/parent/child.py",
            "import unittest\n"
            "class ChildTests(unittest.TestCase):\n"
            "    def test_child(self): pass\n",
        )
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "owned Python module file shadows a package prefix",
        ):
            python_test_inventory.discover_methods(self.repository)

    def test_missing_duplicate_and_stale_ids_are_reported_together(self) -> None:
        self.write_source(
            "utils/tests/test_example.py",
            "import unittest\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_missing(self): pass\n"
            "    def test_repeated(self): pass\n",
        )
        repeated = "utils.tests.test_example.ExampleTests.test_repeated"
        stale = "utils.tests.test_example.ExampleTests.test_removed"
        removed_module = "utils.tests.test_deleted.DeletedTests.test_removed"
        self.write_descriptor(
            "first",
            ["python3", "-m", "unittest", repeated, stale, removed_module],
        )
        self.write_descriptor("second", ["python3", "-m", "unittest", repeated])
        with self.assertRaises(python_test_inventory.InventoryError) as captured:
            python_test_inventory.audit(self.repository, self.test_root)
        message = str(captured.exception)
        self.assertIn("missing registrations", message)
        self.assertIn("duplicate registrations", message)
        self.assertIn("stale registrations", message)
        self.assertIn(removed_module, message)

    def test_broad_module_and_class_selectors_are_rejected(self) -> None:
        self.write_source(
            "utils/tests/test_example.py",
            "from unittest import TestCase\n"
            "class ExampleTests(TestCase):\n"
            "    def test_case(self): pass\n",
        )
        exact = "utils.tests.test_example.ExampleTests.test_case"
        self.write_descriptor(
            "broad",
            [
                "python3",
                "-m",
                "unittest",
                "utils.tests.test_example",
                "utils.tests.test_example.ExampleTests",
                exact,
            ],
        )
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "broad unittest selectors are forbidden",
        ):
            python_test_inventory.audit(self.repository, self.test_root)
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "not exact discovered unittest method IDs",
        ):
            python_test_inventory.validate_exact_methods(
                self.repository,
                ["utils.tests.test_example.ExampleTests"],
                prefix="utils.tests.",
            )
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "duplicate unittest method IDs",
        ):
            python_test_inventory.validate_exact_methods(
                self.repository,
                [exact, exact],
                prefix="utils.tests.",
            )

    def test_unrelated_exec_arguments_cannot_spoof_registration(self) -> None:
        self.write_source(
            "utils/tests/test_example.py",
            "import unittest\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_case(self): pass\n",
        )
        exact = "utils.tests.test_example.ExampleTests.test_case"
        self.write_descriptor("spoof", ["python3", "unrelated.py", exact])
        self.write_descriptor("implicit", ["python3", "-m", "unittest"])
        self.write_descriptor(
            "wrong-runner",
            [
                "python3",
                "{repository}/utils/run_git_viewspace_fs_unittests.py",
                "{repository}",
                "{workspace}",
                "--",
                exact,
            ],
        )
        with self.assertRaisesRegex(
            python_test_inventory.InventoryError,
            "owned test ID appears outside a recognized runner",
        ) as captured:
            python_test_inventory.audit(self.repository, self.test_root)
        self.assertIn("unittest runner lacks exact method IDs", str(captured.exception))
        self.assertIn("viewfs runner can register only git-viewspace-fs methods", str(captured.exception))
