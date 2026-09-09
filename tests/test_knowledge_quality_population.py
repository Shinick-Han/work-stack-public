"""Admission of the knowledge retrieval and OpenDocuments source population.

The structural gate only measures what its source sets discover, and only
enforces a dependency rule on what its layers claim. A production file no
source-set root reaches is invisible to every scan; a file a source set admits
but no layer claims is reported as unclassified. These tests drive the REAL
resolver - ``_discover`` and ``_classify_layers`` from ``scripts/quality_gate``
- rather than comparing configuration constants to themselves.

Two populations are distinguished throughout:

* the ACTUAL candidate scan of this checkout, in which every declared root and
  every registered file has now landed, and
* SYNTHETIC temporary trees, used to drive the failure families the integrated
  checkout no longer exhibits - a required root deleted, an unregistered core
  module, a caller reaching past its grant. A synthetic tree proves what the
  registration would refuse; it never stands in for the actual scan.

Every root named here exists in this checkout, so the actual scan reports no
missing production root at all. The refusal that omitting a root would green
the scan is therefore kept as a SYNTHETIC failure - see
``MissingRequiredRootTests`` - and not as a stale expectation of real absence.

The six inbound edges the R7 review found unregistered - the storage,
transport, validation, layout and schema-upgrade callers of the new knowledge
layers - are checked against the REAL import graph of this checkout, and each
grant is shown to be load-bearing by withdrawing it and observing the
violation return.
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

# Roots that were declared ahead of their lanes and have since landed here.
# Discovery must find each of them; a missing one is a configuration defect.
REQUIRED_ROOTS = (
    "integrations/opendocuments",
    "scripts/dist_source_gate.py",
)

# The OpenDocuments adapter modules integrated into this checkout.
OPENDOCUMENTS_MODULES = (
    "integrations/opendocuments/nas_paths.py",
    "integrations/opendocuments/od_client.py",
    "integrations/opendocuments/retrieval_mapper.py",
    "integrations/opendocuments/retrieval_mapper_fields.py",
    "integrations/opendocuments/source_access.py",
)

# The six inbound edges the R7 review found unregistered: an existing caller in
# this checkout importing one of the new knowledge layers. Each entry records
# why the edge matches that caller's actual responsibility.
INBOUND_EDGES = (
    (
        "workstack/maintenance.py",
        "py_storage",
        "workstack/store_knowledge_migration.py",
        "py_store_knowledge_migration",
        "storage maintenance plans the knowledge ledger upgrade exactly as it "
        "already plans the report upgrade through py_store_migration",
    ),
    (
        "workstack/server.py",
        "py_transport",
        "workstack/knowledge_requests_http.py",
        "py_knowledge_requests_http",
        "the server composes the knowledge route mixin, a sibling HTTP surface "
        "carved out of transport so its own downstream reach stays narrow",
    ),
    (
        "workstack/server_post_routes.py",
        "py_transport",
        "workstack/knowledge_requests_http.py",
        "py_knowledge_requests_http",
        "the POST router reads the knowledge route's own body limit and path "
        "prefix instead of restating that surface's constants",
    ),
    (
        "workstack/store_document_validation.py",
        "py_store_validation",
        "workstack/knowledge_ledger_document.py",
        "py_knowledge_ledger_document",
        "document validation delegates the knowledge document's shape rule to "
        "the pure contract module that owns it",
    ),
    (
        "workstack/store_layout.py",
        "py_store_layout",
        "workstack/knowledge_ledger_document.py",
        "py_knowledge_ledger_document",
        "layout places the ledger file using the contract's own canonical name "
        "and default body, which only that module may define",
    ),
    (
        "workstack/store_schema_upgrade.py",
        "py_store_schema_upgrade",
        "workstack/store_knowledge_migration.py",
        "py_store_knowledge_migration",
        "the schema upgrade runs the knowledge migration planner beside the "
        "report migration planner it is already permitted",
    ),
)

LEDGER_CONTRACTS = (
    ("workstack/knowledge_request.py", "py_knowledge_request"),
    ("workstack/capture_retrieval.py", "py_capture_retrieval"),
    ("workstack/knowledge_ledger_document.py", "py_knowledge_ledger_document"),
    ("workstack/knowledge_owner_requests.py", "py_knowledge_owner_requests"),
    ("workstack/store_knowledge_migration.py", "py_store_knowledge_migration"),
)

ISSUER_MODULES = (
    ("workstack/knowledge_request_issuer.py", "py_knowledge_request_issuer"),
    ("workstack/knowledge_requests_http.py", "py_knowledge_requests_http"),
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
    """Create every declared root, so the synthetic tree is a future checkout.

    Roots are read from the configuration under test rather than restated, so
    the fixture keeps working when another lane declares a further root.
    """

    for source_set in config["source_sets"]:
        for name in source_set["roots"]:
            if PurePosixPath(str(name)).suffix:
                write(root, str(name))
            else:
                (root / str(name)).mkdir(parents=True, exist_ok=True)


class SyntheticTreeCase(unittest.TestCase):
    """A temporary checkout in which every declared root really exists."""

    def setUp(self) -> None:
        self.config = load_config(REPOSITORY_ROOT)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub_declared_roots(self.root, self.config)

    def resolved(self) -> Resolution:
        resolution = resolve(self.root, self.config)
        self.assertEqual([], resolution.missing_roots(), "the synthetic tree is incomplete")
        return resolution


class ActualCheckoutPopulationTests(unittest.TestCase):
    """The ACTUAL candidate scan of this checkout."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(REPOSITORY_ROOT)
        cls.actual = resolve(REPOSITORY_ROOT, cls.config)

    def test_every_required_root_has_landed_and_discovery_reaches_it(self) -> None:
        """The declared roots exist here, so no production source is hidden.

        Both roots were declared ahead of their lanes; both have since merged.
        Discovery must now find each of them on disk and report no missing
        production root at all.
        """

        for root in REQUIRED_ROOTS:
            with self.subTest(root=root):
                self.assertTrue(
                    (REPOSITORY_ROOT / root).exists(), f"{root} is declared but absent"
                )
        self.assertEqual([], self.actual.missing_roots())

    def test_the_actual_scan_reports_no_discovery_finding_of_any_kind(self) -> None:
        """Not merely no missing root: no discovery error for this candidate."""

        self.assertEqual([], self.actual.errors)

    def test_the_opendocuments_population_is_the_landed_adapter(self) -> None:
        """The adapter subtree is populated, and every module carries a layer.

        The population is compared against the ``.py`` files actually on disk
        under the subtree, so neither a later helper admitted invisibly nor one
        quietly excluded can pass here. The named modules are asserted present
        rather than exhaustive: a helper landing later joins through the subtree
        glob, and must then be layered like the rest.
        """

        on_disk = sorted(
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in (REPOSITORY_ROOT / "integrations" / "opendocuments").rglob("*.py")
        )
        population = self.actual.populations["python_opendocuments"]
        self.assertEqual(on_disk, population)
        for path in OPENDOCUMENTS_MODULES:
            self.assertIn(path, population)
        for path in population:
            with self.subTest(path=path):
                self.assertEqual("python_opendocuments", self.actual.owner.get(path))
                self.assertEqual("py_opendocuments_adapter", self.actual.layers.get(path))

    def test_the_landed_ledger_issuer_and_gate_carry_their_declared_layers(self) -> None:
        """Representative admitted modules of every lane, in the real scan."""

        expected = dict(LEDGER_CONTRACTS)
        expected.update(dict(ISSUER_MODULES))
        expected["scripts/dist_source_gate.py"] = "py_quality_tooling"
        for path, layer in sorted(expected.items()):
            with self.subTest(path=path):
                self.assertTrue(self.actual.admitted(path), f"{path} escaped every source set")
                self.assertEqual(layer, self.actual.layers.get(path))
                self.assertNotIn(path, self.actual.unclassified)

    def test_retrieval_contracts_are_governed_production_source(self) -> None:
        for path in ("workstack/knowledge_request.py", "workstack/capture_retrieval.py"):
            with self.subTest(path=path):
                self.assertTrue(self.actual.admitted(path), f"{path} escaped every source set")
                self.assertEqual("python_core", self.actual.owner[path])

    def test_retrieval_contracts_carry_their_declared_layer(self) -> None:
        self.assertEqual(
            "py_knowledge_request", self.actual.layers.get("workstack/knowledge_request.py")
        )
        self.assertEqual(
            "py_capture_retrieval", self.actual.layers.get("workstack/capture_retrieval.py")
        )

    def test_retrieval_contracts_no_longer_report_as_unclassified(self) -> None:
        for path in ("workstack/knowledge_request.py", "workstack/capture_retrieval.py"):
            self.assertNotIn(path, self.actual.unclassified)

    def test_an_unrelated_integration_tree_was_not_swept_in(self) -> None:
        """Only the OpenDocuments subtree is governed, not all of integrations/."""

        for path in (
            "integrations/microsoft-web-capture/bridge.js",
            "integrations/microsoft-web-capture/source.css",
            "integrations/agent-skill/work-stack/SKILL.md",
        ):
            with self.subTest(path=path):
                self.assertTrue((REPOSITORY_ROOT / path).is_file(), "fixture path went stale")
                self.assertFalse(self.actual.admitted(path))


