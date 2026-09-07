#!/usr/bin/env python3
"""Local JSON CLI for read-only remote Linux provision inspect/plan.

Reads at most MAX_DOCUMENT_BYTES+1 bytes of JSON from stdin and writes a
deterministic plan to stdout. This process never SSHes, never installs, and
never writes the filesystem.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

from remote_provision_plan import (  # noqa: E402
    MAX_DOCUMENT_BYTES,
    PlanError,
    encode_plan,
    plan_remote_provision,
)


def _write_error(error: PlanError) -> int:
    payload = json.dumps(
        {"code": error.code, "detail": error.detail},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    sys.stderr.buffer.write(payload + b"\n")
    return 2


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
    try:
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise PlanError("INVALID_DOCUMENT", "request exceeds the inspect/plan bound")
        document = plan_remote_provision(raw)
    except PlanError as error:
        return _write_error(error)
    except (RecursionError, UnicodeError, ValueError):
        return _write_error(PlanError("INVALID_DOCUMENT", "request is not a bounded JSON object"))
    sys.stdout.buffer.write(encode_plan(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
