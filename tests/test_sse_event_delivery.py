"""Public wire contract for the ``/api/v1/events`` sync stream.

Every assertion here parses the raw bytes a real ``Handler`` writes over an
ephemeral loopback fixture this test owns. No private helper call is asserted.

A stand-in store supplies controlled batches. It implements only the surface the
server actually touches, so no ``Store`` is constructed: there is no data
directory, no writer lease and no runtime secret anywhere on disk. That is what
lets a batch be shaped deliberately, including shapes a healthy Store would
never produce.

Against the pre-implementation baseline these are RED: the endpoint collapsed
any retained batch onto ``latest_event_id`` and emitted a single frame.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

RETRY_LINE = "retry: 3000"
SNAPSHOT_KEYS = {"generation", "state"}
FORBIDDEN_SUBSTRINGS = (
    "changed_files",
    "workspace_id",
    "journal",
    "csrf",
    "token",
    "prompt",
    "C:\\",
)


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "sse-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _NullLease:
    def __enter__(self) -> "_NullLease":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _StandInStore:
    """Only the surface ``create_server`` and the events endpoint touch."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}
        self.cursors: list[int] = []
        self.server_info: tuple[str, int] | None = None

    # -- required to stand a server up ----------------------------------
    def server_lease(self) -> _NullLease:
        return _NullLease()

    def initialize(self) -> None:
        return None

    def write_runtime_secret(self, token: str) -> None:
        return None

    def write_server_info(self, host: str, port: int) -> None:
        self.server_info = (host, port)

    def clear_server_runtime(self) -> None:
        self.server_info = None

    # -- what the events endpoint reads ---------------------------------
    def wait_for_sync_events(self, after: int, timeout: float = 15.0) -> dict[str, Any]:
        self.cursors.append(after)
        return self.payload


class _StandInStack:
    def __init__(self, store: _StandInStore) -> None:
        self.store = store


def snapshot_batch(
    identifiers: tuple[int, ...],
    *,
    latest: int | None = None,
    generation: int = 7,
    state: str = "in-sync",
) -> dict[str, Any]:
    """A batch shaped exactly as ``Store.sync_events`` returns one."""

    return {
        "delivery": "bounded-process-local",
        "latest_event_id": max(identifiers) if latest is None else latest,
        "generation": generation,
        "state": state,
        "events": [
            {
                "id": identifier,
                "type": "store.committed",
                "workspace_id": "11111111-2222-4333-8444-555555555555",
                "generation": generation,
                "changed_files": ["backlog.json"],
            }
            for identifier in identifiers
        ],
    }


