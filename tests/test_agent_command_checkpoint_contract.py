from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import io
import json
import sys
import unittest
from typing import Any

from workstack.agent_cli_contract import (
    CHECKPOINT_COMMAND,
    AgentOutcome,
    CheckpointRequest,
    parse_checkpoint_packet,
    render_outcome,
)
from workstack.agent_command_checkpoint import handle_checkpoint


COMMAND = "agent.{}".format(CHECKPOINT_COMMAND)
INTENT_ID = "agent:test:checkpoint-001"
WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
PACKET = {
    "blockers": [],
    "date": "2026-09-02",
    "done": ["Implemented preflight"],
    "next": ["Add coverage"],
    "task_id": "T-0001",
}


def _packet_bytes(packet: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        PACKET if packet is None else packet,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _request(raw: bytes | None = None, *, intent_id: str = INTENT_ID) -> CheckpointRequest:
    return parse_checkpoint_packet(
        raw=_packet_bytes() if raw is None else raw,
        intent_id=intent_id,
    )


def _entry(*, task: str = "Checkpoint task") -> dict[str, object]:
    return {
        "blockers": [],
        "date": PACKET["date"],
        "done": list(PACKET["done"]),
        "next": list(PACKET["next"]),
        "task": task,
        "task_id": PACKET["task_id"],
    }


def _committed_result(*, replayed: bool = False, task: str = "Checkpoint task") -> dict[str, object]:
    return {
        "commit_state": "committed",
        "entry": _entry(task=task),
        "replayed": replayed,
        "transport": "running-server",
        "workspace_uid": WORKSPACE_UID,
    }


_DEFAULT_RESULT = object()


class _RecordingBackend:
    def __init__(
        self,
        result: object = _DEFAULT_RESULT,
        *,
        exception: Exception | None = None,
    ):
        self.result = _committed_result() if result is _DEFAULT_RESULT else result
        self.exception = exception
        self.checkpoint_calls: list[CheckpointRequest] = []
        self.other_calls: list[str] = []

    def checkpoint(self, *, request: CheckpointRequest) -> dict[str, object]:
        self.checkpoint_calls.append(request)
        if len(self.checkpoint_calls) != 1:
            raise AssertionError("checkpoint called more than once")
        if self.exception is not None:
            raise self.exception
        return self.result  # type: ignore[return-value]

    def status(self, **_: Any) -> dict[str, object]:
        self.other_calls.append("status")
        raise AssertionError("status must not be called")

    def context(self, **_: Any) -> dict[str, object]:
        self.other_calls.append("context")
        raise AssertionError("context must not be called")


class CheckpointHandlerContractTest(unittest.TestCase):
    def test_public_exports_are_exact(self) -> None:
        self.assertEqual(
            sys.modules[handle_checkpoint.__module__].__all__,
            ("handle_checkpoint",),
        )
        signature = inspect.signature(handle_checkpoint)
        self.assertEqual(
            tuple(
                (name, parameter.annotation)
                for name, parameter in signature.parameters.items()
            ),
            (("request", "CheckpointRequest"), ("backend", "AgentBackend")),
        )
        self.assertEqual(signature.return_annotation, "AgentOutcome")

    def _call(
        self,
        backend: _RecordingBackend,
        request: CheckpointRequest | None = None,
    ) -> tuple[CheckpointRequest, AgentOutcome]:
        actual_request = _request() if request is None else request
        before = dataclasses.asdict(actual_request)
        outcome = handle_checkpoint(request=actual_request, backend=backend)
        self.assertEqual(len(backend.checkpoint_calls), 1)
        self.assertIs(backend.checkpoint_calls[0], actual_request)
        self.assertEqual(dataclasses.asdict(actual_request), before)
        self.assertEqual(backend.other_calls, [])
        return actual_request, outcome

    def assert_internal_error(self, outcome: AgentOutcome) -> bytes:
        self.assertEqual(outcome.command, COMMAND)
        self.assertEqual(outcome.error_code, "internal_error")
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.error_details, {})
        for field in (
            "commit_state",
            "intent_id",
            "replayed",
            "retryable",
            "task_id",
            "transport",
            "workspace_uid",
        ):
            self.assertIsNone(getattr(outcome, field))
        return render_outcome(outcome=outcome)

    def test_first_commit_preserves_request_and_exact_metadata(self) -> None:
        request, outcome = self._call(_RecordingBackend())
        self.assertEqual(
            set(dataclasses.asdict(request)),
            {"task_id", "date", "done", "next", "blockers", "intent_id"},
        )
        effective_body = dataclasses.asdict(request)
        effective_body.pop("intent_id")
        self.assertEqual(
            set(effective_body), {"task_id", "date", "done", "next", "blockers"}
        )
        self.assertNotIn("workspace_uid", effective_body)
        self.assertEqual(outcome.command, COMMAND)
        self.assertEqual(outcome.commit_state, "committed")
        self.assertEqual(outcome.intent_id, INTENT_ID)
        self.assertEqual(outcome.task_id, PACKET["task_id"])
        self.assertIs(outcome.replayed, False)
        self.assertEqual(outcome.transport, "running-server")
        self.assertEqual(outcome.workspace_uid, WORKSPACE_UID)
        self.assertEqual(outcome.data, _entry())
        self.assertIsNone(outcome.error_code)
        self.assertIsNone(outcome.error_message)
        self.assertIsNone(outcome.retryable)

    def test_same_key_replay_is_one_handler_call_and_preserves_identity(self) -> None:
        request = _request(intent_id="agent:test:same-key-replay")
        backend = _RecordingBackend(_committed_result(replayed=True))
        _, outcome = self._call(backend, request)
        self.assertEqual(len(backend.checkpoint_calls), 1)
        self.assertIs(backend.checkpoint_calls[0], request)
        self.assertEqual(outcome.intent_id, "agent:test:same-key-replay")
        self.assertEqual(outcome.task_id, "T-0001")
        self.assertIs(outcome.replayed, True)
        self.assertEqual(outcome.commit_state, "committed")

    def test_commit_unknown_is_failure_with_required_identity_only(self) -> None:
        backend = _RecordingBackend(
            {
                "commit_state": "unknown",
                "entry": None,
                "transport": "running-server",
                "workspace_uid": WORKSPACE_UID,
            }
        )
        _, outcome = self._call(backend)
        self.assertEqual(outcome.command, COMMAND)
        self.assertEqual(outcome.error_code, "commit_unknown")
        self.assertEqual(outcome.commit_state, "unknown")
        self.assertEqual(outcome.intent_id, INTENT_ID)
        self.assertEqual(outcome.task_id, "T-0001")
        self.assertEqual(outcome.transport, "running-server")
        self.assertEqual(outcome.workspace_uid, WORKSPACE_UID)
        self.assertIsNone(outcome.data)
        self.assertIsNone(outcome.replayed)
        envelope = json.loads(render_outcome(outcome=outcome))
        self.assertNotIn("data", envelope)
        self.assertEqual(
            set(envelope["meta"]),
            {
                "command",
                "commit_state",
                "intent_id",
                "task_id",
                "transport",
                "workspace_uid",
            },
        )

    def test_commit_unknown_rejects_success_data_or_local_transport(self) -> None:
        mutations = [
            {"entry": _entry()},
            {"transport": "exclusive-local"},
        ]
        for mutation in mutations:
            result: dict[str, object] = {
                "commit_state": "unknown",
                "entry": None,
                "transport": "running-server",
                "workspace_uid": WORKSPACE_UID,
            }
            result.update(mutation)
            with self.subTest(mutation=mutation):
                _, outcome = self._call(_RecordingBackend(result))
                self.assert_internal_error(outcome)

    def test_ordinary_failure_omits_command_inapplicable_metadata(self) -> None:
        backend = _RecordingBackend(
            {"error_code": "owner_unavailable", "retryable": True}
        )
        _, outcome = self._call(backend)
        self.assertEqual(outcome.error_code, "owner_unavailable")
        self.assertIs(outcome.retryable, True)
        self.assertIsNone(outcome.data)
        for field in (
            "commit_state",
            "intent_id",
            "replayed",
            "task_id",
            "transport",
            "workspace_uid",
        ):
            self.assertIsNone(getattr(outcome, field))
        envelope = json.loads(render_outcome(outcome=outcome))
        self.assertEqual(envelope["meta"], {"command": COMMAND})

    def test_malformed_backend_results_are_content_free_internal_errors(self) -> None:
        malformed: list[object] = [
            None,
            [],
            "committed",
            {},
            {"commit_state": "committed"},
            {**_committed_result(), "replayed": "yes"},
            {**_committed_result(), "transport": "remote-filesystem"},
            {**_committed_result(), "workspace_uid": "not-a-uuid"},
            {"error_code": "invented_error", "retryable": True},
        ]
        for result in malformed:
            with self.subTest(result=result):
                _, outcome = self._call(_RecordingBackend(result))
                rendered = self.assert_internal_error(outcome)
                self.assertNotIn(b"invented_error", rendered)

    def test_exception_path_token_and_server_body_never_leak(self) -> None:
        secrets = (
            r"X:\fixture-home\.ssh\id_ed25519",
            "csrf=SUPER-SECRET-TOKEN",
            '{"raw_server_body":"private message"}',
        )
        for secret in secrets:
            with self.subTest(secret=secret):
                backend = _RecordingBackend(exception=RuntimeError(secret))
                _, outcome = self._call(backend)
                rendered = self.assert_internal_error(outcome)
                self.assertNotIn(secret, repr(dataclasses.asdict(outcome)))
                self.assertNotIn(secret.encode("utf-8"), rendered)

    def test_handler_is_silent(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        backend = _RecordingBackend()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self._call(backend)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_handler_module_has_no_forbidden_behavior_imports(self) -> None:
        module = sys.modules[handle_checkpoint.__module__]
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
        self.assertEqual(imported, {"__future__", "workstack.agent_cli_contract"})
        for forbidden in ("Store", "WorkStack", "pathlib", "requests", "urllib"):
            self.assertNotIn(forbidden, vars(module))


class CheckpointParserAndRendererContractTest(unittest.TestCase):
    def test_parser_requires_exact_fields_valid_items_and_nonempty_journal(self) -> None:
        invalid_packets: list[bytes] = [
            b"",
            b"   \t\r\n",
            b"null",
            b"[]",
            b'{"task_id":"T-0001"}',
            _packet_bytes({**PACKET, "workspace_uid": WORKSPACE_UID}),
            _packet_bytes({**PACKET, "date": "2026-02-30"}),
            _packet_bytes({**PACKET, "task_id": "task-1"}),
            _packet_bytes({**PACKET, "done": ["x"] * 21}),
            _packet_bytes({**PACKET, "done": ["x" * 1001]}),
            _packet_bytes({**PACKET, "done": [], "next": [], "blockers": []}),
            _packet_bytes(
                {**PACKET, "done": ["  "], "next": ["\t"], "blockers": ["\n"]}
            ),
            b'{"task_id":"T-0001","task_id":"T-0002","date":"2026-09-02","done":["x"],"next":[],"blockers":[]}',
            b"\xff",
        ]
        for raw in invalid_packets:
            with self.subTest(raw=raw[:100]):
                with self.assertRaises(ValueError):
                    parse_checkpoint_packet(raw=raw, intent_id=INTENT_ID)

    def test_parser_invalid_body_never_reaches_backend(self) -> None:
        backend = _RecordingBackend()
        with self.assertRaises(ValueError):
            parse_checkpoint_packet(raw=b"   ", intent_id=INTENT_ID)
        self.assertEqual(backend.checkpoint_calls, [])
        self.assertEqual(backend.other_calls, [])

    def test_parser_uses_utf8_input_bytes_at_32768_boundary(self) -> None:
        multibyte = _packet_bytes({**PACKET, "done": ["界"]})
        at_limit = multibyte + (b" " * (32768 - len(multibyte)))
        self.assertEqual(len(at_limit), 32768)
        self.assertLess(len(at_limit.decode("utf-8")), len(at_limit))
        parsed = parse_checkpoint_packet(raw=at_limit, intent_id=INTENT_ID)
        self.assertEqual(parsed.done, ["界"])
        with self.assertRaises(ValueError):
            parse_checkpoint_packet(raw=at_limit + b" ", intent_id=INTENT_ID)

    def test_invalid_body_failure_is_safe_and_has_ordinary_metadata(self) -> None:
        outcome = AgentOutcome(
            command=COMMAND,
            commit_state=None,
            data=None,
            error_code="invalid_body",
            error_details={},
            error_message="ignored raw C:\\secret token=abc",
            intent_id=None,
            replayed=None,
            retryable=None,
            task_id=None,
            transport=None,
            workspace_uid=None,
        )
        envelope = json.loads(render_outcome(outcome=outcome))
        self.assertEqual(envelope["meta"], {"command": COMMAND})
        self.assertEqual(envelope["error"]["details"], {})
        rendered = json.dumps(envelope)
        self.assertNotIn("C:\\secret", rendered)
        self.assertNotIn("token=abc", rendered)

    def test_renderer_has_exact_checkpoint_data_keys_and_frozen_shape(self) -> None:
        _, outcome = CheckpointHandlerContractTest()._call(_RecordingBackend())
        rendered = render_outcome(outcome=outcome)
        self.assertTrue(rendered.endswith(b"\n"))
        self.assertEqual(rendered.count(b"\n"), 1)
        envelope = json.loads(rendered)
        self.assertEqual(set(envelope), {"contract", "data", "meta"})
        self.assertEqual(envelope["contract"], "workstack.cli.v1")
        self.assertEqual(
            set(envelope["data"]),
            {"blockers", "date", "done", "next", "task", "task_id"},
        )
        self.assertEqual(
            set(envelope["meta"]),
            {
                "command",
                "commit_state",
                "intent_id",
                "replayed",
                "task_id",
                "transport",
                "workspace_uid",
            },
        )

    def test_renderer_bounds_the_full_utf8_envelope_not_data_or_characters(self) -> None:
        template = handle_checkpoint(request=_request(), backend=_RecordingBackend())

        def outcome_for(task: str) -> AgentOutcome:
            return dataclasses.replace(template, data=_entry(task=task))

        one = render_outcome(outcome=outcome_for("x"))
        exact_task_length = 1 + (32768 - len(one))
        exact = render_outcome(outcome=outcome_for("x" * exact_task_length))
        self.assertEqual(len(exact), 32768)
        with self.assertRaises(ValueError):
            render_outcome(outcome=outcome_for("x" * (exact_task_length + 1)))

        base = len(render_outcome(outcome=outcome_for("x"))) - 1
        multibyte_count = ((32768 - base) // len("界".encode("utf-8"))) + 1
        multibyte_task = "界" * multibyte_count
        self.assertLess(len(multibyte_task), 32768)
        with self.assertRaises(ValueError):
            render_outcome(outcome=outcome_for(multibyte_task))


if __name__ == "__main__":
    unittest.main()
