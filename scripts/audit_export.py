#!/usr/bin/env python3
"""Audit Work Stack source exports and runtime data for sensitive material."""

from __future__ import annotations

import argparse
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
    "workstack", "frontend/src", "contracts", "data", "docs", "licenses", "scripts", "tests", "web",
)
SOURCE_FILES = (
    ".gitignore", "README.md", "SECURITY.md", "run_work_stack.py",
    "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
    "frontend/THIRD_PARTY_NOTICES.md", "frontend/tsconfig.json",
    "frontend/tsconfig.app.json", "frontend/tsconfig.node.json", "frontend/vite.config.ts",
)
ROOT_SOURCE_SUFFIXES = {".json", ".md", ".py", ".ps1", ".sh", ".toml", ".txt", ".yaml", ".yml"}
TEXT_SUFFIXES = {
    "", ".css", ".html", ".js", ".json", ".md", ".mjs", ".py", ".ps1", ".sha256",
    ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
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
FROZEN_FIXTURE_HASHES = {
    Path("contracts/workstack-conduit-v1/safety/snapshot-v1-safety-cases.json"):
        "480a16ccb18338417c718aca6e7729037431a201a464b6298e0819a0c349f92f",
}
PRODUCT_LOCK = Path(".workstack.lock")
EMPTY_LOCK_VALUES = {b"", b"\0"}
VALIDATION_PERCENT_DECODE_ROUNDS = 5


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


def files(root: Path, mode: str = "auto") -> Iterator[Path]:
    """Yield the explicit source-release set or every file in a data export."""

    source_mode = mode == "source" or (mode == "auto" and _looks_like_source_repo(root))
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


def _json_strings(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = "{}.{}".format(path, key)
            if isinstance(key, str) and key.casefold().replace("-", "_") in CREDENTIAL_KEYS:
                if child not in (None, "", False):
                    yield child_path, "__CREDENTIAL_VALUE__"
            yield from _json_strings(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_strings(child, "{}[{}]".format(path, index))
    elif isinstance(value, str):
        yield path, value


def _negative_rule_allowed(relative: Path, label: str) -> bool:
    normalized = Path(relative.as_posix())
    return (
        label in NEGATIVE_RULES
        and normalized in NEGATIVE_FIXTURES
    ) or (
        label in NEGATIVE_TEST_RULES.get(normalized, set())
    )


def _exact_frozen_fixture(relative: Path, path: Path) -> bool:
    expected = FROZEN_FIXTURE_HASHES.get(Path(relative.as_posix()))
    return expected is not None and hashlib.sha256(path.read_bytes()).hexdigest() == expected


def _structured_json_findings(relative: Path, value: Any) -> Iterator[str]:
    for json_path, original in _json_strings(value):
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
            if matched and not _negative_rule_allowed(relative, label):
                yield "{} at {}".format(label, json_path)


def audit(root: Path, denied: Iterable[str], mode: str = "auto") -> list[str]:
    findings: list[str] = []
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
        for label, pattern in TEXT_RULES.items():
            if pattern.search(text) and not _negative_rule_allowed(relative, label):
                findings.append("{}: {}".format(relative, label))
        if suffix == ".json":
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                findings.append("{}: invalid JSON".format(relative))
            else:
                for finding in _structured_json_findings(relative, value):
                    findings.append("{}: {}".format(relative, finding))
        folded = text.casefold()
        for term in denied:
            if term.casefold() in folded:
                findings.append("{}: prohibited term".format(relative))
    return findings


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
    audited_count = sum(1 for _ in files(root, options.mode))
    if findings:
        print("EXPORT AUDIT FAILED")
        for finding in findings:
            print("- " + finding)
        return 1
    selected_mode = "source" if options.mode == "source" or (
        options.mode == "auto" and _looks_like_source_repo(root)
    ) else "tree"
    print("EXPORT AUDIT PASSED: {} UTF-8 text files ({} policy)".format(audited_count, selected_mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
