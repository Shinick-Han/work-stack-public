"""Tests for the bounded remote maintenance operation adapter.

Synthetic disposable fixtures only: every store is built under a temporary
directory by the product's own service facade. No live SSOT, no company host,
no network, no dependency install. The supported maintenance calls are the real
ones, the archives are real archives, and every digest is recomputed by the
product's own verifier rather than asserted as a literal.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from workstack import maintenance
from workstack.service import WorkStack
from workstack.store import Store
from workstack.store_knowledge_migration import CURRENT_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
ADAPTER_PATH = SHELL / "remote_update_maintenance.py"
RECEIPTS_PATH = SHELL / "remote_update_maintenance_receipts.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


receipts = _load(RECEIPTS_PATH, "remote_update_maintenance_receipts")
adapter = _load(ADAPTER_PATH, "remote_update_maintenance")


TASK_TITLE = "Preserve this planning fact across the remote update"
NOTE_TEXT = "The context this workspace must still hold after a restore"


class MaintenanceAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "ssot-store-directory"
        self.backups = self.root / "backups"
        self.state = self.root / "operations"
        self.stack = WorkStack(Store(self.source))
        self.task = self.stack.add_task(TASK_TITLE, priority="P1")
        self.note = self.stack.add_note(NOTE_TEXT)
        with Store(self.source).consistent_read() as readiness:
            self.workspace_id = readiness.workspace_uid
        self.backup_operation = str(uuid.uuid4())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # ---- fixtures -----------------------------------------------------

    def backup_request(self, kind: str = "create_backup", operation: str | None = None) -> dict:
        return {
            "kind": kind,
            "operation_id": operation or self.backup_operation,
            "workspace_dir": str(self.source),
            "workspace_id": self.workspace_id,
            "state_root": str(self.state),
            "backup_root": str(self.backups),
        }

    def restore_request(
        self, digest: str, kind: str = "restore_backup", operation: str | None = None
    ) -> dict:
        request = self.backup_request(kind=kind, operation=operation or str(uuid.uuid4()))
        request["source_operation_id"] = self.backup_operation
        request["backup_digest"] = digest
        return request

    def created_backup(self) -> dict:
        result = adapter.run_operation(self.backup_request())
        self.assertEqual(result["status"], "verified", result)
        return result

    def archive_path(self, operation: str | None = None) -> Path:
        directory = self.backups / (operation or self.backup_operation)
        files = sorted(item for item in directory.iterdir() if item.is_file())
        self.assertEqual(len(files), 1, files)
        return files[0]

    def workspace_titles(self) -> list[str]:
        stack = WorkStack(Store(self.source))
        return sorted(item["title"] for item in stack.list_tasks(status="all"))

    def state_entries(self) -> list[str]:
        if not self.state.is_dir():
            return []
        return sorted(os.listdir(self.state))

    # ---- request admission --------------------------------------------

    def test_an_unknown_request_key_is_refused_before_any_effect(self) -> None:
        request = self.backup_request()
        request["restart_after"] = True

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "request_key_unknown")
        self.assertEqual(self.state_entries(), [])
        self.assertFalse(self.backups.exists())

    def test_a_missing_configured_key_is_refused(self) -> None:
        request = self.backup_request()
        del request["backup_root"]

        result = adapter.run_operation(request)

        self.assertEqual(result["code"], "request_invalid")
        self.assertEqual(result["status"], "failed")

    def test_an_unknown_operation_kind_is_refused(self) -> None:
        request = self.backup_request()
        request["kind"] = "delete_backup"

        result = adapter.run_operation(request)

        self.assertEqual(result["code"], "request_kind_unknown")
        self.assertIsNone(result["kind"])

    def test_an_operation_id_that_is_not_a_uuid_is_refused(self) -> None:
        result = adapter.run_operation(self.backup_request(operation="operation-1"))

        self.assertEqual(result["code"], "operation_id_invalid")
        self.assertIsNone(result["operation_id"])
        self.assertEqual(self.state_entries(), [])

    def test_an_operation_state_root_inside_the_workspace_is_refused(self) -> None:
        request = self.backup_request()
        request["state_root"] = str(self.source / "operations")

        result = adapter.run_operation(request)

        self.assertEqual(result["code"], "path_inside_workspace")
        self.assertFalse((self.source / "operations").exists())

    def test_a_backup_root_inside_the_workspace_is_refused(self) -> None:
        request = self.backup_request()
        request["backup_root"] = str(self.source / "backups")

        result = adapter.run_operation(request)

        self.assertEqual(result["code"], "path_inside_workspace")
        self.assertFalse((self.source / "backups").exists())

    def test_a_relative_workspace_path_is_refused(self) -> None:
        request = self.backup_request()
        request["workspace_dir"] = "workspace"

        result = adapter.run_operation(request)

        self.assertEqual(result["code"], "path_invalid")

    # ---- identity ------------------------------------------------------

    def test_a_workspace_held_by_its_owner_refuses_without_binding_anything(self) -> None:
        store = Store(self.source)
        with store.server_lease():
            result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "workspace_held_by_owner")
        self.assertFalse(result["attempted"])
        self.assertEqual(self.state_entries(), [])
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])

    def test_a_wrong_workspace_uuid_refuses_before_any_effect(self) -> None:
        request = self.backup_request()
        request["workspace_id"] = str(uuid.uuid4())

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "workspace_identity_mismatch")
        self.assertEqual(self.state_entries(), [])
        self.assertFalse(self.backups.exists())

    def test_a_receipt_bound_to_another_operation_refuses_this_one(self) -> None:
        other = dict(adapter.binding_of(adapter.parse_request(self.backup_request())))
        other["workspace_dir"] = str(self.root / "another-workspace")
        receipts.bind_attempt(self.state, other)

        result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "receipt_binding_mismatch")
        self.assertFalse(self.backups.exists())

    # ---- create_backup --------------------------------------------------

    def test_a_created_backup_is_verified_and_recorded_once(self) -> None:
        result = self.created_backup()

        archive = self.archive_path()
        self.assertEqual(result["code"], "backup_verified")
        self.assertEqual(result["backup_digest"], maintenance.verify_backup(archive).digest)
        self.assertEqual(result["backup_file_count"], maintenance.verify_backup(archive).file_count)
        self.assertTrue(result["attempted"])
        self.assertTrue(result["workspace_match"])
        self.assertFalse(result["reconciled"])
        self.assertFalse(result["replayed"])
        self.assertEqual(result["store_schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            self.state_entries(),
            [
                self.backup_operation + ".attempt.json",
                self.backup_operation + ".completed.json",
            ],
        )

    def test_the_backup_leaves_the_workspace_task_and_context_untouched(self) -> None:
        before = {
            path.name: path.read_bytes() for path in sorted(self.source.glob("*.json"))
        }

        self.created_backup()

        after = {
            path.name: path.read_bytes() for path in sorted(self.source.glob("*.json"))
        }
        self.assertEqual(after, before)
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])

    def test_reissuing_a_completed_backup_replays_the_receipt_without_a_second_archive(self) -> None:
        first = self.created_backup()

        second = adapter.run_operation(self.backup_request())

        self.assertEqual(second["status"], "verified")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["backup_digest"], first["backup_digest"])
        directory = self.backups / self.backup_operation
        self.assertEqual(len([item for item in directory.iterdir() if item.is_file()]), 1)

    def test_an_attempted_backup_with_no_archive_stays_unknown_and_is_not_reissued(self) -> None:
        binding = adapter.binding_of(adapter.parse_request(self.backup_request()))
        receipts.bind_attempt(self.state, binding)

        result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "backup_attempt_unresolved")
        self.assertTrue(result["attempted"])
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.state_entries(), [self.backup_operation + ".attempt.json"])

    def test_a_lost_reply_reconciles_only_a_fully_verified_unique_archive(self) -> None:
        first = self.created_backup()
        receipts.completed_path(self.state, self.backup_operation).unlink()

        result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["code"], "backup_reconciled")
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["backup_digest"], first["backup_digest"])

    def test_an_attempted_backup_reconciles_while_the_owner_holds_the_lease(self) -> None:
        created = self.created_backup()
        receipts.completed_path(self.state, self.backup_operation).unlink()

        with Store(self.source).server_lease():
            result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["code"], "backup_reconciled")
        self.assertEqual(result["backup_digest"], created["backup_digest"])

    def test_a_lost_reply_with_a_corrupted_archive_stays_unknown(self) -> None:
        self.created_backup()
        receipts.completed_path(self.state, self.backup_operation).unlink()
        archive = self.archive_path()
        archive.write_bytes(archive.read_bytes()[:-32] + b"corrupted-tail-bytes-not-an-archive")

        result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "backup_attempt_unresolved")
        self.assertIsNone(result["backup_digest"])

    def test_a_lost_reply_with_two_archives_stays_unknown(self) -> None:
        self.created_backup()
        receipts.completed_path(self.state, self.backup_operation).unlink()
        archive = self.archive_path()
        archive.with_name("second-" + archive.name).write_bytes(archive.read_bytes())

        result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "backup_attempt_unresolved")

    def test_a_failed_completed_receipt_is_unknown_and_leaves_the_archive_inspectable(self) -> None:
        with mock.patch.object(
            receipts, "complete", side_effect=receipts.ReceiptUnavailable("no receipt")
        ):
            result = adapter.run_operation(self.backup_request())

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "receipt_io_unknown")
        self.assertTrue(result["attempted"])
        self.assertTrue(self.archive_path().is_file())
        observed = adapter.run_operation(self.backup_request("observe_backup"))
        self.assertEqual(observed["status"], "verified")
        self.assertEqual(observed["code"], "backup_reconciled")

    # ---- observation ----------------------------------------------------

    def test_an_observation_of_an_operation_that_never_ran_is_not_run(self) -> None:
        result = adapter.run_operation(self.backup_request("observe_backup"))

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "backup_not_run")
        self.assertFalse(result["attempted"])
        self.assertTrue(result["observation"])
        self.assertEqual(self.state_entries(), [])

    def test_an_observation_writes_nothing_and_does_not_take_the_writer_lease(self) -> None:
        created = self.created_backup()
        before = self.state_entries()

        with Store(self.source).server_lease():
            result = adapter.run_operation(self.backup_request("observe_backup"))

        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["replayed"])
        self.assertTrue(result["observation"])
        self.assertEqual(result["backup_digest"], created["backup_digest"])
        self.assertEqual(self.state_entries(), before)

    def test_a_lost_reply_is_observed_read_only_against_the_same_identity(self) -> None:
        created = self.created_backup()
        receipts.completed_path(self.state, self.backup_operation).unlink()
        before = self.state_entries()

        result = adapter.run_operation(self.backup_request("observe_backup"))

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["code"], "backup_reconciled")
        self.assertEqual(result["backup_digest"], created["backup_digest"])
        self.assertEqual(self.state_entries(), before)

    # ---- restore --------------------------------------------------------

    def test_a_restore_returns_the_workspace_and_refuses_rollback_compatibility(self) -> None:
        created = self.created_backup()
        self.stack.add_task("A fact written after the backup")
        request = self.restore_request(created["backup_digest"])

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(result["code"], "restore_verified")
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])
        self.assertEqual(
            [item["text"] for item in WorkStack(Store(self.source)).workspace_projection()["notes"]],
            [NOTE_TEXT],
        )
        self.assertEqual(result["store_schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertIs(result["rollback_compatible"], False)
        self.assertIs(result["schema_downgraded"], False)
        self.assertIsNone(result["pre_migration_data"])
        self.assertIsNone(result["archive_schema_version"])
        self.assertIs(result["safety_backup_created"], True)

    def test_a_restore_refuses_a_corrupted_selected_archive_before_any_effect(self) -> None:
        created = self.created_backup()
        self.stack.add_task("A fact written after the backup")
        archive = self.archive_path()
        archive.write_bytes(b"not an archive at all")
        request = self.restore_request(created["backup_digest"])

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "backup_verification_failed")
        self.assertFalse(result["attempted"])
        self.assertEqual(
            self.workspace_titles(), sorted([TASK_TITLE, "A fact written after the backup"])
        )

    def test_a_restore_refuses_a_digest_that_is_not_the_selected_archive(self) -> None:
        self.created_backup()
        request = self.restore_request("sha256:" + "0" * 64)

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "backup_digest_mismatch")
        self.assertEqual(self.state_entries(), sorted(self.state_entries()))
        self.assertNotIn(
            request["operation_id"] + ".attempt.json", self.state_entries()
        )

    def test_a_restore_refuses_a_workspace_that_is_not_the_configured_one(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])
        request["workspace_id"] = str(uuid.uuid4())

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "workspace_identity_mismatch")
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])

    def test_a_restore_refuses_a_workspace_held_by_its_owner(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])

        with Store(self.source).server_lease():
            result = adapter.run_operation(request)

        self.assertEqual(result["code"], "workspace_held_by_owner")
        self.assertNotIn(request["operation_id"] + ".attempt.json", self.state_entries())

    def test_an_interrupted_restore_is_never_inferred_complete_from_plausible_bytes(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])
        binding = adapter.binding_of(adapter.parse_request(request))
        receipts.bind_attempt(self.state, binding)
        self.stack.add_task("A fact written after the backup")

        result = adapter.run_operation(request)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "restore_completion_unknown")
        self.assertTrue(result["attempted"])
        self.assertIsNone(result["rollback_compatible"])
        self.assertIsNone(result["pre_migration_data"])
        # The restore was not reissued: the divergent fact is still in place.
        self.assertEqual(
            self.workspace_titles(), sorted([TASK_TITLE, "A fact written after the backup"])
        )
        self.assertNotIn(
            receipts.completed_path(self.state, request["operation_id"]).name,
            self.state_entries(),
        )

    def test_an_interrupted_restore_observes_as_unknown_and_writes_nothing(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])
        receipts.bind_attempt(self.state, adapter.binding_of(adapter.parse_request(request)))
        before = self.state_entries()

        observation = dict(request)
        observation["kind"] = "observe_restore"
        result = adapter.run_operation(observation)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "restore_completion_unknown")
        self.assertTrue(result["observation"])
        self.assertEqual(result["kind"], "restore_backup")
        self.assertEqual(self.state_entries(), before)

    def test_an_attempted_restore_stays_unknown_while_the_owner_holds_the_lease(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])
        receipts.bind_attempt(self.state, adapter.binding_of(adapter.parse_request(request)))

        with Store(self.source).server_lease():
            result = adapter.run_operation(request)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "restore_completion_unknown")
        self.assertTrue(result["attempted"])

    def test_an_unattempted_restore_observes_as_not_run(self) -> None:
        created = self.created_backup()
        observation = self.restore_request(created["backup_digest"], kind="observe_restore")

        result = adapter.run_operation(observation)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "restore_not_run")
        self.assertFalse(result["attempted"])

    def test_a_completed_restore_replays_its_receipt_instead_of_restoring_again(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])
        first = adapter.run_operation(request)
        self.assertEqual(first["status"], "verified")
        self.stack.add_task("A fact written after the restore")

        second = adapter.run_operation(request)

        self.assertEqual(second["status"], "verified")
        self.assertTrue(second["replayed"])
        self.assertEqual(
            self.workspace_titles(), sorted([TASK_TITLE, "A fact written after the restore"])
        )

    # ---- post-effect boundary -------------------------------------------

    def _real_restore_then(self, aftermath):
        """Let the real restore run and return, then trip ``aftermath``.

        The supported service is never replaced: it runs, rewrites the
        destination and returns its own receipt. Only afterwards does the
        competing event happen, which is exactly the gap between
        ``restore_store`` releasing the store's lease and this adapter's
        post-effect measurement of what it restored.
        """

        real = maintenance.restore_store
        self.restore_calls = 0

        def restore(*args, **kwargs):
            self.restore_calls += 1
            receipt = real(*args, **kwargs)
            aftermath()
            return receipt

        return mock.patch.object(adapter.maintenance, "restore_store", restore)

    def test_an_owner_lease_taken_when_the_restore_returns_is_unknown_not_a_refusal(self) -> None:
        created = self.created_backup()
        self.stack.add_task("A fact written after the backup")
        request = self.restore_request(created["backup_digest"])

        owner = contextlib.ExitStack()
        self.addCleanup(owner.close)
        self.enterContext(self._real_restore_then(
            lambda: owner.enter_context(Store(self.source).server_lease())
        ))
        with owner:
            result = adapter.run_operation(request)

        self.assertEqual(self.restore_calls, 1)
        # The archive's data is what the store now holds: the effect happened.
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["code"], "workspace_held_by_owner")
        self.assertTrue(result["attempted"])
        self.assertIsNone(result["store_schema_version"])
        self.assertIn(
            receipts.attempt_path(self.state, request["operation_id"]).name,
            self.state_entries(),
        )
        self.assertNotIn(
            receipts.completed_path(self.state, request["operation_id"]).name,
            self.state_entries(),
        )

        sentinel = "A fact written after the unknown restore"
        self.stack.add_task(sentinel)
        retry = adapter.run_operation(request)

        self.assertEqual(self.restore_calls, 1)
        self.assertEqual(retry["status"], "unknown")
        self.assertEqual(retry["code"], "restore_completion_unknown")
        self.assertTrue(retry["attempted"])
        self.assertEqual(self.workspace_titles(), sorted([TASK_TITLE, sentinel]))

    def test_a_post_effect_read_failure_is_unknown_and_still_attempted(self) -> None:
        created = self.created_backup()
        self.stack.add_task("A fact written after the backup")
        request = self.restore_request(created["backup_digest"])

        with contextlib.ExitStack() as denied:
            with self._real_restore_then(
                lambda: denied.enter_context(
                    mock.patch.object(adapter, "Store", side_effect=OSError("unreadable"))
                )
            ):
                result = adapter.run_operation(request)

        self.assertEqual(self.restore_calls, 1)
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["code"], "maintenance_io_unknown")
        self.assertTrue(result["attempted"])
        self.assertNotIn(
            receipts.completed_path(self.state, request["operation_id"]).name,
            self.state_entries(),
        )

    def test_a_post_effect_receipt_failure_never_reports_a_zero_effect_refusal(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])

        with mock.patch.object(
            receipts, "complete", side_effect=receipts.ReceiptUnavailable("no receipt")
        ):
            result = adapter.run_operation(request)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "receipt_io_unknown")
        self.assertTrue(result["attempted"])
        self.assertEqual(self.workspace_titles(), [TASK_TITLE])

    # ---- boundary -------------------------------------------------------

    def test_the_result_document_carries_no_path_token_or_document_body(self) -> None:
        created = self.created_backup()
        request = self.restore_request(created["backup_digest"])
        restored = adapter.run_operation(request)

        for result in (created, restored):
            body = json.dumps(result, sort_keys=True)
            for secret in (
                str(self.root),
                str(self.source),
                self.source.name,
                self.archive_path().name,
                self.workspace_id,
                TASK_TITLE,
                NOTE_TEXT,
                self.task["id"],
            ):
                self.assertNotIn(secret, body, secret)

    def test_the_result_document_has_exactly_the_declared_fields(self) -> None:
        expected = {
            "schema_version",
            "tool",
            "kind",
            "operation_id",
            "status",
            "code",
            *adapter.RESULT_FIELDS,
        }

        self.assertEqual(set(self.created_backup()), expected)
        self.assertEqual(set(adapter.run_operation({"kind": "nope"})), expected)

    def test_no_unrelated_directory_is_created_anywhere(self) -> None:
        created = self.created_backup()
        adapter.run_operation(self.backup_request("observe_backup"))
        adapter.run_operation(self.restore_request(created["backup_digest"]))

        self.assertEqual(sorted(os.listdir(self.root)), ["backups", "operations", "ssot-store-directory"])
        # The store's own writer-lease marker is still there: nothing here ever
        # removes a lock to get at the data behind it.
        self.assertTrue((self.source / maintenance.LOCK_NAME).is_file())

    def test_the_adapter_never_deletes_or_raw_copies_store_data(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")

        for forbidden in ("shutil", "unlink(", "rmtree", "os.remove", "copyfile", "copytree"):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertEqual(source.count("maintenance.backup_store("), 1)
        self.assertEqual(source.count("maintenance.restore_store("), 1)
        self.assertEqual(source.count("maintenance.verify_backup("), 1)
        for unsupported in ("relocate_store", "initialize_store", "create_backup_download"):
            self.assertNotIn(unsupported, source, unsupported)

    # ---- CLI ------------------------------------------------------------

    def _cli(self, request: object) -> tuple[int, dict]:
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(ADAPTER_PATH)],
            input=json.dumps(request).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            timeout=180,
        )
        return process.returncode, json.loads(process.stdout.decode("utf-8"))

    def test_the_cli_reads_one_stdin_request_and_prints_one_result(self) -> None:
        code, result = self._cli(self.backup_request())

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["backup_digest"], maintenance.verify_backup(self.archive_path()).digest)

    def test_the_cli_exits_non_zero_on_a_refused_request(self) -> None:
        request = self.backup_request()
        request["unexpected"] = 1

        code, result = self._cli(request)

        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "request_key_unknown")

    def test_an_oversized_request_is_refused(self) -> None:
        request = self.backup_request()
        request["workspace_dir"] = "/" + "a" * (adapter.MAX_REQUEST_BYTES + 16)

        code, result = self._cli(request)

        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
