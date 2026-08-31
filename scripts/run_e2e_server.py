#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from workstack.server import create_server  # noqa: E402
from workstack.service import WorkStack  # noqa: E402
from workstack.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Work Stack with a disposable demo store.")
    parser.add_argument("--port", type=int, default=8781)
    arguments = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="workstack-e2e-") as directory:
        stack = WorkStack(Store(Path(directory)))
        stack.store.seed_demo(PROJECT_ROOT / "data")
        server = create_server(stack, "127.0.0.1", arguments.port)
        print("work-stack e2e: http://127.0.0.1:{}/".format(server.actual_port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
