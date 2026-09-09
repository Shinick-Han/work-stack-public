"""Attribution of the Task-status notice an agent apply commits.

Both apply routes reach the same unkeyed ``PATCH``-equivalent write, so both
must report the same caller-reported provenance. The header is the existing
frozen Agent CLI client header already admitted by ``_agent_client_origin``;
absence keeps the ordinary GUI source, and a padded, unknown or repeated
value refuses before the Task is touched.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.cli import apply_agent_update
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store


def _status_notice_sources(stack: WorkStack) -> list[str]:
    return [
        str(item["source"])
        for item in stack.list_mutation_notices(limit=50)["items"]
    ]


class AgentApplyNoticeSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Agent-owned status", detail="Before")
        self.workspace_id = self.store.load("workspace.json")["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def packet(self, **changes: object) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task["id"],
            "expected_revision": 0,
            "changes": changes,
        }

    @contextlib.contextmanager
    def running_owner(self):
        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def browser_patch(
        self, port: int, body: dict[str, object], client: list[str] | None = None
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "GET", "/api/v1/session", headers={"Host": "127.0.0.1:{}".format(port)}
            )
            session = json.loads(connection.getresponse().read().decode("utf-8"))
            raw = json.dumps(body).encode("utf-8")
            headers = [
                ("Host", "127.0.0.1:{}".format(port)),
                ("Origin", "http://127.0.0.1:{}".format(port)),
                ("X-WorkStack-CSRF", session["data"]["csrf_token"]),
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw))),
            ]
            connection.putrequest(
                "PATCH", "/api/v1/tasks/{}".format(self.task["id"]), skip_host=True
            )
            for name, value in headers:
                connection.putheader(name, value)
            for value in client or ():
                connection.putheader("X-WorkStack-Client", value)
            connection.endheaders()
            connection.send(raw)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_local_agent_apply_status_is_attributed_to_the_agent(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = apply_agent_update(
                self.store, self.packet(status="started"), "agent.notice.0001"
            )

        self.assertEqual(result, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["meta"]["mode"], "exclusive-local-store")
        self.assertEqual(receipt["data"]["status"], "started")
        self.assertEqual(_status_notice_sources(self.stack), ["agent"])

    def test_owner_forwarded_agent_apply_status_is_attributed_to_the_agent(self) -> None:
        with self.running_owner() as port:
            del port
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = apply_agent_update(
                    Store(self.root), self.packet(status="started"), "agent.notice.0002"
                )
            self.assertEqual(result, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["meta"]["mode"], "running-server")
            self.assertEqual(receipt["data"]["status"], "started")
        self.assertEqual(_status_notice_sources(self.stack), ["agent"])

    def test_ordinary_browser_patch_without_the_header_stays_gui(self) -> None:
        with self.running_owner() as port:
            status, payload = self.browser_patch(
                port, {"status": "started", "revision": 0}
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["status"], "started")
        self.assertEqual(_status_notice_sources(self.stack), ["gui"])

    def test_unknown_padded_or_repeated_attribution_refuses_before_the_write(self) -> None:
        refused = (
            ["agent-cli-v2"],
            ["agent-cli-v1 "],
            ["AGENT-CLI-V1"],
            [""],
            ["agent-cli-v1", "agent-cli-v1"],
            ["agent-cli-v1", "agent-cli-v2"],
        )
        with self.running_owner() as port:
            for client in refused:
                with self.subTest(client=client):
                    status, payload = self.browser_patch(
                        port, {"status": "started", "revision": 0}, client=list(client)
                    )
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["error"]["code"], "invalid_header")
        task = self.stack.get_task(self.task["id"])
        self.assertEqual(task["revision"], 0)
        self.assertEqual(task["status"], "open")
        self.assertEqual(_status_notice_sources(self.stack), [])

    def test_leading_field_whitespace_is_stripped_by_the_http_parser(self) -> None:
        """Not a trust decision: the value that reaches admission IS exact.

        RFC 9110 optional whitespace after the colon is removed while the field
        is parsed, so ``" agent-cli-v1"`` is never seen as a padded value by
        this or any other route. Trailing whitespace survives parsing and is
        refused above; both behaviours are the frozen ones the worklog route
        already relies on, unchanged here.
        """

        with self.running_owner() as port:
            status, payload = self.browser_patch(
                port,
                {"status": "started", "revision": 0},
                client=[" agent-cli-v1"],
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(_status_notice_sources(self.stack), ["agent"])


if __name__ == "__main__":
    unittest.main()
