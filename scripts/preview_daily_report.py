#!/usr/bin/env python3
"""Emit one daily-v1 preview JSON from a review projection on stdin.

No Store, network, or file writes. Invalid input exits nonzero with one
generic stderr line and no traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from workstack.reporting import (  # noqa: E402
    MAX_PROJECTION_BYTES,
    DailyReportPreviewError,
    preview_daily_report,
)

_ERROR = "preview input is invalid"


def _refuse_constant(_name: str) -> object:
    raise ValueError("non-finite JSON constant")


def _reject() -> int:
    sys.stderr.write(_ERROR + "\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--generated-at", required=True)
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_PROJECTION_BYTES + 1)
    if len(raw) > MAX_PROJECTION_BYTES:
        return _reject()
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_refuse_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return _reject()
    if type(payload) is not dict:
        return _reject()
    try:
        preview = preview_daily_report(
            projection=payload,
            date=args.date,
            template=args.template,
            generated_at=args.generated_at,
        )
    except DailyReportPreviewError:
        return _reject()
    sys.stdout.buffer.write(json.dumps(preview, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
