from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from workstack.storage.experimental_application import (
    ExperimentalV4ApplicationError,
    create_experimental_v4_application,
)
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.store import StoreLockedError
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.checkpoint_change import build_checkpoint_facts


NOW = "2026-09-01T12:00:00Z"
CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


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
            write(
                f"records/{kind}/{uid[:2]}/{uid}.json",
                canonical_json_bytes(dict(record)),
            )
    segments: dict[tuple[str, str], list[dict]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            segments.setdefault((kind, str(event["created_at"])[:7]), []).append(
                dict(event)
            )
    for (kind, month), events in sorted(segments.items()):
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        )
        write(f"streams/{kind}/{month}.ndjson", body)


def _authority_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class ExperimentalV4HTTPCanaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        legacy = WorkStack(Store(self.base / "v3"))
        with mock.patch("workstack.service.utc_now", return_value=NOW), mock.patch(
            "workstack.service.today", return_value=NOW[:10]
        ):
            legacy.add_task("HTTP canary task")
        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at=NOW
        )
        self.authority = self.base / "authority"
        self.authority.mkdir()
        _write_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority,
            self.base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True)
        manifest = build_v4_manifest(read_v4(self.authority), generation=0)
        publish_runtime_manifest(
            self.runtime.manifest_path, manifest, expected_digest=None
        )
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        self._stop_server()
        self.temporary.cleanup()

    def _application(self):
        return create_experimental_v4_application(
            self.authority,
            self.runtime,
            enable_v4_application=True, checkpoint_facts=build_checkpoint_facts,
            clock=lambda: NOW,
            uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            today=lambda: NOW[:10],
            task_note_source_indexes=self.conversion.task_note_source_indexes,
        )

    @staticmethod
    def _stack(application):
        return WorkStack(
            application.store,
            initialize=False,
            capture_reply_commands=application.domain.capture_reply,
            intent_commands=application.domain.intents,
            objective_commands=application.domain.objectives,
            task_commands=application.domain.tasks,
        )

    def _start_server(self, application):
        stack = self._stack(application)
        self.server = create_server(stack, "127.0.0.1", 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def _stop_server(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = None
        self.thread = None

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        actual_headers = dict(headers or {})
        if payload is not None:
            actual_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.actual_port, timeout=5
        )
        connection.request(method, path, body=payload, headers=actual_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def _browser_headers(self, key: str) -> dict[str, str]:
        status, session = self._request("GET", "/api/v1/session")
        self.assertEqual(200, status)
        return {
            "Origin": f"http://127.0.0.1:{self.server.actual_port}",
            "X-WorkStack-CSRF": session["data"]["csrf_token"],
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        }

    def test_http_reads_capture_mutation_refresh_and_restart_persistence(self) -> None:
        application = self._application()
        before_reads = _authority_digest(self.authority)
        self._start_server(application)

        self.assertEqual(
            (200, {"data": {"api_version": "v1", "status": "ready"}}),
            self._request("GET", "/api/v1/health"),
        )
        status, workspace = self._request("GET", "/api/v1/workspace")
        self.assertEqual(200, status)
        self.assertEqual("HTTP canary task", workspace["data"]["tasks"][0]["title"])
        status, search = self._request("GET", "/api/v1/search?q=canary")
        self.assertEqual(200, status)
        self.assertEqual("T-0001", search["data"]["items"][0]["id"])
        self.assertEqual(before_reads, _authority_digest(self.authority))

        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        status, created = self._request(
            "POST",
            "/api/v1/captures",
            packet,
            self._browser_headers("v4.http.capture.0001"),
        )
        self.assertEqual(201, status)
        capture_id = created["data"]["id"]
        self.assertEqual(1, application.store.generation)
        status, refreshed = self._request("GET", "/api/v1/workspace")
        self.assertEqual(200, status)
        self.assertEqual(1, refreshed["data"]["inbox_count"])

        self._stop_server()
        self.assertFalse(application.store.capture_token_path.exists())
        self.assertFalse(application.store.server_info_path.exists())
        restarted = self._application()
        self._start_server(restarted)
        status, captures = self._request("GET", "/api/v1/captures")
        self.assertEqual(200, status)
        self.assertEqual([capture_id], [item["id"] for item in captures["data"]])
        self.assertEqual(1, restarted.store.generation)

    def test_application_factory_is_default_off_without_touch(self) -> None:
        missing = self.base / "missing-authority"
        with mock.patch(
            "workstack.storage.experimental_application.compose_experimental_v4_domain"
        ) as compose:
            with self.assertRaises(ExperimentalV4ApplicationError) as caught:
                create_experimental_v4_application(
                    missing,
                    None,
                    clock=lambda: NOW,
                    uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                )
        self.assertEqual("V4_APPLICATION_OPT_IN_REQUIRED", caught.exception.code)
        compose.assert_not_called()
        self.assertFalse(missing.exists())

    def test_server_runtime_files_cleanup_and_single_writer_lease(self) -> None:
        application = self._application()
        self._start_server(application)
        self.assertIs(
            application.domain.capture_reply,
            self.server.stack.capture_reply_commands,
        )
        self.assertEqual(
            self.server.capture_token,
            application.store.capture_token_path.read_text(encoding="utf-8").strip(),
        )
        info = json.loads(
            application.store.server_info_path.read_text(encoding="utf-8")
        )
        self.assertEqual("127.0.0.1", info["host"])
        self.assertEqual(self.server.actual_port, info["port"])
        competing = self._application()
        with self.assertRaises(StoreLockedError):
            create_server(self._stack(competing), "127.0.0.1", 0)
        self._stop_server()
        self.assertFalse(application.store.capture_token_path.exists())
        self.assertFalse(application.store.server_info_path.exists())


if __name__ == "__main__":
    unittest.main()
