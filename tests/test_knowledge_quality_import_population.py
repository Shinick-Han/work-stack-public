"""Additive admission of the knowledge capture import population.

``test_knowledge_quality_population.py`` already governs the retrieval and
OpenDocuments registration and is at its file budget, so the three capture
import modules integrated after that lane froze are registered here instead,
with independent imports and its own fixtures. Nothing in the sibling file is
moved, weakened or hidden.

Three production modules landed unclassified in this checkout:

* ``workstack/knowledge_capture_packets.py`` - the pure packet contract,
* ``workstack/knowledge_capture_import.py`` - the application orchestrator that
  turns reviewed packets into ledger stages, and
* ``workstack/knowledge_captures_http.py`` - the HTTP import boundary.

Registration is driven through the REAL resolver - ``_discover``,
``_classify_layers``, ``_python_graph`` and ``_layer_violations`` from
``scripts.quality_gate`` - over both populations used by the sibling file:

* the ACTUAL candidate scan of this checkout, where every one of these modules
  and every one of their callers has landed, and
* SYNTHETIC temporary trees, used for the refusal families the integrated
  checkout no longer exhibits - an importer reaching the store, the task
  application or storage; an adapter reaching the packet contract; a dist gate
  helper root declared before its file exists.

The orchestrator really imports ``service_domain._next_id``. That dependency is
kept honest by carving ``service_domain`` and its error taxonomy out of the
coarse ``py_application`` layer into two narrowly named layers - identifier and
error primitives - rather than granting the importer the whole application or
reassigning the application into the foundation. The importer is granted the
identifier layer alone, and the refusal tests below show it still cannot reach
the store, the task application or storage.

Population sizes are deliberately never frozen as literals: each population is
compared against what is actually on disk, so a later module can neither slip
in unregistered nor be quietly dropped.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.quality_gate import (
    _classify_layers,
    _discover,
    _layer_violations,
    _python_graph,
    load_config,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The three integrated modules and the layer each one must now carry.
IMPORTER_MODULES = (
    ("workstack/knowledge_capture_packets.py", "py_knowledge_capture_packets"),
    ("workstack/knowledge_capture_import.py", "py_knowledge_capture_import"),
    ("workstack/knowledge_captures_http.py", "py_knowledge_captures_http"),
)

# The two application modules carved into narrow layers so the orchestrator's
# real identifier dependency can be granted without a whole-application grant.
NARROWED_APPLICATION_MODULES = (
    ("workstack/service_errors.py", "py_service_errors"),
    ("workstack/service_domain.py", "py_service_domain"),
)

# Every internal import each new module really makes, with the layer of the
# target. These are OUTGOING edges: the reach the new layers were granted.
OUTGOING_EDGES = {
    "workstack/knowledge_capture_packets.py": {
        "workstack/capture.py": "py_foundation",
        "workstack/capture_retrieval.py": "py_capture_retrieval",
        "workstack/knowledge_request.py": "py_knowledge_request",
    },
    "workstack/knowledge_capture_import.py": {
        "workstack/knowledge_capture_packets.py": "py_knowledge_capture_packets",
        "workstack/knowledge_ledger_document.py": "py_knowledge_ledger_document",
        "workstack/knowledge_owner_requests.py": "py_knowledge_owner_requests",
        "workstack/knowledge_request.py": "py_knowledge_request",
        "workstack/service_domain.py": "py_service_domain",
    },
    "workstack/knowledge_captures_http.py": {
        "workstack/capture_retrieval.py": "py_capture_retrieval",
        "workstack/knowledge_capture_import.py": "py_knowledge_capture_import",
        "workstack/knowledge_capture_packets.py": "py_knowledge_capture_packets",
        "workstack/knowledge_ledger_document.py": "py_knowledge_ledger_document",
        "workstack/knowledge_request.py": "py_knowledge_request",
        "workstack/knowledge_request_issuer.py": "py_knowledge_request_issuer",
        "workstack/knowledge_requests_http.py": "py_knowledge_requests_http",
        "workstack/server_errors.py": "py_transport",
    },
}

# INCOMING edges: existing callers in this checkout that import one of the new
# modules. Registering only the outgoing reach would leave each of these a
# forbidden import. Every entry records why the grant matches the caller's role.
INCOMING_EDGES = (
    (
        "workstack/service_capture_rules.py",
        "py_application",
        "workstack/knowledge_capture_packets.py",
        "py_knowledge_capture_packets",
        "the capture rules delegate the imported-packet shape rule to the pure "
        "contract that owns it instead of restating the packet schema",
    ),
    (
        "workstack/store_document_validation.py",
        "py_store_validation",
        "workstack/knowledge_capture_packets.py",
        "py_knowledge_capture_packets",
        "document validation asks the packet contract for an imported capture "
        "defect exactly as it already asks the ledger contract for its shape",
    ),
    (
        "workstack/server.py",
        "py_transport",
        "workstack/knowledge_captures_http.py",
        "py_knowledge_captures_http",
        "the server composes the capture import route mixin beside the "
        "knowledge request mixin it already composes",
    ),
    (
        "workstack/server_post_routes.py",
        "py_transport",
        "workstack/knowledge_captures_http.py",
        "py_knowledge_captures_http",
        "POST routing reads that surface's own import path and body limit "
        "rather than restating either constant",
    ),
)

# Each newly declared grant, with the real edges it and only it permits.
# Withdrawing one must bring back exactly those edges.
NEW_GRANTS = (
    (
        "py_application",
        "py_knowledge_capture_packets",
        ("workstack/service_capture_rules.py",),
    ),
    (
        "py_store_validation",
        "py_knowledge_capture_packets",
        ("workstack/store_document_validation.py",),
    ),
    (
        "py_transport",
        "py_knowledge_captures_http",
        ("workstack/server.py", "workstack/server_post_routes.py"),
    ),
    (
        "py_knowledge_capture_import",
        "py_service_domain",
        ("workstack/knowledge_capture_import.py",),
    ),
    (
        "py_knowledge_captures_http",
        "py_knowledge_capture_import",
        ("workstack/knowledge_captures_http.py",),
    ),
)

# The dist gate refactor lands its helpers under this one file prefix. The
# layer claims the prefix now; the base gate file stays a mandatory root.
DIST_GATE_ROOT = "scripts/dist_source_gate.py"
DIST_GATE_HELPERS = (
    "scripts/dist_source_gate_manifest.py",
    "scripts/dist_source_gate_receipt.py",
)
UNRELATED_SCRIPT = "scripts/dist_release_notes.py"

# The dist lane loads its helpers through an explicit
# ``importlib.util.spec_from_file_location`` under its own module names, so the
# receipt <- manifest <- facade direction is invisible to the AST import graph.
# The layer registration therefore governs ADMISSION and CLASSIFICATION of those
# files; it deliberately does not claim to enforce that direction.
DIST_GATE_DYNAMIC_LOAD = (
    "import importlib.util\n"
    "from pathlib import Path\n"
    "\n"
    "def _load(name, relative):\n"
    "    spec = importlib.util.spec_from_file_location(\n"
    "        name, Path(__file__).with_name(relative)\n"
    "    )\n"
    "    return spec\n"
)

# R14 deliberately classified these existing helpers; they must remain in the
# measured production population, not disappear through an exclusion.
FORMER_UNCLASSIFIED_HELPERS = (
    ("desktop/python-webview-shell/desktop_update_process.py", "python_desktop", "py_desktop"),
    ("workstack/cli_output.py", "python_core", "py_cli_output"),
)


@dataclass(frozen=True)
class Resolution:
    """What the real gate makes of one tree: population, owner and layer."""

    populations: dict[str, list[str]]
    errors: list[str]
    owner: dict[str, str]
    layers: dict[str, str]
    unclassified: list[str]

    def admitted(self, path: str) -> bool:
        return path in self.owner

    def missing_roots(self) -> list[str]:
        prefix = "missing production root: "
        return sorted(
            error[len(prefix) :] for error in self.errors if error.startswith(prefix)
        )


def resolve(root: Path, config: dict[str, Any]) -> Resolution:
    """Run the gate's own discovery and layer classification over one tree."""

    populations, errors = _discover(root, config)
    files = sorted(path for paths in populations.values() for path in paths)
    python_files = sorted(path for path in files if path.endswith(".py"))
    frontend_files = sorted(path for path in files if path.endswith((".ts", ".tsx")))
    other_files = sorted(
        path for path in files if path not in python_files and path not in frontend_files
    )
    layers, unclassified = _classify_layers(
        [
            (python_files, config["python_layers"]),
            (frontend_files, config["frontend_layers"]),
            (other_files, config["python_layers"]),
        ]
    )
    owner = {path: name for name, paths in populations.items() for path in paths}
    return Resolution(populations, errors, owner, layers, unclassified)


