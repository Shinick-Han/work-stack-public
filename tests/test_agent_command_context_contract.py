from __future__ import annotations

import contextlib
import copy
import datetime
import io
import inspect
import json
import sys
import unittest
from typing import Any

from workstack.agent_cli_contract import (
    AgentOutcome,
    ContextRequest,
    render_outcome,
)
from workstack.agent_command_context import handle_context


TODAY = datetime.date(2026, 9, 2)
TASK_ID = "T-0042"
WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
TASK_UID = "22222222-2222-4222-8222-222222222222"
TRANSPORT = "exclusive-local"
COMMAND = "agent.context"
MAX_ENVELOPE_BYTES = 32768
CORE_FIELDS = {
    "detail",
    "due",
    "id",
    "priority",
    "revision",
    "status",
    "title",
    "uid",
}
OMITTED = [
    "attachments",
    "captures",
    "objectives",
    "relationships",
    "work_sessions",
]


def _task(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": TASK_ID,
        "uid": TASK_UID,
        "revision": 7,
        "title": "Ship the agent interface",
        "detail": "Keep the context bounded and deterministic.",
        "status": "in_progress",
        "priority": "P1",
        "due": "2026-09-30",
        # Every canary below is forbidden from the context projection.
        "attachments": [{"forbidden_field": "attachment-canary"}],
        "captures": [{"forbidden_field": "capture-canary"}],
        "objectives": [{"forbidden_field": "objective-canary"}],
        "relationships": [{"forbidden_field": "relationship-canary"}],
        "work_sessions": [{"forbidden_field": "session-canary"}],
        "backend_only": "backend-only-canary",
    }
    value.update(changes)
    return value


def _entry(
    day: datetime.date,
    *,
    task_id: str = TASK_ID,
    marker: str = "done",
    done: list[str] | None = None,
    next_items: list[str] | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "date": day.isoformat(),
        "done": [marker] if done is None else done,
        "next": [] if next_items is None else next_items,
        "blockers": [] if blockers is None else blockers,
        "backend_only": "worklog-canary",
    }


def _result(
    *,
    task: dict[str, Any] | None = None,
    entries: list[dict[str, Any]] | None = None,
    workspace_uid: str = WORKSPACE_UID,
    transport: str = TRANSPORT,
) -> dict[str, Any]:
    return {
        "workspace_uid": workspace_uid,
        "transport": transport,
        "task": _task() if task is None else task,
        "entries": [] if entries is None else entries,
    }


