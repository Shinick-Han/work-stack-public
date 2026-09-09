"""D snapshot to E presentation and action resolution.

These tests drive the actual ``RemoteUpdateFlow`` with disposable fake
effect ports and admit clicks through the actual ``RemoteUpdateViewSession``.
They do not substitute a fabricated R6 dictionary for that boundary.
"""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_update_flow_contract as FLOW
import remote_update_presentation as PRESENT
import remote_update_presentation_actions as ACTIONS
import remote_update_projection as PROJECT
import remote_update_view as VIEW
from remote_update_flow import RemoteUpdateFlow
from remote_update_flow_contract import (
    ActivationOutcome,
    BackupOutcome,
    LostResponse,
    OwnerStopFacts,
    PendingOperation,
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    RemoteUpdatePorts,
    RemoteUpdateSnapshot as FlowSnapshot,
    RestoreOutcome,
    RollbackOutcome,
    StartupEvidence,
    VerifyOutcome,
)


SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_SESSION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_WORKSPACE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
CAPABILITY = "0123456789abcdef" * 4
REQUEST_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

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
RESTORE_OK = RestoreOutcome(status="verified", pre_migration_data=True)
ROLLBACK_OK = RollbackOutcome(status="verified", previous_activation_selected=True)
VERIFY_OK = VerifyOutcome(
    status="verified",
    desktop_version="1.0.14",
    remote_version="1.0.14",
    served_ui_version="1.0.14",
    protocol_version="7",
    schema_version="v6",
)
STILL_LIVE_STOP = OwnerStopFacts(
    state="live",
    token_available=True,
    pidfd_available=True,
    process_exit="failed",
    listener_release="unknown",
    lease_release="unknown",
)
MISSING_TOKEN_STOP = OwnerStopFacts(state="live", token_available=False)
UNFENCED_STOP = OwnerStopFacts(state="unfenced", token_available=True)
TOKEN_UNKNOWN_STOP = OwnerStopFacts(state="live", token_available=None)
FOREIGN_STOP = OwnerStopFacts(
    state="foreign",
    token_available=True,
    pidfd_available=True,
    process_exit="unknown",
    listener_release="unknown",
    lease_release="unknown",
)


class Script:
    def __init__(self, calls: list[tuple[str, str]], name: str, results: object) -> None:
        self._calls = calls
        self._name = name
        self._results = list(results)

    def take(self, operation_id: str) -> object:
        self._calls.append((self._name, operation_id))
        if not self._results:
            raise AssertionError(f"unscripted call to {self._name}")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakePreview:
    def __init__(self, calls: list[tuple[str, str]], results: object) -> None:
        self.describe_script = Script(calls, "preview.describe", results)

    def describe(self) -> object:
        return self.describe_script.take("")


class FakeOwner:
    def __init__(
        self, calls: list[tuple[str, str]], stop: object = (), observe: object = ()
    ) -> None:
        self.stop_script = Script(calls, "owner.stop", stop)
        self.observe_script = Script(calls, "owner.observe", observe)

    def stop(self, operation_id: str) -> object:
        return self.stop_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)


class FakeBackup:
    def __init__(
        self,
        calls: list[tuple[str, str]],
        create: object = (),
        observe: object = (),
        restore: object = (),
        observe_restore: object = (),
    ) -> None:
        self.create_script = Script(calls, "backup.create_verified", create)
        self.observe_script = Script(calls, "backup.observe", observe)
        self.restore_script = Script(calls, "backup.restore", restore)
        self.observe_restore_script = Script(
            calls, "backup.observe_restore", observe_restore
        )

    def create_verified(self, operation_id: str) -> object:
        return self.create_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)

    def restore(self, operation_id: str) -> object:
        return self.restore_script.take(operation_id)

    def observe_restore(self, operation_id: str) -> object:
        return self.observe_restore_script.take(operation_id)


class FakePrepare:
    """Both prepare calls, so the call list says which order ran which.

    ``prepare_code_only`` is the separate port method the prepare-first order
    requires; a flow built with ``prepare_before_stop=True`` is refused at
    construction without it, and the default order must never reach it.
    """

    def __init__(
        self,
        calls: list[tuple[str, str]],
        prepare: object = (),
        observe: object = (),
        code_only: object = (),
    ) -> None:
        self.prepare_script = Script(calls, "prepare.prepare", prepare)
        self.observe_script = Script(calls, "prepare.observe", observe)
        self.code_only_script = Script(calls, "prepare.prepare_code_only", code_only)

    def prepare(self, operation_id: str) -> object:
        return self.prepare_script.take(operation_id)

    def prepare_code_only(self, operation_id: str) -> object:
        return self.code_only_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)


class FakeProbe:
    def __init__(self, calls: list[tuple[str, str]], probe: object = ()) -> None:
        self.probe_script = Script(calls, "probe.probe", probe)

    def probe(self, operation_id: str) -> object:
        return self.probe_script.take(operation_id)


class FakeActivation:
    def __init__(
        self,
        calls: list[tuple[str, str]],
        activate: object = (),
        observe: object = (),
        confirm: object = (),
        observe_confirm: object = (),
        rollback: object = (),
        observe_rollback: object = (),
    ) -> None:
        self.activate_script = Script(calls, "activation.activate", activate)
        self.observe_script = Script(calls, "activation.observe", observe)
        self.confirm_script = Script(calls, "activation.confirm", confirm)
        self.observe_confirm_script = Script(
            calls, "activation.observe_confirm", observe_confirm
        )
        self.rollback_script = Script(calls, "activation.rollback", rollback)
        self.observe_rollback_script = Script(
            calls, "activation.observe_rollback", observe_rollback
        )

    def activate(self, operation_id: str) -> object:
        return self.activate_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)

    def confirm(self, operation_id: str) -> object:
        return self.confirm_script.take(operation_id)

    def observe_confirm(self, operation_id: str) -> object:
        return self.observe_confirm_script.take(operation_id)

    def rollback(self, operation_id: str) -> object:
        return self.rollback_script.take(operation_id)

    def observe_rollback(self, operation_id: str) -> object:
        return self.observe_rollback_script.take(operation_id)