def write(root: Path, relative: str, body: str = "") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def stub_declared_roots(root: Path, config: dict[str, Any]) -> None:
    """Create every declared root, so the synthetic tree is a real checkout.

    Roots are read from the configuration under test rather than restated, so
    the fixture keeps working when another lane declares a further root.
    """

    for source_set in config["source_sets"]:
        for name in source_set["roots"]:
            if PurePosixPath(str(name)).suffix:
                write(root, str(name))
            else:
                (root / str(name)).mkdir(parents=True, exist_ok=True)


def layer_named(config: dict[str, Any], name: str) -> dict[str, Any]:
    for layer in config["python_layers"]:
        if layer["name"] == name:
            return layer
    raise AssertionError(f"no layer named {name}")


class ActualImporterRegistrationTests(unittest.TestCase):
    """The ACTUAL candidate scan of this checkout."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(REPOSITORY_ROOT)
        cls.actual = resolve(REPOSITORY_ROOT, cls.config)

    def test_the_actual_scan_still_reports_no_discovery_finding(self) -> None:
        """The additive registration declares no root that is not on disk."""

        self.assertEqual([], self.actual.errors)

    def test_the_landed_dist_helpers_are_discovered_and_layered(self) -> None:
        for path in DIST_GATE_HELPERS:
            with self.subTest(path=path):
                self.assertTrue((REPOSITORY_ROOT / path).is_file())
                self.assertEqual("quality_tooling", self.actual.owner.get(path))
                self.assertEqual("py_quality_tooling", self.actual.layers.get(path))

    def test_the_three_integrated_modules_are_admitted_and_layered(self) -> None:
        for path, layer in IMPORTER_MODULES:
            with self.subTest(path=path):
                self.assertTrue((REPOSITORY_ROOT / path).is_file(), f"{path} is absent")
                self.assertEqual("python_core", self.actual.owner.get(path))
                self.assertEqual(layer, self.actual.layers.get(path))
                self.assertNotIn(path, self.actual.unclassified)

    def test_the_narrowed_application_modules_carry_their_own_layers(self) -> None:
        """The identifier and error primitives left the coarse application layer.

        They are not reassigned into the foundation and keep their real
        dependencies; they simply stop being indistinguishable from the task
        application, so one importer can be granted one of them.
        """

        for path, layer in NARROWED_APPLICATION_MODULES:
            with self.subTest(path=path):
                self.assertEqual(layer, self.actual.layers.get(path))
        application = layer_named(self.config, "py_application")
        for path, _layer in NARROWED_APPLICATION_MODULES:
            self.assertNotIn(path, application["globs"])
        self.assertNotEqual([], application["globs"], "the application layer was emptied")

    def test_the_core_population_is_everything_on_disk_not_a_frozen_count(self) -> None:
        """The population equals the real ``.py`` files under the core root.

        Comparing against disk rather than a recorded number means a module
        landing later is neither admitted invisibly nor quietly excluded.
        """

        on_disk = sorted(
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in (REPOSITORY_ROOT / "workstack").rglob("*.py")
            if "__pycache__" not in path.parts
        )
        self.assertEqual(on_disk, self.actual.populations["python_core"])
        for path, _layer in IMPORTER_MODULES:
            self.assertIn(path, self.actual.populations["python_core"])

    def test_former_unclassified_helpers_remain_measured_in_explicit_layers(self) -> None:
        """Classification fixes retain measurement and reject silent exclusion."""

        for path, population, layer in FORMER_UNCLASSIFIED_HELPERS:
            with self.subTest(path=path):
                self.assertEqual(population, self.actual.owner.get(path))
                self.assertEqual(layer, self.actual.layers.get(path))
                self.assertNotIn(path, self.actual.unclassified)
        self.assertEqual([], self.actual.unclassified)


class ActualImportGraphTests(unittest.TestCase):
    """The REAL import graph of this checkout, not a restated intention."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(REPOSITORY_ROOT)
        cls.actual = resolve(REPOSITORY_ROOT, cls.config)
        python_files = sorted(
            path for path in cls.actual.owner if path.endswith(".py")
        )
        cls.graph, _complexity, cls.graph_errors = _python_graph(
            REPOSITORY_ROOT, python_files, cls.config.get("critical_python_globs", [])
        )
        cls.rules = cls.config["python_layers"] + cls.config["frontend_layers"]

    def violations(self, rules: list[dict[str, Any]] | None = None) -> list[str]:
        return sorted(
            set(
                _layer_violations(
                    (self.graph,), self.actual.layers, rules or self.rules, set()
                )
            )
        )

    def test_the_import_graph_of_this_checkout_parses(self) -> None:
        self.assertEqual([], self.graph_errors)

    def test_each_new_module_imports_exactly_its_registered_reach(self) -> None:
        """The granted reach is the reach the source really takes, no wider."""

        for path, targets in OUTGOING_EDGES.items():
            with self.subTest(path=path):
                self.assertEqual(sorted(targets), sorted(self.graph[path]))
                for target, target_layer in targets.items():
                    self.assertEqual(target_layer, self.actual.layers.get(target))

    def test_each_incoming_caller_is_a_real_import_carrying_its_layers(self) -> None:
        """Incoming edges are registered too, not only the outgoing ones."""

        for caller, caller_layer, target, target_layer, _why in INCOMING_EDGES:
            with self.subTest(caller=caller, target=target):
                self.assertIn(target, self.graph[caller])
                self.assertEqual(caller_layer, self.actual.layers.get(caller))
                self.assertEqual(target_layer, self.actual.layers.get(target))

    def test_the_real_graph_reports_no_forbidden_layer_import(self) -> None:
        self.assertEqual([], self.violations())

    def test_withdrawing_one_new_grant_brings_back_exactly_its_own_edges(self) -> None:
        """Every declared grant is load-bearing for the edges it names."""

        for source_layer, target_layer, callers in NEW_GRANTS:
            with self.subTest(grant=f"{source_layer} -> {target_layer}"):
                rules = copy.deepcopy(self.rules)
                for rule in rules:
                    if rule["name"] == source_layer:
                        rule["may_import"] = [
                            name for name in rule["may_import"] if name != target_layer
                        ]
                violations = self.violations(rules)
                self.assertEqual(len(callers), len(violations), violations)
                for caller, violation in zip(sorted(callers), violations):
                    self.assertIn(caller, violation)
                    self.assertIn(source_layer, violation)
                    self.assertIn(target_layer, violation)

    def test_withdrawing_every_new_grant_reproduces_those_edges_and_no_others(self) -> None:
        rules = copy.deepcopy(self.rules)
        withdrawn = {(source, target) for source, target, _ in NEW_GRANTS}
        for rule in rules:
            rule["may_import"] = [
                name
                for name in rule["may_import"]
                if (rule["name"], name) not in withdrawn
            ]
        expected = sorted(
            caller for _source, _target, callers in NEW_GRANTS for caller in callers
        )
        violations = self.violations(rules)
        self.assertEqual(len(expected), len(violations), violations)
        for caller, violation in zip(expected, violations):
            self.assertIn(caller, violation)