class IncomingSourcePopulationTests(SyntheticTreeCase):
    """SYNTHETIC trees standing in for files still on their own lanes."""

    def _populate_adapter(self) -> None:
        write(self.root, "integrations/opendocuments/od_client.py", "TIMEOUT = 5\n")
        write(self.root, "integrations/opendocuments/nas_paths.py", "SEPARATOR = 1\n")
        write(
            self.root,
            "integrations/opendocuments/source_access.py",
            "from integrations.opendocuments.nas_paths import SEPARATOR\n",
        )
        write(self.root, "integrations/opendocuments/CLIENT.md", "# client\n")

    def test_representative_adapter_and_helper_are_admitted_and_layered(self) -> None:
        self._populate_adapter()
        resolution = self.resolved()
        for path in (
            "integrations/opendocuments/od_client.py",
            "integrations/opendocuments/nas_paths.py",
            "integrations/opendocuments/source_access.py",
        ):
            with self.subTest(path=path):
                self.assertEqual("python_opendocuments", resolution.owner.get(path))
                self.assertEqual("py_opendocuments_adapter", resolution.layers.get(path))
        self.assertEqual([], resolution.unclassified)

    def test_the_mappers_join_without_any_further_registration(self) -> None:
        """The mapper lane's modules, and a nested one, are already governed."""

        self._populate_adapter()
        write(self.root, "integrations/opendocuments/retrieval_mapper.py", "MAPPED = ()\n")
        write(self.root, "integrations/opendocuments/retrieval_mapper_fields.py", "FIELDS = ()\n")
        write(self.root, "integrations/opendocuments/normalise/chunks.py", "CHUNKS = ()\n")
        resolution = self.resolved()
        for path in (
            "integrations/opendocuments/retrieval_mapper.py",
            "integrations/opendocuments/retrieval_mapper_fields.py",
            "integrations/opendocuments/normalise/chunks.py",
        ):
            with self.subTest(path=path):
                self.assertEqual("python_opendocuments", resolution.owner.get(path))
                self.assertEqual("py_opendocuments_adapter", resolution.layers.get(path))

    def test_no_python_under_the_adapter_subtree_escapes_the_scan(self) -> None:
        self._populate_adapter()
        write(self.root, "integrations/opendocuments/retrieval_mapper.py", "MAPPED = ()\n")
        resolution = self.resolved()
        on_disk = sorted(
            path.relative_to(self.root).as_posix()
            for path in (self.root / "integrations" / "opendocuments").rglob("*.py")
        )
        self.assertEqual(on_disk, resolution.populations["python_opendocuments"])

    def test_adapter_documentation_does_not_join_the_production_population(self) -> None:
        self._populate_adapter()
        resolution = self.resolved()
        self.assertFalse(resolution.admitted("integrations/opendocuments/CLIENT.md"))

    def test_a_neighbouring_integration_tree_stays_outside_the_population(self) -> None:
        """Narrowing to the adapter subtree must not quietly widen again."""

        write(self.root, "integrations/microsoft-web-capture/helper.py", "VALUE = 1\n")
        resolution = self.resolved()
        self.assertFalse(resolution.admitted("integrations/microsoft-web-capture/helper.py"))

    def test_external_docs_and_fixture_mapping_do_not_silently_join(self) -> None:
        """Neither a docs page nor a contract fixture becomes production source.

        A ``.py`` under ``contracts/fixtures`` is deliberately included: a
        fixture mapping must not be admitted merely because it is Python.
        """

        write(self.root, "docs/KNOWLEDGE-QUALITY-POPULATION.md", "# population\n")
        write(self.root, "contracts/capture-retrieval-v1.1.md", "# contract\n")
        write(self.root, "contracts/fixtures/opendocuments-adapter/mapping.py", "CASES = ()\n")
        write(self.root, "contracts/fixtures/opendocuments-adapter/cases.json", "{}\n")
        resolution = self.resolved()
        for path in (
            "docs/KNOWLEDGE-QUALITY-POPULATION.md",
            "contracts/capture-retrieval-v1.1.md",
            "contracts/fixtures/opendocuments-adapter/mapping.py",
            "contracts/fixtures/opendocuments-adapter/cases.json",
        ):
            with self.subTest(path=path):
                self.assertFalse(resolution.admitted(path))

    def test_ledger_contract_files_are_admitted_and_layered(self) -> None:
        for path, _ in LEDGER_CONTRACTS:
            write(self.root, path, "VALUE = 1\n")
        resolution = self.resolved()
        for path, layer in LEDGER_CONTRACTS:
            with self.subTest(path=path):
                self.assertEqual("python_core", resolution.owner.get(path))
                self.assertEqual(layer, resolution.layers.get(path))

    def test_issuer_api_modules_are_admitted_and_layered(self) -> None:
        for path, _ in ISSUER_MODULES:
            write(self.root, path, "VALUE = 1\n")
        resolution = self.resolved()
        for path, layer in ISSUER_MODULES:
            with self.subTest(path=path):
                self.assertEqual("python_core", resolution.owner.get(path))
                self.assertEqual(layer, resolution.layers.get(path))

    def test_the_dist_source_gate_is_admitted_and_layered(self) -> None:
        write(self.root, "scripts/dist_source_gate.py", "MANIFEST = 'manifest.json'\n")
        resolution = self.resolved()
        self.assertEqual("quality_tooling", resolution.owner.get("scripts/dist_source_gate.py"))
        self.assertEqual(
            "py_quality_tooling", resolution.layers.get("scripts/dist_source_gate.py")
        )
        self.assertEqual([], resolution.unclassified)

    def test_an_unregistered_core_module_still_fails_loudly(self) -> None:
        """Registration must not become a blanket amnesty for new core files."""

        write(self.root, "workstack/unclaimed_module.py", "VALUE = 1\n")
        resolution = self.resolved()
        self.assertTrue(resolution.admitted("workstack/unclaimed_module.py"))
        self.assertIn("workstack/unclaimed_module.py", resolution.unclassified)


