from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "webview2-shell"
PYTHON_SHELL = ROOT / "desktop" / "python-webview-shell"


class DesktopWebViewShellContractTest(unittest.TestCase):
    def test_build_is_pinned_and_uses_the_inbox_runtime(self) -> None:
        build = (SHELL / "Build-WorkStackShell.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("1.0.4129.50", build)
        self.assertIn("d3934f482d484b89fb4825df720c710664e1143a1e90f7b3a60794ef33f473d2", build)
        self.assertIn("Framework64\\v4.0.30319\\csc.exe", build)
        self.assertIn("runtimes\\win-x64\\native\\WebView2Loader.dll", build)
        self.assertNotIn("dotnet ", build)
        self.assertNotIn("cargo ", build)

    def test_shell_owns_only_the_server_it_started(self) -> None:
        source = (SHELL / "WorkStackShell.cs").read_text(encoding="utf-8-sig")

        self.assertIn('"/api/v1/health"', source)
        self.assertIn('" -NoBrowser"', source)
        self.assertIn("serverStartedByShell = true", source)
        self.assertIn("if (!serverStartedByShell) return", source)
        self.assertIn("Stop-WorkStack.ps1", source)
        self.assertIn("--auto-close-seconds", source)

    def test_source_inbox_owns_the_embedded_microsoft_surface(self) -> None:
        source = (SHELL / "WorkStackShell.cs").read_text(encoding="utf-8-sig")

        self.assertIn("https://outlook.office.com/mail/", source)
        self.assertIn("https://teams.microsoft.com/v2/", source)
        self.assertIn("https://www.office.com/launch/onenote", source)
        self.assertIn('private const string SourceHostPrefix = "workstack-source-host"', source)
        self.assertIn("workStackWebView.CoreWebView2.WebMessageReceived += OnWorkStackMessage", source)
        self.assertIn("Rectangle.Intersect(workStackWebView.ClientRectangle, requestedBounds)", source)
        self.assertIn("sourceViewport.Controls.Add(sourceWebView)", source)
        self.assertIn("requestedBounds.Left - clipped.Left", source)
        self.assertIn("sourceViewport.BringToFront()", source)
        self.assertIn("sourceViewport.Visible = false", source)
        self.assertNotIn("new ToolStrip {", source)

    def test_navigation_authorities_are_separated_and_content_blind(self) -> None:
        source = (SHELL / "WorkStackShell.cs").read_text(encoding="utf-8-sig")

        self.assertIn("IsWorkStackAllowed", source)
        self.assertIn("IsMicrosoftAllowed", source)
        self.assertIn("--probe-provider", source)
        self.assertIn("--probe-result", source)
        self.assertIn("success=", source)
        self.assertIn("host=", source)
        self.assertNotIn("ExecuteScriptAsync", source)
        self.assertNotIn("CookieManager", source)


class PythonDesktopShellContractTest(unittest.TestCase):
    def read(self, name: str) -> str:
        return (PYTHON_SHELL / name).read_text(encoding="utf-8")

    def test_signed_python_host_reuses_the_context_inbox_bridge(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn("import webview", source)
        self.assertIn('SOURCE_HOST_PREFIX = "workstack-source-host"', source)
        self.assertIn('"outlook": "https://outlook.office.com/mail/"', source)
        self.assertIn('"teams": "https://teams.microsoft.com/v2/"', source)
        self.assertIn('"onenote": "https://www.office.com/launch/onenote"', source)
        self.assertIn("window.native.webview", source)
        self.assertIn("WebMessageReceived -=", source)
        self.assertIn("WebMessageReceived +=", source)
        self.assertIn("viewport.Controls.Add(source_webview)", source)
        self.assertIn("Rectangle.Intersect", source)
        self.assertIn("FormClosing +=", source)
        self.assertIn("OUTLOOK_VISIBLE_CAPTURE_SCRIPT", source)
        self.assertNotIn("CookieManager", source)

    def test_provider_webviews_persist_across_tab_switches(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn("self.source_webviews: dict", source)
        self.assertIn("self.source_viewports: dict", source)
        self.assertIn("self.source_initialized: set", source)
        self.assertIn("_navigate_source_once", source)
        self.assertIn("if provider in self.source_initialized", source)
        self.assertNotIn('self.active_provider = ""', source[source.index("def _hide_source"):source.index("def _on_workstack_navigation_starting")])

    def test_native_title_bar_follows_the_product_theme(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn("workstack-window-theme|", source)
        self.assertIn("DwmSetWindowAttribute", source)
        self.assertIn("_apply_native_title_theme", source)

    def test_native_title_bar_uses_the_product_mark_without_duplicate_copy(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn('NATIVE_WINDOW_TITLE = "\\u200b"', source)
        self.assertIn("self.form.Invoke(Action(self._initialize_native_shell))", source)
        self.assertIn("self.form.Text = NATIVE_WINDOW_TITLE", source)
        self.assertIn("self.form.Icon = icon", source)
        self.assertIn('title.value in {"Work Stack", NATIVE_WINDOW_TITLE}', source)

    def test_taskbar_uses_an_explicit_product_identity_and_installed_icon(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn('APP_USER_MODEL_ID = "WorkStack.Desktop"', source)
        self.assertIn("SetCurrentProcessExplicitAppUserModelID", source)
        self.assertIn('self.install_root / "WorkStack.ico"', source)
        self.assertIn("Color.FromArgb(184, 242, 75)", source)
        self.assertNotIn("Color.FromArgb(174, 235, 61)", source)

    def test_dialog_suspension_restores_the_existing_provider_view(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn('f"{SOURCE_HOST_PREFIX}|suspend"', source)
        self.assertIn('f"{SOURCE_HOST_PREFIX}|resume"', source)
        self.assertIn("self.source_suspended", source)
        self.assertIn("def _restore_source", source)

    def test_native_bridge_reads_visible_outlook_content_only_on_explicit_capture(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn('f"{SOURCE_HOST_PREFIX}|capture|"', source)
        self.assertIn("def _send_source_capture", source)
        self.assertIn("self._is_microsoft_url(candidate, provider)", source)
        self.assertIn("CoreWebView2.DocumentTitle", source)
        self.assertIn("Clipboard.GetText", source)
        self.assertIn("PostWebMessageAsJson", source)
        self.assertIn("ExecuteScriptAsync", source)
        self.assertIn("UniqueMessageBody_", source)
        self.assertIn("workstack-outlook-visible-capture", source)
        self.assertIn("WebMessageReceived", source)
        dialog = (ROOT / "frontend" / "src" / "features" / "inbox" / "SourceCaptureDialog.tsx").read_text(encoding="utf-8")
        self.assertIn("Recipients, attachments", dialog)

    def test_shell_owns_only_the_server_it_starts(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn('"/api/v1/health"', source)
        self.assertIn('"config.json"', source)
        self.assertIn('config["port"]', source)
        self.assertIn("Start-WorkStack.ps1", source)
        self.assertIn("Stop-WorkStack.ps1", source)
        self.assertIn("server_started_by_shell", source)
        self.assertIn("server_pid", source)
        self.assertIn('"-StatusPath"', source)
        self.assertIn('"-ProcessId"', source)
        self.assertIn("_acquire_single_instance", source)
        self.assertIn("--auto-close-seconds", source)

    def test_launcher_failure_preserves_the_actionable_powershell_error(self) -> None:
        source = self.read("workstack_desktop.py")
        launcher = source[source.index("    def _ensure_server"):source.index("    def _ensure_remote_server")]

        self.assertNotIn("capture_output=True", launcher)
        self.assertIn('"desktop-launch.out.log"', launcher)
        self.assertIn('"desktop-launch.err.log"', launcher)
        self.assertIn("stdout=launcher_stdout", launcher)
        self.assertIn("stderr=launcher_stderr", launcher)
        self.assertIn("Work Stack launcher failed:", launcher)
        self.assertIn("completed.returncode", launcher)

    def test_native_bridge_requires_the_exact_workstack_origin_and_health_shape(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn("event_args.Source", source)
        self.assertIn("self.workstack_origin", source)
        self.assertIn('{"data": {"api_version": "v1", "status": "ready"}}', source)

    def test_provider_navigation_is_narrow_and_provider_specific(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn("PROVIDER_EXACT_HOSTS", source)
        self.assertIn("PROVIDER_SUFFIXES", source)
        self.assertIn('"office.com"', source)
        self.assertIn('"www.office.com"', source)
        self.assertIn('"www.microsoft365.com"', source)
        self.assertIn('"m365.cloud.microsoft"', source)
        self.assertIn("LaunchingExternalUriScheme", source)
        self.assertIn("def _on_source_external_uri", source)
        self.assertIn('scheme == "msteams"', source)
        self.assertIn("event_args.Cancel = True", source)
        self.assertIn("webbrowser.open", source)
        self.assertNotIn("MICROSOFT_SUFFIXES", source)
        self.assertNotIn('".microsoft.com"', source)
        self.assertNotIn('".live.com"', source)

    def test_microsoft_sign_in_preserves_popup_semantics_and_enables_os_sso(self) -> None:
        source = self.read("workstack_desktop.py")

        self.assertIn("CoreWebView2EnvironmentOptions", source)
        self.assertIn("AllowSingleSignOnUsingOSPrimaryAccount = True", source)
        self.assertIn("CoreWebView2Environment.CreateAsync", source)
        self.assertIn("event_args.GetDeferral()", source)
        self.assertIn("event_args.NewWindow = sender.CoreWebView2", source)
        self.assertIn("EnsureCoreWebView2Async(parent_core.Environment)", source)
        self.assertIn("WindowCloseRequested", source)
        self.assertIn("self.source_auth_sessions", source)
        self.assertIn('scheme == "https" and provider in self.source_auth_sessions', source)
        self.assertNotIn("self.source_webviews[provider].CoreWebView2.Navigate(target)", source)

    def test_microsoft_diagnostics_are_content_blind(self) -> None:
        source = self.read("workstack_desktop.py")
        diagnostic = source[source.index("    def _record_microsoft_diagnostic"):source.index("    @staticmethod\n    def _origin")]

        self.assertIn('"microsoft-webview.log"', source)
        self.assertIn("def _record_microsoft_diagnostic", source)
        self.assertIn('"host": parts.hostname', diagnostic)
        self.assertNotIn('"url": url', diagnostic)
        self.assertNotIn('"path": parts.path', diagnostic)
        self.assertNotIn('"query": parts.query', diagnostic)

    def test_desktop_dependencies_are_exactly_locked(self) -> None:
        requirements = (ROOT / "requirements-windows-desktop.txt").read_text(encoding="utf-8")

        self.assertIn("pywebview==6.2.1", requirements)
        self.assertIn("pythonnet==3.1.0", requirements)
        self.assertIn("--hash=sha256:", requirements)


if __name__ == "__main__":
    unittest.main()
