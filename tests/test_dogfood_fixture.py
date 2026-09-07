from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from workstack.context_projection import project_context_items
from workstack.service import WorkStack
from workstack.storage.migration_conversion import V3ConversionError, convert_v3_documents
from workstack.storage.validation import validate_storage_path
from workstack.store import Store
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V3_DOCUMENT_NAMES,
    V3_DOCUMENT_ORDER,
    V5_DOCUMENT_NAMES,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "create_dogfood_fixture.py"
EXPECTED = json.loads(
    (ROOT / "tests" / "fixtures" / "dogfood" / "expected_semantic.json").read_text(
        encoding="utf-8"
    )
)


def _load_script():
    spec = importlib.util.spec_from_file_location("create_dogfood_fixture", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


dogfood = _load_script()


def _core_files(root: Path) -> dict[str, bytes]:
    """Every historical v3 document the fixture is supposed to consist of.

    The roster is the frozen v3 one, not this build's mutable ``DEFAULTS``: a
    later schema adding a document must not silently redefine what "the v3
    fixture" means, and a missing name has to show up as a missing key rather
    than be filtered away.
    """

    return {name: (root / name).read_bytes() for name in V3_DOCUMENT_ORDER}


def _tree(root: Path) -> dict[str, bytes | None]:
    """Disposable snapshot of a directory tree used by the negative tests."""

    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        snapshot[key] = path.read_bytes() if path.is_file() else None
    return snapshot


def _sole(values: list[str]) -> Any:
    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else unique


def _normalize_golden(expected: dict[str, Any]) -> dict[str, Any]:
    """Golden semantics keyed by human-readable titles, order-insensitive."""

    return {
        "objectives": sorted(
            (
                {
                    "objective": item["objective"],
                    "key_results": sorted(item["key_results"]),
                }
                for item in expected["objectives"]
            ),
            key=lambda item: item["objective"],
        ),
        "tasks": sorted(
            (
                {
                    "title": item["title"],
                    "status": item["status"],
                    "role": item["role"],
                    "depends_on": sorted(item["depends_on"]),
                }
                for item in expected["tasks"]
            ),
            key=lambda item: item["title"],
        ),
        "zero_linked_key_result": expected["zero_linked_key_result"],
        "linked_key_result": expected["linked_key_result"],
        "note_text": expected["note_text"],
        "checkpoint_task": expected["checkpoint_task"],
        "checkpoint_done": list(expected["checkpoint_done"]),
        "checkpoint_next": list(expected["checkpoint_next"]),
        "formats": dict(expected["formats"]),
    }


def _normalize_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Project a generated semantic inventory onto the golden's title space.

    Identifiers, ordering and storage shape are normalized away so the golden --
    not the generator's own constants -- decides every remaining value.  A KR is
    reported as zero-linked when no Task references it while a sibling KR under
    the same Objective is referenced.
    """

    task_titles = {task["id"]: task["title"] for task in inventory["tasks"]}
    key_result_titles: dict[tuple[str, str], str] = {}
    for objective in inventory["objectives"]:
        for key_result in objective["key_results"]:
            key_result_titles[(objective["id"], key_result["id"])] = key_result["text"]
    roles = {task_id: role for role, task_id in inventory["roles"].items()}
    referenced = {
        (ref["objective_id"], ref["key_result_id"])
        for task in inventory["tasks"]
        for ref in task.get("key_result_refs", [])
    }
    normalized: dict[str, Any] = {
        "objectives": sorted(
            (
                {
                    "objective": item["objective"],
                    "key_results": sorted(
                        child["text"] for child in item["key_results"]
                    ),
                }
                for item in inventory["objectives"]
            ),
            key=lambda item: item["objective"],
        ),
        "tasks": sorted(
            (
                {
                    "title": task["title"],
                    "status": task["status"],
                    "role": roles[task["id"]],
                    "depends_on": sorted(
                        task_titles[other] for other in task["dependencies"]
                    ),
                }
                for task in inventory["tasks"]
            ),
            key=lambda item: item["title"],
        ),
        "note_text": inventory["note"]["text"],
        "checkpoint_task": task_titles[inventory["checkpoint"]["task_id"]],
        "checkpoint_done": list(inventory["checkpoint"]["done"]),
        "checkpoint_next": list(inventory["checkpoint"]["next"]),
        "formats": dict(inventory["format_meanings"]),
    }
    if referenced:
        linked_objectives = {objective_id for objective_id, _ in referenced}
        normalized["linked_key_result"] = _sole(
            [key_result_titles[key] for key in referenced]
        )
        normalized["zero_linked_key_result"] = _sole(
            [
                title
                for key, title in key_result_titles.items()
                if key not in referenced and key[0] in linked_objectives
            ]
        )
    return normalized


def _v3_documents(root: Path) -> dict[str, Any]:
    """Decode the fixture in place, without a Store that would upgrade it."""

    return {
        name: json.loads((root / name).read_text(encoding="utf-8"))
        for name in V3_DOCUMENT_ORDER
    }


def _v4_records(destination: Path, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((destination / "records" / kind).rglob("*.json"))
    ]


def _v4_events(destination: Path, kind: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((destination / "streams" / kind).rglob("*.ndjson")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                events.append(json.loads(line))
    return sorted(events, key=lambda event: event["sequence"])


def _v4_inventory(destination: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Rebuild an inventory from the v4 tree on disk, keyed by display ids."""

    tasks = _v4_records(destination, "tasks")
    display_ids = {task["uid"]: task["display_id"] for task in tasks}
    status: dict[str, str] = {}
    for event in _v4_events(destination, "planning-status"):
        status[event["record_uid"]] = event["status"]
    checkpoint = next(
        event
        for event in _v4_events(destination, "worklog")
        if event["kind"] == "entry"
    )
    notes = _v4_records(destination, "notes")
    return {
        "objectives": [
            {
                "id": item["display_id"],
                "objective": item["title"],
                "key_results": [
                    {"id": child["uid"], "text": child["title"]}
                    for child in item["key_results"]
                ],
            }
            for item in _v4_records(destination, "objectives")
        ],
        "tasks": [
            {
                "id": task["display_id"],
                "title": task["title"],
                "status": status[task["uid"]],
                "dependencies": [
                    display_ids[other] for other in task["dependency_uids"]
                ],
                "key_result_refs": list(task.get("key_result_refs", [])),
            }
            for task in tasks
        ],
        "note": notes[0],
        "checkpoint": {
            "task_id": checkpoint["task_display_id"],
            "done": checkpoint["done"],
            "next": checkpoint["next"],
        },
        "roles": receipt["roles"],
        "format_meanings": receipt["format_meanings"],
    }


def _make_alias(link: Path, target: Path) -> None:
    """Create a real directory alias, or skip when the platform forbids one."""

    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        raise unittest.SkipTest("no directory alias available")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    if completed.returncode != 0 or not os.path.lexists(link):
        raise unittest.SkipTest("no directory alias available")


class DogfoodFixtureCreationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dogfood-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def _create(self, name: str, storage_format: str = "v3") -> tuple[Path, dict]:
        destination = self.base / name
        return destination, dogfood.create_fixture(destination, storage_format)

    def test_golden_declares_every_normalized_field(self) -> None:
        self.assertEqual(set(EXPECTED), set(_normalize_golden(EXPECTED)))

    def test_v3_inventory_on_disk_matches_the_complete_golden(self) -> None:
        destination, _ = self._create("v3")
        reloaded = dogfood.semantic_inventory(WorkStack(Store(destination)))
        self.assertEqual(_normalize_inventory(reloaded), _normalize_golden(EXPECTED))

    def test_v3_output_is_a_genuine_historical_v3_authority(self) -> None:
        """The fixture must *be* schema 3, not a current store wearing the label.

        Three separate facts are asserted because a label alone satisfies none
        of them: the directory holds exactly the frozen nine-document v3
        roster, the metadata record says schema 3 with only the evidence v3
        carried, and the released read-only validator admits the tree as
        format 3. The reports document schema 5 added is named explicitly so
        its absence is a checked fact rather than an unobserved one.
        """

        destination, receipt = self._create("v3")

        self.assertEqual(
            {path.name for path in destination.iterdir()}, set(V3_DOCUMENT_NAMES)
        )
        self.assertEqual(V5_DOCUMENT_NAMES - V3_DOCUMENT_NAMES, {REPORTS_DOCUMENT_NAME})
        self.assertFalse((destination / REPORTS_DOCUMENT_NAME).exists())

        metadata = _v3_documents(destination)["store-meta.json"]
        self.assertEqual(metadata["store_schema_version"], 3)
        self.assertEqual(set(metadata["migrations"]), {"identity", "planning_status"})
        self.assertEqual(receipt["store_schema_version"], 3)

        report = validate_storage_path(destination)
        self.assertTrue(report.valid, report.issues)
        self.assertEqual(report.format_version, 3)

    def test_v3_projection_links_status_and_context(self) -> None:
        destination, receipt = self._create("v3")
        # Judged before any Store opens the fixture: a released Store upgrades
        # a v3 authority it admits, so validating afterwards would be
        # validating the upgrade rather than what the generator wrote.
        report = validate_storage_path(destination)
        self.assertTrue(report.valid, report.issues)
        self.assertEqual(report.format_version, 3)
        stack = WorkStack(Store(destination))
        tasks = {task["title"]: task for task in stack.list_tasks(status="all")}
        objectives = {
            item["objective"]: item for item in stack.list_objectives(status="all")
        }

        blocked = next(item["title"] for item in EXPECTED["tasks"] if item["depends_on"])
        free = next(
            item["title"]
            for item in EXPECTED["tasks"]
            if item["role"] == "selected_without_prerequisites"
        )
        for golden in EXPECTED["tasks"]:
            title = golden["title"]
            self.assertEqual(tasks[title]["status"], golden["status"])
            self.assertEqual(
                tasks[title]["dependencies"],
                [tasks[name]["id"] for name in golden["depends_on"]],
            )

        owner = next(
            item
            for item in objectives.values()
            if any(
                child["text"] == EXPECTED["linked_key_result"]
                for child in item["key_results"]
            )
        )
        linked = next(
            child
            for child in owner["key_results"]
            if child["text"] == EXPECTED["linked_key_result"]
        )
        zero = next(
            child
            for child in owner["key_results"]
            if child["text"] == EXPECTED["zero_linked_key_result"]
        )
        refs = {
            (ref["objective_id"], ref["key_result_id"])
            for task in tasks.values()
            for ref in task.get("key_result_refs", [])
        }
        self.assertEqual(refs, {(owner["id"], linked["id"])})
        self.assertEqual(tasks[free].get("key_result_refs", []), [])
        self.assertNotIn(zero["id"], {ref[1] for ref in refs})

        notes = stack.store.load("notes.json")["notes"]
        context = project_context_items(
            notes,
            [],
            [task["id"] for task in tasks.values()],
            [item["id"] for item in objectives.values()],
        )
        self.assertEqual(len(context), 1)
        targets = {
            (item["target"]["kind"], item["target"]["id"])
            for item in context[0]["connections"]
        }
        self.assertEqual(
            targets, {("task", tasks[blocked]["id"]), ("objective", owner["id"])}
        )
        self.assertEqual(receipt["checkpoint"]["task_id"], tasks[free]["id"])

    def test_v3_is_reproducible_across_destinations(self) -> None:
        first, receipt_a = self._create("a")
        second, receipt_b = self._create("b")
        self.assertEqual(_core_files(first), _core_files(second))
        self.assertEqual(receipt_a["workspace_id"], receipt_b["workspace_id"])
        self.assertEqual(receipt_a["tasks"], receipt_b["tasks"])
        self.assertEqual(receipt_a["objectives"], receipt_b["objectives"])
        self.assertEqual(
            _normalize_inventory(receipt_a), _normalize_inventory(receipt_b)
        )

    def test_v3_is_reproducible_when_wall_clock_seconds_differ(self) -> None:
        """Byte equality must hold when destinations are created on different UTC seconds.

        Canonical 2711 failed with notice timestamps 13:20:56Z vs 13:20:57Z.
        Those stamps come from workstack.mutation_service._utc_now, which
        freeze_generation must pin. The outer patch injects the two seconds
        without sleep; the inner freeze must ignore them.
        """

        first, _ = self._create_with_wall_clock("clock-a", "2026-09-06T13:20:56Z")
        second, _ = self._create_with_wall_clock("clock-b", "2026-09-06T13:20:57Z")
        self.assertEqual(_core_files(first), _core_files(second))

    def _create_with_wall_clock(
        self, name: str, stamp: str
    ) -> tuple[Path, dict]:
        with mock.patch("workstack.mutation_service._utc_now", return_value=stamp):
            return self._create(name)

    def test_v4_inventory_on_disk_matches_the_golden_without_refs(self) -> None:
        destination, receipt = self._create("v4", "v4")
        report = validate_storage_path(destination)
        self.assertTrue(report.valid, report.issues)
        self.assertEqual(report.format_version, 4)
        expected = _normalize_golden(EXPECTED)
        expected.pop("linked_key_result")
        expected.pop("zero_linked_key_result")
        self.assertEqual(
            _normalize_inventory(_v4_inventory(destination, receipt)), expected
        )

    def test_declared_format_distinction_is_enforced(self) -> None:
        v3_destination, v3_receipt = self._create("v3")
        v4_destination, v4_receipt = self._create("v4", "v4")
        self.assertEqual(v3_receipt["format_meanings"], EXPECTED["formats"])
        self.assertEqual(v4_receipt["format_meanings"], EXPECTED["formats"])

        self.assertTrue(v3_receipt["key_result_refs_admitted"])
        self.assertTrue(
            any(task["key_result_refs"] for task in v3_receipt["tasks"]),
            "released v3 must persist the scoped refs the golden declares",
        )
        self.assertFalse(v4_receipt["key_result_refs_admitted"])
        self.assertEqual(v4_receipt["source_store_schema_version"], 3)
        self.assertFalse((v4_destination / "backlog.json").exists())
        records = _v4_records(v4_destination, "tasks")
        self.assertEqual(len(records), len(EXPECTED["tasks"]))
        for record in records:
            self.assertNotIn("key_result_refs", record)

        documents = _v3_documents(v3_destination)
        with self.assertRaises(V3ConversionError) as raised:
            convert_v3_documents(documents, candidate_created_at=dogfood.NOW)
        self.assertEqual(raised.exception.code, "SEMANTIC_PARITY_MISMATCH")

    def test_refuses_existing_nonempty_destination_without_writes(self) -> None:
        destination = self.base / "occupied"
        destination.mkdir()
        (destination / "stale.txt").write_text("keep", encoding="utf-8")
        before = _tree(destination)
        with self.assertRaises(dogfood.FixtureRefused) as raised:
            dogfood.create_fixture(destination, "v3")
        self.assertEqual(raised.exception.code, "existing_nonempty_destination")
        self.assertEqual(_tree(destination), before)
        self.assertFalse((destination / "workspace.json").exists())

    def test_refuses_known_live_ssot_paths(self) -> None:
        for live in dogfood.live_ssot_paths():
            with self.subTest(path=str(live)):
                with self.assertRaises(dogfood.FixtureRefused) as raised:
                    dogfood.create_fixture(live, "v3")
                self.assertEqual(raised.exception.code, "live_ssot_destination")

    def test_refuses_configured_live_root_and_descendants_without_writes(self) -> None:
        live = self.base / "configured-live"
        live.mkdir()
        (live / "keep.txt").write_bytes(b"unchanged")
        before = _tree(self.base)
        with mock.patch.dict(os.environ, {"WORK_STACK_HOME": str(live)}):
            with mock.patch.object(dogfood, "Store") as store:
                for destination in (live, live / "new-fixture"):
                    for storage_format in ("v3", "v4"):
                        with self.subTest(destination=destination, format=storage_format):
                            with self.assertRaises(dogfood.FixtureRefused) as raised:
                                dogfood.create_fixture(destination, storage_format)
                            self.assertEqual(raised.exception.code, "live_ssot_destination")
                store.assert_not_called()
        self.assertEqual(_tree(self.base), before)

    def test_refuses_symlink_or_reparse_destination(self) -> None:
        destination = self.base / "linked"
        destination.mkdir()
        with mock.patch.object(dogfood, "is_link_or_reparse", return_value=True):
            with self.assertRaises(dogfood.FixtureRefused) as raised:
                dogfood.create_fixture(destination, "v3")
        self.assertEqual(raised.exception.code, "symlink_or_reparse_destination")
        self.assertFalse((destination / "workspace.json").exists())

    def test_refuses_aliased_ancestor_outside_live_ssot_and_leaves_tree_unchanged(
        self,
    ) -> None:
        target = self.base / "real"
        (target / "inner").mkdir(parents=True)
        (target / "inner" / "keep.txt").write_text("keep", encoding="utf-8")
        alias = self.base / "alias"
        _make_alias(alias, target)
        self.assertFalse(dogfood.is_live_ssot(target))
        before = _tree(target)

        destination = alias / "inner" / "fixture"
        with self.assertRaises(dogfood.FixtureRefused) as raised:
            dogfood.create_fixture(destination, "v3")
        self.assertEqual(raised.exception.code, "symlink_or_reparse_ancestor")
        self.assertEqual(_tree(target), before)
        self.assertFalse(os.path.lexists(destination))
        self.assertFalse((target / "inner" / "fixture").exists())

    def test_refuses_relative_destination_and_leaves_tree_unchanged(self) -> None:
        working = self.base / "cwd"
        (working / "keep").mkdir(parents=True)
        (working / "keep" / "keep.txt").write_text("keep", encoding="utf-8")
        previous = Path.cwd()
        os.chdir(working)
        self.addCleanup(os.chdir, previous)
        before = _tree(working)
        with self.assertRaises(dogfood.FixtureRefused) as raised:
            dogfood.create_fixture(Path("relative") / "fixture", "v3")
        self.assertEqual(raised.exception.code, "relative_destination")
        self.assertEqual(_tree(working), before)

    def test_refuses_parent_traversal_destination_and_leaves_tree_unchanged(
        self,
    ) -> None:
        anchor = self.base / "anchor"
        anchor.mkdir()
        (anchor / "keep.txt").write_text("keep", encoding="utf-8")
        before = _tree(self.base)
        with self.assertRaises(dogfood.FixtureRefused) as raised:
            dogfood.create_fixture(anchor / ".." / "escaped", "v3")
        self.assertEqual(raised.exception.code, "non_literal_destination")
        self.assertEqual(_tree(self.base), before)

    def test_cli_requires_absolute_out_and_writes_receipt(self) -> None:
        missing = io.StringIO()
        with mock.patch.object(sys, "stderr", missing):
            with self.assertRaises(SystemExit) as raised:
                dogfood.main([])
        self.assertEqual(raised.exception.code, 2)

        relative = io.StringIO()
        with mock.patch.object(sys, "stderr", relative):
            code = dogfood.main(["--out", "relative-out", "--format", "v3"])
        self.assertEqual(code, 2)
        self.assertEqual(relative.getvalue().strip(), "relative_destination")

        destination = self.base / "cli"
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout):
            code = dogfood.main(["--out", str(destination), "--format", "v3"])
        self.assertEqual(code, 0)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["format"], "v3")
        self.assertEqual(receipt["store_schema_version"], 3)
        self.assertTrue(receipt["key_result_refs_admitted"])
        self.assertTrue((destination / "workspace.json").is_file())

    def test_generation_writes_nothing_outside_the_destination(self) -> None:
        home = self.base / "home"
        environment = {
            "USERPROFILE": str(home),
            "HOME": str(home),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "WORK_STACK_HOME": str(home / "custom-live"),
        }
        with mock.patch.dict(os.environ, environment):
            dogfood.create_fixture(self.base / "isolated", "v3")
            for live in dogfood.live_ssot_paths():
                self.assertFalse(os.path.lexists(live), str(live))
        self.assertFalse(home.exists())
        isolated = self.base / "isolated"
        self.assertTrue((isolated / "workspace.json").is_file())
        outside = [
            path.relative_to(self.base).as_posix()
            for path in self.base.rglob("*")
            if not path.is_relative_to(isolated)
        ]
        self.assertEqual(outside, [])


if __name__ == "__main__":
    unittest.main()
