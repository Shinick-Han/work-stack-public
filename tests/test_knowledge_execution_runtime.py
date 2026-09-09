"""The operator's execution registry, and the sequence a running owner performs.

Two things live here. The first is *configuration*: what a trusted embedding
process may hand ``create_server``, what a running server then holds, and what
refuses before a server exists at all. Nothing in that half spawns a process or
opens a network connection.

The second is the runtime sequence's own two obligations, which are only
observable with a real server, a real store and a real child: that the attempt
guard belongs to one owner incarnation and to no other, and that no Store
transaction is held across the child while authority is nevertheless re-proven
after it. Those reuse the loopback harness the route suite already builds --
``tests/test_knowledge_execution_http.ExecutionHttpCase`` -- rather than a
second copy of it. The route surface itself, its refusal mapping and the manual
import that follows a proposal are that suite's own subject.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from workstack.knowledge_attempt_guard import KnowledgeAttemptError
from workstack.knowledge_execution_runtime import (
    MAX_DRIVERS,
    KnowledgeDriverBinding,
    KnowledgeDriverConfigurationError,
    admit_drivers,
)
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store

from tests.test_knowledge_execution_http import (
    AFTER_EXPIRY,
    CONNECTIONS,
    OTHER_INTENT_ID,
    OTHER_UPSTREAM_UID,
    REQUESTS,
    ExecutionHttpCase,
)

UPSTREAM_UID = "6a6a6a6a-6666-4666-8666-6666666666bc"
OTHER_UID = "77777777-7777-4777-8777-777777777777"
# A value that exists nowhere else. If it turns up in a repr or a str of a
# binding, the operator's argv and environment are not being withheld.
COMMAND_CANARY = "canary-4b19d0-driver-path"
ENVIRONMENT_CANARY = "canary-4b19d0-driver-secret"


def binding(**overrides: Any) -> KnowledgeDriverBinding:
    fields: dict[str, Any] = {
        "upstream_workspace_uid": UPSTREAM_UID,
        "command": (sys.executable, COMMAND_CANARY),
        "environment": {"WS_DRIVER_TOKEN": ENVIRONMENT_CANARY},
    }
    fields.update(overrides)
    return KnowledgeDriverBinding(**fields)


class DriverRegistryTest(unittest.TestCase):
    """What :func:`admit_drivers` accepts, copies and refuses."""

    def test_no_configuration_is_the_empty_registry(self) -> None:
        """The default is an explicit absence, not a partly configured one."""

        for absent in (None, {}):
            with self.subTest(absent=absent):
                admitted = admit_drivers(absent)
                self.assertEqual(dict(admitted), {})
                with self.assertRaises(TypeError):
                    admitted["team-nas"] = binding()  # type: ignore[index]

    def test_one_admitted_binding_is_a_copy_the_operator_cannot_change(self) -> None:
        """A later mutation of the operator's own objects changes nothing."""

        environment = {"WS_DRIVER_TOKEN": ENVIRONMENT_CANARY}
        command = [sys.executable, COMMAND_CANARY]
        registry = {
            "team-nas": binding(command=command, environment=environment)
        }
        admitted = admit_drivers(registry)

        command.append("--added-later")
        environment["WS_DRIVER_TOKEN"] = "changed-later"
        registry["second-nas"] = binding()

        held = admitted["team-nas"]
        self.assertEqual(held.command, (sys.executable, COMMAND_CANARY))
        self.assertEqual(dict(held.environment), {"WS_DRIVER_TOKEN": ENVIRONMENT_CANARY})
        self.assertEqual(set(admitted), {"team-nas"})
        with self.assertRaises(TypeError):
            held.environment["WS_DRIVER_TOKEN"] = "changed-later"  # type: ignore[index]

    def test_a_binding_never_shows_its_command_or_environment(self) -> None:
        """The pinned path and the operator's environment stay out of a repr."""

        held = admit_drivers({"team-nas": binding()})["team-nas"]
        for rendered in (repr(held), str(held)):
            self.assertIn(UPSTREAM_UID, rendered)
            self.assertNotIn(COMMAND_CANARY, rendered)
            self.assertNotIn(ENVIRONMENT_CANARY, rendered)

    def test_the_registry_is_bounded_by_the_ledger_connection_bound(self) -> None:
        """At most as many pinned drivers as the ledger may hold connections."""

        full = {
            "alias-{}".format(index): binding() for index in range(MAX_DRIVERS)
        }
        self.assertEqual(len(admit_drivers(full)), MAX_DRIVERS)
        full["alias-overflow"] = binding()
        with self.assertRaises(KnowledgeDriverConfigurationError) as refused:
            admit_drivers(full)
        self.assertEqual(refused.exception.code, "driver_registry_full")

    def test_a_malformed_registry_is_refused_with_a_closed_code(self) -> None:
        """Every rejected shape names a rule, and no submitted value at all."""

        absolute = sys.executable
        relative = os.path.basename(sys.executable)
        cases: list[tuple[str, Any, str]] = [
            ("not a mapping", [("team-nas", binding())], "invalid_driver_registry"),
            ("alias grammar", {"team nas": binding()}, "invalid_driver_alias"),
            ("alias with a path", {"../team": binding()}, "invalid_driver_alias"),
            ("alias not a string", {7: binding()}, "invalid_driver_alias"),
            ("not a binding", {"team-nas": {"command": [absolute]}}, "invalid_driver_binding"),
            (
                "upstream not canonical",
                {"team-nas": binding(upstream_workspace_uid=UPSTREAM_UID.upper())},
                "invalid_driver_upstream_workspace_uid",
            ),
            (
                "upstream not a uuid",
                {"team-nas": binding(upstream_workspace_uid="team-nas")},
                "invalid_driver_upstream_workspace_uid",
            ),
            ("empty command", {"team-nas": binding(command=())}, "invalid_driver_command"),
            (
                "relative argv0",
                {"team-nas": binding(command=(relative,))},
                "invalid_driver_command",
            ),
            (
                "command as one string",
                {"team-nas": binding(command=absolute)},
                "invalid_driver_command",
            ),
            (
                "empty argv part",
                {"team-nas": binding(command=(absolute, ""))},
                "invalid_driver_command",
            ),
            (
                "too many argv parts",
                {"team-nas": binding(command=(absolute,) + ("x",) * 16)},
                "invalid_driver_command",
            ),
            (
                "oversized argv part",
                {"team-nas": binding(command=(absolute, "x" * 513))},
                "invalid_driver_command",
            ),
            (
                "non-string argv part",
                {"team-nas": binding(command=(absolute, 3))},
                "invalid_driver_command",
            ),
            (
                "environment not a mapping",
                {"team-nas": binding(environment=[("A", "B")])},
                "invalid_driver_environment",
            ),
            (
                "non-string environment value",
                {"team-nas": binding(environment={"A": 3})},
                "invalid_driver_environment",
            ),
        ]
        for name, registry, code in cases:
            with self.subTest(case=name):
                with self.assertRaises(KnowledgeDriverConfigurationError) as refused:
                    admit_drivers(registry)
                self.assertEqual(refused.exception.code, code)
                rendered = str(refused.exception)
                self.assertEqual(rendered, code)
                self.assertNotIn(ENVIRONMENT_CANARY, rendered)


