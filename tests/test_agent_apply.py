from __future__ import annotations

import contextlib
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


class AgentApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Agent-owned update", detail="Before")
        self.workspace_id = self.store.load("workspace.json")["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def packet(self, **changes: object) -> bytes:
        return json.dumps({
            "workspace_id": self.workspace_id,
            "task_id": self.task["id"],
            "expected_revision": 0,
            "changes": changes,
        }).encode("utf-8")

    def test_applies_one_revision_guarded_update_when_server_is_offline(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = apply_agent_update(
                self.store,
                self.packet(title="Updated by agent", priority="P1"),
                "agent.update.0001",
            )

        self.assertEqual(result, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["meta"]["mode"], "exclusive-local-store")
        self.assertEqual(receipt["data"]["revision"], 1)
        self.assertEqual(self.stack.get_task(self.task["id"])["title"], "Updated by agent")

        with self.assertRaisesRegex(Exception, "revision is stale"):
            apply_agent_update(
                self.store,
                self.packet(title="Unsafe replay"),
                "agent.update.0001",
            )

    def test_forwards_through_the_running_server_instead_of_opening_store(self) -> None:
        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = apply_agent_update(
                    Store(self.root),
                    self.packet(detail="Committed through server"),
                    "agent.update.0002",
                )
            self.assertEqual(result, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["meta"]["mode"], "running-server")
            self.assertEqual(receipt["data"]["detail"], "Committed through server")
            self.assertEqual(receipt["data"]["revision"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rejects_wrong_workspace_and_unbounded_or_unknown_fields(self) -> None:
        wrong = json.loads(self.packet(title="Wrong").decode("utf-8"))
        wrong["workspace_id"] = "00000000-0000-4000-8000-000000000001"
        with self.assertRaisesRegex(ValueError, "does not match"):
            apply_agent_update(
                self.store,
                json.dumps(wrong).encode("utf-8"),
                "agent.update.0003",
            )
        unknown = json.loads(self.packet(title="Wrong").decode("utf-8"))
        unknown["changes"] = {"revision": 99}
        with self.assertRaisesRegex(ValueError, "supported mutable"):
            apply_agent_update(
                self.store,
                json.dumps(unknown).encode("utf-8"),
                "agent.update.0004",
            )
        with self.assertRaisesRegex(ValueError, "32 KiB"):
            apply_agent_update(
                self.store,
                b"{" + b"x" * (32 * 1024),
                "agent.update.0005",
            )


if __name__ == "__main__":
    unittest.main()
