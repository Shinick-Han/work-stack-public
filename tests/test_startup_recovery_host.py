from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

from connection_registry_activation_recovery import ActivationRecoveryStatus
from startup_recovery_host import (
    build_startup_recovery_html,
    parse_startup_recovery_request,
)


MODULE_PATH = SHELL / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_recovery_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


ACTIVATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DIGEST = "sha256:" + "1" * 64


def status() -> ActivationRecoveryStatus:
    return ActivationRecoveryStatus(
        state="recovery_required",
        code="recovery_required",
        message="An unconfirmed connection activation can be restored explicitly.",
        can_restore=True,
        activation_id=ACTIVATION_ID,
        profile_id=PROFILE_ID,
        current_registry_digest=DIGEST,
    )


def document() -> dict[str, object]:
    return {
        "state": "recovery_required",
        "code": "recovery_required",
        "message": "An unconfirmed connection activation can be restored explicitly.",
        "can_restore": True,
        "activation_id": ACTIVATION_ID,
        "profile_id": PROFILE_ID,
        "current_registry_digest": DIGEST,
    }


def request(operation: str, **extra: object) -> str:
    value: dict[str, object] = {
        "type": "workstack-connection-activation-recovery-request",
        "schema_version": 1,
        "request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "activation_id": ACTIVATION_ID,
        "operation": operation,
    }
    if operation == "restore-previous-connection":
        value["expected_registry_digest"] = DIGEST
    value.update(extra)
    return json.dumps(value)