class DeclaredDependencyTests(SyntheticTreeCase):
    """The declared dependencies are enforced edges, not written-down intent."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "workstack/capture.py", "SHA256_RE = None\n")
        write(self.root, "workstack/service.py", "def run():\n    return 1\n")
        write(self.root, "workstack/store.py", "class Store:\n    pass\n")
        write(self.root, "workstack/server_errors.py", "class RequestError(Exception):\n    pass\n")
        write(self.root, "workstack/knowledge_request.py", "REQUEST_VERSION = 1\n")
        write(self.root, "workstack/capture_retrieval.py", "RETRIEVAL_VERSION = 1\n")
        write(self.root, "workstack/knowledge_ledger_document.py", "LEDGER = 1\n")
        write(self.root, "workstack/knowledge_owner_requests.py", "OWNER = 1\n")
        write(self.root, "workstack/knowledge_request_issuer.py", "ISSUER = 1\n")

    def _violations(self) -> list[str]:
        resolution = self.resolved()
        python_files = sorted(path for path in resolution.owner if path.endswith(".py"))
        graph, _, errors = _python_graph(self.root, python_files, [])
        self.assertEqual([], errors)
        rules = self.config["python_layers"] + self.config["frontend_layers"]
        return _layer_violations((graph,), resolution.layers, rules, set())

    def test_the_adapter_may_reuse_the_pure_retrieval_validators(self) -> None:
        write(
            self.root,
            "integrations/opendocuments/retrieval_mapper.py",
            "from workstack.capture_retrieval import RETRIEVAL_VERSION\n"
            "from workstack.knowledge_request import REQUEST_VERSION\n",
        )
        self.assertEqual([], self._violations())

    def test_the_adapter_may_not_reach_into_the_core_store(self) -> None:
        write(
            self.root,
            "integrations/opendocuments/retrieval_mapper.py",
            "from workstack.store import Store\n",
        )
        violations = self._violations()
        self.assertEqual(1, len(violations), violations)
        self.assertIn("integrations/opendocuments/retrieval_mapper.py", violations[0])
        self.assertIn("py_opendocuments_adapter", violations[0])
        self.assertIn("py_legacy_store", violations[0])

    def test_the_adapter_may_not_reach_the_application_service(self) -> None:
        write(
            self.root,
            "integrations/opendocuments/retrieval_mapper.py",
            "from workstack.service import run\n",
        )
        violations = self._violations()
        self.assertEqual(1, len(violations), violations)
        self.assertIn("py_application", violations[0])

    def test_the_issuer_reaches_only_the_knowledge_contracts(self) -> None:
        write(
            self.root,
            "workstack/knowledge_request_issuer.py",
            "from workstack.capture import SHA256_RE\n"
            "from workstack.knowledge_ledger_document import LEDGER\n"
            "from workstack.knowledge_owner_requests import OWNER\n"
            "from workstack.knowledge_request import REQUEST_VERSION\n",
        )
        self.assertEqual([], self._violations())

    def test_the_issuer_may_not_reach_the_application_service(self) -> None:
        write(
            self.root,
            "workstack/knowledge_request_issuer.py",
            "from workstack.service import run\n",
        )
        violations = self._violations()
        self.assertEqual(1, len(violations), violations)
        self.assertIn("py_knowledge_request_issuer", violations[0])
        self.assertIn("py_application", violations[0])

    def test_the_http_surface_reaches_its_transport_error_store_and_issuer(self) -> None:
        write(
            self.root,
            "workstack/knowledge_requests_http.py",
            "from workstack.knowledge_ledger_document import LEDGER\n"
            "from workstack.knowledge_request import REQUEST_VERSION\n"
            "from workstack.knowledge_request_issuer import ISSUER\n"
            "from workstack.server_errors import RequestError\n"
            "from workstack.store import Store\n",
        )
        self.assertEqual([], self._violations())

    def test_the_http_surface_may_not_reach_the_application_service(self) -> None:
        write(
            self.root,
            "workstack/knowledge_requests_http.py",
            "from workstack.service import run\n",
        )
        violations = self._violations()
        self.assertEqual(1, len(violations), violations)
        self.assertIn("py_knowledge_requests_http", violations[0])
        self.assertIn("py_application", violations[0])


class QualityConfigurationValidityTests(unittest.TestCase):
    """The gate's own parser accepts the file and the declarations cohere."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(REPOSITORY_ROOT)

    def test_the_gate_parser_accepts_the_configuration(self) -> None:
        self.assertEqual(1, self.config["schema_version"])

    def test_layer_and_source_set_names_are_unique(self) -> None:
        for key in ("python_layers", "frontend_layers"):
            names = [str(layer["name"]) for layer in self.config[key]]
            self.assertEqual(sorted(set(names)), sorted(names), key)
        set_names = [str(item["name"]) for item in self.config["source_sets"]]
        self.assertEqual(sorted(set(set_names)), sorted(set_names))

    def test_every_permitted_import_names_a_declared_layer(self) -> None:
        declared = {
            str(layer["name"])
            for key in ("python_layers", "frontend_layers")
            for layer in self.config[key]
        }
        for key in ("python_layers", "frontend_layers"):
            for layer in self.config[key]:
                for target in layer.get("may_import", []):
                    with self.subTest(layer=layer["name"], target=target):
                        self.assertIn(str(target), declared)

    def test_the_adapter_layer_grants_only_the_pure_retrieval_validators(self) -> None:
        adapter = next(
            layer
            for layer in self.config["python_layers"]
            if layer["name"] == "py_opendocuments_adapter"
        )
        self.assertEqual(
            ["py_capture_retrieval", "py_knowledge_request"],
            sorted(str(name) for name in adapter["may_import"]),
        )

    def test_the_opendocuments_population_hides_nothing(self) -> None:
        opendocuments = next(
            item for item in self.config["source_sets"] if item["name"] == "python_opendocuments"
        )
        self.assertEqual(["integrations/opendocuments"], opendocuments["roots"])
        self.assertEqual([], opendocuments["exclude_globs"])

    def test_no_architecture_exception_was_added_for_the_new_layers(self) -> None:
        """The new edges use may_import; none needed a boundary exception."""

        introduced = {
            "integrations/opendocuments",
            "workstack/knowledge_request.py",
            "workstack/capture_retrieval.py",
            "workstack/knowledge_ledger_document.py",
            "workstack/knowledge_owner_requests.py",
            "workstack/knowledge_request_issuer.py",
            "workstack/knowledge_requests_http.py",
            "workstack/store_knowledge_migration.py",
            "scripts/dist_source_gate.py",
        }
        for item in self.config["architecture_exceptions"]:
            for side in ("from", "to"):
                for prefix in introduced:
                    with self.subTest(exception=item[side], prefix=prefix):
                        self.assertFalse(str(item[side]).startswith(prefix))