class ServerConstructionTest(unittest.TestCase):
    """What a constructed server holds, and what refuses before one exists."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # Registered before any server, so a server this case opens is closed
        # -- releasing its store lease -- before the directory is removed.
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)

    def start(self, **overrides: Any):
        server = create_server(self.stack, "127.0.0.1", 0, **overrides)
        self.addCleanup(server.server_close)
        return server

    def test_a_server_defaults_to_no_driver_and_its_own_guard(self) -> None:
        server = self.start()
        self.assertEqual(dict(server.knowledge_drivers), {})
        self.assertIsNotNone(server.knowledge_attempt_guard)

    def test_two_servers_hold_different_guards(self) -> None:
        """Instances share nothing: a second owner has registered nothing."""

        first = self.start()
        first.server_close()
        second = self.start()
        self.assertIsNot(first.knowledge_attempt_guard, second.knowledge_attempt_guard)

    def test_an_invalid_binding_refuses_before_the_lease_is_taken(self) -> None:
        """A configuration defect refuses to start a server, and leaks no lease.

        The proof that no lease was taken and no socket was left open is that a
        *valid* server starts immediately afterwards over the same store: the
        released server lease is exclusive, so a leaked one would refuse here.
        """

        with self.assertRaises(KnowledgeDriverConfigurationError) as refused:
            self.start(
                knowledge_drivers={
                    "team-nas": binding(command=(os.path.basename(sys.executable),))
                }
            )
        self.assertEqual(refused.exception.code, "invalid_driver_command")
        server = self.start(knowledge_drivers={"team-nas": binding()})
        self.assertEqual(set(server.knowledge_drivers), {"team-nas"})

    def test_a_running_server_holds_an_immutable_registry(self) -> None:
        """There is no route, and no attribute path, that rebinds a driver."""

        registry = {"team-nas": binding()}
        server = self.start(knowledge_drivers=registry)
        registry["team-nas"] = binding(upstream_workspace_uid=OTHER_UID)
        self.assertEqual(
            server.knowledge_drivers["team-nas"].upstream_workspace_uid, UPSTREAM_UID
        )
        with self.assertRaises(TypeError):
            server.knowledge_drivers["team-nas"] = registry["team-nas"]  # type: ignore[index]


class ExecutionAttemptTest(ExecutionHttpCase):
    """The guard belongs to this incarnation, and to no other."""

    def test_a_replayed_issue_does_not_rearm_a_spent_attempt(self) -> None:
        document = self.ready()
        self.arm_answer()
        self.assertEqual(self.execute(document)[0], 200)

        # The same intent, issued again by the same owner: a replay.
        status, replayed = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, replayed)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], document)

        status, payload = self.execute(document)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "request_already_attempted")
        self.assertEqual(self.child_calls(), 1)

    def test_a_restarted_owner_refuses_an_older_request(self) -> None:
        """Restart is not resume: a previous incarnation registered nothing."""

        document = self.ready()
        self.arm_answer()
        self.restart()

        status, payload = self.execute(document)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "request_not_registered")

        # A replayed issue on the new owner recognises the record without
        # authorising it again, so it does not make the old request runnable.
        status, replayed = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, replayed)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(self.execute(document)[0], 409)
        self.assertEqual(self.child_calls(), 0)

        # A newly reviewed request is the supported way forward.
        self.assertEqual(self.execute(self.issue(intent_id=OTHER_INTENT_ID))[0], 200)
        self.assertEqual(self.child_calls(), 1)

    def test_two_server_instances_do_not_share_the_guard(self) -> None:
        """A second owner over its own store has registered nothing at all."""

        document = self.ready()
        self.arm_answer()
        other = create_server(
            WorkStack(Store(Path(tempfile.mkdtemp(dir=self.root)))), "127.0.0.1", 0
        )
        self.addCleanup(other.server_close)
        self.assertIsNot(
            other.knowledge_attempt_guard, self.server.knowledge_attempt_guard
        )
        self.assertEqual(self.execute(document)[0], 200)
        # The first server's spent attempt is not visible to the second.
        with self.assertRaises(KnowledgeAttemptError) as unknown:
            other.knowledge_attempt_guard.consume(
                document["request_id"], "sha256:" + "0" * 64
            )
        self.assertEqual(unknown.exception.code, "request_not_registered")


class ExecutionConcurrencyTest(ExecutionHttpCase):
    """No transaction is held across the child, and authority is re-proven."""

    def blocked_execution(self, document: Any) -> list[tuple[int, Any]]:
        """Start one execution and return once its child is really running."""

        self.arm_answer()
        self.arm_barrier()
        results: list[tuple[int, Any]] = []

        def run() -> None:
            results.append(self.execute(document))

        self.worker = threading.Thread(target=run)
        self.worker.start()
        self.await_child()
        return results

    def finish(self, results: list[tuple[int, Any]]) -> tuple[int, Any]:
        self.release_child()
        self.worker.join(timeout=60)
        self.assertEqual(len(results), 1, results)
        return results[0]

    def test_an_unrelated_store_write_proceeds_while_the_child_runs(self) -> None:
        """The outer transaction is released before any child I/O."""

        results = self.blocked_execution(self.ready())
        # A real Store write while the child is blocked. If the execution still
        # held the process lock this would not return.
        finished = threading.Event()

        def write() -> None:
            self.stack.add_task("Written while the driver runs")
            finished.set()

        writer = threading.Thread(target=write)
        writer.start()
        self.assertTrue(finished.wait(timeout=15), "the store was held across the child")
        writer.join(timeout=15)
        self.assertEqual(len(self.document("backlog.json")["tasks"]), 1)

        status, payload = self.finish(results)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.child_calls(), 1)

    def test_a_policy_change_during_the_child_blocks_the_result(self) -> None:
        results = self.blocked_execution(self.ready())
        status, replaced = self.post(
            CONNECTIONS, self.policy_body(alias="team-nas", upstream=OTHER_UPSTREAM_UID)
        )
        self.assertEqual(status, 200, replaced)
        status, payload = self.finish(results)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "policy_revision_changed")
        self.assertEqual(self.document("captures.json")["captures"], [])

    def test_an_expiry_during_the_child_blocks_the_result(self) -> None:
        results = self.blocked_execution(self.ready())
        self.now = AFTER_EXPIRY
        status, payload = self.finish(results)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "request_expired")
        self.assertEqual(self.document("captures.json")["captures"], [])

    def test_a_task_change_during_the_child_blocks_the_result(self) -> None:
        self.with_policy()
        self.stack.add_task("Held task")
        task = self.document("backlog.json")["tasks"][0]
        document = self.issue(
            binding={
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": task["id"],
                "task_revision": task["revision"],
            }
        )
        results = self.blocked_execution(document)
        status, noted = self.post(
            "/api/v1/tasks/{}/notes".format(task["id"]),
            {"text": "Moved while the driver ran", "revision": task["revision"]},
            dict(self.owner_headers(), **{"Idempotency-Key": "b" * 16}),
        )
        self.assertEqual(status, 200, noted)
        status, payload = self.finish(results)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "task_binding_mismatch")
        self.assertEqual(self.document("captures.json")["captures"], [])


if __name__ == "__main__":  # pragma: no cover - parity with the released suites
    unittest.main()
