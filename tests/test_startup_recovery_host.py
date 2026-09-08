from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import connection_registry as REGISTRY
import connection_registry_activation_recovery as MODULE_RECOVERY
import connection_registry_mutations as MUTATIONS
from connection_registry_activation_recovery import ActivationRecoveryStatus
from startup_recovery_host import (
    STARTUP_RECOVERY_DOCUMENT_SOURCES,
    StartupRecoveryLifetime,
    build_startup_recovery_html,
    mint_startup_recovery_capability,
    parse_startup_recovery_request,
)
from tests import test_connection_registry_activation_recovery as recovery_fixtures


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
        "can_reconcile": False,
        "activation_id": ACTIVATION_ID,
        "profile_id": PROFILE_ID,
        "current_registry_digest": DIGEST,
    }


def reconcile_status() -> ActivationRecoveryStatus:
    return ActivationRecoveryStatus(
        state="can_reconcile",
        code="reconcile_required",
        message="Duplicate connection activation records can be closed explicitly.",
        can_restore=False,
        activation_id=ACTIVATION_ID,
        profile_id=PROFILE_ID,
        current_registry_digest=DIGEST,
        can_reconcile=True,
    )


def reconcile_document() -> dict[str, object]:
    return {
        "state": "can_reconcile",
        "code": "reconcile_required",
        "message": "Duplicate connection activation records can be closed explicitly.",
        "can_restore": False,
        "can_reconcile": True,
        "activation_id": ACTIVATION_ID,
        "profile_id": PROFILE_ID,
        "current_registry_digest": DIGEST,
    }


CAPABILITY = "0" * 63 + "1"


def armed(
    status: dict[str, object] | None = None,
    capability: str = CAPABILITY,
    *,
    accepts_next_action: bool = True,
) -> StartupRecoveryLifetime:
    """The eager lifetime one real render leaves behind, with no native window.

    The host initializes this in its constructor, so a test that builds a host
    through ``__new__`` states it explicitly rather than letting a lazy read
    invent one; ``publish`` is the same call the product uses, so the armed
    generation, capability and pending render all match a real render.
    """

    lifetime = StartupRecoveryLifetime()
    if status is not None:
        lifetime.publish(
            lifetime.generation,
            status,
            capability,
            rendered_page(status, capability),
            lambda _page: None,
            accepts_next_action=accepts_next_action,
        )
    return lifetime


def rendered_page(status: dict[str, object], capability: str = CAPABILITY) -> str:
    """The exact document the product renders for this status and capability."""

    return build_startup_recovery_html(status, capability=capability)


def own_navigation(page: str) -> str:
    """The navigation target this WebView2 runtime reports for *page*.

    Built here from the measured shape — the whole document inlined as base64
    behind ``data:text/html;charset=utf-8;base64,`` — rather than from the
    production helper, so the two are only equal if the product is right.
    """

    inlined = base64.b64encode(page.encode("utf-8")).decode("ascii")
    return "data:text/html;charset=utf-8;base64," + inlined


