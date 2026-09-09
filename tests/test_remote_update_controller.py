"""The native remote update controller, over the actual D flow and E page.

Every test here drives a real ``RemoteUpdateFlow`` through disposable effect
ports, projects it with the actual ``remote_update_projection``, and admits
clicks through the actual ``RemoteUpdateViewSession``.  No alternate state
machine, no fabricated R6 document and no stand-in for the page stand between
the controller and the modules it is supposed to compose.

The worker is the real ``BoundedRequestWorker``, so the off-UI-thread
property is observed rather than asserted: every port records the thread it
ran on.  The host double queues UI work instead of running it, so a test
decides exactly when a completion is allowed to try to paint.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_update_controller as CONTROLLER
import remote_update_diagnostics as DIAGNOSTICS
import remote_update_host_bridge as BRIDGE
import remote_update_projection as PROJECT
from remote_update_controller import (
    RemoteUpdateController,
    RemoteUpdateHostHooks,
    RemoteUpdateSelection,
)
from remote_update_flow import RemoteUpdateFlow
from remote_update_flow_contract import (
    ActivationOutcome,
    BackupOutcome,
    LostResponse,
    OwnerStopFacts,
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    RemoteUpdatePorts,
    RestoreOutcome,
    RollbackOutcome,
    StartupEvidence,
    VerifyOutcome,
)
from remote_update_presentation import present_remote_update
from remote_update_view import (
    REMOTE_UPDATE_REQUEST_TYPE,
    RemoteUpdateViewSession,
)


SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_SESSION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
REQUEST_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
OTHER_REQUEST_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
WAIT_SECONDS = 10.0

PREVIEW_OK = PreviewFacts(
    desktop_version="1.0.14",
    remote_version="1.0.13",
    served_ui_version="1.0.13",
    protocol_version="7",
    schema_version_before="v5",
    schema_version_target="v6",
    migration_required=True,
    install_capability="available",
    install_method="verified_unpack",
)
STOP_OK = OwnerStopFacts(
    state="dead",
    token_available=True,
    pidfd_available=True,
    process_exit="verified",
    listener_release="verified",
    lease_release="verified",
)
BACKUP_OK = BackupOutcome(status="verified", migration_required=True)
PREPARE_OK = PrepareOutcome(
    status="verified",
    capability="available",
    method="verified_unpack",
    previous_app_retained=True,
    previous_profile_retained=True,
)
PROBE_OK = ProbeOutcome(
    status="verified",
    workspace_match="verified",
    served_ui_version="1.0.14",
    protocol_version="7",
)
ACTIVATE_OK = ActivationOutcome(
    status="verified", committed=True, state="pending", restart_required=True
)
CONFIRM_OK = ActivationOutcome(
    status="verified",
    committed=True,
    state="confirmed",
    remote_version="1.0.14",
    schema_version="v6",
)
VERIFY_OK = VerifyOutcome(
    status="verified",
    desktop_version="1.0.14",
    remote_version="1.0.14",
    served_ui_version="1.0.14",
    protocol_version="7",
    schema_version="v6",
)


class Port:
    """One scripted D effect port that records where each call ran."""

    def __init__(self, calls: list, name: str, **scripts: object) -> None:
        self._calls = calls
        self._name = name
        self._scripts = {key: list(value) for key, value in scripts.items()}

    def take(self, method: str, operation_id: str) -> object:
        self._calls.append((f"{self._name}.{method}", operation_id, threading.get_ident()))
        results = self._scripts.get(method)
        if not results:
            raise AssertionError(f"unscripted call to {self._name}.{method}")
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class PreviewPort(Port):
    def describe(self) -> object:
        return self.take("describe", "")


class OwnerPort(Port):
    def stop(self, operation_id: str) -> object:
        return self.take("stop", operation_id)

    def observe(self, operation_id: str) -> object:
        return self.take("observe", operation_id)


class BackupPort(Port):
    def create_verified(self, operation_id: str) -> object:
        return self.take("create_verified", operation_id)

    def observe(self, operation_id: str) -> object:
        return self.take("observe", operation_id)

    def restore(self, operation_id: str) -> object:
        return self.take("restore", operation_id)

    def observe_restore(self, operation_id: str) -> object:
        return self.take("observe_restore", operation_id)


class PreparePort(Port):
    def prepare(self, operation_id: str) -> object:
        return self.take("prepare", operation_id)

    def observe(self, operation_id: str) -> object:
        return self.take("observe", operation_id)


class ProbePort(Port):
    def probe(self, operation_id: str) -> object:
        return self.take("probe", operation_id)


class ActivationPort(Port):
    def activate(self, operation_id: str) -> object:
        return self.take("activate", operation_id)

    def observe(self, operation_id: str) -> object:
        return self.take("observe", operation_id)

    def confirm(self, operation_id: str) -> object:
        return self.take("confirm", operation_id)

    def observe_confirm(self, operation_id: str) -> object:
        return self.take("observe_confirm", operation_id)

    def rollback(self, operation_id: str) -> object:
        return self.take("rollback", operation_id)

    def observe_rollback(self, operation_id: str) -> object:
        return self.take("observe_rollback", operation_id)


class VerificationPort(Port):
    def verify(self, operation_id: str) -> object:
        return self.take("verify", operation_id)


class Flow:
    """An actual ``RemoteUpdateFlow`` over scripted ports."""

    def __init__(self, **scripts: object) -> None:
        self.calls: list = []
        self.issued: list[str] = []
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=PreviewPort(
                    self.calls, "preview", describe=scripts.get("preview", (PREVIEW_OK,))
                ),
                owner=OwnerPort(
                    self.calls,
                    "owner",
                    stop=scripts.get("stop", (STOP_OK,)),
                    observe=scripts.get("stop_observe", ()),
                ),
                backup=BackupPort(
                    self.calls,
                    "backup",
                    create_verified=scripts.get("backup", (BACKUP_OK,)),
                    observe=scripts.get("backup_observe", ()),
                    restore=scripts.get("restore", ()),
                    observe_restore=scripts.get("restore_observe", ()),
                ),
                prepare=PreparePort(
                    self.calls,
                    "prepare",
                    prepare=scripts.get("prepare", (PREPARE_OK,)),
                    observe=scripts.get("prepare_observe", ()),
                ),
                probe=ProbePort(self.calls, "probe", probe=scripts.get("probe", (PROBE_OK,))),
                activation=ActivationPort(
                    self.calls,
                    "activation",
                    activate=scripts.get("activate", (ACTIVATE_OK,)),
                    observe=scripts.get("activate_observe", ()),
                    confirm=scripts.get("confirm", (CONFIRM_OK,)),
                    observe_confirm=scripts.get("confirm_observe", ()),
                    rollback=scripts.get("rollback", ()),
                    observe_rollback=scripts.get("rollback_observe", ()),
                ),
                verification=VerificationPort(
                    self.calls, "verification", verify=scripts.get("verify", (VERIFY_OK,))
                ),
            ),
            operation_ids=self._operation_id,
            journal=None,
        )

    def _operation_id(self, kind: str) -> str:
        issue = f"op-{kind}-{1 + sum(1 for name in self.issued if name == kind)}"
        self.issued.append(kind)
        return issue

    def run(self, steps: int) -> None:
        for _ in range(steps):
            self.flow.advance()

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def threads(self, name: str) -> list[int]:
        return [call[2] for call in self.calls if call[0] == name]


class Host:
    """The finite host seam: a queued UI dispatcher and bounded callbacks."""

    def __init__(self, selection: object, pc_actions: tuple[str, ...] = ()) -> None:
        self.selection = selection
        self.pages: list[str] = []
        self.retired = 0
        self.restarts = 0
        self.evidence: object = None
        self.clipboard: list[str] = []
        self.pc_calls: list[str] = []
        self.pc_offer = pc_actions
        self.traces: list[str] = []
        self._ui_lock = threading.Lock()
        self._ui: list = []

    def hooks(self) -> RemoteUpdateHostHooks:
        return RemoteUpdateHostHooks(
            selected_session=lambda: self.selection,
            render=self.pages.append,
            dispatch_ui=self.dispatch_ui,
            retire_view=self.retire_view,
            restart_desktop=self.restart_desktop,
            restart_evidence=lambda: self.evidence,
            copy_diagnostics=self.copy_diagnostics,
            pc_check=lambda: self.pc_calls.append("check"),
            pc_download=lambda: self.pc_calls.append("download"),
            pc_install=lambda: self.pc_calls.append("install"),
            pc_actions=lambda: self.pc_offer,
            theme=lambda: "dark",
        )

    def dispatch_ui(self, action: object) -> bool:
        with self._ui_lock:
            self._ui.append(action)
        return True

    def queued(self) -> int:
        with self._ui_lock:
            return len(self._ui)

    def pump(self) -> int:
        with self._ui_lock:
            pending, self._ui = self._ui, []
        for action in pending:
            action()
        return len(pending)

    def retire_view(self) -> None:
        self.retired += 1

    def restart_desktop(self) -> bool:
        self.restarts += 1
        return True

    def copy_diagnostics(self, report: str) -> bool:
        self.clipboard.append(report)
        return True


def wait_for(predicate, message: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {message}")


def click(operation: str, capability: str, request_id: str = REQUEST_ID) -> str:
    return json.dumps({
        "type": REMOTE_UPDATE_REQUEST_TYPE,
        "schema_version": 1,
        "request_id": request_id,
        "capability": capability,
        "operation": operation,
    })


class Fixture:
    """One controller over one real flow and one queued host."""

    def __init__(self, flow: Flow, pc_actions: tuple[str, ...] = ()) -> None:
        self.flow = flow
        self.views: list[RemoteUpdateViewSession] = []
        self.selection = RemoteUpdateSelection(SESSION, WORKSPACE, flow.flow)
        self.host = Host(self.selection, pc_actions)
        self.controller = RemoteUpdateController(
            self.host.hooks(),
            view_factory=self._view,
            trace=self.host.traces.append,
        )

    def _view(self) -> RemoteUpdateViewSession:
        view = RemoteUpdateViewSession()
        self.views.append(view)
        return view

    def capability(self) -> str:
        return str(self.views[-1].capability)

    def next_action(self) -> str | None:
        return present_remote_update(self.views[-1].snapshot).next_action

    def click_next(self, request_id: str = REQUEST_ID) -> bool:
        action = self.next_action()
        assert action is not None, "the page offers no next action"
        return self.controller.handle_web_message(
            click(action, self.capability(), request_id)
        )

    def settle(self) -> None:
        """Wait for the worker's completion and let it try to paint."""

        wait_for(self.host.queued, "the completion to reach the UI dispatcher")
        self.host.pump()

    def close(self) -> None:
        self.controller.close()


