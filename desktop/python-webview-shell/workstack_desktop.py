from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
APPLICATION_ROOT = SCRIPT_DIRECTORY.parents[1]
for import_root in (SCRIPT_DIRECTORY, APPLICATION_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from workstack import __version__ as WORKSTACK_VERSION
from brand_assets import BrandAssetMissing, has_mark_ico, inline_mark_markup, mark_ico_path
from native_theme import (
    load_persisted_theme,
    normalize_theme,
    persist_theme,
    theme_color,
    theme_colorref,
    theme_rgb,
)
from workstack_update import (
    MAX_MANIFEST_BYTES,
    UPDATE_MANIFEST_URL,
    DownloadedUpdate,
    OlderUpdateManifest,
    UpdatePreferences,
    download_update,
    fetch_url_bytes,
    load_update_preferences,
    parse_update_manifest,
    save_update_preferences,
)
from remote_connection_monitor import RemoteConnectionMonitor
from bounded_request_worker import BoundedRequestWorker
from ssh_profile_metadata import run_remote_profile_metadata_check
from connection_registry import ConnectionProfile, ConnectionRegistry, SshConnectionProfile
from connection_registry_compat import (
    export_active_legacy_mirror,
    rebind_active_local_workspace,
    rebind_active_remote_workspace,
)
from local_workspace_rebind import read_confirmed_local_rebind


class LocalRebindMirrorError(RuntimeError):
    """The registry authority was committed but its derived mirror was not.

    Distinguishes partial completion from a pre-commit refusal, so the host can
    report that the identity IS saved instead of implying nothing persisted.
    """
from connection_registry_mutations import (
    ConnectionRegistryMutationService,
    RegistryConflictError,
    current_registry_snapshot,
    pending_activation_for_registry,
    registry_digest,
)
from connection_registry_activation_recovery import (
    ActivationRecoveryRefusedError,
    ConnectionRegistryActivationRecoveryService,
    activation_recovery_status_to_document,
)
from connection_registry_startup import (
    LocalStartupSelection,
    SshStartupSelection,
    ensure_connection_registry,
    fresh_local_store_required,
    select_active_profile_for_startup,
)
from connection_registry_host_contract import (
    MAX_HOST_REQUEST_BYTES as CONNECTION_REGISTRY_MAX_REQUEST_BYTES,
    ConnectionRegistryHostService,
    RegistryHostErrorResponse,
    encode_registry_host_response,
)
from ssot_connection import (
    REMOTE_CONNECTION_FILE,
    RemoteConnectionProfile,
    build_ssh_check_command,
    build_ssh_tunnel_command,
    check_remote_connection,
    connection_profile_from_draft,
    find_ssh_executable,
    load_connection_draft,
    load_remote_connection_profile,
    profile_with_runtime_forward_port,
    resolve_runtime_forward_port,
    run_remote_connection_check,
    save_connection_draft,
    validate_connection_draft,
)
from startup_recovery_host import (
    build_startup_recovery_html,
    parse_startup_recovery_request,
)


SOURCE_HOST_PREFIX = "workstack-source-host"
UPDATE_HOST_PREFIX = "workstack-update-host"
SSOT_HOST_PREFIX = "workstack-ssot-host"
DESKTOP_MINIMUM_REMOTE_PROTOCOL = 1
REMOTE_REBIND_COORDINATION_SECONDS = 30.0
PROVIDER_URLS = {
    "outlook": "https://outlook.office.com/mail/",
    "teams": "https://teams.microsoft.com/v2/",
    "onenote": "https://www.office.com/launch/onenote",
}
_REGISTRY_TYPE_PATTERN = re.compile(
    r'"type"\s*:\s*"workstack-connection-registry-request"'
)
_REGISTRY_REQUEST_ID_PATTERN = re.compile(
    r'"request_id"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
)
_REGISTRY_OPERATION_PATTERN = re.compile(
    r'"operation"\s*:\s*"(get-registry|save-registry|discover-ssh-aliases|choose-local-directory|test-profile|activate-profile)"'
)


def connection_registry_startup_enabled(environment: object = os.environ) -> bool:
    """Enable the released desktop registry unless a recovery launch opts out."""

    getter = getattr(environment, "get", None)
    value = getter("WORKSTACK_CONNECTION_REGISTRY_V1", "1") if callable(getter) else "1"
    return value != "0"


def _is_connection_registry_host_message(message: str) -> bool:
    if not isinstance(message, str) or not message.lstrip().startswith("{"):
        return False
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(_REGISTRY_TYPE_PATTERN.search(message[:4096]))


class RemoteAuthorityMismatch(RuntimeError):
    """The tunnel is live, but it no longer reaches the configured authority."""


COMMON_AUTH_HOSTS = frozenset(
    {
        "login.microsoftonline.com",
        "login.live.com",
        "office.com",
        "www.office.com",
        "www.microsoft365.com",
        "m365.cloud.microsoft",
    }
)
PROVIDER_EXACT_HOSTS = {
    "outlook": frozenset(
        {
            "outlook.office.com",
            "outlook.office365.com",
            "outlook.live.com",
            "outlook.cloud.microsoft",
        }
    ),
    "teams": frozenset({"teams.microsoft.com", "teams.cloud.microsoft", "teams.live.com"}),
    "onenote": frozenset(
        {
            "www.office.com",
            "www.microsoft365.com",
            "m365.cloud.microsoft",
            "www.onenote.com",
            "onenote.cloud.microsoft",
            "onedrive.live.com",
        }
    ),
}
PROVIDER_SUFFIXES = {
    "outlook": (),
    "teams": (),
    "onenote": (".sharepoint.com",),
}
EXPECTED_HEALTH = {"data": {"api_version": "v1", "status": "ready"}}
ERROR_ALREADY_EXISTS = 183
NATIVE_WINDOW_TITLE = "\u200b"
APP_USER_MODEL_ID = "WorkStack.Desktop"
OUTLOOK_VISIBLE_CAPTURE_SCRIPT = r"""
(() => {
  const requestId = __REQUEST_ID__;
  const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const visible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  try {
    const focused = document.getElementById('focused');
    const scope = focused || document;
    const bodyCandidates = Array.from(scope.querySelectorAll('[id^="UniqueMessageBody_"]')).filter(visible);
    const titleCandidates = Array.from(scope.querySelectorAll('[id^="MSG_"][id$="_SUBJECT"], [id^="CONV_"][id$="_SUBJECT"]')).filter(visible);
    const body = normalize(bodyCandidates.at(-1)?.innerText || '');
    const title = normalize(titleCandidates.find((element) => normalize(element.innerText))?.innerText || '');
    window.chrome.webview.postMessage(JSON.stringify({
      type: 'workstack-outlook-visible-capture',
      request_id: requestId,
      title: title.slice(0, 500),
      text: body.slice(0, 4000),
    }));
  } catch (_error) {
    window.chrome.webview.postMessage(JSON.stringify({
      type: 'workstack-outlook-visible-capture',
      request_id: requestId,
      title: '',
      text: '',
    }));
  }
})()
"""


SOURCE_ZOOM_FILE = "source-zoom.json"
RUNTIME_CONFIG_FILE = "runtime-config.json"
SOURCE_ZOOM_DEFAULT = 100
SOURCE_ZOOM_MIN = 50
SOURCE_ZOOM_MAX = 200
STARTUP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="color-scheme" content="__WS_THEME__">
<style>
html,body{height:100%;margin:0;background:__WS_BG__;color:__WS_TEXT__;font:14px system-ui,sans-serif}
body{display:grid;place-items:center}.shell{display:flex;align-items:center;gap:16px}
.mark{width:42px;height:42px;display:grid;place-items:center}
.mark svg{width:42px;height:42px;display:block}.copy{display:grid;gap:5px}.title{font-size:20px;font-weight:750}
.status{color:__WS_MUTED__}.pulse{animation:pulse 1.1s ease-in-out infinite}@keyframes pulse{50%{opacity:.45}}
</style></head><body><div class="shell">__WS_MARK__<div class="copy">
<div class="title">Work Stack</div><div class="status pulse">Preparing your workspace…</div>
</div></div></body></html>"""


def build_startup_html(theme: str) -> str:
    normalized = normalize_theme(theme)
    return (
        STARTUP_HTML
        .replace("__WS_MARK__", inline_mark_markup())
        .replace("__WS_THEME__", normalized)
        .replace("__WS_BG__", theme_color(normalized, "bg.app"))
        .replace("__WS_TEXT__", theme_color(normalized, "text.primary"))
        .replace("__WS_ACCENT__", theme_color(normalized, "brand.accent"))
        .replace("__WS_INK__", theme_color(normalized, "brand.ink"))
        .replace("__WS_MUTED__", theme_color(normalized, "text.muted"))
    )


def load_source_zoom(state_root: Path) -> dict[str, int]:
    defaults = {provider: SOURCE_ZOOM_DEFAULT for provider in PROVIDER_URLS}
    path = state_root / SOURCE_ZOOM_FILE
    if not path.is_file():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return defaults
    if not isinstance(raw, dict) or set(raw) - set(PROVIDER_URLS):
        return defaults
    for provider, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or not SOURCE_ZOOM_MIN <= value <= SOURCE_ZOOM_MAX:
            return defaults
        defaults[provider] = value
    return defaults


def save_source_zoom(state_root: Path, values: dict[str, int]) -> None:
    payload = {provider: values.get(provider, SOURCE_ZOOM_DEFAULT) for provider in PROVIDER_URLS}
    if any(isinstance(value, bool) or not isinstance(value, int) or not SOURCE_ZOOM_MIN <= value <= SOURCE_ZOOM_MAX for value in payload.values()):
        raise ValueError("Microsoft source zoom must be an integer from 50 to 200")
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / SOURCE_ZOOM_FILE
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_source_show_message(message: str) -> tuple[str, int, int, int, int] | None:
    parts = message.split("|")
    if len(parts) != 7 or parts[:2] != [SOURCE_HOST_PREFIX, "show"]:
        return None
    provider = parts[2]
    if provider not in PROVIDER_URLS:
        return None
    try:
        left, top, width, height = (int(value) for value in parts[3:])
    except ValueError:
        return None
    if not (-10_000 <= left <= 10_000 and -10_000 <= top <= 10_000):
        return None
    if not (160 <= width <= 10_000 and 120 <= height <= 10_000):
        return None
    return provider, left, top, width, height


def parse_source_capture_request(message: str) -> tuple[str, str] | None:
    parts = message.split("|", 3)
    if len(parts) != 4 or parts[:2] != [SOURCE_HOST_PREFIX, "capture"]:
        return None
    provider, request_id = parts[2:]
    if provider not in PROVIDER_URLS or not request_id or len(request_id) > 128:
        return None
    return provider, request_id


def parse_args() -> argparse.Namespace:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    inferred_install_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run Work Stack in the signed Python WebView host.")
    parser.add_argument("--install-root", type=Path, default=inferred_install_root)
    parser.add_argument("--state-root", type=Path, default=local_app_data / "WorkStack")
    parser.add_argument("--url", default="")
    parser.add_argument("--probe-provider", choices=tuple(PROVIDER_URLS), default="")
    parser.add_argument("--probe-result", type=Path)
    parser.add_argument("--auto-close-seconds", type=int, default=0)
    parser.add_argument(
        "--check-remote-connection",
        action="store_true",
        help="validate the SSH profile and perform a read-only remote prerequisite check",
    )
    return parser.parse_args()


class NativeStartupSplash:
    """Immediate full-work-area surface shown while the WebView runtime initializes."""

    def __init__(self, theme: str) -> None:
        self.theme = normalize_theme(theme)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd = 0

    @staticmethod
    def _declare_mark_icon_prototypes(user32) -> None:
        """Declare pointer-sized signatures before any handle crosses ctypes.

        Without these the default ``c_int`` return truncates a 64-bit HICON,
        so the handle drawn and destroyed would not be the handle Windows
        returned. Declaring is idempotent, so repeated paints are safe.
        """

        from ctypes import wintypes

        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.DrawIconEx.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HICON,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.HBRUSH,
            wintypes.UINT,
        ]
        user32.DrawIconEx.restype = wintypes.BOOL
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL

    @staticmethod
    def _load_mark_icon(user32):
        """Load the packaged versioned mark as an owned HICON, or None.

        The caller destroys the returned handle. A missing or unloadable asset
        returns None so the surface stays plain; no stale icon is searched for
        and no user file is touched.
        """

        if not has_mark_ico():
            return None
        try:
            NativeStartupSplash._declare_mark_icon_prototypes(user32)
            # IMAGE_ICON with LR_LOADFROMFILE.
            handle = user32.LoadImageW(None, str(mark_ico_path()), 1, 56, 56, 0x0010)
        except Exception:
            return None
        return handle or None

    @staticmethod
    def _draw_mark_icon(user32, hdc, left: int, top: int, handle) -> bool:
        """Draw the loaded mark and always release the handle exactly once.

        Returns whether the draw itself reported success. The handle is
        destroyed on both paths, and it is the same handle that was passed in.
        """

        if handle is None:
            return False
        try:
            # DI_NORMAL: image and mask together.
            return bool(user32.DrawIconEx(hdc, left, top, handle, 56, 56, 0, None, 0x0003))
        finally:
            user32.DestroyIcon(handle)

    def start(self) -> None:
        if os.name != "nt" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1)

    def close(self) -> None:
        if os.name == "nt" and self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, 0x0010, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateFontW.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        class_name = f"WorkStackStartupSplash{os.getpid()}"
        background = gdi32.CreateSolidBrush(theme_colorref(self.theme, "bg.app"))
        accent = gdi32.CreateSolidBrush(theme_colorref(self.theme, "brand.accent"))
        title_font = gdi32.CreateFontW(-24, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        status_font = gdi32.CreateFontW(-15, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hdc", wintypes.HDC),
                ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32),
            ]

        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        user32.BeginPaint.restype = wintypes.HDC
        user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        user32.EndPaint.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        user32.FillRect.restype = ctypes.c_int
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.UpdateWindow.argtypes = [wintypes.HWND]
        user32.UpdateWindow.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.SystemParametersInfoW.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            wintypes.LPVOID,
            wintypes.UINT,
        ]
        user32.SystemParametersInfoW.restype = wintypes.BOOL
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        gdi32.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
        gdi32.TextOutW.restype = wintypes.BOOL

        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        @callback_type
        def window_proc(hwnd, message, wparam, lparam):
            if message == 0x000F:
                paint = PAINTSTRUCT()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
                client = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(client))
                user32.FillRect(hdc, ctypes.byref(client), background)
                content_left = max(34, ((client.right - client.left) - 440) // 2 + 34)
                content_top = max(42, ((client.bottom - client.top) - 142) // 2 + 42)
                mark = wintypes.RECT(
                    content_left,
                    content_top,
                    content_left + 56,
                    content_top + 56,
                )
                # The packaged versioned mark, drawn through the existing Win32
                # path. The handle is owned here and destroyed below; when the
                # asset is missing the accent tile is left plain rather than
                # falling back to a glyph or a stale icon.
                mark_icon = self._load_mark_icon(user32)
                if not self._draw_mark_icon(user32, hdc, content_left, content_top, mark_icon):
                    user32.FillRect(hdc, ctypes.byref(mark), accent)
                gdi32.SetBkMode(hdc, 1)
                gdi32.SetTextColor(hdc, theme_colorref(self.theme, "brand.ink"))
                old_font = gdi32.SelectObject(hdc, title_font)
                gdi32.SetTextColor(hdc, theme_colorref(self.theme, "text.primary"))
                gdi32.TextOutW(hdc, content_left + 78, content_top + 1, "Work Stack", 10)
                gdi32.SelectObject(hdc, status_font)
                gdi32.SetTextColor(hdc, theme_colorref(self.theme, "text.muted"))
                status = "Preparing your workspace..."
                gdi32.TextOutW(hdc, content_left + 78, content_top + 36, status, len(status))
                gdi32.SelectObject(hdc, old_font)
                user32.EndPaint(hwnd, ctypes.byref(paint))
                return 0
            if message == 0x0002:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(
            0,
            ctypes.cast(window_proc, ctypes.c_void_p),
            0,
            0,
            instance,
            0,
            user32.LoadCursorW(None, ctypes.c_void_p(32512)),
            background,
            None,
            class_name,
        )
        try:
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                return
            work_area = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
                left = work_area.left
                top = work_area.top
                width = work_area.right - work_area.left
                height = work_area.bottom - work_area.top
            else:
                left = 0
                top = 0
                width = user32.GetSystemMetrics(0)
                height = user32.GetSystemMetrics(1)
            self._hwnd = user32.CreateWindowExW(
                0x00000088,
                class_name,
                "Work Stack",
                0x90000000,
                left,
                top,
                width,
                height,
                None,
                None,
                instance,
                None,
            )
            if not self._hwnd:
                return
            user32.ShowWindow(self._hwnd, 5)
            user32.UpdateWindow(self._hwnd)
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._ready.set()
            self._hwnd = 0
            user32.UnregisterClassW(class_name, instance)
            for handle in (background, accent, title_font, status_font):
                if handle:
                    gdi32.DeleteObject(handle)


class WorkStackDesktopHost:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.install_root = options.install_root.resolve()
        self.state_root = options.state_root.resolve()
        self.current_theme = load_persisted_theme(self.state_root)
        self.profile_root = self.state_root / "desktop-webview-profile"
        self.microsoft_profile_root = self.state_root / "desktop-microsoft-profile"
        self.microsoft_diagnostic_path = self.state_root / "logs" / "microsoft-webview.log"
        self.update_preferences = load_update_preferences(self.state_root)
        self.source_zoom = load_source_zoom(self.state_root)
        self.update_check_thread: threading.Thread | None = None
        self.downloaded_update: DownloadedUpdate | None = None
        self.install_update_on_exit = False
        self.update_status: dict[str, object] = {
            "type": "workstack-update-status",
            "state": "idle",
            "current_version": WORKSTACK_VERSION,
            "latest_version": WORKSTACK_VERSION,
            "release_url": "",
            "message": "",
        }
        # The profile registry is the desktop authority for the selected SSOT.
        # Keep an explicit opt-out for recovery builds, but do not require users
        # to launch the installed application with a hidden environment flag.
        self.connection_registry_startup_enabled = connection_registry_startup_enabled()
        self.connection_registry_mutations = ConnectionRegistryMutationService(
            self.state_root
        )
        self.connection_activation_recovery = ConnectionRegistryActivationRecoveryService(
            self.state_root,
            mutation_service=self.connection_registry_mutations,
        )
        self.startup_recovery_status: dict[str, object] | None = None
        self.startup_recovery_in_progress = False
        self.connection_registry_snapshot: ConnectionRegistry | None = None
        self.connection_registry_digest = registry_digest(None)
        self.runtime_connection_profile_id = ""
        self.local_startup_selection: LocalStartupSelection | None = None
        self.active_connection_draft = load_connection_draft(self.state_root)
        self.connection_registry_host = ConnectionRegistryHostService(
            self.state_root,
            local_directory_picker=self._choose_local_ssot_directory,
            ssh_profile_tester=run_remote_profile_metadata_check,
            mutation_service=(
                self.connection_registry_mutations
                if self.connection_registry_startup_enabled
                else None
            ),
            activation_observer=self._observe_connection_registry_activation,
        )
        self.connection_registry_worker = BoundedRequestWorker(
            self._execute_connection_registry_request,
            self._deliver_connection_registry_response,
            maximum_pending=16,
            thread_name="workstack-connection-registry-worker",
        )
        configured_remote_profile = connection_profile_from_draft(self.active_connection_draft)
        self.remote_profile = (
            profile_with_runtime_forward_port(configured_remote_profile)
            if configured_remote_profile is not None
            else None
        )
        if self.remote_profile is not None and options.url:
            raise RuntimeError("--url cannot be combined with storage_mode 'ssh-remote'")
        self.workstack_url = (
            f"http://127.0.0.1:{self.remote_profile.local_forward_port}/"
            if self.remote_profile is not None
            else options.url or self._configured_url()
        )
        self.workstack_origin = self._origin(self.workstack_url)
        self.server_started_by_host = False
        self.server_pid: int | None = None
        self.server_process: subprocess.Popen | None = None
        self.startup_thread: threading.Thread | None = None
        self.startup_ready = threading.Event()
        self.startup_error: BaseException | None = None
        self.remote_ssh_process: subprocess.Popen | None = None
        self.remote_ssh_log = None
        self.remote_monitor: RemoteConnectionMonitor | None = None
        self.remote_reconnect_lock = threading.Lock()
        self.remote_shutdown_requested = threading.Event()
        self.remote_authority_lock = threading.RLock()
        self.remote_rebind_target = ""
        self.remote_rebind_deadline = 0.0
        self.remote_recovery_required = threading.Event()
        self.remote_recovery_message = ""
        self.remote_product_version = ""
        self.remote_protocol_version: int | None = None
        self.server_stop_thread: threading.Thread | None = None
        self.instance_mutex = None
        self.window: webview.Window | None = None
        self.form = None
        self.workstack_webview = None
        self.source_viewports: dict[str, object] = {}
        self.source_webviews: dict[str, object] = {}
        self.source_ready: set[str] = set()
        self.source_initialized: set[str] = set()
        self.source_auth_sessions: set[str] = set()
        self.source_event_handlers: list[object] = []
        self.source_environment_task = None
        self.source_popups: dict[str, dict[str, object]] = {}
        self.pending_source_captures: dict[str, dict[str, str]] = {}
        self.source_suspended = False
        self.source_host_active = False
        self.active_provider = ""
        self.native_icon = None
        self.probe_recorded = False
        self.runtime_version = "unknown"
        self.startup_splash = NativeStartupSplash(self.current_theme)

    def run(self) -> int:
        self._apply_process_identity()
        if not self._acquire_single_instance():
            self._activate_existing_window()
            return 0
        try:
            self.connection_registry_worker.start()
            self.startup_splash.start()
            self.startup_thread = threading.Thread(target=self._prepare_server, daemon=False)
            self.startup_thread.start()
            import webview

            self.profile_root.mkdir(parents=True, exist_ok=True)
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            self.window = webview.create_window(
                "Work Stack",
                html=build_startup_html(self.current_theme),
                min_size=(1024, 700),
                maximized=True,
                background_color=theme_color(self.current_theme, "native.overlay"),
            )
            webview.start(
                self._on_window_started,
                gui="edgechromium",
                debug=False,
                private_mode=False,
                storage_path=str(self.profile_root),
            )
            return 0
        finally:
            self.startup_splash.close()
            self.connection_registry_worker.stop(timeout=5)
            if self.startup_thread is not None:
                self.startup_thread.join(timeout=20)
            self._stop_remote_monitor()
            if self.server_stop_thread is not None:
                self.server_stop_thread.join(timeout=12)
            else:
                self._stop_owned_server()
            self._release_single_instance()
            self._launch_pending_update()

    def _prepare_server(self) -> None:
        try:
            if getattr(self, "connection_registry_startup_enabled", False):
                self._prepare_connection_registry_runtime()
            self._ensure_server()
            if getattr(self, "connection_registry_startup_enabled", False):
                self._confirm_pending_connection_registry_activation()
        except BaseException as error:
            self.startup_error = error
        finally:
            self.startup_ready.set()

    def _prepare_connection_registry_runtime(self) -> None:
        config, config_path = self._local_runtime_config()
        local_data_dir = config.get("data_dir")
        if not isinstance(local_data_dir, str) or not local_data_dir:
            raise RuntimeError(f"Work Stack data directory is invalid: {config_path}")
        installation_identity = str(self.state_root).casefold()
        if fresh_local_store_required(self.state_root, local_data_dir):
            self._initialize_fresh_local_store(Path(local_data_dir).resolve())
        migrated = ensure_connection_registry(
            self.state_root,
            installation_identity=installation_identity,
            local_data_dir=local_data_dir,
            remote_identity_reader=self._read_registry_remote_identity,
        )
        selection = select_active_profile_for_startup(
            self.state_root,
            remote_identity_reader=self._read_registry_remote_identity,
        )
        current, current_digest = current_registry_snapshot(self.state_root)
        if current_digest != registry_digest(migrated):
            raise RegistryConflictError(
                "Connection registry changed while the active workspace was verified"
            )
        self.connection_registry_snapshot = current
        self.connection_registry_digest = current_digest
        self.runtime_connection_profile_id = selection.profile_id
        if isinstance(selection, LocalStartupSelection):
            self.local_startup_selection = selection
            self.active_connection_draft = {"storage_mode": "local"}
            self.remote_profile = None
            self.workstack_url = self.options.url or self._configured_url()
        elif isinstance(selection, SshStartupSelection):
            if self.options.url:
                raise RuntimeError("--url cannot be combined with an active SSH profile")
            self.local_startup_selection = None
            configured = RemoteConnectionProfile(
                ssh_host_alias=selection.ssh_host_alias,
                remote_app_dir=selection.remote_app_dir,
                remote_data_dir=selection.remote_data_dir,
                local_forward_port=selection.preferred_forward_port,
                workspace_id=selection.expected_workspace_id,
                remote_port=selection.remote_port,
            )
            self.remote_profile = profile_with_runtime_forward_port(configured)
            self.active_connection_draft = {
                "storage_mode": "ssh-remote",
                "ssh_host_alias": selection.ssh_host_alias,
                "remote_app_dir": selection.remote_app_dir,
                "remote_data_dir": selection.remote_data_dir,
                "local_forward_port": selection.preferred_forward_port,
                "workspace_id": selection.expected_workspace_id,
                "remote_port": selection.remote_port,
            }
            self.workstack_url = (
                f"http://127.0.0.1:{self.remote_profile.local_forward_port}/"
            )
        else:  # pragma: no cover - closed typed union
            raise RuntimeError("Active connection profile has an unsupported kind")
        self.workstack_origin = self._origin(self.workstack_url)
        export_active_legacy_mirror(
            self.state_root,
            expected_registry_digest=self.connection_registry_digest,
        )

    def _initialize_fresh_local_store(self, data_path: Path) -> None:
        """Create the first local workspace of a wholly fresh installation.

        The desktop host never constructs a Store.  The product's own offline
        maintenance entry does, in the bundled runtime, exactly as the pre-launch
        backup runs.  ``maintenance initialize`` refuses unless the directory is
        absent or empty, so it can never repair, migrate or overwrite planning
        data; the registry step that follows binds the identity it created like
        any other existing local Store.
        """

        python_path = self.install_root / "runtime" / "python.exe"
        entry_path = self.install_root / "run_work_stack.py"
        if not python_path.is_file() or not entry_path.is_file():
            raise RuntimeError("Work Stack installation is incomplete. Re-run the installer.")
        log_path = self.state_root / "logs"
        log_path.mkdir(parents=True, exist_ok=True)
        receipt_path = log_path / "initialize.out.log"
        error_path = log_path / "initialize.err.log"
        with receipt_path.open("wb") as receipt, error_path.open("wb") as error_log:
            completed = subprocess.run(
                [
                    str(python_path),
                    str(entry_path),
                    "--data-dir",
                    str(data_path),
                    "maintenance",
                    "initialize",
                ],
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL,
                stdout=receipt,
                stderr=error_log,
            )
        if completed.returncode != 0:
            detail = error_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Work Stack could not create its first workspace: {detail or 'unknown error'}"
            )
        self._trace(f"first local workspace created by the bundled runtime at {data_path}")

    def _observe_connection_registry_activation(
        self, registry: ConnectionRegistry, digest: str
    ) -> None:
        """Refresh configuration cache and the one-way downgrade mirror."""

        self.connection_registry_snapshot = registry
        self.connection_registry_digest = digest
        export_active_legacy_mirror(
            self.state_root,
            expected_registry_digest=digest,
        )

    def _confirm_pending_connection_registry_activation(self) -> None:
        """Confirm activation only after server/tunnel readiness has succeeded."""

        digest = self.connection_registry_digest
        pending = pending_activation_for_registry(self.state_root, digest)
        if pending is None:
            return
        expected_workspace_id = self._runtime_expected_workspace_id()
        if not self._server_sync_matches_expected(expected_workspace_id):
            raise RuntimeError(
                "Connection activation remains pending because the running server is not "
                "in sync with the selected workspace. Review workspace synchronization "
                "before confirming this connection."
            )
        self.connection_registry_mutations.confirm(
            pending.activation_id,
            expected_registry_digest=digest,
        )

    def _runtime_expected_workspace_id(self) -> str:
        selection = self.local_startup_selection
        if selection is not None:
            return selection.expected_workspace_id
        profile = self.remote_profile
        if profile is not None:
            return profile.workspace_id
        raise RuntimeError("The running connection does not identify an expected workspace")

    def _server_sync_matches_expected(
        self, expected_workspace_id: str, *, timeout: float = 1.5
    ) -> bool:
        sync_url = urllib.parse.urljoin(self.workstack_url, "/api/v1/sync/status")
        try:
            with urllib.request.urlopen(sync_url, timeout=timeout) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("state") != "in-sync":
            return False
        return (
            data.get("workspace_id") == expected_workspace_id
            and data.get("candidate_workspace_id") == expected_workspace_id
        )

    @staticmethod
    def _read_registry_remote_identity(profile: ConnectionProfile) -> str:
        if not isinstance(profile, SshConnectionProfile):
            raise RuntimeError("Remote identity verification requires an SSH profile")
        return run_remote_profile_metadata_check(profile).actual_workspace_id

    def _navigate_when_ready(self) -> None:
        self.startup_ready.wait()
        if self.window is None:
            return
        if self.startup_error is not None:
            if self._show_startup_activation_recovery():
                return
            show_startup_error(self.startup_error)
            self.window.destroy()
            return
        self.window.load_url(self.workstack_url)

    def _show_startup_activation_recovery(self) -> bool:
        if not getattr(self, "connection_registry_startup_enabled", False):
            return False
        try:
            status = self.connection_activation_recovery.inspect()
            document = activation_recovery_status_to_document(status)
            if document["state"] != "recovery_required" or not document["can_restore"]:
                return False
            html_document = build_startup_recovery_html(document, theme=self.current_theme)
        except Exception:
            return False
        write_startup_error_log(self.startup_error or RuntimeError("Startup failed"))
        self.startup_recovery_status = document
        self.startup_recovery_in_progress = False
        try:
            self.window.load_html(html_document)
        except Exception:
            self.startup_recovery_status = None
            return False
        return True

    def _dispatch_startup_recovery_message(self, message: str) -> bool:
        status = getattr(self, "startup_recovery_status", None)
        if status is None:
            return False
        request = parse_startup_recovery_request(message)
        if request is None or request.activation_id != status.get("activation_id"):
            return False
        if request.operation == "exit":
            self.window.destroy()
            return True
        if self.startup_recovery_in_progress:
            return True
        if request.expected_registry_digest != status.get("current_registry_digest"):
            return True
        self.startup_recovery_in_progress = True
        threading.Thread(
            target=self._restore_previous_startup_connection,
            args=(request.activation_id, request.expected_registry_digest),
            daemon=True,
        ).start()
        return True

    def _restore_previous_startup_connection(
        self, activation_id: str, expected_registry_digest: str
    ) -> None:
        status = self.startup_recovery_status
        if status is None:
            return
        try:
            self.connection_activation_recovery.restore(
                activation_id,
                expected_registry_digest=expected_registry_digest,
            )
            html_document = build_startup_recovery_html(
                status,
                outcome="restored",
                theme=self.current_theme,
            )
        except ActivationRecoveryRefusedError as error:
            html_document = build_startup_recovery_html(
                status,
                outcome="refused",
                safe_message=error.safe_message,
                theme=self.current_theme,
            )
        except Exception:
            html_document = build_startup_recovery_html(
                status,
                outcome="refused",
                theme=self.current_theme,
            )
        if self.window is not None:
            self.window.load_html(html_document)

    def _on_form_closing(self, _sender, _event_args) -> None:
        self.connection_registry_worker.stop(timeout=0)
        self._stop_remote_monitor()
        if (self.server_started_by_host or self.remote_ssh_process is not None) and self.server_stop_thread is None:
            self.server_stop_thread = threading.Thread(
                target=self._stop_owned_server_after_window,
                daemon=False,
            )
            self.server_stop_thread.start()

    def _stop_owned_server_after_window(self) -> None:
        time.sleep(0.5)
        self._stop_owned_server()

    def _on_window_started(self) -> None:
        if self.options.auto_close_seconds > 0 and self.window is not None:
            seconds = min(max(self.options.auto_close_seconds, 1), 60)
            timer = threading.Timer(seconds, self.window.destroy)
            timer.daemon = True
            timer.start()
        deadline = time.monotonic() + 10
        while self.window is not None and self.window.native is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.window is None or self.window.native is None:
            raise RuntimeError("The native Work Stack window was not created.")

        from System import Action

        self.form = self.window.native
        self.form.Invoke(Action(self._initialize_native_shell))
        self.startup_splash.close()
        threading.Thread(target=self._navigate_when_ready, daemon=True).start()

    def _initialize_native_shell(self) -> None:
        self.form.FormClosing += self._on_form_closing
        self._apply_native_brand()
        self._apply_native_theme(self.current_theme)
        self._configure_native_views()

    def _configure_native_views(self) -> None:
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment

        self.workstack_webview = self.window.native.webview
        self.runtime_version = str(CoreWebView2Environment.GetAvailableBrowserVersionString())
        self.workstack_webview.NavigationStarting += self._on_workstack_navigation_starting
        self.workstack_webview.WebMessageReceived -= self.window.native.browser.on_script_notify
        self.workstack_webview.WebMessageReceived += self._on_workstack_message
        self.workstack_webview.CoreWebView2InitializationCompleted += self._on_workstack_ready
        if self.workstack_webview.CoreWebView2 is not None:
            self._create_source_views(self.workstack_webview.CoreWebView2.Environment)
        self._apply_native_surface_theme()

    def _on_workstack_ready(self, sender, event_args) -> None:
        if not event_args.IsSuccess:
            if self.options.probe_result:
                self._write_probe(False, "initializing", "WorkStackInitializationFailed")
            return
        self._create_source_views(sender.CoreWebView2.Environment)
        self._post_update_status()
        self._post_ssot_current_status()
        self._start_remote_monitor()
        if self.update_preferences.auto_check and not self.options.probe_result:
            self._start_update_check()

    def _create_source_views(self, fallback_environment) -> None:
        if self.source_webviews or self.source_environment_task is not None:
            return

        from System import Action
        from System.Threading.Tasks import Task
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment, CoreWebView2EnvironmentOptions

        self.microsoft_profile_root.mkdir(parents=True, exist_ok=True)
        options = CoreWebView2EnvironmentOptions()
        options.AllowSingleSignOnUsingOSPrimaryAccount = True
        self.source_environment_task = CoreWebView2Environment.CreateAsync(
            None,
            str(self.microsoft_profile_root),
            options,
        )
        self._record_microsoft_diagnostic(
            "environment-start",
            stage=f"os-sso-enabled:{self.source_environment_task.Status}",
        )

        def environment_ready(task) -> None:
            self.source_environment_task = None
            if not task.IsFaulted and not task.IsCanceled:
                self._record_microsoft_diagnostic("environment-ready", success=True)
                self._create_source_views_with_environment(task.Result)
                return
            self._record_microsoft_diagnostic("environment-ready", success=False)
            self._create_source_views_with_environment(fallback_environment)

        def environment_finished(task) -> None:
            self._record_microsoft_diagnostic("environment-finished", stage=str(task.Status))
            try:
                dispatch = Action(lambda: environment_ready(task))
                self.source_event_handlers.append(dispatch)
                self.form.Invoke(dispatch)
            except Exception as error:
                self._record_microsoft_diagnostic(
                    "environment-dispatch",
                    success=False,
                    stage=type(error).__name__,
                )
                self._trace(f"Microsoft environment dispatch failed: {type(error).__name__}: {error}")

        completion = Action[Task](environment_finished)
        self.source_event_handlers.extend((environment_ready, environment_finished, completion))
        self.source_environment_task.ContinueWith(completion)

    def _create_source_views_with_environment(self, environment) -> None:
        if self.source_webviews:
            return

        import System.Windows.Forms as WinForms
        from Microsoft.Web.WebView2.WinForms import WebView2
        from System.Drawing import Color

        external_background = Color.FromArgb(*theme_rgb(self.current_theme, "native.externalLoading"))
        for provider in PROVIDER_URLS:
            viewport = WinForms.Panel()
            viewport.Visible = False
            viewport.BackColor = external_background

            source_webview = WebView2()
            source_webview.DefaultBackgroundColor = external_background
            source_webview.ZoomFactor = self.source_zoom[provider] / 100.0
            navigation_starting = lambda sender, event_args, key=provider: self._on_source_navigation_starting(key, sender, event_args)
            navigation_completed = lambda sender, event_args, key=provider: self._on_source_navigation_completed(key, sender, event_args)
            source_ready = lambda sender, event_args, key=provider: self._on_source_ready(key, sender, event_args)
            self.source_event_handlers.extend((navigation_starting, navigation_completed, source_ready))
            source_webview.NavigationStarting += navigation_starting
            source_webview.NavigationCompleted += navigation_completed
            source_webview.CoreWebView2InitializationCompleted += source_ready
            viewport.Controls.Add(source_webview)
            self.form.Controls.Add(viewport)
            self.source_viewports[provider] = viewport
            self.source_webviews[provider] = source_webview
            source_webview.EnsureCoreWebView2Async(environment)

        if self.options.probe_provider:
            from System.Drawing import Rectangle

            width = max(640, self.form.ClientSize.Width - 120)
            height = max(480, self.form.ClientSize.Height - 170)
            self._show_source(self.options.probe_provider, Rectangle(60, 90, width, height))

    def _on_source_ready(self, provider: str, sender, event_args) -> None:
        if not event_args.IsSuccess:
            self._record_microsoft_diagnostic("source-ready", provider, success=False)
            if self.options.probe_result:
                self._write_probe(False, "initializing", "InitializationFailed")
            return

        settings = sender.CoreWebView2.Settings
        settings.AreDevToolsEnabled = False
        settings.AreDefaultContextMenusEnabled = True
        settings.IsPasswordAutosaveEnabled = False
        settings.IsGeneralAutofillEnabled = False
        new_window = lambda source, args, key=provider: self._on_source_new_window(key, source, args)
        source_message = lambda source, args, key=provider: self._on_source_message(key, source, args)
        external_uri = lambda source, args, key=provider: self._on_source_external_uri(key, source, args)
        self.source_event_handlers.extend((new_window, source_message, external_uri))
        sender.CoreWebView2.NewWindowRequested += new_window
        sender.CoreWebView2.WebMessageReceived += source_message
        sender.CoreWebView2.LaunchingExternalUriScheme += external_uri
        self.source_ready.add(provider)
        self._record_microsoft_diagnostic("source-ready", provider, success=True)
        if self.active_provider == provider or self.options.probe_provider == provider:
            self._navigate_source_once(provider)

    def _on_workstack_message(self, _sender, event_args) -> None:
        try:
            message = str(event_args.TryGetWebMessageAsString())
        except Exception:
            return
        if self._dispatch_startup_recovery_message(message):
            return
        if self._origin(str(event_args.Source)) != self.workstack_origin:
            return
        if self._dispatch_workstack_host_message(message):
            return
        source_bounds = parse_source_show_message(message)
        if source_bounds is None:
            return
        provider, left, top, width, height = source_bounds
        from System.Drawing import Rectangle

        self._show_source(provider, Rectangle(left, top, width, height))

    def _dispatch_workstack_host_message(self, message: str) -> bool:
        if _is_connection_registry_host_message(message):
            self._handle_connection_registry_message(message)
            return True
        if message.startswith(f"{UPDATE_HOST_PREFIX}|"):
            self._handle_update_message(message)
            return True
        if message.startswith(f"{SSOT_HOST_PREFIX}|"):
            self._handle_ssot_message(message)
            return True
        if self._dispatch_source_host_message(message):
            return True
        if message.startswith("workstack-window-theme|"):
            theme = message.partition("|")[2]
            if theme in {"dark", "light"}:
                try:
                    persist_theme(self.state_root, theme)
                except (OSError, ValueError):
                    self._trace("desktop theme preference could not be persisted")
                self._apply_native_theme(theme)
            return True
        return False

    def _handle_connection_registry_message(self, message: str) -> None:
        if self.connection_registry_worker.submit(message):
            return
        response = self._connection_registry_busy_response(message)
        self._post_connection_registry_response(response)

    def _execute_connection_registry_request(self, message: str) -> str:
        try:
            return self.connection_registry_host.handle_json(message)
        except Exception:
            correlation = self._connection_registry_correlation(message)
            request_id, operation = correlation if correlation is not None else (None, None)
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=request_id,
                    operation=operation,
                    code="internal_error",
                    message="Connection registry operation could not be completed.",
                )
            )

    @staticmethod
    def _connection_registry_busy_response(message: str) -> str:
        correlation = WorkStackDesktopHost._connection_registry_correlation(message)
        if correlation is None:
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=None,
                    operation=None,
                    code="invalid_request",
                    message="Connection registry request is invalid.",
                )
            )
        request_id, operation = correlation
        return encode_registry_host_response(
            RegistryHostErrorResponse(
                request_id=request_id,
                operation=operation,
                code="busy",
                message="Connection registry is busy. Try again shortly.",
            )
        )

    @staticmethod
    def _connection_registry_correlation(message: str) -> tuple[str, str] | None:
        if not isinstance(message, str) or len(message) > CONNECTION_REGISTRY_MAX_REQUEST_BYTES:
            return None
        prefix = message[:4096]
        if len(_REGISTRY_TYPE_PATTERN.findall(prefix)) != 1:
            return None
        request_ids = _REGISTRY_REQUEST_ID_PATTERN.findall(prefix)
        operations = _REGISTRY_OPERATION_PATTERN.findall(prefix)
        if len(request_ids) != 1 or len(operations) != 1:
            return None
        try:
            request_id = str(uuid.UUID(request_ids[0]))
        except ValueError:
            return None
        if request_id != request_ids[0] or uuid.UUID(request_id).int == 0:
            return None
        return request_id, operations[0]

    def _deliver_connection_registry_response(self, response: str) -> None:
        form = self.form
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return
        from System import Action

        def deliver_on_ui() -> None:
            current = self.form
            if current is not form or bool(getattr(current, "IsDisposed", False)):
                return
            self._post_connection_registry_response(response)

        try:
            form.BeginInvoke(Action(deliver_on_ui))
        except Exception:
            self._trace("connection registry response could not be marshalled to the UI thread")

    def _post_connection_registry_response(self, response: str) -> None:
        core = (
            self.workstack_webview.CoreWebView2
            if self.workstack_webview is not None
            else None
        )
        if core is not None:
            core.PostWebMessageAsJson(response)

    def _choose_local_ssot_directory(self) -> str | None:
        form = self.form
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return None
        from System import Action
        from System.Windows.Forms import DialogResult, FolderBrowserDialog

        selected: list[str | None] = [None]

        def choose() -> None:
            dialog = FolderBrowserDialog()
            try:
                dialog.Description = "Choose a Work Stack SSOT directory"
                dialog.ShowNewFolderButton = True
                if dialog.ShowDialog(form) == DialogResult.OK:
                    selected[0] = str(dialog.SelectedPath)
            finally:
                dialog.Dispose()

        try:
            form.Invoke(Action(choose))
        except Exception as error:
            raise RuntimeError("The local SSOT directory picker could not be opened") from error
        return selected[0]

    @staticmethod
    def _save_connection_registry_from_host(_state_root, _registry):
        raise RuntimeError(
            "Connection registry mutations are disabled until activation safety is released"
        )

    def _dispatch_source_host_message(self, message: str) -> bool:
        if message == f"{SOURCE_HOST_PREFIX}|hide":
            self._deactivate_source()
            return True
        if message == f"{SOURCE_HOST_PREFIX}|suspend":
            self.source_suspended = True
            self._hide_source()
            return True
        if message == f"{SOURCE_HOST_PREFIX}|resume":
            self.source_suspended = False
            self._restore_source()
            return True
        if message.startswith(f"{SOURCE_HOST_PREFIX}|capture|"):
            self._send_source_capture(message)
            return True
        if message == f"{SOURCE_HOST_PREFIX}|zoom-status":
            self._post_source_zoom()
            return True
        if message.startswith(f"{SOURCE_HOST_PREFIX}|zoom|"):
            self._set_source_zoom(message)
            return True
        return False

    def _show_source(self, provider: str, requested_bounds) -> None:
        from System.Drawing import Rectangle

        if provider not in PROVIDER_URLS or self.workstack_webview is None:
            return
        clipped = Rectangle.Intersect(self.workstack_webview.ClientRectangle, requested_bounds)
        if clipped.Width < 160 or clipped.Height < 120:
            self._hide_source()
            return

        screen_location = self.workstack_webview.PointToScreen(clipped.Location)
        form_location = self.form.PointToClient(screen_location)
        for key, viewport in self.source_viewports.items():
            viewport.Visible = key == provider and not self.source_suspended
        viewport = self.source_viewports[provider]
        source_webview = self.source_webviews[provider]
        viewport.Bounds = Rectangle(form_location, clipped.Size)
        source_webview.Bounds = Rectangle(
            requested_bounds.Left - clipped.Left,
            requested_bounds.Top - clipped.Top,
            requested_bounds.Width,
            requested_bounds.Height,
        )
        self.active_provider = provider
        self.source_host_active = True
        if self.source_suspended:
            return
        viewport.Visible = True
        viewport.BringToFront()
        if provider in self.source_ready:
            self._navigate_source_once(provider)

    def _navigate_source_once(self, provider: str) -> None:
        if provider in self.source_initialized:
            return
        self.source_initialized.add(provider)
        self.source_webviews[provider].CoreWebView2.Navigate(PROVIDER_URLS[provider])

    def _hide_source(self) -> None:
        for viewport in self.source_viewports.values():
            viewport.Visible = False

    def _deactivate_source(self) -> None:
        self.source_host_active = False
        self._hide_source()

    def _restore_source(self) -> None:
        if self.source_suspended or not self.source_host_active or not self.active_provider:
            return
        viewport = self.source_viewports.get(self.active_provider)
        if viewport is None:
            return
        viewport.Visible = True
        viewport.BringToFront()

    def _post_source_zoom(self) -> None:
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        core.PostWebMessageAsJson(json.dumps({
            "type": "workstack-source-zoom",
            "values": self.source_zoom,
        }, ensure_ascii=True, separators=(",", ":")))

    def _ssot_status_payload(
        self,
        draft: dict[str, object],
        state: str,
        *,
        message: str = "",
        restart_required: bool | None = None,
    ) -> dict[str, object]:
        normalized = validate_connection_draft(draft)
        storage_mode = str(normalized["storage_mode"])
        active_draft = getattr(self, "active_connection_draft", normalized)
        remote_profile = getattr(self, "remote_profile", None)
        profile = {
            key: value for key, value in normalized.items() if key != "storage_mode"
        }
        if restart_required is None:
            restart_required = normalized != active_draft
        log_path = (
            str(self.state_root / "desktop-launch" / "remote-ssh.log")
            if storage_mode == "ssh-remote"
            else ""
        )
        return {
            "type": "workstack-ssot-connection-status",
            "state": state,
            "storage_mode": storage_mode,
            "profile": profile,
            "message": message[:500],
            "restart_required": restart_required,
            "log_path": log_path,
            "session_change_detection": (
                storage_mode == "ssh-remote"
                and normalized == active_draft
                and remote_profile is not None
            ),
            "runtime_forward_port": (
                remote_profile.local_forward_port
                if storage_mode == "ssh-remote" and remote_profile is not None
                else None
            ),
            "remote_product_version": getattr(self, "remote_product_version", ""),
            "remote_protocol_version": getattr(self, "remote_protocol_version", None),
        }

    def _post_ssot_status(self, payload: dict[str, object]) -> None:
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        core.PostWebMessageAsJson(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))

    def _post_ssot_current_status(self) -> None:
        try:
            draft = load_connection_draft(self.state_root)
            restart_required = draft != self.active_connection_draft
            message = self._current_ssot_message(draft, restart_required)
            payload = self._ssot_status_payload(
                draft,
                "ready",
                message=message,
                restart_required=restart_required,
            )
        except RuntimeError as error:
            payload = self._ssot_status_payload(
                self.active_connection_draft,
                "error",
                message=str(error),
                restart_required=False,
            )
        self._post_ssot_status(payload)

    def _current_ssot_message(self, draft: dict[str, object], restart_required: bool) -> str:
        if restart_required:
            return "Restart Work Stack to activate the saved connection."
        if self.remote_profile is None or draft.get("storage_mode") != "ssh-remote":
            return ""
        preferred = int(draft["local_forward_port"])
        runtime = self.remote_profile.local_forward_port
        if preferred == runtime:
            return "Remote SSOT connection is active."
        return f"Remote SSOT connection is active on local port {runtime}; preferred port {preferred} was occupied."

    def _dispatch_ssot_status(self, payload: dict[str, object]) -> None:
        if self.form is None:
            self._post_ssot_status(payload)
            return
        try:
            from System import Action

            dispatch = Action(lambda: self._post_ssot_status(payload))
            self.source_event_handlers.append(dispatch)
            self.form.BeginInvoke(dispatch)
        except Exception as error:
            self._trace(f"SSOT status dispatch failed: {type(error).__name__}")

    def _start_remote_monitor(self) -> None:
        if self.remote_profile is None:
            return
        current = self.remote_monitor
        if current is not None and current.is_running:
            return
        recovery = getattr(self, "remote_recovery_required", None)
        if recovery is None:
            recovery = threading.Event()
            self.remote_recovery_required = recovery
        if recovery.is_set():
            self._publish_remote_connection_state("disconnected")
            return
        self.remote_shutdown_requested.clear()
        self.remote_monitor = RemoteConnectionMonitor(
            is_healthy=self._is_remote_session_healthy,
            is_process_alive=self._is_remote_process_alive,
            reconnect_once=self._reconnect_remote_once,
            publish_state=self._publish_remote_connection_state,
            reload_view=self._reload_workstack_after_reconnect,
            is_recovery_required=recovery.is_set,
            on_recovery_required=self._fail_closed_remote_authority,
        )
        self.remote_monitor.start()

    def _stop_remote_monitor(self) -> None:
        shutdown = getattr(self, "remote_shutdown_requested", None)
        if shutdown is not None:
            shutdown.set()
        monitor = getattr(self, "remote_monitor", None)
        self.remote_monitor = None
        if monitor is not None:
            monitor.stop(timeout=5)

    def _is_remote_process_alive(self) -> bool:
        process = self.remote_ssh_process
        return process is not None and process.poll() is None

    def _remote_authority_guard(self) -> threading.RLock:
        lock = getattr(self, "remote_authority_lock", None)
        if lock is None:
            lock = threading.RLock()
            self.remote_authority_lock = lock
        return lock

    def _rebind_coordination_active_locked(self) -> bool:
        target = getattr(self, "remote_rebind_target", "")
        deadline = float(getattr(self, "remote_rebind_deadline", 0.0))
        if not target:
            return False
        if time.monotonic() <= deadline:
            return True
        self.remote_rebind_target = ""
        self.remote_rebind_deadline = 0.0
        return False

    def _begin_remote_workspace_rebind(self, workspace_id: str) -> None:
        target = self._validated_rebound_workspace_id(workspace_id)
        if self.remote_profile is None:
            self._begin_local_workspace_rebind(target)
            return
        with self._remote_authority_guard():
            self.remote_rebind_target = target
            self.remote_rebind_deadline = time.monotonic() + REMOTE_REBIND_COORDINATION_SECONDS

    def _begin_local_workspace_rebind(self, workspace_id: str) -> None:
        """Record the local rebind candidate without changing any authority.

        Nothing is adopted here. The candidate, the profile that was selected at
        start, the identity it currently carries, its directory and the registry
        digest the host is bound to are captured so that completion can prove it
        is completing the same rebind against unchanged state.
        """

        target = self._validated_rebound_workspace_id(workspace_id)
        selection = getattr(self, "local_startup_selection", None)
        if selection is None:
            raise RuntimeError("Local workspace rebind requires an active local profile")
        if target == selection.expected_workspace_id:
            raise RuntimeError("Local workspace rebind requires a changed workspace identity")
        with self._remote_authority_guard():
            self.local_rebind_start = {
                "candidate_workspace_id": target,
                "profile_id": selection.profile_id,
                "previous_workspace_id": selection.expected_workspace_id,
                "data_dir": str(selection.data_dir),
                "registry_digest": self.connection_registry_digest,
                "deadline": time.monotonic() + REMOTE_REBIND_COORDINATION_SECONDS,
            }

    def _local_rebind_start_locked(self, workspace_id: str) -> dict[str, object]:
        start = getattr(self, "local_rebind_start", None)
        if not isinstance(start, dict):
            raise RuntimeError("Local workspace rebind was not started")
        if time.monotonic() > float(start["deadline"]):
            self.local_rebind_start = None
            raise RuntimeError("Local workspace rebind coordination expired; start it again")
        if start["candidate_workspace_id"] != workspace_id:
            raise RuntimeError(
                f"Local workspace rebind completed for {workspace_id}, "
                f"but coordination expected {start['candidate_workspace_id']}"
            )
        return start

    def _complete_local_workspace_rebind(self, workspace_id: str) -> None:
        """Persist the confirmed local identity, or refuse without writing.

        Completion is admitted only when a matching start exists and the Store's
        own persisted rebind evidence, re-read independently here, shows that the
        confirmed rebind really happened for exactly this previous and candidate
        identity. The registry update itself is a digest compare-and-swap under
        the existing mutation lock, so a newer profile selection or metadata edit
        refuses instead of being overwritten.
        """

        expected = self._validated_rebound_workspace_id(workspace_id)
        with self._remote_authority_guard():
            start = self._local_rebind_start_locked(expected)
            # Pre-CAS. Anything that fails here has written nothing.
            confirmed = read_confirmed_local_rebind(
                start["data_dir"],
                expected_previous_workspace_id=str(start["previous_workspace_id"]),
                expected_candidate_workspace_id=expected,
            )
            result = rebind_active_local_workspace(
                self.state_root,
                expected_registry_digest=str(start["registry_digest"]),
                expected_profile_id=str(start["profile_id"]),
                expected_previous_workspace_id=confirmed.previous_workspace_id,
                # Bind the directory that was actually verified, not merely the
                # one recorded at start.
                expected_data_dir=str(confirmed.verified_data_dir),
                observed_workspace_id=confirmed.candidate_workspace_id,
                confirmation_workspace_id=expected,
            )
            # Past this point the registry is committed and is the authority.
            # Align host state with it and consume the coordination BEFORE any
            # derived artefact is written, so a later failure cannot leave the
            # host describing an authority that no longer exists.
            self._adopt_committed_local_rebind(result)
            try:
                export_active_legacy_mirror(
                    self.state_root, expected_registry_digest=result.registry_digest
                )
            except (RuntimeError, OSError) as error:
                raise LocalRebindMirrorError(
                    "The workspace identity was saved to the connection registry, "
                    "but the generated legacy connection mirror could not be "
                    f"rewritten ({error}). The saved identity is in effect; restart "
                    "Work Stack, and the mirror is regenerated on the next "
                    "successful registry write."
                ) from error

    def _adopt_committed_local_rebind(self, result: object) -> None:
        """Make host state describe the registry that was just committed."""

        self.connection_registry_snapshot = result.registry
        self.connection_registry_digest = result.registry_digest
        selection = getattr(self, "local_startup_selection", None)
        if selection is not None:
            self.local_startup_selection = replace(
                selection, expected_workspace_id=result.current_workspace_id
            )
        self.local_rebind_start = None

    def _post_remote_rebind_ready(self, workspace_id: str) -> None:
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        core.PostWebMessageAsJson(json.dumps({
            "type": "workstack-ssot-rebind-ready",
            "workspace_id": workspace_id,
        }, ensure_ascii=True, separators=(",", ":")))

    def _clear_remote_rebind_coordination_locked(self) -> None:
        self.remote_rebind_target = ""
        self.remote_rebind_deadline = 0.0

    def _mark_remote_recovery_required(self, error: RemoteAuthorityMismatch) -> None:
        self.remote_recovery_message = str(error)
        recovery = getattr(self, "remote_recovery_required", None)
        if recovery is None:
            recovery = threading.Event()
            self.remote_recovery_required = recovery
        recovery.set()

    def _is_remote_session_healthy(self) -> bool:
        with self._remote_authority_guard():
            if self._rebind_coordination_active_locked():
                return self._is_ready()
            try:
                self._verify_remote_workspace(require_runtime_protocol=True, timeout=1.5)
            except RemoteAuthorityMismatch as error:
                self._mark_remote_recovery_required(error)
                return False
            except RuntimeError:
                return False
        return True

    def _fail_closed_remote_authority(self) -> None:
        self.remote_shutdown_requested.set()
        self._stop_owned_remote_connection()

    def _remote_recovery_is_required(self) -> bool:
        recovery = getattr(self, "remote_recovery_required", None)
        return recovery is not None and recovery.is_set()

    def _reconnect_remote_once(self, wait_for_active: bool = False) -> bool:
        reconnect_lock = getattr(self, "remote_reconnect_lock", None)
        if reconnect_lock is not None and not reconnect_lock.acquire(blocking=wait_for_active):
            self._trace("SSH reconnect skipped because another reconnect is active")
            return False
        try:
            if wait_for_active and self._is_remote_process_alive() and self._is_remote_session_healthy():
                return True
            if self._remote_recovery_is_required():
                self._fail_closed_remote_authority()
                return False
            return self._replace_remote_connection()
        finally:
            if reconnect_lock is not None:
                reconnect_lock.release()

    def _replace_remote_connection(self) -> bool:
        if (
            self.remote_profile is None
            or self.remote_shutdown_requested.is_set()
            or self._remote_recovery_is_required()
        ):
            return False
        self._stop_owned_remote_connection()
        if self.remote_shutdown_requested.is_set():
            return False
        try:
            self._ensure_remote_server()
        except RuntimeError as error:
            self._trace(f"SSH reconnect attempt failed: {error}")
            return False
        if self.remote_shutdown_requested.is_set():
            self._stop_owned_remote_connection()
            return False
        return True

    def _run_manual_remote_reconnect(self) -> None:
        self._publish_remote_connection_state("reconnecting")
        if not self._reconnect_remote_once(wait_for_active=True):
            self._publish_remote_connection_state("disconnected")
            return
        self._publish_remote_connection_state("ready")
        self._reload_workstack_after_reconnect()

    def _start_manual_remote_reconnect(self) -> None:
        if self.remote_profile is None:
            self._post_ssot_status(self._ssot_status_payload(
                self.active_connection_draft,
                "error",
                message="Activate a saved Remote SSH workspace before reconnecting.",
                restart_required=False,
            ))
            return
        threading.Thread(target=self._run_manual_remote_reconnect, daemon=True).start()

    def _open_ssot_diagnostics(self) -> None:
        diagnostics = self.state_root / "desktop-launch"
        diagnostics.mkdir(parents=True, exist_ok=True)
        if os.name != "nt" or not hasattr(os, "startfile"):
            self._trace(f"SSOT diagnostics folder: {diagnostics}")
            return
        try:
            os.startfile(str(diagnostics))
        except OSError as error:
            self._trace(f"Could not open SSOT diagnostics folder: {error}")

    def _publish_remote_connection_state(self, state: str) -> None:
        messages = {
            "ready": "Remote SSOT connection is healthy.",
            "reconnecting": "Remote SSOT connection was interrupted. Reconnecting safely...",
            "disconnected": "Remote SSOT reconnection was exhausted. Review SSH diagnostics, then use Reconnect now.",
        }
        recovery = getattr(self, "remote_recovery_required", None)
        if state == "disconnected" and recovery is not None and recovery.is_set():
            messages[state] = (
                f"Remote SSOT authority changed: {getattr(self, 'remote_recovery_message', 'identity mismatch')}. "
                "Automatic reconnect is blocked. Verify the remote Workspace ID in SSOT connection settings and restart."
            )
        payload = self._ssot_status_payload(
            self.active_connection_draft,
            state,
            message=messages.get(state, "Remote SSOT state changed."),
        )
        self._dispatch_ssot_status(payload)

    def _reload_workstack_after_reconnect(self) -> None:
        if self.form is None:
            return
        try:
            from System import Action

            def reload_view() -> None:
                self._deactivate_source()
                core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
                if core is not None:
                    core.Reload()

            dispatch = Action(reload_view)
            self.source_event_handlers.append(dispatch)
            self.form.BeginInvoke(dispatch)
        except Exception as error:
            self._trace(f"Work Stack reload after SSH reconnect failed: {type(error).__name__}")

    @staticmethod
    def _decode_ssot_draft(encoded: str) -> dict[str, object]:
        if not encoded or len(encoded) > 32_768:
            raise RuntimeError("Connection draft is missing or too large")
        try:
            decoded = urllib.parse.unquote(encoded, encoding="utf-8", errors="strict")
            raw = json.loads(decoded)
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Connection draft is not valid URL-encoded JSON") from error
        return validate_connection_draft(raw)

    def _test_ssot_connection(self, raw: object) -> dict[str, object]:
        draft = validate_connection_draft(raw)
        active_draft = getattr(self, "active_connection_draft", draft)
        profile = connection_profile_from_draft(draft)
        if profile is not None:
            run_remote_connection_check(profile)
        return self._ssot_status_payload(
            draft,
            "ready",
            message=(
                "Connection prerequisites passed. Save settings, then restart Work Stack to activate this connection."
                if draft != active_draft
                else "Connection prerequisites passed. This connection is already active."
            ),
            restart_required=draft != active_draft,
        )

    def _run_ssot_test(self, draft: dict[str, object]) -> None:
        try:
            payload = self._test_ssot_connection(draft)
        except RuntimeError as error:
            payload = self._ssot_status_payload(
                draft,
                "error",
                message=str(error),
                restart_required=False,
            )
        self._dispatch_ssot_status(payload)

    @staticmethod
    def _validated_rebound_workspace_id(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError("Remote workspace rebind result is not a canonical UUID") from error
        if str(parsed) != value or parsed.int == 0:
            raise RuntimeError("Remote workspace rebind result is not a canonical non-zero UUID")
        return value

    def _coordinate_remote_workspace_rebind(self, workspace_id: str) -> None:
        """Commit the verified remote identity to the local profile.

        The remote server has already atomically committed its manifest before the
        frontend reports success.  The desktop re-reads that authority over the
        active tunnel, then atomically replaces only remote-connection.json.
        """

        expected = self._validated_rebound_workspace_id(workspace_id)
        if self.remote_profile is None or self.active_connection_draft.get("storage_mode") != "ssh-remote":
            raise RuntimeError("Remote workspace rebind cannot update a local SSOT connection")
        with self._remote_authority_guard():
            coordinated_target = getattr(self, "remote_rebind_target", "")
            if coordinated_target and coordinated_target != expected:
                raise RuntimeError(
                    f"Remote workspace rebind completed for {expected}, but coordination expected {coordinated_target}"
                )
            metadata = self._read_remote_storage_metadata()
            actual = metadata["workspace_id"]
            if actual != expected:
                raise RuntimeError(
                    f"Remote workspace rebind reported {expected}, but the server now reports {actual}; "
                    "the local connection was not changed"
                )
            self._require_supported_remote_protocol(metadata)
            runtime_port = self.remote_profile.local_forward_port
            next_draft = {**self.active_connection_draft, "workspace_id": expected}
            if getattr(self, "connection_registry_startup_enabled", False):
                previous = self.remote_profile.workspace_id
                result = rebind_active_remote_workspace(
                    self.state_root,
                    expected_registry_digest=self.connection_registry_digest,
                    expected_profile_id=self.runtime_connection_profile_id,
                    expected_previous_workspace_id=previous,
                    observed_workspace_id=actual,
                    confirmation_workspace_id=expected,
                )
                self.connection_registry_snapshot = result.registry
                self.connection_registry_digest = result.registry_digest
                export_active_legacy_mirror(
                    self.state_root,
                    expected_registry_digest=result.registry_digest,
                )
                saved = next_draft
            else:
                saved = save_connection_draft(self.state_root, next_draft)
            saved_profile = connection_profile_from_draft(saved)
            if saved_profile is None:
                raise RuntimeError("Remote workspace rebind produced a local connection unexpectedly")
            self.active_connection_draft = saved
            self.remote_profile = replace(saved_profile, local_forward_port=runtime_port)
            self._remember_remote_metadata(metadata)
            self._clear_remote_rebind_coordination_locked()
            recovery = getattr(self, "remote_recovery_required", None)
            if recovery is not None:
                recovery.clear()
            self.remote_recovery_message = ""
        self._dispatch_ssot_status(self._ssot_status_payload(
            saved,
            "ready",
            message="Remote workspace identity was verified and saved. Reconnects will use the new identity.",
            restart_required=False,
        ))

    def _run_remote_workspace_rebind(self, workspace_id: str) -> None:
        try:
            self._coordinate_remote_workspace_rebind(workspace_id)
        except RuntimeError as error:
            # The remote authority may already have committed.  Stop the owned
            # tunnel so an uncoordinated local profile cannot continue writing.
            with self._remote_authority_guard():
                self._clear_remote_rebind_coordination_locked()
            self._stop_remote_monitor()
            self._stop_owned_remote_connection()
            self._dispatch_ssot_status(self._ssot_status_payload(
                self.active_connection_draft,
                "error",
                message=(
                    f"Remote workspace changed, but Work Stack could not coordinate the local profile: {error}. "
                    "Enter the verified remote Workspace ID in SSOT connection settings and restart."
                ),
                restart_required=True,
            ))

    def _start_remote_workspace_rebind(self, workspace_id: str) -> None:
        if self.remote_profile is None:
            # A local completion is a bounded local file operation, not a tunnel
            # round trip, so it runs inline and its outcome is reported to the
            # frontend rather than being deferred to a worker thread.
            try:
                self._complete_local_workspace_rebind(workspace_id)
            except LocalRebindMirrorError as error:
                # Partial completion: the authority IS saved. Say so, and still
                # require the restart that activates it, rather than implying
                # nothing was persisted.
                self._post_ssot_status(self._ssot_status_payload(
                    self.active_connection_draft,
                    "error",
                    message=str(error),
                    restart_required=True,
                ))
                return
            except (RuntimeError, OSError, ValueError) as error:
                # Refused before the registry write: nothing was persisted.
                self._post_ssot_status(self._ssot_status_payload(
                    self.active_connection_draft,
                    "error",
                    message=str(error),
                    restart_required=False,
                ))
                return
            self._post_ssot_status(self._ssot_status_payload(
                self.active_connection_draft,
                "saved",
                message="Workspace identity confirmed. Restart Work Stack to use it.",
                restart_required=True,
            ))
            return
        threading.Thread(
            target=self._run_remote_workspace_rebind,
            args=(workspace_id,),
            name="WorkStackRemoteWorkspaceRebind",
            daemon=True,
        ).start()

    def _handle_ssot_message(self, message: str) -> None:
        if message == f"{SSOT_HOST_PREFIX}|status":
            self._post_ssot_current_status()
            return
        if message == f"{SSOT_HOST_PREFIX}|reconnect":
            self._start_manual_remote_reconnect()
            return
        if message == f"{SSOT_HOST_PREFIX}|open-diagnostics":
            self._open_ssot_diagnostics()
            return
        rebind_start_prefix = f"{SSOT_HOST_PREFIX}|rebind-start|"
        if message.startswith(rebind_start_prefix):
            workspace_id = message[len(rebind_start_prefix):]
            try:
                self._begin_remote_workspace_rebind(workspace_id)
                self._post_remote_rebind_ready(workspace_id)
            except RuntimeError as error:
                self._post_ssot_status(self._ssot_status_payload(
                    self.active_connection_draft,
                    "error",
                    message=str(error),
                    restart_required=False,
                ))
            return
        rebind_prefix = f"{SSOT_HOST_PREFIX}|rebind-complete|"
        if message.startswith(rebind_prefix):
            workspace_id = message[len(rebind_prefix):]
            try:
                workspace_id = self._validated_rebound_workspace_id(workspace_id)
            except RuntimeError as error:
                self._post_ssot_status(self._ssot_status_payload(
                    self.active_connection_draft,
                    "error",
                    message=str(error),
                    restart_required=False,
                ))
                return
            self._start_remote_workspace_rebind(workspace_id)
            return
        parts = message.split("|", 2)
        if len(parts) != 3 or parts[0] != SSOT_HOST_PREFIX or parts[1] not in {"test", "save"}:
            return
        try:
            draft = self._decode_ssot_draft(parts[2])
            if parts[1] == "save":
                saved = save_connection_draft(self.state_root, draft)
                restart_required = saved != self.active_connection_draft
                self._post_ssot_status(self._ssot_status_payload(
                    saved,
                    "saved",
                    message=(
                        "Connection saved. Restart Work Stack to activate it."
                        if restart_required
                        else "Connection is already active."
                    ),
                    restart_required=restart_required,
                ))
                return
            self._post_ssot_status(self._ssot_status_payload(
                draft,
                "testing",
                message="Checking connection prerequisites...",
                restart_required=False,
            ))
            worker = threading.Thread(target=self._run_ssot_test, args=(draft,), daemon=True)
            worker.start()
        except RuntimeError as error:
            self._post_ssot_status(self._ssot_status_payload(
                self.active_connection_draft,
                "error",
                message=str(error),
                restart_required=False,
            ))

    def _set_source_zoom(self, message: str) -> None:
        parts = message.split("|")
        if len(parts) != 4:
            return
        _prefix, command, provider, raw_value = parts
        if command != "zoom" or provider not in PROVIDER_URLS:
            return
        try:
            value = int(raw_value)
        except ValueError:
            return
        if not SOURCE_ZOOM_MIN <= value <= SOURCE_ZOOM_MAX:
            return
        self.source_zoom[provider] = value
        save_source_zoom(self.state_root, self.source_zoom)
        source_webview = self.source_webviews.get(provider)
        if source_webview is not None:
            source_webview.ZoomFactor = value / 100.0
        self._post_source_zoom()

    def _update_payload(self) -> dict[str, object]:
        return {
            **self.update_status,
            "preferences": {
                "auto_check": self.update_preferences.auto_check,
                "auto_download": self.update_preferences.auto_download,
                "install_on_exit": self.update_preferences.install_on_exit,
            },
        }

    def _post_update_status(self) -> None:
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        core.PostWebMessageAsJson(json.dumps(self._update_payload(), ensure_ascii=True, separators=(",", ":")))

    def _dispatch_update_status(self) -> None:
        if self.form is None:
            return
        try:
            from System import Action

            dispatch = Action(self._post_update_status)
            self.source_event_handlers.append(dispatch)
            self.form.BeginInvoke(dispatch)
        except Exception as error:
            self._trace(f"update status dispatch failed: {type(error).__name__}")

    def _set_update_status(
        self,
        state: str,
        *,
        latest_version: str = "",
        release_url: str = "",
        message: str = "",
    ) -> None:
        self.update_status = {
            "type": "workstack-update-status",
            "state": state,
            "current_version": WORKSTACK_VERSION,
            "latest_version": latest_version or str(self.update_status.get("latest_version", WORKSTACK_VERSION)),
            "release_url": release_url or str(self.update_status.get("release_url", "")),
            "message": message[:500],
        }
        self._dispatch_update_status()

    def _start_update_check(self, *, force_download: bool = False) -> None:
        if self.update_check_thread is not None and self.update_check_thread.is_alive():
            return
        self._set_update_status("checking", message="Checking the stable GitHub release channel")
        self.update_check_thread = threading.Thread(
            target=self._check_update_worker,
            kwargs={"force_download": force_download},
            name="WorkStackUpdateCheck",
            daemon=True,
        )
        self.update_check_thread.start()

    def _check_update_worker(self, *, force_download: bool) -> None:
        try:
            body = fetch_url_bytes(UPDATE_MANIFEST_URL, MAX_MANIFEST_BYTES)
            manifest = parse_update_manifest(body, current_version=WORKSTACK_VERSION)
            if not manifest.is_newer:
                self.downloaded_update = None
                self.install_update_on_exit = False
                self._set_update_status(
                    "current",
                    latest_version=manifest.version,
                    release_url=manifest.release_url,
                    message="Work Stack is up to date",
                )
                return
            if self.remote_profile is not None:
                try:
                    metadata = self._read_remote_storage_metadata()
                    self._require_supported_remote_protocol(
                        metadata,
                        minimum=manifest.minimum_remote_protocol,
                        purpose=f"update to Work Stack {manifest.version}",
                    )
                    self._remember_remote_metadata(metadata)
                except RuntimeError as error:
                    self._set_update_status(
                        "blocked",
                        latest_version=manifest.version,
                        release_url=manifest.release_url,
                        message=str(error),
                    )
                    return
            if not (force_download or self.update_preferences.auto_download):
                self._set_update_status(
                    "available",
                    latest_version=manifest.version,
                    release_url=manifest.release_url,
                    message="A verified Work Stack update is available",
                )
                return
            self._set_update_status(
                "downloading",
                latest_version=manifest.version,
                release_url=manifest.release_url,
                message="Downloading and verifying the update",
            )
            downloaded = download_update(manifest, self.state_root / "updates")
            self.downloaded_update = downloaded
            self.install_update_on_exit = self.update_preferences.install_on_exit
            self._set_update_status(
                "ready",
                latest_version=manifest.version,
                release_url=manifest.release_url,
                message=(
                    "Verified update will install when Work Stack closes"
                    if self.install_update_on_exit
                    else "Verified update is ready to install"
                ),
            )
        except OlderUpdateManifest as error:
            self.downloaded_update = None
            self.install_update_on_exit = False
            self._set_update_status(
                "current",
                latest_version=error.version,
                release_url=(
                    "https://github.com/Shinick-Han/work-stack-public/releases/tag/"
                    f"v{error.version}"
                ),
                message=(
                    f"Installed Work Stack {error.installed_version} is newer than "
                    f"the stable channel {error.version}"
                ),
            )
        except Exception as error:
            self.downloaded_update = None
            self.install_update_on_exit = False
            self._set_update_status("error", message=f"Update check failed: {error}")

    def _handle_update_message(self, message: str) -> None:
        parts = message.split("|")
        if parts == [UPDATE_HOST_PREFIX, "status"]:
            self._post_update_status()
            return
        if parts == [UPDATE_HOST_PREFIX, "check"]:
            self._start_update_check()
            return
        if parts == [UPDATE_HOST_PREFIX, "download"]:
            self._start_update_check(force_download=True)
            return
        if parts == [UPDATE_HOST_PREFIX, "install"]:
            if self.downloaded_update is None:
                self._start_update_check(force_download=True)
                return
            self.install_update_on_exit = True
            self._set_update_status(
                "installing",
                latest_version=self.downloaded_update.version,
                release_url=self.downloaded_update.release_url,
                message="Closing Work Stack to apply the verified update",
            )
            if self.window is not None:
                self.window.destroy()
            return
        if len(parts) == 5 and parts[:2] == [UPDATE_HOST_PREFIX, "preferences"]:
            values = parts[2:]
            if any(value not in {"0", "1"} for value in values):
                return
            self.update_preferences = UpdatePreferences(*(value == "1" for value in values))
            save_update_preferences(self.state_root, self.update_preferences)
            if not self.update_preferences.install_on_exit:
                self.install_update_on_exit = False
            elif self.downloaded_update is not None:
                self.install_update_on_exit = True
            self._post_update_status()
            if self.update_preferences.auto_check and self.update_status.get("state") == "idle":
                self._start_update_check()
            return
        if parts == [UPDATE_HOST_PREFIX, "open-release"]:
            release_url = str(self.update_status.get("release_url", ""))
            if release_url.startswith("https://github.com/Shinick-Han/work-stack-public/releases/"):
                webbrowser.open(release_url)

    def _launch_pending_update(self) -> None:
        downloaded = self.downloaded_update
        if not self.install_update_on_exit or downloaded is None:
            return
        apply_script = self.install_root / "scripts" / "windows" / "Apply-WorkStackUpdate.ps1"
        if not apply_script.is_file():
            self._trace("verified update was not launched because the update applicator is missing")
            return
        command = [
            "powershell.exe",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(apply_script),
            "-SetupPath",
            str(downloaded.setup_path),
            "-ChecksumPath",
            str(downloaded.checksum_path),
            "-InstallRoot",
            str(self.install_root),
            "-StateRoot",
            str(self.state_root),
            "-ParentProcessId",
            str(os.getpid()),
            "-TargetVersion",
            downloaded.version,
        ]
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        try:
            subprocess.Popen(
                command,
                cwd=self.install_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creation_flags,
            )
            self._trace(f"verified Work Stack {downloaded.version} update applicator started")
        except OSError as error:
            self._trace(f"verified update applicator failed to start: {type(error).__name__}: {error}")

    def _send_source_capture(self, message: str) -> None:
        request = parse_source_capture_request(message)
        if request is None:
            return
        provider, request_id = request
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        capture = self._source_capture_seed(provider)
        if provider == "outlook" and self._begin_outlook_visible_capture(request_id, capture):
            return
        self._post_source_draft(provider, request_id, capture)

    def _source_capture_seed(self, provider: str) -> dict[str, str]:
        source_webview = self.source_webviews.get(provider)
        candidate = str(source_webview.Source) if source_webview is not None and source_webview.Source is not None else ""
        url = candidate[:4096] if candidate and self._is_microsoft_url(candidate, provider) else ""
        document_title = ""
        if source_webview is not None and source_webview.CoreWebView2 is not None:
            document_title = str(source_webview.CoreWebView2.DocumentTitle or "").strip()[:500]
        return {"url": url, "title": document_title, "text": self._clipboard_capture_text()}

    def _clipboard_capture_text(self) -> str:
        clipboard_text = ""
        try:
            from System.Windows.Forms import Clipboard

            if Clipboard.ContainsText():
                clipboard_text = str(Clipboard.GetText()).strip()[:4000]
        except Exception as error:
            self._trace(f"explicit clipboard capture is unavailable: {type(error).__name__}: {error}")
        return clipboard_text

    def _begin_outlook_visible_capture(self, request_id: str, capture: dict[str, str]) -> bool:
        source_webview = self.source_webviews.get("outlook")
        core = source_webview.CoreWebView2 if source_webview is not None else None
        if core is None:
            return False
        if len(self.pending_source_captures) >= 16:
            self.pending_source_captures.pop(next(iter(self.pending_source_captures)))
        self.pending_source_captures[request_id] = capture
        script = OUTLOOK_VISIBLE_CAPTURE_SCRIPT.replace("__REQUEST_ID__", json.dumps(request_id))
        try:
            core.ExecuteScriptAsync(script)
            return True
        except Exception as error:
            self.pending_source_captures.pop(request_id, None)
            self._trace(f"visible Outlook capture is unavailable: {type(error).__name__}: {error}")
            return False

    def _on_source_message(self, provider: str, _sender, event_args) -> None:
        if provider != "outlook" or not self._is_microsoft_url(str(event_args.Source), provider):
            return
        try:
            payload = json.loads(str(event_args.TryGetWebMessageAsString()))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("type") != "workstack-outlook-visible-capture":
            return
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            return
        capture = self.pending_source_captures.pop(request_id, None)
        if capture is None:
            return
        title = payload.get("title")
        text = payload.get("text")
        if isinstance(title, str) and title.strip():
            capture["title"] = title.strip()[:500]
        if isinstance(text, str) and text.strip():
            capture["text"] = text.strip()[:4000]
        self._post_source_draft(provider, request_id, capture)

    def _post_source_draft(self, provider: str, request_id: str, capture: dict[str, str]) -> None:
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        core.PostWebMessageAsJson(json.dumps({
            "type": "workstack-source-draft",
            "request_id": request_id,
            "provider": provider,
            "url": capture["url"],
            "title": capture["title"],
            "text": capture["text"],
        }, ensure_ascii=False, separators=(",", ":")))

    def _on_workstack_navigation_starting(self, _sender, event_args) -> None:
        if getattr(self, "startup_recovery_status", None) is not None:
            target = str(event_args.Uri)
            if target == "about:blank" or target.startswith("data:text/html"):
                return
        if self._origin(str(event_args.Uri)) != self.workstack_origin:
            event_args.Cancel = True

    def _on_source_navigation_starting(self, provider: str, sender, event_args) -> None:
        target = str(event_args.Uri)
        parts = urllib.parse.urlsplit(target)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        microsoft_target = self._is_microsoft_url(target, provider)
        if microsoft_target and host in COMMON_AUTH_HOSTS:
            self.source_auth_sessions.add(provider)
        allowed = microsoft_target or (scheme == "https" and provider in self.source_auth_sessions)
        if allowed and self._is_provider_host(host, provider):
            self.source_auth_sessions.discard(provider)
        self._record_microsoft_diagnostic("navigation-starting", provider, target, allowed=allowed)
        if not allowed:
            event_args.Cancel = True

    def _on_source_new_window(self, provider: str, parent_core, event_args) -> None:
        target = str(event_args.Uri)
        parent_url = str(parent_core.Source)
        target_scheme = urllib.parse.urlsplit(target).scheme.lower()
        parent_host = (urllib.parse.urlsplit(parent_url).hostname or "").lower()
        blank_target = target in {"", "about:blank"}
        is_auth_popup = self._is_microsoft_url(target, provider) or (
            blank_target and self._is_microsoft_url(parent_url, provider)
        ) or (
            target_scheme == "https"
            and (parent_host in COMMON_AUTH_HOSTS or provider in self.source_auth_sessions)
        )
        self._record_microsoft_diagnostic(
            "new-window",
            provider,
            target or parent_url,
            allowed=is_auth_popup,
            stage="embedded-popup" if is_auth_popup else "external-or-blocked",
        )
        if is_auth_popup:
            self._open_source_popup(provider, parent_core, event_args)
            return
        event_args.Handled = True
        if urllib.parse.urlsplit(target).scheme.lower() == "https":
            webbrowser.open(target)

    def _open_source_popup(self, provider: str, parent_core, event_args) -> None:
        import System.Windows.Forms as WinForms
        from Microsoft.Web.WebView2.WinForms import WebView2
        from System.Drawing import Color

        viewport = self.source_viewports.get(provider)
        if viewport is None:
            event_args.Handled = True
            return

        popup_id = uuid.uuid4().hex
        deferral = event_args.GetDeferral()
        overlay = WinForms.Panel()
        overlay.Dock = WinForms.DockStyle.Fill
        overlay.BackColor = Color.FromArgb(*theme_rgb(self.current_theme, "native.overlay"))
        toolbar = WinForms.Panel()
        toolbar.Dock = WinForms.DockStyle.Top
        toolbar.Height = 38
        toolbar.BackColor = Color.FromArgb(*theme_rgb(self.current_theme, "native.toolbar"))
        label = WinForms.Label()
        label.Text = "Microsoft sign-in"
        label.ForeColor = Color.FromArgb(*theme_rgb(self.current_theme, "native.text"))
        label.AutoSize = True
        label.Left = 12
        label.Top = 11
        close_button = WinForms.Button()
        close_button.Text = "Close"
        close_button.Width = 72
        close_button.Height = 28
        close_button.Top = 5
        close_button.Left = max(80, viewport.ClientSize.Width - 82)
        close_button.Anchor = WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Right
        child = WebView2()
        child.Dock = WinForms.DockStyle.Fill
        child.DefaultBackgroundColor = Color.FromArgb(*theme_rgb(self.current_theme, "native.externalLoading"))
        toolbar.Controls.Add(label)
        toolbar.Controls.Add(close_button)
        overlay.Controls.Add(child)
        overlay.Controls.Add(toolbar)
        viewport.Controls.Add(overlay)
        overlay.BringToFront()

        state = {"completed": False, "closed": False}

        def close_popup(_sender=None, _args=None) -> None:
            if state["closed"]:
                return
            state["closed"] = True
            if not state["completed"]:
                event_args.Handled = True
                deferral.Complete()
                state["completed"] = True
            self._record_microsoft_diagnostic("popup-closed", provider)
            self.source_popups.pop(popup_id, None)
            overlay.Dispose()

        def popup_navigation_starting(_sender, args) -> None:
            target = str(args.Uri)
            scheme = urllib.parse.urlsplit(target).scheme.lower()
            allowed = target == "about:blank" or scheme == "https"
            self._record_microsoft_diagnostic("popup-navigation-starting", provider, target, allowed=allowed)
            if not allowed:
                args.Cancel = True

        def popup_navigation_completed(sender, args) -> None:
            self._record_microsoft_diagnostic(
                "popup-navigation-completed",
                provider,
                str(sender.Source),
                success=bool(args.IsSuccess),
                web_error=str(args.WebErrorStatus),
            )

        def popup_ready(sender, args) -> None:
            if state["closed"] or state["completed"]:
                return
            if not args.IsSuccess:
                self._record_microsoft_diagnostic("popup-ready", provider, success=False)
                event_args.Handled = True
                deferral.Complete()
                state["completed"] = True
                close_popup()
                return
            settings = sender.CoreWebView2.Settings
            settings.AreDevToolsEnabled = False
            settings.AreDefaultContextMenusEnabled = True
            settings.IsPasswordAutosaveEnabled = False
            settings.IsGeneralAutofillEnabled = False
            window_close = lambda source, close_args: close_popup(source, close_args)
            nested_window = lambda source, window_args: self._on_source_new_window(provider, source, window_args)
            external_uri = lambda source, uri_args: self._on_source_external_uri(provider, source, uri_args)
            sender.CoreWebView2.WindowCloseRequested += window_close
            sender.CoreWebView2.NewWindowRequested += nested_window
            sender.CoreWebView2.LaunchingExternalUriScheme += external_uri
            self.source_event_handlers.extend((window_close, nested_window, external_uri))
            event_args.NewWindow = sender.CoreWebView2
            event_args.Handled = True
            deferral.Complete()
            state["completed"] = True
            self._record_microsoft_diagnostic("popup-ready", provider, success=True)

        close_button.Click += close_popup
        child.NavigationStarting += popup_navigation_starting
        child.NavigationCompleted += popup_navigation_completed
        child.CoreWebView2InitializationCompleted += popup_ready
        handlers = (close_popup, popup_navigation_starting, popup_navigation_completed, popup_ready)
        self.source_event_handlers.extend(handlers)
        self.source_popups[popup_id] = {
            "overlay": overlay,
            "toolbar": toolbar,
            "label": label,
            "webview": child,
            "handlers": handlers,
        }
        try:
            child.EnsureCoreWebView2Async(parent_core.Environment)
        except Exception:
            self._record_microsoft_diagnostic("popup-start", provider, success=False)
            close_popup()

    def _on_source_external_uri(self, provider: str, sender, event_args) -> None:
        scheme = urllib.parse.urlsplit(str(event_args.Uri)).scheme.lower()
        self._record_microsoft_diagnostic(
            "external-uri",
            provider,
            str(event_args.Uri),
            allowed=scheme != "msteams",
        )
        if scheme == "msteams":
            event_args.Cancel = True
            self._trace(f"blocked Teams desktop protocol launch from {provider}")
            if provider == "teams":
                sender.Navigate(PROVIDER_URLS["teams"])

    def _on_source_navigation_completed(self, provider: str, sender, event_args) -> None:
        self._record_microsoft_diagnostic(
            "navigation-completed",
            provider,
            str(sender.Source),
            success=bool(event_args.IsSuccess),
            web_error=str(event_args.WebErrorStatus),
        )
        if self.probe_recorded or self.options.probe_result is None or provider != self.options.probe_provider:
            return
        host = urllib.parse.urlsplit(str(sender.Source)).hostname or "initializing"
        self._write_probe(bool(event_args.IsSuccess), host, str(event_args.WebErrorStatus))
        if self.window is not None:
            self.window.destroy()

    def _write_probe(self, success: bool, host: str, web_error: str) -> None:
        if self.probe_recorded or self.options.probe_result is None:
            return
        self.probe_recorded = True
        result = (
            f"success={str(success).lower()}\n"
            f"host={host}\n"
            f"web_error={web_error}\n"
            f"runtime={self.runtime_version}\n"
        )
        self.options.probe_result.parent.mkdir(parents=True, exist_ok=True)
        self.options.probe_result.write_text(result, encoding="utf-8")

    def _ensure_server(self) -> None:
        if self.remote_profile is not None:
            self._ensure_remote_server()
            return

        config, config_path = self._local_runtime_config()
        try:
            port = config["port"]
            data_path = Path(config["data_dir"]).resolve()
            backup_path = Path(config["backup_dir"]).resolve()
            retention = config["backup_retention"]
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise RuntimeError(f"Work Stack configuration is invalid: {config_path}") from error
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise RuntimeError(f"Work Stack port is invalid: {config_path}")
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
            raise RuntimeError(f"Work Stack backup retention is invalid: {config_path}")

        expected_workspace_id = self._read_local_workspace_identity(data_path)
        if self._is_ready():
            active_workspace_id = self._read_server_workspace_identity()
            if expected_workspace_id is not None and active_workspace_id == expected_workspace_id:
                self._trace(
                    f"server already ready for configured workspace {expected_workspace_id}; "
                    "desktop host does not own it"
                )
                return
            port = self._select_available_local_port(
                port,
                expected_workspace_id=expected_workspace_id,
                active_workspace_id=active_workspace_id,
            )
        elif self._loopback_port_listening(port):
            port = self._select_available_local_port(port)

        python_path = self.install_root / "runtime" / "python.exe"
        entry_path = self.install_root / "run_work_stack.py"
        if not python_path.is_file() or not entry_path.is_file():
            raise RuntimeError("Work Stack installation is incomplete. Re-run the installer.")

        log_path = self.state_root / "logs"
        for directory in (data_path, backup_path, log_path):
            directory.mkdir(parents=True, exist_ok=True)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if (data_path / "workspace.json").is_file():
            backup_error_path = log_path / "backup.err.log"
            with backup_error_path.open("wb") as backup_error:
                backup = subprocess.run(
                    [
                        str(python_path),
                        str(entry_path),
                        "--data-dir",
                        str(data_path),
                        "maintenance",
                        "backup",
                        "--out",
                        str(backup_path),
                    ],
                    check=False,
                    creationflags=creation_flags,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=backup_error,
                )
            if backup.returncode != 0:
                detail = backup_error_path.read_text(encoding="utf-8", errors="replace").strip()
                raise RuntimeError(f"Automatic pre-launch backup failed: {detail or 'unknown error'}")
            self._prune_backups(backup_path, retention)

        stdout_path = log_path / "server.out.log"
        stderr_path = log_path / "server.err.log"
        with stdout_path.open("wb") as server_stdout, stderr_path.open("wb") as server_stderr:
            process = subprocess.Popen(
                [
                    str(python_path),
                    str(entry_path),
                    "--data-dir",
                    str(data_path),
                    "graph",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=self.install_root,
                creationflags=creation_flags,
                stdin=subprocess.DEVNULL,
                stdout=server_stdout,
                stderr=server_stderr,
            )
        for _attempt in range(100):
            if process.poll() is not None:
                break
            if self._is_ready():
                self.server_process = process
                self.server_started_by_host = True
                self.server_pid = process.pid
                self._trace(f"server started directly by desktop host (PID {process.pid})")
                return
            time.sleep(0.05)

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(f"Work Stack did not start: {detail or 'server readiness timed out'}")

    @staticmethod
    def _read_local_workspace_identity(data_path: Path) -> str | None:
        workspace_path = data_path / "workspace.json"
        try:
            payload = json.loads(workspace_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Configured workspace identity is invalid: {workspace_path}") from error
        workspace_id = payload.get("id") if isinstance(payload, dict) else None
        try:
            parsed_workspace_id = uuid.UUID(str(workspace_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError(f"Configured workspace identity is invalid: {workspace_path}") from error
        if str(parsed_workspace_id) != workspace_id or parsed_workspace_id.int == 0:
            raise RuntimeError(f"Configured workspace identity is invalid: {workspace_path}")
        return workspace_id

    def _read_server_workspace_identity(self, *, timeout: float = 1.5) -> str | None:
        storage_url = urllib.parse.urljoin(self.workstack_url, "/api/v1/storage")
        try:
            with urllib.request.urlopen(storage_url, timeout=timeout) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        workspace_id = data.get("workspace_id") if isinstance(data, dict) else None
        try:
            parsed_workspace_id = uuid.UUID(str(workspace_id))
        except (AttributeError, TypeError, ValueError):
            return None
        if str(parsed_workspace_id) != workspace_id or parsed_workspace_id.int == 0:
            return None
        return workspace_id

    def _select_available_local_port(
        self,
        configured_port: int,
        *,
        expected_workspace_id: str | None = None,
        active_workspace_id: str | None = None,
    ) -> int:
        runtime_port = resolve_runtime_forward_port(configured_port)
        if runtime_port == configured_port:
            raise RuntimeError(f"Configured port {configured_port} changed while Work Stack was starting. Try again.")
        self.workstack_url = f"http://127.0.0.1:{runtime_port}/"
        self.workstack_origin = self._origin(self.workstack_url)
        if expected_workspace_id is not None or active_workspace_id is not None:
            self._trace(
                "configured port serves a different workspace "
                f"(expected={expected_workspace_id or 'uninitialized'}, "
                f"active={active_workspace_id or 'unverified'}); using session port {runtime_port}"
            )
        else:
            self._trace(
                f"configured port {configured_port} is occupied; using session port {runtime_port}"
            )
        return runtime_port

    @staticmethod
    def _loopback_port_listening(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.15)
            return client.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _prune_backups(backup_path: Path, retention: int) -> None:
        # Do not resolve each file here. Windows packaged-app path virtualization can
        # expose a file under LOCALAPPDATA while resolving that same file into the
        # package LocalCache tree. The glob is already rooted at the configured
        # directory, so compare normalized lexical parents and unlink only that entry.
        backup_root = Path(os.path.abspath(backup_path))
        backups = sorted(
            backup_root.glob("workstack-backup-*.zip"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for expired in backups[retention:]:
            candidate = Path(os.path.abspath(expired))
            if candidate.parent != backup_root:
                raise RuntimeError("Refusing to prune a backup outside the configured backup directory.")
            candidate.unlink()

    def _ensure_remote_server(self) -> None:
        if self.remote_profile is None:
            raise RuntimeError("Remote server startup requested without an SSH profile")
        if self._is_ready():
            raise RuntimeError(
                f"Local forward port {self.remote_profile.local_forward_port} is already serving Work Stack; "
                "close that process or choose another local_forward_port"
            )
        ssh_executable = find_ssh_executable()
        command = build_ssh_tunnel_command(self.remote_profile, ssh_executable)
        launch_root = self.state_root / "desktop-launch"
        launch_root.mkdir(parents=True, exist_ok=True)
        log_path = launch_root / "remote-ssh.log"
        self.remote_ssh_log = log_path.open("a", encoding="utf-8")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.remote_ssh_process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self.remote_ssh_log,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        except OSError as error:
            self.remote_ssh_log.close()
            self.remote_ssh_log = None
            raise RuntimeError(f"Could not start OpenSSH: {error}") from error

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.remote_ssh_process.poll() is not None:
                returncode = self.remote_ssh_process.returncode
                self._close_remote_log()
                self.remote_ssh_process = None
                raise RuntimeError(
                    f"SSH remote Work Stack exited before becoming ready (exit {returncode}). "
                    f"Review {log_path} and run --check-remote-connection."
                )
            if self._is_ready():
                self._verify_remote_workspace()
                self._trace(f"SSH remote Work Stack is ready through local port {self.remote_profile.local_forward_port}")
                return
            time.sleep(0.25)
        self._stop_owned_remote_connection()
        raise RuntimeError(
            f"SSH remote Work Stack did not become ready within 25 seconds. "
            f"Review {log_path} and run --check-remote-connection."
        )

    def _read_remote_storage_metadata(self, *, timeout: float = 3.0) -> dict[str, object]:
        if self.remote_profile is None:
            raise RuntimeError("Remote workspace verification requested without an SSH profile")
        storage_url = urllib.parse.urljoin(self.workstack_url, "/api/v1/storage")
        try:
            with urllib.request.urlopen(storage_url, timeout=timeout) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        except (OSError, UnicodeError, ValueError, urllib.error.URLError) as error:
            raise RuntimeError("Remote Work Stack storage identity could not be verified") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RemoteAuthorityMismatch("Remote Work Stack storage metadata is invalid")
        workspace_id = data.get("workspace_id")
        product_version = data.get("product_version")
        protocol_version = data.get("remote_protocol_version")
        try:
            parsed_workspace = uuid.UUID(str(workspace_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise RemoteAuthorityMismatch("Remote Work Stack storage identity is invalid") from error
        if str(parsed_workspace) != workspace_id or parsed_workspace.int == 0:
            raise RemoteAuthorityMismatch("Remote Work Stack storage identity is invalid")
        if not isinstance(product_version, str) or not product_version or len(product_version) > 64:
            raise RemoteAuthorityMismatch("Remote Work Stack did not report a valid product version")
        if isinstance(protocol_version, bool) or not isinstance(protocol_version, int) or protocol_version < 0:
            raise RemoteAuthorityMismatch(
                f"Remote Work Stack {product_version} did not report a compatible remote protocol. "
                "Upgrade the Work Stack files in remote_app_dir, then reconnect."
            )
        return {
            "workspace_id": workspace_id,
            "product_version": product_version,
            "remote_protocol_version": protocol_version,
        }

    @staticmethod
    def _require_supported_remote_protocol(
        metadata: dict[str, object],
        *,
        minimum: int = DESKTOP_MINIMUM_REMOTE_PROTOCOL,
        purpose: str = "this Work Stack desktop",
    ) -> None:
        actual = int(metadata["remote_protocol_version"])
        if actual < minimum:
            raise RemoteAuthorityMismatch(
                f"Remote Work Stack {metadata['product_version']} reports protocol {actual}, but {purpose} "
                f"requires protocol {minimum}. Upgrade the Work Stack files in remote_app_dir, then reconnect."
            )

    def _remember_remote_metadata(self, metadata: dict[str, object]) -> None:
        self.remote_product_version = str(metadata["product_version"])
        self.remote_protocol_version = int(metadata["remote_protocol_version"])

    def _verify_remote_workspace(
        self,
        *,
        require_runtime_protocol: bool = False,
        timeout: float = 3.0,
    ) -> None:
        if self.remote_profile is None:
            raise RuntimeError("Remote workspace verification requested without an SSH profile")
        metadata = self._read_remote_storage_metadata(timeout=timeout)
        actual = metadata["workspace_id"]
        if actual != self.remote_profile.workspace_id:
            raise RemoteAuthorityMismatch(
                f"Remote Work Stack workspace identity {actual} does not match remote-connection.json "
                f"({self.remote_profile.workspace_id}). Verify the remote directory and update the saved Workspace ID."
            )
        self._require_supported_remote_protocol(metadata)
        observed_protocol = getattr(self, "remote_protocol_version", None)
        actual_protocol = int(metadata["remote_protocol_version"])
        if require_runtime_protocol and observed_protocol is not None and actual_protocol != observed_protocol:
            raise RemoteAuthorityMismatch(
                f"Remote Work Stack protocol changed from {observed_protocol} to {actual_protocol} during this session. "
                "Verify or upgrade remote_app_dir, then restart Work Stack."
            )
        self._remember_remote_metadata(metadata)

    def _stop_owned_server(self) -> None:
        if self.remote_ssh_process is not None:
            self._stop_owned_remote_connection()
            return
        if not self.server_started_by_host or self.server_pid is None:
            self._trace("stop skipped; desktop host does not own the server")
            return
        owned_pid = self.server_pid
        process = self.server_process
        self.server_process = None
        self.server_started_by_host = False
        self.server_pid = None
        self._trace(f"stopping server owned by desktop host (PID {owned_pid})")
        if process is not None and process.pid == owned_pid:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            return
        stopper = self.install_root / "scripts" / "windows" / "Stop-WorkStack.ps1"
        if not stopper.is_file():
            return
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(stopper),
                    "-InstallRoot",
                    str(self.install_root),
                    "-ProcessId",
                    str(owned_pid),
                ],
                check=False,
                timeout=10,
                creationflags=creation_flags,
                capture_output=True,
                text=True,
            )
            self._trace(f"owned server stop command completed with exit {result.returncode}")
            if result.stdout.strip():
                self._trace(result.stdout.strip())
            if result.stderr.strip():
                self._trace(result.stderr.strip())
        except (OSError, subprocess.SubprocessError):
            self._trace("owned server stop command failed")
            pass

    def _stop_owned_remote_connection(self) -> None:
        process = self.remote_ssh_process
        self.remote_ssh_process = None
        if process is not None and process.poll() is None:
            self._trace(f"stopping SSH connection owned by desktop host (PID {process.pid})")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._close_remote_log()

    def _close_remote_log(self) -> None:
        if self.remote_ssh_log is not None:
            self.remote_ssh_log.close()
            self.remote_ssh_log = None

    def _is_ready(self) -> bool:
        health_url = urllib.parse.urljoin(self.workstack_url, "/api/v1/health")
        try:
            with urllib.request.urlopen(health_url, timeout=1.5) as response:
                if response.status != 200:
                    return False
                payload = json.load(response)
                return payload == {"data": {"api_version": "v1", "status": "ready"}}
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _configured_url(self) -> str:
        config, config_path = self._local_runtime_config()
        try:
            port = config["port"]
        except (KeyError, TypeError) as error:
            raise RuntimeError(f"Work Stack configuration is invalid: {config_path}") from error
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise RuntimeError(f"Work Stack port is invalid: {config_path}")
        return f"http://127.0.0.1:{port}/"

    def _local_runtime_config(self) -> tuple[dict[str, object], Path]:
        config_path = self.state_root / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            config_path = self.install_root / RUNTIME_CONFIG_FILE
            try:
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Work Stack configuration is invalid: {config_path}") from error
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Work Stack configuration is invalid: {config_path}") from error
        if not isinstance(config, dict):
            raise RuntimeError(f"Work Stack configuration is invalid: {config_path}")
        selection = getattr(self, "local_startup_selection", None)
        if selection is not None:
            config = {
                **config,
                "data_dir": str(selection.data_dir),
                "backup_dir": str(selection.backup_dir),
            }
        return config, config_path

    def _apply_native_theme(self, theme: str) -> None:
        self.current_theme = normalize_theme(theme)
        self._apply_native_title_theme(self.current_theme)
        self._apply_native_surface_theme()
        self._apply_source_popup_theme()

    def _apply_native_surface_theme(self) -> None:
        if os.name != "nt":
            return
        try:
            from System.Drawing import Color

            overlay_color = Color.FromArgb(*theme_rgb(self.current_theme, "native.overlay"))
            loading_color = Color.FromArgb(
                *theme_rgb(self.current_theme, "native.externalLoading")
            )
            if self.form is not None:
                self.form.BackColor = overlay_color
            if self.workstack_webview is not None:
                self.workstack_webview.DefaultBackgroundColor = overlay_color
            for viewport in tuple(self.source_viewports.values()):
                viewport.BackColor = loading_color
            for source_webview in tuple(self.source_webviews.values()):
                source_webview.DefaultBackgroundColor = loading_color
        except (AttributeError, OSError, RuntimeError, ValueError):
            self._trace("native surface theme is unavailable")

    def _apply_source_popup_theme(self) -> None:
        if os.name != "nt" or not self.source_popups:
            return
        try:
            from System.Drawing import Color

            overlay_color = Color.FromArgb(*theme_rgb(self.current_theme, "native.overlay"))
            toolbar_color = Color.FromArgb(*theme_rgb(self.current_theme, "native.toolbar"))
            text_color = Color.FromArgb(*theme_rgb(self.current_theme, "native.text"))
            for popup in tuple(self.source_popups.values()):
                popup["overlay"].BackColor = overlay_color
                popup["toolbar"].BackColor = toolbar_color
                popup["label"].ForeColor = text_color
        except (AttributeError, KeyError, OSError, RuntimeError, ValueError):
            self._trace("native popup theme is unavailable")

    def _apply_native_title_theme(self, theme: str) -> None:
        if os.name != "nt" or self.form is None:
            return
        try:
            hwnd = ctypes.c_void_p(int(self.form.Handle.ToInt64()))
            dark = ctypes.c_int(1 if theme == "dark" else 0)
            caption = ctypes.c_int(self._colorref(theme_color(theme, "native.caption")))
            text = ctypes.c_int(self._colorref(theme_color(theme, "native.text")))
            border = ctypes.c_int(self._colorref(theme_color(theme, "native.border")))
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        except (AttributeError, OSError, ValueError):
            self._trace("native title theme is unavailable on this Windows build")

    def _apply_native_brand(self) -> None:
        if self.form is None:
            return
        self.form.Text = NATIVE_WINDOW_TITLE
        try:
            from System.Drawing import Icon

            # The packaged, versioned mark is the only source. A stale
            # install-root WorkStack.ico is deliberately not consulted, and no
            # separate GDI geometry is drawn as a fallback: if the packaged
            # asset is missing the window simply keeps its default icon.
            if not has_mark_ico():
                raise BrandAssetMissing("the packaged Work Stack icon is unavailable")
            icon = Icon(str(mark_ico_path()))
            self.native_icon = icon
            self._set_native_window_icon(icon)
        except Exception as error:
            self._trace(f"native Work Stack icon is unavailable: {type(error).__name__}: {error}")
    def _set_native_window_icon(self, icon) -> None:
        self.form.Icon = icon
        if os.name != "nt":
            return
        hwnd = ctypes.c_void_p(int(self.form.Handle.ToInt64()))
        icon_handle = ctypes.c_void_p(int(icon.Handle.ToInt64()))
        ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, icon_handle)
        ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, icon_handle)

    @staticmethod
    def _apply_process_identity() -> None:
        if os.name != "nt":
            return
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        result = shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        if result < 0:
            raise OSError(f"SetCurrentProcessExplicitAppUserModelID failed: 0x{result & 0xffffffff:08x}")

    @staticmethod
    def _colorref(value: str) -> int:
        red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
        return red | (green << 8) | (blue << 16)

    @staticmethod
    def _trace(message: str) -> None:
        if os.environ.get("WORKSTACK_DESKTOP_DEBUG") == "1":
            print(f"[workstack-desktop] {message}", file=sys.stderr, flush=True)

    def _record_microsoft_diagnostic(
        self,
        event: str,
        provider: str = "host",
        url: str = "",
        **details: object,
    ) -> None:
        parts = urllib.parse.urlsplit(url)
        record: dict[str, object] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event[:80],
            "provider": provider if provider in PROVIDER_URLS else "host",
            "scheme": parts.scheme.lower()[:24],
            "host": parts.hostname or "",
        }
        for key in ("allowed", "success", "stage", "web_error"):
            value = details.get(key)
            if isinstance(value, bool):
                record[key] = value
            elif isinstance(value, str):
                record[key] = value[:120]
        try:
            self.microsoft_diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            if self.microsoft_diagnostic_path.is_file() and self.microsoft_diagnostic_path.stat().st_size > 262_144:
                previous = self.microsoft_diagnostic_path.with_suffix(".previous.log")
                previous.unlink(missing_ok=True)
                self.microsoft_diagnostic_path.replace(previous)
            with self.microsoft_diagnostic_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
        except OSError:
            self._trace("Microsoft WebView diagnostic log is unavailable")

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port

    @staticmethod
    def _is_microsoft_url(url: str, provider: str) -> bool:
        if provider not in PROVIDER_URLS:
            return False
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https":
            return False
        host = (parsed.hostname or "").lower()
        if host in COMMON_AUTH_HOSTS or host in PROVIDER_EXACT_HOSTS[provider]:
            return True
        return any(host.endswith(suffix) and host != suffix[1:] for suffix in PROVIDER_SUFFIXES[provider])

    @staticmethod
    def _is_provider_host(host: str, provider: str) -> bool:
        if provider not in PROVIDER_URLS:
            return False
        if host in PROVIDER_EXACT_HOSTS[provider]:
            return True
        return any(host.endswith(suffix) and host != suffix[1:] for suffix in PROVIDER_SUFFIXES[provider])

    def _acquire_single_instance(self) -> bool:
        if os.name != "nt":
            return True
        digest = hashlib.sha256(str(self.state_root).casefold().encode("utf-8")).hexdigest()[:24]
        name = f"Local\\WorkStackDesktop-{digest}"
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError()
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.instance_mutex = handle
        return True

    def _release_single_instance(self) -> None:
        if self.instance_mutex is not None and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.instance_mutex)
            self.instance_mutex = None

    @staticmethod
    def _activate_existing_window() -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def activate(hwnd, _lparam) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value in {"Work Stack", NATIVE_WINDOW_TITLE} and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
                return False
            return True

        user32.EnumWindows(callback_type(activate), 0)


def write_startup_error_log(error: BaseException) -> Path | None:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    log_path = local_app_data / "WorkStack" / "logs" / "desktop-startup.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = (
            f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}]\n"
            + "".join(traceback.format_exception(error))
            + "\n"
        )
        with log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(record)
        return log_path
    except OSError:
        return None


def show_startup_error(error: BaseException) -> None:
    log_path = write_startup_error_log(error)
    detail = str(error)
    if log_path is not None:
        detail = f"{detail}\n\nDiagnostic log: {log_path}"
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, detail, "Work Stack could not start", 0x10)
    else:
        print(f"Work Stack could not start: {detail}", file=sys.stderr)


def main() -> int:
    try:
        options = parse_args()
        if options.check_remote_connection:
            return check_remote_connection(options.state_root)
        return WorkStackDesktopHost(options).run()
    except Exception as error:
        show_startup_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
