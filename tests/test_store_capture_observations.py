"""Storing one historical capture-observation, and refusing to when it must not.

Every fixture here is a throwaway directory this test wrote itself through the
released ``Store``/``WorkStack`` APIs. Nothing reads a real home directory,
opens a live SSOT, installs anything or calls a provider: the only child this
suite would ever spawn is asserted never to happen.

The suite is written against the two questions this slice has to answer
separately: which captures container a collection may hold, and what the one
explicit write is allowed to do to it. Opening, reading and initializing a
store are proved to answer neither.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstack import maintenance, store_report_migration  # noqa: E402
from workstack.maintenance import BackupValidationError  # noqa: E402
from workstack.capture_observations import (  # noqa: E402
    CaptureObservationError,
    OBSERVATION_SCHEMA,
)
from workstack.knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME  # noqa: E402
from workstack.knowledge_verification_protocol import (  # noqa: E402
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
)
from workstack.service import WorkStack  # noqa: E402
from workstack.store import (  # noqa: E402
    JOURNAL_NAME,
    MIGRATION_BACKUP_DIR,
    Store,
    StoreCorruptError,
)
from workstack.store_document_validation import validate_document_values  # noqa: E402
from workstack.store_rosters import (  # noqa: E402
    REPORTS_DOCUMENT_NAME,
    V6_DOCUMENT_ORDER,
)
from workstack.storage.read_contract import RepositoryReadError  # noqa: E402
from workstack.storage.read_v3_snapshot import read_historical_v3  # noqa: E402

CONTRACTS = ROOT / "contracts"
CAPTURES = "captures.json"
ALIAS = "od-primary"
UPSTREAM = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
OTHER_WORKSPACE = "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77"
NAS_VERSION = "sha256-" + ("a" * 64)
DIGEST = "sha256:" + ("ab" * 32)
REQUESTED_AT = "2026-09-09T12:00:00Z"
EXPIRES_AT = "2026-09-09T12:01:00Z"
ACCEPTED_AT = "2026-09-09T12:00:10Z"
LATER_ACCEPTED_AT = "2026-09-09T12:00:20Z"


def _nonce(index: int) -> str:
    return "00000000-0000-4000-8000-{:012d}".format(index)


class _StoreObservationCase(unittest.TestCase):
    """A real v6 store holding one ingested Capture, in a temporary directory."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.store.initialize()
        self.stack = WorkStack(self.store)
        self.capture_id = self._ingest("capture-packet-v1.fixture.json", "obs.0001")
        self.workspace_uid = self.store.load("workspace.json")["id"]
        self.revision = self._capture_revision(self.capture_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- fixtures -------------------------------------------------------

    def _ingest(self, fixture: str, key: str) -> str:
        packet = json.loads((CONTRACTS / fixture).read_text(encoding="utf-8"))
        return str(self.stack.ingest_capture(packet, key)["body"]["data"]["id"])

    def _capture_revision(self, capture_id: str) -> int:
        for record in self.store.load(CAPTURES)["captures"]:
            if record["id"] == capture_id:
                return int(record["revision"])
        raise AssertionError("the ingested Capture is missing")

    def _observation(
        self,
        *,
        capture_id: str | None = None,
        revision: int | None = None,
        verification_id: str = _nonce(1),
        accepted_at: str = ACCEPTED_AT,
        workspace_uid: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = {
            "schema": REQUEST_SCHEMA,
            "verification_id": verification_id,
            "binding": {
                "workspace_uid": workspace_uid or self.workspace_uid,
                "capture_id": capture_id or self.capture_id,
                "capture_revision": self.revision if revision is None else revision,
            },
            "connection": {
                "alias": ALIAS,
                "upstream_workspace_uid": UPSTREAM,
                "policy_revision": 7,
            },
            "corpus_refs": ["engineering"],
            "evidence": [
                {
                    "document_ref": "docalpha0001",
                    "source_type": "nas.file",
                    "expected_source_version": NAS_VERSION,
                }
            ],
            "requested_at": REQUESTED_AT,
            "expires_at": EXPIRES_AT,
        }
        record: dict[str, Any] = {
            "schema": OBSERVATION_SCHEMA,
            "capture_digest": DIGEST,
            "verifier_alias": ALIAS,
            "accepted_at": accepted_at,
            "request": request,
            "result": {
                "schema": RESULT_SCHEMA,
                "verification_id": verification_id,
                "checked_at": accepted_at,
                "evidence": [
                    {
                        "document_ref": "docalpha0001",
                        "source_type": "nas.file",
                        "expected_source_version": NAS_VERSION,
                        "observed_source_version": NAS_VERSION,
                        "status": "current",
                        "code": "hash_matched",
                    }
                ],
            },
        }
        if extra:
            record.update(extra)
        return record

    # -- observations of the directory ----------------------------------

    def bodies(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in V6_DOCUMENT_ORDER}

    def archives(self) -> list[Path]:
        directory = self.root / MIGRATION_BACKUP_DIR
        return sorted(directory.iterdir()) if directory.is_dir() else []

    def journal_exists(self) -> bool:
        return (self.root / JOURNAL_NAME).exists()

    def captures_document(self) -> dict[str, Any]:
        return json.loads((self.root / CAPTURES).read_text(encoding="utf-8"))

    def assert_only_captures_changed(self, before: dict[str, bytes]) -> None:
        after = self.bodies()
        for name in V6_DOCUMENT_ORDER:
            if name == CAPTURES:
                continue
            self.assertEqual(after[name], before[name], name)

    def assert_nothing_written(self, before: dict[str, bytes]) -> None:
        self.assertEqual(self.bodies(), before)
        self.assertFalse(self.journal_exists())

    # -- legacy roster fixtures -----------------------------------------

    def v5_values(self) -> dict[str, dict[str, Any]]:
        values = {name: self.store.load(name) for name in V6_DOCUMENT_ORDER}
        values.pop(KNOWLEDGE_DOCUMENT_NAME)
        metadata = values["store-meta.json"]
        metadata["store_schema_version"] = 5
        metadata["migrations"].pop("knowledge")
        return values

    def v3_values(self) -> dict[str, dict[str, Any]]:
        values = self.v5_values()
        values.pop(REPORTS_DOCUMENT_NAME)
        metadata = values["store-meta.json"]
        metadata["store_schema_version"] = 3
        metadata["migrations"].pop("reports")
        return values


class ContainerAdmissionTests(_StoreObservationCase):
    """Which captures container each collection version may hold."""

    def _v6_values(self) -> dict[str, dict[str, Any]]:
        return {name: self.store.load(name) for name in V6_DOCUMENT_ORDER}

    def test_collection_six_admits_container_one_and_container_two(self) -> None:
        values = self._v6_values()
        self.assertEqual(values[CAPTURES]["version"], 1)
        self.assertEqual(
            validate_document_values(values, schema_version=6).workspace_uid,
            self.workspace_uid,
        )
        values[CAPTURES] = {
            "version": 2,
            "captures": values[CAPTURES]["captures"],
            "observations": [self._observation()],
        }
        readiness = validate_document_values(values, schema_version=6)
        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(readiness.workspace_uid, self.workspace_uid)

    def test_unknown_container_versions_and_fields_refuse(self) -> None:
        captures = self.store.load(CAPTURES)["captures"]
        for document in (
            {"version": 3, "captures": captures, "observations": []},
            {"version": 2.0, "captures": captures, "observations": []},
            {"version": "2", "captures": captures, "observations": []},
            {"version": 2, "captures": captures},
            {"version": 2, "captures": captures, "observations": [], "extra": 1},
            {"version": 2, "captures": {}, "observations": []},
            {"version": 1, "captures": captures, "observations": []},
        ):
            values = self._v6_values()
            values[CAPTURES] = document
            with self.assertRaises(StoreCorruptError):
                validate_document_values(values, schema_version=6)

    def test_a_malformed_stored_observation_is_store_corruption(self) -> None:
        values = self._v6_values()
        values[CAPTURES] = {
            "version": 2,
            "captures": values[CAPTURES]["captures"],
            "observations": [self._observation(workspace_uid=OTHER_WORKSPACE)],
        }
        with self.assertRaises(StoreCorruptError) as caught:
            validate_document_values(values, schema_version=6)
        self.assertIn("workspace_mismatch", str(caught.exception))

    def test_legacy_collection_shapes_keep_container_one_only(self) -> None:
        for version, values in ((5, self.v5_values()), (3, self.v3_values())):
            self.assertEqual(
                validate_document_values(
                    copy.deepcopy(values), schema_version=version
                ).schema_version,
                version,
            )
            values[CAPTURES] = {
                "version": 2,
                "captures": values[CAPTURES]["captures"],
                "observations": [],
            }
            with self.assertRaises(StoreCorruptError):
                validate_document_values(values, schema_version=version)


class DefaultPathsNeverMigrateTests(_StoreObservationCase):
    """Initializing, opening and reading leave container 1 exactly as it is."""

    def test_fresh_open_and_read_do_not_convert_or_back_up(self) -> None:
        before = self.bodies()
        self.assertEqual(self.captures_document()["version"], 1)
        reopened = Store(self.root)
        reopened.initialize()
        reopened.load(CAPTURES)
        with reopened.consistent_read() as readiness:
            self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(self.captures_document()["version"], 1)
        self.assertEqual(self.bodies(), before)
        self.assertEqual(self.archives(), [])
        self.assertFalse(self.journal_exists())


class ExplicitFirstWriteTests(_StoreObservationCase):
    """The single 1 -> 2 activation: its gate, its archive and its one commit."""

    def test_first_record_creates_container_two_with_a_verified_backup(self) -> None:
        before = self.bodies()
        captures_before = self.store.load(CAPTURES)["captures"]
        recorded = self.store.record_capture_observation(self._observation())
        self.assertEqual(recorded["request"]["binding"]["capture_id"], self.capture_id)

        document = self.captures_document()
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["captures"], captures_before)
        self.assertEqual(len(document["observations"]), 1)
        self.assertEqual(document["observations"][0], recorded)
        self.assert_only_captures_changed(before)
        self.assertFalse(self.journal_exists())

        archives = self.archives()
        self.assertEqual(len(archives), 1)
        verified = store_report_migration.verify_archive_file(archives[0])
        self.assertEqual(verified.store_schema_version, 6)
        self.assertEqual(verified.workspace_id, self.workspace_uid)
        self.assertEqual(verified.file_count, len(V6_DOCUMENT_ORDER))
        self.assertEqual(verified.bodies, before)

    def test_the_record_survives_reopening_the_store(self) -> None:
        recorded = self.store.record_capture_observation(self._observation())
        reopened = Store(self.root)
        readiness = reopened.initialize()
        self.assertEqual(readiness.schema_version, 6)
        document = reopened.load(CAPTURES)
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["observations"], [recorded])

    def test_an_identical_record_replay_writes_nothing(self) -> None:
        record = self._observation()
        first = self.store.record_capture_observation(record)
        after_first = self.bodies()
        archives = self.archives()

        replayed = self.store.record_capture_observation(copy.deepcopy(record))
        self.assertEqual(replayed, first)
        self.assertEqual(self.bodies(), after_first)
        self.assertEqual(self.archives(), archives)
        self.assertFalse(self.journal_exists())

    def test_a_second_capture_updates_without_another_format_backup(self) -> None:
        self.store.record_capture_observation(self._observation())
        archives = self.archives()
        other_id = self._ingest("capture-packet-v1.manual.fixture.json", "obs.0002")
        other = self._observation(
            capture_id=other_id,
            revision=self._capture_revision(other_id),
            verification_id=_nonce(2),
            accepted_at=LATER_ACCEPTED_AT,
        )
        self.store.record_capture_observation(other)

        document = self.captures_document()
        self.assertEqual(document["version"], 2)
        self.assertEqual(len(document["observations"]), 2)
        self.assertEqual(
            [item["request"]["binding"]["capture_id"] for item in document["observations"]],
            sorted({self.capture_id, other_id}),
        )
        self.assertEqual(self.archives(), archives)
        self.assertFalse(self.journal_exists())

    def test_the_stored_capture_rows_and_wire_projection_are_unchanged(self) -> None:
        listed = self.stack.list_captures("all")
        rows = self.store.load(CAPTURES)["captures"]
        self.store.record_capture_observation(self._observation())
        self.assertEqual(self.stack.list_captures("all"), listed)
        self.assertEqual(self.store.load(CAPTURES)["captures"], rows)
        for projected in self.stack.list_captures("all"):
            self.assertNotIn("observations", projected)