class FakeVerification:
    def __init__(self, calls: list[tuple[str, str]], verify: object = ()) -> None:
        self.verify_script = Script(calls, "verification.verify", verify)

    def verify(self, operation_id: str) -> object:
        return self.verify_script.take(operation_id)


class FakeJournal:
    """The durable record, holding the identities the real port would hold."""

    def __init__(self, entries: tuple[object, ...] = ()) -> None:
        self.entries = tuple(entries)

    def load(self) -> tuple[object, ...]:
        return self.entries

    def record(self, entries: object) -> None:
        self.entries = tuple(entries) if isinstance(entries, (tuple, list)) else (entries,)


class Harness:
    """Actual D flow plus disposable fake ports."""

    def __init__(
        self,
        journal: object = None,
        *,
        prepare_before_stop: bool = False,
        **scripts: object,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.issued: list[str] = []
        self.journal = journal
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=FakePreview(self.calls, scripts.get("preview", (PREVIEW_OK,))),
                owner=FakeOwner(
                    self.calls,
                    stop=scripts.get("stop", (STOP_OK,)),
                    observe=scripts.get("stop_observe", ()),
                ),
                backup=FakeBackup(
                    self.calls,
                    create=scripts.get("backup", (BACKUP_OK,)),
                    observe=scripts.get("backup_observe", ()),
                    restore=scripts.get("restore", ()),
                    observe_restore=scripts.get("restore_observe", ()),
                ),
                prepare=FakePrepare(
                    self.calls,
                    prepare=scripts.get("prepare", (PREPARE_OK,)),
                    observe=scripts.get("prepare_observe", ()),
                    code_only=scripts.get("code_only", (PREPARE_OK,)),
                ),
                probe=FakeProbe(self.calls, probe=scripts.get("probe", (PROBE_OK,))),
                activation=FakeActivation(
                    self.calls,
                    activate=scripts.get("activate", (ACTIVATE_OK,)),
                    observe=scripts.get("activate_observe", ()),
                    confirm=scripts.get("confirm", (CONFIRM_OK,)),
                    observe_confirm=scripts.get("confirm_observe", ()),
                    rollback=scripts.get("rollback", ()),
                    observe_rollback=scripts.get("rollback_observe", ()),
                ),
                verification=FakeVerification(
                    self.calls, verify=scripts.get("verify", (VERIFY_OK,))
                ),
            ),
            operation_ids=self._operation_id,
            journal=journal,
            prepare_before_stop=prepare_before_stop,
        )

    def _operation_id(self, stage: str) -> str:
        issue = f"op-{stage}-{1 + sum(1 for name in self.issued if name == stage)}"
        self.issued.append(stage)
        return issue

    def run(self, steps: int) -> None:
        for _ in range(steps):
            self.flow.advance()

    def anchor(self) -> object:
        """The activation identity D itself retains, pending or confirmed."""

        return self.flow.retained_activation()

    def anchor_id(self) -> str:
        anchor = self.anchor()
        assert anchor is not None, "the flow retains no activation identity"
        return anchor.operation_id

    def resume(self) -> object:
        """Restart against the exact receipt D is holding, never a guessed id."""

        return self.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=self.anchor_id(),
                authority_selected="verified",
                workspace_match="verified",
            )
        )


def project(source: object, pc_actions: tuple[str, ...] = ()) -> PROJECT.RemoteUpdateProjection:
    return PROJECT.project_remote_update(
        source,
        session_id=SESSION,
        workspace_id=WORKSPACE,
        pc_actions=pc_actions,
    )


def click(session: VIEW.RemoteUpdateViewSession, operation: str) -> VIEW.RemoteUpdateAdmission:
    payload = {
        "type": VIEW.REMOTE_UPDATE_REQUEST_TYPE,
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "capability": CAPABILITY,
        "operation": operation,
    }
    return session.admit(json.dumps(payload))


def show(page: PROJECT.RemoteUpdateProjection) -> VIEW.RemoteUpdateViewSession:
    session = VIEW.RemoteUpdateViewSession()
    session.show(page.snapshot, lambda _html: None, capability=CAPABILITY)
    return session


def resolve(
    admission: VIEW.RemoteUpdateAdmission,
    page: PROJECT.RemoteUpdateProjection,
    current: object,
    *,
    session_id: str = SESSION,
    workspace_id: str = WORKSPACE,
) -> PROJECT.ResolvedRemoteUpdateOperation:
    return PROJECT.resolve_remote_update_action(
        admission,
        page,
        current=current,
        session_id=session_id,
        workspace_id=workspace_id,
    )