class StartupRecoveryHostContractTest(unittest.TestCase):
    def test_startup_page_is_rendered_for_the_selected_theme(self) -> None:
        light = MODULE.build_startup_html("light")
        dark = MODULE.build_startup_html("dark")
        unknown = MODULE.build_startup_html("system")

        self.assertIn('content="light"', light)
        self.assertIn('content="dark"', dark)
        self.assertNotEqual(light, dark)
        self.assertEqual(unknown, dark)
        self.assertNotIn("__WS_", light)

    def test_parser_accepts_only_exact_cas_bound_requests(self) -> None:
        parsed = parse_startup_recovery_request(request("restore-previous-connection"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.activation_id, ACTIVATION_ID)
        self.assertEqual(parsed.expected_registry_digest, DIGEST)
        self.assertEqual(
            parse_startup_recovery_request(request("exit")).operation,
            "exit",
        )

        self.assertIsNone(parse_startup_recovery_request(request("restore-previous-connection", path="C:/secret")))
        self.assertIsNone(parse_startup_recovery_request(request("restore-previous-connection", expected_registry_digest="sha256:bad")))
        self.assertIsNone(parse_startup_recovery_request("x" * 3000))

    def test_page_uses_fixed_copy_and_never_renders_startup_error(self) -> None:
        page = build_startup_recovery_html(document())
        self.assertIn("Restore previous connection", page)
        self.assertIn(ACTIVATION_ID, page)
        self.assertNotIn("private startup detail", page)
        self.assertNotIn("Traceback", page)

        restored = build_startup_recovery_html(document(), outcome="restored")
        self.assertIn("Previous connection restored", restored)
        self.assertNotIn('id="restore"', restored)

    def test_page_uses_the_persisted_theme_without_inline_color_literals(self) -> None:
        light = build_startup_recovery_html(document(), theme="light")
        dark = build_startup_recovery_html(document(), theme="dark")
        unknown = build_startup_recovery_html(document(), theme="system")

        self.assertIn('content="light"', light)
        self.assertIn('content="dark"', dark)
        self.assertNotEqual(light, dark)
        self.assertEqual(unknown, dark)


class DesktopStartupRecoveryIntegrationTest(unittest.TestCase):
    def bare_host(self):
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.connection_registry_startup_enabled = True
        host.connection_activation_recovery = mock.Mock()
        host.connection_activation_recovery.inspect.return_value = status()
        host.startup_error = RuntimeError("private startup detail")
        host.window = mock.Mock()
        host.current_theme = "light"
        host.startup_recovery_status = None
        host.startup_recovery_in_progress = False
        return host

    def test_failed_pending_activation_renders_ephemeral_recovery_page(self) -> None:
        host = self.bare_host()
        with mock.patch.object(MODULE, "write_startup_error_log") as write_log:
            self.assertTrue(host._show_startup_activation_recovery())

        write_log.assert_called_once_with(host.startup_error)
        rendered = host.window.load_html.call_args.args[0]
        self.assertIn("Restore previous connection", rendered)
        self.assertIn('content="light"', rendered)
        self.assertNotIn(str(host.startup_error), rendered)

    def test_web_theme_message_persists_and_updates_open_native_surfaces(self) -> None:
        host = self.bare_host()
        host.state_root = Path("C:/bounded-test-state")
        with (
            mock.patch.object(MODULE, "persist_theme") as persist,
            mock.patch.object(host, "_apply_native_theme") as apply,
        ):
            self.assertTrue(host._dispatch_workstack_host_message("workstack-window-theme|light"))

        persist.assert_called_once_with(host.state_root, "light")
        apply.assert_called_once_with("light")

    def test_gate_off_or_nonrecoverable_status_keeps_existing_failure_path(self) -> None:
        host = self.bare_host()
        host.connection_registry_startup_enabled = False
        self.assertFalse(host._show_startup_activation_recovery())
        host.connection_activation_recovery.inspect.assert_not_called()

        host.connection_registry_startup_enabled = True
        host.connection_activation_recovery.inspect.return_value = ActivationRecoveryStatus(
            "none", "no_recovery", "No connection activation requires recovery.", False
        )
        self.assertFalse(host._show_startup_activation_recovery())
        host.window.load_html.assert_not_called()

        host.connection_activation_recovery.inspect.side_effect = RuntimeError("internal")
        self.assertFalse(host._show_startup_activation_recovery())
        host.window.load_html.assert_not_called()

    def test_restore_is_explicit_and_bound_to_advertised_digest(self) -> None:
        host = self.bare_host()
        host.startup_recovery_status = document()
        thread = mock.Mock()
        with mock.patch.object(MODULE.threading, "Thread", return_value=thread):
            self.assertTrue(
                host._dispatch_startup_recovery_message(
                    request("restore-previous-connection")
                )
            )
        thread.start.assert_called_once()
        self.assertTrue(host.startup_recovery_in_progress)

        host.startup_recovery_in_progress = False
        wrong = request("restore-previous-connection").replace(DIGEST, "sha256:" + "2" * 64)
        with mock.patch.object(MODULE.threading, "Thread") as start:
            self.assertTrue(host._dispatch_startup_recovery_message(wrong))
        start.assert_not_called()

    def test_successful_restore_requires_restart_and_removes_retry_action(self) -> None:
        host = self.bare_host()
        host.startup_recovery_status = document()
        host.connection_activation_recovery.restore.return_value = mock.sentinel.result

        host._restore_previous_startup_connection(ACTIVATION_ID, DIGEST)

        host.connection_activation_recovery.restore.assert_called_once_with(
            ACTIVATION_ID,
            expected_registry_digest=DIGEST,
        )
        page = host.window.load_html.call_args.args[0]
        self.assertIn("Previous connection restored", page)
        self.assertNotIn('id="restore"', page)

    def test_exit_is_explicit_and_internal_recovery_navigation_is_scoped(self) -> None:
        host = self.bare_host()
        host.startup_recovery_status = document()
        self.assertTrue(host._dispatch_startup_recovery_message(request("exit")))
        host.window.destroy.assert_called_once()

        recovery_navigation = types.SimpleNamespace(Uri="data:text/html;base64,abc", Cancel=False)
        host._on_workstack_navigation_starting(None, recovery_navigation)
        self.assertFalse(recovery_navigation.Cancel)

        host.startup_recovery_status = None
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        host._origin = lambda _value: ("data", "", None)
        ordinary_navigation = types.SimpleNamespace(Uri="data:text/html;base64,abc", Cancel=False)
        host._on_workstack_navigation_starting(None, ordinary_navigation)
        self.assertTrue(ordinary_navigation.Cancel)


if __name__ == "__main__":
    unittest.main()
