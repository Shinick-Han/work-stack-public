"""Public wire contract for attributed checkpoint publication.

Every assertion parses the raw bytes a real ``Handler`` writes over an ephemeral
loopback server this test owns, driven by real HTTP requests against a real
contained ``Store``. No private helper call is asserted for the route itself.

Against the pre-implementation baseline these are RED: an unknown or repeated
``X-WorkStack-Client`` was accepted, and a correctly attributed checkpoint
committed with no typed frame on ``/api/v1/events``.

The fixture owns its server and shuts it down with ``shutdown``,
``server_close`` and ``join`` before the temporary root is removed. Nothing
installed, live, browser-based or external is touched, and no product
entrypoint subprocess is launched: this route needs none.
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


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "checkpoint-wire"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _WireCase(unittest.TestCase):
    """A real Store, a real server, a real socket."""

    def setUp(self) -> None:
        from workstack.server import create_server
        from workstack.service import WorkStack
        from workstack.store import Store

        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Wire boundary")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])
        self.origin = "http://127.0.0.1:{}".format(self.port)

    def tearDown(self) -> None:
        # Strict, ordered teardown: the socket first, then the thread, and only
        # then the root, so no lease or handle survives the removal.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.temporary.cleanup()

    # -- wire helpers ----------------------------------------------------
    def post_entry(self, key: str, *, client=None, date="2026-09-03", done=("one",)):
        body = json.dumps({
            "date": date,
            "task_id": self.task["id"],
            "done": list(done),
            "next": [],
            "blockers": [],
        }).encode("utf-8")
        headers = {
            "Host": "127.0.0.1:{}".format(self.port),
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": self.origin,
            "X-WorkStack-CSRF": self.server.csrf_token,
            "Idempotency-Key": key,
        }
        if client is not None:
            headers[ATTRIBUTED_HEADER] = client
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request("POST", "/api/v1/review/entries", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def post_entry_raw(self, key: str, client_values: list[str]) -> int:
        """Hand-built request, so the attributed header can legitimately repeat."""

        payload = json.dumps({
            "date": "2026-09-03",
            "task_id": self.task["id"],
            "done": ["one"],
            "next": [],
            "blockers": [],
        }).encode("utf-8")
        lines = [
            "POST /api/v1/review/entries HTTP/1.1",
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "X-WorkStack-CSRF: {}".format(self.server.csrf_token),
            "Idempotency-Key: {}".format(key),
            "Connection: close",
        ]
        lines.extend("{}: {}".format(ATTRIBUTED_HEADER, value) for value in client_values)
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
        return int(raw.split(b" ", 2)[1])

    def events(self, cursor: int = 0) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(
                "GET", "/api/v1/events",
                headers={
                    "Host": "127.0.0.1:{}".format(self.port),
                    "Last-Event-ID": str(cursor),
                },
            )
            response = connection.getresponse()
            return response.read().decode("utf-8")
        finally:
            connection.close()

    def typed_frames(self, stream: str) -> list[dict]:
        frames = []
        for block in stream.split("\n\n"):
            if "event: workstack.change.v1" not in block:
                continue
            for line in block.splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
        return frames


class AttributedHeaderPolicy(_WireCase):
    """D3: parsed only here, and refused before any mutation."""

    def entry_count(self) -> int:
        worklog = self.stack.store.load("worklog.json")
        return sum(len(day.get("entries", [])) for day in worklog.get("days", {}).values())

    def test_a_missing_header_commits_with_no_agent_notice(self) -> None:
        status, _ = self.post_entry("wire.missing.0001")
        self.assertEqual(status, 201)
        self.assertEqual(self.entry_count(), 1)
        self.assertEqual(self.typed_frames(self.events()), [])

    def test_the_exact_single_value_attributes_and_publishes(self) -> None:
        status, _ = self.post_entry("wire.exact.0001", client=ATTRIBUTED_VALUE)
        self.assertEqual(status, 201)
        frames = self.typed_frames(self.events())
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["origin"], ATTRIBUTED_VALUE)
        self.assertEqual(frames[0]["task_id"], self.task["id"])

    def test_empty_and_unknown_values_refuse_before_mutation(self) -> None:
        """Exact match on the PARSED field value: no folding, no prefix.

        An earlier version of this comment claimed the HTTP layer strips
        whitespace at BOTH ends. That was wrong. The pinned parser
        (``email._policybase.header_source_parse``) strips leading SP and HTAB
        and the trailing CRLF only; a trailing SP or HTAB survives into the
        parsed value. Those cases are asserted separately below, on raw wire
        bytes, with the actual parsed value recorded.
        """

        for index, value in enumerate(
            ("", "browser", "AGENT-CLI-V1", "agent-cli-v1x", "agent-cli", "agent-cli-v10")
        ):
            with self.subTest(value=repr(value)):
                status, _ = self.post_entry("wire.bad.{:04d}".format(index), client=value)
                self.assertEqual(status, 400, value)
        self.assertEqual(self.entry_count(), 0, "no refused request may have written")
        self.assertEqual(self.typed_frames(self.events()), [])

    def test_a_repeated_identical_value_is_still_refused(self) -> None:
        status = self.post_entry_raw("wire.repeat.0001", [ATTRIBUTED_VALUE, ATTRIBUTED_VALUE])
        self.assertEqual(status, 400)
        self.assertEqual(self.entry_count(), 0)

    def test_a_repeated_differing_value_is_refused(self) -> None:
        status = self.post_entry_raw("wire.repeat.0002", [ATTRIBUTED_VALUE, "browser"])
        self.assertEqual(status, 400)
        self.assertEqual(self.entry_count(), 0)

    def test_existing_origin_and_csrf_enforcement_is_unchanged(self) -> None:
        body = json.dumps({
            "date": "2026-09-03", "task_id": self.task["id"],
            "done": ["one"], "next": [], "blockers": [],
        }).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST", "/api/v1/review/entries", body=body,
                headers={
                    "Host": "127.0.0.1:{}".format(self.port),
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Origin": self.origin,
                    "Idempotency-Key": "wire.csrf.0001",
                    ATTRIBUTED_HEADER: ATTRIBUTED_VALUE,
                },
            )
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()
        self.assertEqual(self.entry_count(), 0)


class LostResponseRetry(_WireCase):
    """A lost response after a real commit yields one fact and one event."""

    def recorded(self) -> list[dict]:
        activity = self.stack.store.load("activity.json")
        return [r for r in activity.get("activity", []) if r.get("type") == "worklog.recorded"]

    def test_an_identical_retry_after_a_real_commit_publishes_once(self) -> None:
        first_status, first_body = self.post_entry("wire.retry.0001", client=ATTRIBUTED_VALUE)
        self.assertEqual(first_status, 201)
        self.assertEqual(len(self.recorded()), 1, "the commit really happened")

        # The caller lost that response and retries the identical bytes under
        # the identical key. Only the reply differs: it is marked replayed.
        second_status, second_body = self.post_entry("wire.retry.0001", client=ATTRIBUTED_VALUE)
        self.assertEqual(second_status, 200)
        self.assertTrue(json.loads(second_body)["meta"]["replayed"])
        self.assertFalse(json.loads(first_body)["meta"]["replayed"])
        self.assertEqual(json.loads(first_body)["data"], json.loads(second_body)["data"])

        self.assertEqual(len(self.recorded()), 1, "a retry records no second fact")
        frames = self.typed_frames(self.events())
        self.assertEqual(len(frames), 1, "a replay publishes no second event")
        self.assertIs(frames[0]["replayed"], False)


class TypedFrameOnTheWire(_WireCase):
    """D2: the frame a real client actually receives."""

    def test_the_frame_carries_exactly_the_twelve_fields_and_its_own_name(self) -> None:
        self.post_entry("wire.frame.0001", client=ATTRIBUTED_VALUE)
        stream = self.events()
        self.assertIn("event: workstack.change.v1\n", stream)
        frames = self.typed_frames(stream)
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            set(frames[0]),
            {
                "event_id", "kind", "workspace_uid", "task_id", "date",
                "checkpoint_id", "done_count", "next_count", "blocker_count",
                "first_for_task", "origin", "replayed",
            },
        )

    def test_the_sse_id_equals_the_payload_event_id(self) -> None:
        self.post_entry("wire.ident.0001", client=ATTRIBUTED_VALUE)
        stream = self.events()
        block = [b for b in stream.split("\n\n") if "workstack.change.v1" in b][0]
        identifier = int([l for l in block.splitlines() if l.startswith("id: ")][0][4:])
        payload = json.loads([l for l in block.splitlines() if l.startswith("data: ")][0][6:])
        self.assertEqual(identifier, payload["event_id"])

    def test_no_raw_key_title_or_item_text_reaches_the_wire(self) -> None:
        self.post_entry("wire.leak.0001", client=ATTRIBUTED_VALUE, done=("a secret item",))
        stream = self.events()
        for forbidden in ("wire.leak.0001", "a secret item", "Wire boundary", "csrf"):
            self.assertNotIn(forbidden, stream, forbidden)

    def test_the_legacy_sync_frame_still_arrives_for_an_ordinary_change(self) -> None:
        self.stack.add_task("Ordinary change")
        stream = self.events()
        self.assertIn("event: sync\n", stream)
        self.assertEqual(self.typed_frames(stream), [])


class ParsedHeaderExactness(_WireCase):
    """TP-F5: exactness applies to the ONE parsed field value.

    Header NAMES are case-insensitive. Leading optional whitespace is erased by
    the pinned HTTP parser before the handler can see it, so a value that
    arrives exact after that erasure is accepted; the parser is deliberately not
    modified to recover it. Trailing SP and HTAB survive and must refuse with
    zero writes. Attribution remains provenance, never authentication.
    """

    def entry_count(self) -> int:
        worklog = self.stack.store.load("worklog.json")
        return sum(len(day.get("entries", [])) for day in worklog.get("days", {}).values())

    def parsed_value(self, raw_value: str) -> str | None:
        """What the pinned parser actually hands the handler for this field."""

        import email.parser

        message = email.parser.Parser().parsestr(
            "{}:{}\r\n\r\n".format(ATTRIBUTED_HEADER, raw_value)
        )
        return message.get(ATTRIBUTED_HEADER)

    def post_raw_header(self, key: str, header_lines: list[str]) -> int:
        payload = json.dumps({
            "date": "2026-09-03", "task_id": self.task["id"],
            "done": ["one"], "next": [], "blockers": [],
        }).encode("utf-8")
        lines = [
            "POST /api/v1/review/entries HTTP/1.1",
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "X-WorkStack-CSRF: {}".format(self.server.csrf_token),
            "Idempotency-Key: {}".format(key),
            "Connection: close",
        ] + header_lines
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as sock:
            sock.sendall(request)
            chunks = []
            while True:
                received = sock.recv(65536)
                if not received:
                    break
                chunks.append(received)
        return int(b"".join(chunks).split(b" ", 2)[1])

    # -- accepted: the parsed value is exact ------------------------------
    def test_a_normal_single_separator_space_is_accepted(self) -> None:
        self.assertEqual(self.parsed_value(" agent-cli-v1"), "agent-cli-v1")
        status = self.post_raw_header("f5.normal.0001", ["X-WorkStack-Client: agent-cli-v1"])
        self.assertEqual(status, 201)
        self.assertEqual(self.entry_count(), 1)

    def test_extra_leading_space_and_htab_are_erased_and_accepted(self) -> None:
        for raw in ("   agent-cli-v1", "\tagent-cli-v1", " \t agent-cli-v1"):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(
                    self.parsed_value(raw), "agent-cli-v1",
                    "the pinned parser erases leading SP and HTAB",
                )
        status = self.post_raw_header(
            "f5.leading.0001", ["X-WorkStack-Client:\t  agent-cli-v1"]
        )
        self.assertEqual(status, 201, "an exact value after erasure is accepted")
        self.assertEqual(self.entry_count(), 1)

    def test_a_lowercase_header_name_is_accepted(self) -> None:
        status = self.post_raw_header("f5.name.0001", ["x-workstack-client: agent-cli-v1"])
        self.assertEqual(status, 201, "header names are case-insensitive")
        self.assertEqual(self.entry_count(), 1)

    # -- refused: the parsed value is not exact ---------------------------
    def test_a_trailing_space_survives_the_parser_and_refuses(self) -> None:
        self.assertEqual(
            self.parsed_value(" agent-cli-v1 "), "agent-cli-v1 ",
            "the pinned parser does NOT strip a trailing SP",
        )
        status = self.post_raw_header("f5.trail.0001", ["X-WorkStack-Client: agent-cli-v1 "])
        self.assertEqual(status, 400)
        self.assertEqual(self.entry_count(), 0, "a refused request writes nothing")

    def test_a_trailing_htab_survives_the_parser_and_refuses(self) -> None:
        self.assertEqual(self.parsed_value(" agent-cli-v1\t"), "agent-cli-v1\t")
        status = self.post_raw_header("f5.trail.0002", ["X-WorkStack-Client: agent-cli-v1\t"])
        self.assertEqual(status, 400)
        self.assertEqual(self.entry_count(), 0)

    def test_comma_joined_values_refuse(self) -> None:
        status = self.post_raw_header(
            "f5.comma.0001", ["X-WorkStack-Client: agent-cli-v1,agent-cli-v1"]
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.entry_count(), 0)

    def test_duplicate_fields_with_differing_name_casing_refuse(self) -> None:
        status = self.post_raw_header(
            "f5.dup.0001",
            ["X-WorkStack-Client: agent-cli-v1", "x-workstack-client: agent-cli-v1"],
        )
        self.assertEqual(status, 400, "two parsed values are never one exact value")
        self.assertEqual(self.entry_count(), 0)


class LostResponseBeforeAnyReply(_WireCase):
    """The owner really commits, then the connection closes with no reply."""

    def recorded(self) -> list[dict]:
        activity = self.stack.store.load("activity.json")
        return [r for r in activity.get("activity", []) if r.get("type") == "worklog.recorded"]

    def raw_request(self, key: str) -> bytes:
        payload = json.dumps({
            "date": "2026-09-03", "task_id": self.task["id"],
            "done": ["one"], "next": [], "blockers": [],
        }).encode("utf-8")
        lines = [
            "POST /api/v1/review/entries HTTP/1.1",
            "Host: 127.0.0.1:{}".format(self.port),
            "Content-Type: application/json",
            "Content-Length: {}".format(len(payload)),
            "Origin: {}".format(self.origin),
            "X-WorkStack-CSRF: {}".format(self.server.csrf_token),
            "Idempotency-Key: {}".format(key),
            "{}: {}".format(ATTRIBUTED_HEADER, ATTRIBUTED_VALUE),
            "Connection: close",
        ]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload

    def test_a_commit_whose_response_is_never_read_replays_exactly_once(self) -> None:
        request = self.raw_request("wire.lost.0001")

        # Send the bytes, then close WITHOUT reading any response at all. The
        # owner commits; the caller never learns the outcome.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        try:
            sock.sendall(request)
        finally:
            sock.close()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.recorded():
            time.sleep(0.05)
        self.assertEqual(len(self.recorded()), 1, "the owner really committed")

        # The identical raw bytes under the identical key, this time read.
        retry = self.raw_request("wire.lost.0001")
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
        body = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        self.assertTrue(body["meta"]["replayed"])

        self.assertEqual(len(self.recorded()), 1, "the lost commit is not repeated")
        frames = self.typed_frames(self.events())
        self.assertEqual(len(frames), 1, "exactly one typed event survives the loss")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