class MissingRequiredRootTests(SyntheticTreeCase):
    """A genuinely missing required root is still a loud discovery failure.

    The integrated checkout has every root, so this refusal can no longer be
    observed there. It is driven synthetically instead of being dropped: a
    declared root that vanishes must be reported, never silently skipped.
    """

    def test_a_vanished_directory_root_is_reported_as_missing(self) -> None:
        removed = "integrations/opendocuments"
        (self.root / removed).rmdir()
        resolution = resolve(self.root, self.config)
        self.assertEqual([removed], resolution.missing_roots())

    def test_a_vanished_file_root_is_reported_as_missing(self) -> None:
        removed = "scripts/dist_source_gate.py"
        (self.root / removed).unlink()
        resolution = resolve(self.root, self.config)
        self.assertEqual([removed], resolution.missing_roots())

    def test_every_required_root_is_individually_load_bearing(self) -> None:
        """No required root is redundant with another set's coverage."""

        for root_name in REQUIRED_ROOTS:
            with self.subTest(root=root_name):
                target = self.root / root_name
                if target.is_dir():
                    target.rmdir()
                else:
                    target.unlink()
                resolution = resolve(self.root, self.config)
                self.assertIn(root_name, resolution.missing_roots())
                if root_name.endswith(".py"):
                    target.write_text("", encoding="utf-8")
                else:
                    target.mkdir(parents=True)


