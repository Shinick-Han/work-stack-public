from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.check_theme_colors import check, scan, write_baseline


class ThemeColorPolicyTest(unittest.TestCase):
    def test_generated_theme_tokens_are_checked_out_with_canonical_lf(self) -> None:
        root = Path(__file__).resolve().parents[1]
        attributes = (root / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn(
            "frontend/src/generated/theme-tokens.css text eol=lf", attributes
        )
        self.assertIn(
            "desktop/python-webview-shell/generated/theme_tokens.py text eol=lf",
            attributes,
        )

    def test_baseline_allows_reduction_and_rejects_new_literals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "frontend" / "src" / "component.css"
            source.parent.mkdir(parents=True)
            source.write_text(".card { color: #fff; background: rgb(1 2 3); }\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                write_baseline(root, Path("quality/theme-color-baseline.json"))
                self.assertEqual(check(root, Path("quality/theme-color-baseline.json")), 0)

            source.write_text(".card { color: var(--ws-text); background: #123456; }\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(check(root, Path("quality/theme-color-baseline.json")), 1)

    def test_generated_and_test_sources_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "frontend" / "src" / "generated" / "theme-tokens.css"
            generated.parent.mkdir(parents=True)
            generated.write_text(":root { --ws-bg: #010203; }\n", encoding="utf-8")
            test_source = root / "frontend" / "src" / "component.test.tsx"
            test_source.write_text("const color = '#fff'\n", encoding="utf-8")

            self.assertEqual(scan(root), {})

    def test_baseline_has_a_stable_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "quality" / "theme-color-baseline.json"
            baseline.parent.mkdir(parents=True)
            baseline.write_text(json.dumps({"schema_version": 1, "files": {}}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check(root, Path("quality/theme-color-baseline.json")), 0)
