#!/usr/bin/env python3
"""Markdown and JSON structural-quality reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from quality_metrics import (
    CCN_LIMIT,
    FILE_LINE_LIMIT,
    FUNCTION_LINE_LIMIT,
    debt_map,
    file_debt_map,
)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def markdown_section(title: str, rows: list[str]) -> list[str]:
    lines = ["", f"## {title}", ""]
    lines.extend(rows if rows else ["- None"])
    return lines


def write_markdown(path: Path, report: Mapping[str, Any], errors: list[str]) -> None:
    python_ccn = debt_map(report.get("python_complexity", {}), "ccn", CCN_LIMIT)
    typescript_ccn = debt_map(report.get("typescript_complexity", {}), "ccn", CCN_LIMIT)
    python_len = debt_map(report.get("python_complexity", {}), "length", FUNCTION_LINE_LIMIT)
    typescript_len = debt_map(
        report.get("typescript_functions", {}), "length", FUNCTION_LINE_LIMIT
    )
    file_len = file_debt_map(report.get("file_lengths", {}), FILE_LINE_LIMIT)
    anonymous = list(report.get("anonymous_typescript_findings", []))
    unresolved = list(report.get("unresolved_typescript_identities", []))
    lines = [
        "# Work Stack structural quality report",
        "",
        f"- Candidate source digest: `{report['candidate_source_digest']}`",
        f"- Configuration digest: `{report['config_digest']}`",
        f"- Production files: {report['source_file_count']}",
        f"- Governed Python functions: {report.get('governed_python_functions', 0)}",
        f"- Governed stable TypeScript functions: {report.get('governed_typescript_functions', 0)}",
        f"- Python CCN>{CCN_LIMIT} offenders: {len(python_ccn)}",
        f"- Stable TypeScript CCN>{CCN_LIMIT} offenders: {len(typescript_ccn)}",
        f"- Python functions >{FUNCTION_LINE_LIMIT} lines: {len(python_len)}",
        f"- Stable TypeScript functions >{FUNCTION_LINE_LIMIT} lines: {len(typescript_len)}",
        f"- Production files >{FILE_LINE_LIMIT} lines: {len(file_len)}",
        f"- Diagnostic anonymous/computed TypeScript findings: {len(anonymous)}",
        f"- Unresolved named TypeScript identities: {len(unresolved)}",
        f"- TypeScript complexity findings: {len(report.get('typescript_complexity', {}))}",
        f"- TypeScript depth/size diagnostics: {sum(1 for item in report.get('typescript_diagnostics', []) if item.get('rule_id') != 'complexity')}",
        f"- Result: {'FAIL' if errors else 'PASS'}",
    ]
    lines.extend(
        markdown_section(
            "Python CCN offenders",
            [f"- `{symbol}` CCN {ccn}" for symbol, ccn in python_ccn.items()],
        )
    )
    lines.extend(
        markdown_section(
            "Stable TypeScript CCN offenders",
            [f"- `{symbol}` CCN {ccn}" for symbol, ccn in typescript_ccn.items()],
        )
    )
    lines.extend(
        markdown_section(
            "Diagnostic anonymous/computed TypeScript findings",
            [
                f"- `{item.get('path')}::{item.get('name')}` "
                f"{item.get('category', 'anonymous')} CCN {item.get('ccn')}"
                for item in anonymous
            ],
        )
    )
    lines.extend(
        markdown_section(
            "Unresolved named TypeScript identities",
            [
                f"- `{item.get('path')}::{item.get('name')}@{item.get('line')}:{item.get('column')}`"
                for item in unresolved
            ],
        )
    )
    length_rows = [f"- `{symbol}` {n} lines" for symbol, n in python_len.items()]
    length_rows.extend(f"- `{symbol}` {n} lines" for symbol, n in typescript_len.items())
    lines.extend(markdown_section("Function length offenders", length_rows))
    lines.extend(
        markdown_section(
            "Production file length offenders",
            [f"- `{path}` {n} lines" for path, n in file_len.items()],
        )
    )
    lines.extend(markdown_section("Blocking findings", [f"- {error}" for error in errors]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
