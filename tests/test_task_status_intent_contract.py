"""The keyed Task-status intent on PATCH /api/v1/tasks/{id}.

Two halves. The direct-service half drives the real ``WorkStack`` over a real
contained ``Store`` and reads the documents back off disk. The wire half drives
a real ephemeral loopback server with real requests and parses the bytes a
client actually receives.

Nothing installed, live, browser-based or external is touched, no CLI child is
started, and no save method is bypassed or simulated.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workstack.capture import canonical_digest
from workstack.service import (
    DomainError,
    RevisionConflictError,
    RevisionExhaustedError,
    WorkStack,
)
from workstack.store import Store
from workstack.storage.document_repository import StoreDocumentRepository

MAX_REVISION = 9007199254740991


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "task-status-intent"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _IntentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Intent boundary")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------
    def path_for(self, task_id: str | None = None) -> str:
        return "/api/v1/tasks/{}".format(task_id or self.task["id"])

    def intent(self, key, status, revision, *, task_id=None, digest=None):
        body = {"status": status, "revision": revision}
        return self.stack.set_task_status_v1(
            task_id or self.task["id"], body, key,
            path=self.path_for(task_id), request_digest=digest,
        )

    def documents(self) -> dict:
        return {
            name: (self.root / name).read_bytes()
            for name in ("backlog.json", "activity.json", "worklog.json")
        }

    def stored_task(self) -> dict:
        backlog = self.stack.store.load("backlog.json")
        return [t for t in backlog["tasks"] if t["id"] == self.task["id"]][0]

    def receipts(self) -> list:
        return self.stack.store.load("activity.json").get("idempotency", [])

    def planning_facts(self) -> list:
        activity = self.stack.store.load("activity.json")
        return [
            r for r in activity.get("planning_status", [])
            if r.get("task_id") == self.task["id"]
        ]


class FreshAndReplay(_IntentCase):
    def test_a_fresh_keyed_action_returns_the_full_task_and_replayed_false(self) -> None:
        result = self.intent("intent.fresh.0001", "started", 0)
        self.assertEqual(result["status"], 200)
        self.assertFalse(result["body"]["meta"]["replayed"])
        data = result["body"]["data"]
        self.assertEqual(data["id"], self.task["id"])
        self.assertEqual(data["title"], "Intent boundary")
        self.assertEqual(data["status"], "started")
        self.assertEqual(data["revision"], 1)

    def test_an_exact_replay_returns_the_original_receipt(self) -> None:
        first = self.intent("intent.replay.0001", "started", 0)
        before = self.documents()
        replay = self.intent("intent.replay.0001", "started", 0)
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], first["body"]["data"])
        self.assertEqual(self.documents(), before, "a replay writes nothing")

    def test_an_old_receipt_replays_after_unrelated_revisions(self) -> None:
        first = self.intent("intent.old.0001", "started", 0)
        self.stack.set_task_status(self.task["id"], "done", 1)
        self.stack.set_task_status(self.task["id"], "started", 2)
        before = self.documents()
        replay = self.intent("intent.old.0001", "started", 0)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], first["body"]["data"])
        self.assertEqual(replay["body"]["data"]["revision"], 1)
        self.assertEqual(self.documents(), before)

    def test_the_receipt_survives_a_recreated_service_on_the_same_store(self) -> None:
        first = self.intent("intent.reopen.0001", "started", 0)
        reopened = WorkStack(Store(self.root))
        replay = reopened.set_task_status_v1(
            self.task["id"], {"status": "started", "revision": 0},
            "intent.reopen.0001", path=self.path_for(),
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], first["body"]["data"])

    def test_formatting_equivalent_bodies_replay_and_a_changed_body_conflicts(self) -> None:
        self.intent("intent.format.0001", "started", 0)
        reordered = {"revision": 0, "status": "started"}
        replay = self.stack.set_task_status_v1(
            self.task["id"], reordered, "intent.format.0001", path=self.path_for()
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])
        with self.assertRaises(Exception) as caught:
            self.intent("intent.format.0001", "done", 0)
        self.assertEqual(getattr(caught.exception, "code", None), "idempotency_conflict")

    def test_a_supplied_digest_that_does_not_match_the_body_refuses(self) -> None:
        original = {"status": "started", "revision": 0}
        before = self.documents()
        with self.assertRaises(DomainError):
            self.intent(
                "intent.forge.0001", "done", 0, digest=canonical_digest(original)
            )
        self.assertEqual(self.documents(), before)

    def test_distinct_keys_at_one_revision_give_one_winner(self) -> None:
        self.assertEqual(self.intent("intent.race.0001", "started", 0)["status"], 200)
        with self.assertRaises(RevisionConflictError):
            self.intent("intent.race.0002", "done", 0)
        self.assertEqual(self.stored_task()["revision"], 1)


class SameStatusNoOp(_IntentCase):
    def test_a_valid_no_op_persists_only_its_receipt(self) -> None:
        before_task = dict(self.stored_task())
        before_facts = len(self.planning_facts())
        result = self.intent("intent.noop.0001", "open", 0)
        self.assertEqual(result["status"], 200)
        self.assertFalse(result["body"]["meta"]["replayed"])
        after = self.stored_task()
        self.assertEqual(after, before_task, "the Task must be byte-identical")
        self.assertEqual(after["revision"], 0)
        self.assertEqual(len(self.planning_facts()), before_facts, "no planning fact")
        self.assertIn(
            "intent.noop.0001", [r["key"] for r in self.receipts()], "receipt persisted"
        )

    def test_a_no_op_still_requires_a_successful_revision_check(self) -> None:
        before = self.documents()
        with self.assertRaises(RevisionConflictError):
            self.intent("intent.noop.0002", "open", 7)
        self.assertEqual(self.documents(), before)

    def test_a_no_op_receipt_replays(self) -> None:
        first = self.intent("intent.noop.0003", "open", 0)
        replay = self.intent("intent.noop.0003", "open", 0)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], first["body"]["data"])

    def test_a_no_op_at_max_revision_still_succeeds(self) -> None:
        backlog = self.stack.store.load("backlog.json")
        for task in backlog["tasks"]:
            if task["id"] == self.task["id"]:
                task["revision"] = MAX_REVISION
        self.stack.store.save("backlog.json", backlog)
        result = self.intent("intent.max.0001", "open", MAX_REVISION)
        self.assertEqual(result["status"], 200)
        self.assertEqual(self.stored_task()["revision"], MAX_REVISION)

    def test_a_changing_action_at_max_revision_refuses(self) -> None:
        backlog = self.stack.store.load("backlog.json")
        for task in backlog["tasks"]:
            if task["id"] == self.task["id"]:
                task["revision"] = MAX_REVISION
        self.stack.store.save("backlog.json", backlog)
        before = self.documents()
        with self.assertRaises(RevisionExhaustedError):
            self.intent("intent.max.0002", "started", MAX_REVISION)
        self.assertEqual(self.documents(), before)


class StrictKeyAndBody(_IntentCase):
    def test_a_malformed_or_empty_key_refuses_before_any_write(self) -> None:
        before = self.documents()
        for key in ("", "short", "a" * 129, "bad key!"):
            with self.subTest(key=key):
                with self.assertRaises(DomainError):
                    self.intent(key, "started", 0)
        self.assertEqual(self.documents(), before)

    def test_a_str_subclass_key_refuses(self) -> None:
        class _Key(str):
            pass

        with self.assertRaises(DomainError):
            self.intent(_Key("intent.subclass.0001"), "started", 0)

    def test_any_other_body_shape_refuses(self) -> None:
        before = self.documents()
        for body in (
            {"status": "started"},
            {"revision": 0},
            {"status": "started", "revision": 0, "title": "new"},
            {"status": "started", "revision": 0, "detail": None},
            {"status": 7, "revision": 0},
            {"status": "unknown", "revision": 0},
            {"status": "started", "revision": -1},
            {"status": "started", "revision": True},
            {"status": "started", "revision": "0"},
            [],
            None,
        ):
            with self.subTest(body=body):
                with self.assertRaises(DomainError):
                    self.stack.set_task_status_v1(
                        self.task["id"], body, "intent.body.0001", path=self.path_for()
                    )
        self.assertEqual(self.documents(), before)


class CompositionOwnership(_IntentCase):
    def test_a_cross_store_composition_refuses_before_writes(self) -> None:
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        foreign = WorkStack(Store(Path(other.name)))
        crossed = WorkStack(
            self.stack.store,
            initialize=False,
            document_repository=StoreDocumentRepository(foreign.store),
        )
        before = self.documents()
        with self.assertRaises(DomainError):
            crossed.set_task_status_v1(
                self.task["id"], {"status": "started", "revision": 0},
                "intent.cross.0001", path=self.path_for(),
            )
        self.assertEqual(self.documents(), before)

    def test_an_injected_repository_refuses_including_a_no_op(self) -> None:
        inner = self.stack.documents

        class _Delegating:
            def load(self, document):
                return inner.load(document)

            def save(self, document, value):
                return inner.save(document, value)

            def save_many(self, writes, operation_id=None):
                return inner.save_many(writes, operation_id=operation_id)

            def total_bytes(self):
                return inner.total_bytes()

        self.stack.documents = _Delegating()
        try:
            for key, status in (("intent.inject.0001", "started"), ("intent.inject.0002", "open")):
                with self.subTest(status=status):
                    with self.assertRaises(DomainError):
                        self.intent(key, status, 0)
        finally:
            self.stack.documents = inner

    def test_the_ordinary_unkeyed_patch_is_unaffected_by_composition(self) -> None:
        inner = self.stack.documents

        class _Delegating:
            def load(self, document):
                return inner.load(document)

            def save(self, document, value):
                return inner.save(document, value)

            def save_many(self, writes, operation_id=None):
                return inner.save_many(writes, operation_id=operation_id)

            def total_bytes(self):
                return inner.total_bytes()

        self.stack.documents = _Delegating()
        try:
            patched = self.stack.patch_task(
                self.task["id"], {"title": "Renamed", "revision": 0}
            )
        finally:
            self.stack.documents = inner
        self.assertEqual(patched["title"], "Renamed")


class AtomicityAndFailure(_IntentCase):
    def test_a_changing_action_saves_task_fact_and_receipt_in_one_call(self) -> None:
        """Counted at the real Store, so the admitted composition is intact.

        Swapping the repository for a recording stand-in is not an option here:
        the keyed branch requires the released same-Store composition by object
        identity and would refuse before saving anything, which is exactly the
        behaviour a different test pins.
        """

        store = self.stack.store
        before_facts = len(self.planning_facts())
        original = store.save_many
        calls = []

        def watched(writes, operation_id=None):
            calls.append(tuple(sorted(writes)))
            return original(writes, operation_id=operation_id)

        store.save_many = watched
        try:
            self.intent("intent.atomic.0001", "started", 0)
        finally:
            store.save_many = original

        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0], ("activity.json", "backlog.json"), calls[0])
        # add_task already records the Task's opening planning fact, so the
        # change adds exactly one more rather than being the only one.
        self.assertEqual(len(self.planning_facts()), before_facts + 1)
        self.assertIn("intent.atomic.0001", [r["key"] for r in self.receipts()])

    def test_a_no_op_saves_only_activity_at_the_real_store(self) -> None:
        store = self.stack.store
        original = store.save_many
        calls = []

        def watched(writes, operation_id=None):
            calls.append(tuple(sorted(writes)))
            return original(writes, operation_id=operation_id)

        store.save_many = watched
        try:
            self.intent("intent.atomic.0002", "open", 0)
        finally:
            store.save_many = original
        self.assertEqual(calls, [("activity.json",)], calls)

    def test_a_save_failure_leaves_every_byte_unchanged(self) -> None:
        store = self.stack.store
        original = store.save_many

        def failing(writes, operation_id=None):
            raise OSError("injected save failure")

        before = self.documents()
        store.save_many = failing
        try:
            with self.assertRaises(OSError):
                self.intent("intent.savefail.0001", "started", 0)
        finally:
            store.save_many = original
        self.assertEqual(self.documents(), before)
        self.assertEqual(
            [r for r in self.receipts() if r["key"] == "intent.savefail.0001"], []
        )

    def test_worklog_and_checkpoint_history_are_untouched(self) -> None:
        self.stack.add_worklog_v1(
            {
                "date": "2026-09-03", "task_id": self.task["id"],
                "done": ["one"], "next": [], "blockers": [],
            },
            "intent.worklog.0001",
            origin="agent-cli-v1",
        )
        before_worklog = (self.root / "worklog.json").read_bytes()
        before_audit = self.stack.list_checkpoint_audit()
        self.intent("intent.worklog.0002", "started", 0)
        self.assertEqual((self.root / "worklog.json").read_bytes(), before_worklog)
        self.assertEqual(self.stack.list_checkpoint_audit(), before_audit)


class UndoCompensation(_IntentCase):
    def test_a_distinct_key_compensates_at_the_forward_receipt_revision(self) -> None:
        before_facts = len(self.planning_facts())
        forward = self.intent("intent.forward.0001", "started", 0)
        self.assertEqual(forward["body"]["data"]["revision"], 1)
        undo = self.intent("intent.undo.0001", "open", 1)
        self.assertEqual(undo["status"], 200)
        self.assertEqual(undo["body"]["data"]["status"], "open")
        self.assertEqual(undo["body"]["data"]["revision"], 2)
        self.assertEqual(
            len(self.planning_facts()), before_facts + 2,
            "the forward action and its compensation are both appended",
        )

    def test_an_intervening_edit_makes_the_undo_conflict(self) -> None:
        self.intent("intent.forward.0002", "started", 0)
        self.stack.set_task_status(self.task["id"], "done", 1)
        before = self.documents()
        with self.assertRaises(RevisionConflictError):
            self.intent("intent.undo.0002", "open", 1)
        self.assertEqual(self.documents(), before, "a conflict is not a rebase")


class _WireCase(unittest.TestCase):
    def setUp(self) -> None:
        from workstack.server import create_server

        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Wire intent")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        # The sink is CONNECTED to the owned server's real error callback. An
        # earlier version created this list and asserted it stayed empty without
        # ever wiring it, so the assertion could not fail no matter what the
        # handler did. Nothing appends to it except the server itself.
        self.errors: list = []
        self.server.handle_error = self._record_handler_error
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])
        self.origin = "http://127.0.0.1:{}".format(self.port)

    def _record_handler_error(self, request, client_address) -> None:
        """The owned server's real handle_error, recording instead of printing."""

        import traceback

        self.errors.append(traceback.format_exc())

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "the fixture server thread must exit")
        self.assertEqual(self.errors, [], "the handler recorded errors")
        self.temporary.cleanup()

    def patch(self, body, *, key=None, headers=None, task_id=None):
        payload = json.dumps(body).encode("utf-8")
        outgoing = {
            "Host": "127.0.0.1:{}".format(self.port),
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "Origin": self.origin,
            "X-WorkStack-CSRF": self.server.csrf_token,
        }
        if key is not None:
            outgoing["Idempotency-Key"] = key
        if headers:
            outgoing.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "PATCH", "/api/v1/tasks/{}".format(task_id or self.task["id"]),
                body=payload, headers=outgoing,
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def raw_patch(self, body, extra_headers, *, csrf=True) -> tuple[int, bytes]:
        payload = json.dumps(body).encode("utf-8")
        lines = [
            "PATCH /api/v1/tasks/{} HTTP/1.1".format(self.task["id"]),
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "Connection: close",
        ]
        if csrf:
            lines.append("X-WorkStack-CSRF: {}".format(self.server.csrf_token))
        lines.extend(extra_headers)
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as sock:
            sock.sendall(request)
            chunks = []
            while True:
                received = sock.recv(65536)
                if not received:
                    break
                chunks.append(received)
        raw = b"".join(chunks)
        return int(raw.split(b" ", 2)[1]), raw

    def stored_revision(self) -> int:
        backlog = self.stack.store.load("backlog.json")
        return [t for t in backlog["tasks"] if t["id"] == self.task["id"]][0]["revision"]


