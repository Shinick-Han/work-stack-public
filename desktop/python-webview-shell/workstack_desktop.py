from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import webview


SOURCE_HOST_PREFIX = "workstack-source-host"
PROVIDER_URLS = {
    "outlook": "https://outlook.office.com/mail/",
    "teams": "https://teams.microsoft.com/v2/",
    "onenote": "https://www.office.com/launch/onenote",
}
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


REMOTE_CONNECTION_FILE = "remote-connection.json"
SSH_HOST_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,255}$")


@dataclass(frozen=True)
class RemoteConnectionProfile:
    ssh_host_alias: str
    remote_app_dir: str
    remote_data_dir: str
    local_forward_port: int
    workspace_id: str
    remote_port: int = 8765


def _validated_port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ValueError(f"{field} must be an integer from 1 to 65535")
    return value


def _validated_remote_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute Linux path")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{field} contains an invalid control character")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(f"{field} must not contain '.' or '..' path segments")
    normalized = value.rstrip("/") or "/"
    if normalized == "/":
        raise ValueError(f"{field} must not be the Linux filesystem root")
    return normalized


def load_remote_connection_profile(state_root: Path) -> RemoteConnectionProfile | None:
    profile_path = state_root / REMOTE_CONNECTION_FILE
    if not profile_path.is_file():
        return None
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Remote connection profile is invalid JSON: {profile_path}") from error
    if not isinstance(raw, dict):
        raise RuntimeError(f"Remote connection profile must contain one JSON object: {profile_path}")
    mode = raw.get("storage_mode")
    if mode == "local":
        unexpected = set(raw) - {"storage_mode"}
        if unexpected:
            raise RuntimeError(f"Local connection profile has unsupported fields: {', '.join(sorted(unexpected))}")
        return None
    if mode != "ssh-remote":
        raise RuntimeError("remote-connection.json storage_mode must be 'local' or 'ssh-remote'")
    required = {
        "storage_mode", "ssh_host_alias", "remote_app_dir", "remote_data_dir", "local_forward_port",
        "workspace_id",
    }
    allowed = required | {"remote_port"}
    missing = required - set(raw)
    unexpected = set(raw) - allowed
    if missing:
        raise RuntimeError(f"Remote connection profile is missing: {', '.join(sorted(missing))}")
    if unexpected:
        raise RuntimeError(f"Remote connection profile has unsupported fields: {', '.join(sorted(unexpected))}")
    alias = raw["ssh_host_alias"]
    if not isinstance(alias, str) or not SSH_HOST_ALIAS_PATTERN.fullmatch(alias):
        raise RuntimeError("ssh_host_alias must be a configured OpenSSH alias without spaces or shell characters")
    try:
        workspace_id = str(uuid.UUID(str(raw["workspace_id"])))
        if workspace_id != raw["workspace_id"] or uuid.UUID(workspace_id).int == 0:
            raise ValueError("workspace_id must be a canonical non-nil UUID")
        return RemoteConnectionProfile(
            ssh_host_alias=alias,
            remote_app_dir=_validated_remote_path(raw["remote_app_dir"], "remote_app_dir"),
            remote_data_dir=_validated_remote_path(raw["remote_data_dir"], "remote_data_dir"),
            local_forward_port=_validated_port(raw["local_forward_port"], "local_forward_port"),
            workspace_id=workspace_id,
            remote_port=_validated_port(raw.get("remote_port", 8765), "remote_port"),
        )
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"Remote connection profile is invalid: {error}") from error


def find_ssh_executable() -> str:
    executable = shutil.which("ssh.exe") or shutil.which("ssh")
    if not executable:
        raise RuntimeError("OpenSSH client was not found. Enable the Windows OpenSSH Client feature first.")
    return executable


