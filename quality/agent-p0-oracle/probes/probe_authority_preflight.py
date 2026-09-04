#!/usr/bin/env python3
"""Oracle probe P1: authority preflight must never construct Store or mutate the tree.

Oracle self-test seam (not part of the M0 product ABI):

    admit(*, data_dir, expected_workspace_uid, format_probe, uid_probe, store_factory) -> str

- format_probe(data_dir) -> "v3" | "v4" | None  (None means missing/unknown authority)
- uid_probe(data_dir) -> str                    (actual workspace UID)
- store_factory() -> object                     (records every Store construction)

Invariants:
- refusal cases (missing, v4, UID mismatch) must not construct Store  -> P0-STORE-BEFORE-PREFLIGHT
- refusals must leave the authority tree byte-identical               -> P0-PREFLIGHT-TREE-MUTATION
- the admitted case constructs Store exactly once.

Usage:
    python -I probe_authority_preflight.py --subject <module.py> --report <report.json>

Exit codes: 0 pass, 2 violation, 3 invalid subject, 4 probe error.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROBE_NAME = "authority-preflight"
VIOLATION_STORE = "P0-STORE-BEFORE-PREFLIGHT"
VIOLATION_TREE = "P0-PREFLIGHT-TREE-MUTATION"
VIOLATION_EXCEPTION = "P0-PREFLIGHT-EXCEPTION"

EXPECTED_UID = "2f0c6a10-5a4e-4a3f-9c6d-7c1f4f6b9e21"
OTHER_UID = "9d3b1c55-8e21-4f0a-b7a2-5e9d0a1c3f77"

AUTHORITY_RELATIVE = "authority/authority.json"
AUTHORITY_BYTES = ('{"format":"v3","workspace_uid":"%s"}' % EXPECTED_UID).encode("utf-8")


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    entries = []
    for path in root.rglob("*"):
        if path.is_file() or path.is_dir():
            entries.append((path.relative_to(root).as_posix().lower(), path.is_dir()))
    for relative, _is_dir in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        absolute = root / relative
        if absolute.is_file():
            digest.update(absolute.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class _StoreFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return object()


def _load_subject(subject_path: Path):
    spec = importlib.util.spec_from_file_location("agent_p0_probe_subject_p1", subject_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    admit = getattr(module, "admit", None)
    if not callable(admit):
        return None
    return admit


def run_scenario(admit) -> tuple[str, list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="p0-probe-p1-") as temporary:
        root = Path(temporary)
        authority_dir = root / "authority"
        authority_dir.mkdir()
        (authority_dir / "authority.json").write_bytes(AUTHORITY_BYTES)
        digest_before = _tree_digest(root)

        cases = [
            ("missing", root / "missing", None, OTHER_UID),
            ("v4", authority_dir, "v4", EXPECTED_UID),
            ("uid-mismatch", authority_dir, "v3", OTHER_UID),
        ]
        total_calls = 0
        for name, data_dir, probe_format, actual_uid in cases:
            store_factory = _StoreFactory()
            format_probe = lambda _data_dir, _fmt=probe_format: _fmt
            uid_probe = lambda _data_dir, _uid=actual_uid: _uid
            try:
                admit(
                    data_dir=data_dir,
                    expected_workspace_uid=EXPECTED_UID,
                    format_probe=format_probe,
                    uid_probe=uid_probe,
                    store_factory=store_factory,
                )
            except Exception as error:
                violations.append(
                    {"id": VIOLATION_EXCEPTION, "observed": {"case": name, "error": type(error).__name__}, "expected": {"case": name, "error": None}}
                )
                continue
            if store_factory.calls != 0:
                violations.append(
                    {"id": VIOLATION_STORE, "observed": {"case": name, "store_calls": store_factory.calls}, "expected": {"case": name, "store_calls": 0}}
                )
            total_calls += store_factory.calls

        store_factory = _StoreFactory()
        try:
            admitted = admit(
                data_dir=authority_dir,
                expected_workspace_uid=EXPECTED_UID,
                format_probe=lambda _data_dir: "v3",
                uid_probe=lambda _data_dir: EXPECTED_UID,
                store_factory=store_factory,
            )
        except Exception as error:
            violations.append({"id": VIOLATION_EXCEPTION, "observed": {"case": "admitted", "error": type(error).__name__}, "expected": {"case": "admitted", "error": None}})
        else:
            if not isinstance(admitted, str):
                violations.append({"id": VIOLATION_EXCEPTION, "observed": {"case": "admitted", "returned": type(admitted).__name__}, "expected": {"case": "admitted", "returned": "str"}})
            if store_factory.calls != 1:
                violations.append(
                    {"id": VIOLATION_STORE, "observed": {"case": "admitted", "store_calls": store_factory.calls}, "expected": {"case": "admitted", "store_calls": 1}}
                )
        total_calls += store_factory.calls

        digest_after = _tree_digest(root)
        if digest_before != digest_after:
            violations.append({"id": VIOLATION_TREE, "observed": {"tree_digest_after": digest_after, "authority_paths_unchanged": False}, "expected": {"tree_digest_unchanged": True, "total_store_calls": 1}})
        return "violation" if violations else "pass", violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 authority preflight sentinel.")
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)

    subject_name = arguments.subject.name
    admit = _load_subject(arguments.subject)
    if admit is None:
        report = {"probe": PROBE_NAME, "subject": subject_name, "verdict": "invalid_subject", "violations": []}
    else:
        try:
            verdict, violations = run_scenario(admit)
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
