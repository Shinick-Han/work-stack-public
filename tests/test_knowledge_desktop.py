from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
for import_root in (SHELL, ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import knowledge_desktop as KD
import knowledge_host as KH

DESKTOP_SPEC = importlib.util.spec_from_file_location(
    "workstack_desktop_knowledge_desktop_test", SHELL / "workstack_desktop.py"
)
assert DESKTOP_SPEC is not None and DESKTOP_SPEC.loader is not None
DESKTOP = importlib.util.module_from_spec(DESKTOP_SPEC)
sys.modules[DESKTOP_SPEC.name] = DESKTOP
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    DESKTOP_SPEC.loader.exec_module(DESKTOP)


REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE = "66666666-6666-4666-8666-666666666666"


def request(operation: str, **extra: object) -> str:
    return json.dumps(
        {
            "type": "workstack-knowledge-request",
            "schema_version": 1,
            "request_id": REQUEST_ID,
            "operation": operation,
            **extra,
        },
        separators=(",", ":"),
    )


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.vaults: list[dict[str, str]] = []

    def status(self) -> dict[str, object]:
        self.calls.append(("status", threading.get_ident()))
        return {"vaults": list(self.vaults)}


class KnowledgeDesktopImportTest(unittest.TestCase):
    def test_application_root_is_the_project_root_not_the_desktop_folder(self) -> None:
        self.assertEqual(KD._APPLICATION_ROOT, ROOT)
        self.assertEqual(KD._SHELL_DIRECTORY, SHELL)
        self.assertIn(str(ROOT), sys.path)

    def test_module_loads_isolated_from_an_unrelated_working_directory(self) -> None:
        script = textwrap.dedent(
            """
            import importlib.util
            import sys

            spec = importlib.util.spec_from_file_location(
                "knowledge_desktop_isolated", sys.argv[1]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            print(module.KnowledgeDesktopMixin.__name__)
            """
        )
        with tempfile.TemporaryDirectory() as unrelated:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script, str(SHELL / "knowledge_desktop.py")],
                cwd=unrelated,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "KnowledgeDesktopMixin")


