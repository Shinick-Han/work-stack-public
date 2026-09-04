"""The desktop surfaces consume the packaged mark, not a glyph or a stale icon.

Nothing here launches the desktop shell, the installer or a real window: the
HTML builders are pure functions and the native icon path is exercised through
a small stand-in that records the Win32 calls and proves the icon handle is
released.
"""

from __future__ import annotations

import builtins
import ctypes
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from ctypes import wintypes
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import brand_assets as BRAND  # noqa: E402

RECOVERY_SPEC = importlib.util.spec_from_file_location(
    "startup_recovery_host_brand_test", SHELL / "startup_recovery_host.py"
)
assert RECOVERY_SPEC is not None and RECOVERY_SPEC.loader is not None
RECOVERY = importlib.util.module_from_spec(RECOVERY_SPEC)
sys.modules[RECOVERY_SPEC.name] = RECOVERY
RECOVERY_SPEC.loader.exec_module(RECOVERY)

DESKTOP_SPEC = importlib.util.spec_from_file_location(
    "workstack_desktop_brand_test", SHELL / "workstack_desktop.py"
)
assert DESKTOP_SPEC is not None and DESKTOP_SPEC.loader is not None
DESKTOP = importlib.util.module_from_spec(DESKTOP_SPEC)
sys.modules[DESKTOP_SPEC.name] = DESKTOP
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    DESKTOP_SPEC.loader.exec_module(DESKTOP)

THEMES = ("dark", "light")

# A handle wider than 32 bits: the default ctypes c_int return would
# sign-extend this to 0xFFFFFFFFABCDEF00.
WIDE_HANDLE = 0x0000_0001_ABCD_EF00


class _Stub:
    """A callable that also carries argtypes/restype like a ctypes function."""

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        return self._behaviour(*arguments)


class FakeUser32:
    """Callback-only stand-in. No window, no real Win32 object."""

    def __init__(self, draw_result: int = 1) -> None:
        self.loaded: list[tuple] = []
        self.drawn: list[tuple] = []
        self.destroyed: list[int] = []
        self._draw_result = draw_result
        self.LoadImageW = _Stub(self._load)
        self.DrawIconEx = _Stub(self._draw)
        self.DestroyIcon = _Stub(self._destroy)

    def _load(self, instance, name, kind, width, height, flags):
        self.loaded.append((name, kind, width, height, flags))
        return WIDE_HANDLE

    def _draw(self, hdc, left, top, handle, width, height, step, brush, flags):
        self.drawn.append((hdc, left, top, handle, width, height, flags))
        return self._draw_result

    def _destroy(self, handle):
        self.destroyed.append(handle)
        return 1


class PackagedAssetResolutionTest(unittest.TestCase):
    def test_asset_paths_are_package_relative_and_not_cwd_dependent(self) -> None:
        for path in (BRAND.mark_svg_path(), BRAND.mark_ico_path(), BRAND.GEOMETRY_PATH):
            self.assertTrue(path.is_absolute())
            self.assertEqual(path.parent, SHELL / "assets")
            self.assertTrue(path.is_file(), f"packaged asset is missing: {path.name}")

    def test_reading_the_mark_returns_a_bare_svg_element(self) -> None:
        markup = BRAND.read_mark_svg()
        self.assertTrue(markup.startswith("<svg"))
        self.assertTrue(markup.endswith("</svg>"))
        self.assertIn("#B8F24B", markup)
        self.assertIn("#12150D", markup)

    def test_the_reader_never_pulls_more_than_the_limit_plus_one(self) -> None:
        """A size check after an unbounded read has already allocated the file."""

        oversized = Path(tempfile.mkdtemp()) / "oversized-mark.svg"
        oversized.write_bytes(b"<svg>" + b"x" * 1_200_000 + b"</svg>")
        sizes: list[int] = []
        real_open = builtins.open

        def watching_open(file, mode="r", *arguments, **keywords):
            handle = real_open(file, mode, *arguments, **keywords)
            if Path(os.fspath(file)) != oversized:
                return handle
            inner_read = handle.read

            def read(size=-1):
                sizes.append(size)
                return inner_read(size)

            handle.read = read  # type: ignore[method-assign]
            return handle

        with mock.patch.object(BRAND, "MARK_SVG_PATH", oversized):
            with mock.patch.object(builtins, "open", watching_open):
                with self.assertRaises(BRAND.BrandAssetMissing):
                    BRAND.read_mark_svg()

        self.assertEqual(sizes, [BRAND.MAX_MARK_BYTES + 1])
        self.assertNotIn(-1, sizes, "the reader must not issue an unbounded read")

    def test_invalid_utf8_and_oversized_files_fall_back_to_the_empty_mark(self) -> None:
        root = Path(tempfile.mkdtemp())
        invalid = root / "invalid.svg"
        invalid.write_bytes(b"\xff\xfe<svg></svg>")
        oversized = root / "big.svg"
        oversized.write_bytes(b"<svg>" + b"x" * 1_200_000 + b"</svg>")
        before = {path: path.read_bytes() for path in (invalid, oversized)}

        for path in (invalid, oversized, root / "absent.svg"):
            with mock.patch.object(BRAND, "MARK_SVG_PATH", path):
                with self.assertRaises(BRAND.BrandAssetMissing):
                    BRAND.read_mark_svg()
                self.assertEqual(
                    BRAND.inline_mark_markup(), '<div class="mark" aria-hidden="true"></div>'
                )

        # No user file was rewritten or repaired on any failure path.
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload)
    def test_a_missing_asset_is_reported_rather_than_substituted(self) -> None:
        with mock.patch.object(BRAND, "MARK_SVG_PATH", SHELL / "assets" / "absent.svg"):
            with self.assertRaises(BRAND.BrandAssetMissing):
                BRAND.read_mark_svg()
            fallback = BRAND.inline_mark_markup()
        # Bounded, explicit and empty: no stale glyph and no yellow drawing.
        self.assertEqual(fallback, '<div class="mark" aria-hidden="true"></div>')
        self.assertNotIn("|||", fallback)


