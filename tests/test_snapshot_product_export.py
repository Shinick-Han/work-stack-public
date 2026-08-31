from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from workstack.cli import main as cli_main
from workstack.service import (
    SnapshotDisclosureRequiredError,
    SnapshotExportConflictError,
    SnapshotExportRefusedError,
    SnapshotStoreNotReadyError,
    WorkStack,
)
from workstack.snapshot import canonical_snapshot_bytes, snapshot_digest
from workstack.snapshot_export import write_snapshot_file
from workstack.server import create_server
from workstack.store import DEFAULTS, Store


class SnapshotProductExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        objective = self.stack.add_objective("Keep execution aligned")
        self.task = self.stack.add_task(
            "Prepare deterministic handoff",
            "Review the exact snapshot before carrying it to Conduit.",
            "P1",
            "2026-09-02",
            ["private-tag"],
            [objective["id"]],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def persisted_bytes(self) -> dict[str, bytes | None]:
        paths = {name: self.store.path(name) for name in DEFAULTS}
        paths["journal"] = self.store.journal_path
        return {
            name: path.read_bytes() if path.exists() else None
            for name, path in paths.items()
        }

    def test_preview_is_exact_deterministic_and_read_only_across_restart(self) -> None:
        before = self.persisted_bytes()
        artifact = self.stack.planning_snapshot(self.task["id"])

        self.assertEqual(artifact.filename, self.task["uid"] + ".workstack-task.json")
        self.assertEqual(artifact.snapshot["title"], self.task["title"])
        self.assertEqual(artifact.snapshot["detail"], self.task["detail"])
        self.assertEqual(artifact.snapshot["revision"], self.task["revision"])
        self.assertEqual(
            artifact.omissions,
            ("objectives", "dependencies", "subtasks", "notes", "tags"),
        )
        self.assertEqual(artifact.canonical_bytes, canonical_snapshot_bytes(artifact.snapshot))
        self.assertEqual(artifact.digest, snapshot_digest(artifact.canonical_bytes))
        self.assertEqual(self.persisted_bytes(), before)

        restarted = WorkStack(Store(self.root)).planning_snapshot(self.task["id"])
        self.assertEqual(restarted.canonical_bytes, artifact.canonical_bytes)
        self.assertEqual(restarted.digest, artifact.digest)
        self.assertEqual(self.persisted_bytes(), before)

    def test_internal_scheduling_fields_do_not_enter_frozen_snapshot(self) -> None:
        updated = self.stack.patch_task(
            self.task["id"],
            {"scheduled": "2026-09-02", "estimate_minutes": 90, "revision": 0},
        )
        artifact = self.stack.planning_snapshot(updated["id"])
        self.assertNotIn("scheduled", artifact.snapshot)
        self.assertNotIn("estimate_minutes", artifact.snapshot)
        self.assertEqual(
            set(artifact.snapshot),
            {
                "format", "workspace_uid", "planning_task_uid", "legacy_task_id",
                "revision", "title", "detail", "planning_status", "planning_priority",
                "due_date", "origin_ref",
            },
        )

    def test_confirmation_and_stale_review_refuse_without_mutation(self) -> None:
        preview = self.stack.planning_snapshot(self.task["id"])
        before = self.persisted_bytes()
        with self.assertRaises(SnapshotDisclosureRequiredError):
            self.stack.confirmed_snapshot_export(
                self.task["id"], preview.snapshot["revision"], preview.digest, False
            )
        with self.assertRaises(SnapshotExportConflictError):
            self.stack.confirmed_snapshot_export(
                self.task["id"], preview.snapshot["revision"] + 1, preview.digest, True
            )
        with self.assertRaises(SnapshotExportConflictError):
            self.stack.confirmed_snapshot_export(
                self.task["id"], preview.snapshot["revision"], "sha256:" + "0" * 64, True
            )
        self.assertEqual(self.persisted_bytes(), before)
        confirmed = self.stack.confirmed_snapshot_export(
            self.task["id"], preview.snapshot["revision"], preview.digest, True
        )
        self.assertEqual(confirmed.canonical_bytes, preview.canonical_bytes)
        self.assertEqual(self.persisted_bytes(), before)

        updated = self.stack.patch_task(
            self.task["id"],
            {
                "detail": "The committed planning detail changed.",
                "revision": preview.snapshot["revision"],
            },
        )
        changed = self.stack.planning_snapshot(self.task["id"])
        self.assertEqual(changed.snapshot["revision"], updated["revision"])
        self.assertNotEqual(changed.canonical_bytes, preview.canonical_bytes)
        self.assertNotEqual(changed.digest, preview.digest)
        after_edit = self.persisted_bytes()
        with self.assertRaises(SnapshotExportConflictError):
            self.stack.confirmed_snapshot_export(
                self.task["id"], preview.snapshot["revision"], preview.digest, True
            )
        self.assertEqual(self.persisted_bytes(), after_edit)

    def test_privacy_refusal_is_diagnostic_only_and_read_only(self) -> None:
        unsafe = self.stack.add_task(
            "Unsafe handoff candidate",
            "Authorization: Bearer " + "a" * 24,
        )
        before = self.persisted_bytes()
        with self.assertRaises(SnapshotExportRefusedError) as refusal:
            self.stack.planning_snapshot(unsafe["id"])
        self.assertEqual(refusal.exception.code, "SNAPSHOT_CREDENTIAL_SUSPECTED")
        self.assertNotIn("a" * 24, str(refusal.exception))
        self.assertNotIn("a" * 24, json.dumps(refusal.exception.details))
        self.assertEqual(self.persisted_bytes(), before)

    def test_pending_recovery_is_refused_without_replay_or_repair(self) -> None:
        target = json.loads(self.store.path("notes.json").read_text(encoding="utf-8"))
        target["notes"].append({"id": "N-9999", "text": "must not replay", "links": []})
        compact = json.dumps(
            target, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        journal = {
            "version": 1,
            "operation_id": "snapshot-must-not-recover",
            "created_at": "2026-08-30T00:00:00Z",
            "writes": [
                {
                    "name": "notes.json",
                    "value": target,
                    "sha256": "sha256:" + hashlib.sha256(compact).hexdigest(),
                }
            ],
        }
        self.store.journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        before = self.persisted_bytes()

        with self.assertRaises(SnapshotStoreNotReadyError):
            self.stack.planning_snapshot(self.task["id"])

        self.assertEqual(self.persisted_bytes(), before)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli_main([
                "--data-dir",
                str(self.root),
                "snapshot",
                "preview",
                self.task["id"],
            ])
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("SNAPSHOT_STORE_NOT_READY", stderr.getvalue())
        self.assertEqual(self.persisted_bytes(), before)

    def test_file_delivery_is_exclusive_and_cleans_partial_write(self) -> None:
        artifact = self.stack.planning_snapshot(self.task["id"])
        output = self.root / artifact.filename
        write_snapshot_file(output, artifact.canonical_bytes)
        self.assertEqual(output.read_bytes(), artifact.canonical_bytes)

        with self.assertRaises(FileExistsError):
            write_snapshot_file(output, b"replacement\n")
        self.assertEqual(output.read_bytes(), artifact.canonical_bytes)
        self.assertEqual(list(self.root.glob(".*.workstack-export.tmp")), [])

        failed = self.root / ("f" * 120 + ".workstack-task.json")
        with mock.patch.object(os, "link", side_effect=OSError("injected publish failure")):
            with self.assertRaisesRegex(OSError, "injected publish failure"):
                write_snapshot_file(failed, artifact.canonical_bytes)
        self.assertFalse(failed.exists())
        self.assertEqual(list(self.root.glob(".*.workstack-export.tmp")), [])

        too_long = self.root / ("l" * 260 + ".workstack-task.json")
        with self.assertRaises(OSError):
            write_snapshot_file(too_long, artifact.canonical_bytes)
        self.assertFalse(too_long.exists())
        self.assertEqual(list(self.root.glob(".*.workstack-export.tmp")), [])

    def test_cli_requires_a_review_coordinate_and_explicit_confirmation(self) -> None:
        artifact = self.stack.planning_snapshot(self.task["id"])
        output = self.root.parent / (self.task["uid"] + ".workstack-task.json")
        output.unlink(missing_ok=True)
        before = self.persisted_bytes()
        common = [
            "--data-dir",
            str(self.root),
            "snapshot",
            "export",
            self.task["id"],
            "--out",
            str(output),
            "--expected-revision",
            str(artifact.snapshot["revision"]),
            "--expected-digest",
            artifact.digest,
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(common), 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.persisted_bytes(), before)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(common + ["--confirm-disclosure"]), 0)
        self.assertEqual(output.read_bytes(), artifact.canonical_bytes)
        self.assertEqual(self.persisted_bytes(), before)
        output.unlink()


class SnapshotLoopbackExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Review exact bytes", "No live link is created.")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def persisted_bytes(self) -> dict[str, bytes | None]:
        return {
            name: path.read_bytes() if path.exists() else None
            for name, path in {
                **{name: self.store.path(name) for name in DEFAULTS},
                "journal": self.store.journal_path,
            }.items()
        }

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        outgoing_headers = dict(headers or {})
        if encoded is not None:
            outgoing_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.actual_port, timeout=5
        )
        try:
            connection.request(method, path, encoded, outgoing_headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = {
                key.casefold(): value for key, value in response.getheaders()
            }
            return response.status, raw, response_headers
        finally:
            connection.close()

    def browser_headers(self) -> dict[str, str]:
        status, raw, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        token = json.loads(raw)["data"]["csrf_token"]
        return {
            "Origin": "http://127.0.0.1:{}".format(self.server.actual_port),
            "X-WorkStack-CSRF": token,
        }

    def test_preview_then_confirmed_download_delivers_only_exact_bytes(self) -> None:
        before = self.persisted_bytes()
        path = "/api/v1/tasks/{}/snapshot".format(self.task["id"])
        status, raw, _ = self.request("GET", path)
        self.assertEqual(status, 200)
        preview = json.loads(raw)["data"]
        self.assertEqual(preview["snapshot"]["title"], self.task["title"])
        self.assertEqual(preview["snapshot"]["detail"], self.task["detail"])
        self.assertEqual(
            preview["omissions"],
            ["objectives", "dependencies", "subtasks", "notes", "tags"],
        )
        self.assertEqual(self.persisted_bytes(), before)

        export_path = path + "/export"
        status, refused, _ = self.request(
            "POST",
            export_path,
            {
                "disclosure_confirmed": False,
                "expected_revision": preview["snapshot"]["revision"],
                "expected_digest": preview["digest"],
            },
            self.browser_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(refused)["error"]["code"], "snapshot_disclosure_required"
        )
        self.assertEqual(self.persisted_bytes(), before)

        status, delivered, headers = self.request(
            "POST",
            export_path,
            {
                "disclosure_confirmed": True,
                "expected_revision": preview["snapshot"]["revision"],
                "expected_digest": preview["digest"],
            },
            self.browser_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(delivered, canonical_snapshot_bytes(preview["snapshot"]))
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(headers["x-workstack-snapshot-digest"], preview["digest"])
        self.assertEqual(
            headers["content-disposition"],
            'attachment; filename="{}"'.format(preview["filename"]),
        )
        self.assertEqual(self.persisted_bytes(), before)

if __name__ == "__main__":
    unittest.main()
