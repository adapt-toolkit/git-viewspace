#!/usr/bin/env python3
"""Prove that owned Python unittest methods have exact harness registrations."""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class InventoryError(Exception):
    """The Python unittest inventory is incomplete or ambiguous."""


@dataclass(frozen=True)
class SourceFamily:
    directory: str
    pattern: str
    module_prefix: str


SOURCE_FAMILIES = (
    SourceFamily("utils/tests", "*.py", "utils.tests"),
    SourceFamily("apps/git-viewspace-fs/git_viewspace_fs/tests", "*.py", "git_viewspace_fs.tests"),
    SourceFamily("apps/git-viewspace/tests", "*.py", "tests"),
    SourceFamily("apps/git-viewspace/OverlaySync/tests", "*.py", ""),
)
PYTHON_ID_PATTERN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}")
OWNED_ID_PREFIXES = (
    "utils.tests.test_",
    "git_viewspace_fs.tests.",
    "tests.test_",
    "test_overlay_sync.",
    "test_actions.",
    "test_publication.",
    "test_server.",
)


def _module_name(family: SourceFamily, directory: Path, path: Path) -> str:
    parts = path.relative_to(directory).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    relative_module = ".".join(parts)
    return (
        f"{family.module_prefix}.{relative_module}"
        if family.module_prefix and relative_module
        else family.module_prefix
        if family.module_prefix
        else relative_module
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


def _bound_names(node: ast.stmt) -> set[str]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets.extend(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets.append(node.target)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        targets.append(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets.extend(item.optional_vars for item in node.items if item.optional_vars is not None)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}

    names: set[str] = set()
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
    return names


_UNITTEST_MODULES = {
    ("unittest",),
    ("unittest", "case"),
    ("unittest", "async_case"),
}
_UNITTEST_BASES = {
    ("unittest", "TestCase"),
    ("unittest", "case", "TestCase"),
    ("unittest", "IsolatedAsyncioTestCase"),
    ("unittest", "async_case", "IsolatedAsyncioTestCase"),
}
_UNITTEST_SYMBOLS = _UNITTEST_MODULES | _UNITTEST_BASES


def _supported_import_bindings(node: ast.stmt) -> dict[str, tuple[str, ...]]:
    bindings: dict[str, tuple[str, ...]] = {}
    if isinstance(node, ast.Import):
        for alias in node.names:
            canonical = tuple(alias.name.split("."))
            if not canonical or canonical[0] != "unittest":
                continue
            if alias.asname:
                if canonical in _UNITTEST_SYMBOLS:
                    bindings[alias.asname] = canonical
            else:
                bindings["unittest"] = ("unittest",)
    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        module = tuple(node.module.split("."))
        if module not in _UNITTEST_MODULES:
            return bindings
        for alias in node.names:
            if alias.name == "*":
                if module == ("unittest",):
                    bindings.update(
                        {
                            "TestCase": ("unittest", "TestCase"),
                            "IsolatedAsyncioTestCase": (
                                "unittest",
                                "IsolatedAsyncioTestCase",
                            ),
                            "case": ("unittest", "case"),
                            "async_case": ("unittest", "async_case"),
                        }
                    )
                continue
            canonical = module + (alias.name,)
            if canonical in _UNITTEST_SYMBOLS:
                bindings[alias.asname or alias.name] = canonical
    return bindings


def _resolved_symbol(
    expression: ast.expr,
    aliases: dict[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    parts = _attribute_parts(expression)
    if parts is None or parts[0] not in aliases:
        return None
    return aliases[parts[0]] + parts[1:]


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_target_names(element))
        return names
    return set()


class _ModuleBindingVisitor(ast.NodeVisitor):
    """Collect bindings evaluated in module scope without entering local scopes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, ast.AST]] = []
        self.imports: list[ast.stmt] = []
        self.classes: list[ast.ClassDef] = []
        self.attribute_mutations: list[tuple[str, str, ast.AST]] = []
        self.setattr_targets: list[tuple[str, ast.Call]] = []

    def _record_target(self, target: ast.expr, origin: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.events.append((target.id, origin))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._record_target(element, origin)
        elif isinstance(target, ast.Attribute):
            parts = _attribute_parts(target)
            if parts is not None:
                self.attribute_mutations.append((parts[0], target.attr, origin))

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append(node)
        for alias in node.names:
            self.events.append((alias.asname or alias.name.partition(".")[0], node))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append(node)
        for alias in node.names:
            if alias.name != "*":
                self.events.append((alias.asname or alias.name, node))

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_target(target, node)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target, node)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target, node)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record_target(node.target, node)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record_target(target, node)

    def visit_For(self, node: ast.For) -> None:
        self._record_target(node.target, node)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._record_target(item.optional_vars, node)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.events.append((node.name, node))
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.events.append((node.name, node))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.events.append((node.name, node))
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.events.append((node.name, node))
        self.classes.append(node)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for decorator in node.decorator_list:
            self.visit(decorator)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            self.setattr_targets.append((node.args[0].id, node))
        self.generic_visit(node)


def _test_case_classes(module: ast.Module) -> dict[str, frozenset[str]]:
    aliases: dict[str, tuple[str, ...]] = {}
    allowed_bindings: set[tuple[int, str]] = set()
    direct_imports: set[int] = set()
    for node in module.body:
        bindings = _supported_import_bindings(node)
        if not bindings:
            continue
        direct_imports.add(id(node))
        for name, canonical in bindings.items():
            if name in aliases and aliases[name] != canonical:
                raise InventoryError(f"ambiguous unittest import alias: {name}")
            aliases[name] = canonical
            allowed_bindings.add((id(node), name))

    while True:
        changed = False
        for node in module.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            else:
                continue
            canonical = _resolved_symbol(value, aliases)
            if canonical not in _UNITTEST_SYMBOLS:
                continue
            names: set[str] = set()
            for target in targets:
                names.update(_target_names(target))
            if not names:
                continue
            for name in names:
                if name in aliases and aliases[name] != canonical:
                    raise InventoryError(f"ambiguous unittest alias assignment: {name}")
                if name not in aliases:
                    aliases[name] = canonical
                    changed = True
                allowed_bindings.add((id(node), name))
        if not changed:
            break

    visitor = _ModuleBindingVisitor()
    visitor.visit(module)
    if any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        for node in visitor.imports
    ):
        raise InventoryError("module-scope wildcard imports are unsupported")
    for node in visitor.imports:
        if _supported_import_bindings(node) and id(node) not in direct_imports:
            raise InventoryError("conditional unittest imports are unsupported")
    rebound = sorted(
        {
            name
            for name, node in visitor.events
            if name in aliases and (id(node), name) not in allowed_bindings
        }
    )
    if rebound:
        raise InventoryError("unittest aliases are rebound: " + ", ".join(rebound))
    global_rebindings = sorted(
        {
            name
            for node in ast.walk(module)
            if isinstance(node, ast.Global)
            for name in node.names
            if name in aliases
        }
    )
    if global_rebindings:
        raise InventoryError(
            "unittest aliases have global rebinding declarations: "
            + ", ".join(global_rebindings)
        )

    classes = {node.name: node for node in module.body if isinstance(node, ast.ClassDef)}
    conditional_classes = [node.name for node in visitor.classes if node not in classes.values()]
    if conditional_classes:
        raise InventoryError(
            "conditional module-scope classes are unsupported: "
            + ", ".join(sorted(conditional_classes))
        )

    def base_reference(base: ast.expr) -> str | None:
        canonical = _resolved_symbol(base, aliases)
        if canonical in _UNITTEST_BASES:
            return ".".join(canonical)
        if isinstance(base, ast.Name) and base.id in classes:
            return base.id
        if (
            isinstance(base, ast.Name)
            and isinstance(getattr(builtins, base.id, None), type)
        ):
            return "object"
        return None

    def is_proven_local_non_test_base(base: ast.expr) -> bool:
        parts = _attribute_parts(base)
        if parts is None or len(parts) != 2 or parts[0] not in classes:
            return False
        namespace = classes[parts[0]]
        nested = next(
            (
                member
                for member in namespace.body
                if isinstance(member, ast.ClassDef) and member.name == parts[1]
            ),
            None,
        )
        if nested is None:
            return False
        return not nested.bases and not any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test")
            for member in nested.body
        )

    test_cases = {
        name
        for name, node in classes.items()
        if any(_resolved_symbol(base, aliases) in _UNITTEST_BASES for base in node.bases)
    }
    while True:
        inherited = {
            name
            for name, node in classes.items()
            if any(
                isinstance(base, ast.Name) and base.id in test_cases
                for base in node.bases
            )
        }
        expanded = test_cases | inherited
        if expanded == test_cases:
            break
        test_cases = expanded

    def validate_ancestry(class_name: str, seen: frozenset[str] = frozenset()) -> None:
        if class_name in seen:
            raise InventoryError(f"cyclic local class inheritance: {class_name}")
        for base in classes[class_name].bases:
            reference = base_reference(base)
            if reference is None:
                raise InventoryError(
                    f"unresolved base in TestCase ancestry: {class_name}"
                )
            if reference in classes:
                validate_ancestry(reference, seen | {class_name})

    for class_name in test_cases:
        validate_ancestry(class_name)

    own_attributes: dict[str, dict[str, bool]] = {}
    for name, node in classes.items():
        attributes: dict[str, bool] = {}
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if member.name == "runTest":
                    raise InventoryError(f"runTest-style unittest is unsupported: {name}")
                if member.name.startswith("test"):
                    is_property = any(
                        isinstance(decorator, ast.Name) and decorator.id == "property"
                        for decorator in member.decorator_list
                    )
                    attributes[member.name] = not is_property
            else:
                assigned_tests = {
                    bound for bound in _bound_names(member) if bound.startswith("test")
                }
                nested_tests = {
                    child.name
                    for child in ast.walk(member)
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name.startswith("test")
                }
                assigned_tests.update(nested_tests)
                if assigned_tests:
                    raise InventoryError(
                        f"dynamically assigned class test methods are unsupported: {name}"
                    )
        own_attributes[name] = attributes

    for name, node in classes.items():
        unresolved = [
            base
            for base in node.bases
            if base_reference(base) is None and not is_proven_local_non_test_base(base)
        ]
        if unresolved:
            raise InventoryError(f"unresolved top-level class base: {name}")

    relevant_classes: set[str] = set(test_cases)

    def add_local_ancestry(class_name: str) -> None:
        for base in classes[class_name].bases:
            reference = base_reference(base)
            if reference in classes and reference not in relevant_classes:
                relevant_classes.add(reference)
                add_local_ancestry(reference)

    for class_name in tuple(test_cases):
        add_local_ancestry(class_name)
    for class_name in relevant_classes:
        node = classes[class_name]
        if node.decorator_list or any(keyword.arg == "metaclass" for keyword in node.keywords):
            raise InventoryError(
                f"decorated or metaclass-driven TestCase ancestry is unsupported: {class_name}"
            )
    for root, attribute, _ in visitor.attribute_mutations:
        if root in relevant_classes and attribute.startswith("test"):
            raise InventoryError("module-scope test method mutation is unsupported")
    for root, _ in visitor.setattr_targets:
        if root in relevant_classes:
            raise InventoryError("module-scope TestCase setattr/delattr is unsupported")
    for name, node in visitor.events:
        if name in relevant_classes and not (
            isinstance(node, ast.ClassDef) and node is classes[name]
        ):
            raise InventoryError(f"TestCase ancestry class is rebound or aliased: {name}")
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_tests":
            raise InventoryError("load_tests protocol is unsupported")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "type"
            ):
                raise InventoryError("dynamic module-scope class construction is unsupported")

    mro_cache: dict[str, tuple[str, ...]] = {
        "object": ("object",),
        "unittest.TestCase": ("unittest.TestCase", "object"),
        "unittest.case.TestCase": ("unittest.case.TestCase", "object"),
        "unittest.IsolatedAsyncioTestCase": (
            "unittest.IsolatedAsyncioTestCase",
            "unittest.TestCase",
            "object",
        ),
        "unittest.async_case.IsolatedAsyncioTestCase": (
            "unittest.async_case.IsolatedAsyncioTestCase",
            "unittest.TestCase",
            "object",
        ),
    }

    def class_mro(class_name: str) -> tuple[str, ...]:
        if class_name in mro_cache:
            return mro_cache[class_name]
        references = [base_reference(base) for base in classes[class_name].bases]
        bases = [reference for reference in references if reference is not None]
        if not bases:
            bases = ["object"]
        sequences = [list(class_mro(base)) for base in bases] + [list(bases)]
        result = [class_name]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if not any(sequence[0] in other[1:] for other in sequences)
                ),
                None,
            )
            if candidate is None:
                raise InventoryError(f"cannot resolve local class MRO: {class_name}")
            result.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        mro_cache[class_name] = tuple(result)
        return mro_cache[class_name]

    discovered: dict[str, frozenset[str]] = {}
    for class_name in test_cases:
        order = class_mro(class_name)
        candidates = {
            method
            for ancestor in order
            for method in own_attributes.get(ancestor, {})
        }
        methods = set()
        for method in candidates:
            for ancestor in order:
                if method not in own_attributes.get(ancestor, {}):
                    continue
                if own_attributes[ancestor][method]:
                    methods.add(method)
                break
        discovered[class_name] = frozenset(methods)
    return discovered


def discover_methods(repository: Path) -> dict[str, Path]:
    """Return methods defined by concrete top-level ``TestCase`` subclasses."""

    discovered: dict[str, Path] = {}
    source_modules: dict[str, Path] = {}
    for family in SOURCE_FAMILIES:
        directory = repository / family.directory
        if not directory.is_dir():
            raise InventoryError(f"owned Python test directory is missing: {family.directory}")
        for path in sorted(directory.rglob(family.pattern)):
            try:
                source = path.read_text(encoding="utf-8")
                module = ast.parse(source, filename=os.fspath(path))
            except (OSError, SyntaxError, UnicodeError) as error:
                raise InventoryError(f"cannot parse {path.relative_to(repository)}: {error}") from error
            module_name = _module_name(family, directory, path)
            relative_path = path.relative_to(repository)
            previous = source_modules.get(module_name)
            if previous is not None and previous != relative_path:
                raise InventoryError(
                    f"owned Python module name collision {module_name!r}: "
                    f"{previous}, {relative_path}"
                )
            for existing_module, existing_path in source_modules.items():
                if (
                    existing_module
                    and module_name.startswith(existing_module + ".")
                    and existing_path.name != "__init__.py"
                ) or (
                    module_name
                    and existing_module.startswith(module_name + ".")
                    and relative_path.name != "__init__.py"
                ):
                    raise InventoryError(
                        "owned Python module file shadows a package prefix: "
                        f"{existing_path}, {relative_path}"
                    )
            source_modules[module_name] = relative_path
            try:
                test_case_classes = _test_case_classes(module)
            except InventoryError as error:
                raise InventoryError(
                    f"cannot inventory {path.relative_to(repository)}: {error}"
                ) from error
            module_parts = path.relative_to(directory).with_suffix("").parts
            if test_case_classes and not module_name:
                raise InventoryError(
                    f"test-bearing module has no importable name: {path.relative_to(repository)}"
                )
            if test_case_classes and any(
                part != "__init__" and not part.isidentifier() for part in module_parts
            ):
                raise InventoryError(
                    f"test-bearing module path is not importable: {path.relative_to(repository)}"
                )
            for node in module.body:
                if not isinstance(node, ast.ClassDef) or node.name not in test_case_classes:
                    continue
                for method_name in sorted(test_case_classes[node.name]):
                    test_id = f"{module_name}.{node.name}.{method_name}"
                    if test_id in discovered:
                        raise InventoryError(f"duplicate discovered unittest ID: {test_id}")
                    discovered[test_id] = path.relative_to(repository)
    if not discovered:
        raise InventoryError("owned Python unittest inventory is empty")
    return discovered


def validate_exact_methods(
    repository: Path,
    test_ids: Sequence[str],
    *,
    prefix: str,
) -> None:
    """Require a nonempty, duplicate-free list of exact discovered method IDs."""

    discovered = discover_methods(repository)
    duplicates = sorted(
        test_id for test_id in set(test_ids) if test_ids.count(test_id) > 1
    )
    invalid = sorted(
        test_id
        for test_id in set(test_ids)
        if not test_id.startswith(prefix) or test_id not in discovered
    )
    diagnostics = []
    if not test_ids:
        diagnostics.append("at least one exact unittest method ID is required")
    if duplicates:
        diagnostics.append("duplicate unittest method IDs: " + ", ".join(duplicates))
    if invalid:
        diagnostics.append("not exact discovered unittest method IDs: " + ", ".join(invalid))
    if diagnostics:
        raise InventoryError("; ".join(diagnostics))


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _looks_owned(value: str, modules: frozenset[str]) -> bool:
    return (
        PYTHON_ID_PATTERN.fullmatch(value) is not None
        and (
            any(value.startswith(module + ".") for module in modules)
            or value.startswith(OWNED_ID_PREFIXES)
        )
    )


def _registered_argument_tail(
    arguments: object,
) -> tuple[str | None, list[str] | None, str | None]:
    if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
        return None, None, None
    if arguments[:3] == ["python3", "-m", "unittest"]:
        if len(arguments) == 3:
            return None, None, "unittest runner lacks exact method IDs"
        return "ordinary", arguments[3:], None
    if (
        len(arguments) >= 5
        and arguments[0] == "python3"
        and arguments[1] == "{repository}/utils/run_git_viewspace_fs_unittests.py"
        and arguments[2:4] == ["{repository}", "{workspace}"]
    ):
        tail = arguments[4:]
        if tail and tail[0] == "--fixture":
            tail = tail[1:]
        if tail and tail[0] == "--":
            if len(tail) == 1:
                return None, None, "viewfs unittest runner lacks exact method IDs"
            return "viewfs", tail[1:], None
        return None, None, "viewfs unittest runner lacks its exact -- delimiter"
    return None, None, None


def registered_methods(
    test_root: Path,
    discovered: dict[str, Path],
) -> tuple[
    dict[str, list[Path]],
    list[tuple[str, Path]],
    list[tuple[str, Path]],
]:
    """Read registrations directly from descriptor strings.

    Exact dotted unittest IDs are the sole registration source. Broad module or class selectors are
    returned separately so discovery-style catch-all cases cannot masquerade as complete coverage.
    """

    modules = frozenset(test_id.rsplit(".", 2)[0] for test_id in discovered)
    classes = frozenset(test_id.rsplit(".", 1)[0] for test_id in discovered)
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
        runner, tail, shape_error = _registered_argument_tail(arguments)
        mentioned = [
            value
            for value in _strings(arguments)
            if value in modules or value in classes or _looks_owned(value, modules)
        ]
        if shape_error is not None:
            invalid.append((shape_error, relative))
            continue
        if tail is None:
            if mentioned:
                invalid.append(("owned test ID appears outside a recognized runner", relative))
            continue
        for value in tail:
            if runner == "viewfs" and not value.startswith("git_viewspace_fs.tests."):
                invalid.append(("viewfs runner can register only git-viewspace-fs methods", relative))
                continue
            if runner == "ordinary" and value.startswith("git_viewspace_fs.tests."):
                invalid.append(("git-viewspace-fs methods require the isolated viewfs runner", relative))
                continue
            if value in modules or value in classes:
                broad.append((value, relative))
            elif value in discovered or _looks_owned(value, modules):
                registrations[value].append(relative)
            else:
                invalid.append((f"non-exact unittest argument {value!r}", relative))
    return dict(registrations), broad, invalid


def audit(repository: Path, test_root: Path) -> int:
    discovered = discover_methods(repository)
    registrations, broad, invalid = registered_methods(test_root, discovered)
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
            "broad unittest selectors are forbidden:\n  "
            + "\n  ".join(f"{value}: {path}" for value, path in sorted(broad))
        )
    if invalid:
        diagnostics.append(
            "invalid unittest registrations:\n  "
            + "\n  ".join(f"{message}: {path}" for message, path in sorted(invalid))
        )
    if diagnostics:
        raise InventoryError("\n".join(diagnostics))
    return len(discovered)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python-test-inventory")
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
        print(f"python-test-inventory: {error}", file=sys.stderr)
        return 1
    print(f"registered {count} owned Python unittest methods exactly once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
