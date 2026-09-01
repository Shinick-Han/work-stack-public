from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store


class HttpGetCharacterizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.stack = WorkStack(Store(Path(self.temporary.name)))
        self.task = self.stack.add_task("GET contract")
        self.objective = self.stack.add_objective("GET objective", "2026-Q3")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
        headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, raw, headers

    def json_request(self, path: str) -> tuple[int, dict, dict[str, str]]:
        status, raw, headers = self.request(path)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        return status, json.loads(raw.decode("utf-8")), headers

    def test_exact_read_route_table_keeps_data_envelope(self) -> None:
        routes = (
            "/api/state",
            "/api/v1/session",
            "/api/v1/health",
            "/api/v1/sync/status",
            "/api/v1/sync/events?after=0",
            "/api/v1/storage",
            "/api/v1/workspace",
            "/api/v1/search?q=GET&limit=10",
            "/api/v1/review?date=2026-08-31&days=7",
            "/api/v1/work-sessions",
            "/api/v1/objectives/{}".format(self.objective["id"]),
            "/api/v1/tasks/{}/snapshot".format(self.task["id"]),
            "/api/v1/tasks/{}".format(self.task["id"]),
            "/api/v1/captures?status=inbox",
        )
        for path in routes:
            with self.subTest(path=path):
                status, payload, headers = self.json_request(path)
                self.assertEqual(status, 200)
                self.assertIn("x-workstack-request-id", headers)
                if path != "/api/state":
                    self.assertEqual(set(payload), {"data"})

    def test_query_refusal_table_and_unknown_api_shape(self) -> None:
        cases = (
            ("/api/v1/sync/status?extra=1", "invalid_query"),
            ("/api/v1/sync/events?after=-1", "invalid_query"),
            ("/api/v1/events?extra=1", "invalid_query"),
            ("/api/v1/search?q=x", "invalid_query"),
            ("/api/v1/review?date=2026-08-31&days=0", "invalid_query"),
            ("/api/v1/work-sessions?extra=1", "invalid_query"),
            ("/api/v1/captures?extra=1", "invalid_query"),
            ("/api/v1/not-a-route", "not_found"),
            ("/api/not-a-route", "not_found"),
        )
        for path, code in cases:
            with self.subTest(path=path):
                status, payload, _ = self.json_request(path)
                self.assertEqual(status, 400 if code == "invalid_query" else 404)
                self.assertEqual(payload["error"]["code"], code)


if __name__ == "__main__":
    unittest.main()