class BoundedRefusalTests(_StoreObservationCase):
    """A record this store cannot accept is refused before anything is written."""

    def refuse(self, code: str, record: dict[str, Any]) -> None:
        before = self.bodies()
        with self.assertRaises(CaptureObservationError) as caught:
            self.store.record_capture_observation(record)
        self.assertEqual(caught.exception.code, code)
        self.assert_nothing_written(before)
        self.assertEqual(self.archives(), [])

    def test_an_unknown_capture_is_a_bounded_semantic_refusal(self) -> None:
        self.refuse("unknown_capture", self._observation(capture_id="C-9999"))

    def test_a_stale_capture_revision_is_a_bounded_semantic_refusal(self) -> None:
        self.refuse(
            "capture_revision_mismatch", self._observation(revision=self.revision + 1)
        )

    def test_another_workspace_is_refused(self) -> None:
        self.refuse(
            "workspace_mismatch", self._observation(workspace_uid=OTHER_WORKSPACE)
        )

    def test_an_unknown_record_field_is_refused(self) -> None:
        self.refuse("unknown_field", self._observation(extra={"note": "x"}))

    def test_an_older_observation_for_the_same_capture_is_refused(self) -> None:
        self.store.record_capture_observation(
            self._observation(verification_id=_nonce(3), accepted_at=LATER_ACCEPTED_AT)
        )
        before = self.bodies()
        with self.assertRaises(CaptureObservationError) as caught:
            self.store.record_capture_observation(
                self._observation(verification_id=_nonce(4), accepted_at=ACCEPTED_AT)
            )
        self.assertEqual(caught.exception.code, "stale_observation")
        self.assert_nothing_written(before)


