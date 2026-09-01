from __future__ import annotations

import copy
import re
import unittest

from workstack.service import WorkStack
from workstack.store import StoreCorruptError


def _running_session(**changes: object) -> dict[str, object]:
    session: dict[str, object] = {
        "id": "WS-000001",
        "task_id": "T-0001",
        "task": "Prepare decision brief",
        "date": "2026-08-31",
        "state": "running",
        "worklog_state": "not_ready",
        "started_at": "2026-08-31T01:00:00Z",
        "updated_at": "2026-08-31T01:00:00Z",
        "segments": [
            {"started_at": "2026-08-31T01:00:00Z", "ended_at": None}
        ],
    }
    session.update(changes)
    return session


def _worklog(session: object | None = None) -> dict[str, object]:
    sessions = [] if session is None else [session]
    return {"days": {"2026-08-31": {"sessions": sessions}}}


class WorkSessionRecordsCharacterizationTests(unittest.TestCase):
    def assert_invalid(self, value: dict[str, object], message: str) -> None:
        with self.assertRaisesRegex(StoreCorruptError, "^" + re.escape(message) + "$"):
            WorkStack._work_session_records(value)

    def test_valid_running_paused_and_stopped_records_keep_order_and_identity(self) -> None:
        running = _running_session()
        paused = _running_session(
            id="WS-000002",
            state="paused",
            segments=[
                {
                    "started_at": "2026-08-30T01:00:00Z",
                    "ended_at": "2026-08-30T01:10:00Z",
                }
            ],
            date="2026-08-30",
            started_at="2026-08-30T01:00:00Z",
            updated_at="2026-08-30T01:10:00Z",
        )
        stopped = _running_session(
            id="WS-000003",
            state="stopped",
            worklog_state="pending",
            segments=[
                {
                    "started_at": "2026-08-29T01:00:00Z",
                    "ended_at": "2026-08-29T01:10:00Z",
                }
            ],
            date="2026-08-29",
            started_at="2026-08-29T01:00:00Z",
            updated_at="2026-08-29T01:10:00Z",
        )
        worklog = {
            "days": {
                "2026-08-29": {"sessions": [stopped]},
                "2026-08-30": {"sessions": [paused]},
            }
        }
        records = WorkStack._work_session_records(worklog)
        self.assertEqual(records, [stopped, paused])
        self.assertIs(records[0], stopped)

        self.assertEqual(WorkStack._work_session_records(_worklog(running)), [running])

    def test_day_and_session_envelope_failures_are_stable(self) -> None:
        cases = (
            ({"days": []}, "persisted worklog days are invalid"),
            ({"days": {"not-a-date": {"sessions": []}}}, "persisted worklog date is invalid"),
            ({"days": {"2026-08-31": []}}, "persisted worklog day is invalid"),
            ({"days": {"2026-08-31": {"sessions": {}}}}, "persisted work sessions are invalid"),
            (_worklog("session"), "persisted work session is invalid"),
        )
        for value, message in cases:
            with self.subTest(message=message):
                self.assert_invalid(value, message)

    def test_session_identity_state_and_timestamp_failures_are_stable(self) -> None:
        cases = (
            ("id", lambda session: session.update(id="WS-1"), "persisted work session id is invalid"),
            ("date", lambda session: session.update(date="2026-08-30"), "persisted work session date is invalid"),
            ("task id", lambda session: session.update(task_id="task"), "persisted work session task id is invalid"),
            ("task title", lambda session: session.update(task="  "), "persisted work session task title is invalid"),
            ("state", lambda session: session.update(state="unknown"), "persisted work session state is invalid"),
            ("worklog state", lambda session: session.update(worklog_state="pending"), "persisted work session worklog state is invalid"),
            ("started", lambda session: session.update(started_at="bad"), "persisted work session started_at is invalid"),
            ("updated", lambda session: session.update(updated_at="bad"), "persisted work session updated_at is invalid"),
        )
        for label, mutate, message in cases:
            session = _running_session()
            mutate(session)
            with self.subTest(label=label):
                self.assert_invalid(_worklog(session), message)

    def test_segment_chain_failures_are_stable(self) -> None:
        cases = (
            ("empty", [], "persisted work session segments are invalid"),
            ("schema", [{}], "persisted work session segment is invalid"),
            (
                "overlap",
                [
                    {"started_at": "2026-08-31T01:00:00Z", "ended_at": "2026-08-31T01:10:00Z"},
                    {"started_at": "2026-08-31T01:09:00Z", "ended_at": None},
                ],
                "persisted work session segments overlap",
            ),
            (
                "open middle",
                [
                    {"started_at": "2026-08-31T01:00:00Z", "ended_at": None},
                    {"started_at": "2026-08-31T01:10:00Z", "ended_at": None},
                ],
                "persisted work session open segment is invalid",
            ),
            (
                "negative",
                [{"started_at": "2026-08-31T01:10:00Z", "ended_at": "2026-08-31T01:00:00Z"}],
                "persisted work session segment has negative duration",
            ),
            (
                "running closed",
                [{"started_at": "2026-08-31T01:00:00Z", "ended_at": "2026-08-31T01:10:00Z"}],
                "persisted work session open segment is inconsistent",
            ),
        )
        for label, segments, message in cases:
            session = _running_session(segments=segments)
            with self.subTest(label=label):
                self.assert_invalid(_worklog(session), message)

    def test_duplicate_ids_and_multiple_active_sessions_are_rejected(self) -> None:
        first = _running_session()
        duplicate = copy.deepcopy(first)
        worklog = {"days": {"2026-08-31": {"sessions": [first, duplicate]}}}
        self.assert_invalid(worklog, "persisted work session id is invalid")

        paused = _running_session(
            id="WS-000002",
            state="paused",
            segments=[
                {"started_at": "2026-08-31T02:00:00Z", "ended_at": "2026-08-31T02:10:00Z"}
            ],
        )
        worklog = {"days": {"2026-08-31": {"sessions": [first, paused]}}}
        self.assert_invalid(worklog, "multiple active work sessions are persisted")


if __name__ == "__main__":
    unittest.main()
