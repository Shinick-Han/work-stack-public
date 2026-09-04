"""The public wire contract for the two approved D5 routes.

Every assertion parses the raw bytes a real ``Handler`` writes over an ephemeral
loopback server this test owns, driven by real HTTP requests against a real
contained ``Store``. The fixture shuts the server down with ``shutdown``,
``server_close`` and ``join`` before removing its root.

Nothing installed, live, browser-based or external is touched, and no product
entrypoint subprocess is launched: these routes need none.
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

ATTRIBUTED_HEADER = "X-WorkStack-Client"
ATTRIBUTED_VALUE = "agent-cli-v1"
AUDIT_PATH = "/api/v1/review/checkpoints"


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "transition-wire"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _WireCase(unittest.TestCase):
    def setUp(self) -> None:
        from workstack.server import create_server
        from workstack.service import WorkStack
        from workstack.store import Store

        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Wire transition")
        self.stack.add_worklog_v1(
            {
                "date": "2026-09-03", "task_id": self.task["id"],
                "done": ["one"], "next": [], "blockers": [],
            },
            "wire.entry.0001",
            origin=ATTRIBUTED_VALUE,
        )
        self.checkpoint = self.stack.list_checkpoint_audit()["entries"][0]["checkpoint_id"]
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])
        self.origin = "http://127.0.0.1:{}".format(self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------
    def transition_path(self, checkpoint: str | None = None) -> str:
        return "/api/v1/review/checkpoints/{}/transitions".format(
            checkpoint or self.checkpoint
        )

    def post(self, key, body, *, client=ATTRIBUTED_VALUE, checkpoint=None,
             raw_body: bytes | None = None):
        payload = raw_body if raw_body is not None else json.dumps(body).encode("utf-8")
        headers = {
            "Host": "127.0.0.1:{}".format(self.port),
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "Origin": self.origin,
            "X-WorkStack-CSRF": self.server.csrf_token,
            "Idempotency-Key": key,
        }
        if client is not None:
            headers[ATTRIBUTED_HEADER] = client
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST", self.transition_path(checkpoint), body=payload, headers=headers
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def get(self, path=AUDIT_PATH):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "GET", path, headers={"Host": "127.0.0.1:{}".format(self.port)}
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def raw_post(self, key, body, extra_headers, *, checkpoint=None) -> tuple[int, bytes]:
        payload = json.dumps(body).encode("utf-8")
        lines = [
            "POST {} HTTP/1.1".format(self.transition_path(checkpoint)),
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "X-WorkStack-CSRF: {}".format(self.server.csrf_token),
            "Idempotency-Key: {}".format(key),
            "Connection: close",
        ] + extra_headers
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

    def supersede_body(self, revision=0, explanation="because"):
        return {
            "state": "superseded",
            "revision": revision,
            "reason": {"code": "incorrect", "explanation": explanation},
        }

    def entry_count(self) -> int:
        worklog = self.stack.store.load("worklog.json")
        return sum(len(day.get("entries", [])) for day in worklog.get("days", {}).values())

    def events(self) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(
                "GET", "/api/v1/events",
                headers={"Host": "127.0.0.1:{}".format(self.port), "Last-Event-ID": "0"},
            )
            return connection.getresponse().read().decode("utf-8")
        finally:
            connection.close()

    def transition_frames(self, stream: str) -> list:
        """Transition payloads, selected by their frozen KIND.

        Both schemas travel under the approved workstack.change.v1 event
        name, so the payload kind is what distinguishes them, never the
        event name.
        """

        frames = []
        for block in stream.split("\n\n"):
            if "event: workstack.change.v1" not in block:
                continue
            for line in block.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if "transition_revision" in payload:
                        frames.append(payload)
        return frames


class AuditRoute(_WireCase):
    def test_the_audit_returns_the_exact_frozen_view(self) -> None:
        status, body = self.get()
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"data"})
        self.assertEqual(sorted(payload["data"]), ["entries", "workspace_uid"])
        entry = payload["data"]["entries"][0]
        self.assertEqual(
            sorted(entry),
            ["checkpoint_id", "entry", "locator", "recorded", "revision", "state", "transitions"],
        )

    def test_the_audit_accepts_no_query_parameters(self) -> None:
        status, body = self.get(AUDIT_PATH + "?date=2026-09-03")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_query")

    def test_the_audit_reflects_a_committed_transition(self) -> None:
        self.post("audit.trans.0001", self.supersede_body())
        payload = json.loads(self.get()[1])
        row = payload["data"]["entries"][0]
        self.assertEqual(row["state"], "superseded")
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["transitions"][0]["reason"]["code"], "incorrect")


class TransitionRoute(_WireCase):
    def test_a_fresh_transition_returns_201_with_the_eleven_field_event(self) -> None:
        status, body = self.post("fresh.0001", self.supersede_body())
        self.assertEqual(status, 201, body)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"data", "meta"})
        self.assertFalse(payload["meta"]["replayed"])
        self.assertEqual(len(payload["data"]), 11)
        self.assertEqual(payload["data"]["type"], "worklog.superseded")
        self.assertEqual(payload["data"]["reason"]["explanation"], "because")

    def test_a_matching_replay_returns_200_with_the_original_event(self) -> None:
        first = json.loads(self.post("replay.0001", self.supersede_body())[1])
        status, body = self.post("replay.0001", self.supersede_body())
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["meta"]["replayed"])
        self.assertEqual(payload["data"], first["data"])

    def test_the_superseded_proposal_route_does_not_exist(self) -> None:
        payload = json.dumps(self.supersede_body()).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST", "/api/v1/review/entries/{}/state".format(self.checkpoint),
                body=payload,
                headers={
                    "Host": "127.0.0.1:{}".format(self.port),
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Origin": self.origin,
                    "X-WorkStack-CSRF": self.server.csrf_token,
                    "Idempotency-Key": "superseded.0001",
                },
            )
            self.assertEqual(connection.getresponse().status, 404)
        finally:
            connection.close()

    def test_a_history_conflict_is_409_with_its_closed_code(self) -> None:
        self.post("conflict.0001", self.supersede_body())
        status, body = self.post("conflict.0002", self.supersede_body(revision=0))
        self.assertEqual(status, 409)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "checkpoint_transition_conflict")
        self.assertEqual(error["details"]["transition_code"], "stale_revision")

    def test_a_malformed_body_is_400_invalid_request(self) -> None:
        for body in (
            {"state": "unknown", "revision": 0, "reason": {"code": "incorrect", "explanation": "x"}},
            {"state": "superseded", "revision": 0},
            {"state": "superseded", "revision": 0, "reason": {"code": "restore", "explanation": "x"}},
            {"state": "superseded", "revision": -1,
             "reason": {"code": "incorrect", "explanation": "x"}},
        ):
            with self.subTest(body=body):
                status, raw = self.post("malformed.{:04d}".format(abs(hash(str(body))) % 10000), body)
                self.assertEqual(status, 400, raw)
                self.assertEqual(json.loads(raw)["error"]["code"], "invalid_request")
        self.assertEqual(self.entry_count(), 1, "no refused request may mutate")

    def test_an_unknown_checkpoint_is_a_locator_mismatch_conflict(self) -> None:
        status, body = self.post(
            "unknown.0001", self.supersede_body(), checkpoint="CP-" + "b" * 64
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads(body)["error"]["details"]["transition_code"], "locator_mismatch"
        )

    def test_a_noncanonical_checkpoint_path_refuses(self) -> None:
        status, body = self.post(
            "noncanon.0001", self.supersede_body(), checkpoint="CP-" + "B" * 64
        )
        self.assertIn(status, (400, 409), body)
        self.assertEqual(self.entry_count(), 1)

    def test_an_escaped_lone_surrogate_is_refused_without_echoing_it(self) -> None:
        """The digest cannot encode it, and that happens before the handler."""

        raw = b'{"state":"superseded","revision":0,"reason":{"code":"incorrect","explanation":"\\ud800"}}'
        status, body = self.post("surrogate.0001", None, raw_body=raw)
        self.assertEqual(status, 400, body)
        # The approved malformed taxonomy for this route is
        # invalid_request; an earlier version asserted invalid_body.
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        self.assertNotIn(b"ud800", body)
        self.assertEqual(self.entry_count(), 1)

    def test_a_duplicate_attribution_header_refuses_before_mutation(self) -> None:
        status, _ = self.raw_post(
            "duphdr.0001", self.supersede_body(),
            ["{}: {}".format(ATTRIBUTED_HEADER, ATTRIBUTED_VALUE),
             "x-workstack-client: {}".format(ATTRIBUTED_VALUE)],
        )
        self.assertEqual(status, 400)
        self.assertEqual(len(self.stack.list_checkpoint_audit()["entries"][0]["transitions"]), 0)

    def test_a_trailing_whitespace_attribution_value_refuses(self) -> None:
        status, _ = self.raw_post(
            "trailhdr.0001", self.supersede_body(),
            ["{}: {} ".format(ATTRIBUTED_HEADER, ATTRIBUTED_VALUE)],
        )
        self.assertEqual(status, 400)
        self.assertEqual(len(self.stack.list_checkpoint_audit()["entries"][0]["transitions"]), 0)

    def test_missing_csrf_still_refuses_before_replay(self) -> None:
        payload = json.dumps(self.supersede_body()).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST", self.transition_path(), body=payload,
                headers={
                    "Host": "127.0.0.1:{}".format(self.port),
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Origin": self.origin,
                    "Idempotency-Key": "nocsrf.0001",
                },
            )
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()


class TransitionNoticeOnTheWire(_WireCase):
    def test_an_attributed_transition_emits_its_own_typed_frame(self) -> None:
        self.post("frame.0001", self.supersede_body())
        stream = self.events()
        self.assertIn("event: workstack.change.v1\n", stream)
        self.assertNotIn("workstack.transition.v1", stream)
        frames = self.transition_frames(stream)
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            sorted(frames[0]),
            ["checkpoint_id", "date", "entry_digest", "event_id", "kind", "ordinal",
             "origin", "state", "task_id", "transition_revision", "workspace_uid"],
        )

    def test_the_frame_carries_no_reason_key_or_prose(self) -> None:
        self.post("leak.0001", self.supersede_body(explanation="a private note"))
        stream = self.events()
        for forbidden in ("a private note", "leak.0001", "incorrect", "explanation", "reason"):
            self.assertNotIn(forbidden, stream, forbidden)

    def test_a_browser_transition_emits_no_transition_frame(self) -> None:
        status, _ = self.post("browser.0001", self.supersede_body(), client=None)
        self.assertEqual(status, 201)
        self.assertEqual(self.transition_frames(self.events()), [])

    def test_the_committed_notice_and_the_transition_frame_coexist(self) -> None:
        self.post("mixed.0001", self.supersede_body())
        stream = self.events()
        self.assertIn("event: workstack.change.v1\n", stream)
        self.assertIn("event: sync\n", stream)
        self.assertEqual(len(self.transition_frames(stream)), 1)

    def test_the_sse_id_equals_the_transition_payload_event_id(self) -> None:
        self.post("ident.0001", self.supersede_body())
        block = [
            b for b in self.events().split("\n\n")
            if "transition_revision" in b
        ][0]
        identifier = int([l for l in block.splitlines() if l.startswith("id: ")][0][4:])
        payload = json.loads([l for l in block.splitlines() if l.startswith("data: ")][0][6:])
        self.assertEqual(identifier, payload["event_id"])


class TransitionResponseLoss(_WireCase):
    def test_a_committed_transition_whose_response_is_never_read_replays_once(self) -> None:
        payload = json.dumps(self.supersede_body()).encode("utf-8")
        lines = [
            "POST {} HTTP/1.1".format(self.transition_path()),
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "X-WorkStack-CSRF: {}".format(self.server.csrf_token),
            "Idempotency-Key: lost.0001",
            "{}: {}".format(ATTRIBUTED_HEADER, ATTRIBUTED_VALUE),
            "Connection: close",
        ]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        try:
            sock.sendall(request)
        finally:
            sock.close()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.stack.list_checkpoint_audit()["entries"][0]["transitions"]:
                break
            time.sleep(0.05)
        row = self.stack.list_checkpoint_audit()["entries"][0]
        self.assertEqual(len(row["transitions"]), 1, "the transition really committed")

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

        self.assertEqual(
            len(self.stack.list_checkpoint_audit()["entries"][0]["transitions"]), 1
        )
        self.assertEqual(len(self.transition_frames(self.events())), 1)


class ApprovedTransportEventName(_WireCase):
    """D5I-F3: both payload variants ride the approved transport event name."""

    def test_a_transition_frame_uses_the_approved_event_name(self) -> None:
        self.post("name.0001", self.supersede_body())
        stream = self.events()
        self.assertIn("event: workstack.change.v1\n", stream)
        self.assertNotIn(
            "workstack.transition.v1", stream,
            "the transport event name is not the payload schema name",
        )

    def test_committed_and_transition_frames_coexist_under_one_name(self) -> None:
        self.post("name.0002", self.supersede_body())
        frames = []
        for block in self.events().split("\n\n"):
            if "event: workstack.change.v1" not in block:
                continue
            for line in block.splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
        kinds = [frame["kind"] for frame in frames]
        self.assertIn("agent.checkpoint.committed", kinds)
        self.assertIn("agent.checkpoint.superseded", kinds)
        self.assertEqual(
            [len(f) for f in frames if f["kind"] == "agent.checkpoint.superseded"], [11]
        )
        self.assertEqual(
            [len(f) for f in frames if f["kind"] == "agent.checkpoint.committed"], [12]
        )


class TransitionParityOnTheWire(_WireCase):
    """D5I-F4: impossible parity refuses the batch before any header."""

    def publish_notice(self, **overrides):
        notice = {
            "event_id": 0,
            "kind": "agent.checkpoint.restored",
            "workspace_uid": self.stack.store.readiness.workspace_uid,
            "task_id": self.task["id"],
            "date": "2026-09-03",
            "checkpoint_id": self.checkpoint,
            "ordinal": 0,
            "entry_digest": "sha256:" + "a" * 64,
            "state": "active",
            "transition_revision": 1,
            "origin": "agent-cli-v1",
        }
        notice.update(overrides)
        self.stack.store.publish_change_notice(
            lambda event_id: dict(notice, event_id=event_id)
        )

    def test_an_impossible_parity_notice_refuses_the_whole_batch(self) -> None:
        """active must be an even positive revision; 1 is impossible."""

        self.publish_notice()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(
                "GET", "/api/v1/events",
                headers={"Host": "127.0.0.1:{}".format(self.port), "Last-Event-ID": "0"},
            )
            response = connection.getresponse()
            status = response.status
            body = response.read()
        finally:
            connection.close()
        self.assertEqual(status, 500, body[:200])
        self.assertNotIn(b"event:", body, "no SSE prefix may be written")
        self.assertEqual(json.loads(body)["error"]["code"], "internal_error")

    def test_a_superseded_even_revision_is_also_impossible(self) -> None:
        self.publish_notice(
            kind="agent.checkpoint.superseded", state="superseded", transition_revision=2
        )
        status, _ = self.get("/api/v1/events")
        self.assertEqual(status, 500)

    def test_a_healthy_mixed_batch_still_streams(self) -> None:
        self.post("parity.0001", self.supersede_body())
        stream = self.events()
        self.assertIn("event: workstack.change.v1\n", stream)
        self.assertIn("event: sync\n", stream)


class ErrorTaxonomy(_WireCase):
    """D5I-F5: the three actual wire cases the approved mapping fixes."""

    def test_a_malformed_checkpoint_path_is_400_invalid_request(self) -> None:
        status, body = self.post(
            "badcp.0001", self.supersede_body(), checkpoint="not-a-checkpoint"
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        self.assertEqual(self.entry_count(), 1)

    def test_an_absent_canonical_checkpoint_stays_409_locator_mismatch(self) -> None:
        """The healthy negative control the malformed case must not disturb."""

        status, body = self.post(
            "absentcp.0001", self.supersede_body(), checkpoint="CP-" + "b" * 64
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads(body)["error"]["details"]["transition_code"], "locator_mismatch"
        )

    def test_a_surrogate_body_is_400_invalid_request(self) -> None:
        raw = b'{"state":"superseded","revision":0,"reason":{"code":"incorrect","explanation":"\\ud800"}}'
        status, body = self.post("surrogate.0002", None, raw_body=raw)
        self.assertEqual(status, 400, body)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        self.assertNotIn(b"ud800", body)
        self.assertEqual(self.entry_count(), 1)

    def test_invalid_known_history_is_409_history_invalid(self) -> None:
        """A duplicated genuine transition is a history conflict, not a 400."""

        self.post("dup.0001", self.supersede_body())
        activity = self.stack.store.load("activity.json")
        duplicate = [
            r for r in activity["activity"] if r["type"] == "worklog.superseded"
        ][0]
        activity["activity"].append(dict(duplicate, id="E-009999"))
        self.stack.store.save("activity.json", activity)

        status, body = self.get()
        self.assertEqual(status, 409, body)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "checkpoint_transition_conflict")
        self.assertEqual(error["details"]["transition_code"], "history_invalid")


class DuplicateWithBindingFaultOnTheWire(_WireCase):
    """D5F-F1 at the real owner: the public conflict reason must be binding."""

    def recorded_records(self, activity):
        return [r for r in activity["activity"] if r["type"] == "worklog.recorded"]

    def persist_duplicate(self, **transition_overrides):
        activity = self.stack.store.load("activity.json")
        template = self.recorded_records(activity)[0]["details"]
        activity["activity"].append({
            "id": "E-009101", "type": "worklog.recorded",
            "created_at": "2026-09-03T00:00:00Z", "task_id": template["task_id"],
            "details": dict(template, checkpoint_id="CP-" + "c" * 64),
        })
        if transition_overrides:
            event = {
                "type": "worklog.superseded",
                "workspace_uid": template["workspace_uid"],
                "task_id": template["task_id"],
                "checkpoint_id": template["checkpoint_id"],
                "date": template["date"],
                "ordinal": template["ordinal"],
                "entry_digest": template["entry_digest"],
                "state": "superseded",
                "revision": 1,
                "reason": {"code": "incorrect", "explanation": "x"},
                "origin": None,
            }
            event.update(transition_overrides)
            activity["activity"].append({
                "id": "E-009102", "type": "worklog.superseded",
                "created_at": "2026-09-03T00:00:00Z", "task_id": event["task_id"],
                "details": event,
            })
        self.stack.store.save("activity.json", activity)

    def documents(self):
        return {
            name: (self.root / name).read_bytes()
            for name in ("worklog.json", "activity.json", "backlog.json")
        }

    def test_a_duplicate_with_a_wrong_task_transition_is_a_locator_mismatch(self) -> None:
        self.persist_duplicate(task_id="T-0002")
        before = self.documents()
        before_events = self.stack.store.sync_events(0)["latest_event_id"]

        status, body = self.post("bindorder.0001", self.supersede_body())
        self.assertEqual(status, 409, body)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "checkpoint_transition_conflict")
        self.assertEqual(
            error["details"]["transition_code"], "locator_mismatch",
            "binding outranks the duplicate association verdict",
        )
        self.assertEqual(self.documents(), before, "no document may change")
        self.assertEqual(
            self.stack.store.sync_events(0)["latest_event_id"], before_events,
            "no event may be published",
        )

    def test_a_matching_duplicate_alone_remains_a_history_conflict(self) -> None:
        self.persist_duplicate()
        status, body = self.post("bindorder.0002", self.supersede_body())
        self.assertEqual(status, 409, body)
        self.assertEqual(
            json.loads(body)["error"]["details"]["transition_code"], "history_invalid"
        )

    def test_the_healthy_transition_control_still_commits(self) -> None:
        status, body = self.post("bindorder.0003", self.supersede_body())
        self.assertEqual(status, 201, body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
