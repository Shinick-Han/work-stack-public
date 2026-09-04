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
# Supported non-code graph assets: they must exist inside the repository and
# never become a code dependency edge.
ASSET_SUFFIXES = (".css", ".svg")
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


# The desktop shell is a script root: its immediate files are imported by bare
# name rather than through a package, so their module identity drops the two
# leading directory components. Everything BELOW that root is ordinary Python,
# so its relative path components are kept as a dotted identity. Returning the
# bare stem for every descendant instead collapsed each package initializer to
# "__init__" and every same-named child to its basename, which both invented a
# self edge for an initializer importing its sibling and hid the real edges of
# any second package with the same child name.
DESKTOP_SCRIPT_ROOT = ("desktop", "python-webview-shell")


def _module_parts(path: str) -> "list[str]":
    """The dotted identity components of one Python file, root-relative."""

    pure = PurePosixPath(path).with_suffix("")
    parts = list(pure.parts)
    if tuple(parts[:2]) == DESKTOP_SCRIPT_ROOT:
        parts = parts[2:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return parts


def _python_module(path: str) -> str | None:
    if PurePosixPath(path).suffix != ".py":
        return None
    parts = _module_parts(path)
    if not parts or any("-" in part for part in parts):
        return None
    return ".".join(parts)


def _alias_targets(
    module: str, aliases: "list[ast.alias]", modules: set[str]
) -> list[str]:
    """Resolve EACH alias of a from-import on its own.

    An alias that names a real submodule is an edge to that submodule; an alias
    that names anything else - an ordinary exported function, a class, a name
    that does not exist - keeps the edge to the module it was imported from. A
    single submodule among the aliases therefore cannot cancel the package edge
    the other names genuinely create, and no missing module is invented.
    """

    targets = []
    for alias in aliases:
        if alias.name == "*":
            continue
        candidate = f"{module}.{alias.name}" if module else alias.name
        targets.append(candidate if candidate in modules else module)
    return targets


def _package_of(path: str, module: str) -> str:
    """The package a relative import inside this file is relative to."""

    if PurePosixPath(path).name == "__init__.py":
        return module
    return module.rpartition(".")[0]


def _resolve_python_import(
    current: str,
    node: ast.AST,
    modules: set[str],
    package: str | None = None,
) -> set[str]:
    """The internal modules one import statement depends on.

    ``package`` is the package a relative import is relative TO. It must come
    from the measured filename, because the dotted name alone cannot say
    whether a module is a package initializer: workstack/pkg/__init__.py is the
    module workstack.pkg AND the package workstack.pkg, while
    workstack/pkg/user.py is the module workstack.pkg.user inside the package
    workstack.pkg. Guessing by stripping one component gets the initializer
    wrong. When it is not supplied the old assumption is kept, so an ordinary
    module resolves exactly as before.
    """

    imports: set[str] = set()
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level:
            owner = current.rpartition(".")[0] if package is None else package
            parts = owner.split(".") if owner else []
            keep = max(0, len(parts) - node.level + 1)
            module = ".".join(parts[:keep] + ([module] if module else []))
        # `from . import a, b` and `from pkg import a, b` name real submodules
        # when those submodules exist, so the edge is to each of them rather
        # than to the package alone. A name that is not a module - an ordinary
        # exported function or class - keeps the package or module edge, and no
        # submodule is invented for it.
        names = _alias_targets(module, node.names, modules) or [module]
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


def _frontend_asset(root: Path, source: str, specifier: str) -> bool | None:
    """Is this a supported non-code asset, and does the file really exist?

    ``None`` means the specifier is not an asset at all. ``True`` means a
    supported asset that exists inside the measured repository, which is a real
    dependency of the file but never a code edge. ``False`` means an asset
    specifier that does not resolve - an absent file or a path that escapes the
    repository - which stays an error rather than being ignored.
    """

    if not specifier.startswith(".") or not specifier.endswith(ASSET_SUFFIXES):
        return None
    candidate = (root / source).parent / specifier
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return candidate.is_file()


def _frontend_target(root: Path, source: str, specifier: str, known: set[str]) -> str | None:
    if not specifier.startswith("."):
        return None
    base = (root / source).parent / specifier
    candidates = [base]
    if PurePosixPath(specifier).suffix in ("", ".ts", ".tsx"):
        # Only an extensionless specifier may grow a source extension. Without
        # this, an explicit ./Thing.css or ./Thing.scss would silently become
        # the sibling ./Thing.tsx and manufacture a false edge.
        candidates += [
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


def _frontend_graph(
    root: Path, frontend_files: "list[str]"
) -> "tuple[dict[str, set[str]], list[str]]":
    """The frontend import graph, and the specifiers that did not resolve.

    A supported asset is a genuine dependency of the file but never a code
    edge, so it contributes no vertex; one that does not exist, or that escapes
    the repository, is reported rather than ignored.
    """

    known = set(frontend_files)
    graph: dict[str, set[str]] = {path: set() for path in frontend_files}
    errors: list[str] = []
    for path in frontend_files:
        text = (root / path).read_text(encoding="utf-8")
        for match in IMPORT_RE.finditer(text):
            specifier = match.group(1) or match.group(2)
            asset = _frontend_asset(root, path, specifier)
            if asset is True:
                continue
            if asset is False:
                errors.append(f"unresolved frontend asset in {path}: {specifier}")
                continue
            target = _frontend_target(root, path, specifier, known)
            if target:
                graph[path].add(target)
            elif specifier.startswith("."):
                errors.append(f"unresolved frontend import in {path}: {specifier}")
    return graph, errors


def _classify_layers(
    groups: "list[tuple[list[str], list[dict[str, Any]]]]",
) -> "tuple[dict[str, str], list[str]]":
    """Assign each file its layer, collecting the files no rule claims."""

    layers: dict[str, str] = {}
    unclassified: list[str] = []
    for paths, rules in groups:
        for path in paths:
            layer, errors = _layer_for(path, rules)
            if layer:
                layers[path] = layer
            unclassified.extend(errors)
    return layers, unclassified


def _python_imports(
    path: str, module: str, tree: ast.AST, python_modules: dict[str, str]
) -> "tuple[set[str], list[str]]":
    """The internal targets one module imports, and its unresolved internals."""

    targets: set[str] = set()
    errors: list[str] = []
    known = set(python_modules)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        resolved = _resolve_python_import(
            module, node, known, package=_package_of(path, module)
        )
        for imported in resolved:
            targets.add(python_modules[imported])
        if _is_internal_import(node) and not resolved:
            errors.append(f"unresolved internal Python import in {path}:{node.lineno}")
    return targets, errors


def _is_internal_import(node: "ast.Import | ast.ImportFrom") -> bool:
    if isinstance(node, ast.ImportFrom):
        return bool(node.level) or bool(node.module and node.module.startswith("workstack"))
    return any(alias.name.startswith("workstack") for alias in node.names)


def _python_graph(
    root: Path, python_files: "list[str]", critical_patterns: "list[str]"
) -> "tuple[dict[str, set[str]], dict[str, dict[str, Any]], list[str]]":
    """The Python import graph, the per-symbol complexity and any parse errors."""

    python_modules = {module: path for path in python_files if (module := _python_module(path))}
    graph: dict[str, set[str]] = {path: set() for path in python_files}
    complexities: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for module, path in python_modules.items():
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as error:
            errors.append(f"cannot parse {path}: {error}")
            continue
        targets, import_errors = _python_imports(path, module, tree, python_modules)
        graph[path].update(targets)
        errors.extend(import_errors)
        for qualified_name, node in _function_symbols(tree):
            complexities[f"{path}::{qualified_name}"] = {
                "path": path,
                "name": qualified_name,
                "line": node.lineno,
                "ccn": _complexity(node),
                "critical": _matches(path, critical_patterns),
            }
    return graph, complexities, errors


def _layer_violations(
    graphs: "tuple[dict[str, set[str]], ...]",
    layers: dict[str, str],
    rules: "list[dict[str, Any]]",
    exceptions: "set[tuple[str, str]]",
) -> "list[str]":
    """Edges that cross a layer boundary no rule or exception allows."""

    allowed = {
        str(item["name"]): {str(name) for name in item.get("may_import", [])}
        for item in rules
    }
    violations: list[str] = []
    for graph in graphs:
        for source, targets in graph.items():
            source_layer = layers.get(source)
            if not source_layer:
                continue
            for target in targets:
                target_layer = layers.get(target)
                if not target_layer or source_layer == target_layer:
                    continue
                if target_layer in allowed.get(source_layer, set()):
                    continue
                if (source, target) in exceptions:
                    continue
                violations.append(
                    f"forbidden layer import: {source} ({source_layer})"
                    f" -> {target} ({target_layer})"
                )
    return violations


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
    layers, unclassified = _classify_layers(
        [(python_files, python_layers), (frontend_files, frontend_layers)]
    )

    exceptions, exception_errors = _exception_pairs(config)
    config_errors.extend(exception_errors)
    python_graph, complexities, python_errors = _python_graph(
        root, python_files, list(config.get("critical_python_globs", []))
    )
    config_errors.extend(python_errors)

    frontend_graph, frontend_errors = _frontend_graph(root, frontend_files)
    config_errors.extend(frontend_errors)

    violations = _layer_violations(
        (python_graph, frontend_graph), layers, python_layers + frontend_layers, exceptions
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
