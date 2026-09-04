#!/usr/bin/env python3
"""Audit Work Stack source exports and runtime data for sensitive material."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import mmap
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import unquote


# Repository audits are allow-listed. Dependencies, compiler output, caches, and VCS
# metadata are reproducible/non-product artifacts rather than source-release inputs.
EXCLUDED_PARTS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".vite",
    "__pycache__", "build", "coverage", "dist", "node_modules", "venv", ".venv",
}
EXCLUDED_PARTS_CASEFOLDED = {item.casefold() for item in EXCLUDED_PARTS}
SOURCE_DIRS = (
    "workstack", "frontend/src", "frontend/e2e", "contracts", "data", "desktop", "docs",
    "integrations", "licenses", "quality", "scripts", "tests", "theme", "web", ".github",
)
SOURCE_FILES = (
    ".gitignore", "README.md", "SECURITY.md", "run_work_stack.py",
    "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
    "frontend/THIRD_PARTY_NOTICES.md", "frontend/tsconfig.json",
    "frontend/tsconfig.app.json", "frontend/tsconfig.node.json", "frontend/vite.config.ts",
    ".coveragerc", ".gitattributes", "frontend/eslint.config.js",
    "frontend/playwright.compat.config.ts", "frontend/playwright.config.ts",
)
ROOT_SOURCE_SUFFIXES = {".json", ".md", ".py", ".ps1", ".sh", ".toml", ".txt", ".yaml", ".yml"}
# SVG is XML, so it is ordinary scanned UTF-8 text like every other entry here:
# each text rule, every --deny term, the invalid-UTF-8 check and the symbolic-link
# refusal apply to it unchanged. It is NOT an exempt asset, and admitting it grants
# nothing to any other suffix - binaries such as .ico remain unexpected file types.
# .cs is ordinary source text. .jsonl is UTF-8 text AND a JSON value per nonblank
# line: the raw text rules and every --deny term scan the complete original text as
# usual, and each nonblank line is additionally parsed and put through the same
# recursive structured rules, with line-specific diagnostics. Blank lines are
# insignificant separators; no line or value is silently discarded.
TEXT_SUFFIXES = {
    "", ".cs", ".css", ".html", ".js", ".json", ".jsonl", ".md", ".mjs", ".py", ".ps1",
    ".sha256", ".sh", ".svg", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
BLOCKED_SUFFIXES = {
    ".db", ".log", ".npy", ".onnx", ".pickle", ".pkl", ".pyc",
    ".sqlite", ".sqlite3", ".tok", ".pw",
}

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
CANARY_RE = re.compile(r"(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE", re.I)
PRIVATE_KEY_RE = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")
PERSONAL_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/]"
    r"|/(?:home|Users|u)/)[^\\/\s]+[\\/]"
)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)[\"']?(?:password|passwd|secret|access[_-]?token|api[_-]?key)[\"']?"
    r"\s*[:=]\s*[\"'][^\"']{6,}[\"']"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{24,}\b")
STRUCTURED_URL_CREDENTIAL_RE = re.compile(
    r"(?i)[?&#;](?:"
    r"(?:access|refresh|id|oauth)[_.-]?token|"
    r"(?:oauth|authorization)[_.-]?code|"
    r"authorization|bearer|token|client[_.-]?secret|"
    r"password|passwd|api[_.-]?key|secret|code"
    r")[\"']?\s*[:=]"
)
STRUCTURED_CREDENTIAL_VALUE_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|(?<![A-Za-z0-9])(?:"
    r"(?:access|refresh|id|oauth)[_.-]?token|"
    r"client[_.-]?(?:secret|assertion)|authorization|bearer|token|"
    r"password|passwd|api[_.-]?key|secret|saml[_.-]?response"
    r")(?![A-Za-z0-9])[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r")"
)
STRUCTURED_RECIPIENT_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:to|cc|bcc|recipients?)(?![A-Za-z0-9])\s*[:=]"
)
HEADER_RE = re.compile(r"(?im)^(?:from|to|cc|bcc|subject|sent|date):\s*.+$")
HTML_RE = re.compile(r"</?[A-Za-z][^>]*>")
QUOTED_REPLY_RE = re.compile(r"(?im)^on .{1,240} wrote:\s*$")
QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
CREDENTIAL_KEYS = {
    "password", "passwd", "secret", "access_token", "accesstoken", "api_key", "apikey",
}

TEXT_RULES = {
    "email address": EMAIL_RE,
    "raw-content canary": CANARY_RE,
    "private key": PRIVATE_KEY_RE,
    "credential value": CREDENTIAL_ASSIGNMENT_RE,
    "bearer token": BEARER_RE,
    "personal path": PERSONAL_PATH_RE,
}
NEGATIVE_FIXTURES = {
    Path("contracts/capture-packet-v1.negative-raw.json"),
    Path("contracts/capture-packet-v1.value-negative-cases.json"),
}
NEGATIVE_RULES = {
    "email address", "raw-content canary", "raw mail header block", "HTML source content",
    "quoted reply", "long quoted passage", "credential value", "bearer token",
}
NEGATIVE_TEST_RULES = {
    Path("tests/test_capture.py"): {"raw-content canary"},
    Path("tests/test_audit_export.py"): {"email address", "raw-content canary"},
}
SOURCE_METADATA_RULES = {
    Path("frontend/package-lock.json"): {"email address"},
}
FROZEN_FIXTURE_HASHES = {
    Path("contracts/workstack-conduit-v1/safety/snapshot-v1-safety-cases.json"):
        "480a16ccb18338417c718aca6e7729037431a201a464b6298e0819a0c349f92f",
}
# ONE reviewed synthetic fixture occurrence, and SOURCE policy only.
#
# Source versus tree is a real boundary, not a convenience. SOURCE mode audits the
# working repository, whose own negative tests must contain the poisoned values they
# prove never reach the wire; deleting that literal would delete the evidence. TREE
# mode audits a prepared export of arbitrary bytes for publication, where no such
# provenance exists, so it stays strict and refuses this very path - the allowance
# below is never consulted there.
#
# The allowance is bound to the exact complete source of one uniquely named method,
# hashed after LF normalization, and then to the single personal-path match found
# inside that verified extent. Only that one span is permitted. A changed literal, a
# changed function body, a duplicated or shadowed class/method, a parse error, the
# same literal anywhere else in the same file, a relocated occurrence or any other
# filename all lose it. Every other rule and every --deny term keeps scanning the
# ORIGINAL complete text, including this extent. This is a reviewed exception for one
# audited fixture; it is not a claim that arbitrary Python, or any whole file,
# function or test directory, is safe.
APPROVED_SOURCE_FIXTURES = {
    Path("tests/test_sse_event_delivery.py"): (
        "SnapshotFieldContract",
        "test_poisoned_store_fields_never_reach_the_wire",
        "personal path",
        "83c77b1afad623b59ff87be030a4d06049df5f52b18ab28eada21e453736cbc5",
    ),
}
# Exactly one known, already released, generated and brand-checked binary, under
# SOURCE policy only, and only after the ordinary symbolic-link and regular-file
# refusals plus exact SHA256 identity. This is not a generic .ico or binary
# allowance: a wrong path, changed bytes, an appended payload, a malformed
# replacement, another binary suffix or a symbolic link all refuse, and tree mode
# keeps rejecting it. The hash proves identity of these known bytes; nothing here
# claims the file was scanned as UTF-8 text, so success output counts it apart.
APPROVED_SOURCE_BINARIES = {
    Path("desktop/python-webview-shell/assets/WorkStack-Mark-Lime-v2.ico"):
        "9ce3c456141509bd62f2a499c7af55f41b8dc1dc69c75d3e0171dcc53c9ea209",
}
# Two exact literal command placeholders in the frozen Oracle manifest that the
# HTML rule reads as markup. ONLY the "HTML source content" rule, ONLY these exact
# structural paths, and ONLY these exact original values are permitted, in SOURCE
# policy. A changed value, a moved path, another file, a percent-encoded
# replacement, real HTML added to either string, or duplicate-key ambiguity
# anywhere in the document all lose it. Every other rule and every --deny term
# still scans the original value and the original complete text, and tree mode
# still refuses these matches.
# Keyed by STRUCTURAL COORDINATE, not by the readable path. A coordinate is the
# exact sequence of steps taken to reach a value: ("key", name) for an object
# member and ("index", position) for an array element. The dotted rendering below
# is for diagnostics only and is ambiguous by construction - one flat key literally
# named "digest_recipes.candidate_diff" renders identically to two nested keys, and
# so would a partially collapsed spelling of the longer path. Those are genuinely
# different documents, so they must not inherit this allowance. Separating the two
# also keeps an object member distinct from an array element that renders alike.
APPROVED_STRUCTURED_VALUES = {
    Path("quality/agent-p0-oracle/manifest.v1.json"): {
        (("key", "digest_recipes"), ("key", "candidate_diff")): (
            "HTML source content",
            "normalize git diff --raw -z --no-abbrev <base>...<head> tuples, including"
            " status, modes, object IDs and both rename paths, into canonical JSON"
            " before hashing",
        ),
        (
            ("key", "envelope"), ("key", "failure"), ("key", "variants"),
            ("key", "ordinary_command_failure"), ("key", "meta_required"),
            ("key", "command"),
        ): (
            "HTML source content",
            "agent.<command>",
        ),
    },
}
PRODUCT_LOCK = Path(".workstack.lock")
EMPTY_LOCK_VALUES = {b"", b"\0"}
VALIDATION_PERCENT_DECODE_ROUNDS = 5
_AMBIGUOUS: set = set()


class PercentDecodingLimitError(ValueError):
    pass


def _excluded(relative: Path) -> bool:
    return any(part.casefold() in EXCLUDED_PARTS_CASEFOLDED for part in relative.parts)


def _walk(root: Path, start: Path, *, exclude_generated: bool) -> Iterator[Path]:
    if not start.exists():
        return
    if start.is_file() or start.is_symlink():
        yield start
        return
    for path in sorted(start.rglob("*")):
        relative = path.relative_to(root)
        if exclude_generated and _excluded(relative):
            continue
        if path.is_file() or path.is_symlink():
            yield path


def _looks_like_source_repo(root: Path) -> bool:
    return (root / "workstack").is_dir() and (root / "frontend" / "src").is_dir()


def _source_mode(root: Path, mode: str) -> bool:
    """Whether this run applies the source allow-list rather than the tree policy."""

    return mode == "source" or (mode == "auto" and _looks_like_source_repo(root))


def files(root: Path, mode: str = "auto") -> Iterator[Path]:
    """Yield the explicit source-release set or every file in a data export."""

    source_mode = _source_mode(root, mode)
    seen: set[Path] = set()
    if source_mode:
        starts = [root / item for item in SOURCE_DIRS]
        starts.extend(root / item for item in SOURCE_FILES)
        starts.extend(
            path for path in root.iterdir()
            if path.is_file() and (path.suffix.casefold() in ROOT_SOURCE_SUFFIXES or path.name == ".gitignore")
        )
    else:
        starts = [root]
    for start in starts:
        for path in _walk(root, start, exclude_generated=source_mode):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def _decoded(value: str) -> str:
    decoded = value
    for _ in range(VALIDATION_PERCENT_DECODE_ROUNDS):
        candidate = unquote(decoded)
        if candidate == decoded:
            return decoded
        decoded = candidate
    if unquote(decoded) != decoded:
        raise PercentDecodingLimitError
    return decoded


def _json_strings(value: Any, coordinate: tuple = ()) -> Iterator[tuple[tuple, str]]:
    """Every string in the document, with the exact steps taken to reach it.

    The coordinate is structural: ("key", name) for an object member and
    ("index", position) for an array element. It is never flattened, so no key
    whose own name contains dots or brackets can impersonate another location.
    Nothing about the input is rewritten, normalized or dropped here.
    """

    if isinstance(value, dict):
        for key, child in value.items():
            child_coordinate = coordinate + (("key", key),)
            if isinstance(key, str) and key.casefold().replace("-", "_") in CREDENTIAL_KEYS:
                if child not in (None, "", False):
                    yield child_coordinate, "__CREDENTIAL_VALUE__"
            yield from _json_strings(child, child_coordinate)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_strings(child, coordinate + (("index", index),))
    elif isinstance(value, str):
        yield coordinate, value


def _render_path(coordinate: tuple) -> str:
    """The familiar readable diagnostic path for one coordinate.

    Ambiguous on purpose - it is meant to be legible, not to identify anything.
    """

    rendered = "$"
    for kind, step in coordinate:
        rendered += ".{}".format(step) if kind == "key" else "[{}]".format(step)
    return rendered


def _negative_rule_allowed(relative: Path, label: str) -> bool:
    normalized = Path(relative.as_posix())
    return (
        label in NEGATIVE_RULES
        and normalized in NEGATIVE_FIXTURES
    ) or (
        label in NEGATIVE_TEST_RULES.get(normalized, set())
    )


def _rule_allowed(relative: Path, label: str) -> bool:
    normalized = Path(relative.as_posix())
    return _negative_rule_allowed(normalized, label) or label in SOURCE_METADATA_RULES.get(
        normalized, set()
    )


def _exact_frozen_fixture(relative: Path, path: Path) -> bool:
    expected = FROZEN_FIXTURE_HASHES.get(Path(relative.as_posix()))
    return expected is not None and hashlib.sha256(path.read_bytes()).hexdigest() == expected


def _approved_binary(relative: Path, path: Path) -> bool:
    """Whether this is the one known binary, by regular file and exact identity."""

    expected = APPROVED_SOURCE_BINARIES.get(Path(relative.as_posix()))
    if expected is None or not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return hashlib.sha256(data).hexdigest() == expected


def _duplicated_keys(pairs: "list[tuple[str, Any]]") -> dict:
    """json object hook that records ambiguity instead of silently collapsing it."""

    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        _AMBIGUOUS.add(True)
    return dict(pairs)


def _parsed_json(text: str) -> "tuple[Any, bool]":
    """The parsed document and whether any object in it had duplicate keys."""

    _AMBIGUOUS.clear()
    value = json.loads(text, object_pairs_hook=_duplicated_keys)
    return value, bool(_AMBIGUOUS)


def _structured_json_findings(
    relative: Path, value: Any, allowance: dict | None = None
) -> Iterator[str]:
    allowed = allowance or {}
    for coordinate, original in _json_strings(value):
        json_path = _render_path(coordinate)
        if original == "__CREDENTIAL_VALUE__":
            if not _negative_rule_allowed(relative, "credential value"):
                yield "credential value at {}".format(json_path)
            continue
        try:
            text = _decoded(original)
        except PercentDecodingLimitError:
            yield "over-depth percent encoding at {}".format(json_path)
            continue
        rules = (
            ("email address", bool(EMAIL_RE.search(text))),
            ("raw-content canary", bool(CANARY_RE.search(text))),
            ("raw mail header block", len(HEADER_RE.findall(text)) >= 2),
            ("HTML source content", bool(HTML_RE.search(text))),
            ("quoted reply", bool(QUOTED_REPLY_RE.search(text))),
            ("long quoted passage", len(QUOTE_LINE_RE.findall(text)) >= 4),
            ("bearer token", bool(BEARER_RE.search(text))),
            (
                "credential material",
                bool(
                    STRUCTURED_URL_CREDENTIAL_RE.search(text)
                    or STRUCTURED_CREDENTIAL_VALUE_RE.search(text)
                ),
            ),
            (
                "recipient assignment",
                (
                    json_path.startswith("$.captures")
                    or json_path.rsplit(".", 1)[-1]
                    in {"remote_message_ref", "web_url"}
                )
                and bool(STRUCTURED_RECIPIENT_ASSIGNMENT_RE.search(text)),
            ),
            ("personal path", bool(PERSONAL_PATH_RE.search(text))),
        )
        for label, matched in rules:
            if not matched or _rule_allowed(relative, label):
                continue
            if allowed.get(coordinate) == (label, original):
                continue
            yield "{} at {}".format(label, json_path)


def _jsonl_findings(relative: Path, text: str, allowance: dict) -> Iterator[str]:
    """Each nonblank line parsed as its own JSON value, with line diagnostics."""

    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            value, ambiguous = _parsed_json(line)
        except json.JSONDecodeError:
            yield "line {}: invalid JSON".format(number)
            continue
        for finding in _structured_json_findings(
            relative, value, {} if ambiguous else allowance
        ):
            yield "line {}: {}".format(number, finding)


def _line_starts(text: str) -> list[int]:
    """The character index each line of the text begins at."""

    starts = [0]
    position = text.find("\n")
    while position != -1:
        starts.append(position + 1)
        position = text.find("\n", position + 1)
    return starts


def _char_offset(text: str, starts: list[int], lineno: int, column: int) -> int | None:
    """Turn one AST position into a character index into the original text.

    ``col_offset`` counts UTF-8 BYTES, not characters. Treating it as a character
    index would let a multibyte character earlier on the line shift or widen the
    approved span, so the byte prefix of that line is decoded back to characters.
    """

    if not 1 <= lineno <= len(starts):
        return None
    end = starts[lineno] if lineno < len(starts) else len(text)
    try:
        prefix = text[starts[lineno - 1]:end].encode("utf-8")[:column].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return starts[lineno - 1] + len(prefix)


def _owned_definition(
    scope: ast.AST, body: "list[ast.stmt]", name: str, kinds: tuple
) -> Any | None:
    """The one definition of that name this scope LEXICALLY OWNS.

    Ownership is direct membership of ``body``, never mere descent. A definition
    nested inside some other class or function below this scope is owned by that
    inner container, so the approved bytes cannot be carried under an unapproved
    owner - a Foreign class nested in the approved class, a local function, or the
    approved class itself relocated under another owner - and still be accepted.
    Uniqueness is still measured over the WHOLE subtree, so the previous duplicate
    and shadowed-definition refusal is preserved exactly.
    """

    everywhere = [
        node for node in ast.walk(scope) if isinstance(node, kinds) and node.name == name
    ]
    owned = [node for node in body if isinstance(node, kinds) and node.name == name]
    if len(everywhere) != 1 or len(owned) != 1:
        return None
    return owned[0]


def _method_extent(text: str, class_name: str, method_name: str) -> tuple[int, int] | None:
    """The character span of one uniquely located method inside valid parsed Python."""

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    # The class must be owned by the MODULE and the method by that class.
    owner = _owned_definition(tree, tree.body, class_name, (ast.ClassDef,))
    if owner is None:
        return None
    method = _owned_definition(
        owner, owner.body, method_name, (ast.FunctionDef, ast.AsyncFunctionDef)
    )
    if method is None or method.end_lineno is None or method.end_col_offset is None:
        return None
    starts = _line_starts(text)
    start = _char_offset(text, starts, method.lineno, method.col_offset)
    end = _char_offset(text, starts, method.end_lineno, method.end_col_offset)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _approved_span(relative: Path, text: str) -> tuple[str, int, int] | None:
    """The single rule occurrence one reviewed synthetic fixture is allowed to hold."""

    entry = APPROVED_SOURCE_FIXTURES.get(Path(relative.as_posix()))
    if entry is None:
        return None
    class_name, method_name, label, digest = entry
    extent = _method_extent(text, class_name, method_name)
    if extent is None:
        return None
    start, end = extent
    segment = text[start:end].replace("\r\n", "\n")
    if hashlib.sha256(segment.encode("utf-8")).hexdigest() != digest:
        return None
    matches = list(TEXT_RULES[label].finditer(text[start:end]))
    if len(matches) != 1:
        return None
    return label, start + matches[0].start(), start + matches[0].end()


def _text_rule_findings(
    relative: Path, text: str, allowed: tuple[str, int, int] | None
) -> Iterator[str]:
    """Every text rule matching the original complete text outside its one allowance."""

    for label, pattern in TEXT_RULES.items():
        if _rule_allowed(relative, label):
            continue
        for match in pattern.finditer(text):
            if allowed == (label, match.start(), match.end()):
                continue
            yield label
            break


def _structured_findings(
    relative: Path, text: str, suffix: str, allowance: dict
) -> Iterator[str]:
    """The JSON and JSON Lines findings for one already decoded text file."""

    if suffix == ".jsonl":
        yield from _jsonl_findings(relative, text, allowance)
        return
    if suffix != ".json":
        return
    try:
        value, ambiguous = _parsed_json(text)
    except json.JSONDecodeError:
        yield "invalid JSON"
        return
    yield from _structured_json_findings(
        relative, value, {} if ambiguous else allowance
    )


def audit(root: Path, denied: Iterable[str], mode: str = "auto") -> list[str]:
    findings: list[str] = []
    source_mode = _source_mode(root, mode)
    audited = list(files(root, mode))
    if not audited:
        return ["no auditable files found"]
    for path in audited:
        relative = path.relative_to(root)
        if path.is_symlink():
            findings.append("{}: symbolic link".format(relative))
            continue
        if relative == PRODUCT_LOCK:
            try:
                lock_stat = path.stat()
            except OSError:
                findings.append("{}: unreadable product lock".format(relative))
                continue
            if not stat.S_ISREG(lock_stat.st_mode):
                findings.append("{}: product lock is not a regular file".format(relative))
                continue
            if lock_stat.st_size == 0:
                continue
            if lock_stat.st_size != 1:
                findings.append("{}: product lock contains data".format(relative))
                continue
            try:
                with path.open("rb") as lock_file:
                    try:
                        lock_value = lock_file.read()
                    except OSError:
                        # Windows byte-range locks reject ordinary reads of the one-byte
                        # sentinel. A read-only mapping still lets the auditor verify it.
                        with mmap.mmap(lock_file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                            lock_value = mapped[:]
            except OSError:
                findings.append("{}: unreadable product lock".format(relative))
                continue
            if lock_value not in EMPTY_LOCK_VALUES:
                findings.append("{}: product lock contains data".format(relative))
            continue
        if source_mode and _approved_binary(relative, path):
            continue
        suffix = path.suffix.casefold()
        if suffix in BLOCKED_SUFFIXES:
            findings.append("{}: blocked suffix".format(relative))
            continue
        if suffix not in TEXT_SUFFIXES:
            findings.append("{}: unexpected file type".format(relative))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append("{}: non-UTF-8 content".format(relative))
            continue
        if _exact_frozen_fixture(relative, path):
            continue
        allowed = _approved_span(relative, text) if source_mode else None
        findings.extend(
            "{}: {}".format(relative, label)
            for label in _text_rule_findings(relative, text, allowed)
        )
        allowance = (
            APPROVED_STRUCTURED_VALUES.get(Path(relative.as_posix()), {})
            if source_mode else {}
        )
        for finding in _structured_findings(relative, text, suffix, allowance):
            findings.append("{}: {}".format(relative, finding))
        folded = text.casefold()
        for term in denied:
            if term.casefold() in folded:
                findings.append("{}: prohibited term".format(relative))
    return findings


def census(root: Path, mode: str = "auto") -> "tuple[int, int]":
    """How many files were scanned as text, and how many are the approved binary.

    The two are reported apart because only the first were decoded and scanned;
    the second were admitted by exact identity alone.
    """

    source_mode = _source_mode(root, mode)
    approved = 0
    scanned = 0
    for path in files(root, mode):
        if source_mode and _approved_binary(path.relative_to(root), path):
            approved += 1
        else:
            scanned += 1
    return scanned, approved


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("root", nargs="?", type=Path, default=Path("."))
    arguments.add_argument("--deny", action="append", default=[])
    arguments.add_argument(
        "--mode", choices=("auto", "source", "tree"), default="auto",
        help="source uses the product allow-list; tree scans an arbitrary export recursively",
    )
    options = arguments.parse_args()
    root = options.root.resolve()
    findings = audit(root, options.deny, options.mode)
    if findings:
        print("EXPORT AUDIT FAILED")
        for finding in findings:
            print("- " + finding)
        return 1
    scanned, approved = census(root, options.mode)
    selected_mode = "source" if _source_mode(root, options.mode) else "tree"
    print(
        "EXPORT AUDIT PASSED: {} UTF-8 text files and {} approved binary files"
        " ({} policy)".format(scanned, approved, selected_mode)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
