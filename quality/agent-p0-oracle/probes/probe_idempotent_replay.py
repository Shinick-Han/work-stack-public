#!/usr/bin/env python3
"""Oracle probe P2: idempotent replay must reuse identical request bytes and one key.

Oracle self-test seam (not part of the M0 product ABI):

    replay_checkpoint(*, send, body, intent_key, store, response_lost) -> str

- send(body: bytes, intent_key: str) -> dict   (transport attempt; may raise response_lost)
- store.record(entry: dict) -> None            (fake local journal; a compliant subject never
                                                records locally, the server owns idempotency)
- response_lost: exception class raised by the fake transport after a lost response

Fake server behavior: the first attempt commits one journal entry for the key and raises
response_lost; an identical retry (same body bytes and same key) replays without appending;
any different body/key pair is recorded as a conflict attempt.

Invariants:
- at most two send attempts                                        -> P0-RETRY-LOOP
- every attempt uses identical body bytes and identical intent key -> P0-FRESH-KEY
- exactly one journal entry for the intent key; no local recording -> P0-DUPLICATE-WORKLOG
- response_lost must be handled, not propagated                    -> P0-REPLAY-EXCEPTION

Usage:
    python -I probe_idempotent_replay.py --subject <module.py> --report <report.json>

Exit codes: 0 pass, 2 violation, 3 invalid subject, 4 probe error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

PROBE_NAME = "idempotent-replay"
VIOLATION_RETRY_LOOP = "P0-RETRY-LOOP"
VIOLATION_FRESH_KEY = "P0-FRESH-KEY"
VIOLATION_DUPLICATE = "P0-DUPLICATE-WORKLOG"
VIOLATION_EXCEPTION = "P0-REPLAY-EXCEPTION"

INTENT_KEY = "agent:selftest:checkpoint-1"
BODY = b'{"blockers":[],"date":"2026-09-02","done":["Implemented the workspace preflight."],"next":["Add response-loss coverage."],"task_id":"T-0001"}'


class ResponseLost(Exception):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class _FakeStore:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(dict(entry))


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []
        self.journal: dict[str, int] = {}

    def send(self, body: bytes, intent_key: str) -> dict[str, Any]:
        self.calls.append((bytes(body), str(intent_key)))
        if len(self.calls) == 1:
            self.journal[intent_key] = self.journal.get(intent_key, 0) + 1
            raise ResponseLost("response lost after commit")
        first_body, first_key = self.calls[0]
        if body == first_body and intent_key == first_key:
            return {"committed": True, "replayed": True}
        self.journal[intent_key] = self.journal.get(intent_key, 0) + 1
        return {"committed": False, "conflict": True}


def _load_subject(subject_path: Path):
    spec = importlib.util.spec_from_file_location("agent_p0_probe_subject_p2", subject_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    replay = getattr(module, "replay_checkpoint", None)
    if not callable(replay):
        return None
    return replay


def run_scenario(replay_checkpoint) -> tuple[str, list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    transport = _FakeTransport()
    store = _FakeStore()
    try:
        result = replay_checkpoint(
            send=transport.send,
            body=BODY,
            intent_key=INTENT_KEY,
            store=store,
            response_lost=ResponseLost,
        )
    except ResponseLost:
        violations.append({"id": VIOLATION_EXCEPTION, "observed": {"propagated": "ResponseLost"}, "expected": {"bounded_replay": 1}})
    except Exception as error:
        violations.append({"id": VIOLATION_EXCEPTION, "observed": {"error": type(error).__name__}, "expected": {"error": None}})
        result = None
    if result is not None and not isinstance(result, str):
        violations.append({"id": VIOLATION_EXCEPTION, "observed": {"returned": type(result).__name__}, "expected": {"returned": "str"}})

    attempts = len(transport.calls)
    if attempts > 2:
        violations.append({"id": VIOLATION_RETRY_LOOP, "observed": {"send_calls": attempts}, "expected": {"max_attempts": 2}})
    if attempts >= 1:
        first_body, first_key = transport.calls[0]
        for index, (body, key) in enumerate(transport.calls[1:], start=2):
            if body != first_body or key != first_key:
                violations.append(
                    {
                        "id": VIOLATION_FRESH_KEY,
                        "observed": {"attempt": index, "intent_key": key, "body_equal": body == first_body},
                        "expected": {"intent_key": first_key, "body_equal": True},
                    }
                )
                break
    journal_entries = transport.journal.get(INTENT_KEY, 0)
    total_entries = journal_entries + len(store.entries)
    if total_entries != 1 or store.entries:
        violations.append(
            {
                "id": VIOLATION_DUPLICATE,
                "observed": {"journal_entries": journal_entries, "local_records": len(store.entries)},
                "expected": {"journal_entries": 1, "local_records": 0},
            }
        )
    return "violation" if violations else "pass", violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2 idempotent replay sentinel.")
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)

    subject_name = arguments.subject.name
    replay_checkpoint = _load_subject(arguments.subject)
    if replay_checkpoint is None:
        report = {"probe": PROBE_NAME, "subject": subject_name, "verdict": "invalid_subject", "violations": []}
    else:
        try:
            verdict, violations = run_scenario(replay_checkpoint)
            report = {"probe": PROBE_NAME, "subject": subject_name, "verdict": verdict, "violations": violations}
        except Exception as error:  # pragma: no cover - defensive, fail loud
            report = {"probe": PROBE_NAME, "subject": subject_name, "verdict": "probe_error", "violations": [{"id": "P0-PROBE-ERROR", "observed": {"error": type(error).__name__}, "expected": {}}]}

    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.report.with_name(arguments.report.name + ".tmp")
    temporary.write_bytes(_canonical_bytes(report))
    temporary.replace(arguments.report)

    if report["verdict"] == "pass":
        return 0
    if report["verdict"] == "violation":
        return 2
    if report["verdict"] == "invalid_subject":
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
