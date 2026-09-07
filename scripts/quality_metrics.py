#!/usr/bin/env python3
"""Decrease-only complexity, function-length and file-length ratchets."""

from __future__ import annotations

from typing import Any, Mapping


CCN_LIMIT = 15
FUNCTION_LINE_LIMIT = 100
FILE_LINE_LIMIT = 800


def function_length(function: Any) -> int:
    end = function.end_lineno or function.lineno
    return end - function.lineno + 1


def file_line_count(root: Any, relative: str) -> int:
    return len((root / relative).read_text(encoding="utf-8").splitlines())


def is_stable_symbol(item: Mapping[str, Any]) -> bool:
    if item.get("stable") is False:
        return False
    category = str(item.get("category") or "")
    if category in {"anonymous", "computed", "unresolved"}:
        return False
    return not str(item.get("name") or "").startswith("<anonymous@")


def debt_map(items: Mapping[str, Mapping[str, Any]], field: str, limit: int) -> dict[str, int]:
    measured: dict[str, int] = {}
    for symbol, item in items.items():
        if not is_stable_symbol(item):
            continue
        raw = item.get(field)
        if raw is None:
            continue
        value = int(raw)
        if value > limit:
            measured[symbol] = value
    return dict(sorted(measured.items()))


def file_debt_map(lengths: Mapping[str, int], limit: int) -> dict[str, int]:
    return dict(sorted((path, n) for path, n in lengths.items() if n > limit))


def admissible_file_length_debt(
    lengths: Mapping[str, int],
    sealed_lengths: Mapping[str, int],
    limit: int,
) -> dict[str, int]:
    """Record only giants that were already over the limit at the sealed HEAD."""

    debt: dict[str, int] = {}
    for path, current in lengths.items():
        if current <= limit:
            continue
        sealed = sealed_lengths.get(path)
        if sealed is None:
            continue
        if int(sealed) <= limit:
            continue
        debt[path] = current
    return dict(sorted(debt.items()))


def ratchet_errors(
    measured: Mapping[str, int],
    allowed: Mapping[str, int],
    new_template: str,
    up_template: str,
) -> list[str]:
    errors: list[str] = []
    for symbol, value in measured.items():
        previous = allowed.get(symbol)
        if previous is None:
            errors.append(new_template.format(symbol=symbol, value=value))
        elif value > int(previous):
            errors.append(
                up_template.format(symbol=symbol, previous=int(previous), value=value)
            )
    return errors


def python_ccn_errors(report: Mapping[str, Any], baseline: Mapping[str, Any]) -> list[str]:
    allowed = baseline.get("python_complexity_debt")
    if allowed is None:
        allowed = baseline.get("critical_complexity_debt", {})
    errors: list[str] = []
    for symbol, item in report.get("python_complexity", {}).items():
        ccn = int(item["ccn"])
        if ccn <= CCN_LIMIT:
            continue
        previous = allowed.get(symbol)
        critical = bool(item.get("critical"))
        if previous is None:
            kind = "critical function" if critical else "function"
            errors.append(
                f"new {kind} exceeds CCN {CCN_LIMIT}: {symbol} has CCN {ccn}"
            )
        elif ccn > int(previous):
            label = "critical complexity" if critical else "complexity"
            errors.append(f"{label} increased: {symbol} {previous} -> {ccn}")
    return errors


def typescript_ccn_errors(report: Mapping[str, Any], baseline: Mapping[str, Any]) -> list[str]:
    allowed = baseline.get("typescript_complexity_debt")
    if allowed is None:
        allowed = baseline.get("critical_typescript_complexity_debt", {})
    errors: list[str] = []
    for symbol, item in report.get("typescript_complexity", {}).items():
        if not is_stable_symbol(item):
            continue
        ccn = int(item["ccn"])
        if ccn <= CCN_LIMIT:
            continue
        previous = allowed.get(symbol)
        critical = bool(item.get("critical"))
        if previous is None:
            kind = "critical TypeScript function" if critical else "TypeScript function"
            errors.append(
                f"new {kind} exceeds CCN {CCN_LIMIT}: {symbol} has CCN {ccn}"
            )
        elif ccn > int(previous):
            label = (
                "critical TypeScript complexity" if critical else "TypeScript complexity"
            )
            errors.append(f"{label} increased: {symbol} {previous} -> {ccn}")
    return errors


def function_length_errors(report: Mapping[str, Any], baseline: Mapping[str, Any]) -> list[str]:
    python_measured = debt_map(
        report.get("python_complexity", {}), "length", FUNCTION_LINE_LIMIT
    )
    typescript_measured = debt_map(
        report.get("typescript_functions", {}), "length", FUNCTION_LINE_LIMIT
    )
    errors = ratchet_errors(
        python_measured,
        baseline.get("python_function_length_debt", {}),
        "new function exceeds 100 lines: {symbol} has {value} lines",
        "function length increased: {symbol} {previous} -> {value}",
    )
    errors.extend(
        ratchet_errors(
            typescript_measured,
            baseline.get("typescript_function_length_debt", {}),
            "new TypeScript function exceeds 100 lines: {symbol} has {value} lines",
            "TypeScript function length increased: {symbol} {previous} -> {value}",
        )
    )
    return errors


def file_length_errors(report: Mapping[str, Any], baseline: Mapping[str, Any]) -> list[str]:
    measured = file_debt_map(report.get("file_lengths", {}), FILE_LINE_LIMIT)
    return ratchet_errors(
        measured,
        baseline.get("production_file_length_debt", {}),
        "new production file exceeds 800 lines: {symbol} has {value} lines",
        "production file length increased: {symbol} {previous} -> {value}",
    )


def evaluate_structure(report: Mapping[str, Any], baseline: Mapping[str, Any], schema_version: int) -> list[str]:
    errors = list(report.get("config_errors", []))
    errors.extend(
        f"unclassified production source: {path}"
        for path in report.get("unclassified_files", [])
    )
    errors.extend(report.get("architecture_violations", []))
    for population, cycles in report.get("dependency_cycles", {}).items():
        for cycle in cycles:
            errors.append(f"{population} dependency cycle: {' -> '.join(cycle)}")
    if baseline.get("schema_version") != schema_version:
        errors.append("baseline schema mismatch")
    if baseline.get("config_digest") != report.get("config_digest"):
        errors.append("config_digest does not match the active quality configuration")
    return errors
