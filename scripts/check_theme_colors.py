#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_BASELINE = Path("quality/theme-color-baseline.json")
COLOR_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b"
    r"|\brgba?\([^)]*\)"
    r"|\bhsla?\([^)]*\)"
    r"|\bColor\.FromArgb\([^)]*\)",
)
SOURCE_ROOTS = (
    Path("frontend/src"),
    Path("desktop/python-webview-shell"),
)
SOURCE_SUFFIXES = {".css", ".py", ".ts", ".tsx"}


def _excluded(path: Path) -> bool:
    normalized = path.as_posix()
    return (
        "/generated/" in f"/{normalized}/"
        or ".test." in path.name
        or ".spec." in path.name
        or path.name.endswith("_test.py")
        or path.name.startswith("test_")
    )


def scan(root: Path) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for source_root in SOURCE_ROOTS:
        absolute_root = root / source_root
        if not absolute_root.is_dir():
            continue
        for path in sorted(absolute_root.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if _excluded(relative):
                continue
            matches = (match.group(0).lower() for match in COLOR_RE.finditer(path.read_text(encoding="utf-8")))
            values = Counter(value for value in matches if not (value.startswith("color.fromargb") and "theme_rgb" in value))
            if values:
                found[relative.as_posix()] = dict(sorted(values.items()))
    return found


def _payload(files: dict[str, dict[str, int]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": "Existing literals may decrease but new literal values or increased use fail the gate.",
        "files": files,
    }


def write_baseline(root: Path, baseline: Path) -> None:
    target = root / baseline
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_payload(scan(root)), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote theme color baseline: {target}")


def check(root: Path, baseline: Path) -> int:
    target = root / baseline
    try:
        expected_payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"theme color baseline is invalid: {error}", file=sys.stderr)
        return 2
    if expected_payload.get("schema_version") != SCHEMA_VERSION or not isinstance(expected_payload.get("files"), dict):
        print("theme color baseline must use schema_version 1 and contain a files object", file=sys.stderr)
        return 2

    expected: dict[str, dict[str, int]] = expected_payload["files"]
    actual = scan(root)
    errors: list[str] = []
    for path, values in sorted(actual.items()):
        allowed = expected.get(path, {})
        for value, count in sorted(values.items()):
            limit = allowed.get(value, 0)
            if count > limit:
                errors.append(f"{path}: {value} appears {count} time(s), baseline allows {limit}")
    if errors:
        print("theme color literal policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("Use semantic theme tokens. Rewrite the baseline only as part of an explicitly reviewed migration.", file=sys.stderr)
        return 1

    current_total = sum(sum(values.values()) for values in actual.values())
    baseline_total = sum(sum(values.values()) for values in expected.values())
    print(f"theme color literal policy passed: {current_total} occurrence(s), baseline {baseline_total}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prevent direct product color literals from increasing.")
    parser.add_argument("command", choices=("check", "write-baseline"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    if arguments.command == "write-baseline":
        write_baseline(root, arguments.baseline)
        return 0
    return check(root, arguments.baseline)


if __name__ == "__main__":
    raise SystemExit(main())