class MappingTests(unittest.TestCase):
    def test_preview_maps_run_preview_not_the_raw_d_code(self) -> None:
        harness = Harness()
        idle = project(harness.flow, pc_actions=("check_this_pc",))
        self.assertEqual("run_preview", harness.flow.snapshot().actions[0])
        self.assertEqual("check_this_pc", idle.snapshot.actions[0])
        self.assertIn("preview_server_update", idle.snapshot.actions)
        self.assertNotIn("run_preview", idle.snapshot.actions)
        harness.run(1)
        page = project(harness.flow)
        self.assertEqual("preview", page.snapshot.stage)
        self.assertNotEqual("preview_ready", page.snapshot.code)
        self.assertEqual("PREVIEW_READY", page.diagnostics["code"])
        self.assertNotEqual(page.snapshot.code, page.diagnostics["code"])

    def test_every_d_code_and_action_has_an_explicit_map(self) -> None:
        from remote_update_presentation import CODES as E_CODES

        self.assertEqual(set(PROJECT.D_TO_E_CODE), set(FLOW.CODES))
        self.assertEqual(set(PROJECT.D_TO_F_CODE), set(FLOW.CODES))
        self.assertEqual(set(PROJECT.D_TO_E_ACTION), set(FLOW.ACTIONS))
        self.assertTrue(set(PROJECT.D_TO_E_CODE.values()) <= E_CODES)

    def test_unknown_d_code_stays_unknown_and_drops_raw_pass_through(self) -> None:
        snapshot = FlowSnapshot(
            stage="failed",
            code="not_a_flow_code",
            versions=dict.fromkeys(FLOW.VERSION_FIELDS),
            owner={name: "unknown" for name in FLOW.OWNER_FIELDS},
            install={"capability": "unknown", "method": "unknown"},
            backup={"status": "unknown", "migration_required": None},
            actions=("rollback_activation", "run_verify"),
        )
        page = project(snapshot)
        self.assertEqual("unknown", page.snapshot.code)
        self.assertNotIn("rollback_activation", page.snapshot.actions)
        self.assertNotIn("run_verify", page.snapshot.actions)
        self.assertNotIn("verify_connection", page.snapshot.actions)
        self.assertIsNone(page.diagnostics["code"])

    def test_path_token_and_argv_never_cross_from_to_document(self) -> None:
        harness = Harness()
        harness.run(1)
        document = harness.flow.snapshot().to_document()
        document["path"] = "C:/secret/ssot"
        document["token"] = "session-token-value"
        document["argv"] = ["python", "-c", "print(1)"]
        page = project(document)
        html = VIEW.build_remote_update_html(page.snapshot, capability=CAPABILITY)
        import remote_update_diagnostics as DIAGNOSTICS

        report = DIAGNOSTICS.format_report(page.diagnostics)
        for leaked in ("C:/secret/ssot", "session-token-value", "python", "print(1)"):
            self.assertNotIn(leaked, html)
            self.assertNotIn(leaked, report)
            self.assertNotIn(leaked, json.dumps(page.diagnostics))

    def test_resolve_activation_pairing_maps_to_review_not_rollback(self) -> None:
        snapshot = FlowSnapshot(
            stage="failed",
            code="restore_verified_activation_selected",
            versions=dict.fromkeys(FLOW.VERSION_FIELDS),
            owner={"state": "dead", "token_available": False,
                   "pidfd_available": None, "process_exit": "verified",
                   "listener_release": "verified", "lease_release": "verified"},
            install={"capability": "available", "method": "verified_unpack"},
            backup={"status": "verified", "migration_required": False},
            actions=("resolve_activation_pairing", "inspect_diagnostics"),
        )
        page = project(snapshot)
        self.assertEqual(("review",), page.snapshot.actions)
        self.assertNotIn("rollback_activation", page.snapshot.actions)


