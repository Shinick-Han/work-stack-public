#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = 1
IMPORT_RE = re.compile(
    r"(?:import|export)\s+(?:type\s+)?(?:[^'\"]*?\s+from\s+)?['\"]([^'\"]+)['\"]"
    r"|import\(\s*['\"]([^'\"]+)['\"]\s*\)"
)
FRONTEND_DECLARATION_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)
FRONTEND_COMPLEXITY_RE = re.compile(r"complexity of (\d+)", re.IGNORECASE)
FRONTEND_MESSAGE_NAME_RE = re.compile(r"^(?:Async\s+)?(?:Function|Method)\s+'([^']+)'", re.IGNORECASE)


def _path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches(path: str, patterns: Iterable[str]) -> bool:
    pure = PurePosixPath(path)
    return any(pure.match(pattern) or fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_config_bytes(payload: bytes) -> bytes:
    """Keep configuration digests stable across Git line-ending policies."""
    return payload.replace(b"\r\n", b"\n")


def _digest_files(root: Path, paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(set(paths)):
        candidate = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if candidate.is_file():
            payload = _canonical_config_bytes(candidate.read_bytes())
            digest.update(str(len(payload)).encode("ascii"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(payload).digest())
        else:
            digest.update(b"MISSING")
        digest.update(b"\n")
    return digest.hexdigest()


def load_config(root: Path) -> dict[str, Any]:
    path = root / "quality" / "quality-config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported quality config schema: {payload.get('schema_version')!r}")
    return payload


def _discover(root: Path, config: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    populations: dict[str, list[str]] = {}
    errors: list[str] = []
    owners: dict[str, str] = {}
    for source_set in config.get("source_sets", []):
        name = str(source_set["name"])
        extensions = {str(extension) for extension in source_set.get("extensions", [])}
        exclusions = [str(pattern) for pattern in source_set.get("exclude_globs", [])]
        found: list[str] = []
        for root_name in source_set.get("roots", []):
            source_root = root / str(root_name)
            candidates = [source_root] if source_root.is_file() else source_root.rglob("*") if source_root.is_dir() else []
            if not source_root.exists():
                errors.append(f"missing production root: {root_name}")
                continue
            for candidate in candidates:
                if not candidate.is_file() or candidate.suffix not in extensions:
                    continue
                relative = _path(candidate, root)
                if _matches(relative, exclusions):
                    continue
                if relative in owners:
                    errors.append(f"source belongs to multiple populations: {relative}")
                    continue
                owners[relative] = name
                found.append(relative)
        populations[name] = sorted(found)
    return populations, sorted(set(errors))


def _layer_for(path: str, layers: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    matches = [str(layer["name"]) for layer in layers if _matches(path, layer.get("globs", []))]
    if len(matches) == 1:
        return matches[0], []
    if not matches:
        return None, [path]
    return None, [f"{path} (multiple layers: {', '.join(sorted(matches))})"]


def _python_module(path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return None
    if pure.parts[:2] == ("desktop", "python-webview-shell"):
        return pure.stem
    parts = list(pure.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or any("-" in part for part in parts):
        return None
    return ".".join(parts)


def _resolve_python_import(current: str, node: ast.AST, modules: set[str]) -> set[str]:
    imports: set[str] = set()
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level:
            package = current.split(".")[:-1]
            keep = max(0, len(package) - node.level + 1)
            module = ".".join(package[:keep] + ([module] if module else []))
        names = [module]
    else:
        return imports
    for name in names:
        candidate = name
        while candidate:
            if candidate in modules:
                imports.add(candidate)
                break
            candidate = candidate.rpartition(".")[0]
    return imports


def _frontend_target(root: Path, source: str, specifier: str, known: set[str]) -> str | None:
    if not specifier.startswith("."):
        return None
    base = (root / source).parent / specifier
    candidates = [
        base,
        base.with_suffix(".ts"),
        base.with_suffix(".tsx"),
        base / "index.ts",
        base / "index.tsx",
    ]
    for candidate in candidates:
        try:
            relative = candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if relative in known:
            return relative
    return None


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    result: set[tuple[str, ...]] = set()
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def canonical(items: list[str]) -> tuple[str, ...]:
        body = items[:-1]
        rotations = [tuple(body[index:] + body[:index]) for index in range(len(body))]
        smallest = min(rotations)
        return smallest + (smallest[0],)

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            index = stack.index(node)
            result.add(canonical(stack[index:] + [node]))
            return
        visiting.add(node)
        stack.append(node)
        for target in sorted(graph.get(node, set())):
            visit(target)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)
    return [list(cycle) for cycle in sorted(result)]


def _complexity(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(0, len(node.values) - 1)
        elif isinstance(node, ast.Try):
            score += len(node.handlers) + int(bool(node.orelse))
        elif isinstance(node, ast.Match):
            score += max(0, len(node.cases) - 1)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(generator.ifs) for generator in node.generators)
    return score


def _function_symbols(tree: ast.AST) -> Iterable[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.items: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualified_name = ".".join([*self.scope, node.name])
            self.items.append((qualified_name, node))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API name
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast API name
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API name
            self._visit_function(node)

    collector = Collector()
    collector.visit(tree)
    return collector.items


def _frontend_function_name(message: str, source_line: str, line: int, column: int) -> tuple[str, bool]:
    named = FRONTEND_MESSAGE_NAME_RE.search(message)
    if named:
        return named.group(1), True
    declaration = FRONTEND_DECLARATION_RE.search(source_line)
    if declaration:
        return declaration.group(1) or declaration.group(2), True
    return f"<anonymous@{line}:{column}>", False


def _measure_frontend_complexity(
    root: Path,
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    settings = config.get("frontend_complexity")
    if not settings:
        return {}, [], []
    command = [str(part) for part in settings.get("command", [])]
    if not command:
        return {}, [], ["frontend complexity command is missing"]
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
        return {}, [], [f"frontend complexity command failed: {detail[-1] if detail else completed.returncode}"]
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError as error:
        return {}, [], [f"frontend complexity output is not JSON: {error}"]

    complexities: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    critical_patterns = [str(pattern) for pattern in settings.get("critical_globs", [])]
    for result in payload:
        try:
            path = Path(str(result["filePath"])).resolve().relative_to(root).as_posix()
        except (KeyError, ValueError):
            continue
        source_lines = (root / path).read_text(encoding="utf-8").splitlines()
        for message in result.get("messages", []):
            rule_id = str(message.get("ruleId") or "")
            line = int(message.get("line") or 1)
            column = int(message.get("column") or 1)
            text = str(message.get("message") or "")
            diagnostic = {
                "path": path,
                "rule_id": rule_id,
                "line": line,
                "column": column,
                "message": text,
            }
            diagnostics.append(diagnostic)
            if rule_id != "complexity":
                continue
            match = FRONTEND_COMPLEXITY_RE.search(text)
            if not match:
                continue
            source_line = source_lines[line - 1] if 0 < line <= len(source_lines) else ""
            name, stable = _frontend_function_name(text, source_line, line, column)
            symbol = f"{path}::{name}"
            if symbol in complexities:
                symbol = f"{symbol}@{line}:{column}"
                stable = False
            complexities[symbol] = {
                "path": path,
                "name": name,
                "line": line,
                "column": column,
                "ccn": int(match.group(1)),
                "critical": _matches(path, critical_patterns),
                "stable": stable,
            }
    return dict(sorted(complexities.items())), diagnostics, []


def _exception_pairs(config: dict[str, Any]) -> tuple[set[tuple[str, str]], list[str]]:
    pairs: set[tuple[str, str]] = set()
    errors: list[str] = []
    today = date.today().isoformat()
    for index, item in enumerate(config.get("architecture_exceptions", [])):
        missing = [field for field in ("from", "to", "rationale", "expires") if not item.get(field)]
        if missing:
            errors.append(f"architecture exception {index} missing: {', '.join(missing)}")
            continue
        expiry = str(item["expires"])
        try:
            date.fromisoformat(expiry)
        except ValueError:
            errors.append(f"architecture exception {index} has invalid expiry: {expiry}")
            continue
        if expiry < today:
            errors.append(f"architecture exception {index} expired on {expiry}")
            continue
        pairs.add((str(item["from"]), str(item["to"])))
    return pairs, errors


def measure(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    populations, config_errors = _discover(root, config)
    all_files = sorted(path for paths in populations.values() for path in paths)
    config_inputs = [str(path) for path in config.get("config_inputs", [])]
    for path in config_inputs:
        if not (root / path).is_file():
            config_errors.append(f"missing config input: {path}")

    python_files = sorted(path for path in all_files if path.endswith(".py"))
    frontend_files = sorted(path for path in all_files if path.endswith((".ts", ".tsx")))
    python_layers = list(config.get("python_layers", []))
    frontend_layers = list(config.get("frontend_layers", []))
    layers: dict[str, str] = {}
    unclassified: list[str] = []
    for path in python_files:
        layer, errors = _layer_for(path, python_layers)
        if layer:
            layers[path] = layer
        unclassified.extend(errors)
    for path in frontend_files:
        layer, errors = _layer_for(path, frontend_layers)
        if layer:
            layers[path] = layer
        unclassified.extend(errors)

    exceptions, exception_errors = _exception_pairs(config)
    config_errors.extend(exception_errors)
    violations: list[str] = []
    python_modules = {module: path for path in python_files if (module := _python_module(path))}
    python_graph: dict[str, set[str]] = {path: set() for path in python_files}
    complexities: dict[str, dict[str, Any]] = {}
    critical_patterns = list(config.get("critical_python_globs", []))
    for module, path in python_modules.items():
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as error:
            config_errors.append(f"cannot parse {path}: {error}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                resolved = _resolve_python_import(module, node, set(python_modules))
                for imported in resolved:
                    target = python_modules[imported]
                    python_graph[path].add(target)
                internal = (
                    isinstance(node, ast.ImportFrom)
                    and (bool(node.level) or bool(node.module and node.module.startswith("workstack")))
                ) or (
                    isinstance(node, ast.Import)
                    and any(alias.name.startswith("workstack") for alias in node.names)
                )
                if internal and not resolved:
                    config_errors.append(f"unresolved internal Python import in {path}:{node.lineno}")
        for qualified_name, node in _function_symbols(tree):
            symbol = f"{path}::{qualified_name}"
            complexities[symbol] = {
                "path": path,
                "name": qualified_name,
                "line": node.lineno,
                "ccn": _complexity(node),
                "critical": _matches(path, critical_patterns),
            }

    frontend_known = set(frontend_files)
    frontend_graph: dict[str, set[str]] = {path: set() for path in frontend_files}
    for path in frontend_files:
        text = (root / path).read_text(encoding="utf-8")
        for match in IMPORT_RE.finditer(text):
            specifier = match.group(1) or match.group(2)
            target = _frontend_target(root, path, specifier, frontend_known)
            if target:
                frontend_graph[path].add(target)
            elif specifier.startswith(".") and not specifier.endswith(".css"):
                config_errors.append(f"unresolved frontend import in {path}: {specifier}")

    layer_rules: dict[str, set[str]] = {}
    for item in python_layers + frontend_layers:
        layer_rules[str(item["name"])] = {str(name) for name in item.get("may_import", [])}
    for graph in (python_graph, frontend_graph):
        for source, targets in graph.items():
            source_layer = layers.get(source)
            for target in targets:
                target_layer = layers.get(target)
                if not source_layer or not target_layer or source_layer == target_layer:
                    continue
                if target_layer not in layer_rules.get(source_layer, set()) and (source, target) not in exceptions:
                    violations.append(
                        f"forbidden layer import: {source} ({source_layer}) -> {target} ({target_layer})"
                    )

    typescript_complexity, typescript_diagnostics, frontend_complexity_errors = (
        _measure_frontend_complexity(root, config)
    )
    config_errors.extend(frontend_complexity_errors)

    source_digest = _digest_files(root, all_files)
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_source_digest": source_digest,
        "config_digest": _digest_files(root, config_inputs),
        "source_populations": populations,
        "source_file_count": len(all_files),
        "unclassified_files": sorted(set(unclassified)),
        "config_errors": sorted(set(config_errors)),
        "architecture_violations": sorted(set(violations)),
        "dependency_cycles": {
            "python": _cycles(python_graph),
            "frontend": _cycles(frontend_graph),
        },
        "python_complexity": dict(sorted(complexities.items())),
        "typescript_complexity": typescript_complexity,
        "typescript_diagnostics": typescript_diagnostics,
    }


def build_baseline(report: dict[str, Any], measurement_commit: str) -> dict[str, Any]:
    critical_debt = {
        symbol: item["ccn"]
        for symbol, item in report.get("python_complexity", {}).items()
        if item.get("critical") and int(item["ccn"]) > 15
    }
    critical_typescript_debt = {
        symbol: item["ccn"]
        for symbol, item in report.get("typescript_complexity", {}).items()
        if item.get("critical") and item.get("stable") and int(item["ccn"]) > 15
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_commit": measurement_commit,
        "measurement_source_digest": report["candidate_source_digest"],
        "config_digest": report["config_digest"],
        "source_populations": {
            name: len(paths) for name, paths in report.get("source_populations", {}).items()
        },
        "critical_complexity_debt": dict(sorted(critical_debt.items())),
        "critical_typescript_complexity_debt": dict(sorted(critical_typescript_debt.items())),
        "coverage_floors": {},
        "temporary_exceptions": [],
    }


def evaluate(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    errors = list(report.get("config_errors", []))
    errors.extend(f"unclassified production source: {path}" for path in report.get("unclassified_files", []))
    errors.extend(report.get("architecture_violations", []))
    for population, cycles in report.get("dependency_cycles", {}).items():
        for cycle in cycles:
            errors.append(f"{population} dependency cycle: {' -> '.join(cycle)}")
    if baseline.get("schema_version") != SCHEMA_VERSION:
        errors.append("baseline schema mismatch")
    if baseline.get("config_digest") != report.get("config_digest"):
        errors.append("config_digest does not match the active quality configuration")

    allowed_debt = baseline.get("critical_complexity_debt", {})
    for symbol, item in report.get("python_complexity", {}).items():
        ccn = int(item["ccn"])
        if not item.get("critical") or ccn <= 15:
            continue
        previous = allowed_debt.get(symbol)
        if previous is None:
            errors.append(f"new critical function exceeds CCN 15: {symbol} has CCN {ccn}")
        elif ccn > int(previous):
            errors.append(f"critical complexity increased: {symbol} {previous} -> {ccn}")
    allowed_typescript_debt = baseline.get("critical_typescript_complexity_debt", {})
    for symbol, item in report.get("typescript_complexity", {}).items():
        ccn = int(item["ccn"])
        if not item.get("critical") or not item.get("stable") or ccn <= 15:
            continue
        previous = allowed_typescript_debt.get(symbol)
        if previous is None:
            errors.append(f"new critical TypeScript function exceeds CCN 15: {symbol} has CCN {ccn}")
        elif ccn > int(previous):
            errors.append(f"critical TypeScript complexity increased: {symbol} {previous} -> {ccn}")
    return sorted(set(errors))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any], errors: list[str]) -> None:
    lines = [
        "# Work Stack structural quality report",
        "",
        f"- Candidate source digest: `{report['candidate_source_digest']}`",
        f"- Configuration digest: `{report['config_digest']}`",
        f"- Production files: {report['source_file_count']}",
        f"- TypeScript complexity findings: {len(report.get('typescript_complexity', {}))}",
        f"- TypeScript depth/size diagnostics: {sum(1 for item in report.get('typescript_diagnostics', []) if item.get('rule_id') != 'complexity')}",
        f"- Result: {'FAIL' if errors else 'PASS'}",
        "",
        "## Blocking findings",
        "",
    ]
    lines.extend(f"- {error}" for error in errors)
    if not errors:
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=False, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Work Stack proportional structural quality gate")
    parser.add_argument("command", choices=("report", "check", "baseline"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path, default=Path(".artifacts/quality/structural-report.json"))
    parser.add_argument("--baseline", type=Path, default=Path("quality/structural-baseline.json"))
    parser.add_argument("--measurement-commit")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    config = load_config(root)
    report = measure(root, config)
    report_path = args.report if args.report.is_absolute() else root / args.report
    baseline_path = args.baseline if args.baseline.is_absolute() else root / args.baseline

    if args.command == "baseline":
        fatal = list(report["config_errors"])
        fatal.extend(report["unclassified_files"])
        fatal.extend(report["architecture_violations"])
        fatal.extend(cycle for cycles in report["dependency_cycles"].values() for cycle in cycles)
        if fatal:
            for finding in fatal:
                print(f"BLOCKED: {finding}", file=sys.stderr)
            return 1
        baseline = build_baseline(report, args.measurement_commit or _git_head(root))
        _write_json(baseline_path, baseline)
        _write_json(report_path, report)
        _write_markdown(report_path.with_suffix(".md"), report, [])
        print(f"Wrote baseline: {baseline_path}")
        return 0

    if not baseline_path.is_file():
        print(f"Missing baseline: {baseline_path}", file=sys.stderr)
        return 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    errors = evaluate(report, baseline)
    report["blocking_findings"] = errors
    _write_json(report_path, report)
    _write_markdown(report_path.with_suffix(".md"), report, errors)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {report['source_file_count']} production files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
