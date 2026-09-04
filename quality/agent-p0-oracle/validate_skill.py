#!/usr/bin/env python3
"""Repository-owned quick validator for the canonical Work Stack Agent Skill.

This is the pinned Skill validator identity referenced by the O1 directive; validation never
depends on an unpinned user-profile validator. It checks the canonical skill tree only:

- required files exist and every file is valid UTF-8;
- SKILL.md progressively discloses both reference documents;
- no forbidden command surface (Task completion/delete/rebind, sync adoption, send, Git mutation);
- no instruction to edit JSON/NDJSON/SQLite/SSOT files directly;
- no secret/environment/credential collection;
- unknown commit state (commit_unknown) is paired with an explicit stop instruction;
- no embedded user paths or credential-looking literals;
- no executable Skill scripts (P0 Skill ships documentation only).

The validator is deterministic and bounded: no network, no clock, no profile state. Pass/fail
comes from the process exit code plus one canonical JSON report.

Usage:
    python -I validate_skill.py <skill-dir> [--report <report.json>]

Exit codes: 0 valid, 2 invalid, 3 usage or IO error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Pattern

REQUIRED_FILES = ("SKILL.md", "references/commands.md", "references/journal-policy.md")
ALLOWED_FILES = frozenset(REQUIRED_FILES)
SCRIPT_SUFFIXES = (".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".mjs")

VIOLATION_PREFIX = "P0-SKILL"

# (rule id, compiled pattern, human explanation)
FORBIDDEN_SURFACE_RULES: tuple[tuple[str, Pattern[str], str], ...] = (
    ("FORBIDDEN-TASK-MUTATION", re.compile(r"\btasks?\s+(complete|delete|drop|patch|create|rebind)\b", re.IGNORECASE), "Task completion/delete/rebind is unavailable in P0"),
    ("FORBIDDEN-SYNC-ADOPTION", re.compile(r"\b(sync|migration)\s+(adopt|rebind|restore)\b", re.IGNORECASE), "sync adoption/rebind/restore is unavailable in P0"),
    ("FORBIDDEN-SEND", re.compile(r"\bsend\s+(reply|email|message)\b", re.IGNORECASE), "sending external messages is unavailable in P0"),
    ("FORBIDDEN-GIT-MUTATION", re.compile(r"\bgit\s+(push|merge|rebase|reset)\b", re.IGNORECASE), "Git mutation commands must not appear in the Skill"),
    ("FORBIDDEN-AGENT-BIND", re.compile(r"\bagent\s+bind\b", re.IGNORECASE), "agent bind is not part of the P0 Skill"),
)

DIRECT_SSOT_RULES: tuple[tuple[str, Pattern[str], str], ...] = (
    ("DIRECT-SSOT-EDIT", re.compile(r"\b(edit|write|modify|update|overwrite|rewrite)[^.\n]{0,60}\.(json|ndjson|sqlite|db)\b", re.IGNORECASE), "direct authority file edits are prohibited"),
    ("DIRECT-SQL", re.compile(r"\b(insert\s+into|update\s+\w+\s+set|delete\s+from)\b", re.IGNORECASE), "raw SQL mutation instructions are prohibited"),
    ("SQLITE-CLI", re.compile(r"\bsqlite3?\b", re.IGNORECASE), "SQLite tooling must not be invoked by the Skill"),
)

SECRET_RULES: tuple[tuple[str, Pattern[str], str], ...] = (
    ("SECRET-ENV-COLLECTION", re.compile(r"\b(dump|collect|paste|include|print)[^.\n]{0,40}(environment|env\b|credential|token|secret)", re.IGNORECASE), "environment/credential collection must not be requested"),
    ("DOTENV-REFERENCE", re.compile(r"(?<![A-Za-z0-9_])\.env(?![A-Za-z0-9_])"), ".env access must not be instructed"),
    ("CREDENTIAL-LITERAL", re.compile(r"\b(api[_-]?key|password|secret[_-]?key)\s*[:=]", re.IGNORECASE), "credential-shaped literals must not appear"),
    ("TOKEN-SHAPE", re.compile(r"\b(ghp_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})\b"), "credential token shapes must not appear"),
)

USER_PATH_RULES: tuple[tuple[str, Pattern[str], str], ...] = (
    ("USER-PATH-WINDOWS", re.compile(r"[A-Za-z]:\\Users\\"), "absolute user paths must not be embedded"),
    ("USER-PATH-POSIX", re.compile(r"/(home|Users)/[A-Za-z0-9_]+"), "absolute user paths must not be embedded"),
)

CONTINUE_AFTER_UNKNOWN = re.compile(r"(retry|continue|proceed)[^.\n]{0,60}commit_unknown|commit_unknown[^.\n]{0,60}(retry|continue|proceed)", re.IGNORECASE)


class SkillValidatorError(RuntimeError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _violations_for_text(rule_table: tuple[tuple[str, Pattern[str], str], ...], file_name: str, text: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for rule_id, pattern, detail in rule_table:
        match = pattern.search(text)
        if match:
            found.append({"id": f"{VIOLATION_PREFIX}-{rule_id}", "file": file_name, "detail": detail})
    return found


def validate_skill(skill_dir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"skill": skill_dir.name, "valid": False, "violations": []}
    violations: list[dict[str, str]] = report["violations"]

    if not skill_dir.is_dir():
        violations.append({"id": f"{VIOLATION_PREFIX}-MISSING-TREE", "file": str(skill_dir), "detail": "skill directory does not exist"})
        return report

    present: set[str] = set()
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(skill_dir).as_posix()
        present.add(relative)
        if relative not in ALLOWED_FILES:
            violations.append({"id": f"{VIOLATION_PREFIX}-UNEXPECTED-FILE", "file": relative, "detail": "file is not part of the canonical P0 skill tree"})
        if path.suffix.lower() in SCRIPT_SUFFIXES:
            violations.append({"id": f"{VIOLATION_PREFIX}-SCRIPT-PRESENT", "file": relative, "detail": "P0 Skill ships documentation only, not scripts"})

    for required in REQUIRED_FILES:
        if required not in present:
            violations.append({"id": f"{VIOLATION_PREFIX}-MISSING-FILE", "file": required, "detail": "required skill file is absent"})
            continue
        raw = (skill_dir / required).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            violations.append({"id": f"{VIOLATION_PREFIX}-NOT-UTF8", "file": required, "detail": "skill files must be valid UTF-8"})
            continue

        if required == "SKILL.md":
            for reference in REQUIRED_FILES[1:]:
                if reference not in text:
                    violations.append(
                        {
                            "id": f"{VIOLATION_PREFIX}-DISCLOSURE-GAP",
                            "file": required,
                            "detail": f"SKILL.md must progressively disclose {reference}",
                        }
                    )
        violations.extend(_violations_for_text(FORBIDDEN_SURFACE_RULES, required, text))
        violations.extend(_violations_for_text(DIRECT_SSOT_RULES, required, text))
        violations.extend(_violations_for_text(SECRET_RULES, required, text))
        violations.extend(_violations_for_text(USER_PATH_RULES, required, text))

        if "commit_unknown" in text:
            has_stop = any("commit_unknown" in line and "stop" in line.lower() for line in text.splitlines())
            if not has_stop:
                violations.append(
                    {
                        "id": f"{VIOLATION_PREFIX}-UNKNOWN-NOT-STOPPED",
                        "file": required,
                        "detail": "commit_unknown guidance must tell the agent to stop and retain the intent ID",
                    }
                )
            if CONTINUE_AFTER_UNKNOWN.search(text):
                violations.append(
                    {
                        "id": f"{VIOLATION_PREFIX}-CONTINUE-AFTER-UNKNOWN",
                        "file": required,
                        "detail": "the Skill must not instruct continuing or retrying after commit_unknown",
                    }
                )

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in violations:
        key = (item["id"], item["file"], item["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    report["violations"] = unique
    report["valid"] = not unique
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the canonical Work Stack Agent Skill tree.")
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("--report", type=Path, default=None)
    arguments = parser.parse_args(argv)

    try:
        report = validate_skill(arguments.skill_dir)
    except OSError as error:
        print(f"validate_skill: IO error: {error}", file=sys.stderr)
        return 3

    payload = canonical_json_bytes(report)
    if arguments.report is not None:
        report_path = Path(arguments.report)
        if report_path.parent != Path(""):
            report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_name(report_path.name + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(report_path)
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()

    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
