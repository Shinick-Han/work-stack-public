"""The frozen per-version document rosters, and the historical readers of them.

These tests exist because ``DEFAULTS`` used to answer two different questions:
which documents this build writes, and which documents an older store had.
The rosters below are historical facts, so the assertions repeat every name by
hand instead of deriving one constant from another — a test that computed the
expected set the same way the module does would pass through any edit.

Nothing here activates schema 5. ``V5_DOCUMENT_NAMES`` is recorded, and the
suite asserts that ``reports.json`` is still unsupported everywhere.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import WorkStack
from workstack.store import (
    DEFAULTS,
    IDENTITY_STORES,
    STORE_SCHEMA_VERSION,
    Store,
    StoreCorruptError,
    _compact_json,
)
from workstack.store_document_validation import _validate_auxiliary_store
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V1_DOCUMENT_NAMES,
    V1_DOCUMENT_ORDER,
    V2_DOCUMENT_NAMES,
    V2_DOCUMENT_ORDER,
    V3_DOCUMENT_NAMES,
    V3_DOCUMENT_ORDER,
    V3_LEGACY_MARKER_NAMES,
    V3_SORTED_DOCUMENT_NAMES,
    V5_DOCUMENT_NAMES,
    auxiliary_store_defect,
)
from workstack.storage.document_repository import WorkspaceDocument
from workstack.storage.experimental_application import (
    ExperimentalV4ApplicationError,
    create_experimental_v4_application,
)
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.migration_source import V3_SOURCE_FILES
from workstack.storage.reader import read_v4
from workstack.storage.repository import _markers
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.validation import _detect_format, _v3_source_digests
from workstack.checkpoint_change import build_checkpoint_facts


NOW = "2026-09-01T12:00:00Z"
WORKSPACE_UID = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"

# Written out once, by hand, as the oracle for every roster assertion below.
EIGHT_V1_NAMES = {
    "workspace.json",
    "backlog.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
}
NINE_V3_NAMES = EIGHT_V1_NAMES | {"store-meta.json"}
TEN_V5_NAMES = NINE_V3_NAMES | {"reports.json"}


class FrozenRosterLiteralsTest(unittest.TestCase):
    """Each roster is the exact document set of its schema version."""

    def test_v1_roster_is_the_eight_documents_that_predate_metadata(self) -> None:
        self.assertEqual(
            V1_DOCUMENT_NAMES,
            frozenset(
                {
                    "workspace.json",
                    "backlog.json",
                    "okr.json",
                    "worklog.json",
                    "notes.json",
                    "captures.json",
                    "replies.json",
                    "activity.json",
                }
            ),
        )
        self.assertEqual(len(V1_DOCUMENT_NAMES), 8)
        self.assertNotIn("store-meta.json", V1_DOCUMENT_NAMES)

    def test_v2_roster_is_the_v1_eight_plus_the_metadata_document(self) -> None:
        self.assertEqual(
            V2_DOCUMENT_NAMES,
            frozenset(
                {
                    "workspace.json",
                    "backlog.json",
                    "store-meta.json",
                    "okr.json",
                    "worklog.json",
                    "notes.json",
                    "captures.json",
                    "replies.json",
                    "activity.json",
                }
            ),
        )
        self.assertEqual(len(V2_DOCUMENT_NAMES), 9)

    def test_v3_roster_repeats_the_v2_nine_because_v3_changed_payloads(self) -> None:
        self.assertEqual(
            V3_DOCUMENT_NAMES,
            frozenset(
                {
                    "workspace.json",
                    "backlog.json",
                    "store-meta.json",
                    "okr.json",
                    "worklog.json",
                    "notes.json",
                    "captures.json",
                    "replies.json",
                    "activity.json",
                }
            ),
        )
        self.assertEqual(V3_DOCUMENT_NAMES, V2_DOCUMENT_NAMES)

    def test_v5_roster_is_the_v3_nine_plus_reports_and_nothing_else(self) -> None:
        self.assertEqual(
            V5_DOCUMENT_NAMES,
            frozenset(
                {
                    "workspace.json",
                    "backlog.json",
                    "store-meta.json",
                    "okr.json",
                    "worklog.json",
                    "notes.json",
                    "captures.json",
                    "replies.json",
                    "activity.json",
                    "reports.json",
                }
            ),
        )
        self.assertEqual(len(V5_DOCUMENT_NAMES), 10)
        self.assertEqual(V5_DOCUMENT_NAMES - V3_DOCUMENT_NAMES, {REPORTS_DOCUMENT_NAME})

    def test_the_hand_written_oracle_agrees_with_the_module(self) -> None:
        self.assertEqual(V1_DOCUMENT_NAMES, EIGHT_V1_NAMES)
        self.assertEqual(V3_DOCUMENT_NAMES, NINE_V3_NAMES)
        self.assertEqual(V5_DOCUMENT_NAMES, TEN_V5_NAMES)

    def test_ordered_rosters_carry_the_same_names_as_their_sets(self) -> None:
        for order, names, label in (
            (V1_DOCUMENT_ORDER, V1_DOCUMENT_NAMES, "v1"),
            (V2_DOCUMENT_ORDER, V2_DOCUMENT_NAMES, "v2"),
            (V3_DOCUMENT_ORDER, V3_DOCUMENT_NAMES, "v3"),
            (V3_SORTED_DOCUMENT_NAMES, V3_DOCUMENT_NAMES, "v3 sorted"),
        ):
            with self.subTest(roster=label):
                self.assertEqual(frozenset(order), names)
                self.assertEqual(len(order), len(names), "no repeated name")

    def test_the_sorted_roster_is_the_order_callers_published_before(self) -> None:
        self.assertEqual(
            V3_SORTED_DOCUMENT_NAMES,
            (
                "activity.json",
                "backlog.json",
                "captures.json",
                "notes.json",
                "okr.json",
                "replies.json",
                "store-meta.json",
                "worklog.json",
                "workspace.json",
            ),
        )

    def test_legacy_marker_names_are_the_v3_roster_without_the_workspace(self) -> None:
        self.assertEqual(
            V3_LEGACY_MARKER_NAMES,
            frozenset(
                {
                    "backlog.json",
                    "store-meta.json",
                    "okr.json",
                    "worklog.json",
                    "notes.json",
                    "captures.json",
                    "replies.json",
                    "activity.json",
                }
            ),
        )
        self.assertEqual(len(V3_LEGACY_MARKER_NAMES), 8)
        self.assertNotIn("workspace.json", V3_LEGACY_MARKER_NAMES)
        self.assertNotIn(REPORTS_DOCUMENT_NAME, V3_LEGACY_MARKER_NAMES)

    def test_rosters_cannot_be_mutated_in_place(self) -> None:
        for value in (V1_DOCUMENT_NAMES, V2_DOCUMENT_NAMES, V3_DOCUMENT_NAMES, V5_DOCUMENT_NAMES):
            self.assertIsInstance(value, frozenset)
        for value in (V1_DOCUMENT_ORDER, V2_DOCUMENT_ORDER, V3_DOCUMENT_ORDER, V3_SORTED_DOCUMENT_NAMES):
            self.assertIsInstance(value, tuple)


class AuxiliaryDefaultShapeTest(unittest.TestCase):
    """The default-payload rule that moved beside the rosters it belongs to."""

    def _auxiliary_names(self) -> list[str]:
        return [name for name in V3_SORTED_DOCUMENT_NAMES if name not in IDENTITY_STORES]

    def test_every_shipped_default_payload_is_its_own_valid_document(self) -> None:
        names = self._auxiliary_names()
        self.assertEqual(names, ["captures.json", "notes.json", "okr.json", "replies.json", "worklog.json"])
        for name in names:
            with self.subTest(document=name):
                expected = DEFAULTS[name]
                self.assertIsNotNone(expected)
                self.assertIsNone(auxiliary_store_defect(name, dict(expected), expected))

    def test_the_three_released_messages_are_unchanged(self) -> None:
        cases = (
            ({"version": 1}, "notes.json schema is invalid"),
            ({"version": 2, "notes": []}, "notes.json schema is invalid"),
            ({"version": 1, "notes": {}}, "notes.json.notes must be an array"),
            ({"version": 1, "days": []}, "worklog.json.days must be an object"),
        )
        for value, message in cases:
            document = "worklog.json" if "days" in value else "notes.json"
            with self.subTest(value=value):
                self.assertEqual(
                    auxiliary_store_defect(document, value, DEFAULTS[document]), message
                )

    def test_the_store_still_raises_those_messages_itself(self) -> None:
        with self.assertRaises(StoreCorruptError) as caught:
            _validate_auxiliary_store("notes.json", {"version": 1, "notes": {}})
        self.assertEqual(str(caught.exception), "notes.json.notes must be an array")

        # An identity store is still a caller mistake, not a corrupt document.
        with self.assertRaises(ValueError) as refused:
            _validate_auxiliary_store("backlog.json", {"version": 3, "tasks": []})
        self.assertNotIsInstance(refused.exception, StoreCorruptError)


class CurrentBuildWritesV5Test(unittest.TestCase):
    """What this build writes now, stated against the frozen v5 roster.

    These four moved from three to five when schema 5 was activated. The
    historical sets above did not move with them, which is the whole point of
    keeping the two questions apart.
    """

    def test_defaults_are_exactly_the_frozen_v5_roster(self) -> None:
        self.assertEqual(frozenset(DEFAULTS), V5_DOCUMENT_NAMES)
        self.assertEqual(len(DEFAULTS), 10)

    def test_schema_version_is_five(self) -> None:
        self.assertEqual(STORE_SCHEMA_VERSION, 5)

    def test_reports_is_a_supported_document_of_this_build_only(self) -> None:
        self.assertIn(REPORTS_DOCUMENT_NAME, DEFAULTS)
        self.assertIn(REPORTS_DOCUMENT_NAME, V5_DOCUMENT_NAMES)
        self.assertNotIn(REPORTS_DOCUMENT_NAME, V3_DOCUMENT_NAMES)
        self.assertNotIn(REPORTS_DOCUMENT_NAME, V3_SOURCE_FILES)
        self.assertIn("REPORTS", {member.name for member in WorkspaceDocument})

    def test_a_fresh_store_writes_ten_files_including_empty_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Store(root).initialize()
            written = {item.name for item in root.iterdir() if item.suffix == ".json"}
            self.assertEqual(written, set(V5_DOCUMENT_NAMES))
            self.assertEqual(
                json.loads((root / REPORTS_DOCUMENT_NAME).read_text(encoding="utf-8")),
                {"version": 1, "reports": [], "idempotency": []},
            )


class HistoricalReaderRepointingTest(unittest.TestCase):
    """The readers that ask "what did an older store look like?"."""

    def test_migration_source_files_are_unchanged(self) -> None:
        # Deliberately not compared against DEFAULTS any more: the v3 source
        # roster is a historical fact and this build's roster has moved on.
        self.assertEqual(V3_SOURCE_FILES, V3_SORTED_DOCUMENT_NAMES)
        self.assertEqual(len(V3_SOURCE_FILES), 9)

    def test_legacy_markers_still_detect_a_v3_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # A marker that is not workspace.json is what both readers look for.
            (root / "okr.json").write_text("{}", encoding="utf-8")
            self.assertEqual(_detect_format(root), (3, []))
            self.assertEqual(_markers(root), (True, False))

    def test_the_workspace_document_alone_is_not_a_legacy_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workspace.json").write_text("{}", encoding="utf-8")
            self.assertFalse(
                any((root / name).exists() for name in V3_LEGACY_MARKER_NAMES)
            )
            # The released fallback still reports v3 from workspace.json alone.
            self.assertEqual(_detect_format(root), (3, []))

    def test_reports_alone_is_not_a_legacy_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / REPORTS_DOCUMENT_NAME).write_text("{}", encoding="utf-8")
            detected, issues = _detect_format(root)
            self.assertIsNone(detected)
            self.assertEqual([issue.code for issue in issues], ["FORMAT_NOT_DETECTED"])
            self.assertEqual(_markers(root), (False, False))

    def test_source_digests_cover_the_nine_v3_documents_in_the_same_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Store(root).initialize()
            digests = _v3_source_digests(root)
            self.assertEqual(tuple(digests), V3_SORTED_DOCUMENT_NAMES)
            for name in V3_SORTED_DOCUMENT_NAMES:
                self.assertEqual(
                    digests[name],
                    hashlib.sha256((root / name).read_bytes()).hexdigest(),
                )


class LegacyStoreMigrationTest(unittest.TestCase):
    """v1 and v2 fixtures must still migrate, and record the same digests."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _v1_values(self) -> dict[str, dict]:
        return {
            "workspace.json": {"version": 1, "id": WORKSPACE_UID, "name": "Legacy"},
            "backlog.json": {
                "version": 1,
                "tasks": [{"id": "T-0001", "title": "Migrated task", "status": "open"}],
            },
            "okr.json": {"version": 1, "objectives": []},
            "worklog.json": {"version": 1, "days": {}},
            "notes.json": {"version": 1, "notes": []},
            "captures.json": {"version": 1, "captures": []},
            "replies.json": {"version": 1, "replies": []},
            "activity.json": {"version": 1, "activity": [], "idempotency": []},
        }

    def _v2_values(self) -> dict[str, dict]:
        return {
            "workspace.json": {"version": 2, "id": WORKSPACE_UID, "name": "Legacy"},
            "backlog.json": {"version": 2, "tasks": []},
            "store-meta.json": {
                "version": 1,
                "store_schema_version": 2,
                "migration": {
                    "id": "workstack.store.v2",
                    "origin": "fresh",
                    "source_sha256": None,
                },
            },
            "okr.json": {"version": 1, "objectives": []},
            "worklog.json": {"version": 1, "days": {}},
            "notes.json": {"version": 1, "notes": []},
            "captures.json": {"version": 1, "captures": []},
            "replies.json": {"version": 1, "replies": []},
            "activity.json": {"version": 1, "activity": [], "idempotency": []},
        }

    def _materialize(self, values: dict[str, dict]) -> None:
        for name, value in values.items():
            self._write(self.root / name, value)

    def test_v1_source_digest_is_byte_identical_to_the_defaults_expression(self) -> None:
        values = self._v1_values()
        self._materialize(values)

        Store(self.root).initialize()
        recorded = json.loads((self.root / "store-meta.json").read_text(encoding="utf-8"))

        # The expression the migration used before the roster was frozen. It is
        # written against the v3 roster DEFAULTS held at the time, not against
        # today's DEFAULTS, because the point is that the recorded digest did
        # not move when this build's roster did.
        legacy_source = {
            name: values[name]
            for name in V3_DOCUMENT_ORDER
            if name != "store-meta.json"
        }
        frozen_source = {name: values[name] for name in V1_DOCUMENT_ORDER}
        self.assertEqual(_compact_json(legacy_source), _compact_json(frozen_source))

        expected = "sha256:" + hashlib.sha256(_compact_json(legacy_source)).hexdigest()
        self.assertEqual(recorded["migrations"]["identity"]["source_sha256"], expected)
        self.assertEqual(
            recorded["migrations"]["planning_status"]["source_sha256"], expected
        )

    def test_v1_fixture_still_migrates_with_the_same_meaning(self) -> None:
        self._materialize(self._v1_values())

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 5)
        self.assertEqual(readiness.workspace_uid, WORKSPACE_UID)
        self.assertEqual(readiness.task_count, 1)
        self.assertEqual(readiness.migration_origin, "migrated_v1")

        # The v1 semantics are unchanged; only the destination version moved.
        metadata = json.loads((self.root / "store-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["store_schema_version"], 5)
        self.assertEqual(
            metadata["migrations"]["identity"]["id"], "workstack.store.v1-to-v2"
        )
        self.assertEqual(metadata["migrations"]["reports"]["origin"], "migrated_v1")
        backlog = json.loads((self.root / "backlog.json").read_text(encoding="utf-8"))
        self.assertEqual(backlog["version"], 3)
        self.assertEqual(backlog["tasks"][0]["id"], "T-0001")
        activity = json.loads((self.root / "activity.json").read_text(encoding="utf-8"))
        self.assertEqual(activity["version"], 2)
        self.assertEqual(len(activity["planning_status"]), 1)
        self.assertEqual(
            json.loads((self.root / REPORTS_DOCUMENT_NAME).read_text(encoding="utf-8")),
            {"version": 1, "reports": [], "idempotency": []},
        )

    def test_v2_fixture_still_migrates_with_the_same_meaning(self) -> None:
        values = self._v2_values()
        self._materialize(values)

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 5)
        self.assertEqual(readiness.workspace_uid, WORKSPACE_UID)
        self.assertEqual(readiness.migration_origin, "fresh")

        metadata = json.loads((self.root / "store-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["store_schema_version"], 5)
        expected = "sha256:" + hashlib.sha256(_compact_json(dict(values))).hexdigest()
        self.assertEqual(
            metadata["migrations"]["planning_status"]["source_sha256"], expected
        )
        # The reports evidence digests the v2 documents that were detected, not
        # an intermediate v3 snapshot the upgrade derived on the way.
        self.assertEqual(metadata["migrations"]["reports"]["source_sha256"], expected)
        self.assertEqual(
            json.loads((self.root / REPORTS_DOCUMENT_NAME).read_text(encoding="utf-8")),
            {"version": 1, "reports": [], "idempotency": []},
        )

    def test_a_v2_fixture_with_a_broken_auxiliary_store_is_still_refused(self) -> None:
        values = self._v2_values()
        values["notes.json"] = {"version": 1, "notes": {}}
        self._materialize(values)

        with self.assertRaises(Exception) as caught:
            Store(self.root).initialize()
        self.assertIn("notes.json", str(caught.exception))


class V4AdapterKeepsTheV3ProjectionTest(unittest.TestCase):
    """The canary projects exactly nine documents and refuses reports."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        legacy = WorkStack(Store(self.base / "v3"))
        with mock.patch("workstack.service.utc_now", return_value=NOW), mock.patch(
            "workstack.service.today", return_value=NOW[:10]
        ):
            legacy.add_task("Roster canary task")
        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(documents, candidate_created_at=NOW)
        self.authority = self.base / "authority"
        self.authority.mkdir()
        self._write_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority,
            self.base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True)
        first = publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.authority), generation=0),
            expected_digest=None,
        )
        self.published_digest = first.manifest.digest
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_conversion(root: Path, conversion) -> None:
        def write(relative: str, body: bytes) -> None:
            path = root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

        write("store.json", canonical_json_bytes(dict(conversion.store)))
        write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
        for kind, records in conversion.records.items():
            for record in records:
                uid = str(record["uid"])
                write(f"records/{kind}/{uid[:2]}/{uid}.json", canonical_json_bytes(dict(record)))
        segments: dict[tuple[str, str], list[dict]] = {}
        for kind, events in conversion.streams.items():
            for event in events:
                segments.setdefault((kind, str(event["created_at"])[:7]), []).append(dict(event))
        for (kind, month), events in sorted(segments.items()):
            body = b"".join(
                canonical_json_bytes(event) + b"\n"
                for event in sorted(events, key=lambda item: item["sequence"])
            )
            write(f"streams/{kind}/{month}.ndjson", body)

    def _application(self):
        return create_experimental_v4_application(
            self.authority,
            self.runtime,
            enable_v4_application=True,
            checkpoint_facts=build_checkpoint_facts,
            clock=lambda: NOW,
            uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            today=lambda: NOW[:10],
            task_note_source_indexes=self.conversion.task_note_source_indexes,
        )

    def test_every_v3_document_still_loads_and_reports_short_circuits(self) -> None:
        store = self._application().store

        for name in V3_SORTED_DOCUMENT_NAMES:
            with self.subTest(document=name):
                self.assertIsInstance(store.load(name), dict)
                self.assertGreater(store.path(name).stat().st_size, 0)

        for accessor in (store.load, store.path):
            with self.subTest(accessor=accessor.__name__):
                with self.assertRaises(ExperimentalV4ApplicationError) as caught:
                    accessor(REPORTS_DOCUMENT_NAME)
                message = str(caught.exception)
                self.assertEqual(message, "V4_APPLICATION_DOCUMENT_UNKNOWN")
                # Content-free: the refusal names no document, path or workspace.
                self.assertNotIn("reports", message)
                self.assertNotIn(str(self.authority), message)

    def test_a_refresh_notice_lists_exactly_the_nine_v3_documents(self) -> None:
        store = self._application().store
        store.load("workspace.json")  # first refresh; no notice is emitted yet

        # A generation change is what publishes a notice. Republishing the same
        # authority at the next generation is the smallest way to cause one.
        published = build_v4_manifest(read_v4(self.authority), generation=1)
        publish_runtime_manifest(
            self.runtime.manifest_path,
            published,
            expected_digest=self.published_digest,
        )
        store.load("workspace.json")

        events = store.sync_events(0)["events"]
        notices = [item for item in events if item["type"] == "store-committed"]
        self.assertTrue(notices, "a generation change must publish a notice")
        for notice in notices:
            with self.subTest(notice=notice["id"]):
                self.assertEqual(notice["changed_files"], sorted(V3_DOCUMENT_NAMES))
                self.assertEqual(len(notice["changed_files"]), 9)
                self.assertNotIn(REPORTS_DOCUMENT_NAME, notice["changed_files"])


if __name__ == "__main__":
    unittest.main()