class ActualInboundEdgeTests(unittest.TestCase):
    """The six corrected caller edges, against the REAL graph of this checkout.

    ``DeclaredDependencyTests`` proves what the new layers may reach outward.
    These tests prove the opposite direction: the existing callers that import
    them here are permitted, that the grant for each is the thing making it
    permitted, and that nothing else was admitted along with them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(REPOSITORY_ROOT)
        cls.resolution = resolve(REPOSITORY_ROOT, cls.config)
        python_files = sorted(
            path for path in cls.resolution.owner if path.endswith(".py")
        )
        cls.graph, _complexity, cls.graph_errors = _python_graph(
            REPOSITORY_ROOT,
            python_files,
            list(cls.config.get("critical_python_globs", [])),
        )
        cls.rules = cls.config["python_layers"] + cls.config["frontend_layers"]

    def _violations(self, rules: "list[dict[str, Any]] | None" = None) -> list[str]:
        return sorted(
            set(
                _layer_violations(
                    (self.graph,),
                    self.resolution.layers,
                    self.rules if rules is None else rules,
                    set(),
                )
            )
        )

    def _rules_without(self, withdrawn: "set[tuple[str, str]]") -> "list[dict[str, Any]]":
        """The declared rules with named grants removed, to prove they carry."""

        rules = copy.deepcopy(self.rules)
        found = set()
        for rule in rules:
            for source_layer, target_layer in withdrawn:
                if rule["name"] == source_layer and target_layer in rule["may_import"]:
                    rule["may_import"].remove(target_layer)
                    found.add((source_layer, target_layer))
        self.assertEqual(withdrawn, found, "a grant under test is not declared")
        return rules

    def test_the_import_graph_of_this_checkout_parses(self) -> None:
        self.assertEqual([], self.graph_errors)

    def test_each_corrected_edge_is_a_real_import_carrying_its_layers(self) -> None:
        """Each edge is a current module-load import, not a paper edge."""

        for source, source_layer, target, target_layer, why in INBOUND_EDGES:
            with self.subTest(source=source, target=target):
                self.assertIn(target, self.graph.get(source, set()), why)
                self.assertEqual(source_layer, self.resolution.layers.get(source))
                self.assertEqual(target_layer, self.resolution.layers.get(target))

    def test_the_real_graph_reports_no_forbidden_layer_import(self) -> None:
        self.assertEqual([], self._violations())

    def test_withdrawing_one_grant_brings_back_exactly_its_own_edges(self) -> None:
        """Each grant is load-bearing, and reaches no further than its callers."""

        for source, source_layer, target, target_layer, _why in INBOUND_EDGES:
            with self.subTest(source=source, target=target):
                rules = self._rules_without({(source_layer, target_layer)})
                expected = sorted(
                    f"forbidden layer import: {other} ({other_layer})"
                    f" -> {other_target} ({other_target_layer})"
                    for other, other_layer, other_target, other_target_layer, _ in INBOUND_EDGES
                    if (other_layer, other_target_layer) == (source_layer, target_layer)
                )
                self.assertIn(
                    f"forbidden layer import: {source} ({source_layer})"
                    f" -> {target} ({target_layer})",
                    expected,
                )
                self.assertEqual(expected, self._violations(rules))

    def test_withdrawing_every_grant_reproduces_the_six_edges_and_no_others(self) -> None:
        """Nothing beyond the six reviewed edges was admitted by this change."""

        withdrawn = {
            (source_layer, target_layer)
            for _s, source_layer, _t, target_layer, _w in INBOUND_EDGES
        }
        expected = sorted(
            f"forbidden layer import: {source} ({source_layer})"
            f" -> {target} ({target_layer})"
            for source, source_layer, target, target_layer, _w in INBOUND_EDGES
        )
        self.assertEqual(6, len(expected))
        self.assertEqual(expected, self._violations(self._rules_without(withdrawn)))


class CorrectedCallerBoundaryTests(SyntheticTreeCase):
    """The corrected callers gained one neighbour each, not core-wide reach."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "workstack/capture.py", "SHA256_RE = None\n")
        write(self.root, "workstack/service.py", "def run():\n    return 1\n")
        write(self.root, "workstack/store.py", "class Store:\n    pass\n")
        write(self.root, "workstack/knowledge_request.py", "REQUEST_VERSION = 1\n")
        write(self.root, "workstack/knowledge_ledger_document.py", "LEDGER = 1\n")
        write(self.root, "workstack/knowledge_owner_requests.py", "OWNER = 1\n")
        write(self.root, "workstack/store_knowledge_migration.py", "MIGRATION = 1\n")
        write(self.root, "workstack/knowledge_request_issuer.py", "ISSUER = 1\n")
        write(self.root, "workstack/knowledge_requests_http.py", "PREFIX = '/k'\n")

    def _violations(self) -> list[str]:
        resolution = self.resolved()
        python_files = sorted(path for path in resolution.owner if path.endswith(".py"))
        graph, _complexity, errors = _python_graph(self.root, python_files, [])
        self.assertEqual([], errors)
        rules = self.config["python_layers"] + self.config["frontend_layers"]
        return _layer_violations((graph,), resolution.layers, rules, set())

    def _refuses(self, caller: str, caller_layer: str, body: str, target_layer: str) -> None:
        write(self.root, caller, body)
        violations = self._violations()
        self.assertEqual(1, len(violations), violations)
        self.assertIn(caller, violations[0])
        self.assertIn(caller_layer, violations[0])
        self.assertIn(target_layer, violations[0])

    def test_validation_reaches_the_ledger_contract_and_nothing_further(self) -> None:
        write(
            self.root,
            "workstack/store_document_validation.py",
            "from workstack.knowledge_ledger_document import LEDGER\n",
        )
        self.assertEqual([], self._violations())

    def test_validation_may_not_reach_the_request_issuer(self) -> None:
        self._refuses(
            "workstack/store_document_validation.py",
            "py_store_validation",
            "from workstack.knowledge_request_issuer import ISSUER\n",
            "py_knowledge_request_issuer",
        )

    def test_layout_reaches_the_ledger_contract_and_nothing_further(self) -> None:
        write(
            self.root,
            "workstack/store_layout.py",
            "from workstack.knowledge_ledger_document import LEDGER\n",
        )
        self.assertEqual([], self._violations())

    def test_layout_may_not_reach_the_knowledge_migration_planner(self) -> None:
        self._refuses(
            "workstack/store_layout.py",
            "py_store_layout",
            "from workstack.store_knowledge_migration import MIGRATION\n",
            "py_store_knowledge_migration",
        )

    def test_storage_reaches_the_knowledge_migration_planner(self) -> None:
        write(
            self.root,
            "workstack/maintenance.py",
            "from workstack.store_knowledge_migration import MIGRATION\n",
        )
        self.assertEqual([], self._violations())

    def test_storage_may_not_reach_the_knowledge_http_surface(self) -> None:
        self._refuses(
            "workstack/maintenance.py",
            "py_storage",
            "from workstack.knowledge_requests_http import PREFIX\n",
            "py_knowledge_requests_http",
        )

    def test_the_schema_upgrade_reaches_the_knowledge_migration_planner(self) -> None:
        write(
            self.root,
            "workstack/store_schema_upgrade.py",
            "from workstack.store_knowledge_migration import MIGRATION\n",
        )
        self.assertEqual([], self._violations())

    def test_the_schema_upgrade_may_not_reach_the_ledger_contract_directly(self) -> None:
        self._refuses(
            "workstack/store_schema_upgrade.py",
            "py_store_schema_upgrade",
            "from workstack.knowledge_ledger_document import LEDGER\n",
            "py_knowledge_ledger_document",
        )

    def test_transport_reaches_the_knowledge_http_surface(self) -> None:
        write(
            self.root,
            "workstack/server.py",
            "from workstack.knowledge_requests_http import PREFIX\n",
        )
        self.assertEqual([], self._violations())

    def test_transport_may_not_bypass_that_surface_to_reach_the_issuer(self) -> None:
        self._refuses(
            "workstack/server_post_routes.py",
            "py_transport",
            "from workstack.knowledge_request_issuer import ISSUER\n",
            "py_knowledge_request_issuer",
        )

    def test_transport_may_not_reach_the_ledger_contract_directly(self) -> None:
        self._refuses(
            "workstack/server.py",
            "py_transport",
            "from workstack.knowledge_ledger_document import LEDGER\n",
            "py_knowledge_ledger_document",
        )


if __name__ == "__main__":
    unittest.main()