class _SseCase(unittest.TestCase):
    """Own the fixture server and shut it down gracefully."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.addCleanup(self.temporary.cleanup)
        self._saved = {
            name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")
        }
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = self.temporary.name
        self.addCleanup(self._restore)

        from workstack.server import create_server

        self.store = _StandInStore()
        self.server = create_server(_StandInStack(self.store), "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    # -- wire helpers ----------------------------------------------------
    def request(self, *, cursor: str | None = "0", query: str = "", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            outgoing = {"Host": "127.0.0.1:{}".format(self.port)}
            if cursor is not None:
                outgoing["Last-Event-ID"] = cursor
            if headers:
                outgoing.update(headers)
            connection.request("GET", "/api/v1/events" + query, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def request_raw(self, raw_lines: list[str]) -> tuple[int, bytes]:
        """Send a hand-built request, so a header can legitimately repeat."""

        import socket

        request = "GET /api/v1/events HTTP/1.1\r\n" + "".join(
            line + "\r\n" for line in raw_lines
        ) + "\r\n"
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as sock:
            sock.sendall(request.encode("ascii"))
            chunks = []
            while True:
                received = sock.recv(65536)
                if not received:
                    break
                chunks.append(received)
                if b"\r\n\r\n" in b"".join(chunks) and len(b"".join(chunks)) > 0:
                    # One bounded read is enough: every response carries
                    # Content-Length and the fixture never streams.
                    try:
                        sock.settimeout(0.5)
                    except OSError:
                        pass
        raw = b"".join(chunks)
        status = int(raw.split(b" ", 2)[1])
        return status, raw

    def frames(self, body: bytes) -> list[dict[str, str]]:
        """Parse SSE frames into field dictionaries, preserving order."""

        parsed = []
        for block in body.decode("utf-8").split("\n\n"):
            if not block.strip():
                continue
            fields: dict[str, str] = {}
            comments = []
            for line in block.split("\n"):
                if line.startswith(":"):
                    comments.append(line[1:].strip())
                    continue
                name, _, value = line.partition(":")
                fields[name.strip()] = value.strip()
            if comments:
                fields["__comments__"] = ",".join(comments)
            parsed.append(fields)
        return parsed


class OrderedDeliveryContract(_SseCase):
    def test_three_retained_ids_emit_three_ordered_frames(self) -> None:
        self.store.payload = snapshot_batch((1, 2, 3))

        status, headers, body = self.request(cursor="0")

        self.assertEqual(status, 200)
        frames = self.frames(body)
        self.assertEqual([frame["id"] for frame in frames], ["1", "2", "3"])
        self.assertEqual([frame["event"] for frame in frames], ["sync"] * 3)
        for frame in frames:
            self.assertEqual(frame["retry"], "3000")
            self.assertEqual(set(json.loads(frame["data"])), SNAPSHOT_KEYS)

    def test_cursor_suffix_emits_only_ids_after_the_cursor(self) -> None:
        self.store.payload = snapshot_batch((2, 3), latest=3)

        status, _headers, body = self.request(cursor="1")

        self.assertEqual(status, 200)
        frames = self.frames(body)
        self.assertEqual([frame["id"] for frame in frames], ["2", "3"])
        self.assertEqual(self.store.cursors, [1], "the cursor must reach the store")

    def test_the_last_id_is_the_last_retained_record_not_the_latest_sequence(self) -> None:
        """A browser must not be jumped past an omitted retained record."""

        self.store.payload = snapshot_batch((2, 3), latest=9)

        _status, _headers, body = self.request(cursor="1")

        frames = self.frames(body)
        self.assertEqual(frames[-1]["id"], "3")
        self.assertNotIn("9", [frame["id"] for frame in frames])

    def test_a_legitimate_retained_gap_is_delivered_not_rejected(self) -> None:
        """The Store deque is bounded, so evicted ids leave honest gaps."""

        self.store.payload = snapshot_batch((7, 9, 12), latest=12)

        status, _headers, body = self.request(cursor="4")

        self.assertEqual(status, 200)
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["7", "9", "12"])

    def test_a_single_retained_id_still_emits_one_frame(self) -> None:
        self.store.payload = snapshot_batch((5,), latest=5)

        status, _headers, body = self.request(cursor="4")

        self.assertEqual(status, 200)
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["5"])

    def test_the_full_retention_bound_is_delivered(self) -> None:
        identifiers = tuple(range(1, 129))
        self.store.payload = snapshot_batch(identifiers, latest=128)

        status, _headers, body = self.request(cursor="0")

        self.assertEqual(status, 200)
        self.assertEqual(len(self.frames(body)), 128)


class HeartbeatContract(_SseCase):
    def test_empty_retained_list_emits_a_content_free_heartbeat(self) -> None:
        self.store.payload = snapshot_batch((), latest=9)

        status, headers, body = self.request(cursor="9")

        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn(":", text)
        for absent in ("id:", "event:", "data:"):
            self.assertNotIn(absent, text, "heartbeat must carry no {}".format(absent))
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")

    def test_heartbeat_does_not_advance_the_cursor_even_when_ids_were_evicted(self) -> None:
        """Nothing retained after the cursor, but the sequence has moved on."""

        self.store.payload = snapshot_batch((), latest=40)

        _status, _headers, body = self.request(cursor="1")

        self.assertNotIn("id:", body.decode("utf-8"))
        self.assertNotIn("40", body.decode("utf-8"))


class SnapshotFieldContract(_SseCase):
    def test_frames_carry_only_the_generation_and_state_snapshot(self) -> None:
        self.store.payload = snapshot_batch((1, 2))

        _status, _headers, body = self.request(cursor="0")

        for frame in self.frames(body):
            self.assertEqual(set(json.loads(frame["data"])), SNAPSHOT_KEYS)
            self.assertEqual(set(frame) - {"__comments__"}, {"retry", "id", "event", "data"})

    def test_poisoned_store_fields_never_reach_the_wire(self) -> None:
        payload = snapshot_batch((1,))
        payload["events"][0].update({
            "journal": "secret journal text",
            "workspace_path": r"C:\Users\someone\WorkStack",
            "csrf_token": "must-not-appear",
            "prompt": "must-not-appear",
        })
        payload["extra_top_level"] = "must-not-appear"
        self.store.payload = payload

        _status, _headers, body = self.request(cursor="0")

        text = body.decode("utf-8")
        for forbidden in FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, text)
        self.assertNotIn("must-not-appear", text)
        self.assertNotIn("extra_top_level", text)

    def test_every_supported_state_is_carried_verbatim(self) -> None:
        for state in ("in-sync", "external-change-detected", "invalid"):
            with self.subTest(state=state):
                self.store.payload = snapshot_batch((1,), state=state)
                _status, _headers, body = self.request(cursor="0")
                data = json.loads(self.frames(body)[0]["data"])
                self.assertEqual(data["state"], state)

    def test_the_repeated_snapshot_is_the_documented_behaviour(self) -> None:
        """Each frame reports the same current snapshot; it is a refetch hint."""

        self.store.payload = snapshot_batch((1, 2, 3), generation=11)

        _status, _headers, body = self.request(cursor="0")

        payloads = [json.loads(frame["data"]) for frame in self.frames(body)]
        self.assertEqual(payloads, [{"generation": 11, "state": "in-sync"}] * 3)


class HeaderAndSecurityContract(_SseCase):
    def test_successful_response_headers_are_preserved(self) -> None:
        self.store.payload = snapshot_batch((1, 2))

        status, headers, body = self.request(cursor="0")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers.get("X-WorkStack-Request-Id"))

    def test_a_query_string_is_still_rejected(self) -> None:
        self.store.payload = snapshot_batch((1,))

        status, _headers, body = self.request(cursor="0", query="?after=1")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_query")
        self.assertEqual(self.store.cursors, [], "no batch may be requested")

    def test_a_malformed_cursor_is_still_rejected(self) -> None:
        for cursor in ("abc", "1.5", "0x2", "+-1", "1e3"):
            with self.subTest(cursor=cursor):
                self.store.cursors.clear()
                self.store.payload = snapshot_batch((1,))
                status, _headers, body = self.request(cursor=cursor)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid_header")
                self.assertEqual(self.store.cursors, [])

    def test_an_empty_cursor_header_still_means_the_beginning(self) -> None:
        """Existing contract, asserted rather than assumed.

        ``self._header_once("Last-Event-ID") or "0"`` treats an empty header as
        absent, so it is a cursor of 0 and not a client error. This slice
        preserves that; it is recorded here so a future change is deliberate.
        """

        self.store.payload = snapshot_batch((1, 2))

        status, _headers, body = self.request(cursor="")

        self.assertEqual(status, 200)
        self.assertEqual(self.store.cursors, [0])
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["1", "2"])

    def test_surrounding_whitespace_in_the_cursor_is_still_tolerated(self) -> None:
        """Existing contract: ``int(" 1")`` parses, so " 1" is a cursor of 1."""

        self.store.payload = snapshot_batch((2,), latest=2)

        status, _headers, body = self.request(cursor=" 1 ")

        self.assertEqual(status, 200)
        self.assertEqual(self.store.cursors, [1])
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["2"])

    def test_a_negative_cursor_is_still_rejected(self) -> None:
        self.store.payload = snapshot_batch((1,))

        status, _headers, body = self.request(cursor="-1")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_header")
        self.assertEqual(self.store.cursors, [])

    def test_a_duplicate_cursor_header_is_still_rejected(self) -> None:
        self.store.payload = snapshot_batch((1,))

        status, raw = self.request_raw([
            "Host: 127.0.0.1:{}".format(self.port),
            "Last-Event-ID: 1",
            "Last-Event-ID: 2",
            "Connection: close",
        ])

        self.assertEqual(status, 400)
        self.assertIn(b"invalid_header", raw)
        self.assertEqual(self.store.cursors, [])

    def test_a_missing_cursor_defaults_to_the_beginning(self) -> None:
        self.store.payload = snapshot_batch((1, 2))

        status, _headers, body = self.request(cursor=None)

        self.assertEqual(status, 200)
        self.assertEqual(self.store.cursors, [0])
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["1", "2"])


class InvalidInternalBatchContract(_SseCase):
    """A bad batch is this process's fault, not the caller's."""

    def assertInternalRefusal(self, payload: dict[str, Any], *, cursor: str = "0") -> None:
        self.store.payload = payload
        status, headers, body = self.request(cursor=cursor)
        self.assertEqual(status, 500)
        document = json.loads(body)
        self.assertEqual(document["error"]["code"], "internal_error")
        self.assertEqual(document["error"]["details"], {})
        self.assertNotEqual(
            headers.get("Content-Type"), "text/event-stream; charset=utf-8",
            "a refused batch must not open an event stream",
        )
        text = body.decode("utf-8")
        for absent in ("event: sync", "data:", "retry:"):
            self.assertNotIn(absent, text, "no partial SSE body may be written")

    def test_out_of_order_ids_refuse(self) -> None:
        self.assertInternalRefusal(snapshot_batch((3, 1, 2), latest=3))

    def test_duplicate_ids_refuse(self) -> None:
        self.assertInternalRefusal(snapshot_batch((1, 2, 2), latest=3))

    def test_an_id_at_or_before_the_cursor_refuses(self) -> None:
        self.assertInternalRefusal(snapshot_batch((1, 2), latest=2), cursor="1")

    def test_an_id_beyond_the_latest_refuses(self) -> None:
        self.assertInternalRefusal(snapshot_batch((1, 9), latest=3))

    def test_a_batch_beyond_the_retention_bound_refuses(self) -> None:
        identifiers = tuple(range(1, 130))
        self.assertInternalRefusal(snapshot_batch(identifiers, latest=129))

    def test_a_boolean_id_refuses(self) -> None:
        payload = snapshot_batch((1,))
        payload["events"][0]["id"] = True
        self.assertInternalRefusal(payload)

    def test_a_non_integer_id_refuses(self) -> None:
        payload = snapshot_batch((1,))
        payload["events"][0]["id"] = "1"
        self.assertInternalRefusal(payload)

    def test_a_negative_generation_refuses(self) -> None:
        self.assertInternalRefusal(snapshot_batch((1,), generation=-1))

    def test_a_boolean_generation_refuses(self) -> None:
        payload = snapshot_batch((1,))
        payload["generation"] = True
        self.assertInternalRefusal(payload)

    def test_an_unsupported_state_refuses(self) -> None:
        self.assertInternalRefusal(snapshot_batch((1,), state="chaotic"))

    def test_a_non_sequence_events_field_refuses(self) -> None:
        payload = snapshot_batch((1,))
        payload["events"] = {"id": 1}
        self.assertInternalRefusal(payload)

    def test_a_non_object_event_record_refuses(self) -> None:
        payload = snapshot_batch((1,))
        payload["events"] = ["1"]
        self.assertInternalRefusal(payload)

    def test_a_healthy_batch_through_the_same_path_still_succeeds(self) -> None:
        """The control: refusal-only behaviour would not pass this."""

        self.store.payload = snapshot_batch((4, 5), latest=5)
        status, headers, body = self.request(cursor="3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["4", "5"])


class MalformedTopLevelBatchContract(_SseCase):
    """A batch that is not an object at all must still answer, not drop.

    Reaching for a key before classifying the shape raised AttributeError, which
    is not SseEncodingError, so the narrow handler did not catch it and the
    connection closed with no response. These cases pin the wire outcome, not
    the internal exception type.
    """

    CANARY = "top-level-batch-canary-must-not-be-printed"

    def assertSanitizedRefusal(self, payload: Any) -> None:
        self.store.payload = payload
        status, headers, body = self.request(cursor="0")

        self.assertEqual(status, 500, "the connection must answer, not be dropped")
        document = json.loads(body)
        self.assertEqual(document["error"]["code"], "internal_error")
        self.assertEqual(document["error"]["details"], {})
        self.assertNotEqual(
            headers.get("Content-Type"), "text/event-stream; charset=utf-8",
            "a refused batch must not open an event stream",
        )
        text = body.decode("utf-8")
        for absent in ("event: sync", "data:", "retry:", "id:"):
            self.assertNotIn(absent, text, "no partial SSE body may be written")
        self.assertNotIn(self.CANARY, text, "no raw payload may be echoed")

    def test_a_null_batch_refuses_without_dropping_the_connection(self) -> None:
        self.assertSanitizedRefusal(None)

    def test_a_list_batch_refuses_without_dropping_the_connection(self) -> None:
        self.assertSanitizedRefusal([{"id": 1, "canary": self.CANARY}])

    def test_an_empty_list_batch_refuses_without_dropping_the_connection(self) -> None:
        self.assertSanitizedRefusal([])

    def test_a_string_batch_refuses_without_dropping_the_connection(self) -> None:
        self.assertSanitizedRefusal(self.CANARY)

    def test_a_bytes_batch_refuses_without_dropping_the_connection(self) -> None:
        self.assertSanitizedRefusal(self.CANARY.encode("utf-8"))

    def test_an_integer_batch_refuses_without_dropping_the_connection(self) -> None:
        self.assertSanitizedRefusal(7)

    def test_a_mapping_missing_every_field_refuses(self) -> None:
        """An ordinary mapping that simply lacks the fields, for contrast."""

        self.assertSanitizedRefusal({"delivery": "bounded-process-local"})

    def test_a_healthy_request_succeeds_before_and_after_a_malformed_one(self) -> None:
        """The sequence control: one bad batch must not poison the endpoint."""

        self.store.payload = snapshot_batch((1, 2, 3))
        status, _headers, body = self.request(cursor="0")
        self.assertEqual(status, 200)
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["1", "2", "3"])

        for malformed in (None, [], self.CANARY):
            self.store.payload = malformed
            status, _headers, _body = self.request(cursor="0")
            self.assertEqual(status, 500)

        self.store.payload = snapshot_batch((1, 2, 3))
        status, headers, body = self.request(cursor="0")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["1", "2", "3"])


class StoreShapeCompatibility(_SseCase):
    """v3-shaped and v4-shaped batches go through the same adapter."""

    def test_a_v3_shaped_batch_is_delivered(self) -> None:
        self.store.payload = snapshot_batch((1, 2), latest=2)
        status, _headers, body = self.request(cursor="0")
        self.assertEqual(status, 200)
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["1", "2"])

    def test_a_v4_shaped_batch_with_extra_record_fields_is_delivered(self) -> None:
        """No v4 constructor is used; only the batch shape differs."""

        payload = snapshot_batch((1, 2), latest=2)
        for event in payload["events"]:
            event.update({"schema_version": 4, "record_kind": "store.committed",
                          "manifest_digest": "sha256:" + "0" * 64})
        payload["schema_version"] = 4
        self.store.payload = payload

        status, _headers, body = self.request(cursor="0")

        self.assertEqual(status, 200)
        self.assertEqual([frame["id"] for frame in self.frames(body)], ["1", "2"])
        text = body.decode("utf-8")
        self.assertNotIn("manifest_digest", text)
        self.assertNotIn("record_kind", text)
        self.assertNotIn("schema_version", text)


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
