"""The source-checkout entry script as a real child from a foreign cwd.

This is not an import test. Every run below executes the absolute
``integrations/opendocuments/driver_entry.py`` with an explicit interpreter,
an explicit foreign working directory, and an explicit environment that does
not contain ``PYTHONPATH``. That cwd holds shadow ``workstack`` and
``integrations`` packages that raise if imported. Configuration, key and
loopback backend are the synthetic fixtures already used by
``test_opendocuments_driver_main``. No live provider, credential store or
network beyond 127.0.0.1 is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from test_opendocuments_driver_main import (
    CONFIG_ENVIRONMENT_VARIABLE,
    DriverProcessCase,
    fake_backend,
)

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "integrations" / "opendocuments" / "driver_entry.py"

SHADOW_INIT = "raise ImportError('shadow package imported from cwd')\n"


def _origin(server) -> str:
    return "http://127.0.0.1:{}".format(server.server_address[1])


class SourceCheckoutEntryTest(DriverProcessCase):
    """Absolute entry script, foreign cwd, no PYTHONPATH."""

    def setUp(self) -> None:
        super().setUp()
        self.foreign = self.root / "foreign-cwd"
        self.foreign.mkdir()
        self._plant_shadow(self.foreign)

    def _plant_shadow(self, foreign: Path) -> None:
        workstack = foreign / "workstack"
        workstack.mkdir()
        (workstack / "__init__.py").write_text(SHADOW_INIT, encoding="utf-8")
        nested = foreign / "integrations" / "opendocuments"
        nested.mkdir(parents=True)
        (foreign / "integrations" / "__init__.py").write_text(
            SHADOW_INIT, encoding="utf-8"
        )
        (nested / "__init__.py").write_text(SHADOW_INIT, encoding="utf-8")
        (nested / "driver_main.py").write_text(SHADOW_INIT, encoding="utf-8")
        (nested / "driver_entry.py").write_text(SHADOW_INIT, encoding="utf-8")

    def child_environment(self, config: Path | None) -> dict[str, str]:
        environment: dict[str, str] = {}
        if config is not None:
            environment[CONFIG_ENVIRONMENT_VARIABLE] = str(config)
        for inherited in ("SystemRoot", "PATH"):
            if inherited in os.environ:
                environment[inherited] = os.environ[inherited]
        return environment

    def run_child(
        self, payload: bytes, *, config: Path | None = None, argv: list | None = None
    ) -> subprocess.CompletedProcess:
        environment = self.child_environment(config)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertTrue(ENTRY.is_file())
        return subprocess.run(
            [sys.executable, str(ENTRY)] + list(argv or []),
            input=payload,
            capture_output=True,
            cwd=str(self.foreign),
            env=environment,
            timeout=90,
        )

    def test_issued_shape_from_foreign_cwd_posts_once(self) -> None:
        with fake_backend() as backend:
            config = self.write_config(_origin(backend))
            completed = self.run_envelope(self.envelope(), config)
            proposal = self.proposal(completed)
            self.assertEqual(len(backend.posts), 1)
            self.assertEqual(backend.posts[0]["path"], "/api/v1/chat")
        self.assertEqual(len(proposal["items"]), 1)
        self.assertNoDisclosure(completed)

    def test_invalid_input_and_key_stay_closed_with_no_post(self) -> None:
        with fake_backend() as backend:
            config = self.write_config(_origin(backend))
            extra = self.envelope()
            extra["unexpected"] = "field"
            cases = {
                "empty": b"",
                "not json": b"{not json at all",
                "extra field": json.dumps(extra).encode("utf-8"),
            }
            for name, payload in cases.items():
                with self.subTest(case=name):
                    self.assertRefused(self.run_child(payload, config=config))
            self.key_file.unlink()
            self.assertRefused(self.run_envelope(self.envelope(), config))
            self.assertEqual(backend.posts, [])


if __name__ == "__main__":  # pragma: no cover - direct invocation
    import unittest

    unittest.main()
