"""Upgrading a collection store to schema 6, and refusing to when it must not.

Every fixture here is a directory this test wrote itself. Nothing reads a real
home directory, opens a live SSOT or migrates real data: a genuine v5 fixture
is built by initializing a throwaway v6 store and stepping its metadata and its
roster back one version, which is the only way to obtain a v5 authority from a
build that can no longer write one.

The suite is written against the two questions the upgrade has to answer
separately: what version is already on disk, and what do that version's
documents mean. Confusing the two is exactly what a widened roster does, so the
frozen historical sets are asserted here as well.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from workstack import store_knowledge_migration, store_report_migration
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DEFAULT,
    KNOWLEDGE_DOCUMENT_NAME,
)
from workstack.store import (
    DEFAULTS,
    JOURNAL_NAME,
    MIGRATION_BACKUP_DIR,
    STORE_SCHEMA_VERSION,
    Store,
    StoreCorruptError,
)
from workstack.store_document_validation import validate_document_values
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V3_DOCUMENT_NAMES,
    V5_DOCUMENT_NAMES,
    V6_DOCUMENT_NAMES,
)


class KnowledgeMigrationCase(unittest.TestCase):
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

    def materialize(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            self._write(self.root / name, value)

    def fresh_v6_values(self, *, with_task: bool = False) -> dict[str, Any]:
        """A real v6 authority, produced by this build in a throwaway directory."""

        with tempfile.TemporaryDirectory() as scratch:
            store = Store(Path(scratch))
            store.initialize()
            if with_task:
                from workstack.service import WorkStack

                WorkStack(store).add_task("Held task")
            return {
                name: json.loads((Path(scratch) / name).read_text(encoding="utf-8"))
                for name in V6_DOCUMENT_NAMES
            }

    def v5_values(self, *, with_task: bool = False) -> dict[str, Any]:
        """A genuine v5 authority: the v6 roster minus the ledger it added.

        The ten v5 payloads are already exactly what a v5 store held, and the
        metadata is the v6 record without its knowledge evidence and with the
        version it had.
        """

        values = self.fresh_v6_values(with_task=with_task)
        values.pop(KNOWLEDGE_DOCUMENT_NAME)
        metadata = values["store-meta.json"]
        metadata["store_schema_version"] = 5
        del metadata["migrations"]["knowledge"]
        return values

    def v3_values(self) -> dict[str, Any]:
        """A genuine v3 authority, stepped back from a fresh store the same way."""

        values = self.fresh_v6_values()
        values.pop(KNOWLEDGE_DOCUMENT_NAME)
        values.pop(REPORTS_DOCUMENT_NAME)
        metadata = values["store-meta.json"]
        metadata["store_schema_version"] = 3
        del metadata["migrations"]["knowledge"]
        del metadata["migrations"]["reports"]
        return values

    def documents(self) -> dict[str, Any]:
        return {
            name: json.loads((self.root / name).read_text(encoding="utf-8"))
            for name in V6_DOCUMENT_NAMES
        }

    def backups(self) -> list[Path]:
        directory = self.root / MIGRATION_BACKUP_DIR
        return sorted(directory.iterdir()) if directory.is_dir() else []

    def _fail_after(self, store: Store, written: int):  # type: ignore[no-untyped-def]
        """Let ``written`` documents land, then interrupt the replacement loop."""

        original = store._atomic_write_locked
        seen: list[str] = []

        def failing(path: Path, value: Any) -> None:
            if len(seen) >= written:
                raise OSError("injected migration interruption")
            seen.append(path.name)
            original(path, value)

        return failing


class FreshStoreTest(KnowledgeMigrationCase):
    def test_a_fresh_store_is_schema_six_with_eleven_documents(self) -> None:
        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(STORE_SCHEMA_VERSION, 6)
        self.assertEqual(readiness.migration_origin, "fresh")
        written = {item.name for item in self.root.iterdir() if item.suffix == ".json"}
        self.assertEqual(written, set(V6_DOCUMENT_NAMES))
        self.assertEqual(len(DEFAULTS), 11)

    def test_a_fresh_store_writes_the_default_knowledge_ledger(self) -> None:
        Store(self.root).initialize()

        self.assertEqual(self.documents()[KNOWLEDGE_DOCUMENT_NAME], KNOWLEDGE_DEFAULT)
        self.assertEqual(
            set(self.documents()[KNOWLEDGE_DOCUMENT_NAME]),
            {"version", "policy_revision", "connections", "requests"},
        )

    def test_fresh_knowledge_evidence_names_no_source(self) -> None:
        Store(self.root).initialize()

        migrations = self.documents()["store-meta.json"]["migrations"]
        self.assertEqual(
            set(migrations),
            {"identity", "planning_status", "reports", "knowledge"},
        )
        self.assertEqual(
            migrations["knowledge"],
            {
                "id": "workstack.knowledge.v6",
                "origin": "fresh",
                "source_sha256": None,
            },
        )

    def test_a_fresh_store_takes_no_migration_backup(self) -> None:
        Store(self.root).initialize()

        self.assertEqual(self.backups(), [])

    def test_the_layout_and_the_planner_agree_on_the_current_version(self) -> None:
        # The planner may not import the layout it sits underneath, so the two
        # constants are written twice and held equal here.
        self.assertEqual(
            store_knowledge_migration.CURRENT_SCHEMA_VERSION, STORE_SCHEMA_VERSION
        )
        self.assertEqual(store_report_migration.CURRENT_SCHEMA_VERSION, 5)


class V5UpgradeTest(KnowledgeMigrationCase):
    def test_a_v5_store_upgrades_to_v6_with_a_validated_empty_ledger(self) -> None:
        self.materialize(self.v5_values())

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        documents = self.documents()
        self.assertEqual(documents[KNOWLEDGE_DOCUMENT_NAME], KNOWLEDGE_DEFAULT)
        # The ledger the upgrade wrote is admitted by the store's own validator,
        # not merely present.
        validate_document_values(documents, schema_version=6)

    def test_every_existing_document_survives_the_upgrade_unchanged(self) -> None:
        values = self.v5_values(with_task=True)
        self.materialize(values)

        Store(self.root).initialize()

        documents = self.documents()
        for name in V5_DOCUMENT_NAMES:
            if name == "store-meta.json":
                continue
            self.assertEqual(documents[name], values[name], name)
        self.assertTrue(documents["backlog.json"]["tasks"])

    def test_only_the_version_and_the_knowledge_evidence_change_in_metadata(
        self,
    ) -> None:
        values = self.v5_values()
        self.materialize(values)

        Store(self.root).initialize()

        before = copy.deepcopy(values["store-meta.json"])
        after = self.documents()["store-meta.json"]
        self.assertEqual(after["store_schema_version"], 6)
        self.assertEqual(after["migrations"]["knowledge"]["origin"], "migrated_v5")
        self.assertEqual(
            after["migrations"]["knowledge"]["id"], "workstack.knowledge.v5-to-v6"
        )
        self.assertEqual(
            after["migrations"]["reports"], before["migrations"]["reports"]
        )
        self.assertEqual(
            after["migrations"]["identity"], before["migrations"]["identity"]
        )

    def test_the_knowledge_evidence_digests_the_detected_v5_documents(self) -> None:
        values = self.v5_values()
        self.materialize(values)

        Store(self.root).initialize()

        self.assertEqual(
            self.documents()["store-meta.json"]["migrations"]["knowledge"][
                "source_sha256"
            ],
            store_report_migration.source_digest(values),
        )

    def test_a_verifiable_pre_upgrade_backup_of_the_v5_bytes_exists(self) -> None:
        values = self.v5_values()
        self.materialize(values)
        before = {
            name: (self.root / name).read_bytes() for name in V5_DOCUMENT_NAMES
        }

        Store(self.root).initialize()

        backups = self.backups()
        self.assertEqual(len(backups), 1)
        verified = store_report_migration.verify_archive_file(backups[0])
        self.assertEqual(verified.store_schema_version, 5)
        self.assertEqual(verified.file_count, len(V5_DOCUMENT_NAMES))
        # The archive holds the bytes that were on disk, not a re-serialization.
        self.assertEqual(verified.bodies, before)

    def test_reinitializing_an_upgraded_store_is_a_plain_v6_admission(self) -> None:
        self.materialize(self.v5_values())
        Store(self.root).initialize()
        backups_after_upgrade = self.backups()

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(self.backups(), backups_after_upgrade)


class HistoricalUpgradeTest(KnowledgeMigrationCase):
    def test_a_v3_store_reaches_v6_and_keeps_the_report_migration_valid(self) -> None:
        values = self.v3_values()
        self.materialize(values)

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        migrations = self.documents()["store-meta.json"]["migrations"]
        # The released v3-to-v5 report step is still exactly what it was; the
        # knowledge step is recorded beside it rather than in place of it.
        self.assertEqual(migrations["reports"]["origin"], "migrated_v3")
        self.assertEqual(
            migrations["reports"]["id"], "workstack.reports.v3-to-v5"
        )
        self.assertEqual(migrations["knowledge"]["origin"], "migrated_v3")
        self.assertEqual(
            migrations["reports"]["source_sha256"],
            store_report_migration.source_digest(values),
        )
        self.assertEqual(
            migrations["knowledge"]["source_sha256"],
            migrations["reports"]["source_sha256"],
        )
        self.assertEqual(
            self.documents()[REPORTS_DOCUMENT_NAME],
            {"version": 1, "reports": [], "idempotency": []},
        )

    def test_the_released_v5_planner_still_produces_exactly_the_v5_roster(
        self,
    ) -> None:
        values = self.v3_values()
        digest = store_report_migration.source_digest(values)

        writes, operation = store_report_migration.plan_upgrade(
            3, values, digest, now="2026-09-08T09:00:00Z"
        )

        self.assertEqual(set(writes), set(V5_DOCUMENT_NAMES))
        self.assertEqual(writes["store-meta.json"]["store_schema_version"], 5)
        self.assertEqual(operation, "store-migrate-v3-v5-{}".format(digest[7:23]))

    def test_the_v6_planner_refuses_a_version_it_cannot_carry(self) -> None:
        with self.assertRaises(StoreCorruptError):
            store_knowledge_migration.plan_upgrade(
                4, {}, "sha256:" + "0" * 64, now="2026-09-08T09:00:00Z"
            )
        with self.assertRaises(StoreCorruptError):
            store_knowledge_migration.plan_upgrade(
                6, {}, "sha256:" + "0" * 64, now="2026-09-08T09:00:00Z"
            )


class RefusalTest(KnowledgeMigrationCase):
    def test_a_future_schema_is_refused_as_newer_than_this_build(self) -> None:
        values = self.fresh_v6_values()
        values["store-meta.json"]["store_schema_version"] = 7
        self.materialize(values)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("newer than this Work Stack build", str(caught.exception))

    def test_a_v6_roster_claiming_schema_five_is_not_accepted_as_v5(self) -> None:
        # The roster on disk is v6, so the store is judged as v6 and its
        # metadata has to say so. Silently reading it as a v5 store would drop
        # the ledger from every later admission.
        values = self.fresh_v6_values()
        values["store-meta.json"]["store_schema_version"] = 5
        self.materialize(values)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("store schema version is invalid", str(caught.exception))

    def test_v6_metadata_without_knowledge_evidence_is_refused(self) -> None:
        values = self.fresh_v6_values()
        del values["store-meta.json"]["migrations"]["knowledge"]
        self.materialize(values)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("migration evidence is invalid", str(caught.exception))

    def test_fresh_knowledge_evidence_carrying_a_source_is_refused(self) -> None:
        values = self.fresh_v6_values()
        values["store-meta.json"]["migrations"]["knowledge"]["source_sha256"] = (
            "sha256:" + "0" * 64
        )
        self.materialize(values)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("fresh knowledge evidence is invalid", str(caught.exception))

    def test_a_malformed_knowledge_document_fails_closed(self) -> None:
        for broken in (
            {"version": 2, "policy_revision": 0, "connections": [], "requests": []},
            {"version": 1, "policy_revision": -1, "connections": [], "requests": []},
            {
                "version": 1,
                "policy_revision": 0,
                "connections": [],
                "requests": [],
                "query": "leaked",
            },
            {"version": 1, "policy_revision": 0, "connections": []},
            {
                "version": 1,
                "policy_revision": 0,
                "connections": [
                    {
                        "alias": "team",
                        "upstream_workspace_uid": "66666666-6666-4666-8666-666666666666",
                        "corpus_refs": ["nas"],
                        "scope": "project",
                    }
                ],
                "requests": [],
            },
        ):
            with self.subTest(broken=sorted(broken)):
                with tempfile.TemporaryDirectory() as scratch:
                    root = Path(scratch)
                    values = self.fresh_v6_values()
                    values[KNOWLEDGE_DOCUMENT_NAME] = broken
                    for name, value in values.items():
                        self._write(root / name, value)

                    with self.assertRaises(StoreCorruptError) as caught:
                        Store(root).initialize()

                self.assertIn("knowledge.json schema is invalid", str(caught.exception))

    def test_a_ledger_bound_to_another_workspace_is_refused(self) -> None:
        values = self.fresh_v6_values()
        values[KNOWLEDGE_DOCUMENT_NAME] = {
            "version": 1,
            "policy_revision": 1,
            "connections": [],
            "requests": [
                {
                    "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "request_digest": "sha256:" + "1" * 64,
                    "connection_alias": "team",
                    "binding": {
                        "workspace_uid": "66666666-6666-4666-8666-666666666666"
                    },
                    "corpus_refs": ["nas"],
                    "policy_revision": 1,
                    "result_limit": 5,
                    "requested_at": "2026-09-08T09:00:00Z",
                    "expires_at": "2026-09-08T09:05:00Z",
                    "state": "pending",
                    "capture_ids": [],
                    "completion_digest": None,
                    "completed_at": None,
                }
            ],
        }
        self.materialize(values)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("workspace_mismatch", str(caught.exception))

    def ledger_values(
        self, *, connections: list[Any], record_revision: int, corpus_refs: list[str]
    ) -> dict[str, Any]:
        """A fresh v6 authority carrying one hand-written ledger record.

        The record is well formed on every other axis, so whatever the store
        decides here it decides about referential integrity alone.
        """

        values = self.fresh_v6_values()
        workspace_uid = values["workspace.json"]["id"]
        values[KNOWLEDGE_DOCUMENT_NAME] = {
            "version": 1,
            "policy_revision": 1,
            "connections": connections,
            "requests": [
                {
                    "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "request_digest": "sha256:" + "1" * 64,
                    "connection_alias": "team-nas",
                    "binding": {"workspace_uid": workspace_uid},
                    "corpus_refs": corpus_refs,
                    "policy_revision": record_revision,
                    "result_limit": 3,
                    "requested_at": "2026-09-08T09:00:00Z",
                    "expires_at": "2026-09-08T09:05:00Z",
                    "state": "pending",
                    "capture_ids": [],
                    "completion_digest": None,
                    "completed_at": None,
                }
            ],
        }
        return values

    @staticmethod
    def connection(*corpus_refs: str, alias: str = "team-nas") -> dict[str, Any]:
        return {
            "alias": alias,
            "upstream_workspace_uid": "66666666-6666-4666-8666-666666666666",
            "corpus_refs": list(corpus_refs),
            "scope": "workspace",
        }

    def test_a_current_record_without_a_current_connection_is_refused(self) -> None:
        """A record claiming the policy in force, that the policy never granted.

        This is the shape a restart has to fail closed on: nothing about the
        record is malformed, but no connection on the current roster authorises
        it, so the store must not come back reporting it as live authority.
        """

        self.materialize(
            self.ledger_values(connections=[], record_revision=1, corpus_refs=["nas"])
        )

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn(
            "knowledge.json schema is invalid: connection_not_found",
            str(caught.exception),
        )

    def test_a_current_record_outside_the_granted_corpora_is_refused(self) -> None:
        self.materialize(
            self.ledger_values(
                connections=[self.connection("notion")],
                record_revision=1,
                corpus_refs=["nas"],
            )
        )

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn(
            "knowledge.json schema is invalid: corpus_not_granted",
            str(caught.exception),
        )

    def test_an_older_revision_record_survives_a_retired_connection(self) -> None:
        """History stays readable; that is why the check is revision-scoped.

        The same record one revision behind the document is admitted with an
        empty roster, because the owner retiring a connection must not make the
        evidence that a request was ever issued unreadable.
        """

        self.materialize(
            self.ledger_values(connections=[], record_revision=0, corpus_refs=["nas"])
        )

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        ledger = self.documents()[KNOWLEDGE_DOCUMENT_NAME]
        self.assertEqual(ledger["connections"], [])
        self.assertEqual(ledger["requests"][0]["policy_revision"], 0)

    def test_a_partial_v6_roster_is_refused_rather_than_read_as_v5(self) -> None:
        values = self.fresh_v6_values()
        values.pop("notes.json")
        self.materialize(values)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("required store roster is incomplete", str(caught.exception))


class InterruptedUpgradeTest(KnowledgeMigrationCase):
    def test_an_interrupted_upgrade_leaves_a_recoverable_journal(self) -> None:
        self.materialize(self.v5_values())
        store = Store(self.root)

        with mock.patch.object(
            store, "_atomic_write_locked", self._fail_after(store, 4)
        ):
            with self.assertRaises(OSError):
                store.initialize()

        self.assertTrue((self.root / JOURNAL_NAME).exists())

    def test_recovery_completes_the_upgrade_to_a_coherent_v6_store(self) -> None:
        values = self.v5_values(with_task=True)
        self.materialize(values)
        store = Store(self.root)
        with mock.patch.object(
            store, "_atomic_write_locked", self._fail_after(store, 4)
        ):
            with self.assertRaises(OSError):
                store.initialize()

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertFalse((self.root / JOURNAL_NAME).exists())
        documents = self.documents()
        validate_document_values(documents, schema_version=6)
        self.assertEqual(documents[KNOWLEDGE_DOCUMENT_NAME], KNOWLEDGE_DEFAULT)
        self.assertEqual(documents["backlog.json"], values["backlog.json"])

    def test_an_upgrade_interrupted_before_any_write_leaves_the_v5_store(self) -> None:
        values = self.v5_values()
        self.materialize(values)
        before = {
            name: (self.root / name).read_bytes() for name in V5_DOCUMENT_NAMES
        }
        store = Store(self.root)

        with mock.patch.object(
            store, "_atomic_write_locked", self._fail_after(store, 0)
        ):
            with self.assertRaises(OSError):
                store.initialize()

        self.assertFalse((self.root / KNOWLEDGE_DOCUMENT_NAME).exists())
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V5_DOCUMENT_NAMES},
            before,
        )

    def test_a_retried_upgrade_reuses_the_same_deterministic_backup(self) -> None:
        self.materialize(self.v5_values())
        store = Store(self.root)
        with mock.patch.object(
            store, "_atomic_write_locked", self._fail_after(store, 2)
        ):
            with self.assertRaises(OSError):
                store.initialize()
        first = [path.name for path in self.backups()]

        Store(self.root).initialize()

        self.assertEqual([path.name for path in self.backups()], first)


class RosterFactTest(KnowledgeMigrationCase):
    def test_the_v6_roster_is_the_v5_roster_plus_the_ledger(self) -> None:
        self.assertEqual(
            V6_DOCUMENT_NAMES, V5_DOCUMENT_NAMES | {KNOWLEDGE_DOCUMENT_NAME}
        )
        self.assertEqual(len(V6_DOCUMENT_NAMES), 11)
        self.assertNotIn(KNOWLEDGE_DOCUMENT_NAME, V5_DOCUMENT_NAMES)
        self.assertNotIn(KNOWLEDGE_DOCUMENT_NAME, V3_DOCUMENT_NAMES)

    def test_the_historical_rosters_still_admit_their_own_stores(self) -> None:
        # A widened roster would make a healthy v5 directory unreadable; the
        # v5 fixture below is admitted as v5 without being upgraded.
        values = self.v5_values()

        readiness = validate_document_values(values, schema_version=5)

        self.assertEqual(readiness.schema_version, 5)

    def test_the_backup_roster_knows_the_v6_member_set(self) -> None:
        self.assertEqual(
            store_report_migration.backup_roster(6), tuple(sorted(V6_DOCUMENT_NAMES))
        )
