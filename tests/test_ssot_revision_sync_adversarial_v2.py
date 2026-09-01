from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import RevisionConflictError, WorkStack
from workstack.store import (
    MAX_REVISION,
    Store,
    StoreCorruptError,
    StoreExternalChangeError,
)


class RevisionAwareSsotSyncAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        # Establish the persisted baseline before any adversarial interleaving.
        self.store.sync_status()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _append_external_note(self, note_id: str) -> bytes:
        path = self.store.path("notes.json")
        notes = json.loads(path.read_text(encoding="utf-8"))
        notes["notes"].append(
            {"id": note_id, "title": "External agent", "body": "Changed remotely"}
        )
        path.write_text(json.dumps(notes), encoding="utf-8")
        return path.read_bytes()

    def _advance_external_task(self, task_id: str, *, detail: str, revision: int) -> bytes:
        path = self.store.path("backlog.json")
        backlog = json.loads(path.read_text(encoding="utf-8"))
        task = next(item for item in backlog["tasks"] if item["id"] == task_id)
        task["detail"] = detail
        task["revision"] = revision
        path.write_text(json.dumps(backlog), encoding="utf-8")
        return path.read_bytes()

    def test_p1_simultaneous_external_edit_is_not_silently_absorbed_by_local_commit(
        self,
    ) -> None:
        original_atomic_write = self.store._atomic_write_locked
        injected = False
        external_bytes: bytes | None = None

        def interleaved_write(path: Path, value: object) -> None:
            nonlocal injected, external_bytes
            original_atomic_write(path, value)
            if path == self.store.path("backlog.json") and not injected:
                injected = True
                # A raw external agent does not participate in Work Stack's process
                # lease. This is the exact interval after local preflight and before
                # the committed manifest is advanced.
                external_bytes = self._append_external_note("N-racing-agent")

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=interleaved_write
        ):
            try:
                self.stack.add_task("Local task racing with the agent")
            except (StoreCorruptError, StoreExternalChangeError):
                pass

        self.assertTrue(injected)
        self.assertEqual(self.store.path("notes.json").read_bytes(), external_bytes)
        self.assertEqual(
            self.store.sync_status()["state"],
            "external-change-detected",
            "a concurrent external edit must remain reviewable, never be absorbed into the local baseline",
        )

    def test_stale_local_task_base_is_rejected_after_external_revision_is_adopted(
        self,
    ) -> None:
        task = self.stack.add_task("Shared task")
        external_bytes = self._advance_external_task(
            task["id"], detail="Agent revision one", revision=1
        )
        pending = self.store.sync_status()
        self.assertEqual(pending["state"], "external-change-detected")
        self.store.adopt_external_change(
            pending["generation"], pending["manifest_digest"]
        )

        with self.assertRaises(RevisionConflictError):
            self.stack.patch_task(
                task["id"], {"detail": "Stale browser edit", "revision": 0}
            )

        self.assertEqual(self.store.path("backlog.json").read_bytes(), external_bytes)
        self.assertEqual(self.stack.get_task(task["id"])["detail"], "Agent revision one")

    def test_p1_adoption_retry_after_lost_response_is_semantically_idempotent(
        self,
    ) -> None:
        self._append_external_note("N-response-loss")
        pending = self.store.sync_status()

        # The manifest commit succeeds. Only delivery of the success response is lost.
        with mock.patch.object(
            self.store,
            "sync_status",
            side_effect=ConnectionError("success response was lost"),
        ):
            with self.assertRaises(ConnectionError):
                self.store.adopt_external_change(
                    pending["generation"], pending["manifest_digest"]
                )

        retry = self.store.adopt_external_change(
            pending["generation"], pending["manifest_digest"]
        )
        self.assertEqual(retry["state"], "in-sync")
        self.assertGreater(retry["generation"], pending["generation"])

    def test_p1_adoption_receipt_failure_after_manifest_commit_remains_retryable(
        self,
    ) -> None:
        self._append_external_note("N-receipt-failure")
        pending = self.store.sync_status()
        original_atomic_write = self.store._atomic_write_locked

        def fail_receipt(path: Path, value: object) -> None:
            if path == self.store.sync_adoption_receipt_path:
                raise OSError("simulated receipt durability failure")
            original_atomic_write(path, value)

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=fail_receipt
        ):
            with self.assertRaises(OSError):
                self.store.adopt_external_change(
                    pending["generation"],
                    pending["manifest_digest"],
                    "sync-receipt-failure-key",
                )

        # The authoritative manifest has already advanced. Reconstruct the Store
        # exactly as a desktop/server restart would, then retry the same operation.
        # Recovery must not depend on process-local memory or a receipt that never
        # became durable.
        restarted_store = Store(self.root)
        WorkStack(restarted_store)
        retry = restarted_store.adopt_external_change(
            pending["generation"],
            pending["manifest_digest"],
            "sync-receipt-failure-key",
        )
        self.assertEqual(retry["state"], "in-sync")

    def test_successful_adoption_replay_survives_process_restart(self) -> None:
        self._append_external_note("N-adoption-restart")
        pending = self.store.sync_status()
        key = "sync-adoption-restart-key"
        accepted = self.store.adopt_external_change(
            pending["generation"], pending["manifest_digest"], key
        )
        self.assertEqual(accepted["state"], "in-sync")

        restarted_store = Store(self.root)
        restarted_stack = WorkStack(restarted_store)
        replay = restarted_store.adopt_external_change(
            pending["generation"], pending["manifest_digest"], key
        )

        self.assertEqual(replay["state"], "in-sync")
        self.assertEqual(replay["generation"], accepted["generation"])
        self.assertEqual(restarted_stack.store.sync_status(), replay)

    def test_external_change_detection_survives_process_restart(self) -> None:
        external_bytes = self._append_external_note("N-restart")
        first = self.store.sync_status()
        self.assertEqual(first["state"], "external-change-detected")

        restarted_store = Store(self.root)
        restarted_stack = WorkStack(restarted_store)
        restarted = restarted_store.sync_status()

        self.assertEqual(restarted["state"], "external-change-detected")
        self.assertEqual(restarted["changed_files"], ["notes.json"])
        with self.assertRaises(StoreExternalChangeError):
            restarted_stack.add_task("Restart must not thaw writes")
        self.assertEqual(self.store.path("notes.json").read_bytes(), external_bytes)

    def test_malformed_external_task_revisions_fail_closed_without_baseline_advance(
        self,
    ) -> None:
        malformed_values = (True, "1", -1, MAX_REVISION + 1)
        for malformed in malformed_values:
            with self.subTest(revision=malformed), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                stack = WorkStack(store)
                task = stack.add_task("Revision metadata target")
                baseline_manifest = store.store_manifest_path.read_bytes()
                backlog_path = store.path("backlog.json")
                backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
                backlog["tasks"][0]["detail"] = "Malformed external candidate"
                backlog["tasks"][0]["revision"] = malformed
                backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
                candidate_bytes = backlog_path.read_bytes()

                status = store.sync_status()
                self.assertEqual(status["state"], "invalid")
                self.assertEqual(status["changed_files"], ["backlog.json"])
                with self.assertRaises(StoreExternalChangeError):
                    store.adopt_external_change(
                        status["generation"], status["manifest_digest"]
                    )
                self.assertEqual(backlog_path.read_bytes(), candidate_bytes)
                self.assertEqual(store.store_manifest_path.read_bytes(), baseline_manifest)
                # Reads and writes remain frozen; inspect only the already-preserved
                # candidate bytes rather than bypassing that public safety boundary.
                preserved = json.loads(candidate_bytes)
                self.assertEqual(preserved["tasks"][0]["id"], task["id"])
                self.assertEqual(preserved["tasks"][0]["revision"], malformed)

    def test_invalid_multi_file_candidate_is_preserved_without_partial_adoption(self) -> None:
        task = self.stack.add_task("No partial adoption")
        baseline_manifest = self.store.store_manifest_path.read_bytes()
        external_notes = self._append_external_note("N-valid-half")
        external_backlog = self._advance_external_task(
            task["id"], detail="Invalid half", revision=task["revision"]
        )

        status = self.store.sync_status()
        self.assertEqual(status["state"], "invalid")
        self.assertEqual(status["changed_files"], ["backlog.json", "notes.json"])
        with self.assertRaises(StoreExternalChangeError):
            self.store.adopt_external_change(
                status["generation"], status["manifest_digest"]
            )

        self.assertEqual(self.store.path("notes.json").read_bytes(), external_notes)
        self.assertEqual(self.store.path("backlog.json").read_bytes(), external_backlog)
        self.assertEqual(self.store.store_manifest_path.read_bytes(), baseline_manifest)

    def test_manifest_replace_failure_preserves_candidate_and_leaves_no_temp_file(
        self,
    ) -> None:
        external_notes = self._append_external_note("N-manifest-failure")
        pending = self.store.sync_status()
        baseline_manifest = self.store.store_manifest_path.read_bytes()
        original_replace = os.replace

        def fail_manifest_replace(source: str, destination: str) -> None:
            if Path(destination) == self.store.store_manifest_path:
                raise OSError("simulated manifest replace failure")
            original_replace(source, destination)

        with mock.patch("workstack.store.os.replace", side_effect=fail_manifest_replace):
            with self.assertRaises(OSError):
                self.store.adopt_external_change(
                    pending["generation"], pending["manifest_digest"]
                )

        self.assertEqual(self.store.path("notes.json").read_bytes(), external_notes)
        self.assertEqual(self.store.store_manifest_path.read_bytes(), baseline_manifest)
        self.assertEqual(self.store.sync_status()["state"], "external-change-detected")
        self.assertEqual(
            list(self.store.runtime_root.glob(self.store.store_manifest_path.name + ".*.tmp")),
            [],
        )

    def test_p1_restart_recovery_does_not_absorb_an_unrelated_external_edit(
        self,
    ) -> None:
        original_atomic_write = self.store._atomic_write_locked
        injected = False
        failed_manifest = False
        external_bytes: bytes | None = None

        def crash_after_interleaving(path: Path, value: object) -> None:
            nonlocal injected, failed_manifest, external_bytes
            original_atomic_write(path, value)
            if path == self.store.path("backlog.json") and not injected:
                injected = True
                external_bytes = self._append_external_note("N-recovery-race")
            elif path == self.store.store_manifest_path and injected and not failed_manifest:
                failed_manifest = True
                raise OSError("simulated crash after the manifest replace")

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=crash_after_interleaving
        ):
            with self.assertRaises(OSError):
                self.stack.add_task("Local task interrupted before reconciliation")

        self.assertTrue(self.store.journal_path.exists())
        self.assertEqual(self.store.path("notes.json").read_bytes(), external_bytes)

        restarted_store = Store(self.root)
        WorkStack(restarted_store)
        restarted = restarted_store.sync_status()

        self.assertEqual(
            restarted["state"],
            "external-change-detected",
            "journal recovery may replay local targets but must not baseline an unrelated agent edit",
        )
        self.assertEqual(restarted["changed_files"], ["notes.json"])
        self.assertEqual(restarted_store.path("notes.json").read_bytes(), external_bytes)

    def test_p1_restart_recovery_never_overwrites_a_concurrently_changed_target(
        self,
    ) -> None:
        task = self.stack.add_task("Declared target race")
        original_atomic_write = self.store._atomic_write_locked
        injected = False
        external_bytes: bytes | None = None

        def overwrite_declared_target(path: Path, value: object) -> None:
            nonlocal injected, external_bytes
            original_atomic_write(path, value)
            if path == self.store.path("backlog.json") and not injected:
                injected = True
                backlog = json.loads(path.read_text(encoding="utf-8"))
                changed = next(
                    item for item in backlog["tasks"] if item["id"] == task["id"]
                )
                changed["detail"] = "External agent changed the declared target"
                changed["revision"] += 1
                path.write_text(json.dumps(backlog), encoding="utf-8")
                external_bytes = path.read_bytes()

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=overwrite_declared_target
        ):
            with self.assertRaises(StoreCorruptError):
                self.stack.patch_task(
                    task["id"], {"detail": "Local intended value", "revision": 0}
                )

        self.assertTrue(self.store.journal_path.exists())
        self.assertEqual(self.store.path("backlog.json").read_bytes(), external_bytes)

        restarted_store = Store(self.root)
        with self.assertRaises(StoreCorruptError):
            WorkStack(restarted_store)

        self.assertEqual(restarted_store.path("backlog.json").read_bytes(), external_bytes)
        self.assertTrue(
            restarted_store.journal_path.exists(),
            "commit-unknown evidence must remain available for explicit recovery",
        )


if __name__ == "__main__":
    unittest.main()
