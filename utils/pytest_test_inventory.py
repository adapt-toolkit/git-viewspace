#!/usr/bin/env python3
"""Prove that owned pytest functions have exact harness registrations."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence


class InventoryError(Exception):
    """The pytest inventory is incomplete or ambiguous."""


SOURCE_DIRECTORY = "viewspace-intake/tests"
SOURCE_PATTERNS = ("test_*.py", "*_test.py")
OWNED_SELECTOR_PREFIX = f"{SOURCE_DIRECTORY}/"
PYTEST_CONFIGURATION = {
    "addopts": "-q",
    "testpaths": ["tests"],
}


def _bound_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for item in target.elts for name in _bound_names(item)}
    return set()


def _looks_collectible_binding(name: str) -> bool:
    return name.startswith("test") or name.startswith("Test")


class _ModuleMutationVisitor(ast.NodeVisitor):
    """Find collection-changing mutations evaluated in module scope."""

    def __init__(self) -> None:
        self.collectible_bindings: set[str] = set()
        self.collection_overrides = False

    def _record_target(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            if _looks_collectible_binding(target.id):
                self.collectible_bindings.add(target.id)
            if target.id == "__test__":
                self.collection_overrides = True
            if target.id == "pytest_plugins":
                self.collection_overrides = True
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._record_target(element)
        elif isinstance(target, ast.Attribute) and target.attr == "__test__":
            self.collection_overrides = True
        elif (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
        ):
            if target.slice.value == "__test__":
                self.collection_overrides = True
            if isinstance(target.slice.value, str) and _looks_collectible_binding(
                target.slice.value
            ):
                self.collectible_bindings.add(target.slice.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record_target(node.target)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record_target(target)

    def visit_For(self, node: ast.For) -> None:
        self._record_target(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._record_target(item.optional_vars)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name and _looks_collectible_binding(node.name):
            self.collectible_bindings.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name and _looks_collectible_binding(node.name):
            self.collectible_bindings.add(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in {
            "eval",
            "exec",
            "globals",
            "locals",
            "vars",
        }:
            self.collection_overrides = True
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "__test__"
        ):
            self.collection_overrides = True
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and _looks_collectible_binding(node.args[1].value)
        ):
            self.collectible_bindings.add(node.args[1].value)
        self.generic_visit(node)


def _nested_test_definitions(node: ast.AST) -> list[str]:
    return sorted(
        child.name
        for child in ast.walk(node)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and child is not node
        and _looks_collectible_binding(child.name)
    )


def _attribute_parts(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _module_scope_statements(statements: Iterable[ast.stmt]) -> Iterable[ast.stmt]:
    for node in statements:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(node, field, None)
            if isinstance(nested, list):
                yield from _module_scope_statements(nested)
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                yield from _module_scope_statements(handler.body)
        if isinstance(node, ast.Match):
            for case in node.cases:
                yield from _module_scope_statements(case.body)


def _validate_collection_policy(module: ast.Module, relative: Path) -> None:
    pytest_aliases = {"pytest"}
    pytest_mark_aliases: set[str] = set()
    for node in _module_scope_statements(module.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.partition(".")[0]
                if alias.name == "pytest":
                    pytest_aliases.add(binding)
                if binding == "pytest_plugins":
                    raise InventoryError(
                        f"cannot inventory {relative}: pytest plugin bindings are unsupported"
                    )
                if _looks_collectible_binding(binding):
                    raise InventoryError(
                        f"cannot inventory {relative}: imported test bindings are unsupported: "
                        f"{binding}"
                    )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    raise InventoryError(
                        f"cannot inventory {relative}: module-scope wildcard imports are "
                        "unsupported"
                    )
                binding = alias.asname or alias.name
                if binding == "pytest_plugins":
                    raise InventoryError(
                        f"cannot inventory {relative}: pytest plugin bindings are unsupported"
                    )
                if _looks_collectible_binding(binding):
                    raise InventoryError(
                        f"cannot inventory {relative}: imported test bindings are unsupported: "
                        f"{binding}"
                    )
                if node.module == "pytest" and alias.name in {
                    "exit",
                    "skip",
                    "skipif",
                    "xfail",
                    "importorskip",
                }:
                    raise InventoryError(
                        f"cannot inventory {relative}: pytest skip/xfail constructs are "
                        "unsupported"
                    )
                if node.module == "pytest" and alias.name == "mark":
                    pytest_mark_aliases.add(binding)

    forbidden = {"exit", "skip", "skipif", "xfail", "importorskip"}
    for node in ast.walk(module):
        if not isinstance(node, ast.Attribute):
            continue
        parts = _attribute_parts(node)
        if parts is None:
            continue
        is_forbidden = (
            parts[0] in pytest_aliases
            and (
                (len(parts) >= 2 and parts[1] in forbidden)
                or (
                    len(parts) >= 3
                    and parts[1] == "mark"
                    and parts[2] in forbidden
                )
            )
        ) or (
            parts[0] in pytest_mark_aliases
            and len(parts) >= 2
            and parts[1] in forbidden
        )
        if is_forbidden:
            raise InventoryError(
                f"cannot inventory {relative}: pytest skip/xfail constructs are unsupported"
            )

    mutations = _ModuleMutationVisitor()
    mutations.visit(module)
    if mutations.collectible_bindings:
        raise InventoryError(
            f"cannot inventory {relative}: dynamically assigned pytest tests are "
            f"unsupported: {', '.join(sorted(mutations.collectible_bindings))}"
        )
    if mutations.collection_overrides:
        raise InventoryError(
            f"cannot inventory {relative}: dynamic pytest collection overrides are "
            "unsupported"
        )


def _discover_file(repository: Path, path: Path) -> list[str]:
    relative = path.relative_to(repository)
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=os.fspath(relative))
    except (OSError, SyntaxError, UnicodeError) as error:
        raise InventoryError(f"cannot parse {relative}: {error}") from error
    _validate_collection_policy(module, relative)

    discovered: list[str] = []
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                if isinstance(node, ast.AsyncFunctionDef):
                    raise InventoryError(
                        f"cannot inventory {relative}: async pytest tests are unsupported: "
                        f"{node.name}"
                    )
                if any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)):
                    raise InventoryError(
                        f"cannot inventory {relative}: yield pytest tests are unsupported: "
                        f"{node.name}"
                    )
                discovered.append(f"{relative.as_posix()}::{node.name}")
            elif node.decorator_list:
                raise InventoryError(
                    f"cannot inventory {relative}: decorated non-test functions are "
                    f"unsupported: {node.name}"
                )
            continue
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("Test"):
                nested = _nested_test_definitions(node)
                if nested:
                    raise InventoryError(
                        f"cannot inventory {relative}: tests beneath a non-Test class are "
                        f"unsupported: {', '.join(nested)}"
                    )
                if node.decorator_list:
                    raise InventoryError(
                        f"cannot inventory {relative}: decorated non-test classes are "
                        f"unsupported: {node.name}"
                    )
                continue
            methods = [
                member
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name.startswith("test")
            ]
            if node.bases or node.keywords or node.decorator_list:
                raise InventoryError(
                    f"cannot inventory {relative}: inherited or decorated pytest class "
                    f"is unsupported: {node.name}"
                )
            class_mutations = _ModuleMutationVisitor()
            for member in node.body:
                class_mutations.visit(member)
            if class_mutations.collectible_bindings:
                raise InventoryError(
                    f"cannot inventory {relative}: dynamically assigned pytest class "
                    "tests are unsupported: "
                    + ", ".join(sorted(class_mutations.collectible_bindings))
                )
            if class_mutations.collection_overrides:
                raise InventoryError(
                    f"cannot inventory {relative}: dynamic pytest class collection "
                    f"overrides are unsupported: {node.name}"
                )
            for member in methods:
                if isinstance(member, ast.AsyncFunctionDef):
                    raise InventoryError(
                        f"cannot inventory {relative}: async pytest tests are unsupported: "
                        f"{node.name}::{member.name}"
                    )
                if any(
                    isinstance(child, (ast.Yield, ast.YieldFrom))
                    for child in ast.walk(member)
                ):
                    raise InventoryError(
                        f"cannot inventory {relative}: yield pytest tests are unsupported: "
                        f"{node.name}::{member.name}"
                    )
                discovered.append(
                    f"{relative.as_posix()}::{node.name}::{member.name}"
                )
            for member in node.body:
                if isinstance(member, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        member.targets
                        if isinstance(member, ast.Assign)
                        else [member.target]
                    )
                    names = {
                        name
                        for target in targets
                        for name in _bound_names(target)
                        if _looks_collectible_binding(name)
                    }
                    if names:
                        raise InventoryError(
                            f"cannot inventory {relative}: dynamically assigned pytest "
                            f"class tests are unsupported: {', '.join(sorted(names))}"
                        )
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested = _nested_test_definitions(member)
                    if nested:
                        raise InventoryError(
                            f"cannot inventory {relative}: conditional or nested pytest "
                            f"class tests are unsupported: {', '.join(nested)}"
                        )
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {
                name
                for target in targets
                for name in _bound_names(target)
                if _looks_collectible_binding(name)
            }
            if names:
                raise InventoryError(
                    f"cannot inventory {relative}: dynamically assigned pytest tests are "
                    f"unsupported: {', '.join(sorted(names))}"
                )
        nested = _nested_test_definitions(node)
        if nested:
            raise InventoryError(
                f"cannot inventory {relative}: conditional or nested pytest tests are "
                f"unsupported: {', '.join(nested)}"
            )
    return discovered


def _validate_pytest_configuration(repository: Path) -> None:
    configuration_path = repository / "viewspace-intake/pyproject.toml"
    package_root = repository / "viewspace-intake"
    configuration_names = {
        "pytest.ini",
        ".pytest.ini",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
    }
    shadowing = sorted(
        path.relative_to(repository)
        for path in package_root.rglob("*")
        if path.is_file()
        and path.name in configuration_names
        and path != configuration_path
    )
    if shadowing:
        raise InventoryError(
            "shadowing viewspace-intake pytest configuration is unsupported: "
            + ", ".join(map(os.fspath, shadowing))
        )
    try:
        configuration = tomllib.loads(configuration_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeError) as error:
        raise InventoryError(f"cannot read {configuration_path}: {error}") from error
    options = configuration.get("tool", {}).get("pytest", {}).get("ini_options", {})
    if options != PYTEST_CONFIGURATION:
        raise InventoryError(
            "unsupported viewspace-intake pytest configuration; collection policy must remain "
            f"{PYTEST_CONFIGURATION!r}"
        )
    conftests = sorted(
        path.relative_to(repository)
        for path in (repository / "viewspace-intake").rglob("conftest.py")
    )
    if conftests:
        raise InventoryError(
            "viewspace-intake conftest plugins are unsupported: "
            + ", ".join(map(os.fspath, conftests))
        )


def discover_functions(repository: Path) -> dict[str, Path]:
    """Return statically collectible top-level pytest function IDs."""

    _validate_pytest_configuration(repository)
    directory = repository / SOURCE_DIRECTORY
    if not directory.is_dir():
        raise InventoryError(f"owned pytest directory is missing: {SOURCE_DIRECTORY}")
    paths = sorted(
        {
            path
            for pattern in SOURCE_PATTERNS
            for path in directory.rglob(pattern)
        }
    )
    discovered: dict[str, Path] = {}
    for path in paths:
        for test_id in _discover_file(repository, path):
            if test_id in discovered:
                raise InventoryError(f"duplicate discovered pytest ID: {test_id}")
            discovered[test_id] = path.relative_to(repository)
    if not discovered:
        raise InventoryError("owned pytest inventory is empty")
    return discovered


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _looks_owned_selector(value: str, files: frozenset[str]) -> bool:
    if value == SOURCE_DIRECTORY:
        return True
    source = value.partition("::")[0]
    if source in files:
        return True
    if not source.startswith(OWNED_SELECTOR_PREFIX):
        return False
    name = Path(source).name
    if (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py"):
        return True
    prefix = source.rstrip("/") + "/"
    return any(path.startswith(prefix) for path in files)


def _is_broad_selector(
    value: str,
    files: frozenset[str],
    classes: frozenset[str],
) -> bool:
    if value in classes:
        return True
    if "::" in value:
        return False
    if value == SOURCE_DIRECTORY or value in files:
        return True
    prefix = value.rstrip("/") + "/"
    return any(path.startswith(prefix) for path in files)


def _pytest_selectors(arguments: object) -> tuple[list[str] | None, str | None]:
    if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
        return None, None
    if arguments[:3] != ["python3", "-m", "pytest"]:
        return None, None
    selectors: list[str] = []
    plugins: set[str] = set()
    index = 3
    while index < len(arguments):
        value = arguments[index]
        if value == "-q":
            index += 1
            continue
        if value == "-p" and index + 1 < len(arguments):
            plugin = arguments[index + 1]
            if plugin not in {"no:cacheprovider", "utils.pytest_no_skips"}:
                return None, f"unsupported pytest plugin option {plugin!r}"
            plugins.add(plugin)
            index += 2
            continue
        if value.startswith("-"):
            return None, f"unsupported pytest option {value!r}"
        selectors.append(value)
        index += 1
    if not selectors:
        return None, "pytest runner lacks exact function IDs"
    required_plugins = {"no:cacheprovider", "utils.pytest_no_skips"}
    if plugins != required_plugins:
        missing = ", ".join(sorted(required_plugins - plugins))
        return None, f"pytest runner lacks required plugins: {missing}"
    return selectors, None


def _registration_contract_errors(descriptor: object, path: Path) -> list[str]:
    if not isinstance(descriptor, dict):
        return ["descriptor is not an object"]
    errors: list[str] = []
    if descriptor.get("driver") != "exec" or not path.parent.name.endswith("=exec+sr"):
        errors.append("owned pytest registration must be an exec success case")
    prerequisites = descriptor.get("prerequisites")
    if not isinstance(prerequisites, list) or "python-module:pytest" not in prerequisites:
        errors.append("owned pytest registration lacks the pytest prerequisite")
    config = descriptor.get("config")
    if not isinstance(config, dict):
        return [*errors, "owned pytest registration lacks an object config"]
    compare = config.get("compare")
    if not isinstance(compare, list) or "status" not in compare:
        errors.append("owned pytest registration must compare process status")
    if config.get("cwd") != {"anchor": "repository", "path": "."}:
        errors.append("owned pytest registration must run from the repository root")
    environment = config.get("environment")
    if not isinstance(environment, dict):
        errors.append("owned pytest registration lacks an explicit environment")
    else:
        inherited = environment.get("inherit")
        assigned = environment.get("set")
        if inherited != ["PATH"]:
            errors.append("owned pytest registration must inherit only PATH")
        expected_assignments = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "viewspace-intake/src",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TMPDIR": "{workspace}",
        }
        if assigned != expected_assignments:
            errors.append(
                "owned pytest registration must use the exact isolated environment "
                f"{expected_assignments!r}"
            )
    status_path = path.parent / "golden/status"
    try:
        if status_path.read_text(encoding="utf-8") != "0\n":
            errors.append("owned pytest registration must expect status 0")
    except (OSError, UnicodeError) as error:
        errors.append(f"cannot read owned pytest expected status: {error}")
    return errors


def registered_functions(
    test_root: Path,
    discovered: dict[str, Path],
) -> tuple[
    dict[str, list[Path]],
    list[tuple[str, Path]],
    list[tuple[str, Path]],
]:
    """Read exact owned pytest selectors directly from harness descriptors."""

    files = frozenset(path.as_posix() for path in discovered.values())
    classes = frozenset(
        selector.rsplit("::", 1)[0]
        for selector in discovered
        if selector.count("::") == 2
    )
    registrations: dict[str, list[Path]] = defaultdict(list)
    broad: list[tuple[str, Path]] = []
    invalid: list[tuple[str, Path]] = []
    try:
        descriptors = sorted(test_root.glob("*/*/*/spec.json"))
    except OSError as error:
        raise InventoryError(f"cannot scan harness descriptors: {error}") from error
    if not descriptors:
        raise InventoryError(f"no harness descriptors found beneath {test_root}")

    for path in descriptors:
        try:
            descriptor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as error:
            raise InventoryError(f"cannot read {path}: {error}") from error
        relative = path.relative_to(test_root)
        arguments = descriptor.get("config", {}).get("argv", [])
        mentioned = [
            value for value in _strings(arguments) if _looks_owned_selector(value, files)
        ]
        selectors, shape_error = _pytest_selectors(arguments)
        if selectors is None:
            if mentioned:
                invalid.append(
                    (
                        shape_error or "owned pytest selector appears outside the exact runner",
                        relative,
                    )
                )
            continue
        owned = [value for value in selectors if _looks_owned_selector(value, files)]
        if not owned:
            continue
        if shape_error is not None:
            invalid.append((shape_error, relative))
            continue
        unrelated = [value for value in selectors if not _looks_owned_selector(value, files)]
        if unrelated:
            invalid.append(
                (
                    "owned pytest runner contains unrelated selectors: "
                    + ", ".join(unrelated),
                    relative,
                )
            )
        contract_errors = _registration_contract_errors(descriptor, path)
        invalid.extend((message, relative) for message in contract_errors)
        for value in owned:
            if _is_broad_selector(value, files, classes):
                broad.append((value, relative))
            elif value in discovered:
                registrations[value].append(relative)
            else:
                registrations[value].append(relative)
    return dict(registrations), broad, invalid


def audit(repository: Path, test_root: Path) -> int:
    discovered = discover_functions(repository)
    registrations, broad, invalid = registered_functions(test_root, discovered)
    missing = sorted(set(discovered) - set(registrations))
    stale = sorted(set(registrations) - set(discovered))
    duplicate = sorted(
        (test_id, paths) for test_id, paths in registrations.items() if len(paths) != 1
    )
    diagnostics = []
    if missing:
        diagnostics.append("missing registrations:\n  " + "\n  ".join(missing))
    if stale:
        diagnostics.append(
            "stale registrations:\n  "
            + "\n  ".join(
                f"{test_id}: {', '.join(map(os.fspath, registrations[test_id]))}"
                for test_id in stale
            )
        )
    if duplicate:
        diagnostics.append(
            "duplicate registrations:\n  "
            + "\n  ".join(
                f"{test_id}: {', '.join(map(os.fspath, paths))}"
                for test_id, paths in duplicate
            )
        )
    if broad:
        diagnostics.append(
            "broad pytest selectors are forbidden:\n  "
            + "\n  ".join(f"{value}: {path}" for value, path in sorted(broad))
        )
    if invalid:
        diagnostics.append(
            "invalid pytest registrations:\n  "
            + "\n  ".join(f"{message}: {path}" for message, path in sorted(invalid))
        )
    if diagnostics:
        raise InventoryError("\n".join(diagnostics))
    return len(discovered)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pytest-test-inventory")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--test-root", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = build_argument_parser().parse_args(arguments)
    try:
        count = audit(
            options.repository.resolve(strict=True),
            options.test_root.resolve(strict=True),
        )
    except (InventoryError, OSError) as error:
        print(f"pytest-test-inventory: {error}", file=sys.stderr)
        return 1
    print(f"registered {count} owned pytest functions exactly once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
