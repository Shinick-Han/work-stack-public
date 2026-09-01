from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from workstack.service import WorkStack
from workstack.server import create_server
from workstack.store import (
    DEFAULTS,
    Store,
    StoreCorruptError,
    StoreExternalChangeError,
)


def sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class WorkspaceRebindRecoveryRedTest(unittest.TestCase):
    """RED contract for an explicit, authority-local workspace rebind."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "configured-ssot"
        self.runtime = self.base / "authority-runtime"
        self.environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.runtime)}
        )
        self.environment.start()
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.stack.add_task("Old workspace planning state")
        self.old_workspace_id = self.stack.workspace_projection()["workspace"]["id"]
        self.old_manifest = self.store.store_manifest_path.read_bytes()
        self.old_manifest_digest = str(self.store.sync_status()["manifest_digest"])

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def replace_configured_path_with_another_valid_workspace(self) -> str:
        replacement_root = self.base / "replacement-source"
        replacement = Store(replacement_root)
        replacement_stack = WorkStack(replacement)
        replacement_stack.add_task("Replacement workspace planning state")
        workspace_id = replacement_stack.workspace_projection()["workspace"]["id"]
        self.assertNotEqual(workspace_id, self.old_workspace_id)
        for name in DEFAULTS:
            self.store.path(name).write_bytes(replacement.path(name).read_bytes())
        return workspace_id

    def authoritative_bodies(self) -> dict[str, bytes]:
        return {name: self.store.path(name).read_bytes() for name in sorted(DEFAULTS)}

    def preview(self, candidate_workspace_id: str) -> dict[str, object]:
        status = self.store.sync_status()
        self.assertEqual(status["state"], "invalid")
        self.assertEqual(status["workspace_id"], self.old_workspace_id)
        with self.assertRaises(StoreExternalChangeError):
            self.stack.add_task("Restart must not accept the replacement")

        preview = self.store.workspace_rebind_preview()
        self.assertEqual(
            preview["state"], "workspace-identity-mismatch"
        )
        self.assertEqual(preview["manifest_workspace_id"], self.old_workspace_id)
        self.assertEqual(preview["candidate_workspace_id"], candidate_workspace_id)
        self.assertEqual(preview["manifest_digest"], self.old_manifest_digest)
        self.assertRegex(preview["candidate_digest"], r"^sha256:[0-9a-f]{64}$")
        return preview

    def rebind(
        self,
        preview: dict[str, object],
        *,
        confirmed: bool = True,
        previous_workspace_id: str | None = None,
        candidate_workspace_id: str | None = None,
        candidate_digest: str | None = None,
        idempotency_key: str = "workspace.rebind.red.0001",
    ) -> dict[str, object]:
        return self.store.rebind_workspace_identity(
            confirmed=confirmed,
            expected_manifest_workspace_id=(
                previous_workspace_id or str(preview["manifest_workspace_id"])
            ),
            expected_candidate_workspace_id=(
                candidate_workspace_id or str(preview["candidate_workspace_id"])
            ),
            expected_manifest_digest=str(preview["manifest_digest"]),
            expected_candidate_digest=(
                candidate_digest or str(preview["candidate_digest"])
            ),
            idempotency_key=idempotency_key,
        )

    def test_same_path_replacement_requires_the_separate_explicit_action(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)

        with self.assertRaises(StoreExternalChangeError):
            self.store.adopt_external_change(
                int(self.store.sync_status()["generation"]),
                str(self.store.sync_status()["manifest_digest"]),
                "ordinary.adoption.must.not.rebind",
            )

        result = self.rebind(preview)

        self.assertEqual(result["state"], "in-sync")
        self.assertEqual(result["workspace_id"], candidate_workspace_id)
        self.assertFalse(result["recovery"]["planning_mutated"])

    def test_success_preserves_every_planning_byte_and_writes_verifiable_evidence(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        before = self.authoritative_bodies()

        result = self.rebind(preview)

        self.assertEqual(self.authoritative_bodies(), before)
        recovery = result["recovery"]
        backup_path = Path(recovery["backup_path"])
        receipt_path = Path(recovery["receipt_path"])
        quarantine_path = Path(recovery["quarantined_manifest_path"])
        for artifact in (backup_path, receipt_path, quarantine_path):
            self.assertTrue(artifact.is_relative_to(self.store.runtime_root))
            self.assertTrue(artifact.is_file())
            self.assertFalse(artifact.is_relative_to(self.store.root))

        self.assertEqual(sha256(backup_path.read_bytes()), recovery["backup_digest"])
        self.assertEqual(quarantine_path.read_bytes(), self.old_manifest)
        self.assertEqual(
            sha256(quarantine_path.read_bytes()), recovery["quarantined_manifest_digest"]
        )
        with zipfile.ZipFile(backup_path) as archive:
            for name, body in before.items():
                self.assertEqual(archive.read(name), body)

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["operation"], "workspace-rebind")
        self.assertEqual(receipt["previous_workspace_id"], self.old_workspace_id)
        self.assertEqual(receipt["candidate_workspace_id"], candidate_workspace_id)
        self.assertEqual(receipt["candidate_digest"], preview["candidate_digest"])
        self.assertFalse(receipt["planning_mutated"])
        self.assertNotIn("Old workspace planning state", receipt_path.read_text(encoding="utf-8"))
        self.assertNotIn("Replacement workspace planning state", receipt_path.read_text(encoding="utf-8"))

    def test_wrong_identity_or_unconfirmed_intent_remains_fail_closed(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        before = self.authoritative_bodies()
        manifest_before = self.store.store_manifest_path.read_bytes()

        attempts = (
            {"confirmed": False},
            {"previous_workspace_id": "00000000-0000-4000-8000-000000000001"},
            {"candidate_workspace_id": "00000000-0000-4000-8000-000000000002"},
            {"candidate_digest": "sha256:" + "0" * 64},
        )
        for index, overrides in enumerate(attempts):
            with self.subTest(overrides=overrides), self.assertRaises(
                (ValueError, StoreExternalChangeError)
            ):
                self.rebind(
                    preview,
                    idempotency_key=f"workspace.rebind.refused.{index:04d}",
                    **overrides,
                )

        self.assertEqual(self.store.store_manifest_path.read_bytes(), manifest_before)
        self.assertEqual(self.authoritative_bodies(), before)
        self.assertEqual(self.store.sync_status()["state"], "invalid")

    def test_candidate_changed_after_review_is_not_rebound(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        notes_path = self.store.path("notes.json")
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-raced", "title": "Race", "body": "changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        raced = self.authoritative_bodies()

        with self.assertRaises(StoreExternalChangeError):
            self.rebind(preview)

        self.assertEqual(self.authoritative_bodies(), raced)
        self.assertEqual(self.store.sync_status()["state"], "invalid")
        self.assertEqual(self.store.store_manifest_path.read_bytes(), self.old_manifest)

    def test_candidate_changed_at_manifest_replace_never_reports_success(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        notes_path = self.store.path("notes.json")
        reviewed = self.authoritative_bodies()
        original_atomic_write = self.store._atomic_write_locked
        raced: dict[str, bytes] = {}

        def mutate_at_manifest_replace(path: Path, value: object) -> None:
            if path == self.store.store_manifest_path and not raced:
                notes = json.loads(notes_path.read_text(encoding="utf-8"))
                notes["notes"].append(
                    {"id": "N-boundary", "title": "Boundary", "body": "external"}
                )
                notes_path.write_text(json.dumps(notes), encoding="utf-8")
                raced.update(self.authoritative_bodies())
            original_atomic_write(path, value)

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=mutate_at_manifest_replace
        ), self.assertRaises(StoreExternalChangeError):
            self.rebind(preview, idempotency_key="workspace.rebind.boundary.0001")

        self.assertTrue(raced)
        self.assertNotEqual(raced, reviewed)
        self.assertEqual(self.authoritative_bodies(), raced)
        self.assertEqual(self.store.sync_status()["state"], "external-change-detected")
        with self.assertRaises(StoreExternalChangeError):
            self.rebind(preview, idempotency_key="workspace.rebind.boundary.0001")

    def test_replay_rejects_incomplete_or_duplicate_authoritative_file_evidence(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        self.rebind(preview, idempotency_key="workspace.rebind.receipt-files.0001")
        receipt_path = self.store.sync_rebind_receipt_path
        original = json.loads(receipt_path.read_text(encoding="utf-8"))

        invalid_file_sets = (
            original["authoritative_files"][:-1],
            original["authoritative_files"] + [original["authoritative_files"][0]],
        )
        for authoritative_files in invalid_file_sets:
            with self.subTest(file_count=len(authoritative_files)):
                changed = dict(original)
                changed["authoritative_files"] = authoritative_files
                receipt_path.write_text(json.dumps(changed), encoding="utf-8")
                restarted = Store(self.root)
                with self.assertRaises(StoreCorruptError):
                    restarted.rebind_workspace_identity(
                        confirmed=True,
                        expected_manifest_workspace_id=self.old_workspace_id,
                        expected_candidate_workspace_id=candidate_workspace_id,
                        expected_manifest_digest=str(preview["manifest_digest"]),
                        expected_candidate_digest=str(preview["candidate_digest"]),
                        idempotency_key="workspace.rebind.receipt-files.0001",
                    )

        receipt_path.write_text(json.dumps(original), encoding="utf-8")

    def test_replay_rejects_missing_or_corrupt_recovery_artifacts(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        result = self.rebind(
            preview, idempotency_key="workspace.rebind.artifacts.0001"
        )
        coordinate = {
            "confirmed": True,
            "expected_manifest_workspace_id": self.old_workspace_id,
            "expected_candidate_workspace_id": candidate_workspace_id,
            "expected_manifest_digest": str(preview["manifest_digest"]),
            "expected_candidate_digest": str(preview["candidate_digest"]),
            "idempotency_key": "workspace.rebind.artifacts.0001",
        }
        artifact_paths = (
            Path(result["recovery"]["backup_path"]),
            Path(result["recovery"]["quarantined_manifest_path"]),
        )
        for artifact_path in artifact_paths:
            original = artifact_path.read_bytes()
            for mutation in ("missing", "corrupt"):
                with self.subTest(artifact=artifact_path.name, mutation=mutation):
                    if mutation == "missing":
                        artifact_path.unlink()
                    else:
                        artifact_path.write_bytes(original + b"corrupt")
                    restarted = Store(self.root)
                    with self.assertRaises(StoreCorruptError):
                        restarted.rebind_workspace_identity(**coordinate)
                    artifact_path.write_bytes(original)

    def test_replay_validates_backup_and_quarantine_expected_bytes(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        result = self.rebind(
            preview, idempotency_key="workspace.rebind.expected-bytes.0001"
        )
        receipt_path = self.store.sync_rebind_receipt_path
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        backup_path = Path(result["recovery"]["backup_path"])
        quarantine_path = Path(result["recovery"]["quarantined_manifest_path"])
        original_backup = backup_path.read_bytes()
        coordinate = {
            "confirmed": True,
            "expected_manifest_workspace_id": self.old_workspace_id,
            "expected_candidate_workspace_id": candidate_workspace_id,
            "expected_manifest_digest": str(preview["manifest_digest"]),
            "expected_candidate_digest": str(preview["candidate_digest"]),
            "idempotency_key": "workspace.rebind.expected-bytes.0001",
        }

        changed_backup = self._backup_with_replaced_member(
            backup_path.read_bytes(), "notes.json", b'{"version":1,"notes":[]}'
        )
        backup_path.write_bytes(changed_backup)
        receipt["backup_digest"] = sha256(changed_backup)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(StoreCorruptError):
            Store(self.root).rebind_workspace_identity(**coordinate)

        backup_path.write_bytes(original_backup)
        receipt["backup_digest"] = sha256(original_backup)
        changed_quarantine = json.dumps(
            {"version": 1, "workspace_id": self.old_workspace_id}
        ).encode("utf-8")
        quarantine_path.write_bytes(changed_quarantine)
        receipt["quarantined_manifest_digest"] = sha256(changed_quarantine)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(StoreCorruptError):
            Store(self.root).rebind_workspace_identity(**coordinate)

    @staticmethod
    def _backup_with_replaced_member(body: bytes, name: str, replacement: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(body)) as source:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for member in source.namelist():
                    archive.writestr(
                        member, replacement if member == name else source.read(member)
                    )
        return output.getvalue()

    def test_completed_rebind_survives_restart_and_exact_retry(self) -> None:
        candidate_workspace_id = self.replace_configured_path_with_another_valid_workspace()
        preview = self.preview(candidate_workspace_id)
        first = self.rebind(preview, idempotency_key="workspace.rebind.restart.0001")
        before_restart = self.authoritative_bodies()

        restarted = Store(self.root)
        restarted_stack = WorkStack(restarted)
        self.assertEqual(restarted.sync_status()["state"], "in-sync")
        self.assertEqual(
            restarted_stack.workspace_projection()["workspace"]["id"],
            candidate_workspace_id,
        )
        replay = restarted.rebind_workspace_identity(
            confirmed=True,
            expected_manifest_workspace_id=self.old_workspace_id,
            expected_candidate_workspace_id=candidate_workspace_id,
            expected_manifest_digest=str(preview["manifest_digest"]),
            expected_candidate_digest=str(preview["candidate_digest"]),
            idempotency_key="workspace.rebind.restart.0001",
        )
        self.assertEqual(replay, first)
        self.assertEqual(self.authoritative_bodies(), before_restart)

    def test_recovery_artifacts_are_authority_local_in_local_and_remote_modes(self) -> None:
        # The Store contract runs where the authoritative server runs. Naming the
        # authority models local and SSH-remote placement without pretending the
        # Windows desktop can rewrite a remote runtime directory.
        for authority_kind in ("local", "ssh-remote"):
            with self.subTest(authority_kind=authority_kind), tempfile.TemporaryDirectory() as directory:
                authority = Path(directory)
                runtime = authority / f"{authority_kind}-runtime"
                root = authority / "ssot"
                with mock.patch.dict(os.environ, {"WORK_STACK_RUNTIME": str(runtime)}):
                    store = Store(root)
                    stack = WorkStack(store)
                    old_id = stack.workspace_projection()["workspace"]["id"]
                    old_manifest_digest = str(store.sync_status()["manifest_digest"])
                    replacement = Store(authority / "replacement")
                    replacement_stack = WorkStack(replacement)
                    candidate_id = replacement_stack.workspace_projection()["workspace"]["id"]
                    for name in DEFAULTS:
                        store.path(name).write_bytes(replacement.path(name).read_bytes())
                    preview = store.workspace_rebind_preview()
                    result = store.rebind_workspace_identity(
                        confirmed=True,
                        expected_manifest_workspace_id=old_id,
                        expected_candidate_workspace_id=candidate_id,
                        expected_manifest_digest=old_manifest_digest,
                        expected_candidate_digest=str(preview["candidate_digest"]),
                        idempotency_key=f"workspace.rebind.{authority_kind}.0001",
                    )

                self.assertTrue(Path(result["recovery"]["receipt_path"]).is_relative_to(runtime))
                self.assertFalse(Path(result["recovery"]["receipt_path"]).is_relative_to(root))


class WorkspaceRebindApiRedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.base / "runtime")}
        )
        self.environment.start()
        self.root = self.base / "configured-ssot"
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.old_workspace_id = self.stack.workspace_projection()["workspace"]["id"]
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        replacement = Store(self.base / "replacement")
        replacement_stack = WorkStack(replacement)
        replacement_stack.add_task("API replacement planning state")
        self.candidate_workspace_id = replacement_stack.workspace_projection()["workspace"]["id"]
        for name in DEFAULTS:
            self.store.path(name).write_bytes(replacement.path(name).read_bytes())
        self.candidate_bodies = {
            name: self.store.path(name).read_bytes() for name in sorted(DEFAULTS)
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.environment.stop()
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.actual_port, timeout=20
        )
        raw = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        connection.request(method, path, body=raw, headers=headers or {})
        response = connection.getresponse()
        status = response.status
        payload = json.loads(response.read())
        connection.close()
        return status, payload

    def test_preview_and_confirmed_action_are_content_free_same_origin_routes(self) -> None:
        preview_status, preview_envelope = self.request("/api/v1/sync/rebind-preview")
        self.assertEqual(preview_status, 200)
        preview = preview_envelope["data"]
        self.assertEqual(preview["state"], "workspace-identity-mismatch")
        self.assertEqual(preview["manifest_workspace_id"], self.old_workspace_id)
        self.assertEqual(preview["candidate_workspace_id"], self.candidate_workspace_id)
        self.assertNotIn("API replacement planning state", json.dumps(preview_envelope))

        _, session = self.request("/api/v1/session")
        csrf = session["data"]["csrf_token"]
        action_status, action_envelope = self.request(
            "/api/v1/sync/rebind-workspace",
            method="POST",
            body={
                "confirmed": True,
                "expected_manifest_workspace_id": preview["manifest_workspace_id"],
                "expected_candidate_workspace_id": preview["candidate_workspace_id"],
                "expected_manifest_digest": preview["manifest_digest"],
                "expected_candidate_digest": preview["candidate_digest"],
            },
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.actual_port}",
                "X-WorkStack-CSRF": csrf,
                "Idempotency-Key": "workspace.rebind.api.0001",
            },
        )

        self.assertEqual(action_status, 200)
        result = action_envelope["data"]
        self.assertEqual(
            set(result),
            {
                "state",
                "workspace_id",
                "generation",
                "recovery_receipt_digest",
                "planning_mutated",
            },
        )
        self.assertEqual(result["state"], "in-sync")
        self.assertEqual(result["workspace_id"], self.candidate_workspace_id)
        self.assertFalse(result["planning_mutated"])
        self.assertRegex(result["recovery_receipt_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("API replacement planning state", json.dumps(action_envelope))
        self.assertEqual(
            {name: self.store.path(name).read_bytes() for name in sorted(DEFAULTS)},
            self.candidate_bodies,
        )


if __name__ == "__main__":
    unittest.main()