class PreJournalRefusalTests(_StoreObservationCase):
    """A refusal before the journal exists leaves every authoritative byte."""

    def test_a_backup_that_does_not_verify_stops_before_the_journal(self) -> None:
        before = self.bodies()
        with mock.patch.object(
            store_report_migration,
            "verify_archive_file",
            side_effect=ValueError("archive is unreadable"),
        ):
            with self.assertRaises(StoreCorruptError) as caught:
                self.store.record_capture_observation(self._observation())
        self.assertIn("did not verify", str(caught.exception))
        self.assert_nothing_written(before)
        self.assertEqual(self.captures_document()["version"], 1)

    def test_a_rebind_failure_stops_before_the_journal(self) -> None:
        before = self.bodies()
        with mock.patch.object(
            Store,
            "_rebind_prebackup_locked",
            side_effect=StoreCorruptError("migration rollback backup did not verify"),
        ):
            with self.assertRaises(StoreCorruptError):
                self.store.record_capture_observation(self._observation())
        self.assert_nothing_written(before)
        self.assertEqual(self.captures_document()["version"], 1)
        # A verified archive may exist: the refusal is about depending on it,
        # not about having written it. It is not an authoritative document.
        for archive in self.archives():
            self.assertEqual(
                store_report_migration.verify_archive_file(archive).bodies, before
            )

    def test_a_drifted_generation_refuses_before_the_backup(self) -> None:
        before = self.bodies()
        with mock.patch.object(
            Store,
            "_assert_upgrade_source_owned_locked",
            side_effect=StoreCorruptError(
                "upgrade source changed outside Work Stack; "
                "store left at its detected version"
            ),
        ):
            with self.assertRaises(StoreCorruptError):
                self.store.record_capture_observation(self._observation())
        self.assert_nothing_written(before)
        self.assertEqual(self.archives(), [])


