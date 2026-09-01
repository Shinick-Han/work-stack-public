#!/usr/bin/env python3
"""Run bounded, deterministic critical-invariant mutants in disposable copies."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SENTINELS: tuple[dict[str, Any], ...] = (
    {
        "id": "revision-safe-integer-bound",
        "path": "workstack/service.py",
        "original": "def _next_revision(record: dict[str, Any]) -> int:\n    current = _revision(record)\n    if current == MAX_REVISION:",
        "replacement": "def _next_revision(record: dict[str, Any]) -> int:\n    current = _revision(record)\n    if current > MAX_REVISION:",
        "tests": ("tests.test_store_identity",),
    },
    {
        "id": "capture-microsoft-url-bound",
        "path": "workstack/capture.py",
        "original": 'url = _string(value, "source.web_url", maximum=4096)',
        "replacement": 'url = _string(value, "source.web_url", maximum=40960)',
        "tests": ("tests.test_capture",),
    },
    {
        "id": "snapshot-byte-envelope",
        "path": "workstack/snapshot.py",
        "original": "MAX_SNAPSHOT_BYTES = 65_536",
        "replacement": "MAX_SNAPSHOT_BYTES = 65_536_000",
        "tests": ("tests.test_snapshot_v1", "tests.test_snapshot_product_export"),
    },
)


class MutationSentinelError(RuntimeError):
    pass


def validate_sentinels(root: Path = ROOT) -> None:
    identifiers: set[str] = set()
    for sentinel in SENTINELS:
        identifier = sentinel["id"]
        if identifier in identifiers:
            raise MutationSentinelError(f"duplicate mutation sentinel id: {identifier}")
        identifiers.add(identifier)
        path = root / sentinel["path"]
        body = path.read_text(encoding="utf-8")
        count = body.count(sentinel["original"])
        if count != 1:
            raise MutationSentinelError(
                f"mutation sentinel {identifier} expected one exact anchor in {path}, found {count}"
            )
        if sentinel["replacement"] in body:
            raise MutationSentinelError(f"mutation sentinel {identifier} is already present in production source")


def _copy_test_tree(source: Path, destination: Path) -> None:
    for directory in ("workstack", "tests", "contracts", "integrations"):
        candidate = source / directory
        if candidate.exists():
            shutil.copytree(candidate, destination / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for filename in ("run_work_stack.py",):
        candidate = source / filename
        if candidate.exists():
            shutil.copy2(candidate, destination / filename)


def run_sentinel(sentinel: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"workstack-mutant-{sentinel['id']}-") as temporary:
        mutant_root = Path(temporary)
        _copy_test_tree(root, mutant_root)
        target = mutant_root / sentinel["path"]
        body = target.read_text(encoding="utf-8")
        target.write_text(body.replace(sentinel["original"], sentinel["replacement"], 1), encoding="utf-8")
        command = [sys.executable, "-m", "unittest", *sentinel["tests"], "-v"]
        result = subprocess.run(
            command,
            cwd=mutant_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            raise MutationSentinelError(
                f"SURVIVED {sentinel['id']}: focused tests did not detect the critical mutation\n{result.stdout}"
            )
        return {
            "id": sentinel["id"],
            "status": "killed",
            "test_modules": list(sentinel["tests"]),
            "test_exit_code": result.returncode,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Validate and list sentinels without running mutants")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_sentinels()
    if args.list:
        for sentinel in SENTINELS:
            print(sentinel["id"])
        return 0
    results = [run_sentinel(sentinel) for sentinel in SENTINELS]
    for result in results:
        print(f"KILLED {result['id']} by {', '.join(result['test_modules'])}")
    print(f"mutation sentinels passed: {len(results)} killed, 0 survived")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MutationSentinelError, subprocess.TimeoutExpired) as error:
        print(f"mutation sentinel failed: {error}", file=sys.stderr)
        raise SystemExit(1)
