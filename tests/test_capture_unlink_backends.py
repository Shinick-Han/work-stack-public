from __future__ import annotations

import copy
import http.client
import json
import os
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest import mock

from workstack.capture_unlink_receipt import (
    EVENT_TYPE as UNLINK_RECEIPT_EVENT,
    capture_row_digest,
    serialize_receipt,
    validate_receipt,
)
from workstack.server import create_server
from workstack.service import (
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    RevisionConflictError,
    RevisionExhaustedError,
)
from workstack.storage.capture_reply_repository import CaptureReplyRepositoryError
from workstack.store import MAX_REVISION

from tests.test_storage_capture_reply_contract import PACKET_FIXTURE, V4CaptureReplyBackend
from tests.test_service_capture_reply_backend import ServiceHarness


class CaptureUnlinkBackendParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.runtime_environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.base / "runtime")}
        )
        self.runtime_environment.start()
        self.packet = json.loads(PACKET_FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.runtime_environment.stop()
        self.temporary.cleanup()

    def harnesses(self):
        return (
            ServiceHarness(self.base / "one", 3),
            ServiceHarness(self.base / "two", 4),
        )

    def test_public_unlink_shares_v3_mixin_and_v4_repository_oracles(self) -> None:
        for harness in self.harnesses():
            with self.subTest(version=harness.version):
                ingested = harness.stack.ingest_capture(
                    copy.deepcopy(self.packet),
                    f"unlink.parity.ingest.v{harness.version}",
                )
                capture = ingested["body"]["data"]
                linked = harness.stack.link_capture(
                    capture["id"],
                    harness.task["id"],
                    f"unlink.parity.link.v{harness.version}",
                )["body"]["data"]
                first = harness.stack.unlink_capture(
                    capture["id"],
                    harness.task["id"],
                    linked["revision"],
                    f"unlink.parity.drop.v{harness.version}",
                )
                self.assertEqual(first["status"], 200)
                self.assertFalse(first["body"]["meta"]["duplicate"])
                self.assertEqual(first["body"]["data"]["linked_task_ids"], [])
                self.assertEqual(first["body"]["data"]["status"], "inbox")
                self.assertEqual(first["body"]["data"]["id"], capture["id"])
                self.assertEqual(first["body"]["data"]["source"], capture["source"])

                replay = harness.stack.unlink_capture(
                    capture["id"],
                    harness.task["id"],
                    linked["revision"],
                    f"unlink.parity.drop.v{harness.version}",
                )
                self.assertTrue(replay["body"]["meta"]["replayed"])
                self.assertEqual(
                    replay["body"]["data"]["revision"], first["body"]["data"]["revision"]
                )

                duplicate = harness.stack.unlink_capture(
                    capture["id"],
                    harness.task["id"],
                    first["body"]["data"]["revision"],
                    f"unlink.parity.absent.v{harness.version}",
                )
                self.assertTrue(duplicate["body"]["meta"]["duplicate"])
                self.assertEqual(
                    duplicate["body"]["data"]["revision"], first["body"]["data"]["revision"]
                )

                with self.assertRaises(IdempotencyConflictError):
                    harness.stack.unlink_capture(
                        capture["id"],
                        harness.task["id"],
                        first["body"]["data"]["revision"],
                        f"unlink.parity.drop.v{harness.version}",
                    )
                with self.assertRaises(RevisionConflictError) as stale:
                    harness.stack.unlink_capture(
                        capture["id"],
                        harness.task["id"],
                        linked["revision"],
                        f"unlink.parity.stale.v{harness.version}",
                    )
                self.assertEqual(stale.exception.code, "revision_conflict")
                with self.assertRaises(NotFoundError):
                    harness.stack.unlink_capture(
                        capture["id"],
                        "T-9999",
                        first["body"]["data"]["revision"],
                        f"unlink.parity.missing.v{harness.version}",
                    )
                with self.assertRaises(DomainError) as invalid:
                    harness.stack.unlink_capture(
                        capture["id"],
                        harness.task["id"],
                        True,
                        f"unlink.parity.bool.v{harness.version}",
                    )
                self.assertEqual(invalid.exception.code, "invalid_request")

                documents = harness.documents()
                events = [
                    event
                    for event in documents["activity.json"]["activity"]
                    if event.get("type") == "capture.unlinked"
                ]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["capture_id"], capture["id"])
                self.assertEqual(events[0]["task_id"], harness.task["id"])
                stored = documents["captures.json"]["captures"][0]
                self.assertEqual(stored["status"], "inbox")
                self.assertEqual(stored["linked_task_ids"], [])
                self.assertEqual(stored["converted_task_ids"], [])

                receipt_id = first["body"]["meta"]["undo_receipt_id"]
                restored = harness.stack.undo_capture_unlink(
                    capture["id"],
                    receipt_id,
                    first["body"]["data"]["revision"],
                    f"unlink.parity.undo.v{harness.version}",
                )
                self.assertEqual(restored["status"], 200)
                self.assertFalse(restored["body"]["meta"]["duplicate"])
                self.assertEqual(restored["body"]["data"]["status"], "linked")
                self.assertEqual(restored["body"]["data"]["linked_task_ids"], [harness.task["id"]])
                replay_undo = harness.stack.undo_capture_unlink(
                    capture["id"],
                    receipt_id,
                    first["body"]["data"]["revision"],
                    f"unlink.parity.undo.v{harness.version}",
                )
                self.assertTrue(replay_undo["body"]["meta"]["replayed"])
                with self.assertRaises(RevisionConflictError):
                    harness.stack.undo_capture_unlink(
                        capture["id"],
                        receipt_id,
                        first["body"]["data"]["revision"],
                        f"unlink.parity.undo.stale.v{harness.version}",
                    )
                documents = harness.documents()
                undone = [
                    event
                    for event in documents["activity.json"]["activity"]
                    if event.get("type") == "capture.unlink_undone"
                ]
                self.assertEqual(len(undone), 1)
                self.assertFalse(undone[0].get("details"))
                receipts = [
                    event
                    for event in documents["activity.json"]["activity"]
                    if event.get("type") == "capture.unlink_receipt"
                ]
                self.assertEqual(len(receipts), 1)

    def test_v4_repository_unlink_is_reachable_without_the_service_facade(self) -> None:
        backend = V4CaptureReplyBackend(self.base / "v4-direct")
        capture = backend.ingest(copy.deepcopy(self.packet), "unlink.v4.ingest")["body"]["data"]
        linked = backend.link(capture["id"], backend.task["id"], "unlink.v4.link")["body"]["data"]
        removed = backend.repository.unlink_capture(
            capture["id"], backend.task["id"], linked["revision"], "unlink.v4.drop"
        )
        self.assertEqual(removed["status"], 200)
        self.assertEqual(removed["body"]["data"]["status"], "inbox")
        self.assertFalse(removed["body"]["meta"]["duplicate"])
        state = backend.state()
        self.assertEqual(state.captures[0]["linked_task_ids"], [])
        self.assertTrue(any(event["type"] == "capture.unlinked" for event in state.activity))
        self.assertIn("undo_receipt_id", removed["body"]["meta"])
        undone = backend.repository.undo_capture_unlink(
            capture["id"],
            removed["body"]["meta"]["undo_receipt_id"],
            removed["body"]["data"]["revision"],
            "unlink.v4.undo",
        )
        self.assertEqual(undone["status"], 200)
        self.assertEqual(undone["body"]["data"]["status"], "linked")
        self.assertEqual(backend.state().captures[0]["linked_task_ids"], [backend.task["id"]])

        def set_max(documents: dict[str, Any]) -> None:
            documents["captures.json"]["captures"][0]["revision"] = MAX_REVISION

        linked_again = backend.link(capture["id"], backend.task["id"], "unlink.v4.max.link")[
            "body"
        ]["data"]
        with _mutated_v4_load(backend.repository, set_max):
            with self.assertRaises(CaptureReplyRepositoryError) as raised:
                backend.repository.unlink_capture(
                    capture["id"],
                    backend.task["id"],
                    MAX_REVISION,
                    "unlink.v4.max.drop",
                )
        self.assertEqual(raised.exception.code, "revision_exhausted")
        self.assertEqual(raised.exception.command_boundary, "capture-reply")
        self.assertEqual(
            backend.state().captures[0]["linked_task_ids"], [backend.task["id"]]
        )
        self.assertEqual(backend.state().captures[0]["revision"], linked_again["revision"])

    def test_max_revision_and_ambiguous_receipt_are_closed_on_both_backends(self) -> None:
        cases = (
            ("unlink-max", "max-unlink"),
            ("undo-max", "max-undo"),
            ("ambiguous", "ambig"),
            ("ambiguous-extra-details", "ambig-extra"),
        )
        for case, prefix in cases:
            for version in (3, 4):
                harness = ServiceHarness(self.base / f"{prefix}-v{version}", version)
                with self.subTest(version=version, case=case):
                    ingested = harness.stack.ingest_capture(
                        copy.deepcopy(self.packet),
                        f"{prefix}.ingest.v{version}",
                    )
                    capture = ingested["body"]["data"]
                    linked = harness.stack.link_capture(
                        capture["id"],
                        harness.task["id"],
                        f"{prefix}.link.v{version}",
                    )["body"]["data"]
                    if case == "unlink-max":
                        def set_max(documents: dict[str, Any]) -> None:
                            documents["captures.json"]["captures"][0]["revision"] = MAX_REVISION

                        with _backend_documents(harness, set_max):
                            with self.assertRaises(RevisionExhaustedError) as raised:
                                harness.stack.unlink_capture(
                                    capture["id"],
                                    harness.task["id"],
                                    MAX_REVISION,
                                    f"{prefix}.drop.v{version}",
                                )
                        self.assertEqual(raised.exception.code, "revision_exhausted")
                        stored = harness.documents()["captures.json"]["captures"][0]
                        self.assertEqual(stored["linked_task_ids"], [harness.task["id"]])
                        if version == 3:
                            self.assertEqual(stored["revision"], MAX_REVISION)
                        else:
                            self.assertEqual(stored["revision"], linked["revision"])
                        continue

                    removed = harness.stack.unlink_capture(
                        capture["id"],
                        harness.task["id"],
                        linked["revision"],
                        f"{prefix}.drop.v{version}",
                    )
                    receipt_id = removed["body"]["meta"]["undo_receipt_id"]
                    if case == "undo-max":
                        def set_undo_max(documents: dict[str, Any], rid=str(receipt_id)) -> None:
                            row = documents["captures.json"]["captures"][0]
                            row["revision"] = MAX_REVISION
                            _rewrite_receipt_ceiling(documents, rid, row)

                        with _backend_documents(harness, set_undo_max):
                            with self.assertRaises(RevisionExhaustedError) as raised:
                                harness.stack.undo_capture_unlink(
                                    capture["id"],
                                    receipt_id,
                                    MAX_REVISION,
                                    f"{prefix}.restore.v{version}",
                                )
                        self.assertEqual(raised.exception.code, "revision_exhausted")
                        stored = harness.documents()["captures.json"]["captures"][0]
                        self.assertEqual(stored["linked_task_ids"], [])
                        self.assertFalse(
                            any(
                                event.get("type") == "capture.unlink_undone"
                                for event in harness.documents()["activity.json"]["activity"]
                            )
                        )
                        continue

                    append = (
                        _append_extra_details_sibling
                        if case == "ambiguous-extra-details"
                        else _append_pretty_sibling
                    )

                    def add_sibling(
                        documents: dict[str, Any], rid=str(receipt_id), add=append
                    ) -> None:
                        add(documents, rid)

                    with _backend_documents(harness, add_sibling):
                        durable_before = json.dumps(harness.documents(), sort_keys=True)
                        with self.assertRaises(DomainError) as undo_error:
                            harness.stack.undo_capture_unlink(
                                capture["id"],
                                receipt_id,
                                removed["body"]["data"]["revision"],
                                f"{prefix}.restore.v{version}",
                            )
                        self.assertEqual(undo_error.exception.code, "invalid_request")
                        if version == 4:
                            with self.assertRaises(DomainError) as replay_error:
                                harness.stack.unlink_capture(
                                    capture["id"],
                                    harness.task["id"],
                                    linked["revision"],
                                    f"{prefix}.drop.v{version}",
                                )
                            self.assertEqual(replay_error.exception.code, "invalid_request")
                            self.assertEqual(
                                replay_error.exception.details.get("repository_code"),
                                "malformed",
                            )
                        self.assertEqual(
                            json.dumps(harness.documents(), sort_keys=True),
                            durable_before,
                        )
                    stored = harness.documents()["captures.json"]["captures"][0]
                    self.assertEqual(stored["linked_task_ids"], [])
                    self.assertFalse(
                        any(
                            event.get("type") == "capture.unlink_undone"
                            for event in harness.documents()["activity.json"]["activity"]
                        )
                    )

    def test_v4_http_undo_refuses_noncanonical_receipt_id_at_admission(self) -> None:
        """Recovered taint spellings never become an admitted HTTP receipt id."""

        harness = ServiceHarness(self.base / "http-v4-id", 4)
        ingested = harness.stack.ingest_capture(
            copy.deepcopy(self.packet), "http.v4.id.ingest"
        )
        capture = ingested["body"]["data"]
        linked = harness.stack.link_capture(
            capture["id"], harness.task["id"], "http.v4.id.link"
        )["body"]["data"]
        removed = harness.stack.unlink_capture(
            capture["id"], harness.task["id"], linked["revision"], "http.v4.id.drop"
        )
        receipt_id = str(removed["body"]["meta"]["undo_receipt_id"])
        parsed = uuid.UUID(receipt_id)
        server = create_server(harness.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, session = _http_json(server.actual_port, "GET", "/api/v1/session")
            self.assertEqual(status, 200)
            spellings = {
                "uppercase": receipt_id.upper(),
                "hex": parsed.hex,
                "braced": "{{{}}}".format(receipt_id),
                "urn": parsed.urn,
            }
            for name, spelling in spellings.items():
                with self.subTest(spelling=name):
                    status, body = _http_json(
                        server.actual_port,
                        "POST",
                        "/api/v1/captures/{}/undo-unlink".format(capture["id"]),
                        {
                            "receipt_id": spelling,
                            "revision": removed["body"]["data"]["revision"],
                        },
                        {
                            "Origin": "http://127.0.0.1:{}".format(server.actual_port),
                            "X-WorkStack-CSRF": session["data"]["csrf_token"],
                            "Idempotency-Key": "http.v4.id.undo.{}".format(name),
                            "Content-Type": "application/json",
                        },
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(body["error"]["code"], "invalid_body")
            stored = harness.documents()["captures.json"]["captures"][0]
            self.assertEqual(stored["linked_task_ids"], [])
            self.assertFalse(
                any(
                    event.get("type") == "capture.unlink_undone"
                    for event in harness.documents()["activity.json"]["activity"]
                )
            )
            status, restored = _http_json(
                server.actual_port,
                "POST",
                "/api/v1/captures/{}/undo-unlink".format(capture["id"]),
                {
                    "receipt_id": receipt_id,
                    "revision": removed["body"]["data"]["revision"],
                },
                {
                    "Origin": "http://127.0.0.1:{}".format(server.actual_port),
                    "X-WorkStack-CSRF": session["data"]["csrf_token"],
                    "Idempotency-Key": "http.v4.id.undo.canonical",
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(restored["data"]["linked_task_ids"], [harness.task["id"]])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_v4_http_max_revision_unlink_returns_revision_exhausted(self) -> None:
        harness = ServiceHarness(self.base / "http-v4", 4)
        ingested = harness.stack.ingest_capture(
            copy.deepcopy(self.packet), "http.v4.max.ingest"
        )
        capture = ingested["body"]["data"]
        harness.stack.link_capture(
            capture["id"], harness.task["id"], "http.v4.max.link"
        )
        server = create_server(harness.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def set_max(documents: dict[str, Any]) -> None:
                documents["captures.json"]["captures"][0]["revision"] = MAX_REVISION

            status, session = _http_json(server.actual_port, "GET", "/api/v1/session")
            self.assertEqual(status, 200)
            headers = {
                "Origin": "http://127.0.0.1:{}".format(server.actual_port),
                "X-WorkStack-CSRF": session["data"]["csrf_token"],
                "Idempotency-Key": "http.v4.max.drop",
                "Content-Type": "application/json",
            }
            with _mutated_v4_load(harness.repository, set_max):
                status, body = _http_json(
                    server.actual_port,
                    "POST",
                    "/api/v1/captures/{}/unlink".format(capture["id"]),
                    {"task_id": harness.task["id"], "revision": MAX_REVISION},
                    headers,
                )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "revision_exhausted")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(
            harness.documents()["captures.json"]["captures"][0]["linked_task_ids"],
            [harness.task["id"]],
        )


@contextmanager
def _mutated_v4_load(repository, mutate: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    original = repository._load

    def patched():
        loaded = original()
        mutate(loaded[2])
        return loaded

    repository._load = patched
    try:
        yield
    finally:
        repository._load = original


@contextmanager
def _backend_documents(harness, mutate: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    if harness.version != 3:
        with _mutated_v4_load(harness.repository, mutate):
            yield
        return
    documents = {
        "captures.json": harness.stack.store.load("captures.json"),
        "activity.json": harness.stack.store.load("activity.json"),
    }
    mutate(documents)
    harness.stack.store.save_many(documents)
    yield


def _rewrite_receipt_ceiling(
    documents: dict[str, Any], receipt_id: str, capture: dict[str, Any]
) -> None:
    for event in documents["activity.json"].get("activity") or []:
        if event.get("type") != UNLINK_RECEIPT_EVENT:
            continue
        blob = json.loads(event["details"]["receipt"])
        if blob.get("receipt_id") != receipt_id:
            continue
        blob["after_revision"] = MAX_REVISION
        blob["before_revision"] = MAX_REVISION - 1
        blob["after_digest"] = capture_row_digest(capture)
        event["details"]["receipt"] = serialize_receipt(validate_receipt(blob)).decode("utf-8")


def _receipt_sibling(
    documents: dict[str, Any], receipt_id: str
) -> tuple[dict[str, Any] | None, str]:
    for event in list(documents["activity.json"].get("activity") or []):
        if event.get("type") != UNLINK_RECEIPT_EVENT:
            continue
        blob = event.get("details", {}).get("receipt")
        if type(blob) is not str:
            continue
        if json.loads(blob).get("receipt_id") != receipt_id:
            continue
        sibling = copy.deepcopy(event)
        sibling["id"] = "E-009999"
        return sibling, blob
    return None, ""


def _append_sibling(documents: dict[str, Any], sibling: dict[str, Any] | None) -> None:
    if sibling is not None:
        documents["activity.json"].setdefault("activity", []).append(sibling)


def _append_pretty_sibling(documents: dict[str, Any], receipt_id: str) -> None:
    sibling, blob = _receipt_sibling(documents, receipt_id)
    if sibling is not None:
        sibling["details"] = {"receipt": json.dumps(json.loads(blob), indent=2)}
    _append_sibling(documents, sibling)


def _append_extra_details_sibling(documents: dict[str, Any], receipt_id: str) -> None:
    """Same closed receipt bytes, smuggled inside a widened details envelope."""

    sibling, blob = _receipt_sibling(documents, receipt_id)
    if sibling is not None:
        sibling["details"] = {"receipt": blob, "extra": "x"}
    _append_sibling(documents, sibling)


def _http_json(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    outgoing = None
    actual = dict(headers or {})
    if body is not None:
        outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
        actual.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=outgoing, headers=actual)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    return status, json.loads(raw.decode("utf-8"))