class PostJournalRecoveryTests(_StoreObservationCase):
    """Once the journal exists the write may complete on the next open."""

    def test_an_interrupted_commit_recovers_the_intended_container_two(self) -> None:
        before = self.bodies()
        record = self._observation()
        original = Store._atomic_write_locked

        def fault(store: Store, path: Path, value: Any) -> None:
            if path.name == CAPTURES:
                raise OSError("simulated interruption after the journal")
            original(store, path, value)

        with mock.patch.object(Store, "_atomic_write_locked", fault):
            with self.assertRaises(OSError):
                self.store.record_capture_observation(record)
        self.assertTrue(self.journal_exists())
        self.assertEqual(self.captures_document()["version"], 1)
        self.assertEqual(self.bodies(), before)

        with mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("no provider call")
        ):
            with mock.patch.object(
                subprocess, "run", side_effect=AssertionError("no provider call")
            ):
                reopened = Store(self.root)
                readiness = reopened.initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertFalse(self.journal_exists())
        document = reopened.load(CAPTURES)
        self.assertEqual(document["version"], 2)
        self.assertEqual(len(document["observations"]), 1)
        self.assertEqual(
            document["observations"][0]["request"]["verification_id"],
            record["request"]["verification_id"],
        )
        self.assertEqual(document["captures"], json.loads(before[CAPTURES])["captures"])
        self.assert_only_captures_changed(before)