class StartupHtmlBrandTest(unittest.TestCase):
    def test_both_themes_inline_the_packaged_mark_instead_of_the_glyph(self) -> None:
        markup = BRAND.read_mark_svg()
        for theme in THEMES:
            html = DESKTOP.build_startup_html(theme)
            self.assertIn(markup, html)
            self.assertNotIn("|||", html)
            self.assertNotIn("__WS_MARK__", html)

    def test_theme_tokens_and_surrounding_colors_are_still_substituted(self) -> None:
        for theme in THEMES:
            html = DESKTOP.build_startup_html(theme)
            self.assertNotIn("__WS_", html)
            self.assertIn(DESKTOP.theme_color(theme, "bg.app"), html)
            self.assertIn(DESKTOP.theme_color(theme, "text.primary"), html)
            self.assertIn("Preparing your workspace", html)


class RecoveryHostBrandTest(unittest.TestCase):
    STATUS = {
        "activation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "current_registry_digest": "sha256:" + "a" * 64,
    }

    def _render(self, theme: str, outcome: str = "ready") -> str:
        return RECOVERY.build_startup_recovery_html(
            dict(self.STATUS), outcome=outcome, theme=theme
        )

    def test_recovery_html_uses_the_same_mark_and_keeps_its_actions(self) -> None:
        markup = BRAND.read_mark_svg()
        for theme in THEMES:
            html = self._render(theme)
            self.assertIn(markup, html)
            self.assertNotIn("|||", html)
            self.assertIn('aria-hidden="true"', html)
            self.assertIn('id="restore"', html)
            self.assertIn(RECOVERY.theme_color(theme, "bg.app"), html)

    def test_the_refused_outcome_keeps_its_copy_and_drops_only_the_restore_action(self) -> None:
        ready = self._render("dark", "ready")
        refused = self._render("dark", "refused")
        self.assertIn('id="restore"', ready)
        self.assertNotIn('id="restore"', refused)
        self.assertIn(BRAND.read_mark_svg(), refused)
        self.assertIn("Connection could not be restored", refused)

    def test_a_non_recoverable_status_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RECOVERY.build_startup_recovery_html({"activation_id": "nope"})


