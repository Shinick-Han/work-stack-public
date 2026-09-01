from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SSH_MODULES = (
    "tests.test_ssot_connection_profile",
    "tests.test_desktop_ssh_remote",
    "tests.test_desktop_remote_resilience_integration",
)


def run_unit_matrix() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromNames(SSH_MODULES)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def run_wsl_canary(distro: str) -> int:
    script = ROOT / "scripts" / "windows" / "test_workstack_wsl_ssh_canary.py"
    result = subprocess.run(
        [sys.executable, str(script), "--distro", distro],
        cwd=ROOT,
        check=False,
        timeout=600,
    )
    return int(result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Work Stack SSH regression gates")
    parser.add_argument(
        "--wsl-distro",
        help="also run the real isolated WSL/OpenSSH canary against this distribution",
    )
    arguments = parser.parse_args(argv)
    if not run_unit_matrix():
        return 1
    if arguments.wsl_distro:
        return run_wsl_canary(arguments.wsl_distro)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
