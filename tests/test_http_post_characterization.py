from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.server import DEFAULT_BODY_LIMIT, create_server
from workstack.service import WorkStack
from workstack.store import Store


class HttpPostCharacterizationTest(unittest.TestCase):
    """Freeze POST boundary order, error envelopes, and legacy retirement."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, raw, response_headers

    def session_headers(self) -> dict[str, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/v1/session")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": payload["data"]["csrf_token"],
            "Content-Type": "application/json",
        }

    def json_error(
        self,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[int, dict]:
        status, raw, response_headers = self.request(path, body, headers)
        self.assertEqual(response_headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(response_headers["cache-control"], "no-store")
        return status, json.loads(raw.decode("utf-8"))

    def test_v1_boundary_error_table_preserves_validation_order(self) -> None:
        valid = json.dumps({"title": "Boundary"}, separators=(",", ":")).encode("utf-8")
        browser = self.session_headers()
        cases = (
            (
                "wrong host is rejected before body parsing",
                valid,
                {**browser, "Host": "example.com"},
                400,
                "invalid_host",
            ),
            (
                "content type precedes browser authorization",
                valid,
                {key: value for key, value in browser.items() if key != "Content-Type"},
                415,
                "unsupported_media_type",
            ),
            (
                "malformed json precedes browser authorization",
                b"{",
                {"Content-Type": "application/json"},
                400,
                "invalid_json",
            ),
            (
                "missing origin",
                valid,
                {"Content-Type": "application/json"},
                403,
                "origin_required",
            ),
            (
                "wrong origin",
                valid,
                {**browser, "Origin": "http://localhost:{}".format(self.port)},
                403,
                "invalid_origin",
            ),
            (
                "wrong csrf",
                valid,
                {**browser, "X-WorkStack-CSRF": "wrong"},
                403,
                "invalid_csrf",
            ),
            (
                "idempotency required after browser authorization",
                valid,
                browser,
                400,
                "idempotency_key_required",
            ),
        )
        before = self.store.path("backlog.json").read_bytes()
        for label, body, headers, expected_status, expected_code in cases:
            with self.subTest(label=label):
                status, payload = self.json_error("/api/v1/tasks", body, headers)
                self.assertEqual(status, expected_status)
                self.assertEqual(payload["error"]["code"], expected_code)
                self.assertEqual(set(payload["error"]), {"code", "message", "details"})
                self.assertEqual(self.store.path("backlog.json").read_bytes(), before)

    def test_body_limit_is_enforced_before_browser_authorization(self) -> None:
        status, payload = self.json_error(
            "/api/v1/tasks",
            b"",
            {
                "Content-Type": "application/json",
                "Content-Length": str(DEFAULT_BODY_LIMIT + 1),
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "body_too_large")

    def test_route_and_legacy_retirement_table(self) -> None:
        body = b"{}"
        browser = self.session_headers()
        cases = (
            ("/api/v1/not-a-route", browser, 404, "not_found"),
            ("/api/tasks", browser, 410, "legacy_task_writer_disabled"),
            ("/api/objectives", browser, 410, "legacy_writer_disabled"),
            ("/api/worklog", browser, 410, "legacy_writer_disabled"),
            ("/api/notes", browser, 410, "legacy_writer_disabled"),
        )
        for path, headers, expected_status, expected_code in cases:
            with self.subTest(path=path):
                status, payload = self.json_error(path, body, headers)
                self.assertEqual(status, expected_status)
                self.assertEqual(payload["error"]["code"], expected_code)

        status, raw, response_headers = self.request("/api/not-a-route", body, browser)
        self.assertEqual(status, 404)
        self.assertTrue(response_headers["content-type"].startswith("text/html"))
        self.assertIn(b"Error code: 404", raw)

    def test_task_creation_replay_and_conflict_shape(self) -> None:
        headers = {
            **self.session_headers(),
            "Idempotency-Key": "characterization.task.0001",
        }
        body = json.dumps({"title": "Created once"}, separators=(",", ":")).encode("utf-8")
        status, raw, _ = self.request("/api/v1/tasks", body, headers)
        created = json.loads(raw.decode("utf-8"))
        self.assertEqual(status, 201)
        self.assertFalse(created["meta"]["replayed"])

        status, raw, _ = self.request("/api/v1/tasks", body, headers)
        replayed = json.loads(raw.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], created["data"])

        different = json.dumps({"title": "Different"}, separators=(",", ":")).encode("utf-8")
        status, payload = self.json_error("/api/v1/tasks", different, headers)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "idempotency_conflict")


if __name__ == "__main__":
    unittest.main()