class NativeIconTest(unittest.TestCase):
    """The window icon comes from the packaged ICO, never the install root."""

    def test_apply_native_brand_loads_the_packaged_icon(self) -> None:
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.form = types.SimpleNamespace(Text="", Icon=None)
        host.install_root = Path("C:/does-not-exist-install-root")
        host.native_icon = None
        traced: list[str] = []
        host._trace = traced.append
        applied: list[object] = []
        host._set_native_window_icon = applied.append
        loaded: list[str] = []

        class Icon:
            def __init__(self, path: str) -> None:
                loaded.append(path)

        with mock.patch.dict(sys.modules, {
            "System": types.SimpleNamespace(),
            "System.Drawing": types.SimpleNamespace(Icon=Icon),
        }):
            host._apply_native_brand()

        self.assertEqual(loaded, [str(BRAND.mark_ico_path())])
        self.assertEqual(len(applied), 1)
        self.assertEqual(host.form.Text, DESKTOP.NATIVE_WINDOW_TITLE)
        self.assertEqual(traced, [])
        self.assertNotIn("WorkStack.ico", "".join(loaded))

    def test_a_missing_packaged_icon_traces_and_sets_no_icon(self) -> None:
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.form = types.SimpleNamespace(Text="", Icon=None)
        host.install_root = Path("C:/does-not-exist-install-root")
        host.native_icon = None
        traced: list[str] = []
        host._trace = traced.append
        applied: list[object] = []
        host._set_native_window_icon = applied.append

        with mock.patch.object(DESKTOP, "has_mark_ico", lambda: False):
            with mock.patch.dict(sys.modules, {
                "System": types.SimpleNamespace(),
                "System.Drawing": types.SimpleNamespace(Icon=object),
            }):
                host._apply_native_brand()

        self.assertEqual(applied, [])
        self.assertEqual(len(traced), 1)
        self.assertIn("icon is unavailable", traced[0])

    def test_the_splash_loads_draws_and_releases_one_wide_owned_handle(self) -> None:
        """The whole product path with a handle that does not fit in 32 bits."""

        user32 = FakeUser32()

        handle = DESKTOP.NativeStartupSplash._load_mark_icon(user32)
        drawn = DESKTOP.NativeStartupSplash._draw_mark_icon(user32, 7, 11, 13, handle)

        self.assertEqual(handle, WIDE_HANDLE, "the loaded handle was truncated")
        self.assertTrue(drawn)
        self.assertEqual(user32.loaded[0][0], str(BRAND.mark_ico_path()))
        self.assertEqual(user32.loaded[0][1:], (1, 56, 56, 0x0010))
        # The exact handle reaches the draw, and is destroyed exactly once.
        self.assertEqual(user32.drawn, [(7, 11, 13, WIDE_HANDLE, 56, 56, 0x0003)])
        self.assertEqual(user32.destroyed, [WIDE_HANDLE])

    def test_a_failed_draw_still_releases_the_handle_exactly_once(self) -> None:
        user32 = FakeUser32(draw_result=0)

        handle = DESKTOP.NativeStartupSplash._load_mark_icon(user32)
        drawn = DESKTOP.NativeStartupSplash._draw_mark_icon(user32, 0, 0, 0, handle)

        self.assertFalse(drawn, "a failed draw must be reported so the tile stays plain")
        self.assertEqual(user32.destroyed, [WIDE_HANDLE])

    def test_no_handle_means_no_draw_and_no_destroy(self) -> None:
        user32 = FakeUser32()

        self.assertFalse(DESKTOP.NativeStartupSplash._draw_mark_icon(user32, 0, 0, 0, None))
        self.assertEqual(user32.drawn, [])
        self.assertEqual(user32.destroyed, [])

    def test_pointer_sized_prototypes_are_declared_before_the_handle_crosses_ctypes(self) -> None:
        """Without these the default c_int return sign-extends a wide HICON."""

        user32 = FakeUser32()
        DESKTOP.NativeStartupSplash._load_mark_icon(user32)

        self.assertIs(user32.LoadImageW.restype, wintypes.HANDLE)
        self.assertIs(user32.DestroyIcon.restype, wintypes.BOOL)
        self.assertIs(user32.DrawIconEx.restype, wintypes.BOOL)
        self.assertEqual(user32.LoadImageW.argtypes[0], wintypes.HINSTANCE)
        self.assertEqual(user32.LoadImageW.argtypes[1], wintypes.LPCWSTR)
        self.assertEqual(user32.DrawIconEx.argtypes[3], wintypes.HICON)
        self.assertEqual(user32.DestroyIcon.argtypes, [wintypes.HICON])
        for size in (
            ctypes.sizeof(wintypes.HANDLE),
            ctypes.sizeof(wintypes.HICON),
            ctypes.sizeof(wintypes.HINSTANCE),
        ):
            self.assertEqual(size, ctypes.sizeof(ctypes.c_void_p))

    def test_the_splash_reports_no_handle_when_the_asset_is_missing(self) -> None:
        class User32:
            def LoadImageW(self, *_arguments):  # noqa: N802
                raise AssertionError("must not load when the asset is missing")

        with mock.patch.object(DESKTOP, "has_mark_ico", lambda: False):
            self.assertIsNone(DESKTOP.NativeStartupSplash._load_mark_icon(User32()))


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
