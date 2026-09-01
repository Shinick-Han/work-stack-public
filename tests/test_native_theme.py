from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
sys.path.insert(0, str(SHELL))

from native_theme import (
    MAX_THEME_STATE_BYTES,
    THEME_STATE_FILE,
    load_persisted_theme,
    normalize_theme,
    persist_theme,
    theme_color,
    theme_colorref,
    theme_rgb,
)


class NativeThemeTest(unittest.TestCase):
    def test_normalizes_unknown_themes_to_dark(self) -> None:
        self.assertEqual(normalize_theme("light"), "light")
        self.assertEqual(normalize_theme("system"), "dark")

    def test_exposes_complete_native_palettes(self) -> None:
        required = (
            "native.caption",
            "native.text",
            "native.border",
            "native.overlay",
            "native.toolbar",
            "native.externalLoading",
            "brand.accent",
            "brand.ink",
        )
        for theme in ("dark", "light"):
            for token in required:
                self.assertRegex(theme_color(theme, token), r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")

    def test_converts_theme_colors_for_winforms(self) -> None:
        dark = theme_rgb("dark", "native.overlay")
        light = theme_rgb("light", "native.overlay")
        self.assertEqual(len(dark), 3)
        self.assertEqual(len(light), 3)
        self.assertNotEqual(dark, light)
        red, green, blue = dark
        self.assertEqual(theme_colorref("dark", "native.overlay"), red | green << 8 | blue << 16)

    def test_persisted_theme_round_trips_as_bounded_exact_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            self.assertEqual(load_persisted_theme(state_root), "dark")
            self.assertEqual(persist_theme(state_root, "light"), "light")
            self.assertEqual(load_persisted_theme(state_root), "light")
            encoded = (state_root / THEME_STATE_FILE).read_bytes()
            self.assertLessEqual(len(encoded), MAX_THEME_STATE_BYTES)
            self.assertEqual(encoded, b'{"schema_version":1,"theme":"light"}\n')
            self.assertEqual(list(state_root.glob(f".{THEME_STATE_FILE}.*.tmp")), [])

    def test_invalid_theme_documents_fail_safely_to_dark(self) -> None:
        invalid_documents = (
            b"",
            b"not-json",
            b'{"schema_version":1,"theme":"system"}',
            b'{"schema_version":true,"theme":"light"}',
            b'{"schema_version":1,"theme":"light","unknown":1}',
            b'{"schema_version":1,"schema_version":1,"theme":"light"}',
            b'{"schema_version":NaN,"theme":"light"}',
            b"\xef\xbb\xbf" + b'{"schema_version":1,"theme":"light"}',
            b" " * (MAX_THEME_STATE_BYTES + 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            path = state_root / THEME_STATE_FILE
            for encoded in invalid_documents:
                with self.subTest(encoded=encoded[:40]):
                    path.write_bytes(encoded)
                    self.assertEqual(load_persisted_theme(state_root), "dark")
