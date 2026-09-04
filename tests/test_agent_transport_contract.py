from __future__ import annotations

import contextlib
import datetime
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

from workstack.agent_cli_contract import CheckpointRequest, ContextRequest, StatusRequest
from workstack.agent_transport import create_running_server_backend


WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
OTHER_UID = "22222222-2222-4222-8222-222222222222"
HOST = "127.0.0.1"
PORT = 8765
TODAY = datetime.date(2026, 9, 2)
CSRF = "csrf-canary-value"
SESSION = (200, {"data": {"csrf_token": CSRF}})
STORAGE = (
    200,
    {"data": {"store_schema_version": 3, "workspace_id": WORKSPACE_UID}},
)
SYNC = (200, {"data": {"state": "in-sync"}})

Response = tuple[int, dict[str, object]]
Step = Response | BaseException | Callable[[dict[str, object]], Response]


class RecordingRequester:
    """Strict JsonRequester fake: a wrong keyword ABI fails before it records."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
    ) -> Response:
        call: dict[str, object] = {
            "body": body,
            "headers": headers,
            "host": host,
            "method": method,
            "path": path,
            "port": port,
        }
        self.calls.append(call)
        if not self.steps:
            raise AssertionError("unexpected request: {!r}".format(call))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(call)
        return step


def _server_info(path: Path, value: object | None = None) -> None:
    if value is None:
        value = {"host": HOST, "port": PORT, "version": 1}
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _backend(info_path: Path, requester: RecordingRequester):
    return create_running_server_backend(
        server_info_path=info_path,
        expected_workspace_uid=WORKSPACE_UID,
        request_json=requester,
    )


def _status(backend, data_dir: Path) -> dict[str, object]:
    return backend.status(
        request=StatusRequest(
            data_dir=data_dir,
            expected_workspace_uid=WORKSPACE_UID,
        )
    )


def _checkpoint() -> CheckpointRequest:
    return CheckpointRequest(
        task_id="T-0001",
        date="2026-09-02",
        done=["done"],
        next=["next"],
        blockers=[],
        intent_id="intent.0001",
    )


def _entry(*, replayed: bool, status: int = 201) -> Response:
    return (
        status,
        {
            "data": {
                "blockers": [],
                "date": "2026-09-02",
                "done": ["done"],
                "next": ["next"],
                "task": "Task title",
                "task_id": "T-0001",
            },
            "meta": {"replayed": replayed},
        },
    )


def _task() -> dict[str, object]:
    return {
        "detail": "detail",
        "due": None,
        "id": "T-0001",
        "priority": "P2",
        "revision": 3,
        "status": "open",
        "title": "Task title",
        "uid": "33333333-3333-4333-8333-333333333333",
    }


class RunningServerMetadataContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.info = self.root / "server-info.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_bounded_loopback_metadata_and_status_wire_contract(self) -> None:
        expected_keys = {
            "actual_workspace_uid",
            "capability_reason",
            "capability_supported",
            "contract",
            "data_dir_available",
            "exclusive_local_available",
            "expected_workspace_uid",
            "ready",
            "running_server_available",
            "storage_format",
        }
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                _server_info(self.info, {"host": host, "port": PORT, "version": 1})
                requester = RecordingRequester(SESSION, STORAGE, SYNC)
                result = _status(_backend(self.info, requester), self.root)
                self.assertEqual(set(result), expected_keys)
                self.assertEqual(result["actual_workspace_uid"], WORKSPACE_UID)
                self.assertEqual(result["expected_workspace_uid"], WORKSPACE_UID)
                self.assertEqual(result["contract"], "workstack.cli.v1")
                self.assertEqual(result["storage_format"], "v3")
                self.assertTrue(result["ready"])
                self.assertTrue(result["running_server_available"])
                self.assertEqual(
                    requester.calls,
                    [
                        {
                            "body": None,
                            "headers": None,
                            "host": host,
                            "method": "GET",
                            "path": "/api/v1/session",
                            "port": PORT,
                        },
                        {
                            "body": None,
                            "headers": None,
                            "host": host,
                            "method": "GET",
                            "path": "/api/v1/storage",
                            "port": PORT,
                        },
                        {
                            "body": None,
                            "headers": None,
                            "host": host,
                            "method": "GET",
                            "path": "/api/v1/sync/status",
                            "port": PORT,
                        },
                    ],
                )

    def test_non_in_sync_status_is_not_ready_and_redacts_sync_details(self) -> None:
        _server_info(self.info)
        requester = RecordingRequester(
            SESSION,
            STORAGE,
            (
                200,
                {
                    "data": {
                        "state": "invalid",
                        "reason": "absolute-path-and-validation-canary",
                        "changed_files": ["workspace.json"],
                    }
                },
            ),
        )

        result = _status(_backend(self.info, requester), self.root)

        self.assertFalse(result["ready"])
        self.assertTrue(result["capability_supported"])
        self.assertEqual(result["capability_reason"], "store_sync_required")
        self.assertNotIn("absolute-path-and-validation-canary", repr(result))
        self.assertNotIn("workspace.json", repr(result))

    def test_malformed_sync_status_fails_closed_without_raw_response(self) -> None:
        _server_info(self.info)
        for malformed in (
            (200, {"data": {}}),
            (200, {"data": {"state": "future-state", "raw": "raw-canary"}}),
            (200, {"data": {"state": ["in-sync"], "raw": "raw-canary"}}),
        ):
            with self.subTest(malformed=malformed):
                requester = RecordingRequester(SESSION, STORAGE, malformed)
                with self.assertRaises(OSError) as raised:
                    _status(_backend(self.info, requester), self.root)
                self.assertEqual(str(raised.exception), "sync status response is invalid")
                self.assertNotIn("raw-canary", str(raised.exception))

    def test_rejects_every_invalid_metadata_before_http(self) -> None:
        invalid_values: list[object] = [
            {"host": "0.0.0.0", "port": PORT, "version": 1},
            {"host": "example.com", "port": PORT, "version": 1},
            {"host": HOST, "port": 0, "version": 1},
            {"host": HOST, "port": 65536, "version": 1},
            {"host": HOST, "port": True, "version": 1},
            {"host": HOST, "port": "8765", "version": 1},
            {"host": HOST, "port": PORT, "version": 2},
            {"host": HOST, "port": PORT},
            {"host": HOST, "port": PORT, "version": 1, "extra": True},
            [HOST, PORT, 1],
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                _server_info(self.info, value)
                requester = RecordingRequester()
                with self.assertRaises(OSError):
                    _status(_backend(self.info, requester), self.root)
                self.assertEqual(requester.calls, [])

        self.info.write_bytes(b"{malformed")
        with self.assertRaises(OSError):
            _status(_backend(self.info, RecordingRequester()), self.root)
        self.info.write_bytes(b"{" + b"x" * 4096 + b"}")
        with self.assertRaises(OSError):
            _status(_backend(self.info, RecordingRequester()), self.root)

    def test_missing_or_dead_owner_never_constructs_or_falls_back_to_store(self) -> None:
        import workstack.store

        absent = self.root / "absent-server-info.json"
        with mock.patch.object(
            workstack.store,
            "Store",
            side_effect=AssertionError("local fallback forbidden"),
        ) as store:
            with self.assertRaises(OSError):
                _status(_backend(absent, RecordingRequester()), self.root)
            store.assert_not_called()

        _server_info(self.info)
        requester = RecordingRequester(OSError("connection refused"))
        with mock.patch.object(
            workstack.store,
            "Store",
            side_effect=AssertionError("local fallback forbidden"),
        ) as store:
            with self.assertRaises(OSError):
                _status(_backend(self.info, requester), self.root)
            store.assert_not_called()
        self.assertEqual(len(requester.calls), 1)

    def test_workspace_mismatch_stops_before_any_task_content_read(self) -> None:
        _server_info(self.info)
        requester = RecordingRequester(
            SESSION,
            (200, {"data": {"store_schema_version": 3, "workspace_id": OTHER_UID}}),
        )
        backend = _backend(self.info, requester)
        with self.assertRaises(ValueError):
            backend.context(request=ContextRequest(task_id="T-0001"), today=TODAY)
        self.assertEqual(
            [call["path"] for call in requester.calls],
            ["/api/v1/session", "/api/v1/storage"],
        )

    def test_noncanonical_expected_or_request_uid_is_rejected_before_http(self) -> None:
        _server_info(self.info)
        requester = RecordingRequester()
        with self.assertRaises(OSError):
            create_running_server_backend(
                server_info_path=self.info,
                expected_workspace_uid="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                request_json=requester,
            )
        backend = _backend(self.info, requester)
        with self.assertRaises(ValueError):
            backend.status(
                request=StatusRequest(
                    data_dir=self.root,
                    expected_workspace_uid=OTHER_UID,
                )
            )
        self.assertEqual(requester.calls, [])


class RunningServerContextContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.info = self.root / "server-info.json"
        _server_info(self.info)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_then_exactly_31_daily_gets_newest_first_and_never_weekly(self) -> None:
        dates = [(TODAY - datetime.timedelta(days=index)).isoformat() for index in range(31)]
        steps: list[Step] = [SESSION, STORAGE, (200, {"data": {"task": _task()}})]
        for index, date in enumerate(dates):
            entries: list[dict[str, object]] = []
            if index in (0, 30):
                entries.append(
                    {
                        "blockers": [],
                        "done": ["day {}".format(index)],
                        "next": [],
                        "task_id": "T-0001",
                    }
                )
            steps.append((200, {"data": {"day": {"entries": entries}}}))
        requester = RecordingRequester(*steps)
        result = _backend(self.info, requester).context(
            request=ContextRequest(task_id="T-0001"), today=TODAY
        )

        self.assertEqual(set(result), {"entries", "task", "transport", "workspace_uid"})
        self.assertEqual(result["task"], _task())
        self.assertEqual(result["workspace_uid"], WORKSPACE_UID)
        self.assertEqual(result["transport"], "running-server")
        self.assertEqual(
            [entry["date"] for entry in result["entries"]],
            [dates[0], dates[30]],
        )
        paths = [call["path"] for call in requester.calls]
        self.assertEqual(
            paths[:3],
            ["/api/v1/session", "/api/v1/storage", "/api/v1/tasks/T-0001"],
        )
        self.assertEqual(
            paths[3:],
            ["/api/v1/review?date={}&days=1".format(date) for date in dates],
        )
        self.assertTrue(all("weekly" not in path for path in paths))
        self.assertTrue(all("days=31" not in path for path in paths))
        self.assertEqual(requester.steps, [])

    def test_session_storage_task_and_daily_failures_are_not_retried(self) -> None:
        cases: list[list[Step]] = [
            [OSError("session unavailable")],
            [SESSION, OSError("storage unavailable")],
            [SESSION, STORAGE, OSError("task unavailable")],
            [
                SESSION,
                STORAGE,
                (200, {"data": {"task": _task()}}),
                OSError("daily unavailable"),
            ],
        ]
        for steps in cases:
            with self.subTest(step_count=len(steps)):
                requester = RecordingRequester(*steps)
                with self.assertRaises(OSError):
                    _backend(self.info, requester).context(
                        request=ContextRequest(task_id="T-0001"), today=TODAY
                    )
                self.assertEqual(len(requester.calls), len(steps))


class RunningServerCheckpointContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.info = self.root / "server-info.json"
        _server_info(self.info)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_body_headers_and_semantic_success(self) -> None:
        requester = RecordingRequester(SESSION, STORAGE, _entry(replayed=False))
        result = _backend(self.info, requester).checkpoint(request=_checkpoint())
        expected_body = (
            b'{"blockers":[],"date":"2026-09-02","done":["done"],'
            b'"next":["next"],"task_id":"T-0001"}'
        )
        self.assertEqual(
            requester.calls[2],
            {
                "body": expected_body,
                "headers": {
                    "Content-Type": "application/json",
                    "Idempotency-Key": "intent.0001",
                    "Origin": "http://127.0.0.1:8765",
                    # Sender provenance hint. Added without relaxing anything:
                    # this dict is still compared whole, so a missing, extra or
                    # altered header still fails the assertion.
                    "X-WorkStack-Client": "agent-cli-v1",
                    "X-WorkStack-CSRF": CSRF,
                },
                "host": HOST,
                "method": "POST",
                "path": "/api/v1/review/entries",
                "port": PORT,
            },
        )
        self.assertNotIn(b"workspace_uid", expected_body)
        self.assertEqual(
            result,
            {
                "commit_state": "committed",
                "entry": _entry(replayed=False)[1]["data"],
                "replayed": False,
                "transport": "running-server",
                "workspace_uid": WORKSPACE_UID,
            },
        )

    def test_ipv6_origin_is_bracketed_exactly(self) -> None:
        _server_info(self.info, {"host": "::1", "port": PORT, "version": 1})
        requester = RecordingRequester(SESSION, STORAGE, _entry(replayed=False))
        _backend(self.info, requester).checkpoint(request=_checkpoint())
        self.assertEqual(
            requester.calls[2]["headers"]["Origin"],
            "http://[::1]:8765",
        )

    def test_lost_after_commit_replays_identical_bytes_and_key_once(self) -> None:
        requester = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("response lost after commit"),
            _entry(replayed=True, status=200),
        )
        result = _backend(self.info, requester).checkpoint(request=_checkpoint())
        posts = [call for call in requester.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertEqual(posts[0]["headers"], posts[1]["headers"])
        # The provenance hint must survive the replay, not only the first send:
        # a checkpoint that lost its response is still agent-written.
        for post in posts:
            self.assertEqual(post["headers"]["X-WorkStack-Client"], "agent-cli-v1")
            self.assertEqual(post["headers"]["Idempotency-Key"], "intent.0001")
        self.assertEqual(result["commit_state"], "committed")
        self.assertIs(result["replayed"], True)

    def test_provenance_header_is_absent_from_preflight_reads(self) -> None:
        """Attribution belongs on the write, not on the reads that precede it."""

        requester = RecordingRequester(SESSION, STORAGE, _entry(replayed=False))
        _backend(self.info, requester).checkpoint(request=_checkpoint())

        gets = [call for call in requester.calls if call["method"] == "GET"]
        self.assertEqual(
            [call["path"] for call in gets], ["/api/v1/session", "/api/v1/storage"]
        )
        for call in gets:
            self.assertIsNone(call["headers"])
            self.assertIsNone(call["body"])

    def test_provenance_header_carries_no_authority(self) -> None:
        """It is a hint: the CSRF token and Origin still do the actual work."""

        requester = RecordingRequester(SESSION, STORAGE, _entry(replayed=False))
        _backend(self.info, requester).checkpoint(request=_checkpoint())

        headers = requester.calls[2]["headers"]
        self.assertEqual(headers["X-WorkStack-Client"], "agent-cli-v1")
        # Removing the hint must not be what makes the request valid, so the
        # credential-bearing headers are asserted to still be present and
        # unchanged alongside it.
        self.assertEqual(headers["X-WorkStack-CSRF"], CSRF)
        self.assertEqual(headers["Origin"], "http://127.0.0.1:8765")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("X-WorkStack-Client-Signature", headers)

    def test_lost_before_commit_second_attempt_commits_once(self) -> None:
        requester = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("request lost before server"),
            _entry(replayed=False),
        )
        result = _backend(self.info, requester).checkpoint(request=_checkpoint())
        posts = [call for call in requester.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertEqual(result["commit_state"], "committed")
        self.assertIs(result["replayed"], False)

    def test_both_post_attempts_unverifiable_yield_commit_unknown(self) -> None:
        requester = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("first response lost"),
            TimeoutError("replay response lost"),
        )
        result = _backend(self.info, requester).checkpoint(request=_checkpoint())
        self.assertEqual(
            result,
            {
                "commit_state": "unknown",
                "entry": None,
                "replayed": False,
                "transport": "running-server",
                "workspace_uid": WORKSPACE_UID,
            },
        )
        posts = [call for call in requester.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertEqual(
            posts[0]["headers"]["Idempotency-Key"],
            posts[1]["headers"]["Idempotency-Key"],
        )

    def test_pre_post_failures_never_retry_or_manufacture_commit_unknown(self) -> None:
        for steps in (
            [OSError("session failure")],
            [SESSION, OSError("storage failure")],
        ):
            with self.subTest(step_count=len(steps)):
                requester = RecordingRequester(*steps)
                with self.assertRaises(OSError):
                    _backend(self.info, requester).checkpoint(request=_checkpoint())
                self.assertEqual(len(requester.calls), len(steps))
                self.assertFalse(
                    any(call["method"] == "POST" for call in requester.calls)
                )

    def test_http_error_and_same_key_different_body_conflict_are_not_retried(self) -> None:
        for status, code in ((503, "unavailable"), (409, "idempotency_conflict")):
            with self.subTest(status=status):
                requester = RecordingRequester(
                    SESSION,
                    STORAGE,
                    (status, {"error": {"code": code, "raw": "raw-body-canary"}}),
                )
                with self.assertRaises(OSError) as raised:
                    _backend(self.info, requester).checkpoint(request=_checkpoint())
                self.assertNotIn("raw-body-canary", str(raised.exception))
                posts = [call for call in requester.calls if call["method"] == "POST"]
                self.assertEqual(len(posts), 1)


class RunningServerRedactionContractTest(unittest.TestCase):
    def test_failure_surfaces_never_expose_path_csrf_token_cookie_or_raw_body(self) -> None:
        with tempfile.TemporaryDirectory(prefix="absolute-path-canary-") as directory:
            root = Path(directory)
            info = root / "secret-server-info.json"
            _server_info(info)
            requester = RecordingRequester(
                (
                    200,
                    {
                        "data": {
                            "csrf_token": CSRF,
                            "token": "token-canary-value",
                            "cookie": "cookie-canary-value",
                        }
                    },
                ),
                (
                    200,
                    {
                        "data": {
                            "workspace_id": OTHER_UID,
                            "store_schema_version": 3,
                            "raw": "raw-body-canary",
                        }
                    },
                ),
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(ValueError) as raised:
                    _backend(info, requester).context(
                        request=ContextRequest(task_id="T-0001"), today=TODAY
                    )
            rendered = "\n".join(
                (str(raised.exception), stdout.getvalue(), stderr.getvalue())
            )
            for canary in (
                str(root),
                CSRF,
                "token-canary-value",
                "cookie-canary-value",
                "raw-body-canary",
            ):
                self.assertNotIn(canary, rendered)


if __name__ == "__main__":
    unittest.main()