class RecordingBackend:
    def __init__(
        self,
        result: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = _result() if result is None else result
        self.error = error
        self.calls: list[tuple[ContextRequest, datetime.date]] = []

    def context(
        self, *, request: ContextRequest, today: datetime.date
    ) -> dict[str, Any]:
        self.calls.append((request, today))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.result)

    def status(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("agent.context must not call AgentBackend.status")

    def checkpoint(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("agent.context must not call AgentBackend.checkpoint")


def _invoke(
    backend: RecordingBackend,
    *,
    request: ContextRequest | None = None,
    today: datetime.date = TODAY,
) -> AgentOutcome:
    return handle_context(
        request=ContextRequest(task_id=TASK_ID) if request is None else request,
        backend=backend,
        today=today,
    )


def _success_data(outcome: AgentOutcome) -> dict[str, Any]:
    if outcome.error_code is not None or outcome.data is None:
        raise AssertionError("expected context success, got {!r}".format(outcome))
    return outcome.data


def _assert_content_free_internal(
    case: unittest.TestCase, outcome: AgentOutcome
) -> None:
    case.assertEqual(outcome.command, COMMAND)
    case.assertEqual(outcome.error_code, "internal_error")
    case.assertEqual(
        outcome.error_message, "unexpected exception; envelope is content-free"
    )
    case.assertEqual(outcome.error_details, {})
    case.assertIsNone(outcome.data)
    case.assertIsNone(outcome.task_id)
    case.assertIsNone(outcome.transport)
    case.assertIsNone(outcome.workspace_uid)
    case.assertIsNone(outcome.commit_state)
    case.assertIsNone(outcome.intent_id)
    case.assertIsNone(outcome.replayed)
    case.assertIsNone(outcome.retryable)


class ContextBackendBoundaryTests(unittest.TestCase):
    def test_public_exports_are_exact(self) -> None:
        self.assertEqual(
            sys.modules[handle_context.__module__].__all__, ("handle_context",)
        )
        signature = inspect.signature(handle_context)
        self.assertEqual(
            tuple(
                (name, parameter.annotation)
                for name, parameter in signature.parameters.items()
            ),
            (
                ("request", "ContextRequest"),
                ("backend", "AgentBackend"),
                ("today", "datetime.date"),
            ),
        )
        self.assertEqual(signature.return_annotation, "AgentOutcome")

    def test_calls_exact_backend_method_once_with_exact_keyword_values(self) -> None:
        request = ContextRequest(task_id=TASK_ID)
        backend = RecordingBackend()

        outcome = _invoke(backend, request=request)

        _success_data(outcome)
        self.assertEqual(backend.calls, [(request, TODAY)])
        self.assertIs(backend.calls[0][0], request)

    def test_handler_has_no_single_use_state_across_invocations(self) -> None:
        backend = RecordingBackend()

        first = _invoke(backend)
        second = _invoke(backend)

        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(render_outcome(outcome=first), render_outcome(outcome=second))

    def test_backend_exception_is_content_free_and_writes_no_streams(self) -> None:
        sensitive_canary = "raw-backend-secret-C2"
        backend = RecordingBackend(error=RuntimeError(sensitive_canary))
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            outcome = _invoke(backend)

        self.assertEqual(len(backend.calls), 1)
        _assert_content_free_internal(self, outcome)
        rendered = render_outcome(outcome=outcome)
        self.assertNotIn(sensitive_canary.encode("utf-8"), rendered)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_malformed_backend_mapping_is_content_free_internal_error(self) -> None:
        malformed = _result()
        malformed["entries"] = ["not-a-worklog-mapping"]
        outcome = _invoke(RecordingBackend(malformed))
        _assert_content_free_internal(self, outcome)


class ContextSuccessEnvelopeTests(unittest.TestCase):
    def test_success_has_exact_agent_context_outcome_and_envelope(self) -> None:
        outcome = _invoke(RecordingBackend())
        data = _success_data(outcome)

        self.assertEqual(outcome.command, COMMAND)
        self.assertEqual(outcome.task_id, TASK_ID)
        self.assertEqual(outcome.transport, TRANSPORT)
        self.assertEqual(outcome.workspace_uid, WORKSPACE_UID)
        self.assertIsNone(outcome.commit_state)
        self.assertIsNone(outcome.intent_id)
        self.assertIsNone(outcome.replayed)
        self.assertIsNone(outcome.retryable)
        self.assertIsNone(outcome.error_code)
        self.assertIsNone(outcome.error_message)
        self.assertEqual(outcome.error_details, {})
        envelope = json.loads(render_outcome(outcome=outcome))
        self.assertEqual(
            envelope,
            {
                "contract": "workstack.cli.v1",
                "data": data,
                "meta": {
                    "command": COMMAND,
                    "task_id": TASK_ID,
                    "transport": TRANSPORT,
                    "workspace_uid": WORKSPACE_UID,
                },
            },
        )

    def test_task_projection_is_an_exact_allowlist(self) -> None:
        data = _success_data(_invoke(RecordingBackend()))
        projected = data["task"]

        self.assertEqual(set(projected), CORE_FIELDS)
        self.assertEqual(projected, {key: _task()[key] for key in CORE_FIELDS})
        encoded = json.dumps(data, ensure_ascii=False)
        for canary in (
            "attachment-canary",
            "capture-canary",
            "objective-canary",
            "relationship-canary",
            "session-canary",
            "backend-only-canary",
        ):
            self.assertNotIn(canary, encoded)

    def test_context_shape_and_omission_markers_are_exact(self) -> None:
        data = _success_data(_invoke(RecordingBackend()))
        self.assertEqual(
            set(data), {"workspace_uid", "task", "recent_worklog", "omitted"}
        )
        self.assertEqual(data["workspace_uid"], WORKSPACE_UID)
        self.assertEqual(data["recent_worklog"], [])
        self.assertEqual(data["omitted"], OMITTED)

    def test_invalid_task_uid_fails_before_content_can_escape(self) -> None:
        sensitive_canary = "invalid-task-uid-secret"
        raw_task = _task(uid=sensitive_canary)
        outcome = _invoke(RecordingBackend(_result(task=raw_task)))
        _assert_content_free_internal(self, outcome)
        self.assertNotIn(sensitive_canary.encode(), render_outcome(outcome=outcome))

    def test_missing_task_allowlist_field_is_internal_failure(self) -> None:
        raw_task = _task()
        del raw_task["revision"]
        _assert_content_free_internal(
            self, _invoke(RecordingBackend(_result(task=raw_task)))
        )

    def test_task_id_mismatch_is_internal_failure(self) -> None:
        raw_task = _task(id="T-9999")
        _assert_content_free_internal(
            self, _invoke(RecordingBackend(_result(task=raw_task)))
        )


class ContextWorklogWindowTests(unittest.TestCase):
    def test_window_is_today_through_today_minus_thirty_inclusive(self) -> None:
        entries = [
            _entry(TODAY + datetime.timedelta(days=1), marker="future"),
            _entry(TODAY, marker="today"),
            _entry(TODAY - datetime.timedelta(days=30), marker="day-30"),
            _entry(TODAY - datetime.timedelta(days=31), marker="day-31"),
            _entry(TODAY, task_id="T-9999", marker="unrelated"),
        ]
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))

        self.assertEqual(
            [item["done"][0] for item in data["recent_worklog"]],
            ["today", "day-30"],
        )
        self.assertNotIn("recent_worklog_overflow", data["omitted"])

    def test_entries_are_newest_first_with_stable_same_day_order(self) -> None:
        entries = [
            _entry(TODAY - datetime.timedelta(days=2), marker="old"),
            _entry(TODAY, marker="same-day-first"),
            _entry(TODAY - datetime.timedelta(days=1), marker="middle"),
            _entry(TODAY, marker="same-day-second"),
        ]
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))

        self.assertEqual(
            [item["done"][0] for item in data["recent_worklog"]],
            ["same-day-first", "same-day-second", "middle", "old"],
        )

    def test_only_newest_five_relevant_entries_survive(self) -> None:
        entries = [
            _entry(TODAY - datetime.timedelta(days=offset), marker=str(offset))
            for offset in range(7)
        ]
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))

        self.assertEqual(
            [item["done"][0] for item in data["recent_worklog"]],
            ["0", "1", "2", "3", "4"],
        )
        self.assertEqual(
            data["omitted"], OMITTED + ["recent_worklog_overflow"]
        )

    def test_filtered_entries_do_not_create_a_false_overflow(self) -> None:
        entries = [_entry(TODAY, marker="kept")]
        entries.extend(
            _entry(TODAY, task_id="T-9999", marker="other-{}".format(index))
            for index in range(8)
        )
        entries.extend(
            _entry(
                TODAY - datetime.timedelta(days=31 + index),
                marker="stale-{}".format(index),
            )
            for index in range(8)
        )
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))

        self.assertEqual(len(data["recent_worklog"]), 1)
        self.assertEqual(data["recent_worklog"][0]["done"], ["kept"])
        self.assertEqual(data["omitted"], OMITTED)

    def test_worklog_projection_has_exact_fields_and_no_backend_canaries(self) -> None:
        data = _success_data(
            _invoke(RecordingBackend(_result(entries=[_entry(TODAY)])))
        )
        projected = data["recent_worklog"][0]

        self.assertEqual(set(projected), {"date", "done", "next", "blockers"})
        self.assertNotIn("task_id", projected)
        self.assertNotIn("backend_only", projected)

    def test_noncanonical_and_invalid_dates_are_filtered_not_crashed(self) -> None:
        entries = [
            _entry(TODAY, marker="valid"),
            {**_entry(TODAY, marker="timestamp"), "date": "2026-09-02T00:00:00"},
            {**_entry(TODAY, marker="invalid"), "date": "2026-02-30"},
            {**_entry(TODAY, marker="not-string"), "date": 20260902},
        ]
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))
        self.assertEqual(
            [item["done"][0] for item in data["recent_worklog"]], ["valid"]
        )


