"""Structural contracts for the classified architecture.

Every assertion here runs the ACTUAL quality resolver against the ACTUAL
selected configuration and the ACTUAL production source. Disposable fixtures
are used only where a contract is about source that must NOT exist yet - a
future desktop file, a deliberately ambiguous layer, a mutated import - so no
product module is ever imported or executed to learn what it depends on.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import quality_gate  # noqa: E402

CONFIG = quality_gate.load_config(ROOT)
PYTHON_LAYERS: list[dict[str, Any]] = list(CONFIG["python_layers"])
CRITICAL_GLOBS = [str(pattern) for pattern in CONFIG.get("critical_python_globs", [])]

NEW_MODULES = (
    "workstack/checkpoint_transition.py",
    "workstack/checkpoint_change.py",
    "workstack/checkpoint_projection.py",
    "workstack/context_projection.py",
    "workstack/sse_events.py",
    "workstack/cli_writer.py",
    "workstack/checkpoint_state_cli.py",
)

EXPECTED_LAYER = {
    "workstack/checkpoint_transition.py": "py_checkpoint_contract",
    "workstack/checkpoint_change.py": "py_checkpoint_facts",
    "workstack/checkpoint_projection.py": "py_checkpoint_projection",
    "workstack/context_projection.py": "py_context_projection",
    "workstack/sse_events.py": "py_sse_encoder",
    "workstack/cli_writer.py": "py_cli_writer",
    "workstack/checkpoint_state_cli.py": "py_checkpoint_cli",
    "desktop/python-webview-shell/local_workspace_rebind.py": "py_desktop_rebind",
}

DESKTOP_DIRECTORY = ROOT / "desktop" / "python-webview-shell"


def layer_of(path: str) -> str | None:
    """The single layer that claims a path, or None when it is not exactly one."""

    layer, _errors = quality_gate._layer_for(path, PYTHON_LAYERS)
    return layer


def permissions(layer: str) -> set[str]:
    for rule in PYTHON_LAYERS:
        if str(rule["name"]) == layer:
            return {str(name) for name in rule.get("may_import", [])}
    raise AssertionError(f"no such layer: {layer}")


def edge_allowed(source: str, target: str) -> bool:
    """Whether the configuration lets one production path import another."""

    source_layer = layer_of(source)
    target_layer = layer_of(target)
    assert source_layer and target_layer, (source, source_layer, target, target_layer)
    if source_layer == target_layer:
        return True
    return target_layer in permissions(source_layer)


def from_import_names(path: str, module_name: str) -> set[str]:
    """The exact names one real file imports from one module.

    A relative import inside the same package names the same module, so it is
    resolved against the measured filename rather than matched textually.
    """

    module = quality_gate._python_module(path)
    assert module, path
    package = quality_gate._package_of(path, module)
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            resolved = quality_gate._resolve_python_import(
                module, node, {module_name}, package=package
            )
            if module_name in resolved:
                names.update(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name == module_name)
    return names


class Classification(unittest.TestCase):
    """Every production Python file must be claimed by exactly one layer."""

    def test_the_seven_new_modules_take_their_exact_layers(self) -> None:
        for path in NEW_MODULES:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file(), f"missing source: {path}")
                self.assertEqual(layer_of(path), EXPECTED_LAYER[path])

    def test_the_seven_new_modules_are_critical(self) -> None:
        for path in NEW_MODULES:
            with self.subTest(path=path):
                self.assertTrue(quality_gate._matches(path, CRITICAL_GLOBS))

    def test_every_desktop_source_classifies_exactly_once(self) -> None:
        desktop = sorted(
            quality_gate._path(item, ROOT)
            for item in DESKTOP_DIRECTORY.rglob("*.py")
            if item.is_file()
        )
        self.assertEqual(len(desktop), 21, desktop)
        for path in desktop:
            with self.subTest(path=path):
                layer, errors = quality_gate._layer_for(path, PYTHON_LAYERS)
                self.assertIsNotNone(layer, errors)
        self.assertEqual(
            layer_of("desktop/python-webview-shell/local_workspace_rebind.py"),
            "py_desktop_rebind",
        )
        others = {path for path in desktop} - {
            "desktop/python-webview-shell/local_workspace_rebind.py"
        }
        for path in sorted(others):
            with self.subTest(path=path):
                self.assertEqual(layer_of(path), "py_desktop")

    def test_a_future_desktop_file_is_refused_as_unclassified(self) -> None:
        # Enumerated globs, not a wildcard bucket: a new file must be declared.
        future = "desktop/python-webview-shell/not_yet_declared.py"
        graph, layers, errors = parsed_python_fixture({future: "VALUE = 1\n"})
        self.assertEqual(graph, {future: set()})
        self.assertNotIn(future, layers)
        self.assertEqual(errors, [future])

    def test_two_layers_claiming_one_path_is_an_ambiguity_failure(self) -> None:
        # No first-match rule: an overlap must fail rather than pick a winner.
        duplicated = [
            {"name": "py_probe_a", "globs": ["workstack/sse_events.py"]},
            {"name": "py_probe_b", "globs": ["workstack/sse_events.py"]},
        ]
        path = "workstack/sse_events.py"
        graph, layers, errors = parsed_python_fixture({path: "VALUE = 1\n"}, duplicated)
        self.assertEqual(graph, {path: set()})
        self.assertNotIn(path, layers)
        self.assertEqual(errors, [f"{path} (multiple layers: py_probe_a, py_probe_b)"])


class PermittedEdges(unittest.TestCase):
    """The consumer edges the selected configuration deliberately allows."""

    def test_cli_package_may_reach_the_checkpoint_cli_and_writer(self) -> None:
        for target in ("workstack/checkpoint_state_cli.py", "workstack/cli_writer.py"):
            with self.subTest(target=target):
                self.assertTrue(edge_allowed("workstack/cli.py", target))

    def test_checkpoint_cli_may_reach_its_pure_contract_and_the_writer(self) -> None:
        source = "workstack/checkpoint_state_cli.py"
        self.assertTrue(edge_allowed(source, "workstack/checkpoint_transition.py"))
        self.assertTrue(edge_allowed(source, "workstack/cli_writer.py"))

    def test_service_may_reach_the_four_pure_owners(self) -> None:
        for target in (
            "workstack/checkpoint_change.py",
            "workstack/checkpoint_projection.py",
            "workstack/checkpoint_transition.py",
            "workstack/context_projection.py",
        ):
            with self.subTest(target=target):
                self.assertTrue(edge_allowed("workstack/service.py", target))

    def test_the_server_may_reach_the_sse_encoder(self) -> None:
        self.assertTrue(edge_allowed("workstack/server.py", "workstack/sse_events.py"))

    def test_projection_may_reach_the_pure_contract_and_the_serializer(self) -> None:
        source = "workstack/checkpoint_projection.py"
        self.assertTrue(edge_allowed(source, "workstack/checkpoint_transition.py"))
        self.assertEqual(permissions("py_checkpoint_projection"), {
            "py_checkpoint_contract", "py_storage",
        })

    def test_facts_may_reach_storage_and_context_may_reach_capture(self) -> None:
        self.assertEqual(permissions("py_checkpoint_facts"), {"py_storage"})
        self.assertTrue(
            edge_allowed("workstack/context_projection.py", "workstack/capture.py")
        )

    def test_the_writer_may_reach_the_store_and_the_host_the_rebind(self) -> None:
        self.assertEqual(permissions("py_cli_writer"), {"py_legacy_store"})
        self.assertTrue(edge_allowed(
            "desktop/python-webview-shell/workstack_desktop.py",
            "desktop/python-webview-shell/local_workspace_rebind.py",
        ))
        self.assertEqual(permissions("py_desktop_rebind"), {"py_legacy_store"})

    def test_the_pure_contract_and_encoder_may_import_nothing(self) -> None:
        self.assertEqual(permissions("py_checkpoint_contract"), set())
        self.assertEqual(permissions("py_sse_encoder"), set())


class ForbiddenEdges(unittest.TestCase):
    """The inverses that must stay impossible under the same configuration."""

    def test_the_store_and_foundation_cannot_reach_application_or_cli(self) -> None:
        for source in (
            "workstack/store.py", "workstack/storage/canonical.py", "workstack/capture.py",
        ):
            for target in (
                "workstack/service.py",
                "workstack/cli.py",
                "workstack/checkpoint_state_cli.py",
            ):
                with self.subTest(source=source, target=target):
                    self.assertTrue((ROOT / source).is_file(), source)
                    self.assertFalse(edge_allowed(source, target))

    def test_pure_transition_and_sse_cannot_reach_the_store_service_or_cli(self) -> None:
        for source in ("workstack/checkpoint_transition.py", "workstack/sse_events.py"):
            for target in (
                "workstack/store.py",
                "workstack/service.py",
                "workstack/cli.py",
            ):
                with self.subTest(source=source, target=target):
                    self.assertFalse(edge_allowed(source, target))

    def test_facts_projection_and_context_cannot_reach_the_server_or_cli(self) -> None:
        for source in (
            "workstack/checkpoint_change.py",
            "workstack/checkpoint_projection.py",
            "workstack/context_projection.py",
        ):
            for target in ("workstack/server.py", "workstack/cli.py"):
                with self.subTest(source=source, target=target):
                    self.assertFalse(edge_allowed(source, target))

    def test_the_writer_cannot_reach_the_service_server_or_checkpoint_cli(self) -> None:
        for target in (
            "workstack/service.py",
            "workstack/server.py",
            "workstack/checkpoint_state_cli.py",
        ):
            with self.subTest(target=target):
                self.assertFalse(edge_allowed("workstack/cli_writer.py", target))

    def test_the_rebind_cannot_reach_the_service_server_cli_or_ordinary_host(self) -> None:
        source = "desktop/python-webview-shell/local_workspace_rebind.py"
        for target in (
            "workstack/service.py",
            "workstack/server.py",
            "workstack/cli.py",
            "desktop/python-webview-shell/workstack_desktop.py",
        ):
            with self.subTest(target=target):
                self.assertFalse(edge_allowed(source, target))

    def test_ordinary_desktop_modules_cannot_reach_the_store(self) -> None:
        # Only the rebind adapter earned that permission.
        self.assertNotIn("py_legacy_store", permissions("py_desktop"))
        self.assertFalse(edge_allowed(
            "desktop/python-webview-shell/ssot_connection.py", "workstack/store.py",
        ))


class PackageAliasResolution(unittest.TestCase):
    """Mixed symbol and submodule aliases must use the admitted fallback."""

    def setUp(self) -> None:
        self.modules = {
            "workstack",
            "workstack.cli",
            "workstack.cli_writer",
            "workstack.checkpoint_state_cli",
            "workstack.checkpoint_transition",
        }

    def resolve(self, source: str, path: str) -> set[str]:
        node = ast.parse(source).body[0]
        module = quality_gate._python_module(path)
        assert module
        return quality_gate._resolve_python_import(
            module, node, self.modules, package=quality_gate._package_of(path, module)
        )

    def test_a_submodule_alias_resolves_to_that_submodule(self) -> None:
        resolved = self.resolve(
            "from workstack import checkpoint_state_cli", "workstack/cli.py"
        )
        self.assertEqual(resolved, {"workstack.checkpoint_state_cli"})

    def test_a_mixed_symbol_and_submodule_import_keeps_both_meanings(self) -> None:
        resolved = self.resolve(
            "from workstack import cli_writer, SOME_CONSTANT", "workstack/cli.py"
        )
        # The submodule is an edge; the plain symbol falls back to the package.
        self.assertIn("workstack.cli_writer", resolved)
        self.assertIn("workstack", resolved)

    def test_a_package_initializer_resolves_its_relative_import(self) -> None:
        resolved = self.resolve(
            "from . import cli_writer", "workstack/__init__.py"
        )
        self.assertEqual(resolved, {"workstack.cli_writer"})


class ActualSourceImports(unittest.TestCase):
    """Parse the real files: the narrow permissions must be narrowly used."""

    WRITER = "workstack/cli_writer.py"
    REBIND = "desktop/python-webview-shell/local_workspace_rebind.py"
    VALIDATORS = {
        "StoreCorruptError",
        "_validate_store_manifest_files",
        "_validate_store_manifest_header",
        "_validate_store_manifest_tasks",
        "_validated_rebind_file_records",
    }

    def test_the_writer_imports_only_MAX_REVISION_from_the_store(self) -> None:
        self.assertTrue((ROOT / self.WRITER).is_file())
        names = from_import_names(self.WRITER, "workstack.store")
        self.assertEqual(names, {"MAX_REVISION"}, names)
        # The permission is for a constant, never for constructing a Store.
        source = (ROOT / self.WRITER).read_text(encoding="utf-8")
        self.assertNotIn("Store(", source)

    def test_a_writer_mutant_that_widens_the_store_import_fails(self) -> None:
        mutant = "from workstack.store import MAX_REVISION, Store\n"
        names = self.names_in(mutant, "workstack.store")
        self.assertNotEqual(names, {"MAX_REVISION"})
        broad = "import workstack.store\n"
        self.assertEqual(self.names_in(broad, "workstack.store"), {"workstack.store"})

    def test_the_rebind_pure_validators_are_exactly_the_five_read_only_names(self) -> None:
        self.assertTrue((ROOT / self.REBIND).is_file())
        tree = ast.parse((ROOT / self.REBIND).read_text(encoding="utf-8"), filename=self.REBIND)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_pure_store_validators":
                target = node
        self.assertIsNotNone(target, "_pure_store_validators is missing")
        imported: set[str] = set()
        for node in ast.walk(target):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
            self.assertNotIsInstance(node, ast.Import)
        self.assertEqual(imported, self.VALIDATORS, imported)

    def test_the_returned_validator_namespace_maps_the_expected_functions(self) -> None:
        source = (ROOT / self.REBIND).read_text(encoding="utf-8")
        for name in sorted(self.VALIDATORS - {"StoreCorruptError"}):
            with self.subTest(name=name):
                self.assertIn(name, source)
        self.assertNotIn("Store(", source)

    def names_in(self, source: str, module_name: str) -> set[str]:
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == module_name:
                names.update(alias.name for alias in node.names)
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names


class CriticalComplexity(unittest.TestCase):
    """A newly critical source is measured by the unchanged helper."""

    def ccn_of(self, source: str) -> int:
        tree = ast.parse(source)
        measured = dict(quality_gate._function_symbols(tree))
        self.assertEqual(len(measured), 1, sorted(measured))
        (node,) = measured.values()
        return quality_gate._complexity(node)

    def test_a_sixteen_branch_function_exceeds_the_threshold(self) -> None:
        body = "\n".join(f"    if value == {index}:\n        return {index}" for index in range(15))
        source = f"def probe(value):\n{body}\n    return -1\n"
        self.assertEqual(self.ccn_of(source), 16)
        self.assertGreater(self.ccn_of(source), 15)

    def test_a_fifteen_branch_control_meets_the_threshold(self) -> None:
        body = "\n".join(f"    if value == {index}:\n        return {index}" for index in range(14))
        source = f"def probe(value):\n{body}\n    return -1\n"
        self.assertEqual(self.ccn_of(source), 15)
        self.assertLessEqual(self.ccn_of(source), 15)

    def test_every_newly_critical_module_symbol_is_within_the_threshold(self) -> None:
        for path in NEW_MODULES:
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
            for name, node in quality_gate._function_symbols(tree):
                with self.subTest(path=path, name=name):
                    self.assertLessEqual(quality_gate._complexity(node), 15)


class FrontendProhibitions(unittest.TestCase):
    """The frontend layer permissions must be preserved exactly."""

    def setUp(self) -> None:
        self.layers = list(CONFIG["frontend_layers"])

    def permission(self, layer: str) -> set[str]:
        for rule in self.layers:
            if str(rule["name"]) == layer:
                return {str(name) for name in rule.get("may_import", [])}
        raise AssertionError(f"no such frontend layer: {layer}")

    def layer_for(self, path: str) -> str | None:
        layer, _errors = quality_gate._layer_for(path, self.layers)
        return layer

    def test_the_task_and_workspace_features_stay_separated(self) -> None:
        tasks = self.layer_for("frontend/src/features/tasks/TaskBoard.tsx")
        workspace = self.layer_for("frontend/src/features/workspace/WorkspacePage.tsx")
        self.assertIsNotNone(tasks)
        self.assertIsNotNone(workspace)
        self.assertNotEqual(tasks, workspace)
        self.assertNotIn(workspace, self.permission(str(tasks)))
        self.assertNotIn(tasks, self.permission(str(workspace)))

    def test_the_domain_cannot_reach_a_feature_even_for_types(self) -> None:
        domain = self.layer_for("frontend/src/domain/schemas.ts")
        feature = self.layer_for("frontend/src/features/tasks/TaskBoard.tsx")
        self.assertIsNotNone(domain)
        self.assertIsNotNone(feature)
        self.assertNotIn(feature, self.permission(str(domain)))

    def test_a_neutral_domain_and_component_path_still_classify(self) -> None:
        for path in (
            "frontend/src/domain/types.ts",
            "frontend/src/components/Button.tsx",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(self.layer_for(path))


class ConfigurationShape(unittest.TestCase):
    """The selected configuration must not grow an escape hatch."""

    def test_no_layer_invents_an_exclude_or_wildcard_bucket(self) -> None:
        for rule in PYTHON_LAYERS:
            with self.subTest(layer=rule["name"]):
                self.assertNotIn("exclude_globs", rule)
                for glob in rule.get("globs", []):
                    self.assertNotEqual(str(glob), "**")
                    self.assertNotEqual(str(glob), "*")

    def test_the_desktop_layer_enumerates_its_twenty_paths(self) -> None:
        desktop = next(rule for rule in PYTHON_LAYERS if rule["name"] == "py_desktop")
        globs = [str(item) for item in desktop["globs"]]
        self.assertEqual(len(globs), 20)
        self.assertNotIn("desktop/python-webview-shell/**", globs)
        self.assertNotIn("desktop/python-webview-shell/local_workspace_rebind.py", globs)

    def test_the_config_stays_valid_json_with_the_expected_layers(self) -> None:
        raw = json.loads((ROOT / "quality" / "quality-config.json").read_text(encoding="utf-8"))
        names = {str(rule["name"]) for rule in raw["python_layers"]}
        for expected in set(EXPECTED_LAYER.values()):
            with self.subTest(layer=expected):
                self.assertIn(expected, names)


# ---------------------------------------------------------------------------
# QAC-F1: one reusable structural checker, used on real source and on mutants
# ---------------------------------------------------------------------------

WRITER_PATH = "workstack/cli_writer.py"
REBIND_PATH = "desktop/python-webview-shell/local_workspace_rebind.py"
STORE_MODULE = "workstack.store"
HELPER_NAME = "_pure_store_validators"

VALIDATOR_NAMES = (
    "StoreCorruptError",
    "_validate_store_manifest_files",
    "_validate_store_manifest_header",
    "_validate_store_manifest_tasks",
    "_validated_rebind_file_records",
)

EXPECTED_NAMESPACE = {
    "StoreCorruptError": "StoreCorruptError",
    "validate_manifest_header": "_validate_store_manifest_header",
    "validate_manifest_files": "_validate_store_manifest_files",
    "validate_manifest_tasks": "_validate_store_manifest_tasks",
    "validate_file_records": "_validated_rebind_file_records",
}


def _import_owner(node: ast.ImportFrom, package: str) -> str:
    """The absolute module an ImportFrom names, relative level included."""

    if not node.level:
        return node.module or ""
    parts = package.split(".") if package else []
    base = parts[: len(parts) - node.level + 1] if node.level > 1 else parts
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _store_imports(tree: ast.AST, package: str) -> "list[tuple[str, str | None]]":
    """Every (name, asname) this module takes from the Store, at any level."""

    taken: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _import_owner(node, package) == STORE_MODULE:
            taken.extend((alias.name, alias.asname) for alias in node.names)
    return taken


def _whole_module_store_imports(tree: ast.AST, package: str) -> list[str]:
    """Both whole-module import spellings widen beyond the admitted names."""

    widened: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            widened.extend(
                alias.name for alias in node.names if alias.name == STORE_MODULE
            )
        if isinstance(node, ast.ImportFrom) and _import_owner(node, package) == "workstack":
            widened.extend(STORE_MODULE for alias in node.names if alias.name == "store")
    return widened


def _store_aliases(tree: ast.AST, package: str) -> set[str]:
    """Every local name that refers to the Store class, alias included."""

    names: set[str] = set()
    for name, asname in _store_imports(tree, package):
        if name == "Store":
            names.add(asname or name)
    return names


def _constructs_store(tree: ast.AST, package: str) -> list[str]:
    """Calls to the Store class under any local name, whitespace irrelevant."""

    aliases = _store_aliases(tree, package)
    built: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in aliases:
                built.append(node.func.id)
    return built


def _helper_nodes(tree: ast.AST, name: str) -> "list[ast.FunctionDef | ast.AsyncFunctionDef]":
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _namespace_mapping(helper: "ast.FunctionDef") -> "dict[str, list[str]]":
    """Only the actual, sole direct return supplies the namespace slots."""

    returns = [node for node in ast.walk(helper) if isinstance(node, ast.Return)]
    if len(returns) != 1 or returns[0] is not helper.body[-1]:
        return {}
    value = returns[0].value
    if not isinstance(value, ast.Call) or value.args:
        return {}
    if ast.dump(value.func) != ast.dump(ast.parse("types.SimpleNamespace", mode="eval").body):
        return {}
    mapping: dict[str, list[str]] = {}
    for keyword in value.keywords:
        bound = keyword.value.id if isinstance(keyword.value, ast.Name) else "<not a name>"
        mapping.setdefault(keyword.arg or "**", []).append(bound)
    return mapping


def writer_store_contract(source: str, path: str = WRITER_PATH) -> list[str]:
    """The writer may take the MAX_REVISION constant and nothing else."""

    tree = ast.parse(source, filename=path)
    package = quality_gate._package_of(path, quality_gate._python_module(path) or "")
    problems: list[str] = []
    taken = _store_imports(tree, package)
    if [name for name, _ in taken] != ["MAX_REVISION"]:
        problems.append(f"store import is not exactly MAX_REVISION: {taken}")
    problems.extend(
        f"aliased store import: {name} as {asname}" for name, asname in taken if asname
    )
    owners = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
              and _import_owner(node, package) == STORE_MODULE]
    if len(owners) != 1 or (owners[0].level, owners[0].module) != (1, "store"):
        problems.append("writer requires its exact relative Store constant import")
    problems.extend(f"whole-module store import: {name}" for name in _whole_module_store_imports(tree, package))
    problems.extend(f"store construction: {name}" for name in _constructs_store(tree, package))
    return problems


def _validator_import_problems(helper, tree, package) -> list[str]:
    """Bind each import occurrence to the direct helper body, not equal tuples."""

    problems: list[str] = []
    direct = [node for node in helper.body if isinstance(node, ast.ImportFrom)]
    inside = [(alias.name, alias.asname) for node in direct for alias in node.names]
    if len(direct) != 1 or (direct[0].level, direct[0].module) != (0, STORE_MODULE):
        problems.append("helper requires one absolute workstack.store import")
    if sorted(name for name, _ in inside) != sorted(VALIDATOR_NAMES):
        problems.append(f"helper store import is not the five validators: {inside}")
    problems.extend(
        f"aliased validator import: {name} as {asname}" for name, asname in inside if asname
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _import_owner(node, package) == STORE_MODULE:
            if node not in direct:
                problems.append(f"store import outside the direct helper: line {node.lineno}")
    return problems


def _validator_body_problems(helper: ast.FunctionDef, tree: ast.Module) -> list[str]:
    """The approved adapter is one module-level import-and-return function."""

    body = list(helper.body)
    if ast.get_docstring(helper) is not None:
        body = body[1:]
    if helper not in tree.body:
        return ["validator helper is not module-level"]
    if len(body) != 2 or not isinstance(body[0], ast.ImportFrom) or not isinstance(body[1], ast.Return):
        return ["validator helper must directly import and return its namespace"]
    return []


def _namespace_problems(helper) -> list[str]:
    """The returned namespace must be exactly the five expected bindings."""

    mapping = _namespace_mapping(helper)
    problems = [
        f"duplicate namespace slot: {slot}"
        for slot, bound in sorted(mapping.items()) if len(bound) > 1
    ]
    flattened = {slot: bound[0] for slot, bound in mapping.items() if len(bound) == 1}
    if flattened != EXPECTED_NAMESPACE:
        problems.append(f"namespace mapping is not the expected five bindings: {flattened}")
    return problems


def rebind_store_contract(source: str, path: str = REBIND_PATH) -> list[str]:
    """The rebind adapter may take exactly five read-only validators."""

    tree = ast.parse(source, filename=path)
    package = quality_gate._package_of(path, quality_gate._python_module(path) or "")
    helpers = _helper_nodes(tree, HELPER_NAME)
    if len(helpers) != 1:
        return [f"expected exactly one {HELPER_NAME}, found {len(helpers)}"]
    helper = helpers[0]
    if not isinstance(helper, ast.FunctionDef):
        return ["validator helper must be synchronous"]
    problems = _validator_import_problems(helper, tree, package)
    problems.extend(_validator_body_problems(helper, tree))
    problems.extend(f"whole-module store import: {name}" for name in _whole_module_store_imports(tree, package))
    problems.extend(f"store construction: {name}" for name in _constructs_store(tree, package))
    problems.extend(_namespace_problems(helper))
    return problems


class StoreSourceContract(unittest.TestCase):
    """The same checker decides the real source and every mutant."""

    def rebind_source(self) -> str:
        return (ROOT / REBIND_PATH).read_text(encoding="utf-8")

    def writer_source(self) -> str:
        return (ROOT / WRITER_PATH).read_text(encoding="utf-8")

    def test_the_actual_sources_satisfy_the_checker(self) -> None:
        self.assertEqual(writer_store_contract(self.writer_source()), [])
        self.assertEqual(rebind_store_contract(self.rebind_source()), [])

    def test_an_async_definition_cannot_shadow_the_validator_helper(self) -> None:
        source = self.rebind_source() + "\nasync def _pure_store_validators():\n    return None\n"
        definitions = [node for node in ast.parse(source).body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name == HELPER_NAME]
        self.assertEqual([type(node) for node in definitions],
                         [ast.FunctionDef, ast.AsyncFunctionDef])
        self.assertNotEqual(rebind_store_contract(source), [])

    def test_an_otherwise_valid_async_validator_is_still_refused(self) -> None:
        source = self.rebind_source().replace(
            "def _pure_store_validators(", "async def _pure_store_validators(", 1,
        )
        definitions = [node for node in ast.parse(source).body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name == HELPER_NAME]
        self.assertEqual([type(node) for node in definitions], [ast.AsyncFunctionDef])
        self.assertNotEqual(rebind_store_contract(source), [])

    def test_an_unrelated_async_function_preserves_the_validator_contract(self) -> None:
        source = self.rebind_source() + "\nasync def unrelated_fixture_helper():\n    return None\n"
        ast.parse(source)
        self.assertEqual(rebind_store_contract(source), [])

    def test_the_actual_rebind_returns_the_exact_five_bindings(self) -> None:
        tree = ast.parse(self.rebind_source(), filename=REBIND_PATH)
        (helper,) = _helper_nodes(tree, HELPER_NAME)
        mapping = _namespace_mapping(helper)
        self.assertEqual({slot: bound[0] for slot, bound in mapping.items()}, EXPECTED_NAMESPACE)

    def test_an_unreachable_namespace_cannot_replace_the_actual_return(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "    return types.SimpleNamespace(",
            "    return None\n    types.SimpleNamespace(", 1,
        ), rebind_store_contract)

    def test_a_relative_store_import_cannot_impersonate_the_absolute_owner(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "    from workstack.store import (", "    from .workstack.store import (", 1,
        ), rebind_store_contract)

    def test_an_equal_import_occurrence_outside_the_helper_is_refused(self) -> None:
        suffix = "\nfrom workstack.store import _validate_store_manifest_header\n"
        self.assert_refused(self.rebind_source() + suffix, rebind_store_contract)

    def test_package_form_whole_store_imports_cannot_evade_the_budget(self) -> None:
        self.assert_refused(
            self.rebind_source() + "\nfrom workstack import store as widened\n",
            rebind_store_contract,
        )
        self.assert_refused(
            self.writer_source() + "\nfrom . import store as widened\n",
            writer_store_contract,
        )

    def test_the_writer_constant_keeps_its_exact_import_level(self) -> None:
        self.assert_refused(self.writer_source().replace(
            "from .store import MAX_REVISION", "from workstack.store import MAX_REVISION", 1,
        ), writer_store_contract)

    def test_nested_import_and_return_decoys_are_refused(self) -> None:
        source = self.rebind_source()
        tree = ast.parse(source)
        (helper,) = _helper_nodes(tree, HELPER_NAME)
        segment = ast.get_source_segment(source, helper)
        assert segment is not None
        nested = "def _pure_store_validators():\n    def decoy():\n"
        nested += "\n".join("        " + line for line in segment.splitlines()[1:])
        nested += "\n    return None\n"
        self.assert_refused(source.replace(segment, nested, 1), rebind_store_contract)

    def assert_refused(self, source: str, checker) -> None:
        ast.parse(source)  # a valid-syntax mutant, parsed and never imported
        self.assertNotEqual(checker(source), [])

    def test_a_wrong_header_binding_is_refused(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "validate_manifest_header=_validate_store_manifest_header",
            "validate_manifest_header=_validate_store_manifest_tasks",
        ), rebind_store_contract)

    def test_a_missing_namespace_slot_is_refused(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "        validate_manifest_header=_validate_store_manifest_header,\n", ""
        ), rebind_store_contract)

    def test_an_extra_namespace_slot_is_refused(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "        validate_file_records=_validated_rebind_file_records,\n",
            "        validate_file_records=_validated_rebind_file_records,\n"
            "        extra_slot=_validate_store_manifest_files,\n",
        ), rebind_store_contract)

    def test_a_duplicate_namespace_slot_is_refused(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "        validate_file_records=_validated_rebind_file_records,\n",
            "        validate_file_records=_validated_rebind_file_records,\n"
            "        validate_manifest_tasks=_validate_store_manifest_files,\n",
        ), rebind_store_contract)

    def test_a_broad_store_import_outside_the_helper_is_refused(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            f"def {HELPER_NAME}():",
            f"import workstack.store as extra_store\n\n\ndef {HELPER_NAME}():",
        ), rebind_store_contract)

    def test_an_aliased_store_construction_is_refused(self) -> None:
        # Whitespace before the call and an alias must not hide it.
        self.assert_refused(self.rebind_source().replace(
            f"def {HELPER_NAME}():",
            "from workstack.store import Store as Repository\n\n\n"
            "def _unexecuted_probe():\n    return Repository ('fixture')\n\n\n"
            f"def {HELPER_NAME}():",
        ), rebind_store_contract)

    def test_five_names_taken_from_the_wrong_owner_are_refused(self) -> None:
        self.assert_refused(self.rebind_source().replace(
            "    from workstack.store import (", "    from workstack.service import ("
        ), rebind_store_contract)

    def test_a_second_helper_definition_is_refused(self) -> None:
        self.assert_refused(
            self.rebind_source() + f"\n\ndef {HELPER_NAME}():\n    return None\n",
            rebind_store_contract,
        )

    def test_an_aliased_writer_constant_is_refused(self) -> None:
        self.assert_refused(self.writer_source().replace(
            "from .store import MAX_REVISION", "from .store import MAX_REVISION as _MAX"
        ), writer_store_contract)

    def test_a_widened_writer_import_is_refused(self) -> None:
        self.assert_refused(self.writer_source().replace(
            "from .store import MAX_REVISION", "from .store import MAX_REVISION, Store"
        ), writer_store_contract)
        self.assert_refused(self.writer_source().replace(
            "from .store import MAX_REVISION",
            "import workstack.store\nfrom .store import MAX_REVISION",
        ), writer_store_contract)


# ---------------------------------------------------------------------------
# QAC-F2: the actual critical evaluator, per newly critical path
# ---------------------------------------------------------------------------

def _probe_source(branches: int) -> str:
    body = "\n".join(
        f"    if value == {index}:\n        return {index}" for index in range(branches)
    )
    return f"def probe(value):\n{body}\n    return -1\n"


def _fixture_config(path: str) -> "dict[str, Any]":
    """The selected critical rule for one path, with an inert frontend seam."""

    return {
        "schema_version": CONFIG["schema_version"],
        "source_sets": [
            {"name": "python_core", "roots": [path.split("/")[0]], "extensions": [".py"]}
        ],
        "config_inputs": [],
        # Explicitly inert: the resolver returns empty measurements and runs no
        # command, so no npm or frontend runtime is ever reached.
        "frontend_complexity": {},
        "critical_python_globs": [path],
        "python_layers": [{"name": "py_probe", "globs": [f"{path.split('/')[0]}/**"], "may_import": []}],
        "frontend_layers": [],
        "architecture_exceptions": [],
    }


# Only config_digest may be reconciled by the root after source admission.
# Raw file identities for this packet belong in its terminal evidence; this
# semantic fingerprint preserves every other measured field, including unknown
# additions, without preventing that explicitly authorized metadata change.
PRESERVED_BASELINE_DIGEST = "34e7e46f08d6f887a7d6580ab4f907e232f6362d549920b7c6797ca4758b1ab0"


def baseline_preservation_problems(baseline: dict[str, Any]) -> list[str]:
    preserved = {key: value for key, value in baseline.items() if key != "config_digest"}
    payload = json.dumps(preserved, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != PRESERVED_BASELINE_DIGEST:
        return ["preserved baseline debt, populations or metadata changed"]
    return []


class CriticalEvaluator(unittest.TestCase):
    """measure, build_baseline and evaluate, unchanged, for all seven paths."""

    def run_probe(self, path: str, branches: int, baseline: "dict[str, Any] | None"):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_probe_source(branches), encoding="utf-8")
            config = _fixture_config(path)
            report = quality_gate.measure(root, config)
            if baseline is None:
                baseline = quality_gate.build_baseline(report, "fixture-commit")
            return report, baseline, quality_gate.evaluate(report, baseline)

    def test_a_healthy_probe_gives_no_errors_and_empty_debt(self) -> None:
        for path in NEW_MODULES:
            with self.subTest(path=path):
                report, baseline, errors = self.run_probe(path, 14, None)
                self.assertEqual(errors, [])
                self.assertEqual(baseline["critical_complexity_debt"], {})
                symbol = f"{path}::probe"
                self.assertEqual(report["python_complexity"][symbol]["ccn"], 15)
                self.assertTrue(report["python_complexity"][symbol]["critical"])

    def test_only_the_probe_changes_and_the_evaluator_refuses_ccn16(self) -> None:
        for path in NEW_MODULES:
            with self.subTest(path=path):
                _report, baseline, _errors = self.run_probe(path, 14, None)
                # The baseline comes from the healthy fixture alone, never from
                # a failing report and never from the repository.
                _r16, _b16, errors = self.run_probe(path, 15, baseline)
                self.assertIn(
                    f"new critical function exceeds CCN 15: {path}::probe has CCN 16",
                    errors,
                )

    def test_the_repository_baseline_and_config_are_untouched(self) -> None:
        # Byte identity during this packet is verified separately in its evidence.
        # Long-lived tests preserve the baseline semantics, not the stale digest.
        baseline = json.loads((ROOT / "quality/structural-baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline_preservation_problems(baseline), [])
        self.assertEqual(CONFIG["schema_version"], 1)
        self.assertTrue(set(NEW_MODULES).issubset(CONFIG["critical_python_globs"]))

    def test_only_config_digest_reconciliation_is_accepted_on_a_disposable_clone(self) -> None:
        original = json.loads((ROOT / "quality/structural-baseline.json").read_text(encoding="utf-8"))
        updated = dict(original, config_digest=quality_gate._digest_files(ROOT, CONFIG["config_inputs"]))
        with tempfile.TemporaryDirectory() as raw:
            clone = Path(raw) / "baseline.json"
            clone.write_text(json.dumps(updated), encoding="utf-8")
            parsed = json.loads(clone.read_text(encoding="utf-8"))
        self.assertEqual(parsed["config_digest"], quality_gate._digest_files(ROOT, CONFIG["config_inputs"]))
        self.assertEqual(baseline_preservation_problems(parsed), [])

    def test_digest_reconciliation_cannot_hide_other_baseline_changes(self) -> None:
        original = json.loads((ROOT / "quality/structural-baseline.json").read_text(encoding="utf-8"))
        mutations = {
            "critical_complexity_debt": {"workstack/service.py::probe": 16},
            "critical_typescript_complexity_debt": {"frontend/src/app/probe.ts::probe": 16},
            "source_populations": dict(original["source_populations"], python_core=0),
            "coverage_floors": {"python_core": 0},
            "temporary_exceptions": [{"from": "a", "to": "b", "expires": "2099-01-01"}],
            "measurement_commit": "replacement",
            "measurement_source_digest": "replacement",
            "schema_version": 2,
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                clone = Path(raw) / "baseline.json"
                changed = dict(original, config_digest=quality_gate._digest_files(ROOT, CONFIG["config_inputs"]))
                changed[field] = value
                clone.write_text(json.dumps(changed), encoding="utf-8")
                self.assertEqual(
                    baseline_preservation_problems(json.loads(clone.read_text(encoding="utf-8"))),
                    ["preserved baseline debt, populations or metadata changed"],
                )


# ---------------------------------------------------------------------------
# QAC-F3: actual consumer imports and real graph violations
# ---------------------------------------------------------------------------

def _production_modules() -> "dict[str, str]":
    populations, errors = quality_gate._discover(ROOT, CONFIG)
    assert not errors, errors
    files = sorted(path for paths in populations.values() for path in paths if path.endswith(".py"))
    return {
        module: path for path in files if (module := quality_gate._python_module(path))
    }


def actual_imports(path: str) -> "set[str]":
    """The production paths one REAL source file imports, via the resolver."""

    modules = _production_modules()
    module = quality_gate._python_module(path)
    assert module, path
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    targets, errors = quality_gate._python_imports(path, module, tree, modules)
    assert not errors, errors
    return targets


def parsed_python_fixture(files: dict[str, str], rules=None):
    """Discover real fixture files, parse them, resolve imports, then classify."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        config = {"source_sets": [{"name": "fixture", "roots": ["."], "extensions": [".py"]}]}
        populations, errors = quality_gate._discover(root, config)
        assert not errors, errors
        discovered = populations["fixture"]
        assert discovered == sorted(files), discovered
        graph, _complexity, errors = quality_gate._python_graph(root, discovered, [])
        assert not errors, errors
        layers, unclassified = quality_gate._classify_layers(
            [(discovered, PYTHON_LAYERS if rules is None else rules)]
        )
        return graph, layers, unclassified