def request(operation: str, capability: str = CAPABILITY, **extra: object) -> str:
    value: dict[str, object] = {
        "type": "workstack-connection-activation-recovery-request",
        "schema_version": 1,
        "request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "activation_id": ACTIVATION_ID,
        "capability": capability,
        "operation": operation,
    }
    if operation != "exit":
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

    def test_parser_binds_reconciliation_to_a_digest_and_nothing_else(self) -> None:
        parsed = parse_startup_recovery_request(request("reconcile-activation-evidence"))
        self.assertEqual("reconcile-activation-evidence", parsed.operation)
        self.assertEqual(DIGEST, parsed.expected_registry_digest)

        for rejected in (
            request("reconcile-activation-evidence", path="C:/secret"),
            request("reconcile-activation-evidence", expected_registry_digest="sha256:bad"),
            request("reconcile-activation-evidence", activation_id="latest"),
            request("purge-activation-evidence"),
            request("exit", expected_registry_digest=DIGEST),
        ):
            self.assertIsNone(parse_startup_recovery_request(rejected))

    def test_reconcile_page_offers_only_reconciliation(self) -> None:
        page = build_startup_recovery_html(reconcile_document(), capability=CAPABILITY)

        self.assertIn("Close duplicate records", page)
        self.assertIn('data-operation="reconcile-activation-evidence"', page)
        self.assertNotIn('id="restore"', page)
        self.assertNotIn("Restore previous connection", page)
        self.assertIn(DIGEST, page)

        refused = build_startup_recovery_html(
            reconcile_document(),
            capability=CAPABILITY,
            outcome="refused",
            safe_message="Multiple connection activations require manual review.",
        )
        self.assertIn("Duplicate records could not be closed", refused)
        self.assertIn("Multiple connection activations require manual review.", refused)
        self.assertNotIn("<button id=\"reconcile\"", refused)

    def test_every_message_bearing_page_escapes_its_fixed_copy(self) -> None:
        probe = '<img src=x onerror="alert(1)">'

        for outcome in ("refused", "reviewed"):
            with self.subTest(outcome=outcome):
                page = build_startup_recovery_html(
                    reconcile_document(),
                    capability=CAPABILITY,
                    outcome=outcome,
                    safe_message=probe,
                )

                self.assertNotIn(probe, page)
                self.assertIn("&lt;img", page)
                self.assertNotIn("data-operation=", page)

    def test_page_refuses_a_status_that_advertises_no_single_action(self) -> None:
        blocked = dict(reconcile_document(), can_reconcile=False)
        both = dict(reconcile_document(), can_restore=True)

        for invalid in (blocked, both):
            with self.assertRaises(ValueError):
                build_startup_recovery_html(invalid, capability=CAPABILITY)

    def test_page_uses_fixed_copy_and_never_renders_startup_error(self) -> None:
        page = build_startup_recovery_html(document(), capability=CAPABILITY)
        self.assertIn("Restore previous connection", page)
        self.assertIn(ACTIVATION_ID, page)
        self.assertNotIn("private startup detail", page)
        self.assertNotIn("Traceback", page)

        restored = build_startup_recovery_html(document(), capability=CAPABILITY, outcome="restored")
        self.assertIn("Previous connection restored", restored)
        self.assertNotIn('id="restore"', restored)

    def test_page_uses_the_persisted_theme_without_inline_color_literals(self) -> None:
        light = build_startup_recovery_html(document(), capability=CAPABILITY, theme="light")
        dark = build_startup_recovery_html(document(), capability=CAPABILITY, theme="dark")
        unknown = build_startup_recovery_html(
            document(), capability=CAPABILITY, theme="system"
        )

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
        host.startup_recovery = armed()
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
        host.startup_recovery = armed(document())
        thread = mock.Mock()
        with mock.patch.object(MODULE.threading, "Thread", return_value=thread):
            self.assertTrue(
                host._dispatch_startup_recovery_message(
                    request("restore-previous-connection")
                )
            )
        thread.start.assert_called_once()
        self.assertTrue(host.startup_recovery.in_progress)

        host.startup_recovery.in_progress = False
        wrong = request("restore-previous-connection").replace(DIGEST, "sha256:" + "2" * 64)
        with mock.patch.object(MODULE.threading, "Thread") as start:
            self.assertFalse(host._dispatch_startup_recovery_message(wrong))
        start.assert_not_called()

    def test_successful_restore_requires_restart_and_removes_retry_action(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(document())
        host.connection_activation_recovery.restore.return_value = mock.sentinel.result

        host._apply_startup_recovery_action(
            "restore-previous-connection", ACTIVATION_ID, DIGEST, host.startup_recovery.generation
        )

        host.connection_activation_recovery.restore.assert_called_once_with(
            ACTIVATION_ID,
            expected_registry_digest=DIGEST,
        )
        page = host.window.load_html.call_args.args[0]
        self.assertIn("Previous connection restored", page)
        self.assertNotIn('id="restore"', page)

    def test_reconcilable_startup_renders_the_reconcile_page(self) -> None:
        host = self.bare_host()
        host.connection_activation_recovery.inspect.return_value = reconcile_status()
        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertTrue(host._show_startup_activation_recovery())

        rendered = host.window.load_html.call_args.args[0]
        self.assertIn("Close duplicate records", rendered)
        self.assertNotIn("Restore previous connection", rendered)
        self.assertNotIn(str(host.startup_error), rendered)
        self.assertTrue(host.startup_recovery.status["can_reconcile"])

    def test_only_the_advertised_action_may_be_dispatched(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(document())

        with mock.patch.object(MODULE.threading, "Thread") as thread:
            self.assertTrue(
                host._dispatch_startup_recovery_message(
                    request("reconcile-activation-evidence")
                )
            )

        thread.assert_not_called()
        self.assertFalse(host.startup_recovery.in_progress)

    def test_a_second_click_starts_no_second_reconciliation(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(reconcile_document())
        message = request("reconcile-activation-evidence")

        with mock.patch.object(MODULE.threading, "Thread") as thread:
            self.assertTrue(host._dispatch_startup_recovery_message(message))
            self.assertTrue(host._dispatch_startup_recovery_message(message))

        thread.assert_called_once()
        self.assertEqual(
            ("reconcile-activation-evidence", ACTIVATION_ID, DIGEST, host.startup_recovery.generation),
            thread.call_args.kwargs["args"],
        )
        self.assertTrue(host.startup_recovery.in_progress)

    def test_a_worker_that_cannot_start_leaves_no_busy_page(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(reconcile_document())

        with mock.patch.object(MODULE.threading, "Thread", side_effect=RuntimeError("no thread")):
            self.assertTrue(
                host._dispatch_startup_recovery_message(
                    request("reconcile-activation-evidence")
                )
            )

        self.assertFalse(host.startup_recovery.in_progress)
        page = host.window.load_html.call_args.args[0]
        self.assertIn("Duplicate records could not be closed", page)
        self.assertNotIn('data-operation="reconcile-activation-evidence"', page)
        host.connection_activation_recovery.reconcile.assert_not_called()

    def test_reconciliation_rerenders_the_freshly_inspected_restore_page(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(reconcile_document())
        host.connection_activation_recovery.reconcile.return_value = (
            MODULE_RECOVERY.ActivationReconciliationReport(
                "resumed", "Duplicate connection activation records were closed.", status()
            )
        )

        host._apply_startup_recovery_action(
            "reconcile-activation-evidence", ACTIVATION_ID, DIGEST, host.startup_recovery.generation
        )

        host.connection_activation_recovery.reconcile.assert_called_once_with(
            ACTIVATION_ID,
            expected_registry_digest=DIGEST,
        )
        self.assertEqual(document(), host.startup_recovery.status)
        self.assertFalse(host.startup_recovery.in_progress)
        page = host.window.load_html.call_args.args[0]
        self.assertIn("Restore previous connection", page)
        self.assertNotIn("Close duplicate records", page)

    def test_a_refused_reconciliation_keeps_the_advertised_status(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(reconcile_document())
        host.connection_activation_recovery.reconcile.side_effect = (
            MODULE_RECOVERY.ActivationRecoveryRefusedError("multiple_pending_activations")
        )

        host._apply_startup_recovery_action(
            "reconcile-activation-evidence", ACTIVATION_ID, DIGEST, host.startup_recovery.generation
        )

        self.assertEqual(reconcile_document(), host.startup_recovery.status)
        self.assertTrue(host.startup_recovery.in_progress)
        page = host.window.load_html.call_args.args[0]
        self.assertIn("Multiple connection activations require manual review.", page)
        self.assertNotIn('data-operation=', page)

    def test_an_unexpected_reconciliation_failure_is_reviewed_not_refused(self) -> None:
        """Only a proven zero-write path may use the refusal copy."""

        host = self.bare_host()
        host.startup_recovery = armed(reconcile_document())
        host.connection_activation_recovery.reconcile.side_effect = KeyError(
            "activation_id"
        )

        host._apply_startup_recovery_action(
            "reconcile-activation-evidence", ACTIVATION_ID, DIGEST, host.startup_recovery.generation
        )

        self.assertEqual(reconcile_document(), host.startup_recovery.status)
        self.assertTrue(host.startup_recovery.in_progress)
        page = host.window.load_html.call_args.args[0]
        self.assertIn("Connection records need review", page)
        self.assertIn("could not confirm", page)
        self.assertNotIn("Duplicate records could not be closed", page)
        self.assertNotIn("data-operation=", page)

    def test_an_unexpected_restore_failure_keeps_its_refusal(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(document())
        host.connection_activation_recovery.restore.side_effect = KeyError(
            "activation_id"
        )

        host._apply_startup_recovery_action(
            "restore-previous-connection", ACTIVATION_ID, DIGEST, host.startup_recovery.generation
        )

        page = host.window.load_html.call_args.args[0]
        self.assertIn("Connection could not be restored", page)
        self.assertNotIn("data-operation=", page)

    def test_exit_is_explicit_and_internal_recovery_navigation_is_scoped(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(document())
        self.assertTrue(host._dispatch_startup_recovery_message(request("exit")))
        host.window.destroy.assert_called_once()
        # Closing invalidates the render, so a replay of the same request from
        # the document that is going away cannot close the window a second time.
        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.assertFalse(host._dispatch_startup_recovery_message(request("exit")))
        host.window.destroy.assert_called_once()

        host.startup_recovery = armed(document())
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        recovery_navigation = types.SimpleNamespace(Uri="about:blank", Cancel=False)
        host._on_workstack_navigation_starting(None, recovery_navigation)
        self.assertFalse(recovery_navigation.Cancel)

        # Recovering never admits an arbitrary in-memory document, only the
        # exact source the recovery page itself is loaded from.
        forged = types.SimpleNamespace(Uri="data:text/html;base64,abc", Cancel=False)
        host._on_workstack_navigation_starting(None, forged)
        self.assertTrue(forged.Cancel)

        host.startup_recovery = armed()
        ordinary_navigation = types.SimpleNamespace(Uri="about:blank", Cancel=False)
        host._on_workstack_navigation_starting(None, ordinary_navigation)
        self.assertTrue(ordinary_navigation.Cancel)



NATIVE_RECOVERY_SOURCE = "about:blank"


class RecoveryOriginAdmissionTest(unittest.TestCase):
    """The bridge must admit recovery requests only from the recovery page."""

    def bare_host(self):
        return DesktopStartupRecoveryIntegrationTest().bare_host()

    def host(self):
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.startup_recovery = armed(reconcile_document())
        host.current_theme = "light"
        host.window = mock.Mock()
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        return host

    def test_the_trusted_source_is_the_one_the_native_shell_reports(self) -> None:
        self.assertIn(NATIVE_RECOVERY_SOURCE, STARTUP_RECOVERY_DOCUMENT_SOURCES)
        self.assertNotIn("", STARTUP_RECOVERY_DOCUMENT_SOURCES)
        self.assertNotIn("null", STARTUP_RECOVERY_DOCUMENT_SOURCES)
        # A navigation target and a message source are separate facts: the
        # document the runtime inlines into the navigation is admitted there,
        # never here.
        self.assertFalse(
            any(source.startswith("data:") for source in STARTUP_RECOVERY_DOCUMENT_SOURCES)
        )

    def test_a_foreign_source_with_a_valid_binding_starts_no_recovery(self) -> None:
        for forged in (
            "https://attacker.invalid/recovery-forgery",
            "http://127.0.0.1:8765/",
            "file:///C:/recovery.html",
            "data:text/html;base64,abc",
            # The exact document this render armed for navigation admission:
            # it buys nothing on the message side.
            own_navigation(rendered_page(reconcile_document())),
            "",
        ):
            with self.subTest(source=forged):
                host = self.host()
                event = types.SimpleNamespace(
                    TryGetWebMessageAsString=lambda: request(
                        "reconcile-activation-evidence"
                    ),
                    Source=forged,
                )
                with mock.patch.object(MODULE.threading, "Thread") as thread:
                    host._on_workstack_message(None, event)

                thread.assert_not_called()
                self.assertFalse(host.startup_recovery.in_progress)
                host.window.load_html.assert_not_called()
                host.window.destroy.assert_not_called()

    def test_the_native_recovery_source_dispatches_the_advertised_action(self) -> None:
        host = self.host()
        started = mock.Mock()
        event = types.SimpleNamespace(
            TryGetWebMessageAsString=lambda: request("reconcile-activation-evidence"),
            Source=NATIVE_RECOVERY_SOURCE,
        )

        with mock.patch.object(MODULE.threading, "Thread", return_value=started) as thread:
            host._on_workstack_message(None, event)

        started.start.assert_called_once()
        self.assertTrue(host.startup_recovery.in_progress)
        self.assertEqual(
            ("reconcile-activation-evidence", ACTIVATION_ID, DIGEST, host.startup_recovery.generation),
            thread.call_args.kwargs["args"],
        )

    def test_the_recovery_source_never_reaches_the_ordinary_host_bridge(self) -> None:
        host = self.host()
        host.startup_recovery = armed()
        event = types.SimpleNamespace(
            TryGetWebMessageAsString=lambda: "workstack-window-theme|light",
            Source=NATIVE_RECOVERY_SOURCE,
        )

        with mock.patch.object(host, "_dispatch_workstack_host_message") as ordinary:
            host._on_workstack_message(None, event)

        ordinary.assert_not_called()

    def test_an_unreadable_native_event_is_ignored(self) -> None:
        host = self.host()

        def unreadable():
            raise RuntimeError("native failure")

        event = types.SimpleNamespace(
            TryGetWebMessageAsString=unreadable, Source=NATIVE_RECOVERY_SOURCE
        )
        with mock.patch.object(MODULE.threading, "Thread") as thread:
            host._on_workstack_message(None, event)

        thread.assert_not_called()
        self.assertFalse(host.startup_recovery.in_progress)

    def test_the_page_mints_request_ids_without_a_secure_context(self) -> None:
        # The page is loaded in memory, where the secure-context-gated uuid
        # generator is absent, so a request id built from it never posts at all.
        page = build_startup_recovery_html(
            reconcile_document(), capability=CAPABILITY, theme="light"
        )

        self.assertNotIn("randomUUID", page)
        self.assertIn("crypto.getRandomValues", page)

    def test_a_foreign_document_sharing_the_source_cannot_act_or_exit(self) -> None:
        # "about:blank" is what every in-memory document in this shell reports,
        # so the source alone separates nothing.  The per-render capability is
        # what a document that never rendered this page cannot produce.
        for forged in ("f" * 64, "", "0" * 63, "0" * 65, "NOTHEX" + "0" * 58):
            for operation in ("reconcile-activation-evidence", "exit"):
                with self.subTest(capability=forged, operation=operation):
                    host = self.host()
                    event = types.SimpleNamespace(
                        TryGetWebMessageAsString=lambda: request(
                            operation, capability=forged
                        ),
                        Source=NATIVE_RECOVERY_SOURCE,
                    )
                    with mock.patch.object(MODULE.threading, "Thread") as thread:
                        host._on_workstack_message(None, event)

                    thread.assert_not_called()
                    host.window.destroy.assert_not_called()
                    host.window.load_html.assert_not_called()
                    self.assertFalse(host.startup_recovery.in_progress)

    def test_a_capability_from_an_earlier_render_no_longer_acts(self) -> None:
        host = self.host()
        host.startup_recovery = armed(reconcile_document(), mint_startup_recovery_capability())
        superseded = request("reconcile-activation-evidence", capability=CAPABILITY)
        event = types.SimpleNamespace(
            TryGetWebMessageAsString=lambda: superseded,
            Source=NATIVE_RECOVERY_SOURCE,
        )

        with mock.patch.object(MODULE.threading, "Thread") as thread:
            host._on_workstack_message(None, event)

        thread.assert_not_called()
        self.assertFalse(host.startup_recovery.in_progress)

    def test_every_render_arms_a_fresh_unpredictable_capability(self) -> None:
        host = self.bare_host()
        host.connection_activation_recovery.inspect.return_value = reconcile_status()
        rendered = []
        for _ in range(3):
            with mock.patch.object(MODULE, "write_startup_error_log"):
                self.assertTrue(host._show_startup_activation_recovery())
            rendered.append(host.startup_recovery.capability)
            self.assertIn(host.startup_recovery.capability, host.window.load_html.call_args.args[0])

        self.assertEqual(3, len(set(rendered)))
        for capability in rendered:
            self.assertEqual(64, len(capability))
            self.assertTrue(all(character in "0123456789abcdef" for character in capability))

    def test_a_completed_action_arms_the_capability_of_the_page_it_loads(self) -> None:
        host = self.bare_host()
        host.startup_recovery = armed(reconcile_document())
        host.connection_activation_recovery.reconcile.return_value = (
            MODULE_RECOVERY.ActivationReconciliationReport(
                "resumed", "Duplicate connection activation records were closed.", status()
            )
        )

        host._apply_startup_recovery_action(
            "reconcile-activation-evidence", ACTIVATION_ID, DIGEST, host.startup_recovery.generation
        )

        page = host.window.load_html.call_args.args[0]
        self.assertNotEqual(CAPABILITY, host.startup_recovery.capability)
        self.assertIn(host.startup_recovery.capability, page)
        self.assertNotIn(CAPABILITY, page)

    def test_a_page_that_never_loads_arms_no_capability(self) -> None:
        host = self.bare_host()
        host.connection_activation_recovery.inspect.return_value = reconcile_status()
        host.window.load_html.side_effect = RuntimeError("native failure")

        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertFalse(host._show_startup_activation_recovery())

        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)

    def test_the_parser_requires_a_bounded_capability(self) -> None:
        self.assertIsNotNone(parse_startup_recovery_request(request("exit")))
        for rejected in (
            request("exit", capability="F" * 64),
            request("exit", capability="0" * 63),
            request("exit").replace('"capability": "%s", ' % CAPABILITY, ""),
            request("exit").replace('"capability"', '"capabilities"'),
        ):
            self.assertIsNone(parse_startup_recovery_request(rejected))


class StartupRecoveryLifetimeTest(unittest.TestCase):
    """A worker may publish only into the page generation it started from."""

    def host(self, status: dict[str, object] | None = None):
        host = DesktopStartupRecoveryIntegrationTest().bare_host()
        host.startup_recovery = armed(status or document())
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        return host

    def blocked_restore(self, host):
        """Start the real worker through the real dispatcher, stopped inside it.

        The barrier is the service call itself, so the worker is genuinely
        parked in the middle of the mutation while the test drives the events
        that retire its page; nothing here sleeps or polls.
        """

        entered, release, started = threading.Event(), threading.Event(), []
        # Bind the real class first: patching MODULE.threading patches this
        # module's threading too, and a factory that read it back would recurse.
        real_thread = threading.Thread

        def restore(_activation_id: str, *, expected_registry_digest: str) -> object:
            entered.set()
            self.assertTrue(release.wait(10))
            return mock.sentinel.restored

        host.connection_activation_recovery.restore.side_effect = restore

        def record(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            started.append(worker)
            return worker

        with mock.patch.object(MODULE.threading, "Thread", record):
            self.assertTrue(
                host._dispatch_startup_recovery_message(
                    request("restore-previous-connection")
                )
            )
        self.assertTrue(entered.wait(10))
        return release, started[0]

    def finish(self, release, worker) -> None:
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def test_a_worker_blocked_across_exit_neither_rearms_nor_renders(self) -> None:
        host = self.host()
        release, worker = self.blocked_restore(host)

        self.assertTrue(host._dispatch_startup_recovery_message(request("exit")))
        host.window.destroy.assert_called_once()
        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.finish(release, worker)

        # The restore was already running and may well have committed.  Closing
        # never claimed to undo it: it dropped the page that would have
        # described it, and it does not run the action a second time.
        host.connection_activation_recovery.restore.assert_called_once()
        host.window.load_html.assert_not_called()
        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.assertTrue(host.startup_recovery.closed)

        self.assertFalse(host._dispatch_startup_recovery_message(request("exit")))
        self.assertFalse(
            host._dispatch_startup_recovery_message(
                request("restore-previous-connection")
            )
        )
        host.window.destroy.assert_called_once()
        host.connection_activation_recovery.restore.assert_called_once()

    def test_a_worker_blocked_across_navigation_away_publishes_nothing(self) -> None:
        host = self.host()
        release, worker = self.blocked_restore(host)

        navigation = types.SimpleNamespace(Uri="http://127.0.0.1:8765/", Cancel=False)
        host._on_workstack_navigation_starting(None, navigation)
        self.assertFalse(navigation.Cancel)
        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.finish(release, worker)

        host.window.load_html.assert_not_called()
        self.assertIsNone(host.startup_recovery.status)
        self.assertFalse(host._dispatch_startup_recovery_message(request("exit")))
        host.window.destroy.assert_not_called()

        # Navigating away retires the page without sealing the flow, so a
        # later render still arms a capability of its own.
        host.connection_activation_recovery.inspect.return_value = reconcile_status()
        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertTrue(host._show_startup_activation_recovery())
        self.assertNotEqual(CAPABILITY, host.startup_recovery.capability)

    def test_accepted_navigation_away_disarms_the_recovery_document(self) -> None:
        host = self.host()

        navigation = types.SimpleNamespace(Uri="http://127.0.0.1:8765/app", Cancel=False)
        host._on_workstack_navigation_starting(None, navigation)

        self.assertFalse(navigation.Cancel)
        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.assertFalse(host.startup_recovery.in_progress)

    def test_closing_the_native_window_disarms_the_recovery_document(self) -> None:
        host = self.host()
        host.remote_shutdown_requested = mock.Mock()
        host.connection_registry_worker = mock.Mock()
        host._stop_knowledge_desktop = mock.Mock()
        host._stop_remote_monitor = mock.Mock()
        host.server_started_by_host = False
        host.remote_ssh_process = None
        host.server_stop_thread = None

        host._on_form_closing(None, None)

        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.assertTrue(host.startup_recovery.closed)

    def test_only_the_in_memory_document_a_render_started_is_admitted(self) -> None:
        host = self.host()

        own = types.SimpleNamespace(Uri="about:blank", Cancel=False)
        host._on_workstack_navigation_starting(None, own)
        self.assertFalse(own.Cancel)

        # The recovery page reports the source every in-memory document in this
        # shell reports, so a second about:blank that no render of ours started
        # is not this page and is refused without disturbing what is on screen.
        unknown = types.SimpleNamespace(Uri="about:blank", Cancel=False)
        host._on_workstack_navigation_starting(None, unknown)
        self.assertTrue(unknown.Cancel)
        self.assertEqual(document(), host.startup_recovery.status)
        self.assertEqual(CAPABILITY, host.startup_recovery.capability)

    def test_the_document_the_runtime_really_reports_for_our_render_is_admitted(self) -> None:
        """The shipped render must reach the DOM on the runtime that inlines it.

        This runtime reports a ``NavigateToString`` navigation as the whole
        rendered document, base64 behind ``data:text/html;charset=utf-8;base64,``.
        The page the host actually loaded is read back off ``load_html``, so the
        admitted target is the product's own bytes and not a test fixture.
        """

        host = DesktopStartupRecoveryIntegrationTest().bare_host()
        host.workstack_origin = ("http", "127.0.0.1", 8765)
        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertTrue(host._show_startup_activation_recovery())
        page = host.window.load_html.call_args.args[0]

        own = types.SimpleNamespace(Uri=own_navigation(page), Cancel=False)
        host._on_workstack_navigation_starting(None, own)

        self.assertFalse(own.Cancel)
        self.assertEqual(document(), host.startup_recovery.status)

        # One render admits one navigation: replaying the very document that
        # was just admitted is refused like any other foreign document.
        replay = types.SimpleNamespace(Uri=own_navigation(page), Cancel=False)
        host._on_workstack_navigation_starting(None, replay)
        self.assertTrue(replay.Cancel)
        self.assertEqual(document(), host.startup_recovery.status)

    def test_a_foreign_data_document_is_refused_however_close_it_looks(self) -> None:
        host = self.host()
        armed_page = rendered_page(document())

        foreign = [
            own_navigation("<!doctype html><html><body>foreign</body></html>"),
            own_navigation(armed_page + " "),
            own_navigation(armed_page)[:-4],
            "data:text/html;charset=utf-8;base64,",
            "data:text/html,%3Ch1%3Edata-origin-probe%3C/h1%3E",
            "data:text/html;base64,abc",
        ]
        for target in foreign:
            with self.subTest(target=target[:48]):
                navigation = types.SimpleNamespace(Uri=target, Cancel=False)
                host._on_workstack_navigation_starting(None, navigation)
                self.assertTrue(navigation.Cancel)

        # None of them disturbed, consumed or replaced the page on screen.
        self.assertEqual(document(), host.startup_recovery.status)
        self.assertEqual(CAPABILITY, host.startup_recovery.capability)
        still_ours = types.SimpleNamespace(Uri=own_navigation(armed_page), Cancel=False)
        host._on_workstack_navigation_starting(None, still_ours)
        self.assertFalse(still_ours.Cancel)

    def test_an_escaped_report_of_our_own_document_is_still_our_document(self) -> None:
        """A runtime that escapes the inlined document still renders our page.

        Decoding is exact, so it only ever matches the same bytes: an escaped
        foreign document decodes to a foreign document and stays refused.
        """

        host = self.host()
        escaped = own_navigation(rendered_page(document())).replace("+", "%2B").replace("/", "%2F")

        foreign = types.SimpleNamespace(
            Uri=own_navigation("<p>foreign</p>").replace("+", "%2B"), Cancel=False
        )
        host._on_workstack_navigation_starting(None, foreign)
        self.assertTrue(foreign.Cancel)

        own = types.SimpleNamespace(Uri=escaped, Cancel=False)
        host._on_workstack_navigation_starting(None, own)
        self.assertFalse(own.Cancel)

    def test_a_retired_page_no_longer_admits_the_document_it_rendered(self) -> None:
        host = self.host()
        stale = own_navigation(rendered_page(document()))

        away = types.SimpleNamespace(Uri="http://127.0.0.1:8765/app", Cancel=False)
        host._on_workstack_navigation_starting(None, away)
        self.assertFalse(away.Cancel)

        exited = self.host()
        self.assertTrue(exited._dispatch_startup_recovery_message(request("exit")))

        for host_label, retired in (("navigated-away", host), ("exited", exited)):
            for label, target in (("data", stale), ("opaque", "about:blank")):
                with self.subTest(retired=host_label, shape=label):
                    navigation = types.SimpleNamespace(Uri=target, Cancel=False)
                    retired._on_workstack_navigation_starting(None, navigation)
                    self.assertTrue(navigation.Cancel)

        # A later render arms its own document only.  The retired one does not
        # come back to life behind it.
        host.connection_activation_recovery.inspect.return_value = reconcile_status()
        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertTrue(host._show_startup_activation_recovery())
        fresh = host.window.load_html.call_args.args[0]
        self.assertNotEqual(stale, own_navigation(fresh))

        replayed = types.SimpleNamespace(Uri=stale, Cancel=False)
        host._on_workstack_navigation_starting(None, replayed)
        self.assertTrue(replayed.Cancel)

        current = types.SimpleNamespace(Uri=own_navigation(fresh), Cancel=False)
        host._on_workstack_navigation_starting(None, current)
        self.assertFalse(current.Cancel)

    def test_a_newer_render_supersedes_a_worker_that_started_from_the_old_page(self) -> None:
        host = self.host()
        release, worker = self.blocked_restore(host)
        host.connection_activation_recovery.inspect.return_value = reconcile_status()

        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertTrue(host._show_startup_activation_recovery())
        newer = host.startup_recovery.capability
        self.assertEqual(1, host.window.load_html.call_count)
        self.finish(release, worker)

        self.assertEqual(newer, host.startup_recovery.capability)
        self.assertEqual(reconcile_document(), host.startup_recovery.status)
        self.assertEqual(1, host.window.load_html.call_count)

        # Exactly one render is outstanding, so the superseded page's document
        # is no longer admitted while the newer one still is.
        superseded = types.SimpleNamespace(
            Uri=own_navigation(rendered_page(document())), Cancel=False
        )
        host._on_workstack_navigation_starting(None, superseded)
        self.assertTrue(superseded.Cancel)
        current = types.SimpleNamespace(
            Uri=own_navigation(host.window.load_html.call_args.args[0]), Cancel=False
        )
        host._on_workstack_navigation_starting(None, current)
        self.assertFalse(current.Cancel)

    def test_a_page_that_cannot_reach_the_screen_leaves_nothing_armed(self) -> None:
        host = self.host()
        host.window = None
        host.connection_activation_recovery.inspect.return_value = reconcile_status()

        with mock.patch.object(MODULE, "write_startup_error_log"):
            self.assertFalse(host._show_startup_activation_recovery())

        self.assertIsNone(host.startup_recovery.status)
        self.assertIsNone(host.startup_recovery.capability)
        self.assertFalse(host.startup_recovery.in_progress)


class ReconciliationTruthfulnessTest(unittest.TestCase):
    """A page that follows a reconciliation must describe the real disk state."""

    def prepared(self, root: Path):
        _original, candidate, mutations, kept = (
            recovery_fixtures.ActivationRecoveryTest()._activate(root)
        )
        duplicate = recovery_fixtures.plant_duplicate_pending_receipt(root, kept)
        return candidate, mutations, kept, duplicate

    def host(self, service, advertised):
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.connection_activation_recovery = service
        host.startup_recovery = armed(
            MODULE_RECOVERY.activation_recovery_status_to_document(advertised),
            accepts_next_action=False,
        )
        host.current_theme = "light"
        host.window = mock.Mock()
        return host

    def click(self, host, advertised):
        host._apply_startup_recovery_action(
            "reconcile-activation-evidence",
            advertised.activation_id,
            advertised.current_registry_digest,
            host.startup_recovery.generation,
        )
        return host.window.load_html.call_args.args[0]

    def state_of(self, root: Path, receipt) -> str:
        return MUTATIONS.load_activation_receipt(root, receipt.activation_id).state

    def fault_on_second_receipt(self):
        """Fail the second compare-and-swap the way a real evidence race does."""

        original = MUTATIONS._replace_receipt_if_digest
        calls: list[str] = []

        def faulted(path, receipt, expected_digest):
            calls.append(receipt.activation_id)
            if len(calls) == 2:
                raise MUTATIONS.RegistryConflictError("Activation receipt changed")
            return original(path, receipt, expected_digest)

        return mock.patch.object(
            MUTATIONS, "_replace_receipt_if_digest", side_effect=faulted
        )

    def test_a_kept_rollback_changing_before_the_core_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, duplicate = self.prepared(root)
            advertised = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root
            ).inspect()

            class RetainedRollbackChangesBeforeCore:
                def reconcile_activations(self, **fields: object):
                    rollback = root / MUTATIONS.ACTIVATION_DIRECTORY / kept.rollback_file
                    rollback.write_bytes(b"{}")
                    return mutations.reconcile_activations(**fields)

            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=RetainedRollbackChangesBeforeCore()
            )
            host = self.host(service, advertised)
            before = recovery_fixtures.byte_tree(root)

            page = self.click(host, advertised)

            # The kept receipt is validated inside the lock before the first
            # transition, so the only changed bytes are the race's own, and the
            # refusal the page shows describes what is really on disk.
            raced = str(Path(MUTATIONS.ACTIVATION_DIRECTORY) / kept.rollback_file)
            self.assertEqual(
                {**before, raced: b"{}"}, recovery_fixtures.byte_tree(root)
            )
            self.assertEqual("pending", self.state_of(root, duplicate))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertIn("Duplicate records could not be closed", page)
            self.assertNotIn("data-operation=", page)

    def test_foreign_evidence_after_the_commit_reports_the_committed_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, duplicate = self.prepared(root)
            advertised = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root
            ).inspect()

            class EvidenceArrivesAfterCoreCommit:
                def reconcile_activations(self, **fields: object):
                    outcome = mutations.reconcile_activations(**fields)
                    recovery_fixtures.plant_foreign_pending_receipt(root, kept)
                    return outcome

            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=EvidenceArrivesAfterCoreCommit()
            )
            host = self.host(service, advertised)

            page = self.click(host, advertised)

            self.assertEqual("superseded", self.state_of(root, duplicate))
            self.assertEqual("pending", self.state_of(root, kept))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertNotIn("Duplicate records could not be closed", page)
            self.assertIn("Connection records need review", page)
            self.assertIn("were closed and the connection", page)
            self.assertNotIn("data-operation=", page)
            self.assertTrue(host.startup_recovery.in_progress)

    def test_a_second_receipt_fault_reports_incomplete_and_keeps_every_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, first = self.prepared(root)
            second = recovery_fixtures.plant_second_duplicate_pending_receipt(root, kept)
            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            host = self.host(service, advertised)
            rollbacks = {
                receipt.rollback_file: (
                    root / MUTATIONS.ACTIVATION_DIRECTORY / receipt.rollback_file
                ).read_bytes()
                for receipt in (kept, first, second)
            }

            with self.fault_on_second_receipt():
                page = self.click(host, advertised)

            states = [self.state_of(root, receipt) for receipt in (first, second)]
            self.assertEqual(1, states.count("superseded"))
            self.assertEqual(1, states.count("pending"))
            self.assertEqual("pending", self.state_of(root, kept))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertIn("Connection records need review", page)
            self.assertIn("closed and others were not", page)
            self.assertNotIn("data-operation=", page)
            # Nothing is purged, so a later launch can still resume or restore.
            for name, payload in rollbacks.items():
                path = root / MUTATIONS.ACTIVATION_DIRECTORY / name
                self.assertTrue(path.is_file())
                self.assertEqual(payload, path.read_bytes())

    def test_a_restart_resumes_the_remaining_duplicate_and_restores_the_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _candidate, mutations, kept, first = self.prepared(root)
            second = recovery_fixtures.plant_second_duplicate_pending_receipt(root, kept)
            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()

            with self.fault_on_second_receipt():
                self.click(self.host(service, advertised), advertised)

            # A restart re-inspects and finds a plan that is still provable.
            restarted = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(root)
            resumed = restarted.inspect()
            self.assertEqual("can_reconcile", resumed.state)
            self.assertEqual(kept.activation_id, resumed.activation_id)

            report = restarted.reconcile(
                resumed.activation_id,
                expected_registry_digest=resumed.current_registry_digest,
            )

            self.assertEqual("resumed", report.state)
            self.assertEqual("recovery_required", report.status.state)
            for receipt in (first, second):
                self.assertEqual("superseded", self.state_of(root, receipt))
            result = restarted.restore(
                kept.activation_id,
                expected_registry_digest=resumed.current_registry_digest,
            )
            self.assertEqual("restored", result.state)
            self.assertEqual(
                kept.previous_registry_digest, result.restored_registry_digest
            )

    def test_records_that_cannot_be_read_back_report_uncertainty_with_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _candidate, mutations, _kept, duplicate = self.prepared(root)
            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            host = self.host(service, advertised)

            committed: list[str] = []
            write = MUTATIONS._replace_receipt_if_digest
            read = MUTATIONS.load_activation_receipt

            def record(path, receipt, expected_digest):
                write(path, receipt, expected_digest)
                committed.append(receipt.activation_id)

            def unreadable_after_the_write(state_root, activation_id):
                if committed:
                    raise RuntimeError("Could not inspect activation records")
                return read(state_root, activation_id)

            with (
                mock.patch.object(
                    MUTATIONS, "_replace_receipt_if_digest", side_effect=record
                ),
                mock.patch.object(
                    MUTATIONS,
                    "load_activation_receipt",
                    side_effect=unreadable_after_the_write,
                ),
            ):
                page = self.click(host, advertised)

            self.assertEqual("superseded", self.state_of(root, duplicate))
            self.assertIn("Connection records need review", page)
            self.assertIn("could not confirm", page)
            self.assertIn("review the connection records manually", page)
            self.assertNotIn("data-operation=", page)

    def test_an_unexpected_second_receipt_fault_reports_the_partial_change(self) -> None:
        """An unexpected error after a real write may not claim nothing moved."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, first = self.prepared(root)
            second = recovery_fixtures.plant_second_duplicate_pending_receipt(root, kept)
            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            host = self.host(service, advertised)
            write = MUTATIONS._replace_receipt_if_digest
            calls: list[str] = []

            def unexpected_on_second(path, receipt, expected_digest):
                calls.append(receipt.activation_id)
                if len(calls) == 2:
                    raise KeyError("activation_id")
                return write(path, receipt, expected_digest)

            with mock.patch.object(
                MUTATIONS, "_replace_receipt_if_digest", side_effect=unexpected_on_second
            ):
                page = self.click(host, advertised)

            states = [self.state_of(root, receipt) for receipt in (first, second)]
            self.assertEqual(1, states.count("superseded"))
            self.assertEqual(1, states.count("pending"))
            self.assertEqual("pending", self.state_of(root, kept))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertIn("Connection records need review", page)
            self.assertIn("closed and others were not", page)
            self.assertNotIn("Duplicate records could not be closed", page)
            self.assertNotIn("data-operation=", page)
            self.assertTrue(host.startup_recovery.in_progress)
            for receipt in (kept, first, second):
                self.assertTrue(
                    (
                        root / MUTATIONS.ACTIVATION_DIRECTORY / receipt.rollback_file
                    ).is_file()
                )

    def test_an_unavailable_post_write_inspection_reports_uncertainty(self) -> None:
        """A committed core plus an unexpectedly unavailable inspection is uncertain."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, duplicate = self.prepared(root)
            service = MODULE_RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            inspect = service.inspect
            calls: list[str] = []

            def unavailable_after_the_write():
                calls.append("inspect")
                if len(calls) > 1:
                    raise KeyError("activation_id")
                return inspect()

            service.inspect = unavailable_after_the_write
            host = self.host(service, advertised)

            page = self.click(host, advertised)

            self.assertEqual(2, len(calls))
            self.assertEqual("superseded", self.state_of(root, duplicate))
            self.assertEqual("pending", self.state_of(root, kept))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertIn("Connection records need review", page)
            self.assertIn("could not confirm", page)
            self.assertNotIn("Duplicate records could not be closed", page)
            self.assertNotIn("data-operation=", page)
            self.assertTrue(host.startup_recovery.in_progress)


if __name__ == "__main__":
    unittest.main()