def build_remote_server_command(profile: RemoteConnectionProfile) -> str:
    runner = f"{profile.remote_app_dir}/run_work_stack.py"
    identity_store = f"{profile.remote_data_dir}/store-meta.json"
    workspace_store = f"{profile.remote_data_dir}/workspace.json"
    arguments = [
        "python3", runner, "--data-dir", profile.remote_data_dir, "graph", "serve",
        "--host", "127.0.0.1", "--port", str(profile.remote_port),
    ]
    return (
        f"test -f {shlex.quote(identity_store)} && "
        f"test -f {shlex.quote(workspace_store)} && "
        f"cd -- {shlex.quote(profile.remote_app_dir)} && exec "
    ) + " ".join(
        shlex.quote(argument) for argument in arguments
    )


def build_ssh_tunnel_command(profile: RemoteConnectionProfile, ssh_executable: str) -> list[str]:
    return [
        ssh_executable, "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{profile.local_forward_port}:127.0.0.1:{profile.remote_port}",
        profile.ssh_host_alias,
        build_remote_server_command(profile),
    ]


def build_ssh_check_command(profile: RemoteConnectionProfile, ssh_executable: str) -> list[str]:
    runner = f"{profile.remote_app_dir}/run_work_stack.py"
    remote_check = " && ".join((
        f"test -f {shlex.quote(runner)}",
        f"test -d {shlex.quote(profile.remote_data_dir)}",
        f"test -f {shlex.quote(profile.remote_data_dir + '/store-meta.json')}",
        f"test -f {shlex.quote(profile.remote_data_dir + '/workspace.json')}",
        "command -v python3 >/dev/null 2>&1",
        f"cd -- {shlex.quote(profile.remote_app_dir)}",
        f"python3 {shlex.quote(runner)} --help >/dev/null 2>&1",
    ))
    return [
        ssh_executable, "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        profile.ssh_host_alias,
        remote_check,
    ]


def check_remote_connection(state_root: Path) -> int:
    profile = load_remote_connection_profile(state_root.resolve())
    if profile is None:
        raise RuntimeError(f"SSH remote mode is not configured in {state_root / REMOTE_CONNECTION_FILE}")
    command = build_ssh_check_command(profile, find_ssh_executable())
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            command, check=False, timeout=15, creationflags=creation_flags,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("SSH connection check timed out after 15 seconds") from error
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        suffix = f" Last SSH message: {detail[-1][:300]}" if detail else ""
        raise RuntimeError(
            "SSH connection check failed. Confirm the host alias, known-host key, SSH agent, remote paths, and python3."
            + suffix
        )
    print(json.dumps({
        "status": "ready",
        "storage_mode": "ssh-remote",
        "ssh_host_alias": profile.ssh_host_alias,
        "local_forward_port": profile.local_forward_port,
        "remote_port": profile.remote_port,
        "workspace_id": profile.workspace_id,
    }, separators=(",", ":")))
    return 0


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