def expected_violation(source: str, target: str, layers: dict[str, str]) -> str:
    return f"forbidden layer import: {source} ({layers[source]}) -> {target} ({layers[target]})"


REQUIRED_CONSUMERS = {
    "workstack/cli.py": {"workstack/checkpoint_state_cli.py", "workstack/cli_writer.py"},
    "workstack/checkpoint_state_cli.py": {"workstack/checkpoint_transition.py", "workstack/cli_writer.py"},
    "workstack/service.py": {
        "workstack/checkpoint_change.py", "workstack/checkpoint_projection.py",
        "workstack/checkpoint_transition.py", "workstack/context_projection.py",
    },
    "workstack/server.py": {"workstack/sse_events.py"},
    "workstack/checkpoint_projection.py": {"workstack/checkpoint_transition.py", "workstack/storage/canonical.py"},
    "workstack/checkpoint_change.py": {"workstack/storage/canonical.py"},
    "workstack/context_projection.py": {"workstack/capture.py"},
    WRITER_PATH: {"workstack/store.py"},
    "desktop/python-webview-shell/workstack_desktop.py": {REBIND_PATH},
    REBIND_PATH: {"workstack/store.py"},
}


def real_violations(graph: "dict[str, set[str]]") -> "list[str]":
    """Run the actual policy over a graph of real production paths."""

    paths = sorted(set(graph) | {target for targets in graph.values() for target in targets})
    layers, unclassified = quality_gate._classify_layers([(paths, PYTHON_LAYERS)])
    assert not unclassified, unclassified
    return quality_gate._layer_violations((graph,), layers, PYTHON_LAYERS, set())