class ImporterGrantShapeTests(unittest.TestCase):
    """The grants are precise caller-role edges, not blanket reach."""

    def setUp(self) -> None:
        self.config = load_config(REPOSITORY_ROOT)

    def test_every_permitted_import_names_a_declared_layer(self) -> None:
        declared = {
            str(layer["name"])
            for layer in self.config["python_layers"] + self.config["frontend_layers"]
        }
        for layer in self.config["python_layers"]:
            for name in layer["may_import"]:
                with self.subTest(layer=layer["name"], grant=name):
                    self.assertIn(name, declared)

    def test_layer_names_stay_unique_after_the_additions(self) -> None:
        names = [str(layer["name"]) for layer in self.config["python_layers"]]
        self.assertEqual(sorted(set(names)), sorted(names))

    def test_the_packet_contract_is_granted_no_store_or_application_reach(self) -> None:
        packets = layer_named(self.config, "py_knowledge_capture_packets")
        for forbidden in (
            "py_legacy_store",
            "py_storage",
            "py_application",
            "py_service_domain",
            "py_transport",
        ):
            self.assertNotIn(forbidden, packets["may_import"])

    def test_the_orchestrator_is_granted_the_identifier_layer_alone(self) -> None:
        """Not the application, the store or storage - one narrow neighbour."""

        importer = layer_named(self.config, "py_knowledge_capture_import")
        self.assertIn("py_service_domain", importer["may_import"])
        for forbidden in (
            "py_application",
            "py_legacy_store",
            "py_storage",
            "py_transport",
        ):
            self.assertNotIn(forbidden, importer["may_import"])

    def test_the_identifier_layer_did_not_become_a_foundation_grab(self) -> None:
        """It keeps its real dependencies and claims one file only."""

        domain = layer_named(self.config, "py_service_domain")
        self.assertEqual(["workstack/service_domain.py"], domain["globs"])
        self.assertEqual(["py_service_errors", "py_legacy_store"], domain["may_import"])
        errors = layer_named(self.config, "py_service_errors")
        self.assertEqual(["workstack/service_errors.py"], errors["globs"])
        self.assertEqual(["py_snapshot"], errors["may_import"])

    def test_no_architecture_exception_was_added_for_the_new_layers(self) -> None:
        """The registration is layers and grants, never a per-edge waiver."""

        for exception in self.config.get("architecture_exceptions", []):
            for endpoint in (str(exception["from"]), str(exception["to"])):
                self.assertFalse(
                    endpoint.startswith("workstack/knowledge_capture"),
                    f"a waiver was added for {endpoint}",
                )
                self.assertFalse(
                    endpoint.startswith("workstack/service_domain"),
                    f"a waiver was added for {endpoint}",
                )


