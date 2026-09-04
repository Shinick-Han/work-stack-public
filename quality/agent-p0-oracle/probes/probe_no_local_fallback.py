#!/usr/bin/env python3
"""Oracle probe P3: an unavailable online owner must never fall back to local mutation.

Oracle self-test seam (not part of the M0 product ABI):

    dispatch_after_online_failure(*, send, transport_unavailable, local_factory) -> str

- send() -> dict                              (online attempt; raises transport_unavailable)
- transport_unavailable: exception class      (injected by the probe)
- local_factory() -> object with .checkpoint  (local mutation path; must never be reached)

Invariants:
- the local factory is never invoked after any online failure     -> P0-NO-FALLBACK
- the online path performs at most two attempts                    -> P0-ONLINE-RETRY-BOUND
- the subject returns a string outcome without propagating         -> P0-DISPATCH-EXCEPTION

Usage:
    python -I probe_no_local_fallback.py --subject <module.py> --report <report.json>

Exit codes: 0 pass, 2 violation, 3 invalid subject, 4 probe error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

PROBE_NAME = "no-local-fallback"
VIOLATION_FALLBACK = "P0-NO-FALLBACK"
VIOLATION_RETRY_BOUND = "P0-ONLINE-RETRY-BOUND"
VIOLATION_EXCEPTION = "P0-DISPATCH-EXCEPTION"


class TransportUnavailable(Exception):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class _FakeLocalBackend:
    def __init__(self) -> None:
        self.calls = 0

    def checkpoint(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"committed": True}


class _FakeLocalFactory:
    def __init__(self) -> None:
        self.backends = 0

    def __call__(self) -> _FakeLocalBackend:
        self.backends += 1
        return _FakeLocalBackend()


class _AlwaysUnavailableTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(self) -> dict[str, Any]:
        self.calls += 1
        raise TransportUnavailable("server owner unreachable")


def _load_subject(subject_path: Path):
    spec = importlib.util.spec_from_file_location("agent_p0_probe_subject_p3", subject_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    dispatch = getattr(module, "dispatch_after_online_failure", None)
    if not callable(dispatch):
        return None
    return dispatch


def run_scenario(dispatch) -> tuple[str, list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    transport = _AlwaysUnavailableTransport()
    local_factory = _FakeLocalFactory()
    try:
        result = dispatch(send=transport.send, transport_unavailable=TransportUnavailable, local_factory=local_factory)
    except TransportUnavailable:
        violations.append({"id": VIOLATION_EXCEPTION, "observed": {"propagated": "TransportUnavailable"}, "expected": {"outcome": "str"}})
    except Exception as error:
        violations.append({"id": VIOLATION_EXCEPTION, "observed": {"error": type(error).__name__}, "expected": {"error": None}})
        result = None
    if result is not None and not isinstance(result, str):
        violations.append({"id": VIOLATION_EXCEPTION, "observed": {"returned": type(result).__name__}, "expected": {"returned": "str"}})

    if local_factory.backends != 0:
        violations.append({"id": VIOLATION_FALLBACK, "observed": {"local_factory_calls": local_factory.backends}, "expected": {"local_factory_calls": 0}})
    if transport.calls > 2:
        violations.append({"id": VIOLATION_RETRY_BOUND, "observed": {"send_calls": transport.calls}, "expected": {"max_attempts": 2}})
    return "violation" if violations else "pass", violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 no-local-fallback sentinel.")
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)

    subject_name = arguments.subject.name
    dispatch = _load_subject(arguments.subject)
    if dispatch is None:
        report = {"probe": PROBE_NAME, "subject": subject_name, "verdict": "invalid_subject", "violations": []}
    else:
        try:
            verdict, violations = run_scenario(dispatch)
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