class ActualConsumerImports(unittest.TestCase):
    """Read the real imports, then judge them with the real policy."""

    def assert_clean(self, source: str) -> "set[str]":
        targets = actual_imports(source)
        self.assertEqual(real_violations({source: targets}), [])
        for required in sorted(REQUIRED_CONSUMERS.get(source, set())):
            self.assertIn(required, targets, f"required actual import removed: {source} -> {required}")
        return targets

    def test_the_checkpoint_cli_actually_imports_its_contract_and_writer(self) -> None:
        targets = self.assert_clean("workstack/checkpoint_state_cli.py")
        self.assertIn("workstack/checkpoint_transition.py", targets)
        self.assertIn("workstack/cli_writer.py", targets)

    def test_the_cli_package_actually_reaches_the_new_submodules(self) -> None:
        targets = self.assert_clean("workstack/cli.py")
        self.assertTrue({
            "workstack/checkpoint_state_cli.py", "workstack/cli_writer.py",
        }.issubset(targets), targets)

    def test_the_service_actually_reaches_its_pure_owners(self) -> None:
        targets = self.assert_clean("workstack/service.py")
        self.assertTrue({
            "workstack/checkpoint_change.py",
            "workstack/checkpoint_projection.py",
            "workstack/checkpoint_transition.py",
            "workstack/context_projection.py",
        }.issubset(targets), targets)

    def test_the_server_projection_context_and_writer_stay_clean(self) -> None:
        for path in (
            "workstack/server.py",
            "workstack/checkpoint_projection.py",
            "workstack/context_projection.py",
            "workstack/cli_writer.py",
            "workstack/checkpoint_change.py",
            "workstack/sse_events.py",
            "workstack/checkpoint_transition.py",
        ):
            with self.subTest(path=path):
                self.assert_clean(path)

    def test_the_pure_contract_and_encoder_import_no_production_module(self) -> None:
        for path in ("workstack/checkpoint_transition.py", "workstack/sse_events.py"):
            with self.subTest(path=path):
                self.assertEqual(actual_imports(path), set())

    def test_the_actual_host_and_rebind_keep_their_required_edges(self) -> None:
        for path in ("desktop/python-webview-shell/workstack_desktop.py", REBIND_PATH):
            with self.subTest(path=path):
                self.assert_clean(path)