class WireContract(_WireCase):
    def test_a_keyed_patch_returns_200_with_replayed_false_then_true(self) -> None:
        status, body = self.patch(
            {"status": "started", "revision": 0}, key="wire.intent.0001"
        )
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertFalse(payload["meta"]["replayed"])
        self.assertEqual(payload["data"]["status"], "started")

        status, body = self.patch(
            {"status": "started", "revision": 0}, key="wire.intent.0001"
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["meta"]["replayed"])

    def test_an_unkeyed_patch_keeps_its_ordinary_envelope(self) -> None:
        status, body = self.patch({"title": "Renamed", "revision": 0})
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"data"}, "no meta is added to ordinary PATCH")
        self.assertEqual(payload["data"]["title"], "Renamed")

    def test_a_keyed_patch_with_a_general_body_refuses_400(self) -> None:
        status, body = self.patch(
            {"title": "Renamed", "revision": 0}, key="wire.intent.0002"
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.stored_revision(), 0, "nothing may be written")

    def test_an_empty_or_malformed_key_refuses_400(self) -> None:
        for key in ("", "short", "bad key!"):
            with self.subTest(key=key):
                status, _ = self.patch({"status": "started", "revision": 0}, key=key)
                self.assertEqual(status, 400)
        self.assertEqual(self.stored_revision(), 0)

    def test_a_duplicate_key_header_refuses_400(self) -> None:
        status, _ = self.raw_patch(
            {"status": "started", "revision": 0},
            ["Idempotency-Key: wire.dup.0001", "idempotency-key: wire.dup.0001"],
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.stored_revision(), 0)

    def test_a_case_varied_key_header_still_selects_the_keyed_branch(self) -> None:
        status, body = self.raw_patch(
            {"status": "started", "revision": 0},
            ["idempotency-key: wire.case.0001"],
        )
        self.assertEqual(status, 200, body)
        self.assertIn(b'"replayed": false', body.replace(b'"replayed":false', b'"replayed": false'))

    def test_security_precedes_the_replay_lookup(self) -> None:
        status, _ = self.raw_patch(
            {"status": "started", "revision": 0},
            ["Idempotency-Key: wire.csrf.0001"], csrf=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.stored_revision(), 0)
        self.assertEqual(
            [r for r in self.stack.store.load("activity.json")["idempotency"]
             if r["key"] == "wire.csrf.0001"],
            [], "a refused request leaves no receipt",
        )

    def test_a_stale_revision_conflicts(self) -> None:
        status, body = self.patch(
            {"status": "started", "revision": 5}, key="wire.stale.0001"
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(self.stored_revision(), 0)

    def test_a_committed_response_that_is_lost_replays_once(self) -> None:
        payload = json.dumps({"status": "started", "revision": 0}).encode("utf-8")
        lines = [
            "PATCH /api/v1/tasks/{} HTTP/1.1".format(self.task["id"]),
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "X-WorkStack-CSRF: {}".format(self.server.csrf_token),
            "Idempotency-Key: wire.lost.0001",
            "Connection: close",
        ]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        try:
            sock.sendall(request)
        finally:
            sock.close()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.stored_revision() == 0:
            time.sleep(0.05)
        self.assertEqual(self.stored_revision(), 1, "the change really committed")

        retry = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
        self.assertEqual(request, retry, "the retry must be byte-identical")
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as second:
            second.sendall(retry)
            chunks = []
            while True:
                received = second.recv(65536)
                if not received:
                    break
                chunks.append(received)
        raw = b"".join(chunks)
        self.assertEqual(int(raw.split(b" ", 2)[1]), 200, raw[:120])
        self.assertTrue(json.loads(raw.split(b"\r\n\r\n", 1)[1])["meta"]["replayed"])
        self.assertEqual(self.stored_revision(), 1, "the retry changed nothing")


FIXTURE_FAULT_LABEL = "workstack-fixture-controlled-handler-fault"


class WireHandlerErrorSinkIsConnected(_WireCase):
    """TSI-T1: prove the asserted sink really is the server's callback.

    A controlled fault is raised inside the request-handling path so it reaches
    the server's own ``handle_error``. Nothing appends to the sink by hand and
    no test value is substituted: if the wiring were removed, this case would
    fail because the sink would stay empty.

    The acknowledged fault is the only record cleared, and it is cleared here so
    the inherited teardown assertion still means what it says.
    """

    def install_controlled_fault(self) -> None:
        base = self.server.RequestHandlerClass

        class _FaultingHandler(base):
            def handle(inner) -> None:  # noqa: N805 - handler receives itself
                raise RuntimeError(FIXTURE_FAULT_LABEL)

        self.server.RequestHandlerClass = _FaultingHandler
        self.addCleanup(setattr, self.server, "RequestHandlerClass", base)

    def test_a_controlled_handler_fault_reaches_the_asserted_sink(self) -> None:
        self.assertEqual(self.errors, [], "the sink starts empty")
        self.install_controlled_fault()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "GET", "/api/v1/health",
                headers={"Host": "127.0.0.1:{}".format(self.port)},
            )
            try:
                connection.getresponse().read()
            except Exception:
                # The controlled fault means there is no response to read; the
                # point of this case is the server-side record, not the reply.
                pass
        finally:
            connection.close()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.errors:
            time.sleep(0.02)

        self.assertEqual(len(self.errors), 1, self.errors)
        self.assertIn(FIXTURE_FAULT_LABEL, self.errors[0])
        self.assertIn("RuntimeError", self.errors[0])

        # Clear ONLY this acknowledged fixture fault.
        self.errors.clear()

    def test_a_healthy_request_records_nothing(self) -> None:
        """The negative control: the sink stays empty when nothing goes wrong."""

        status, body = self.patch(
            {"status": "started", "revision": 0}, key="wire.sink.0001"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.errors, [])