class ContextEnvelopeBoundTests(unittest.TestCase):
    def _ascii_detail_budget(self) -> int:
        baseline = _invoke(RecordingBackend(_result(task=_task(detail=""))))
        baseline_size = len(render_outcome(outcome=baseline))
        self.assertLess(baseline_size, MAX_ENVELOPE_BYTES)
        return MAX_ENVELOPE_BYTES - baseline_size

    def test_exact_full_envelope_32768_byte_boundary_succeeds(self) -> None:
        budget = self._ascii_detail_budget()
        outcome = _invoke(
            RecordingBackend(_result(task=_task(detail="x" * budget)))
        )
        rendered = render_outcome(outcome=outcome)

        self.assertEqual(len(rendered), MAX_ENVELOPE_BYTES)
        self.assertIsNone(outcome.error_code)

    def test_first_byte_over_full_envelope_boundary_is_context_too_large(self) -> None:
        sensitive_canary = "oversized-core-secret-"
        budget = self._ascii_detail_budget()
        detail = sensitive_canary + (
            "x" * (budget + 1 - len(sensitive_canary))
        )
        outcome = _invoke(RecordingBackend(_result(task=_task(detail=detail))))

        self.assertEqual(outcome.command, COMMAND)
        self.assertEqual(outcome.error_code, "context_too_large")
        self.assertEqual(
            outcome.error_message,
            "the Task core projection alone exceeds the envelope bound",
        )
        self.assertEqual(outcome.error_details, {})
        self.assertIsNone(outcome.data)
        rendered = render_outcome(outcome=outcome)
        self.assertNotIn(sensitive_canary.encode(), rendered)
        self.assertLessEqual(len(rendered), MAX_ENVELOPE_BYTES)

    def test_limit_counts_utf8_bytes_not_python_characters(self) -> None:
        budget = self._ascii_detail_budget()
        detail = ("가" * (budget // 3)) + ("x" * (budget % 3))
        outcome = _invoke(RecordingBackend(_result(task=_task(detail=detail))))
        rendered = render_outcome(outcome=outcome)

        self.assertEqual(len(rendered), MAX_ENVELOPE_BYTES)
        self.assertLess(len(rendered.decode("utf-8")), len(rendered))

    def test_large_worklog_is_trimmed_and_marks_overflow(self) -> None:
        budget = self._ascii_detail_budget()
        # Leave enough room for the overflow marker but not for this entry.
        task = _task(detail="d" * (budget - 400))
        entries = [_entry(TODAY, done=["w" * 900])]
        outcome = _invoke(RecordingBackend(_result(task=task, entries=entries)))
        data = _success_data(outcome)

        self.assertEqual(data["recent_worklog"], [])
        self.assertEqual(
            data["omitted"], OMITTED + ["recent_worklog_overflow"]
        )
        self.assertLessEqual(len(render_outcome(outcome=outcome)), MAX_ENVELOPE_BYTES)


class ContextMutationSentinelTests(unittest.TestCase):
    """Focused sentinels for the semantic mutations seen in parallel workers."""

    def test_mutant_using_get_context_instead_of_protocol_context_is_killed(self) -> None:
        backend = RecordingBackend()
        _success_data(_invoke(backend))
        self.assertEqual(len(backend.calls), 1)

    def test_mutant_using_30_dates_instead_of_31_is_killed(self) -> None:
        boundary = _entry(TODAY - datetime.timedelta(days=30), marker="boundary")
        data = _success_data(
            _invoke(RecordingBackend(_result(entries=[boundary])))
        )
        self.assertEqual(data["recent_worklog"][0]["done"], ["boundary"])

    def test_mutant_sorting_oldest_first_is_killed(self) -> None:
        entries = [
            _entry(TODAY - datetime.timedelta(days=1), marker="older"),
            _entry(TODAY, marker="newer"),
        ]
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))
        self.assertEqual(data["recent_worklog"][0]["done"], ["newer"])

    def test_mutant_retaining_six_entries_is_killed(self) -> None:
        entries = [
            _entry(TODAY - datetime.timedelta(days=index), marker=str(index))
            for index in range(6)
        ]
        data = _success_data(_invoke(RecordingBackend(_result(entries=entries))))
        self.assertEqual(len(data["recent_worklog"]), 5)
        self.assertIn("recent_worklog_overflow", data["omitted"])

    def test_mutant_measuring_data_instead_of_full_envelope_is_killed(self) -> None:
        budget = ContextEnvelopeBoundTests._ascii_detail_budget(self)
        outcome = _invoke(
            RecordingBackend(_result(task=_task(detail="z" * (budget + 1))))
        )
        self.assertEqual(outcome.error_code, "context_too_large")


if __name__ == "__main__":
    unittest.main()