class ForbiddenInverseGraphs(unittest.TestCase):
    """Every prohibited edge becomes a real violation of the real policy."""

    INVERSES = (
        ("workstack/store.py", "workstack/service.py"),
        ("workstack/store.py", "workstack/cli.py"),
        ("workstack/store.py", "workstack/checkpoint_state_cli.py"),
        ("workstack/storage/canonical.py", "workstack/service.py"),
        ("workstack/storage/canonical.py", "workstack/cli.py"),
        ("workstack/storage/canonical.py", "workstack/checkpoint_state_cli.py"),
        ("workstack/capture.py", "workstack/service.py"),
        ("workstack/capture.py", "workstack/cli.py"),
        ("workstack/capture.py", "workstack/checkpoint_state_cli.py"),
        ("workstack/checkpoint_transition.py", "workstack/store.py"),
        ("workstack/checkpoint_transition.py", "workstack/service.py"),
        ("workstack/checkpoint_transition.py", "workstack/cli.py"),
        ("workstack/sse_events.py", "workstack/store.py"),
        ("workstack/sse_events.py", "workstack/service.py"),
        ("workstack/sse_events.py", "workstack/cli.py"),
        ("workstack/checkpoint_change.py", "workstack/server.py"),
        ("workstack/checkpoint_change.py", "workstack/cli.py"),
        ("workstack/checkpoint_projection.py", "workstack/server.py"),
        ("workstack/checkpoint_projection.py", "workstack/cli.py"),
        ("workstack/context_projection.py", "workstack/server.py"),
        ("workstack/context_projection.py", "workstack/cli.py"),
        ("workstack/cli_writer.py", "workstack/service.py"),
        ("workstack/cli_writer.py", "workstack/server.py"),
        ("workstack/cli_writer.py", "workstack/checkpoint_state_cli.py"),
        (REBIND_PATH, "workstack/service.py"),
        (REBIND_PATH, "workstack/server.py"),
        (REBIND_PATH, "workstack/cli.py"),
        (REBIND_PATH, "desktop/python-webview-shell/workstack_desktop.py"),
        ("desktop/python-webview-shell/ssot_connection.py", "workstack/store.py"),
    )

    def test_every_forbidden_edge_is_reported(self) -> None:
        for source, target in self.INVERSES:
            with self.subTest(source=source, target=target):
                module = quality_gate._python_module(target)
                self.assertIsNotNone(module)
                graph, layers, unclassified = parsed_python_fixture({
                    source: f"import {module}\n", target: "VALUE = 1\n",
                })
                self.assertEqual(unclassified, [])
                self.assertEqual(graph, {source: {target}, target: set()})
                violations = quality_gate._layer_violations((graph,), layers, PYTHON_LAYERS, set())
                self.assertEqual(violations, [expected_violation(source, target, layers)])

    def test_a_mixed_alias_import_keeps_both_forbidden_edges(self) -> None:
        # A named submodule AND a package symbol in one statement: both survive.
        source = "workstack/sse_events.py"
        targets = {WRITER_PATH, "workstack/__init__.py"}
        graph, layers, unclassified = parsed_python_fixture({
            source: "from workstack import cli_writer, MAX_REVISION\n",
            WRITER_PATH: "VALUE = 1\n",
            "workstack/__init__.py": "MAX_REVISION = 100\n",
        })
        self.assertEqual(unclassified, [])
        self.assertEqual(graph[source], targets)
        violations = quality_gate._layer_violations((graph,), layers, PYTHON_LAYERS, set())
        self.assertCountEqual(violations, [expected_violation(source, target, layers) for target in targets])


