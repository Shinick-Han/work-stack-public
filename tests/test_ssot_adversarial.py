from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_ssot_adversarial", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"


def remote_draft(**overrides: object) -> dict[str, object]:
    draft: dict[str, object] = {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "local_forward_port": 18765,
        "remote_port": 8765,
        "workspace_id": WORKSPACE_ID,
    }
    draft.update(overrides)
    return draft


class SsotConnectionCenterAdversarialTest(unittest.TestCase):
    def test_url_encoded_draft_parser_rejects_malformed_or_oversized_input(self) -> None:
        malformed = (
            "",
            "%ED%A0%80",
            urllib.parse.quote("not-json", safe=""),
            urllib.parse.quote("[]", safe=""),
            urllib.parse.quote(json.dumps({"storage_mode": "ssh-remote"}), safe=""),
            "x" * 32_769,
        )

        for encoded in malformed:
            with self.subTest(encoded=encoded[:40]):
                with self.assertRaises(RuntimeError):
                    MODULE.WorkStackDesktopHost._decode_ssot_draft(encoded)

    def test_message_router_accepts_ssot_commands_only_from_exact_workstack_origin(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        host._handle_ssot_message = mock.Mock()

        def event(source: str, message: str) -> object:
            return types.SimpleNamespace(
                Source=source,
                TryGetWebMessageAsString=mock.Mock(return_value=message),
            )

        host._on_workstack_message(
            None,
            event("https://outlook.office.com/mail/", "workstack-ssot-host|status"),
        )
        host._on_workstack_message(
            None,
            event("http://127.0.0.1:8766/", "workstack-ssot-host|status"),
        )
        host._handle_ssot_message.assert_not_called()

        host._on_workstack_message(
            None,
            event("http://127.0.0.1:8765/?view=focus", "workstack-ssot-host|status"),
        )
        host._handle_ssot_message.assert_called_once_with("workstack-ssot-host|status")

    def test_registry_router_uses_strict_json_channel_only_from_workstack_origin(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        host._handle_connection_registry_message = mock.Mock()

        payload = json.dumps(
            {
                "type": "workstack-connection-registry-request",
                "schema_version": 1,
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "operation": "get-registry",
            },
            separators=(",", ":"),
        )
        event = lambda source: types.SimpleNamespace(
            Source=source,
            TryGetWebMessageAsString=mock.Mock(return_value=payload),
        )

        host._on_workstack_message(None, event("https://outlook.office.com/mail/"))
        host._handle_connection_registry_message.assert_not_called()
        host._on_workstack_message(None, event("http://127.0.0.1:8765/"))
        host._handle_connection_registry_message.assert_called_once_with(payload)

        reordered = json.dumps(
            {
                "operation": "get-registry",
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "schema_version": 1,
                "type": "workstack-connection-registry-request",
            },
            indent=2,
        )
        host._on_workstack_message(
            None,
            types.SimpleNamespace(
                Source="http://127.0.0.1:8765/",
                TryGetWebMessageAsString=mock.Mock(return_value=reordered),
            ),
        )
        self.assertEqual(host._handle_connection_registry_message.call_count, 2)
        host._handle_connection_registry_message.assert_called_with(reordered)

    def test_registry_handler_enqueues_work_without_running_service_on_ui_callback(self) -> None:
        core = mock.Mock()
        service = mock.Mock()
        worker = mock.Mock()
        worker.submit.return_value = True
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.connection_registry_host = service
        host.connection_registry_worker = worker
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_connection_registry_message('{"untrusted":"payload"}')

        worker.submit.assert_called_once_with('{"untrusted":"payload"}')
        service.handle_json.assert_not_called()
        core.PostWebMessageAsJson.assert_not_called()

    def test_desktop_registry_mutations_remain_dark_until_activation_safety_release(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "activation safety"):
            MODULE.WorkStackDesktopHost._save_connection_registry_from_host(
                Path("unused"), {"untrusted": "registry"}
            )

    def test_registry_busy_response_retains_valid_request_correlation(self) -> None:
        request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        payload = json.dumps(
            {
                "type": "workstack-connection-registry-request",
                "schema_version": 1,
                "request_id": request_id,
                "operation": "get-registry",
            }
        )
        core = mock.Mock()
        worker = mock.Mock()
        worker.submit.return_value = False
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.connection_registry_worker = worker
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_connection_registry_message(payload)

        response = json.loads(core.PostWebMessageAsJson.call_args.args[0])
        self.assertFalse(response["ok"])
        self.assertEqual(response["request_id"], request_id)
        self.assertEqual(response["operation"], "get-registry")
        self.assertEqual(response["error"]["code"], "busy")

    def test_registry_busy_response_does_not_guess_invalid_correlation(self) -> None:
        core = mock.Mock()
        worker = mock.Mock()
        worker.submit.return_value = False
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.connection_registry_worker = worker
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_connection_registry_message('{"request_id":"secret"}')

        response = json.loads(core.PostWebMessageAsJson.call_args.args[0])
        self.assertFalse(response["ok"])
        self.assertIsNone(response["request_id"])
        self.assertIsNone(response["operation"])
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_registry_service_exception_becomes_correlated_internal_error(self) -> None:
        request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        payload = json.dumps({
            "type": "workstack-connection-registry-request",
            "schema_version": 1,
            "request_id": request_id,
            "operation": "test-profile",
            "profile": {},
        })
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.connection_registry_host = mock.Mock()
        host.connection_registry_host.handle_json.side_effect = TimeoutError("secret")

        response = json.loads(host._execute_connection_registry_request(payload))

        self.assertFalse(response["ok"])
        self.assertEqual(response["request_id"], request_id)
        self.assertEqual(response["operation"], "test-profile")
        self.assertEqual(response["error"]["code"], "internal_error")
        self.assertNotIn("secret", response["error"]["message"])

    def test_registry_worker_response_is_marshaled_before_touching_webview(self) -> None:
        callbacks: list[object] = []
        form = types.SimpleNamespace(
            IsDisposed=False,
            BeginInvoke=mock.Mock(side_effect=lambda callback: callbacks.append(callback)),
        )
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.form = form
        host._post_connection_registry_response = mock.Mock()
        system = types.ModuleType("System")
        system.Action = lambda callback: callback

        with mock.patch.dict(sys.modules, {"System": system}):
            host._deliver_connection_registry_response('{"ok":true}')

        form.BeginInvoke.assert_called_once()
        host._post_connection_registry_response.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        host._post_connection_registry_response.assert_called_once_with('{"ok":true}')

    def test_registry_response_is_dropped_after_form_disposal(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.form = types.SimpleNamespace(IsDisposed=True, BeginInvoke=mock.Mock())
        host._post_connection_registry_response = mock.Mock()

        host._deliver_connection_registry_response('{"ok":true}')

        host.form.BeginInvoke.assert_not_called()
        host._post_connection_registry_response.assert_not_called()

    def test_registry_response_rechecks_disposal_when_marshaled_callback_runs(self) -> None:
        callbacks: list[object] = []
        form = types.SimpleNamespace(
            IsDisposed=False,
            BeginInvoke=mock.Mock(side_effect=lambda callback: callbacks.append(callback)),
        )
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.form = form
        host._post_connection_registry_response = mock.Mock()
        system = types.ModuleType("System")
        system.Action = lambda callback: callback

        with mock.patch.dict(sys.modules, {"System": system}):
            host._deliver_connection_registry_response('{"ok":true}')

        form.IsDisposed = True
        callbacks[0]()
        host._post_connection_registry_response.assert_not_called()

    def test_local_directory_picker_marshals_to_ui_and_cancel_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "not-created"
            instances: list[object] = []

            class Dialog:
                def __init__(self) -> None:
                    self.Description = ""
                    self.ShowNewFolderButton = False
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
            host = object.__new__(MODULE.WorkStackDesktopHost)
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
                selected = host._choose_local_ssot_directory()

            self.assertIsNone(selected)
            self.assertFalse(candidate.exists())
            form.Invoke.assert_called_once()
            self.assertEqual(instances[0].owner, form)
            self.assertTrue(instances[0].ShowNewFolderButton)
            instances[0].dispose.assert_called_once()

    def test_local_directory_picker_returns_selection_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "selected-but-not-created"

            class Dialog:
                Description = ""
                ShowNewFolderButton = False
                SelectedPath = str(candidate)

                def ShowDialog(self, _owner: object) -> str:
                    return "OK"

                def Dispose(self) -> None:
                    return None

            form = types.SimpleNamespace(
                IsDisposed=False,
                Invoke=mock.Mock(side_effect=lambda callback: callback()),
            )
            host = object.__new__(MODULE.WorkStackDesktopHost)
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
                selected = host._choose_local_ssot_directory()

            self.assertEqual(selected, str(candidate))
            self.assertFalse(candidate.exists())

    def test_handler_ignores_unknown_commands_and_does_not_echo_invalid_payload(self) -> None:
        core = mock.Mock()
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.active_connection_draft = {"storage_mode": "local"}
        host.state_root = Path("C:/state")
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_ssot_message("workstack-ssot-host|delete|ignored")
        core.PostWebMessageAsJson.assert_not_called()

        forbidden_value = "do-" + "not-echo"
        secret = urllib.parse.quote(
            json.dumps({"storage_mode": "local", "password": forbidden_value}),
            safe="",
        )
        host._handle_ssot_message(f"workstack-ssot-host|save|{secret}")
        payload = core.PostWebMessageAsJson.call_args.args[0]
        self.assertNotIn(forbidden_value, payload)
        self.assertEqual(json.loads(payload)["state"], "error")

    def test_validation_rejects_secret_and_unknown_fields_for_both_modes(self) -> None:
        forbidden = ("password", "private_key", "identity_file", "ssh_args", "unknown")
        for field in forbidden:
            with self.subTest(mode="local", field=field):
                with self.assertRaisesRegex(RuntimeError, "unsupported fields"):
                    MODULE.validate_connection_draft({"storage_mode": "local", field: "value"})
            with self.subTest(mode="ssh-remote", field=field):
                with self.assertRaisesRegex(RuntimeError, "unsupported fields"):
                    MODULE.validate_connection_draft(remote_draft(**{field: "value"}))

    def test_local_save_is_exact_minimal_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = MODULE.save_connection_draft(root, {"storage_mode": "local"})
            contents = (root / MODULE.REMOTE_CONNECTION_FILE).read_text(encoding="utf-8")

        self.assertEqual(saved, {"storage_mode": "local"})
        self.assertEqual(contents, '{"storage_mode":"local"}\n')

    def test_atomic_save_failure_preserves_existing_profile_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / MODULE.REMOTE_CONNECTION_FILE
            original = b'{"storage_mode":"local"}\n'
            path.write_bytes(original)

            with mock.patch.object(MODULE.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(RuntimeError, "Could not save"):
                    MODULE.save_connection_draft(root, remote_draft())

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                [candidate.name for candidate in root.iterdir()],
                [MODULE.REMOTE_CONNECTION_FILE],
            )


if __name__ == "__main__":
    unittest.main()