class FlowBoundaryTests(unittest.TestCase):
    def test_lost_response_reconcile_observes_the_same_operation(self) -> None:
        harness = Harness(
            prepare=(LostResponse("op-prepare-1"),),
            prepare_observe=(PREPARE_OK,),
        )
        harness.run(4)
        pending = harness.flow.pending_operation()
        self.assertIsNotNone(pending)
        self.assertEqual("op-prepare-1", pending.operation_id)
        self.assertEqual(("reconcile_pending", "inspect_diagnostics"), harness.flow.snapshot().actions)
        page = project(harness.flow)
        session = show(page)
        admitted = click(session, "reconcile_pending")
        self.assertEqual("action", admitted.outcome)
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("reconcile", resolved.host_kind)
        self.assertEqual("reconcile_pending", resolved.d_operation)
        self.assertEqual("op-prepare-1", resolved.operation_id)
        self.assertFalse(resolved.mutation)
        self.assertTrue(resolved.read_only)
        self.assertEqual(1, sum(1 for name, _ in harness.calls if name == "prepare.prepare"))
        self.assertFalse(any(name == "prepare.observe" for name, _ in harness.calls))

    def test_restart_pending_maps_desktop_restart_not_advance(self) -> None:
        harness = Harness()
        harness.run(6)
        snap = harness.flow.snapshot()
        self.assertEqual("activate_pending_restart", snap.code)
        self.assertEqual("restart_desktop", snap.actions[0])
        anchor = harness.anchor()
        self.assertEqual(FLOW.KIND_ACTIVATION_PENDING, anchor.kind)
        page = project(harness.flow)
        self.assertEqual("restart", page.snapshot.actions[0])
        self.assertNotIn("continue_flow", page.snapshot.actions)
        session = show(page)
        admitted = click(session, "restart")
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("restart", resolved.host_kind)
        self.assertEqual("restart_desktop", resolved.d_operation)
        # The binding carries D's own retained receipt, not a reconstructed id.
        self.assertEqual(anchor.operation_id, resolved.operation_id)
        self.assertFalse(resolved.mutation)

    def test_empty_d_actions_do_not_synthesize_a_mutation(self) -> None:
        snapshot = FlowSnapshot(
            stage="failed",
            code="restore_failed",
            versions=dict.fromkeys(FLOW.VERSION_FIELDS),
            owner={"state": "dead", "token_available": False,
                   "pidfd_available": None, "process_exit": "verified",
                   "listener_release": "verified", "lease_release": "verified"},
            install={"capability": "unknown", "method": "unknown"},
            backup={"status": "verified", "migration_required": True},
            actions=(),
        )
        page = project(snapshot)
        self.assertEqual((), page.snapshot.actions)
        session = show(page)
        for operation in ("restore_backup", "retry", "rollback_activation", "reconcile_pending"):
            admitted = click(session, operation)
            self.assertEqual("ignored", admitted.outcome)
            resolved = resolve(admitted, page, snapshot)
            self.assertEqual("ignored", resolved.outcome)

    def test_stale_page_stage_and_operation_clicks_are_rejected(self) -> None:
        harness = Harness()
        page = project(harness.flow)
        session = show(page)
        harness.run(1)
        admitted = click(session, "preview_server_update")
        self.assertEqual("action", admitted.outcome)
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("ignored", resolved.outcome)
        self.assertEqual("stale_stage", resolved.reason)
        lost = Harness(prepare=(LostResponse("op-prepare-1"),))
        lost.run(4)
        pending_page = project(lost.flow)
        pending_session = show(pending_page)
        admitted_pending = click(pending_session, "reconcile_pending")
        current_document = lost.flow.snapshot().to_document()
        stale_op = resolve(admitted_pending, pending_page, current_document)
        self.assertEqual("ignored", stale_op.outcome)
        self.assertEqual("stale_operation", stale_op.reason)

    def test_stale_workspace_and_session_clicks_are_rejected(self) -> None:
        harness = Harness()
        harness.run(1)
        page = project(harness.flow)
        session = show(page)
        admitted = click(session, "preview_server_update")
        self.assertEqual(
            "stale_binding",
            resolve(admitted, page, harness.flow, workspace_id=OTHER_WORKSPACE).reason,
        )
        self.assertEqual(
            "stale_binding",
            resolve(admitted, page, harness.flow, session_id=OTHER_SESSION).reason,
        )

    def test_foreign_owner_maps_to_review_and_keeps_this_pc_actions(self) -> None:
        harness = Harness(stop=(FOREIGN_STOP,))
        harness.run(2)
        page = project(harness.flow, pc_actions=("update_this_pc",))
        self.assertEqual("owner_foreign", page.snapshot.code)
        self.assertEqual("update_this_pc", page.snapshot.actions[0])
        self.assertIn("review", page.snapshot.actions)
        self.assertNotIn("stop_owner", page.snapshot.actions)
        self.assertNotIn("update_connected_server", page.snapshot.actions)
        session = show(page)
        self.assertEqual("action", click(session, "update_this_pc").outcome)
        session = show(page)
        self.assertEqual("ignored", click(session, "stop_owner").outcome)
        review_page = project(harness.flow)
        self.assertEqual("review", review_page.snapshot.actions[0])

    def test_read_only_verify_admits_a_live_new_owner(self) -> None:
        harness = Harness()
        harness.run(6)
        harness.resume()
        self.assertEqual("run_verify", harness.flow.snapshot().actions[0])
        page = project(harness.flow)
        self.assertEqual("verify_connection", page.snapshot.actions[0])
        live = replace(
            page.snapshot.owner,
            state="live",
            token_available=True,
            process_exit="unknown",
            listener_release="unknown",
            lease_release="unknown",
        )
        live_page = PROJECT.RemoteUpdateProjection(
            snapshot=replace(page.snapshot, owner=live),
            binding=page.binding,
            diagnostics=page.diagnostics,
            d_document=page.d_document,
        )
        session = show(live_page)
        admitted = click(session, "verify_connection")
        self.assertEqual("action", admitted.outcome)
        resolved = resolve(admitted, live_page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("verify", resolved.host_kind)
        self.assertEqual("run_verify", resolved.d_operation)
        self.assertTrue(resolved.read_only)
        self.assertFalse(resolved.mutation)

    def test_verified_restore_then_rollback_uses_the_offered_pairing(self) -> None:
        harness = Harness(
            verify=(VerifyOutcome(status="failed"),),
            restore=(RESTORE_OK,),
            rollback=(ROLLBACK_OK,),
        )
        harness.run(6)
        harness.resume()
        harness.flow.advance()
        confirmed = harness.anchor()
        self.assertEqual(FLOW.KIND_ACTIVATION_CONFIRMED, confirmed.kind)
        restored = harness.flow.restore()
        self.assertEqual("restore_verified_activation_selected", restored.code)
        self.assertEqual("rollback_activation", restored.actions[0])
        page = project(harness.flow)
        self.assertEqual("failed", page.snapshot.code)
        self.assertEqual("rollback_activation", page.snapshot.actions[0])
        session = show(page)
        admitted = click(session, "rollback_activation")
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("rollback", resolved.host_kind)
        self.assertEqual("rollback_activation", resolved.d_operation)
        # The rollback targets the confirmed receipt D still retains.
        self.assertEqual(confirmed.operation_id, resolved.operation_id)
        self.assertEqual([], [name for name, _ in harness.calls if name.endswith("rollback")])

    def test_close_retires_the_screen_and_does_not_finish_or_cancel(self) -> None:
        harness = Harness()
        harness.run(6)
        harness.resume()
        harness.flow.advance()
        self.assertEqual("finish", harness.flow.snapshot().actions[0])
        page = project(harness.flow)
        self.assertEqual(("close",), page.snapshot.actions)
        session = show(page)
        admitted = click(session, "close")
        self.assertEqual("close", admitted.outcome)
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("retire", resolved.outcome)
        self.assertEqual("retire", resolved.host_kind)
        self.assertIsNone(resolved.d_operation)
        self.assertEqual("close_retires_screen", resolved.reason)
        self.assertFalse(resolved.mutation)

    def test_to_document_and_to_dict_shapes_are_accepted(self) -> None:
        harness = Harness()
        harness.run(1)
        from_object = project(harness.flow.snapshot())
        from_document = project(harness.flow.snapshot().to_document())
        self.assertEqual(from_object.snapshot.actions, from_document.snapshot.actions)
        self.assertEqual(from_object.binding.d_code, from_document.binding.d_code)

    def test_reconcile_is_not_retry_after_a_lost_restore(self) -> None:
        harness = Harness(
            verify=(VerifyOutcome(status="failed"),),
            restore=(LostResponse("op-backup_restore-1"),),
        )
        harness.run(6)
        harness.resume()
        harness.flow.advance()
        harness.flow.restore()
        page = project(harness.flow)
        self.assertEqual("reconcile_pending", page.snapshot.actions[0])
        self.assertNotIn("retry", page.snapshot.actions)
        session = show(page)
        admitted = click(session, "reconcile_pending")
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("reconcile", resolved.host_kind)
        self.assertEqual("op-backup_restore-1", resolved.operation_id)
        self.assertNotEqual("retry", resolved.host_kind)

    def test_foreign_capability_stays_unbound_at_the_view(self) -> None:
        harness = Harness()
        harness.run(1)
        page = project(harness.flow)
        session = show(page)
        payload = json.dumps({
            "type": VIEW.REMOTE_UPDATE_REQUEST_TYPE,
            "schema_version": 1,
            "request_id": REQUEST_ID,
            "capability": "ab" * 32,
            "operation": "preview_server_update",
        })
        admitted = session.admit(payload)
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("unbound", admitted.outcome)
        self.assertEqual("unbound", resolved.outcome)


class RestartAnchorTests(unittest.TestCase):
    """The projection binds to the activation identity D holds after a restart.

    D's settled correction keeps a confirmed activation as a passive retained
    anchor, so a desktop that crashes past confirmation rebuilds the same
    receipt.  E must carry that exact identity into the page binding and back
    out of a click.  It must never reconstruct one, and never publish an idle
    page because a fresh in-process object defaulted to empty.
    """

    @staticmethod
    def crash(harness: Harness) -> Harness:
        """Reload a desktop from the durable record this flow wrote."""

        return Harness(journal=FakeJournal(harness.journal.entries))

    def test_a_confirmed_anchor_survives_the_restart_into_the_page_binding(self) -> None:
        harness = Harness(journal=FakeJournal())
        harness.run(6)
        harness.resume()
        confirmed = harness.anchor()
        self.assertEqual(FLOW.KIND_ACTIVATION_CONFIRMED, confirmed.kind)

        restarted = self.crash(harness)
        reloaded = restarted.anchor()
        self.assertEqual(FLOW.KIND_ACTIVATION_CONFIRMED, reloaded.kind)
        self.assertEqual(confirmed.operation_id, reloaded.operation_id)

        snap = restarted.flow.snapshot()
        self.assertEqual("activate", snap.stage)
        self.assertEqual("activate_committed", snap.code)
        self.assertEqual("run_verify", snap.actions[0])

        page = project(restarted.flow)
        self.assertEqual("verify_connection", page.snapshot.actions[0])
        self.assertNotIn("restart", page.snapshot.actions)
        self.assertNotIn("rollback_activation", page.snapshot.actions)
        self.assertEqual(confirmed.operation_id, page.binding.operation_id)

        session = show(page)
        admitted = click(session, "verify_connection")
        resolved = resolve(admitted, page, restarted.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("verify", resolved.host_kind)
        self.assertEqual("run_verify", resolved.d_operation)
        self.assertEqual(confirmed.operation_id, resolved.operation_id)
        self.assertTrue(resolved.read_only)
        self.assertFalse(resolved.mutation)
        self.assertEqual([], restarted.calls)

    def test_a_page_bound_before_the_crash_is_stale_against_the_reloaded_flow(self) -> None:
        harness = Harness(journal=FakeJournal())
        harness.run(6)
        pending_page = project(harness.flow)
        self.assertEqual("restart", pending_page.snapshot.actions[0])
        session = show(pending_page)
        admitted = click(session, "restart")

        harness.resume()
        restarted = self.crash(harness)

        # Same operation id, but the confirmed anchor is a later condition:
        # the pre-restart page cannot replay its restart against it.
        self.assertEqual(
            pending_page.binding.operation_id, restarted.anchor().operation_id
        )
        resolved = resolve(admitted, pending_page, restarted.flow)
        self.assertEqual("ignored", resolved.outcome)
        self.assertEqual("stale_code", resolved.reason)
        self.assertEqual([], restarted.calls)

    def test_an_anchor_without_retention_evidence_reviews_and_never_rolls_back(self) -> None:
        # A record written before anything measured the previous application:
        # the reload owes an open question, never a rollback offer.
        restarted = Harness(
            journal=FakeJournal((
                PendingOperation(
                    "activate", "op-activate-1", FLOW.KIND_ACTIVATION_CONFIRMED
                ),
                PendingOperation(
                    "backup", "op-backup_restore-1", FLOW.KIND_BACKUP_RESTORE
                ),
            )),
            restore_observe=(RESTORE_OK,),
        )
        restarted.flow.reconcile()

        snap = restarted.flow.snapshot()
        self.assertIn("resolve_activation_pairing", snap.actions)
        self.assertNotIn("rollback_activation", snap.actions)
        self.assertNotIn("dismiss", snap.actions)
        self.assertNotIn("finish", snap.actions)

        page = project(restarted.flow)
        self.assertIn("review", page.snapshot.actions)
        self.assertNotIn("rollback_activation", page.snapshot.actions)
        session = show(page)
        self.assertEqual("ignored", click(session, "rollback_activation").outcome)
        resolved = resolve(click(session, "review"), page, restarted.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("review", resolved.host_kind)
        self.assertEqual("resolve_activation_pairing", resolved.d_operation)
        self.assertTrue(resolved.read_only)
        self.assertFalse(resolved.mutation)
        self.assertEqual(
            [], [name for name, _ in restarted.calls if name.startswith("activation")]
        )


class BindingTests(unittest.TestCase):
    def test_path_like_binding_ids_are_refused(self) -> None:
        harness = Harness()
        with self.assertRaises(ValueError):
            PROJECT.project_remote_update(
                harness.flow,
                session_id="C:/secret",
                workspace_id=WORKSPACE,
            )
        with self.assertRaises(ValueError):
            PROJECT.project_remote_update(
                harness.flow,
                session_id=SESSION,
                workspace_id="not-a-uuid",
            )
        UUID(SESSION)
        UUID(WORKSPACE)


class OfferedStopTests(unittest.TestCase):
    """The first mutating step of the actual flow has to be reachable.

    D offers ``run_stop`` out of a fresh preview, before any owner evidence
    exists, because the authenticated stop port is what produces that
    evidence.  These drive the actual flow, the actual page and the actual
    admission; none of them hands E an owner fact the preview never measured.
    """

    def test_fresh_preview_stop_click_reaches_the_real_owner_port(self) -> None:
        harness = Harness()
        harness.run(1)
        page = project(harness.flow)
        self.assertEqual("preview", page.snapshot.stage)
        self.assertEqual(
            ("run_stop", "inspect_diagnostics", "cancel"), page.binding.d_actions
        )
        # Nothing pretends the owner was measured: the page still says unknown.
        self.assertEqual("unknown", page.snapshot.owner.state)
        self.assertIsNone(page.snapshot.owner.token_available)
        self.assertEqual("stop_owner", page.snapshot.actions[0])
        self.assertEqual(
            "stop_owner", PRESENT.present_remote_update(page.snapshot).next_action
        )

        session = show(page)
        admitted = click(session, "stop_owner")
        self.assertEqual("action", admitted.outcome)
        resolved = resolve(admitted, page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("advance", resolved.host_kind)
        self.assertEqual("run_stop", resolved.d_operation)
        self.assertTrue(resolved.mutation)
        self.assertFalse(resolved.read_only)
        self.assertEqual([("preview.describe", "")], harness.calls)

        # The host performs exactly the resolved advance; D calls the port.
        harness.flow.advance()
        self.assertEqual(("owner.stop", "op-stop-1"), harness.calls[-1])
        after = harness.flow.snapshot()
        self.assertEqual("stop", after.stage)
        self.assertEqual("stop_verified", after.code)

    def test_full_offered_journey_runs_end_to_end_through_the_page(self) -> None:
        harness = Harness()
        clicked: list[str] = []
        operations: list[str | None] = []
        for _ in range(12):
            page = project(harness.flow)
            action = PRESENT.present_remote_update(page.snapshot).next_action or "close"
            admitted = click(show(page), action)
            resolved = resolve(admitted, page, harness.flow)
            clicked.append(action)
            operations.append(resolved.d_operation)
            if resolved.outcome == "retire":
                break
            self.assertEqual("dispatch", resolved.outcome)
            if resolved.host_kind in ("advance", "verify"):
                harness.flow.advance()
            elif resolved.host_kind == "restart":
                harness.resume()
            else:
                self.fail(f"journey stalled on {action} as {resolved.host_kind}")
        self.assertEqual(
            [
                "preview_server_update", "stop_owner", "continue_flow",
                "continue_flow", "continue_flow", "continue_flow", "restart",
                "verify_connection", "close",
            ],
            clicked,
        )
        self.assertEqual(
            [
                "run_preview", "run_stop", "run_backup", "run_prepare",
                "run_probe", "run_activate", "restart_desktop", "run_verify", None,
            ],
            operations,
        )
        self.assertEqual(
            [
                ("preview.describe", ""),
                ("owner.stop", "op-stop-1"),
                ("backup.create_verified", "op-backup-1"),
                ("prepare.prepare", "op-prepare-1"),
                ("probe.probe", "op-probe-1"),
                ("activation.activate", "op-activate-1"),
                ("activation.confirm", "op-activate-1"),
                ("verification.verify", "op-verify-1"),
            ],
            harness.calls,
        )
        self.assertEqual("update_ready", harness.flow.snapshot().code)

    def test_measured_stop_refusal_and_unknown_never_reach_backup(self) -> None:
        cases = (
            ("missing_token", MISSING_TOKEN_STOP, "failed",
             "stop_refused_missing_token", "token_missing", "review"),
            ("unfenced", UNFENCED_STOP, "failed",
             "stop_refused_unfenced_receipt", "owner_unfenced", "review"),
            ("foreign", FOREIGN_STOP, "failed",
             "stop_refused_foreign_host", "owner_foreign", "review"),
            ("token_unknown", TOKEN_UNKNOWN_STOP, "unknown",
             "stop_token_unknown", "unknown", "reconcile_pending"),
        )
        for name, facts, stage, d_code, e_code, next_action in cases:
            with self.subTest(stop=name):
                harness = Harness(stop=(facts,))
                harness.run(2)
                settled = harness.flow.snapshot()
                self.assertEqual((stage, d_code), (settled.stage, settled.code))
                page = project(harness.flow)
                self.assertEqual(e_code, page.snapshot.code)
                self.assertNotIn("stop_owner", page.snapshot.actions)
                self.assertNotIn("continue_flow", page.snapshot.actions)
                self.assertEqual(
                    next_action,
                    PRESENT.present_remote_update(page.snapshot).next_action,
                )
                self.assertEqual("ignored", click(show(page), "stop_owner").outcome)
                self.assertEqual("ignored", click(show(page), "continue_flow").outcome)
                # One authenticated stop was attempted and no backup followed.
                self.assertEqual(
                    [("preview.describe", ""), ("owner.stop", "op-stop-1")],
                    harness.calls,
                )

    def test_stop_is_admitted_only_from_the_offer_the_flow_makes_now(self) -> None:
        harness = Harness()
        idle = project(harness.flow)
        # Arbitrary: D offers the preview, not the stop, so this is not the click.
        self.assertNotIn("stop_owner", idle.snapshot.actions)
        self.assertEqual("ignored", click(show(idle), "stop_owner").outcome)
        self.assertFalse(ACTIONS.action_is_admissible(idle.snapshot, "stop_owner"))

        harness.run(1)
        page = project(harness.flow)
        admitted = click(show(page), "stop_owner")
        self.assertEqual("action", admitted.outcome)
        # Stale: the same click resolved against a flow that already stopped.
        harness.flow.advance()
        stale = resolve(admitted, page, harness.flow)
        self.assertEqual("ignored", stale.outcome)
        self.assertEqual("stale_stage", stale.reason)

        self.assertEqual(
            [("preview.describe", ""), ("owner.stop", "op-stop-1")], harness.calls
        )

        # Absent: an offer stripped from the page is never re-synthesized.
        fresh = Harness()
        fresh.run(1)
        offered = project(fresh.flow)
        stripped = PROJECT.RemoteUpdateProjection(
            snapshot=replace(offered.snapshot, actions=()),
            binding=offered.binding,
            diagnostics=offered.diagnostics,
            d_document=offered.d_document,
        )
        self.assertFalse(ACTIONS.action_is_admissible(stripped.snapshot, "stop_owner"))
        self.assertEqual("ignored", click(show(stripped), "stop_owner").outcome)
        # Even a binding that still lists the action does not resurrect it:
        # the predicate reads the offer on the page that was actually shown.
        self.assertIn("stop_owner", stripped.binding.e_actions)
        self.assertEqual(
            "not_admitted",
            resolve(click(show(offered), "stop_owner"), stripped, fresh.flow).reason,
        )
        self.assertEqual([("preview.describe", "")], fresh.calls)


class PrepareFirstJourneyTests(unittest.TestCase):
    """The explicitly selected code-first order, clicked end to end.

    The flow is the real ``RemoteUpdateFlow(prepare_before_stop=True)`` with
    disposable fake effect ports; the projection, the presentation, the view
    session's admission and the resolution are the real E side.  Nothing here
    substitutes a document for the flow: every page comes from the flow's own
    published snapshot, and every step is the button that page offers.
    """

    def test_the_code_first_order_runs_end_to_end_through_the_page(self) -> None:
        harness = Harness(prepare_before_stop=True)
        clicked: list[str] = []
        operations: list[str | None] = []
        for _ in range(12):
            page = project(harness.flow)
            action = PRESENT.present_remote_update(page.snapshot).next_action or "close"
            admitted = click(show(page), action)
            resolved = resolve(admitted, page, harness.flow)
            clicked.append(action)
            operations.append(resolved.d_operation)
            if resolved.outcome == "retire":
                break
            self.assertEqual("dispatch", resolved.outcome)
            if resolved.host_kind in ("advance", "verify"):
                harness.flow.advance()
            elif resolved.host_kind == "restart":
                harness.resume()
            else:
                self.fail(f"journey stalled on {action} as {resolved.host_kind}")
        self.assertEqual(
            [
                "preview_server_update", "prepare_app_files", "stop_owner",
                "continue_flow", "continue_flow", "continue_flow", "restart",
                "verify_connection", "close",
            ],
            clicked,
        )
        self.assertEqual(
            [
                "run_preview", "run_prepare_code_only", "run_stop", "run_backup",
                "run_probe", "run_activate", "restart_desktop", "run_verify", None,
            ],
            operations,
        )
        self.assertEqual(
            [
                ("preview.describe", ""),
                ("prepare.prepare_code_only", "op-prepare-1"),
                ("owner.stop", "op-stop-1"),
                ("backup.create_verified", "op-backup-1"),
                ("probe.probe", "op-probe-1"),
                ("activation.activate", "op-activate-1"),
                ("activation.confirm", "op-activate-1"),
                ("verification.verify", "op-verify-1"),
            ],
            harness.calls,
        )
        self.assertEqual("update_ready", harness.flow.snapshot().code)

    def test_staging_is_clickable_while_the_owner_still_serves(self) -> None:
        """The step the default order cannot reach: staging before the stop."""

        harness = Harness(prepare_before_stop=True)
        harness.run(1)
        page = project(harness.flow)
        self.assertEqual(
            ("run_prepare_code_only", "inspect_diagnostics", "cancel"),
            harness.flow.snapshot().actions,
        )
        self.assertEqual(("prepare_app_files", "review", "cancel"), page.snapshot.actions)
        view = PRESENT.present_remote_update(page.snapshot)
        self.assertEqual("prepare_app_files", view.next_action)
        # Nothing is pretended: no stop has run and no backup has been taken.
        self.assertEqual("Unknown", view.owner_state)
        self.assertEqual("unknown", page.snapshot.owner.state)
        self.assertEqual("not_run", page.snapshot.backup.status)

        resolved = resolve(click(show(page), "prepare_app_files"), page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("advance", resolved.host_kind)
        self.assertEqual("run_prepare_code_only", resolved.d_operation)
        self.assertTrue(resolved.mutation)
        self.assertFalse(resolved.read_only)

        harness.flow.advance()
        self.assertEqual(
            [("preview.describe", ""), ("prepare.prepare_code_only", "op-prepare-1")],
            harness.calls,
        )
        self.assertEqual("prepare_ready", harness.flow.snapshot().code)

    def test_the_backup_gate_still_holds_after_early_staging(self) -> None:
        """Staging does not buy the next step: the stop is still the stop."""

        harness = Harness(prepare_before_stop=True, stop=(MISSING_TOKEN_STOP,))
        harness.run(2)
        page = project(harness.flow)
        self.assertEqual("stop_owner", page.snapshot.actions[0])
        # The owner is still unmeasured, so the offered stop is the authority
        # check -- but continuing past it is not offered and not admitted.
        self.assertFalse(ACTIONS.action_is_admissible(page.snapshot, "continue_flow"))
        self.assertEqual("ignored", click(show(page), "continue_flow").outcome)

        harness.flow.advance()
        refused = project(harness.flow)
        self.assertEqual("token_missing", refused.snapshot.code)
        self.assertNotIn("continue_flow", refused.snapshot.actions)
        self.assertNotIn("prepare_app_files", refused.snapshot.actions)
        self.assertEqual("review", PRESENT.present_remote_update(refused.snapshot).next_action)
        # One staging call, one authenticated stop, and no backup behind them.
        self.assertEqual(
            [
                ("preview.describe", ""),
                ("prepare.prepare_code_only", "op-prepare-1"),
                ("owner.stop", "op-stop-1"),
            ],
            harness.calls,
        )

    def test_a_live_owner_after_early_staging_still_retries_the_stop(self) -> None:
        """The narrow stop retry carries the code-first order too.

        Staging happened before the stop here, so a still-live owner is the
        condition this order is most likely to meet.  The retry is admitted on
        the same measured evidence, re-runs the same stop, and the journey then
        reaches the backup that was always behind the verified stop.
        """

        harness = Harness(prepare_before_stop=True, stop=(STILL_LIVE_STOP, STOP_OK))
        harness.run(3)
        self.assertEqual("stop_owner_still_live", harness.flow.snapshot().code)
        page = project(harness.flow)
        self.assertEqual("owner_live", page.snapshot.code)
        self.assertEqual(
            "retry_stop", PRESENT.present_remote_update(page.snapshot).next_action
        )
        resolved = resolve(click(show(page), "retry_stop"), page, harness.flow)
        self.assertEqual(("retry", "retry_stage"), (resolved.host_kind, resolved.d_operation))

        harness.flow.retry()
        self.assertEqual("stop_verified", harness.flow.snapshot().code)
        resumed = project(harness.flow)
        self.assertEqual(
            "run_backup",
            resolve(click(show(resumed), "continue_flow"), resumed, harness.flow).d_operation,
        )
        self.assertEqual(
            [
                ("preview.describe", ""),
                ("prepare.prepare_code_only", "op-prepare-1"),
                ("owner.stop", "op-stop-1"),
                ("owner.stop", "op-stop-2"),
            ],
            harness.calls,
        )

    def test_the_default_order_never_offers_the_code_only_action(self) -> None:
        """Default-mode regression: full preparation stays on the gated button.

        In the default order the prepare offer can only appear behind a
        verified stop and a verified backup, so the gate that admits it is
        satisfied by measured evidence rather than bypassed -- and the click
        still reaches ``prepare.prepare``, never the code-only call.
        """

        harness = Harness()
        harness.run(3)
        self.assertEqual("backup_verified", harness.flow.snapshot().code)
        self.assertEqual(
            ("run_prepare", "inspect_diagnostics", "cancel"),
            harness.flow.snapshot().actions,
        )
        page = project(harness.flow)
        self.assertNotIn("prepare_app_files", page.snapshot.actions)
        self.assertEqual("continue_flow", page.snapshot.actions[0])
        # Admitted on the live-data gate, from what the stop actually measured.
        self.assertEqual("dead", page.snapshot.owner.state)
        self.assertTrue(ACTIONS.owner_allows_remote_mutation(page.snapshot))
        self.assertEqual(
            "run_prepare",
            resolve(click(show(page), "continue_flow"), page, harness.flow).d_operation,
        )
        harness.flow.advance()
        self.assertEqual(("prepare.prepare", "op-prepare-1"), harness.calls[-1])
        self.assertNotIn(
            "prepare.prepare_code_only", [name for name, _ in harness.calls]
        )


class LiveOwnerStopRetryTests(unittest.TestCase):
    """A stop that answered "still running" may be asked again, and only that.

    ``stop_owner_still_live`` is published only after an authenticated stop
    call returned with the session token available and the owner on this host
    still serving.  Re-running that same stop is the journey, not a new remote
    mutation, so it resolves to the flow's own retry and keeps the identity.
    """

    def test_live_owner_stop_retry_reaches_the_port_and_settles_the_stop(self) -> None:
        harness = Harness(stop=(STILL_LIVE_STOP, STOP_OK))
        harness.run(2)
        settled = harness.flow.snapshot()
        self.assertEqual(
            ("failed", "stop_owner_still_live"), (settled.stage, settled.code)
        )
        self.assertIn("retry_stage", settled.actions)

        page = project(harness.flow)
        self.assertEqual("owner_live", page.snapshot.code)
        self.assertIn("retry_stop", page.snapshot.actions)
        self.assertNotIn("retry", page.snapshot.actions)
        self.assertEqual(
            "retry_stop", PRESENT.present_remote_update(page.snapshot).next_action
        )

        resolved = resolve(click(show(page), "retry_stop"), page, harness.flow)
        self.assertEqual("dispatch", resolved.outcome)
        self.assertEqual("retry", resolved.host_kind)
        self.assertEqual("retry_stage", resolved.d_operation)

        # The host performs exactly that: the flow's retry, which re-runs the
        # same stop stage against the same authenticated port.
        harness.flow.retry()
        after = harness.flow.snapshot()
        self.assertEqual(("stop", "stop_verified"), (after.stage, after.code))
        self.assertEqual(
            [
                ("preview.describe", ""),
                ("owner.stop", "op-stop-1"),
                ("owner.stop", "op-stop-2"),
            ],
            harness.calls,
        )
        # The journey continues from there, still behind the quiescence gate.
        resumed = project(harness.flow)
        self.assertEqual("continue_flow", resumed.snapshot.actions[0])
        self.assertEqual(
            "run_backup",
            resolve(
                click(show(resumed), "continue_flow"), resumed, harness.flow
            ).d_operation,
        )

    def test_the_narrow_rules_rest_on_invariants_that_still_hold(self) -> None:
        """What the two new rules assume about D, asserted rather than trusted.

        The stop retry is keyed on ``owner_live`` because exactly one D code
        reaches it, and neither new action is a live-data mutation, so the
        gate that protects the SSOT must still name the same four actions.
        """

        self.assertEqual(
            ["stop_owner_still_live"],
            [d for d, e in PROJECT.D_TO_E_CODE.items() if e == PROJECT.STOP_RETRY_CODE],
        )
        self.assertEqual(
            frozenset({
                "update_connected_server", "retry", "restore_backup", "continue_flow",
            }),
            ACTIONS.NEW_REMOTE_MUTATIONS,
        )

    def test_no_other_stop_outcome_offers_the_stop_retry(self) -> None:
        cases = {
            "missing_token": MISSING_TOKEN_STOP,
            "foreign": FOREIGN_STOP,
            "unfenced": UNFENCED_STOP,
            "token_unknown": TOKEN_UNKNOWN_STOP,
        }
        for name, facts in cases.items():
            with self.subTest(stop=name):
                harness = Harness(stop=(facts,))
                harness.run(2)
                page = project(harness.flow)
                self.assertNotIn("retry_stop", page.snapshot.actions)
                self.assertNotIn("retry_stage", harness.flow.snapshot().actions)
                self.assertEqual("ignored", click(show(page), "retry_stop").outcome)
                self.assertEqual(
                    [("preview.describe", ""), ("owner.stop", "op-stop-1")],
                    harness.calls,
                )

    def test_a_non_stop_failure_keeps_the_general_retry_and_its_gate(self) -> None:
        harness = Harness(backup=(BackupOutcome(status="failed"),))
        harness.run(3)
        self.assertEqual("backup_failed", harness.flow.snapshot().code)
        page = project(harness.flow)
        self.assertIn("retry", page.snapshot.actions)
        self.assertNotIn("retry_stop", page.snapshot.actions)
        # The owner is measurably dead and quiesced here, so the general retry
        # is admissible on its own gate; the stop retry is simply not offered.
        self.assertEqual("dead", page.snapshot.owner.state)
        self.assertEqual(
            "retry_stage",
            resolve(click(show(page), "retry"), page, harness.flow).d_operation,
        )


if __name__ == "__main__":
    unittest.main()