class FrontendTypeOnlyImports(unittest.TestCase):
    """A type-only import is still an edge for the real frontend graph."""

    DOMAIN = "frontend/src/domain/probe_types.ts"
    FEATURE = "frontend/src/features/tasks/ProbeBoard.tsx"
    WORKSPACE = "frontend/src/features/workspace/views/ProbeWorkspace.tsx"
    NEUTRAL = "frontend/src/domain/probe_neutral.ts"
    COMPONENT = "frontend/src/components/ProbeShared.tsx"

    def build(self, files: "dict[str, str]") -> "list[str]":
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for path, text in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            graph, errors = quality_gate._frontend_graph(root, sorted(files))
            assert not errors, errors
            layers, unclassified = quality_gate._classify_layers(
                [(sorted(files), list(CONFIG["frontend_layers"]))]
            )
            assert not unclassified, unclassified
            return quality_gate._layer_violations(
                (graph,), layers, list(CONFIG["frontend_layers"]), set()
            )

    def test_a_type_only_domain_to_feature_import_is_a_violation(self) -> None:
        violations = self.build({
            self.DOMAIN: "import type { Probe } from '../features/tasks/ProbeBoard'\n"
                         "export type Alias = Probe\n",
            self.FEATURE: "export type Probe = { id: string }\n",
        })
        self.assertEqual(violations, [
            f"forbidden layer import: {self.DOMAIN} (fe_domain) -> {self.FEATURE} (fe_feature_tasks)",
        ])

    def test_type_only_tasks_to_workspace_implementation_is_forbidden(self) -> None:
        violations = self.build({
            self.FEATURE: "import type { Probe } from '../workspace/views/ProbeWorkspace'\nexport type Alias = Probe\n",
            self.WORKSPACE: "export type Probe = { id: string }\n",
        })
        self.assertEqual(violations, [
            f"forbidden layer import: {self.FEATURE} (fe_feature_tasks) -> {self.WORKSPACE} (fe_feature_workspace)",
        ])

    def test_type_only_workspace_to_task_renderer_is_forbidden(self) -> None:
        violations = self.build({
            self.WORKSPACE: "import type { Probe } from '../../tasks/ProbeBoard'\nexport type Alias = Probe\n",
            self.FEATURE: "export type Probe = { id: string }\n",
        })
        self.assertEqual(violations, [
            f"forbidden layer import: {self.WORKSPACE} (fe_feature_workspace) -> {self.FEATURE} (fe_feature_tasks)",
        ])

    def test_a_neutral_domain_to_domain_import_is_allowed(self) -> None:
        violations = self.build({
            self.NEUTRAL: "import type { Probe } from './probe_types'\nexport type B = Probe\n",
            self.DOMAIN: "export type Probe = { id: string }\n",
        })
        self.assertEqual(violations, [])

    def test_a_shared_component_and_feature_can_use_neutral_types(self) -> None:
        violations = self.build({
            self.DOMAIN: "export type Probe = { id: string }\n",
            self.COMPONENT: "import type { Probe } from '../domain/probe_types'\nexport type Shared = Probe\n",
            self.FEATURE: "import type { Shared } from '../../components/ProbeShared'\nexport type Alias = Shared\n",
        })
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
