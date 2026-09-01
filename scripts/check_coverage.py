#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _python_metrics(summary: dict[str, Any]) -> dict[str, float]:
    return {
        "lines": float(summary["percent_covered"]),
        "branches": float(summary["percent_branches_covered"]),
    }


def _frontend_metrics(summary: dict[str, Any]) -> dict[str, float]:
    return {
        name: float(summary[name]["pct"])
        for name in ("lines", "branches", "functions")
        if name in summary
    }


def _frontend_relative(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    marker = "/frontend/"
    return normalized.split(marker, 1)[1] if marker in normalized else None


def _check_metrics(label: str, actual: dict[str, float], floors: dict[str, Any], errors: list[str]) -> None:
    for metric, floor_value in floors.items():
        floor = float(floor_value)
        value = actual.get(metric)
        if value is None:
            errors.append(f"{label} is missing {metric} coverage")
        elif value + 1e-9 < floor:
            errors.append(f"{label} {metric} coverage {value:.2f}% is below {floor:.2f}%")


def evaluate(
    python_report: dict[str, Any],
    frontend_report: dict[str, Any],
    floors: dict[str, Any],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if floors.get("schema_version") != 1:
        return ["coverage floor schema mismatch"], warnings

    python_policy = floors.get("python", {})
    _check_metrics("Python global", _python_metrics(python_report["totals"]), python_policy.get("global", {}), errors)
    python_files = {
        path.replace("\\", "/"): value for path, value in python_report.get("files", {}).items()
    }
    python_critical = set(python_policy.get("critical", {}))
    for path, policy in python_policy.get("critical", {}).items():
        item = python_files.get(path)
        if item is None:
            errors.append(f"Python critical file missing from coverage report: {path}")
            continue
        _check_metrics(path, _python_metrics(item["summary"]), policy, errors)
    for path, item in python_files.items():
        if path not in python_critical and int(item["summary"]["covered_lines"]) == 0:
            warnings.append(f"noncritical Python file has zero covered lines: {path}")

    frontend_policy = floors.get("frontend", {})
    _check_metrics("Frontend global", _frontend_metrics(frontend_report["total"]), frontend_policy.get("global", {}), errors)
    frontend_files = {
        relative: value
        for path, value in frontend_report.items()
        if path != "total" and (relative := _frontend_relative(path)) is not None
    }
    frontend_critical = set(frontend_policy.get("critical", {}))
    for path, policy in frontend_policy.get("critical", {}).items():
        item = frontend_files.get(path)
        if item is None:
            errors.append(f"Frontend critical file missing from coverage report: {path}")
            continue
        _check_metrics(path, _frontend_metrics(item), policy, errors)
    for path, item in frontend_files.items():
        if path not in frontend_critical and int(item["lines"]["covered"]) == 0:
            warnings.append(f"noncritical frontend file has zero covered lines: {path}")

    return sorted(set(errors)), sorted(set(warnings))


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root: Path, base_ref: str) -> dict[str, set[int]]:
    result = subprocess.run(
        ["git", "diff", "--unified=0", "--no-ext-diff", f"{base_ref}...HEAD", "--"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ValueError(f"cannot diff changed coverage base {base_ref}: {result.stderr.strip()}")
    paths: dict[str, set[int]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            paths.setdefault(current, set())
            continue
        match = HUNK_RE.match(line)
        if current and match:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            paths[current].update(range(start, start + count))
    return {path: lines for path, lines in paths.items() if lines}


def _ratio(covered: int, total: int) -> float | None:
    return None if total == 0 else 100.0 * covered / total


def evaluate_changed(
    changes: dict[str, set[int]],
    python_report: dict[str, Any],
    frontend_detail: dict[str, Any],
    floors: dict[str, Any],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    critical = set(floors.get("python", {}).get("critical", {})) | {
        f"frontend/{path}" for path in floors.get("frontend", {}).get("critical", {})
    }

    python_files = {
        path.replace("\\", "/"): value for path, value in python_report.get("files", {}).items()
    }
    frontend_files = {
        f"frontend/{relative}": value
        for path, value in frontend_detail.items()
        if (relative := _frontend_relative(path)) is not None
    }
    for path, lines in sorted(changes.items()):
        line_total = line_covered = branch_total = branch_covered = 0
        if path in python_files:
            item = python_files[path]
            executed = set(item.get("executed_lines", []))
            missing = set(item.get("missing_lines", []))
            executable = lines & (executed | missing)
            line_total = len(executable)
            line_covered = len(executable & executed)
            executed_branches = [pair for pair in item.get("executed_branches", []) if pair[0] in lines]
            missing_branches = [pair for pair in item.get("missing_branches", []) if pair[0] in lines]
            branch_covered = len(executed_branches)
            branch_total = branch_covered + len(missing_branches)
        elif path in frontend_files:
            item = frontend_files[path]
            for key, location in item.get("statementMap", {}).items():
                if int(location["start"]["line"]) in lines:
                    line_total += 1
                    line_covered += int(item.get("s", {}).get(key, 0)) > 0
            for key, location in item.get("branchMap", {}).items():
                branch_line = int(location.get("line") or location["loc"]["start"]["line"])
                if branch_line in lines:
                    counts = item.get("b", {}).get(key, [])
                    branch_total += len(counts)
                    branch_covered += sum(int(count) > 0 for count in counts)
        else:
            continue

        line_percent = _ratio(line_covered, line_total)
        branch_percent = _ratio(branch_covered, branch_total)
        findings: list[str] = []
        if line_percent is not None and line_percent + 1e-9 < 80.0:
            findings.append(f"changed lines {line_percent:.2f}% < 80.00%")
        if branch_percent is not None and branch_percent + 1e-9 < 70.0:
            findings.append(f"changed branches {branch_percent:.2f}% < 70.00%")
        if findings:
            message = f"{path}: {', '.join(findings)}"
            (errors if path in critical else warnings).append(message)
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check proportional Work Stack coverage floors")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python-report", type=Path, default=Path(".artifacts/quality/python-coverage.json"))
    parser.add_argument("--frontend-report", type=Path, default=Path(".artifacts/quality/frontend-coverage/coverage-summary.json"))
    parser.add_argument("--floors", type=Path, default=Path("quality/coverage-floors.json"))
    parser.add_argument("--frontend-detail", type=Path, default=Path(".artifacts/quality/frontend-coverage/coverage-final.json"))
    parser.add_argument("--base-ref", default="")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    rooted = lambda path: path if path.is_absolute() else root / path
    try:
        python_report = _read(rooted(args.python_report))
        frontend_report = _read(rooted(args.frontend_report))
        floors = _read(rooted(args.floors))
        errors, warnings = evaluate(python_report, frontend_report, floors)
        if args.base_ref and set(args.base_ref) != {"0"}:
            changed_errors, changed_warnings = evaluate_changed(
                changed_lines(root, args.base_ref),
                python_report,
                _read(rooted(args.frontend_detail)),
                floors,
            )
            errors.extend(changed_errors)
            warnings.extend(changed_warnings)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"COVERAGE GATE ERROR: {error}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"PASS: proportional coverage floors ({len(warnings)} warnings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