class ArchiveCompatibilityTests(_StoreObservationCase):
    """Both containers archive, verify and restore through the released tools."""

    def test_a_container_two_store_backs_up_verifies_and_restores(self) -> None:
        self.store.record_capture_observation(self._observation())
        with tempfile.TemporaryDirectory() as output:
            artifact = maintenance.backup_store(self.root, output)
            self.assertEqual(
                maintenance.verify_backup(artifact.path).digest, artifact.digest
            )
            with tempfile.TemporaryDirectory() as destination:
                receipt = maintenance.restore_store(artifact.path, destination)
                self.assertEqual(receipt.workspace_id, self.workspace_uid)
                restored = json.loads(
                    (Path(destination) / CAPTURES).read_text(encoding="utf-8")
                )
        self.assertEqual(restored, self.captures_document())
        self.assertEqual(restored["version"], 2)
        self.assertEqual(len(restored["observations"]), 1)

    def test_the_rollback_archive_holds_the_exact_original_container_one(self) -> None:
        before = self.bodies()
        self.store.record_capture_observation(self._observation())
        (archive,) = self.archives()
        verified = store_report_migration.verify_archive_file(archive)
        self.assertEqual(verified.bodies[CAPTURES], before[CAPTURES])
        self.assertEqual(verified.values[CAPTURES]["version"], 1)
        with tempfile.TemporaryDirectory() as destination:
            maintenance.restore_store(archive, destination)
            rolled_back = json.loads(
                (Path(destination) / CAPTURES).read_text(encoding="utf-8")
            )
        self.assertEqual(rolled_back["version"], 1)
        self.assertNotIn("observations", rolled_back)