class ControllerMountTests(unittest.TestCase):
    def test_open_paints_one_admissible_next_action(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        self.assertTrue(fixture.controller.open())
        self.assertEqual(len(fixture.host.pages), 1)
        # The flow is idle, so D offers run_preview and the page turns that
        # into exactly one clickable next action.
        self.assertEqual(fixture.next_action(), "preview_server_update")
        self.assertIn("Preview server update", fixture.host.pages[0])
        self.assertIn(fixture.capability(), fixture.host.pages[0])
        self.assertEqual(fixture.flow.names(), [])

    def test_open_refuses_without_a_selected_session(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.host.selection = None
        self.assertFalse(fixture.controller.open())
        self.assertEqual(fixture.host.pages, [])
        self.assertFalse(fixture.controller.is_open)

    def test_open_refuses_a_selection_without_a_real_flow(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.host.selection = RemoteUpdateSelection(SESSION, WORKSPACE, object())
        self.assertFalse(fixture.controller.open())
        self.assertEqual(fixture.host.pages, [])

    def test_navigation_admits_only_the_document_just_rendered(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertTrue(fixture.controller.admits_navigation("about:blank"))
        self.assertFalse(fixture.controller.admits_navigation("https://example.invalid/"))
        fixture.controller.retire()
        self.assertFalse(fixture.controller.admits_navigation("about:blank"))
        self.assertEqual(fixture.host.retired, 1)

    def test_a_foreign_message_is_not_this_page(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertFalse(fixture.controller.handle_web_message('{"type":"other"}'))
        self.assertEqual(len(fixture.host.pages), 1)

    def test_a_wrong_capability_is_recognized_but_changes_nothing(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        forged = click("preview_server_update", "0" * 64)
        self.assertTrue(fixture.controller.handle_web_message(forged))
        self.assertEqual(fixture.flow.names(), [])
        self.assertEqual(fixture.host.queued(), 0)


class ControllerDispatchTests(unittest.TestCase):
    def test_one_click_drives_the_real_flow_off_the_ui_thread(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(fixture.flow.names(), ["preview.describe"])
        self.assertNotIn(threading.get_ident(), fixture.flow.threads("preview.describe"))
        self.assertEqual(len(fixture.host.pages), 2)
        self.assertEqual(fixture.views[-1].snapshot.stage, "preview")

    def test_a_repeated_click_cannot_issue_the_stage_twice(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        capability = fixture.capability()
        message = click("preview_server_update", capability)
        self.assertTrue(fixture.controller.handle_web_message(message))
        # The same click again, and a distinct request id for the same action.
        self.assertTrue(fixture.controller.handle_web_message(message))
        self.assertTrue(
            fixture.controller.handle_web_message(
                click("preview_server_update", capability, OTHER_REQUEST_ID)
            )
        )
        fixture.settle()
        self.assertEqual(fixture.flow.names(), ["preview.describe"])

    def test_a_stale_click_is_ignored_and_the_page_is_refreshed(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        message = click("preview_server_update", fixture.capability())
        # The flow moves on outside the page, exactly as a restart-resume or
        # another mount would move it.
        fixture.flow.flow.advance()
        self.assertTrue(fixture.controller.handle_web_message(message))
        self.assertEqual(fixture.flow.names(), ["preview.describe"])
        fixture.host.pump()
        self.assertEqual(len(fixture.host.pages), 2)
        self.assertIn(
            "remote update click not dispatched: stale_stage", fixture.host.traces
        )

    def test_pc_actions_reach_their_own_callbacks_and_never_the_flow(self) -> None:
        fixture = Fixture(Flow(), pc_actions=("download_this_pc",))
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertEqual(fixture.next_action(), "download_this_pc")
        self.assertTrue(fixture.click_next())
        fixture.host.pump()
        self.assertEqual(fixture.host.pc_calls, ["download"])
        self.assertEqual(fixture.flow.names(), [])
        self.assertEqual(len(fixture.host.pages), 2)

    def test_review_copies_the_bounded_diagnostic_report(self) -> None:
        flow = Flow()
        flow.flow.cancel()
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        # Cancelled offers dismiss and inspect_diagnostics; close is never the
        # next action, so review is.
        self.assertEqual(fixture.next_action(), "review")
        self.assertTrue(fixture.click_next())
        fixture.host.pump()
        self.assertEqual(len(fixture.host.clipboard), 1)
        report = fixture.host.clipboard[0]
        self.assertIn("CANCELLED_BEFORE_PREVIEW", report)
        self.assertLessEqual(len(report), DIAGNOSTICS.MAX_REPORT_CHARACTERS)
        self.assertNotIn(fixture.capability(), report)
        self.assertNotIn(SESSION, report)
        self.assertNotIn(WORKSPACE, report)

    def test_close_retires_the_page_and_never_reopens_that_session(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertTrue(
            fixture.controller.handle_web_message(click("close", fixture.capability()))
        )
        self.assertFalse(fixture.controller.is_open)
        self.assertEqual(fixture.host.retired, 1)
        self.assertEqual(fixture.flow.names(), [])
        # A second mount is a new page over the same flow, with a new
        # capability, because the retired one is closed for good.
        self.assertTrue(fixture.controller.open())
        self.assertEqual(len(fixture.views), 2)
        self.assertNotEqual(fixture.views[0].capability, fixture.views[1].capability)


class ControllerStalePaintTests(unittest.TestCase):
    def test_a_completion_after_close_paints_nothing(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        fixture.click_next()
        wait_for(fixture.host.queued, "the completion to reach the UI dispatcher")
        fixture.controller.retire(closed=True)
        painted = len(fixture.host.pages)
        fixture.host.pump()
        self.assertEqual(len(fixture.host.pages), painted)
        self.assertEqual(fixture.flow.names(), ["preview.describe"])

    def test_a_completion_after_a_profile_change_retires_instead(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        fixture.click_next()
        wait_for(fixture.host.queued, "the completion to reach the UI dispatcher")
        fixture.host.selection = RemoteUpdateSelection(
            OTHER_SESSION, WORKSPACE, Flow().flow
        )
        painted = len(fixture.host.pages)
        fixture.host.pump()
        self.assertEqual(len(fixture.host.pages), painted)
        self.assertEqual(fixture.host.retired, 1)
        self.assertFalse(fixture.controller.is_open)

    def test_a_click_after_a_profile_change_is_refused(self) -> None:
        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        message = click("preview_server_update", fixture.capability())
        fixture.host.selection = RemoteUpdateSelection(
            OTHER_SESSION, WORKSPACE, Flow().flow
        )
        self.assertTrue(fixture.controller.handle_web_message(message))
        self.assertEqual(fixture.flow.names(), [])
        self.assertEqual(fixture.host.retired, 1)
        self.assertEqual(fixture.host.queued(), 0)


class ControllerReconcileTests(unittest.TestCase):
    def _lost_stop(self) -> Flow:
        flow = Flow(
            stop=(LostResponse("op-owner_stop-1"),),
            stop_observe=(STOP_OK,),
        )
        flow.run(1)
        try:
            flow.flow.advance()
        except Exception:  # pragma: no cover - the lost response is settled below
            raise AssertionError("the lost response must not escape the flow")
        return flow

    def test_a_lost_response_reconciles_the_same_operation(self) -> None:
        flow = self._lost_stop()
        pending = flow.flow.pending_operation()
        self.assertIsNotNone(pending)
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        # An unsettled identity offers exactly one thing: settle that identity.
        self.assertEqual(fixture.next_action(), "reconcile_pending")
        resolved = PROJECT.resolve_remote_update_action(
            fixture.views[-1].admit(click("reconcile_pending", fixture.capability())),
            fixture.controller.projection,
            current=flow.flow,
            session_id=SESSION,
            workspace_id=WORKSPACE,
        )
        self.assertEqual(resolved.host_kind, "reconcile")
        self.assertTrue(resolved.read_only)
        self.assertFalse(resolved.mutation)

    def test_reconcile_observes_the_retained_identity_and_issues_nothing(self) -> None:
        flow = self._lost_stop()
        operation_id = flow.flow.pending_operation().operation_id
        issued_before = list(flow.issued)
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(
            flow.names(), ["preview.describe", "owner.stop", "owner.observe"]
        )
        self.assertEqual(flow.calls[-1][1], operation_id)
        self.assertEqual(flow.issued, issued_before)
        self.assertIsNone(flow.flow.pending_operation())


class ControllerRestartTests(unittest.TestCase):
    def _pending_activation(self) -> Flow:
        flow = Flow()
        flow.run(6)
        return flow

    def test_restart_hands_off_to_the_host_and_confirms_nothing(self) -> None:
        flow = self._pending_activation()
        anchor = flow.flow.retained_activation()
        self.assertIsNotNone(anchor)
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertEqual(fixture.next_action(), "restart")
        self.assertTrue(fixture.click_next())
        self.assertEqual(fixture.host.restarts, 1)
        self.assertNotIn("activation.confirm", flow.names())
        self.assertEqual(flow.flow.retained_activation().operation_id, anchor.operation_id)

    def test_resume_confirms_the_retained_receipt_on_real_evidence(self) -> None:
        flow = self._pending_activation()
        anchor_id = flow.flow.retained_activation().operation_id
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        fixture.host.evidence = StartupEvidence(
            selected_activation_id=anchor_id,
            authority_selected="verified",
            workspace_match="verified",
        )
        self.assertTrue(fixture.controller.resume_after_restart())
        fixture.settle()
        self.assertEqual(flow.calls[-1][0], "activation.confirm")
        self.assertEqual(flow.calls[-1][1], anchor_id)
        self.assertNotIn(threading.get_ident(), flow.threads("activation.confirm"))

    def test_resume_without_evidence_leaves_the_receipt_pending(self) -> None:
        flow = self._pending_activation()
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertFalse(fixture.controller.resume_after_restart())
        self.assertNotIn("activation.confirm", flow.names())
        self.assertEqual(fixture.host.queued(), 0)


class ControllerJourneyTests(unittest.TestCase):
    """What the healthy journey actually costs in clicks, gap included."""

    def test_the_owner_stop_is_reachable_once_d_itself_offers_it(self) -> None:
        """The gap this test used to pin is closed, and the click is real.

        After ``preview_ready`` the flow offers ``run_stop`` and the projector
        maps it to ``stop_owner``.  Nothing has observed the owner yet -- the
        preview port carries no owner facts -- so the snapshot's owner is
        all-unknown, and E used to suppress the action for that reason and fall
        through to ``review``.  The frozen E correction admits D's *own* offered
        stop as the authority check, because D is the component that knows
        whether the stop is legal, so the stop is now the page's next action and
        one click drives the real ``run_stop`` operation.

        What is not admitted has not moved: a foreign host and a missing session
        token are still refusals, pinned by ``AdmissionTests`` alongside this.
        """

        fixture = Fixture(Flow())
        self.addCleanup(fixture.close)
        fixture.controller.open()
        fixture.click_next()
        fixture.settle()
        snapshot = fixture.views[-1].snapshot
        self.assertIn("stop_owner", snapshot.actions)
        self.assertEqual(snapshot.owner.state, "unknown")
        self.assertIsNone(snapshot.owner.token_available)
        self.assertEqual(present_remote_update(snapshot).next_action, "stop_owner")
        # And the click reaches the flow's own stop, not some other operation.
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertIn("owner.stop", fixture.flow.names())

    def test_every_other_healthy_stage_costs_exactly_one_click(self) -> None:
        flow = Flow()
        # Stand in for the root-owned owner observation: the stop stage runs
        # once, as it would once the owner is observed live with a token.
        flow.run(2)
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        clicked: list[str] = []
        for _ in range(4):
            action = fixture.next_action()
            clicked.append(str(action))
            self.assertTrue(fixture.click_next())
            fixture.settle()
        self.assertEqual(clicked, ["continue_flow"] * 4)
        # One E action and one host kind drove four different D operations.
        # The controller routes whatever D currently offers and encodes no run
        # order of its own, so an explicitly selected D mode that reorders its
        # stages needs no controller change.
        self.assertEqual(
            set(CONTROLLER.FLOW_CALLS.values()),
            {"advance", "retry", "reconcile", "rollback", "restore", "cancel"},
        )
        self.assertEqual(fixture.views[-1].snapshot.stage, "activate")
        self.assertEqual(fixture.next_action(), "restart")
        self.assertEqual(
            flow.names(),
            [
                "preview.describe",
                "owner.stop",
                "backup.create_verified",
                "prepare.prepare",
                "probe.probe",
                "activation.activate",
            ],
        )


class ControllerRecoveryTests(unittest.TestCase):
    def test_a_failed_stage_offers_one_retry_that_reruns_that_stage(self) -> None:
        flow = Flow(probe=(ProbeOutcome(status="failed"), PROBE_OK))
        flow.run(5)
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertEqual(fixture.next_action(), "retry")
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(flow.names().count("probe.probe"), 2)

    def test_restore_runs_the_supported_restore_over_the_verified_backup(self) -> None:
        flow = Flow(
            probe=(ProbeOutcome(status="failed"),),
            restore=(RestoreOutcome(status="verified", pre_migration_data=True),),
            rollback=(RollbackOutcome(status="verified", previous_activation_selected=True),),
        )
        flow.run(5)
        fixture = Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        capability = fixture.capability()
        self.assertIn("restore_backup", fixture.views[-1].snapshot.actions)
        # Retry is the offered next action here, so restore is not clickable
        # from this screen: one click, one safe action.
        self.assertEqual(fixture.next_action(), "retry")
        self.assertTrue(
            fixture.controller.handle_web_message(click("restore_backup", capability))
        )
        self.assertNotIn("backup.restore", flow.names())


class HostBridgeTests(unittest.TestCase):
    """The parts of the host mixin that need no WinForms or WebView2."""

    class Stub(BRIDGE.RemoteUpdateHostBridgeMixin):
        def __init__(self, state: str = "idle", theme: str = "dark") -> None:
            self.update_status = {"state": state}
            self.current_theme = theme
            self.remote_update_webview = None
            self.traces: list[str] = []

        def _trace(self, note: str) -> None:
            self.traces.append(note)

    def test_pc_actions_follow_the_hosts_own_update_state(self) -> None:
        for state, expected in (
            ("idle", ("check_this_pc",)),
            ("available", ("download_this_pc",)),
            ("ready", ("update_this_pc",)),
            ("error", ("check_this_pc",)),
            # Working states offer nothing, and a current PC offers nothing,
            # so a PC button never stands in front of the remote next action.
            ("checking", ()),
            ("downloading", ()),
            ("installing", ()),
            ("current", ()),
            ("blocked", ()),
        ):
            with self.subTest(state=state):
                self.assertEqual(self.Stub(state)._remote_update_pc_actions(), expected)

    def test_every_offered_pc_action_has_a_controller_callback(self) -> None:
        for action in BRIDGE.PC_ACTION_FOR_UPDATE_STATE.values():
            self.assertIn(action, CONTROLLER.PC_CALLBACKS)

    def test_theme_is_one_of_the_two_the_page_renders(self) -> None:
        self.assertEqual(self.Stub(theme="light")._remote_update_theme(), "light")
        self.assertEqual(self.Stub(theme="dark")._remote_update_theme(), "dark")
        self.assertEqual(self.Stub(theme="nonsense")._remote_update_theme(), "dark")

    def test_rendering_without_a_native_view_raises_instead_of_pretending(self) -> None:
        stub = self.Stub()
        self.assertIsNone(stub._remote_update_core())
        with self.assertRaises(RuntimeError):
            stub._render_remote_update_page("<html></html>")
        # Blanking a view that is not there is a no-op, not an error.
        stub._retire_remote_update_view()

    def test_the_host_owned_seams_refuse_rather_than_simulate(self) -> None:
        stub = self.Stub()
        self.assertIsNone(stub._remote_update_selection())
        self.assertIsNone(stub._remote_update_startup_evidence())
        self.assertFalse(stub._remote_update_restart())
        self.assertFalse(stub._open_remote_update_page())
        self.assertFalse(stub._resume_remote_update_after_restart())
        self.assertFalse(stub._handle_remote_update_message("{}"))
        self.assertFalse(stub._remote_update_admits_navigation("about:blank"))

    def test_the_mixin_supplies_every_hook_the_controller_requires(self) -> None:
        hooks = self.Stub()._remote_update_hooks()
        for name in CONTROLLER.REQUIRED_HOOKS:
            with self.subTest(hook=name):
                self.assertTrue(callable(getattr(hooks, name, None)))
        # A controller over exactly these hooks constructs, so the mixin is a
        # complete seam and not a partial one that fails at first click.
        controller = CONTROLLER.RemoteUpdateController(hooks)
        self.addCleanup(controller.close)
        self.assertFalse(controller.open())


if __name__ == "__main__":
    unittest.main()
