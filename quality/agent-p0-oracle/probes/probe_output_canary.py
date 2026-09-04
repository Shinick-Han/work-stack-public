#!/usr/bin/env python3
"""Oracle probe P4: rendered output is one JSON object plus LF and never carries secrets.

Oracle self-test seam (not part of the M0 product ABI):

    emit(*, payload, secrets, out, err) -> int

- payload: dict    (frozen typed outcome payload to render)
- secrets: dict    (canary values that must never reach out or err)
- out, err: text streams (the only stdout/stderr sinks the subject may use)

Invariants:
- stdout receives exactly one JSON object plus one trailing LF      -> P0-STDOUT-SINGLE-JSON
- no canary value appears on stdout                                 -> P0-SECRET-CANARY-STDOUT
- no canary value appears on stderr                                 -> P0-SECRET-CANARY-STDERR

Usage:
    python -I probe_output_canary.py --subject <module.py> --report <report.json>

Exit codes: 0 pass, 2 violation, 3 invalid subject, 4 probe error.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
from pathlib import Path
from typing import Any

PROBE_NAME = "output-canary"
VIOLATION_SINGLE_JSON = "P0-STDOUT-SINGLE-JSON"
VIOLATION_CANARY_STDOUT = "P0-SECRET-CANARY-STDOUT"
VIOLATION_CANARY_STDERR = "P0-SECRET-CANARY-STDERR"

PAYLOAD = {
    "contract": "workstack.cli.v1",
    "data": {"task_id": "T-0001"},
    "meta": {"command": "agent.checkpoint", "commit_state": "committed", "replayed": False},
}
SECRETS = {"token": "P0-CANARY-TOKEN-3F9A2C7E", "path": "P0-CANARY-PATH-71B4DE08"}


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _load_subject(subject_path: Path):
    spec = importlib.util.spec_from_file_location("agent_p0_probe_subject_p4", subject_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    emit = getattr(module, "emit", None)
    if not callable(emit):
        return None
    return emit


def run_scenario(emit) -> tuple[str, list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    out = io.StringIO()
    err = io.StringIO()
    try:
        result = emit(payload=PAYLOAD, secrets=SECRETS, out=out, err=err)
    except Exception as error:
        violations.append({"id": "P0-EMIT-EXCEPTION", "observed": {"error": type(error).__name__}, "expected": {"error": None}})
        result = None
    if result is not None and not isinstance(result, int):
        violations.append({"id": "P0-EMIT-EXCEPTION", "observed": {"returned": type(result).__name__}, "expected": {"returned": "int"}})

    stdout_text = out.getvalue()
    stderr_text = err.getvalue()

    single = stdout_text.endswith("\n") and stdout_text.count("\n") == 1
    parsed: Any = None
    if single:
        try:
            parsed = json.loads(stdout_text)
        except ValueError:
            single = False
    if not single or not isinstance(parsed, dict):
        violations.append(
            {
                "id": VIOLATION_SINGLE_JSON,
                "observed": {"lines": stdout_text.count("\n"), "parses_as_one_object": isinstance(parsed, dict)},
                "expected": {"lines": 1, "parses_as_one_object": True},
            }
        )

    for name, canary in sorted(SECRETS.items()):
        if canary in stdout_text:
            violations.append({"id": VIOLATION_CANARY_STDOUT, "observed": {"canary": name, "leaked": True}, "expected": {"leaked": False}})
        if canary in stderr_text:
            violations.append({"id": VIOLATION_CANARY_STDERR, "observed": {"canary": name, "leaked": True}, "expected": {"leaked": False}})
    return "violation" if violations else "pass", violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4 stdout single-JSON and secret-canary sentinel.")
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)

    subject_name = arguments.subject.name
    emit = _load_subject(arguments.subject)
    if emit is None:
        report = {"probe": PROBE_NAME, "subject": subject_name, "verdict": "invalid_subject", "violations": []}
    else:
        try:
            verdict, violations = run_scenario(emit)
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