class WorkStackDesktopHost:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.install_root = options.install_root.resolve()
        self.state_root = options.state_root.resolve()
        self.profile_root = self.state_root / "desktop-webview-profile"
        self.microsoft_profile_root = self.state_root / "desktop-microsoft-profile"
        self.microsoft_diagnostic_path = self.state_root / "logs" / "microsoft-webview.log"
        self.remote_profile = load_remote_connection_profile(self.state_root)
        if self.remote_profile is not None and options.url:
            raise RuntimeError("--url cannot be combined with storage_mode 'ssh-remote'")
        self.workstack_url = (
            f"http://127.0.0.1:{self.remote_profile.local_forward_port}/"
            if self.remote_profile is not None
            else options.url or self._configured_url()
        )
        self.workstack_origin = self._origin(self.workstack_url)
        self.server_started_by_shell = False
        self.server_pid: int | None = None
        self.remote_ssh_process: subprocess.Popen | None = None
        self.remote_ssh_log = None
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
        self.active_provider = ""
        self.native_icon = None
        self.probe_recorded = False
        self.runtime_version = "unknown"

    def run(self) -> int:
        self._apply_process_identity()
        if not self._acquire_single_instance():
            self._activate_existing_window()
            return 0
        try:
            self._ensure_server()
            self.profile_root.mkdir(parents=True, exist_ok=True)
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            self.window = webview.create_window(
                "Work Stack",
                self.workstack_url,
                min_size=(1024, 700),
                maximized=True,
                background_color="#0b0d12",
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
            if self.server_stop_thread is not None:
                self.server_stop_thread.join(timeout=12)
            else:
                self._stop_owned_server()
            self._release_single_instance()

    def _on_form_closing(self, _sender, _event_args) -> None:
        if (self.server_started_by_shell or self.remote_ssh_process is not None) and self.server_stop_thread is None:
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

    def _initialize_native_shell(self) -> None:
        self.form.FormClosing += self._on_form_closing
        self._apply_native_brand()
        self._apply_native_title_theme("dark")
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

    def _on_workstack_ready(self, sender, event_args) -> None:
        if not event_args.IsSuccess:
            if self.options.probe_result:
                self._write_probe(False, "initializing", "WorkStackInitializationFailed")
            return
        self._create_source_views(sender.CoreWebView2.Environment)

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

        for provider in PROVIDER_URLS:
            viewport = WinForms.Panel()
            viewport.Visible = False
            viewport.BackColor = Color.White

            source_webview = WebView2()
            source_webview.DefaultBackgroundColor = Color.White
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
        if self._origin(str(event_args.Source)) != self.workstack_origin:
            return
        try:
            message = str(event_args.TryGetWebMessageAsString())
        except Exception:
            return
        if message == f"{SOURCE_HOST_PREFIX}|hide":
            self._hide_source()
            return
        if message == f"{SOURCE_HOST_PREFIX}|suspend":
            self.source_suspended = True
            self._hide_source()
            return
        if message == f"{SOURCE_HOST_PREFIX}|resume":
            self.source_suspended = False
            self._restore_source()
            return
        if message.startswith(f"{SOURCE_HOST_PREFIX}|capture|"):
            self._send_source_capture(message)
            return
        if message.startswith("workstack-window-theme|"):
            theme = message.partition("|")[2]
            if theme in {"dark", "light"}:
                self._apply_native_title_theme(theme)
            return
        parts = message.split("|")
        if len(parts) != 7 or parts[0] != SOURCE_HOST_PREFIX or parts[1] != "show":
            return
        provider = parts[2]
        if provider not in PROVIDER_URLS:
            return
        try:
            left, top, width, height = (int(value) for value in parts[3:])
        except ValueError:
            return
        if (
            left < -10_000
            or top < -10_000
            or left > 10_000
            or top > 10_000
            or width < 160
            or height < 120
            or width > 10_000
            or height > 10_000
        ):
            return

        from System.Drawing import Rectangle

        self._show_source(provider, Rectangle(left, top, width, height))

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

    def _restore_source(self) -> None:
        if self.source_suspended or not self.active_provider:
            return
        viewport = self.source_viewports.get(self.active_provider)
        if viewport is None:
            return
        viewport.Visible = True
        viewport.BringToFront()

    def _send_source_capture(self, message: str) -> None:
        parts = message.split("|", 3)
        if len(parts) != 4:
            return
        _prefix, command, provider, request_id = parts
        if command != "capture" or provider not in PROVIDER_URLS or not request_id or len(request_id) > 128:
            return
        source_webview = self.source_webviews.get(provider)
        candidate = str(source_webview.Source) if source_webview is not None and source_webview.Source is not None else ""
        url = candidate[:4096] if candidate and self._is_microsoft_url(candidate, provider) else ""
        document_title = ""
        if source_webview is not None and source_webview.CoreWebView2 is not None:
            document_title = str(source_webview.CoreWebView2.DocumentTitle or "").strip()[:500]
        core = self.workstack_webview.CoreWebView2 if self.workstack_webview is not None else None
        if core is None:
            return
        clipboard_text = ""
        try:
            from System.Windows.Forms import Clipboard

            if Clipboard.ContainsText():
                clipboard_text = str(Clipboard.GetText()).strip()[:4000]
        except Exception as error:
            self._trace(f"explicit clipboard capture is unavailable: {type(error).__name__}: {error}")
        capture = {
            "url": url,
            "title": document_title,
            "text": clipboard_text,
        }
        if provider == "outlook" and source_webview is not None and source_webview.CoreWebView2 is not None:
            if len(self.pending_source_captures) >= 16:
                self.pending_source_captures.pop(next(iter(self.pending_source_captures)))
            self.pending_source_captures[request_id] = capture
            script = OUTLOOK_VISIBLE_CAPTURE_SCRIPT.replace("__REQUEST_ID__", json.dumps(request_id))
            try:
                source_webview.CoreWebView2.ExecuteScriptAsync(script)
                return
            except Exception as error:
                self.pending_source_captures.pop(request_id, None)
                self._trace(f"visible Outlook capture is unavailable: {type(error).__name__}: {error}")

        self._post_source_draft(provider, request_id, capture)

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
        overlay.BackColor = Color.FromArgb(13, 15, 20)
        toolbar = WinForms.Panel()
        toolbar.Dock = WinForms.DockStyle.Top
        toolbar.Height = 38
        toolbar.BackColor = Color.FromArgb(20, 23, 30)
        label = WinForms.Label()
        label.Text = "Microsoft sign-in"
        label.ForeColor = Color.White
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
        child.DefaultBackgroundColor = Color.White
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
        if self._is_ready():
            self._trace("server already ready; desktop host does not own it")
            return
        launcher = self.install_root / "scripts" / "windows" / "Start-WorkStack.ps1"
        if not launcher.is_file():
            raise FileNotFoundError(f"Installed Work Stack launcher was not found: {launcher}")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        launch_root = self.state_root / "desktop-launch"
        launch_root.mkdir(parents=True, exist_ok=True)
        status_path = launch_root / f"launch-{uuid.uuid4().hex}.json"
        stdout_path = launch_root / "desktop-launch.out.log"
        stderr_path = launch_root / "desktop-launch.err.log"
        try:
            # Do not use subprocess.PIPE here. The background Work Stack server
            # can inherit the PowerShell pipe handles, preventing communicate()
            # from ever observing EOF even after the launcher itself exits.
            with stdout_path.open("wb") as launcher_stdout, stderr_path.open("wb") as launcher_stderr:
                completed = subprocess.run(
                    [
                    "powershell.exe",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(launcher),
                    "-InstallRoot",
                    str(self.install_root),
                    "-StateRoot",
                    str(self.state_root),
                    "-NoBrowser",
                    "-StatusPath",
                    str(status_path),
                    ],
                    check=False,
                    creationflags=creation_flags,
                    stdout=launcher_stdout,
                    stderr=launcher_stderr,
                )
            if completed.returncode != 0:
                combined = "\n".join(
                    path.read_bytes().decode("utf-8", errors="replace")
                    for path in (stderr_path, stdout_path)
                    if path.is_file() and path.stat().st_size
                )
                lines = [line.strip() for line in combined.splitlines() if line.strip()]
                detail = lines[0] if lines else f"PowerShell exited with code {completed.returncode}"
                self._trace(f"launcher failed with exit {completed.returncode}: {combined.strip()}")
                raise RuntimeError(f"Work Stack launcher failed: {detail}")
            result = json.loads(status_path.read_text(encoding="utf-8-sig"))
        except RuntimeError:
            raise
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise RuntimeError("The installed launcher did not report server ownership.") from error
        finally:
            status_path.unlink(missing_ok=True)

        status = result.get("status")
        pid = result.get("pid")
        if status == "started" and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            self.server_started_by_shell = True
            self.server_pid = pid
            self._trace(f"server started by desktop host (PID {pid})")
        elif status == "reused" and pid is None:
            self._trace("launcher reused an existing server; desktop host does not own it")
        else:
            raise RuntimeError("The installed launcher returned an invalid ownership receipt.")
        if not self._is_ready():
            raise RuntimeError("The installed launcher did not produce a ready Work Stack server.")

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

    def _verify_remote_workspace(self) -> None:
        if self.remote_profile is None:
            raise RuntimeError("Remote workspace verification requested without an SSH profile")
        storage_url = urllib.parse.urljoin(self.workstack_url, "/api/v1/storage")
        try:
            with urllib.request.urlopen(storage_url, timeout=3) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        except (OSError, UnicodeError, ValueError, urllib.error.URLError) as error:
            raise RuntimeError("Remote Work Stack storage identity could not be verified") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        actual = data.get("workspace_id") if isinstance(data, dict) else None
        if actual != self.remote_profile.workspace_id:
            raise RuntimeError(
                "Remote Work Stack workspace identity does not match remote-connection.json"
            )

    def _stop_owned_server(self) -> None:
        if self.remote_ssh_process is not None:
            self._stop_owned_remote_connection()
            return
        if not self.server_started_by_shell or self.server_pid is None:
            self._trace("stop skipped; desktop host does not own the server")
            return
        owned_pid = self.server_pid
        self.server_started_by_shell = False
        self.server_pid = None
        self._trace(f"stopping server owned by desktop host (PID {owned_pid})")
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
        config_path = self.state_root / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            port = config["port"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Work Stack configuration is invalid: {config_path}") from error
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise RuntimeError(f"Work Stack port is invalid: {config_path}")
        return f"http://127.0.0.1:{port}/"

    def _apply_native_title_theme(self, theme: str) -> None:
        if os.name != "nt" or self.form is None:
            return
        try:
            hwnd = ctypes.c_void_p(int(self.form.Handle.ToInt64()))
            dark = ctypes.c_int(1 if theme == "dark" else 0)
            caption = ctypes.c_int(self._colorref("#0d0f14" if theme == "dark" else "#f4f6f8"))
            text = ctypes.c_int(self._colorref("#f2f3f5" if theme == "dark" else "#18202a"))
            border = ctypes.c_int(self._colorref("#242832" if theme == "dark" else "#d7dce3"))
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
            from System.Drawing import Bitmap, Color, Graphics, Icon, Rectangle, SolidBrush
            from System.Drawing.Drawing2D import GraphicsPath, SmoothingMode

            installed_icon = self.install_root / "WorkStack.ico"
            if installed_icon.is_file():
                icon = Icon(str(installed_icon))
                self.native_icon = icon
                self._set_native_window_icon(icon)
                return

            bitmap = Bitmap(32, 32)
            graphics = Graphics.FromImage(bitmap)
            path = GraphicsPath()
            accent = SolidBrush(Color.FromArgb(184, 242, 75))
            ink = SolidBrush(Color.FromArgb(25, 34, 16))
            try:
                graphics.SmoothingMode = SmoothingMode.AntiAlias
                graphics.Clear(Color.Transparent)
                path.AddArc(2, 2, 9, 9, 180, 90)
                path.AddArc(21, 2, 9, 9, 270, 90)
                path.AddArc(21, 21, 9, 9, 0, 90)
                path.AddArc(2, 21, 9, 9, 90, 90)
                path.CloseFigure()
                graphics.FillPath(accent, path)
                graphics.FillRectangle(ink, Rectangle(8, 10, 3, 12))
                graphics.FillRectangle(ink, Rectangle(13, 7, 3, 17))
                graphics.FillRectangle(ink, Rectangle(18, 9, 3, 13))
                handle = bitmap.GetHicon()
                icon = Icon.FromHandle(handle).Clone()
                ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(int(handle.ToInt64())))
                self.native_icon = icon
                self._set_native_window_icon(icon)
            finally:
                ink.Dispose()
                accent.Dispose()
                path.Dispose()
                graphics.Dispose()
                bitmap.Dispose()
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


def show_startup_error(error: BaseException) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, str(error), "Work Stack could not start", 0x10)
    else:
        print(f"Work Stack could not start: {error}", file=sys.stderr)


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