class ActualExperimentalV4Composition(unittest.TestCase):
    """TSI-T2: the keyed branch against a REAL v4 application.

    The composition is built by the admitted factory and used as it is: the
    domain, its command objects and the store adapter are the real instances,
    and only their methods are wrapped to count calls, so composition identity
    is never substituted.
    """

    NOW = "2026-09-01T12:00:00Z"

    def setUp(self) -> None:
        from unittest import mock

        from workstack.storage.canonical import canonical_json_bytes
        from workstack.storage.manifest import build_v4_manifest
        from workstack.storage.manifest_store import publish_runtime_manifest
        from workstack.storage.migration_conversion import convert_v3_documents
        from workstack.storage.reader import read_v4
        from workstack.storage.runtime import resolve_runtime_authority
        from workstack.store import DEFAULTS

        # An ordinary SHORT contained path under the new results root: a long
        # path breaks this fixture on Windows before its assertions, and a
        # namespace-prefixed path is rejected by the existing layout policy.
        # Nothing about Windows permissions, the registry or any global path
        # setting is changed here.
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.base = Path(self.temporary.name)
        legacy = WorkStack(Store(self.base / "v3"))
        with mock.patch("workstack.service.utc_now", return_value=self.NOW), mock.patch(
            "workstack.service.today", return_value=self.NOW[:10]
        ):
            self.task = legacy.add_task("V4 composition task")
        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at=self.NOW
        )
        self.authority = self.base / "auth"
        self.authority.mkdir()
        self._write_conversion(self.authority, self.conversion, canonical_json_bytes)
        self.runtime = resolve_runtime_authority(
            self.authority, self.base / "rt", str(self.conversion.store["workspace_uid"])
        )
        self.runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.authority), generation=0),
            expected_digest=None,
        )
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_conversion(root: Path, conversion, canonical_json_bytes) -> None:
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
                    "records/{}/{}/{}.json".format(kind, uid[:2], uid),
                    canonical_json_bytes(dict(record)),
                )
        segments: dict = {}
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
            write("streams/{}/{}.ndjson".format(kind, month), body)

    # -- the real composition ---------------------------------------------
    def application(self):
        from workstack.storage.experimental_application import (
            create_experimental_v4_application,
        )

        return create_experimental_v4_application(
            self.authority,
            self.runtime,
            enable_v4_application=True,
            clock=lambda: self.NOW,
            uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            today=lambda: self.NOW[:10],
            task_note_source_indexes=self.conversion.task_note_source_indexes,
        )

    def stack_for(self, application):
        return WorkStack(
            application.store,
            initialize=False,
            capture_reply_commands=application.domain.capture_reply,
            intent_commands=application.domain.intents,
            objective_commands=application.domain.objectives,
            task_commands=application.domain.tasks,
        )

    def count_backend_calls(self, application) -> list:
        """Wrap the REAL command objects' methods, keeping their identity."""

        seen: list = []
        for label, target in (
            ("tasks", application.domain.tasks),
            ("intents", application.domain.intents),
        ):
            for name in ("patch_task", "set_task_status", "add_worklog"):
                original = getattr(target, name, None)
                if original is None:
                    continue

                def wrapped(*args, _label=label, _name=name, _original=original, **kwargs):
                    seen.append((_label, _name))
                    return _original(*args, **kwargs)

                setattr(target, name, wrapped)
        return seen

    def tree_bytes(self) -> dict:
        snapshot = {}
        for root in (self.authority, self.runtime.runtime_root):
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                snapshot[str(path)] = path.read_bytes()
        return snapshot

    def stored_revision(self, stack) -> int:
        return stack.get_task(self.task["id"])["revision"]

    # -- cases -------------------------------------------------------------
    def test_an_ordinary_unkeyed_scalar_patch_still_succeeds(self) -> None:
        application = self.application()
        stack = self.stack_for(application)
        seen = self.count_backend_calls(application)
        patched = stack.patch_task(self.task["id"], {"title": "Renamed", "revision": 0})
        self.assertEqual(patched["title"], "Renamed")
        self.assertTrue(seen, "the real v4 backend must actually be reached")

    def test_a_keyed_status_change_refuses_before_any_backend_call(self) -> None:
        application = self.application()
        stack = self.stack_for(application)
        seen = self.count_backend_calls(application)
        before_tree = self.tree_bytes()
        before_revision = self.stored_revision(stack)

        with self.assertRaises(DomainError):
            stack.set_task_status_v1(
                self.task["id"], {"status": "started", "revision": before_revision},
                "v4.change.0001",
                path="/api/v1/tasks/{}".format(self.task["id"]),
            )

        self.assertEqual(seen, [], "no Task or planning backend call may happen")
        self.assertEqual(self.tree_bytes(), before_tree, "authority and runtime bytes")
        self.assertEqual(self.stored_revision(stack), before_revision)

    def test_a_keyed_same_status_no_op_refuses_before_any_backend_call(self) -> None:
        application = self.application()
        stack = self.stack_for(application)
        current = stack.get_task(self.task["id"])
        seen = self.count_backend_calls(application)
        before_tree = self.tree_bytes()

        with self.assertRaises(DomainError):
            stack.set_task_status_v1(
                self.task["id"],
                {"status": current["status"], "revision": current["revision"]},
                "v4.noop.0001",
                path="/api/v1/tasks/{}".format(self.task["id"]),
            )

        self.assertEqual(seen, [], "a no-op is not an exception to the rule")
        self.assertEqual(self.tree_bytes(), before_tree)
        self.assertEqual(self.stored_revision(stack), current["revision"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