class KnowledgeDesktopIntegrationTest(unittest.TestCase):
    def test_host_uses_the_knowledge_desktop_mixin(self) -> None:
        self.assertTrue(issubclass(DESKTOP.WorkStackDesktopHost, KD.KnowledgeDesktopMixin))

    def test_knowledge_messages_require_the_workstack_origin(self) -> None:
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        host._handle_knowledge_message = mock.Mock()
        payload = request("status")

        def event(source: str) -> object:
            return types.SimpleNamespace(
                Source=source,
                TryGetWebMessageAsString=mock.Mock(return_value=payload),
            )

        host._on_workstack_message(None, event("https://outlook.office.com/mail/"))
        host._on_workstack_message(None, event("http://127.0.0.1:8766/"))
        host._handle_knowledge_message.assert_not_called()
        host._on_workstack_message(None, event("http://127.0.0.1:8765/?view=focus"))
        host._handle_knowledge_message.assert_called_once_with(payload)

    def test_handler_enqueues_work_without_running_service_on_ui_callback(self) -> None:
        core = mock.Mock()
        knowledge_host = mock.Mock()
        worker = mock.Mock()
        worker.submit.return_value = True
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.knowledge_host = knowledge_host
        host.knowledge_worker = worker
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_knowledge_message('{"untrusted":"payload"}')

        worker.submit.assert_called_once_with('{"untrusted":"payload"}')
        knowledge_host.handle_json.assert_not_called()
        core.PostWebMessageAsJson.assert_not_called()

    def test_worker_does_not_read_sources_on_the_ui_thread(self) -> None:
        registry = FakeRegistry()
        ui_thread = threading.get_ident()
        done = threading.Event()
        delivered: list[str] = []

        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.state_root = Path(tempfile.gettempdir())
        host.form = None
        host.knowledge_host = KH.KnowledgeHostService(
            host.state_root, lambda: None, WORKSPACE, registry=registry
        )
        host.knowledge_worker = KD.BoundedRequestWorker(
            host._execute_knowledge_request,
            lambda response: delivered.append(response) or done.set(),
            maximum_pending=16,
            thread_name="test-knowledge-worker",
        )
        host.knowledge_worker.start()
        try:
            self.assertTrue(host.knowledge_worker.submit(request("status")))
            self.assertTrue(done.wait(2))
        finally:
            host.knowledge_worker.stop(timeout=2)

        self.assertEqual(registry.calls[0][0], "status")
        self.assertNotEqual(registry.calls[0][1], ui_thread)
        payload = json.loads(delivered[0])
        self.assertTrue(payload["ok"], payload)

    def test_busy_response_retains_valid_request_correlation(self) -> None:
        core = mock.Mock()
        worker = mock.Mock()
        worker.submit.return_value = False
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.knowledge_worker = worker
        host.knowledge_host = KH.KnowledgeHostService(
            Path(tempfile.gettempdir()), lambda: None, WORKSPACE, registry=FakeRegistry()
        )
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_knowledge_message(request("status"))

        response = json.loads(core.PostWebMessageAsJson.call_args.args[0])
        self.assertFalse(response["ok"])
        self.assertEqual(response["request_id"], REQUEST_ID)
        self.assertEqual(response["operation"], "status")
        self.assertEqual(response["error"]["code"], "busy")

    def test_service_exception_becomes_correlated_internal_error(self) -> None:
        payload = request("status")
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.knowledge_host = mock.Mock()
        host.knowledge_host.handle_json.side_effect = TimeoutError("secret-path")

        response = json.loads(host._execute_knowledge_request(payload))

        self.assertFalse(response["ok"])
        self.assertEqual(response["request_id"], REQUEST_ID)
        self.assertEqual(response["operation"], "status")
        self.assertEqual(response["error"]["code"], "internal_error")
        self.assertNotIn("secret-path", response["error"]["message"])

    def test_worker_response_is_marshaled_before_touching_webview(self) -> None:
        callbacks: list[object] = []
        form = types.SimpleNamespace(
            IsDisposed=False,
            BeginInvoke=mock.Mock(side_effect=lambda callback: callbacks.append(callback)),
        )
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.form = form
        host._post_knowledge_response = mock.Mock()
        system = types.ModuleType("System")
        system.Action = lambda callback: callback

        with mock.patch.dict(sys.modules, {"System": system}):
            host._deliver_knowledge_response('{"ok":true}')

        form.BeginInvoke.assert_called_once()
        host._post_knowledge_response.assert_not_called()
        callbacks[0]()
        host._post_knowledge_response.assert_called_once_with('{"ok":true}')

    def test_shutdown_drops_delivery_when_the_form_is_gone(self) -> None:
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.form = types.SimpleNamespace(IsDisposed=True, BeginInvoke=mock.Mock())
        host._post_knowledge_response = mock.Mock()

        host._deliver_knowledge_response('{"ok":true}')

        host.form.BeginInvoke.assert_not_called()
        host._post_knowledge_response.assert_not_called()

    def test_shutdown_drops_late_marshaled_delivery_after_form_changes(self) -> None:
        callbacks: list[object] = []
        form = types.SimpleNamespace(
            IsDisposed=False,
            BeginInvoke=mock.Mock(side_effect=lambda callback: callbacks.append(callback)),
        )
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.form = form
        host._post_knowledge_response = mock.Mock()
        system = types.ModuleType("System")
        system.Action = lambda callback: callback

        with mock.patch.dict(sys.modules, {"System": system}):
            host._deliver_knowledge_response('{"ok":true}')

        host.form = types.SimpleNamespace(IsDisposed=False)
        callbacks[0]()
        host._post_knowledge_response.assert_not_called()

    def test_shutdown_drops_late_marshaled_delivery_after_disposal(self) -> None:
        callbacks: list[object] = []
        form = types.SimpleNamespace(
            IsDisposed=False,
            BeginInvoke=mock.Mock(side_effect=lambda callback: callbacks.append(callback)),
        )
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.form = form
        host._post_knowledge_response = mock.Mock()
        system = types.ModuleType("System")
        system.Action = lambda callback: callback

        with mock.patch.dict(sys.modules, {"System": system}):
            host._deliver_knowledge_response('{"ok":true}')

        form.IsDisposed = True
        callbacks[0]()
        host._post_knowledge_response.assert_not_called()

    def test_form_closing_stops_knowledge_worker(self) -> None:
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        # Closing retires any startup recovery page, so the eager lifetime the
        # constructor builds is stated here instead of being invented lazily.
        host.startup_recovery = DESKTOP.StartupRecoveryLifetime()
        host.remote_shutdown_requested = threading.Event()
        host.connection_registry_worker = mock.Mock()
        host.knowledge_worker = mock.Mock()
        host.server_started_by_host = False
        host.server_stop_thread = None
        host.remote_ssh_process = None
        host._stop_remote_monitor = mock.Mock()

        host._on_form_closing(None, None)

        host.knowledge_worker.stop.assert_called_once_with(timeout=0)
        host.connection_registry_worker.stop.assert_called_once_with(timeout=0)

    def test_vault_picker_marshals_to_ui_without_creating_a_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "not-created"
            instances: list[object] = []

            class Dialog:
                def __init__(self) -> None:
                    self.Description = ""
                    self.ShowNewFolderButton = True
                    self.SelectedPath = str(candidate)
                    self.dispose = mock.Mock()
                    instances.append(self)

                def ShowDialog(self, owner: object) -> str:
                    self.owner = owner
                    return "Cancel"

                def Dispose(self) -> None:
                    self.dispose()

            form = types.SimpleNamespace(
                IsDisposed=False,
                Invoke=mock.Mock(side_effect=lambda callback: callback()),
            )
            host = object.__new__(DESKTOP.WorkStackDesktopHost)
            host.form = form
            system = types.ModuleType("System")
            system.__path__ = []
            system.Action = lambda callback: callback
            winforms = types.ModuleType("System.Windows.Forms")
            winforms.DialogResult = types.SimpleNamespace(OK="OK")
            winforms.FolderBrowserDialog = Dialog

            with mock.patch.dict(
                sys.modules,
                {"System": system, "System.Windows.Forms": winforms},
            ):
                selected = host._choose_knowledge_vault_directory()

            self.assertIsNone(selected)
            self.assertFalse(candidate.exists())
            form.Invoke.assert_called_once()
            self.assertFalse(instances[0].ShowNewFolderButton)
            self.assertEqual(instances[0].Description, "Choose a local Markdown vault")
            instances[0].dispose.assert_called_once()

    def test_vault_picker_returns_selection_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "selected-but-not-created"

            class Dialog:
                Description = ""
                ShowNewFolderButton = True
                SelectedPath = str(candidate)

                def ShowDialog(self, _owner: object) -> str:
                    return "OK"

                def Dispose(self) -> None:
                    return None

            form = types.SimpleNamespace(
                IsDisposed=False,
                Invoke=mock.Mock(side_effect=lambda callback: callback()),
            )
            host = object.__new__(DESKTOP.WorkStackDesktopHost)
            host.form = form
            system = types.ModuleType("System")
            system.__path__ = []
            system.Action = lambda callback: callback
            winforms = types.ModuleType("System.Windows.Forms")
            winforms.DialogResult = types.SimpleNamespace(OK="OK")
            winforms.FolderBrowserDialog = Dialog

            with mock.patch.dict(
                sys.modules,
                {"System": system, "System.Windows.Forms": winforms},
            ):
                selected = host._choose_knowledge_vault_directory()

            self.assertEqual(selected, str(candidate))
            self.assertFalse(candidate.exists())

    def test_run_starts_and_stops_the_knowledge_worker(self) -> None:
        source = (SHELL / "workstack_desktop.py").read_text(encoding="utf-8")
        run = source[source.index("    def run(self)"):source.index("    def _on_form_closing")]
        self.assertIn("self._start_knowledge_desktop()", run)
        self.assertIn("self._stop_knowledge_desktop(timeout=5)", run)
        closing = source[
            source.index("    def _on_form_closing"):source.index("    def _stop_owned_server_after_window")
        ]
        self.assertIn("self._stop_knowledge_desktop(timeout=0)", closing)
        bridge = (SHELL / "knowledge_desktop.py").read_text(encoding="utf-8")
        self.assertIn("self.knowledge_worker.start()", bridge)
        self.assertIn("knowledge_worker.stop(timeout=timeout)", bridge)
        self.assertIn('thread_name="workstack-knowledge-worker"', bridge)


if __name__ == "__main__":
    unittest.main()