class CanonicalContainerOrderTests(_StoreObservationCase):
    """A stored container 2 must already BE the canonical observation list.

    ``capture_observations.validate_observations`` is deliberately a
    normalizing validator: it admits a valid but out-of-order list and returns
    it sorted by Capture id. Admitting the unsorted input as stored authority
    would leave the very next identical record replay planning the sorted list,
    differing from the admitted document, and publishing a journaled write --
    breaking the explicit no-op guarantee. Container 2 has never been released,
    so requiring the canonical order at admission cannot refuse a store that
    exists; a noncanonical one is refused before it can become readable
    authority, and nothing is repaired on the way through.

    Every fixture below is this suite's own store, holding two real
    observations for two distinct ingested Captures, whose stored order is
    then reversed.
    """

    def setUp(self) -> None:
        super().setUp()
        other_id = self._ingest("capture-packet-v1.manual.fixture.json", "obs.0002")
        self.records = [
            self._observation(),
            self._observation(
                capture_id=other_id,
                revision=self._capture_revision(other_id),
                verification_id=_nonce(2),
                accepted_at=LATER_ACCEPTED_AT,
            ),
        ]
        for record in self.records:
            self.store.record_capture_observation(record)
        self.canonical = self.captures_document()
        self.assertEqual(self.canonical["version"], 2)
        self.assertEqual(len(self.canonical["observations"]), 2)

    # -- fixtures -------------------------------------------------------

    def descending(self) -> dict[str, Any]:
        """The same admitted container 2, its two rows in descending id order."""

        document = copy.deepcopy(self.canonical)
        document["observations"] = list(reversed(document["observations"]))
        identifiers = [
            item["request"]["binding"]["capture_id"]
            for item in document["observations"]
        ]
        self.assertEqual(identifiers, sorted(identifiers, reverse=True))
        self.assertNotEqual(identifiers[0], identifiers[1])
        self.assertNotEqual(document, self.canonical)
        return document

    def values_holding(self, document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        values = {name: self.store.load(name) for name in V6_DOCUMENT_ORDER}
        values[CAPTURES] = document
        return values

    def archive_holding(self, document: dict[str, Any], directory: Path) -> Path:
        """Pack one archive of this store whose captures.json is ``document``.

        The bytes are packed through the released ``pack_backup_archive``
        helper, which writes exactly the bodies it is handed. No production
        backup, verification or ownership gate is patched or weakened: the
        fixture only stands in for an archive whose captures document was
        never canonical.
        """

        bodies = self.bodies()
        bodies[CAPTURES] = json.dumps(document, sort_keys=True).encode("utf-8")
        packed = store_report_migration.pack_backup_archive(
            bodies,
            workspace_id=self.workspace_uid,
            store_schema_version=6,
            created=dt.datetime(2026, 9, 9, 12, 0, 30, tzinfo=dt.timezone.utc),
        )
        path = directory / str(packed["filename"])
        path.write_bytes(packed["body"])
        return path

    # -- admission ------------------------------------------------------

    def test_the_canonical_container_two_is_admitted(self) -> None:
        readiness = validate_document_values(
            self.values_holding(copy.deepcopy(self.canonical)), schema_version=6
        )
        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(readiness.workspace_uid, self.workspace_uid)

    def test_a_descending_stored_order_is_refused_as_noncanonical(self) -> None:
        values = self.values_holding(self.descending())
        held = copy.deepcopy(values[CAPTURES])
        with self.assertRaises(StoreCorruptError) as caught:
            validate_document_values(values, schema_version=6)
        self.assertIn("not canonical", str(caught.exception))
        self.assertEqual(values[CAPTURES], held)

    def test_a_noncanonical_store_refuses_to_open_and_repairs_nothing(self) -> None:
        (self.root / CAPTURES).write_text(
            json.dumps(self.descending(), sort_keys=True), encoding="utf-8"
        )
        before = self.bodies()
        archives = self.archives()
        for _ in range(2):
            with self.assertRaises(StoreCorruptError) as caught:
                Store(self.root).initialize()
            self.assertIn("not canonical", str(caught.exception))
        self.assertEqual(self.bodies(), before)
        self.assertEqual(self.archives(), archives)
        self.assertFalse(self.journal_exists())

    # -- the supported archive path -------------------------------------

    def test_a_noncanonical_archive_fails_supported_verification(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            artifact = self.archive_holding(self.descending(), Path(output))
            image = artifact.read_bytes()
            with self.assertRaises(store_report_migration.BackupPackError):
                store_report_migration.verify_archive_file(artifact)
            with self.assertRaises(BackupValidationError):
                maintenance.verify_backup(artifact)
            with tempfile.TemporaryDirectory() as destination:
                with self.assertRaises(BackupValidationError):
                    maintenance.restore_store(artifact, destination)
                self.assertEqual(sorted(Path(destination).iterdir()), [])
            self.assertEqual(artifact.read_bytes(), image)

    def test_the_canonical_archive_still_verifies_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            packed = self.archive_holding(copy.deepcopy(self.canonical), Path(output))
            verified = store_report_migration.verify_archive_file(packed)
            self.assertEqual(verified.store_schema_version, 6)
            self.assertEqual(verified.values[CAPTURES], self.canonical)
            self.assertEqual(maintenance.verify_backup(packed).digest, verified.digest)
            with tempfile.TemporaryDirectory() as destination:
                receipt = maintenance.restore_store(packed, destination)
                self.assertEqual(receipt.workspace_id, self.workspace_uid)
                restored = json.loads(
                    (Path(destination) / CAPTURES).read_text(encoding="utf-8")
                )
        self.assertEqual(restored, self.canonical)

    # -- the replay guarantee the admission rule protects ----------------

    def test_replaying_either_retained_record_writes_nothing(self) -> None:
        before = self.bodies()
        archives = self.archives()
        for record in self.records:
            replayed = self.store.record_capture_observation(copy.deepcopy(record))
            self.assertIn(replayed, self.canonical["observations"])
            self.assert_nothing_written(before)
            self.assertEqual(self.archives(), archives)
        self.assertEqual(self.captures_document(), self.canonical)


class SemanticRebuilderReachabilityTests(_StoreObservationCase):
    """The v3 container-1 rebuilder cannot be reached from a v6 collection.

    ``WorkspaceSnapshot.to_v3_documents`` writes ``captures.json`` as container
    1 unconditionally, so the contract asks whether a supported schema 6 path
    can reach it. It cannot: the only collection reader that feeds it,
    ``read_historical_v3``, refuses a directory carrying any document a roster
    newer than v3 added, before a single byte is decoded or rebuilt. Its other
    callers read a normalized v4 authority, which has no captures container at
    all. Nothing in ``storage/semantic.py`` is changed for a spelling match.
    """

    def test_a_v6_source_is_refused_before_any_v3_rebuild(self) -> None:
        for _ in range(2):
            with self.assertRaises(RepositoryReadError) as caught:
                read_historical_v3(Store(self.root))
            self.assertEqual(caught.exception.code, "SOURCE_SCHEMA_NEWER_THAN_V3")
            self.store.record_capture_observation(self._observation())


if __name__ == "__main__":  # pragma: no cover - direct invocation helper
    unittest.main()
