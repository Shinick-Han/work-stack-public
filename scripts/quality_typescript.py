#!/usr/bin/env python3
"""TypeScript complexity, lexical identity and function-length collection."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from quality_metrics import is_stable_symbol


FRONTEND_DECLARATION_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)
FRONTEND_COMPLEXITY_RE = re.compile(r"complexity of (\d+)", re.IGNORECASE)
FRONTEND_MESSAGE_NAME_RE = re.compile(
    r"^(?:Async\s+)?(?:Function|Method)\s+'([^']+)'", re.IGNORECASE
)
COLLECTOR = Path(__file__).with_name("quality_typescript_ast.cjs")


def frontend_function_name(
    message: str, source_line: str, line: int, column: int
) -> tuple[str, bool]:
    named = FRONTEND_MESSAGE_NAME_RE.search(message)
    if named:
        return named.group(1), True
    declaration = FRONTEND_DECLARATION_RE.search(source_line)
    if declaration:
        return declaration.group(1) or declaration.group(2), True
    return f"<anonymous@{line}:{column}>", False


def qualify_typescript_item(item: dict[str, Any]) -> tuple[str | None, str]:
    if item.get("computed"):
        return None, "computed"
    if item.get("anonymous") or not item.get("name"):
        return None, "anonymous"
    containers = list(item.get("containers") or [])
    if any(container is None or container == "" for container in containers):
        return None, "anonymous"
    symbol = item["path"] + "::" + ".".join([*containers, str(item["name"])])
    return symbol, "stable"


def index_typescript_functions(
    raw_items: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    functions: dict[str, dict[str, Any]] = {}
    diagnostic: list[dict[str, Any]] = []
    collisions: dict[str, list[dict[str, Any]]] = {}
    claimed: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        item = dict(raw)
        symbol, category = qualify_typescript_item(item)
        item["category"] = category
        item["stable"] = category == "stable"
        if category != "stable" or symbol is None:
            diagnostic.append(item)
            continue
        item["name"] = symbol.split("::", 1)[1]
        if symbol in claimed:
            collisions.setdefault(symbol, [claimed[symbol]]).append(item)
            continue
        claimed[symbol] = item
        functions[symbol] = item
    errors: list[str] = []
    for symbol, items in sorted(collisions.items()):
        winner = functions.pop(symbol, None)
        group = ([winner] if winner else []) + items
        for item in group:
            item["category"] = "unresolved"
            item["stable"] = False
            diagnostic.append(item)
        errors.append(f"unresolved TypeScript named identity: {symbol}")
    return dict(sorted(functions.items())), diagnostic, errors


def collect_typescript_ast(
    root: Path, frontend_files: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    if not frontend_files:
        return [], []
    if not (root / "frontend" / "node_modules" / "typescript").exists():
        return [], []
    if not COLLECTOR.is_file():
        return [], ["frontend function-length collector is missing"]
    try:
        completed = subprocess.run(
            ["node", str(COLLECTOR)],
            input=json.dumps({"root": str(root), "files": frontend_files}),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return [], [f"frontend function-length command failed: {error}"]
    return _ast_payload(completed)


def _ast_payload(completed: subprocess.CompletedProcess[str]) -> tuple[list[dict[str, Any]], list[str]]:
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return [], [
            "frontend function-length command failed: "
            + (detail[-1] if detail else str(completed.returncode))
        ]
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError as error:
        return [], [f"frontend function-length output is not JSON: {error}"]
    functions = payload.get("functions") if isinstance(payload, dict) else None
    if not isinstance(functions, list):
        return [], ["frontend function-length output is not an object"]
    return functions, []


def _eslint_relative_path(root: Path, result: dict[str, Any]) -> str | None:
    try:
        return Path(str(result["filePath"])).resolve().relative_to(root).as_posix()
    except (KeyError, ValueError):
        return None


def _run_eslint_complexity(
    root: Path, config: dict[str, Any]
) -> tuple[list[Any] | None, list[str]]:
    settings = config.get("frontend_complexity")
    if not settings:
        return [], []
    command = [str(part) for part in settings.get("command", [])]
    if not command:
        return None, ["frontend complexity command is missing"]
    if os.name == "nt" and command[0] == "npm":
        command[0] = "npm.cmd"
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return None, [
            "frontend complexity command failed: "
            f"{detail[-1] if detail else completed.returncode}"
        ]
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError as error:
        return None, [f"frontend complexity output is not JSON: {error}"]
    if not isinstance(payload, list):
        return None, ["frontend complexity output is not JSON"]
    return payload, []


def _eslint_complexity_record(
    path: str,
    source_lines: list[str],
    message: dict[str, Any],
    critical: bool,
    complexities: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if str(message.get("ruleId") or "") != "complexity":
        return None
    text = str(message.get("message") or "")
    match = FRONTEND_COMPLEXITY_RE.search(text)
    if not match:
        return None
    line = int(message.get("line") or 1)
    column = int(message.get("column") or 1)
    source_line = source_lines[line - 1] if 0 < line <= len(source_lines) else ""
    name, stable = frontend_function_name(text, source_line, line, column)
    symbol = f"{path}::{name}"
    if symbol in complexities:
        symbol = f"{symbol}@{line}:{column}"
        stable = False
    return {
        "symbol": symbol,
        "path": path,
        "name": name,
        "line": line,
        "column": column,
        "ccn": int(match.group(1)),
        "critical": critical,
        "stable": stable,
        "category": "stable" if stable else "anonymous",
    }


def measure_frontend_complexity(
    root: Path,
    config: dict[str, Any],
    matches: Callable[[str, Iterable[str]], bool],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    payload, errors = _run_eslint_complexity(root, config)
    if errors:
        return {}, [], errors
    settings = config.get("frontend_complexity") or {}
    critical_patterns = [str(pattern) for pattern in settings.get("critical_globs", [])]
    complexities: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    for result in payload or []:
        path = _eslint_relative_path(root, result)
        if path is None:
            continue
        source_lines = (root / path).read_text(encoding="utf-8").splitlines()
        critical = matches(path, critical_patterns)
        for message in result.get("messages", []):
            diagnostics.append(
                {
                    "path": path,
                    "rule_id": str(message.get("ruleId") or ""),
                    "line": int(message.get("line") or 1),
                    "column": int(message.get("column") or 1),
                    "message": str(message.get("message") or ""),
                }
            )
            record = _eslint_complexity_record(
                path, source_lines, message, critical, complexities
            )
            if record is None:
                continue
            symbol = str(record.pop("symbol"))
            complexities[symbol] = record
    return dict(sorted(complexities.items())), diagnostics, []


def merge_typescript_complexity(
    functions: dict[str, dict[str, Any]],
    eslint_items: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_line: dict[tuple[str, int], str] = {}
    for symbol, item in functions.items():
        by_line[(str(item["path"]), int(item["line"]))] = symbol
    merged: dict[str, dict[str, Any]] = {}
    unmatched_anonymous: list[dict[str, Any]] = []
    for item in eslint_items.values():
        key = by_line.get((str(item["path"]), int(item["line"])))
        if key is None:
            if is_stable_symbol(item):
                merged[f"{item['path']}::{item['name']}"] = dict(item)
            else:
                unmatched_anonymous.append(dict(item))
            continue
        target = dict(functions[key])
        target["ccn"] = int(item["ccn"])
        target["critical"] = bool(item.get("critical"))
        functions[key] = target
        if is_stable_symbol(target):
            merged[key] = target
    return dict(sorted(merged.items())), unmatched_anonymous


def measure_typescript(
    root: Path,
    config: dict[str, Any],
    frontend_files: list[str],
    matches: Callable[[str, Iterable[str]], bool],
) -> dict[str, Any]:
    raw_items, ast_errors = collect_typescript_ast(root, frontend_files)
    functions, diagnostic, identity_errors = index_typescript_functions(raw_items)
    eslint_items, eslint_diagnostics, eslint_errors = measure_frontend_complexity(
        root, config, matches
    )
    complexity, unmatched_anonymous = merge_typescript_complexity(functions, eslint_items)
    anonymous = [
        item for item in diagnostic if item.get("category") in {"anonymous", "computed"}
    ]
    anonymous.extend(unmatched_anonymous)
    unresolved = [item for item in diagnostic if item.get("category") == "unresolved"]
    if eslint_items and not functions:
        complexity = {
            symbol: item for symbol, item in eslint_items.items() if is_stable_symbol(item)
        }
        anonymous.extend(
            item for item in eslint_items.values() if not is_stable_symbol(item)
        )
    return {
        "typescript_functions": functions,
        "typescript_complexity": complexity,
        "typescript_diagnostics": eslint_diagnostics,
        "anonymous_typescript_findings": anonymous,
        "unresolved_typescript_identities": unresolved,
        "errors": ast_errors + identity_errors + eslint_errors,
    }