class SyntheticTreeCase(unittest.TestCase):
    """A temporary checkout in which every declared root really exists."""

    def setUp(self) -> None:
        self.config = load_config(REPOSITORY_ROOT)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub_declared_roots(self.root, self.config)

    def resolved(self, config: dict[str, Any] | None = None) -> Resolution:
        resolution = resolve(self.root, config or self.config)
        self.assertEqual([], resolution.missing_roots(), "the synthetic tree is incomplete")
        return resolution


class ImporterBoundaryTests(SyntheticTreeCase):
    """SYNTHETIC refusals the integrated checkout no longer exhibits."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "workstack/capture.py", "SHA256_RE = None\n")
        write(self.root, "workstack/snapshot.py", "class SnapshotValidationError(Exception):\n    pass\n")
        write(self.root, "workstack/store.py", "MAX_REVISION = 1\n")
        write(self.root, "workstack/maintenance.py", "def sweep():\n    return 1\n")
        write(self.root, "workstack/server_errors.py", "class RequestError(Exception):\n    pass\n")
        write(self.root, "workstack/service_errors.py", "class DomainError(Exception):\n    pass\n")
        write(self.root, "workstack/service_domain.py", "def _next_id():\n    return 1\n")
        write(self.root, "workstack/service_task_commands.py", "def run():\n    return 1\n")
        write(self.root, "workstack/knowledge_request.py", "REQUEST_VERSION = 1\n")
        write(self.root, "workstack/capture_retrieval.py", "RETRIEVAL_VERSION = 1\n")
        write(self.root, "workstack/knowledge_ledger_document.py", "LEDGER = 1\n")
        write(self.root, "workstack/knowledge_owner_requests.py", "OWNER = 1\n")
        write(self.root, "workstack/knowledge_request_issuer.py", "ISSUER = 1\n")
        write(self.root, "workstack/knowledge_requests_http.py", "PREFIX = '/k'\n")
        write(self.root, "workstack/knowledge_capture_packets.py", "PACKET = 1\n")
        write(self.root, "workstack/knowledge_capture_import.py", "IMPORTER = 1\n")
        write(self.root, "workstack/knowledge_captures_http.py", "PREFIX = '/captures'\n")

    def _violations(self) -> list[str]:
        resolution = self.resolved()
        python_files = sorted(path for path in resolution.owner if path.endswith(".py"))
        graph, _complexity, errors = _python_graph(self.root, python_files, [])
        self.assertEqual([], errors)
        rules = self.config["python_layers"] + self.config["frontend_layers"]
        return _layer_violations((graph,), resolution.layers, rules, set())

    def _permits(self, caller: str, body: str) -> None:
        write(self.root, caller, body)
        self.assertEqual([], self._violations())

    def _refuses(self, caller: str, caller_layer: str, body: str, target_layer: str) -> None:
        write(self.root, caller, body)
        violations = self._violations()
        self.assertEqual(1, len(violations), violations)
        self.assertIn(caller, violations[0])
        self.assertIn(caller_layer, violations[0])
        self.assertIn(target_layer, violations[0])

    def test_the_packet_contract_reaches_only_the_pure_retrieval_contracts(self) -> None:
        self._permits(
            "workstack/knowledge_capture_packets.py",
            "from workstack.capture import SHA256_RE\n"
            "from workstack.capture_retrieval import RETRIEVAL_VERSION\n"
            "from workstack.knowledge_request import REQUEST_VERSION\n",
        )

    def test_the_packet_contract_may_not_reach_the_core_store(self) -> None:
        self._refuses(
            "workstack/knowledge_capture_packets.py",
            "py_knowledge_capture_packets",
            "from workstack.store import MAX_REVISION\n",
            "py_legacy_store",
        )

    def test_the_packet_contract_may_not_reach_the_identifier_primitives(self) -> None:
        """A pure contract mints nothing; only the orchestrator holds that grant."""

        self._refuses(
            "workstack/knowledge_capture_packets.py",
            "py_knowledge_capture_packets",
            "from workstack.service_domain import _next_id\n",
            "py_service_domain",
        )

    def test_the_orchestrator_reaches_its_contracts_and_the_identifier_layer(self) -> None:
        self._permits(
            "workstack/knowledge_capture_import.py",
            "from workstack.knowledge_capture_packets import PACKET\n"
            "from workstack.knowledge_ledger_document import LEDGER\n"
            "from workstack.knowledge_owner_requests import OWNER\n"
            "from workstack.knowledge_request import REQUEST_VERSION\n"
            "from workstack.service_domain import _next_id\n",
        )

    def test_the_orchestrator_may_not_reach_the_core_store(self) -> None:
        self._refuses(
            "workstack/knowledge_capture_import.py",
            "py_knowledge_capture_import",
            "from workstack.store import MAX_REVISION\n",
            "py_legacy_store",
        )

    def test_the_orchestrator_may_not_reach_the_task_application(self) -> None:
        """The identifier grant is not a back door into the application."""

        self._refuses(
            "workstack/knowledge_capture_import.py",
            "py_knowledge_capture_import",
            "from workstack.service_task_commands import run\n",
            "py_application",
        )

    def test_the_orchestrator_may_not_reach_storage_maintenance(self) -> None:
        self._refuses(
            "workstack/knowledge_capture_import.py",
            "py_knowledge_capture_import",
            "from workstack.maintenance import sweep\n",
            "py_storage",
        )

    def test_the_http_boundary_reaches_its_orchestrator_and_route_owner(self) -> None:
        self._permits(
            "workstack/knowledge_captures_http.py",
            "from workstack.capture_retrieval import RETRIEVAL_VERSION\n"
            "from workstack.knowledge_capture_import import IMPORTER\n"
            "from workstack.knowledge_capture_packets import PACKET\n"
            "from workstack.knowledge_ledger_document import LEDGER\n"
            "from workstack.knowledge_request import REQUEST_VERSION\n"
            "from workstack.knowledge_request_issuer import ISSUER\n"
            "from workstack.knowledge_requests_http import PREFIX\n"
            "from workstack.server_errors import RequestError\n",
        )

    def test_the_http_boundary_may_not_reach_the_task_application(self) -> None:
        self._refuses(
            "workstack/knowledge_captures_http.py",
            "py_knowledge_captures_http",
            "from workstack.service_task_commands import run\n",
            "py_application",
        )

    def test_the_http_boundary_may_not_reach_the_core_store(self) -> None:
        self._refuses(
            "workstack/knowledge_captures_http.py",
            "py_knowledge_captures_http",
            "from workstack.store import MAX_REVISION\n",
            "py_legacy_store",
        )

    def test_the_adapter_may_not_reach_the_packet_contract(self) -> None:
        """The external adapter keeps its two retrieval validators, no more."""

        self._refuses(
            "integrations/opendocuments/retrieval_mapper.py",
            "py_opendocuments_adapter",
            "from workstack.knowledge_capture_packets import PACKET\n",
            "py_knowledge_capture_packets",
        )

    def test_the_adapter_may_not_reach_the_identifier_primitives(self) -> None:
        self._refuses(
            "integrations/opendocuments/retrieval_mapper.py",
            "py_opendocuments_adapter",
            "from workstack.service_domain import _next_id\n",
            "py_service_domain",
        )

    def test_the_capture_rules_reach_the_packet_contract(self) -> None:
        self._permits(
            "workstack/service_capture_rules.py",
            "from workstack.knowledge_capture_packets import PACKET\n",
        )

    def test_the_application_may_not_reach_the_import_orchestrator(self) -> None:
        """The application consumes packets, never the HTTP-side orchestrator."""

        self._refuses(
            "workstack/service_capture_rules.py",
            "py_application",
            "from workstack.knowledge_capture_import import IMPORTER\n",
            "py_knowledge_capture_import",
        )

    def test_document_validation_reaches_the_packet_contract(self) -> None:
        self._permits(
            "workstack/store_document_validation.py",
            "from workstack.knowledge_capture_packets import PACKET\n"
            "from workstack.knowledge_ledger_document import LEDGER\n",
        )

    def test_document_validation_may_not_reach_the_import_orchestrator(self) -> None:
        self._refuses(
            "workstack/store_document_validation.py",
            "py_store_validation",
            "from workstack.knowledge_capture_import import IMPORTER\n",
            "py_knowledge_capture_import",
        )

    def test_transport_reaches_the_capture_import_surface(self) -> None:
        self._permits(
            "workstack/server_post_routes.py",
            "from workstack.knowledge_captures_http import PREFIX\n",
        )

    def test_transport_may_not_bypass_that_surface_to_the_orchestrator(self) -> None:
        self._refuses(
            "workstack/server_post_routes.py",
            "py_transport",
            "from workstack.knowledge_capture_import import IMPORTER\n",
            "py_knowledge_capture_import",
        )

    def test_transport_may_not_reach_the_packet_contract_directly(self) -> None:
        self._refuses(
            "workstack/server.py",
            "py_transport",
            "from workstack.knowledge_capture_packets import PACKET\n",
            "py_knowledge_capture_packets",
        )

    def test_the_identifier_layer_may_not_reach_the_import_orchestrator(self) -> None:
        """The carve-out points one way; the primitives stay unaware of it."""

        self._refuses(
            "workstack/service_domain.py",
            "py_service_domain",
            "from workstack.knowledge_capture_import import IMPORTER\n",
            "py_knowledge_capture_import",
        )

    def test_an_unregistered_core_module_still_fails_loudly(self) -> None:
        """This registration is not a blanket amnesty for new core files."""

        write(self.root, "workstack/knowledge_capture_unclaimed.py", "VALUE = 1\n")
        resolution = self.resolved()
        self.assertTrue(resolution.admitted("workstack/knowledge_capture_unclaimed.py"))
        self.assertIn("workstack/knowledge_capture_unclaimed.py", resolution.unclassified)


class DistGateHelperAdmissionTests(SyntheticTreeCase):
    """Preparation for the concurrent dist gate refactor, proven synthetically.

    Discovery roots must exist on disk - a declared root that is absent is a
    finding, so the helper files cannot be listed as roots before they land.
    The LAYER prefix carries no such requirement, so it is declared now: when
    at most two ``scripts/dist_source_gate_*.py`` helpers land and their roots
    are added, they are layered without a second configuration change, while
    an unrelated script under the same directory is still refused.
    """

    def setUp(self) -> None:
        super().setUp()
        # Model the pre-landing world explicitly, regardless of the real tree.
        # Missing-root refusals must survive the actual helpers' integration.
        for source_set in self.config["source_sets"]:
            if source_set["name"] == "quality_tooling":
                source_set["roots"] = [
                    path for path in source_set["roots"] if path not in DIST_GATE_HELPERS
                ]
        for path in DIST_GATE_HELPERS:
            (self.root / path).unlink(missing_ok=True)

    def _with_roots(self, *roots: str) -> dict[str, Any]:
        config = copy.deepcopy(self.config)
        for source_set in config["source_sets"]:
            if source_set["name"] == "quality_tooling":
                source_set["roots"] = list(source_set["roots"]) + list(roots)
        return config

    def test_the_base_gate_root_stays_mandatory(self) -> None:
        write(self.root, DIST_GATE_ROOT, "MANIFEST = 'manifest.json'\n")
        resolution = self.resolved()
        self.assertEqual("quality_tooling", resolution.owner.get(DIST_GATE_ROOT))
        self.assertEqual("py_quality_tooling", resolution.layers.get(DIST_GATE_ROOT))
        (self.root / DIST_GATE_ROOT).unlink()
        self.assertEqual([DIST_GATE_ROOT], resolve(self.root, self.config).missing_roots())

    def test_both_helpers_are_layered_by_the_prefix_once_their_roots_land(self) -> None:
        write(self.root, DIST_GATE_ROOT, "MANIFEST = 'manifest.json'\n")
        for helper in DIST_GATE_HELPERS:
            write(self.root, helper, "HELPER = 1\n")
        resolution = self.resolved(self._with_roots(*DIST_GATE_HELPERS))
        for helper in DIST_GATE_HELPERS:
            with self.subTest(helper=helper):
                self.assertEqual("quality_tooling", resolution.owner.get(helper))
                self.assertEqual("py_quality_tooling", resolution.layers.get(helper))
        self.assertEqual([], resolution.unclassified)
        self.assertEqual("py_quality_tooling", resolution.layers.get(DIST_GATE_ROOT))

    def test_an_unrelated_script_is_refused_by_the_prefix(self) -> None:
        """Admitting a root is not the same as claiming a layer for it."""

        write(self.root, DIST_GATE_ROOT, "MANIFEST = 'manifest.json'\n")
        write(self.root, UNRELATED_SCRIPT, "NOTES = 1\n")
        resolution = self.resolved(self._with_roots(UNRELATED_SCRIPT))
        self.assertTrue(resolution.admitted(UNRELATED_SCRIPT))
        self.assertIsNone(resolution.layers.get(UNRELATED_SCRIPT))
        self.assertIn(UNRELATED_SCRIPT, resolution.unclassified)

    def test_a_helper_root_declared_before_its_file_lands_is_a_finding(self) -> None:
        """Why the roots are added with the files, and the prefix in advance."""

        write(self.root, DIST_GATE_ROOT, "MANIFEST = 'manifest.json'\n")
        resolution = resolve(self.root, self._with_roots(*DIST_GATE_HELPERS))
        self.assertEqual(sorted(DIST_GATE_HELPERS), resolution.missing_roots())

    def test_a_helper_that_lands_without_its_root_stays_invisible(self) -> None:
        """The prefix classifies; it does not admit. Discovery still governs."""

        write(self.root, DIST_GATE_ROOT, "MANIFEST = 'manifest.json'\n")
        write(self.root, DIST_GATE_HELPERS[0], "HELPER = 1\n")
        resolution = self.resolved()
        self.assertFalse(resolution.admitted(DIST_GATE_HELPERS[0]))

    def test_a_dynamically_loaded_helper_is_still_admitted_and_classified(self) -> None:
        """Admission and classification, which is all the prefix claims.

        The lane loads the helpers through ``spec_from_file_location`` under its
        own module names, so no AST import edge exists between them. The gate
        therefore measures each file - length, complexity, unclassified status -
        but cannot enforce the receipt <- manifest <- facade direction, and this
        registration does not pretend otherwise. That the graph is silent here
        is asserted rather than assumed.
        """

        write(self.root, DIST_GATE_ROOT, DIST_GATE_DYNAMIC_LOAD)
        for helper in DIST_GATE_HELPERS:
            write(self.root, helper, DIST_GATE_DYNAMIC_LOAD)
        config = self._with_roots(*DIST_GATE_HELPERS)
        resolution = self.resolved(config)
        for path in (DIST_GATE_ROOT, *DIST_GATE_HELPERS):
            with self.subTest(path=path):
                self.assertEqual("quality_tooling", resolution.owner.get(path))
                self.assertEqual("py_quality_tooling", resolution.layers.get(path))
        self.assertEqual([], resolution.unclassified)

        python_files = sorted(path for path in resolution.owner if path.endswith(".py"))
        graph, _complexity, errors = _python_graph(self.root, python_files, [])
        self.assertEqual([], errors)
        for path in (DIST_GATE_ROOT, *DIST_GATE_HELPERS):
            with self.subTest(path=path):
                self.assertEqual(set(), graph[path], "a dynamic load became a graph edge")
        rules = config["python_layers"] + config["frontend_layers"]
        self.assertEqual(
            [], _layer_violations((graph,), resolution.layers, rules, set())
        )

    def test_the_prefix_does_not_double_claim_the_base_gate_file(self) -> None:
        """Two overlapping globs on one file would be a classification error."""

        write(self.root, DIST_GATE_ROOT, "MANIFEST = 'manifest.json'\n")
        resolution = self.resolved()
        self.assertEqual([], resolution.unclassified)
        self.assertEqual("py_quality_tooling", resolution.layers.get(DIST_GATE_ROOT))


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    unittest.main()
