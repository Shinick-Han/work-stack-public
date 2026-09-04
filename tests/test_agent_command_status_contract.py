"""Independent conformance tests for the P0 agent status command handler."""

from __future__ import annotations

import contextlib
import io
import inspect
import json
import os
import pathlib
import socket
import sys
import unittest
import urllib.request
from unittest import mock

from workstack.agent_cli_contract import AgentBackend, AgentOutcome, StatusRequest, render_outcome
from workstack.agent_command_status import handle_status


EXPECTED_UID = "9f7c6d31-b53c-4ac7-8cc8-406c74f0de2e"
OTHER_UID = "d27c7d2b-2df1-49d4-a7a5-c22fd8a0bcd3"
STATUS_FIELDS = frozenset(
    {
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
)


def status_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "actual_workspace_uid": EXPECTED_UID,
        "capability_reason": None,
        "capability_supported": True,
        "contract": "workstack.cli.v1",
        "data_dir_available": True,
        "exclusive_local_available": False,
        "expected_workspace_uid": EXPECTED_UID,
        "ready": True,
        "running_server_available": True,
        "storage_format": "v3",
    }
    data.update(overrides)
    return data


class RecordingBackend:
    """Strict fake that exposes attempts to broaden the command seam."""

    def __init__(
        self,
        *,
        result: object | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.result = status_data() if result is None else result
        self.failure = failure
        self.status_requests: list[StatusRequest] = []
        self.context_calls = 0
        self.checkpoint_calls = 0

    def status(self, *, request: StatusRequest) -> dict[str, object]:
        self.status_requests.append(request)
        if len(self.status_requests) > 1:
            raise AssertionError("status called more than once")
        if self.failure is not None:
            raise self.failure
        return self.result  # type: ignore[return-value]

    def context(self, **_: object) -> dict[str, object]:
        self.context_calls += 1
        raise AssertionError("status handler called context")

    def checkpoint(self, **_: object) -> dict[str, object]:
        self.checkpoint_calls += 1
        raise AssertionError("status handler called checkpoint")


def request() -> StatusRequest:
    return StatusRequest(
        data_dir=pathlib.Path("C:/authority/never-disclose"),
        expected_workspace_uid=EXPECTED_UID,
    )


def invoke(
    *,
    backend: AgentBackend,
    status_request: StatusRequest | None = None,
) -> tuple[AgentOutcome, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        outcome = handle_status(
            request=request() if status_request is None else status_request,
            backend=backend,
        )
    return outcome, stdout.getvalue(), stderr.getvalue()


def assert_no_output(test: unittest.TestCase, stdout: str, stderr: str) -> None:
    test.assertEqual(stdout, "")
    test.assertEqual(stderr, "")


def assert_internal_error(test: unittest.TestCase, outcome: AgentOutcome) -> None:
    test.assertIsInstance(outcome, AgentOutcome)
    test.assertEqual(outcome.command, "agent.status")
    test.assertIsNone(outcome.data)
    test.assertEqual(outcome.error_code, "internal_error")
    test.assertEqual(outcome.error_details, {})
    test.assertIsInstance(outcome.error_message, str)
    test.assertIsNone(outcome.commit_state)
    test.assertIsNone(outcome.intent_id)
    test.assertIsNone(outcome.replayed)
    test.assertIsNone(outcome.task_id)
    test.assertIsNone(outcome.transport)
    test.assertIsNone(outcome.workspace_uid)
    test.assertIsNone(outcome.retryable)


class StatusHandlerContractTests(unittest.TestCase):
    def test_public_exports_are_exact(self) -> None:
        self.assertEqual(
            sys.modules[handle_status.__module__].__all__, ("handle_status",)
        )
        signature = inspect.signature(handle_status)
        self.assertEqual(
            tuple(
                (name, parameter.annotation)
                for name, parameter in signature.parameters.items()
            ),
            (("request", "StatusRequest"), ("backend", "AgentBackend")),
        )
        self.assertEqual(signature.return_annotation, "AgentOutcome")

    def test_calls_only_status_once_with_the_exact_request(self) -> None:
        status_request = request()
        backend = RecordingBackend()
        outcome, stdout, stderr = invoke(backend=backend, status_request=status_request)
        self.assertEqual(backend.status_requests, [status_request])
        self.assertIs(backend.status_requests[0], status_request)
        self.assertEqual(backend.context_calls, 0)
        self.assertEqual(backend.checkpoint_calls, 0)
        self.assertIsNotNone(outcome.data)
        assert_no_output(self, stdout, stderr)

    def test_request_uses_path_and_expected_workspace_uid_fields(self) -> None:
        status_request = request()
        self.assertIsInstance(status_request.data_dir, pathlib.Path)
        self.assertEqual(status_request.expected_workspace_uid, EXPECTED_UID)
        with self.assertRaises((AttributeError, TypeError)):
            status_request.expected_workspace_uid = OTHER_UID  # type: ignore[misc]

    def test_ready_v3_running_server_mapping_is_exact(self) -> None:
        raw = status_data()
        backend = RecordingBackend(result=raw)
        outcome, stdout, stderr = invoke(backend=backend)
        self.assertEqual(set(outcome.data or {}), STATUS_FIELDS)
        self.assertEqual(outcome.data, raw)
        self.assertEqual(outcome.command, "agent.status")
        self.assertEqual(outcome.transport, "running-server")
        self.assertEqual(outcome.workspace_uid, EXPECTED_UID)
        self.assertIsNone(outcome.commit_state)
        self.assertIsNone(outcome.intent_id)
        self.assertIsNone(outcome.replayed)
        self.assertIsNone(outcome.task_id)
        self.assertIsNone(outcome.error_code)
        self.assertEqual(outcome.error_details, {})
        self.assertIsNone(outcome.error_message)
        self.assertIsNone(outcome.retryable)
        assert_no_output(self, stdout, stderr)

    def test_ready_v3_exclusive_local_mapping(self) -> None:
        raw = status_data(
            running_server_available=False,
            exclusive_local_available=True,
        )
        outcome, stdout, stderr = invoke(backend=RecordingBackend(result=raw))
        self.assertEqual(outcome.data, raw)
        self.assertEqual(outcome.transport, "exclusive-local")
        self.assertEqual(outcome.workspace_uid, EXPECTED_UID)
        assert_no_output(self, stdout, stderr)

    def test_running_server_wins_when_both_transports_are_available(self) -> None:
        raw = status_data(exclusive_local_available=True)
        outcome, _, _ = invoke(backend=RecordingBackend(result=raw))
        self.assertEqual(outcome.transport, "running-server")

    def test_stable_refused_capability_mapping_is_not_reinterpreted(self) -> None:
        raw = status_data(
            capability_reason="capability_not_enabled",
            capability_supported=False,
            exclusive_local_available=True,
            ready=False,
            running_server_available=False,
            storage_format="v4",
        )
        outcome, stdout, stderr = invoke(backend=RecordingBackend(result=raw))
        self.assertEqual(outcome.data, raw)
        self.assertEqual(outcome.transport, "exclusive-local")
        self.assertFalse(outcome.data["capability_supported"])
        self.assertEqual(outcome.data["capability_reason"], "capability_not_enabled")
        assert_no_output(self, stdout, stderr)

    def test_renderer_emits_exact_canonical_status_envelope(self) -> None:
        raw = status_data(data_dir_available=False)
        outcome, _, _ = invoke(backend=RecordingBackend(result=raw))
        rendered = render_outcome(outcome=outcome)
        decoded = json.loads(rendered)
        self.assertTrue(rendered.endswith(b"\n"))
        self.assertEqual(set(decoded), {"contract", "data", "meta"})
        self.assertEqual(decoded["contract"], "workstack.cli.v1")
        self.assertEqual(decoded["data"], raw)
        self.assertIs(type(decoded["data"]["data_dir_available"]), bool)
        self.assertEqual(
            decoded["meta"],
            {
                "command": "agent.status",
                "transport": "running-server",
                "workspace_uid": EXPECTED_UID,
            },
        )
        self.assertNotIn(str(request().data_dir), rendered.decode("utf-8"))
        self.assertNotIn("path", decoded["data"])

    def test_result_mapping_is_not_silently_broadened_or_narrowed(self) -> None:
        for mutation in (
            lambda value: value.pop("ready"),
            lambda value: value.__setitem__("resolved_path", "C:/secret"),
        ):
            with self.subTest(mutation=mutation):
                raw = status_data()
                mutation(raw)
                backend = RecordingBackend(result=raw)
                outcome, stdout, stderr = invoke(backend=backend)
                assert_internal_error(self, outcome)
                self.assertEqual(len(backend.status_requests), 1)
                assert_no_output(self, stdout, stderr)

    def test_non_mapping_results_fail_closed(self) -> None:
        for raw in ([], (), "status", 7, False):
            with self.subTest(raw=raw):
                backend = RecordingBackend(result=raw)
                outcome, stdout, stderr = invoke(backend=backend)
                assert_internal_error(self, outcome)
                self.assertEqual(len(backend.status_requests), 1)
                assert_no_output(self, stdout, stderr)

    def test_transport_availability_requires_actual_booleans(self) -> None:
        for field in ("running_server_available", "exclusive_local_available"):
            for invalid in (0, 1, None, "false", [], {}):
                with self.subTest(field=field, invalid=invalid):
                    raw = status_data()
                    raw[field] = invalid
                    outcome, stdout, stderr = invoke(backend=RecordingBackend(result=raw))
                    assert_internal_error(self, outcome)
                    assert_no_output(self, stdout, stderr)

    def test_no_available_transport_fails_closed(self) -> None:
        raw = status_data(
            running_server_available=False,
            exclusive_local_available=False,
        )
        outcome, stdout, stderr = invoke(backend=RecordingBackend(result=raw))
        assert_internal_error(self, outcome)
        assert_no_output(self, stdout, stderr)

    def test_actual_workspace_uid_must_be_a_string(self) -> None:
        for invalid in (None, 7, b"uid", True):
            with self.subTest(invalid=invalid):
                raw = status_data(actual_workspace_uid=invalid)
                outcome, stdout, stderr = invoke(backend=RecordingBackend(result=raw))
                assert_internal_error(self, outcome)
                assert_no_output(self, stdout, stderr)

    def test_frozen_renderer_rejects_malformed_exact_mappings(self) -> None:
        mutations = {
            "bad expected uid": {"expected_workspace_uid": OTHER_UID},
            "bad actual uid": {"actual_workspace_uid": "not-a-uuid"},
            "bad contract": {"contract": "another.contract"},
            "bad storage": {"storage_format": "v5"},
            "bad reason": {"capability_reason": []},
            "bad data availability": {"data_dir_available": 1},
            "bad capability": {"capability_supported": "yes"},
            "bad ready": {"ready": 1},
        }
        for label, change in mutations.items():
            with self.subTest(label=label):
                outcome, stdout, stderr = invoke(
                    backend=RecordingBackend(result=status_data(**change))
                )
                with self.assertRaises(ValueError):
                    render_outcome(outcome=outcome)
                assert_no_output(self, stdout, stderr)

    def test_backend_exception_is_content_free_and_never_retried(self) -> None:
        canary = "TOKEN-77 C:/private/workspace value-from-backend"
        backend = RecordingBackend(failure=RuntimeError(canary))
        outcome, stdout, stderr = invoke(backend=backend)
        self.assertEqual(len(backend.status_requests), 1)
        assert_internal_error(self, outcome)
        self.assertNotIn(canary, outcome.error_message or "")
        self.assertNotIn("TOKEN-77", json.dumps(outcome.error_details))
        self.assertNotIn("private", json.dumps(outcome.error_details))
        rendered = render_outcome(outcome=outcome)
        self.assertNotIn(canary.encode(), rendered)
        self.assertNotIn(b"TOKEN-77", rendered)
        self.assertNotIn(b"private", rendered)
        self.assertNotIn(b"workspace", rendered)
        self.assertEqual(
            json.loads(rendered)["error"],
            {
                "code": "internal_error",
                "details": {},
                "message": "unexpected exception; envelope is content-free",
            },
        )
        assert_no_output(self, stdout, stderr)

    def test_handler_does_not_use_filesystem_network_or_environment(self) -> None:
        backend = RecordingBackend()
        poison = AssertionError("forbidden side effect")
        with (
            mock.patch("builtins.open", side_effect=poison),
            mock.patch.object(pathlib.Path, "exists", side_effect=poison),
            mock.patch.object(pathlib.Path, "read_text", side_effect=poison),
            mock.patch.object(os, "getenv", side_effect=poison),
            mock.patch.object(socket, "create_connection", side_effect=poison),
            mock.patch.object(urllib.request, "urlopen", side_effect=poison),
        ):
            outcome, stdout, stderr = invoke(backend=backend)
        self.assertIsNotNone(outcome.data)
        self.assertEqual(len(backend.status_requests), 1)
        assert_no_output(self, stdout, stderr)

    def test_backend_payload_is_not_mutated(self) -> None:
        raw = status_data()
        before = json.loads(json.dumps(raw))
        invoke(backend=RecordingBackend(result=raw))
        self.assertEqual(raw, before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
