"""The source-checkout verifier entry as a real child from a foreign cwd.

This is not an import test. Every run below executes the absolute
``integrations/opendocuments/source_verifier_entry.py`` with an explicit
interpreter, an explicit foreign working directory, and an explicit environment
that does not contain ``PYTHONPATH``. That cwd holds shadow ``workstack`` and
``integrations`` packages that raise if imported. Protocol A must be present.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from test_od_source_verifier_main import (
    CONFIG_ENVIRONMENT_VARIABLE,
    VerifierProcessCase,
)

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "integrations" / "opendocuments" / "source_verifier_entry.py"

SHADOW_INIT = "raise ImportError('shadow package imported from cwd')\n"


class SourceVerifierCheckoutEntryTest(VerifierProcessCase):
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
        (nested / "source_verifier_main.py").write_text(SHADOW_INIT, encoding="utf-8")
        (nested / "source_verifier_entry.py").write_text(SHADOW_INIT, encoding="utf-8")

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

    def test_matching_bytes_from_foreign_cwd_do_not_import_shadows(self) -> None:
        document = self.request_document()
        result = self.admitted_result(self.run_request(document), document)
        self.assertEqual(result["evidence"][0]["status"], "current")

    def test_invalid_input_stays_closed_from_foreign_cwd(self) -> None:
        config = self.write_config()
        self.assertRefused(self.run_child(b"{not json", config=config))
        self.assertRefused(self.run_child(b"", config=config))


if __name__ == "__main__":  # pragma: no cover - direct invocation
    import unittest

    unittest.main()
