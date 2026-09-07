"""HTTP contract for bounded mutation-notice query and Undo."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.mutation_receipts import EVENT_TYPE
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store


class MutationNoticeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Notice API")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.origin = "http://127.0.0.1:{}".format(self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        outgoing: bytes | None = None
        actual = dict(headers or {})
        if body is not None:
            outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
            actual.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=outgoing, headers=actual)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {
            key.casefold(): value for key, value in response.getheaders()
        }
        status = response.status
        connection.close()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        return status, payload, response_headers

    def browser_headers(self, *, key: str | None = None) -> dict[str, str]:
        status, session, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        headers = {
            "Origin": self.origin,
            "X-WorkStack-CSRF": session["data"]["csrf_token"],
            "Content-Type": "application/json",
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    def test_existing_get_routes_keep_the_data_envelope(self) -> None:
        for path in ("/api/v1/session", "/api/v1/health", "/api/v1/workspace"):
            with self.subTest(path=path):
                status, payload, headers = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(set(payload), {"data"})
                self.assertEqual(
                    headers["content-type"], "application/json; charset=utf-8"
                )

    def test_get_lists_committed_notices_and_refuses_unknown_query(self) -> None:
        headers = self.browser_headers(key="wire.status.0001")
        status, payload, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(self.task["id"]),
            {"status": "started", "revision": 0},
            headers,
        )
        self.assertEqual(status, 200, payload)
        status, listed, _ = self.request("GET", "/api/v1/mutation-notices?limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(set(listed), {"data"})
        items = listed["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status_after"], "started")
        self.assertEqual(items[0]["idempotency_key"], "wire.status.0001")
        self.assertTrue(items[0]["undoable"])
        status, error, _ = self.request("GET", "/api/v1/mutation-notices?extra=1")
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_query")

    def test_undo_enforces_csrf_identity_revision_and_idempotency(self) -> None:
        headers = self.browser_headers(key="wire.status.0002")
        status, _, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(self.task["id"]),
            {"status": "started", "revision": 0},
            headers,
        )
        self.assertEqual(status, 200)
        notice_id = self.request("GET", "/api/v1/mutation-notices")[1]["data"]["items"][0][
            "notice_id"
        ]
        path = "/api/v1/mutation-notices/{}/undo".format(notice_id)
        status, denied, _ = self.request(
            "POST", path, {"revision": 1}, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "origin_required")
        missing_key = self.browser_headers()
        status, refused, _ = self.request("POST", path, {"revision": 1}, missing_key)
        self.assertEqual(status, 400)
        self.assertEqual(refused["error"]["code"], "idempotency_key_required")
        undo_headers = self.browser_headers(key="wire.undo.0002")
        status, undone, _ = self.request("POST", path, {"revision": 1}, undo_headers)
        self.assertEqual(status, 200, undone)
        self.assertEqual(undone["data"]["status"], "open")
        self.assertFalse(undone["meta"]["replayed"])
        status, replay, _ = self.request("POST", path, {"revision": 1}, undo_headers)
        self.assertEqual(status, 200)
        self.assertTrue(replay["meta"]["replayed"])
        stale = self.browser_headers(key="wire.undo.stale2")
        status, conflict, _ = self.request("POST", path, {"revision": 1}, stale)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        activity = self.stack.store.load("activity.json")
        notices = [
            event for event in activity["activity"] if event.get("type") == EVENT_TYPE
        ]
        self.assertEqual(len(notices), 2)

    def test_get_pages_are_bounded(self) -> None:
        revision = 0
        status_value = "open"
        for index in range(3):
            nxt = "started" if status_value == "open" else "open"
            headers = self.browser_headers(key="wire.page.{:04d}".format(index))
            result, payload, _ = self.request(
                "PATCH",
                "/api/v1/tasks/{}".format(self.task["id"]),
                {"status": nxt, "revision": revision},
                headers,
            )
            self.assertEqual(result, 200, payload)
            status_value = nxt
            revision += 1
        status, first, _ = self.request("GET", "/api/v1/mutation-notices?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["data"]["items"]), 1)
        cursor = first["data"]["next_cursor"]
        self.assertTrue(cursor)
        status, second, _ = self.request(
            "GET", "/api/v1/mutation-notices?limit=1&cursor={}".format(cursor)
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(second["data"]["items"]), 1)
        self.assertNotEqual(
            first["data"]["items"][0]["notice_id"],
            second["data"]["items"][0]["notice_id"],
        )
        self.assertIsInstance(cursor, str)
        self.assertFalse(cursor.startswith("E-"))
        status, malformed, _ = self.request("GET", "/api/v1/mutation-notices?cursor=E-000001")
        self.assertEqual(status, 400)
        self.assertEqual(malformed["error"]["code"], "invalid_query")
        self.assertNotIn(self.task["id"], json.dumps(malformed))
        self.assertNotIn(self.stack._workspace_uid(), json.dumps(malformed))


if __name__ == "__main__":
    unittest.main()
