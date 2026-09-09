"""Finite D snapshot to E presentation and host action resolution.

The flow owns semantic operations; the view owns user-facing labels and
admission. This module is the explicit table between them. It reads an
actual flow snapshot object (or ``to_document`` / ``to_dict``), never a
raw-code pass-through, and it performs no effect and no network call.

Close always retires the screen. It never means cancel, dismiss, finish,
or completion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import remote_update_diagnostics as DIAGNOSTICS
import remote_update_flow_contract as FLOW
import remote_update_presentation as PRESENTATION
from remote_update_presentation_actions import (
    MAX_ACTIONS,
    READ_ONLY_ACTIONS,
    THIS_PC_ACTIONS,
    action_is_admissible,
)
from remote_update_view import RemoteUpdateAdmission


SCHEMA_VERSION = FLOW.SCHEMA_VERSION
# The single E code that ``stop_owner_still_live`` maps to, and the only
# condition in which the flow's one retry offer is the owner-stop retry.
STOP_RETRY_CODE = "owner_live"
R6_KEYS = (
    "schema_version",
    "stage",
    "code",
    "versions",
    "owner",
    "install",
    "backup",
    "actions",
)

D_TO_E_ACTION: dict[str, str] = {
    "run_preview": "preview_server_update",
    "run_stop": "stop_owner",
    "run_backup": "continue_flow",
    "run_prepare": "continue_flow",
    "run_prepare_code_only": "prepare_app_files",
    "run_probe": "continue_flow",
    "run_activate": "continue_flow",
    "run_verify": "verify_connection",
    "restart_desktop": "restart",
    "retry_stage": "retry",
    "reconcile_pending": "reconcile_pending",
    "rollback_activation": "rollback_activation",
    "restore_backup": "restore_backup",
    "cancel": "cancel",
    "finish": "close",
    "dismiss": "close",
    "inspect_diagnostics": "review",
    "resolve_owner_authority": "review",
    "resolve_activation_pairing": "review",
}

D_TO_E_CODE: dict[str, str] = {
    "idle": "unknown",
    "preview_ready": "unknown",
    "preview_failed": "failed",
    "preview_unknown": "unknown",
    "stop_verified": "owner_dead",
    "stop_verified_without_pidfd": "owner_dead",
    "stop_refused_missing_token": "token_missing",
    "stop_token_unknown": "unknown",
    "stop_refused_foreign_host": "owner_foreign",
    "stop_refused_unfenced_receipt": "owner_unfenced",
    "stop_owner_still_live": "owner_live",
    "stop_failed": "failed",
    "stop_incomplete_unknown": "unknown",
    "backup_verified": "unknown",
    "backup_failed": "backup_failed",
    "backup_not_run": "backup_required",
    "backup_unknown": "unknown",
    "backup_commit_unknown": "unknown",
    "backup_not_verified_before_start": "backup_required",
    "prepare_ready": "unknown",
    "prepare_failed": "failed",
    "prepare_unknown": "unknown",
    "prepare_commit_unknown": "unknown",
    "prepare_capability_unavailable": "nfs_alternative",
    "previous_app_not_preserved": "failed",
    "previous_app_preservation_unknown": "unknown",
    "probe_verified": "unknown",
    "probe_failed": "failed",
    "probe_unknown": "unknown",
    "probe_workspace_mismatch": "failed",
    "probe_workspace_unknown": "unknown",
    "activate_pending_restart": "unknown",
    "activate_restart_not_selected": "unknown",
    "activate_restart_identity_mismatch": "unknown",
    "activate_workspace_mismatch": "failed",
    "activate_workspace_unknown": "unknown",
    "activate_committed": "unknown",
    "activate_failed": "failed",
    "activate_unknown": "unknown",
    "activate_commit_unknown": "unknown",
    "activate_confirm_failed": "failed",
    "activate_confirm_unknown": "unknown",
    "update_ready": "ready",
    "verify_failed": "failed",
    "verify_unknown": "unknown",
    "cancelled_before_preview": "cancelled",
    "cancelled_before_stop": "cancelled",
    "cancelled_before_backup": "cancelled",
    "cancelled_before_prepare": "cancelled",
    "cancelled_before_probe": "cancelled",
    "cancelled_before_activate": "cancelled",
    "cancel_deferred_commit_unknown": "unknown",
    "restore_verified": "failed",
    "restore_verified_activation_selected": "failed",
    "restore_failed": "failed",
    "restore_unknown": "unknown",
    "rollback_verified": "failed",
    "rollback_failed": "failed",
    "rollback_unknown": "unknown",
}

D_TO_F_CODE: dict[str, str] = {
    "idle": "IDLE",
    "preview_ready": "PREVIEW_READY",
    "preview_failed": "PREVIEW_FAILED",
    "preview_unknown": "PREVIEW_UNKNOWN",
    "stop_verified": "STOP_VERIFIED",
    "stop_verified_without_pidfd": "STOP_VERIFIED_WITHOUT_PIDFD",
    "stop_refused_missing_token": "STOP_REFUSED_MISSING_TOKEN",
    "stop_token_unknown": "STOP_TOKEN_UNKNOWN",
    "stop_refused_foreign_host": "STOP_REFUSED_FOREIGN_HOST",
    "stop_refused_unfenced_receipt": "STOP_REFUSED_UNFENCED_RECEIPT",
    "stop_owner_still_live": "STOP_OWNER_STILL_LIVE",
    "stop_failed": "STOP_FAILED",
    "stop_incomplete_unknown": "STOP_INCOMPLETE_UNKNOWN",
    "backup_verified": "BACKUP_VERIFIED",
    "backup_failed": "BACKUP_FAILED",
    "backup_not_run": "BACKUP_NOT_RUN",
    "backup_unknown": "BACKUP_UNKNOWN",
    "backup_commit_unknown": "BACKUP_COMMIT_UNKNOWN",
    "backup_not_verified_before_start": "BACKUP_NOT_VERIFIED_BEFORE_START",
    "prepare_ready": "PREPARE_READY",
    "prepare_failed": "PREPARE_FAILED",
    "prepare_unknown": "PREPARE_UNKNOWN",
    "prepare_commit_unknown": "PREPARE_COMMIT_UNKNOWN",
    "prepare_capability_unavailable": "PREPARE_CAPABILITY_UNAVAILABLE",
    "previous_app_not_preserved": "PREVIOUS_APP_NOT_PRESERVED",
    "previous_app_preservation_unknown": "PREVIOUS_APP_PRESERVATION_UNKNOWN",
    "probe_verified": "PROBE_VERIFIED",
    "probe_failed": "PROBE_FAILED",
    "probe_unknown": "PROBE_UNKNOWN",
    "probe_workspace_mismatch": "PROBE_WORKSPACE_MISMATCH",
    "probe_workspace_unknown": "PROBE_WORKSPACE_UNKNOWN",
    "activate_pending_restart": "ACTIVATE_PENDING_RESTART",
    "activate_restart_not_selected": "ACTIVATE_RESTART_NOT_SELECTED",
    "activate_restart_identity_mismatch": "ACTIVATE_RESTART_IDENTITY_MISMATCH",
    "activate_workspace_mismatch": "ACTIVATE_WORKSPACE_MISMATCH",
    "activate_workspace_unknown": "ACTIVATE_WORKSPACE_UNKNOWN",
    "activate_committed": "ACTIVATE_COMMITTED",
    "activate_failed": "ACTIVATE_FAILED",
    "activate_unknown": "ACTIVATE_UNKNOWN",
    "activate_commit_unknown": "ACTIVATE_COMMIT_UNKNOWN",
    "activate_confirm_failed": "ACTIVATE_CONFIRM_FAILED",
    "activate_confirm_unknown": "ACTIVATE_CONFIRM_UNKNOWN",
    "update_ready": "UPDATE_READY",
    "verify_failed": "VERIFY_FAILED",
    "verify_unknown": "VERIFY_UNKNOWN",
    "cancelled_before_preview": "CANCELLED_BEFORE_PREVIEW",
    "cancelled_before_stop": "CANCELLED_BEFORE_STOP",
    "cancelled_before_backup": "CANCELLED_BEFORE_BACKUP",
    "cancelled_before_prepare": "CANCELLED_BEFORE_PREPARE",
    "cancelled_before_probe": "CANCELLED_BEFORE_PROBE",
    "cancelled_before_activate": "CANCELLED_BEFORE_ACTIVATE",
    "cancel_deferred_commit_unknown": "CANCEL_DEFERRED_COMMIT_UNKNOWN",
    "restore_verified": "RESTORE_VERIFIED",
    "restore_verified_activation_selected": "RESTORE_VERIFIED_ACTIVATION_SELECTED",
    "restore_failed": "RESTORE_FAILED",
    "restore_unknown": "RESTORE_UNKNOWN",
    "rollback_verified": "ROLLBACK_VERIFIED",
    "rollback_failed": "ROLLBACK_FAILED",
    "rollback_unknown": "ROLLBACK_UNKNOWN",
}

HOST_KIND = {
    "preview_server_update": "advance",
    "stop_owner": "advance",
    "prepare_app_files": "advance",
    "continue_flow": "advance",
    "verify_connection": "verify",
    "retry": "retry",
    "retry_stop": "retry",
    "reconcile_pending": "reconcile",
    "rollback_activation": "rollback",
    "restore_backup": "restore",
    "restart": "restart",
    "cancel": "cancel",
    "close": "retire",
    "review": "review",
    "update_this_pc": "pc_independent",
    "download_this_pc": "pc_independent",
    "check_this_pc": "pc_independent",
    "update_connected_server": "advance",
}

E_TO_D_OPERATION: dict[str, tuple[str, ...]] = {
    "preview_server_update": ("run_preview",),
    "stop_owner": ("run_stop",),
    "prepare_app_files": ("run_prepare_code_only",),
    "continue_flow": (
        "run_backup", "run_prepare", "run_probe", "run_activate",
    ),
    "verify_connection": ("run_verify",),
    "retry": ("retry_stage",),
    "retry_stop": ("retry_stage",),
    "reconcile_pending": ("reconcile_pending",),
    "rollback_activation": ("rollback_activation",),
    "restore_backup": ("restore_backup",),
    "restart": ("restart_desktop",),
    "cancel": ("cancel",),
    "review": (
        "inspect_diagnostics",
        "resolve_owner_authority",
        "resolve_activation_pairing",
    ),
}


@dataclass(frozen=True)
class RemoteUpdateBinding:
    session_id: str
    workspace_id: str
    operation_id: str | None
    operation_kind: str | None
    stage: str
    d_code: str
    e_code: str
    d_actions: tuple[str, ...]
    e_actions: tuple[str, ...]


@dataclass(frozen=True)
class RemoteUpdateProjection:
    snapshot: PRESENTATION.RemoteUpdateSnapshot
    binding: RemoteUpdateBinding
    diagnostics: dict[str, object]
    d_document: dict[str, object]


@dataclass(frozen=True)
class ResolvedRemoteUpdateOperation:
    outcome: Literal["dispatch", "retire", "ignored", "unbound"]
    host_kind: str | None = None
    e_action: str | None = None
    d_operation: str | None = None
    operation_id: str | None = None
    operation_kind: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    mutation: bool = False
    read_only: bool = True
    reason: str | None = None


def project_remote_update(
    source: object,
    *,
    session_id: str,
    workspace_id: str,
    pc_actions: tuple[str, ...] = (),
) -> RemoteUpdateProjection:
    """Project a D snapshot or flow into an E snapshot and F diagnostics."""

    snapshot, pending, retained = _flow_parts(source)
    document = _r6_document(snapshot)
    session = _binding_id(session_id)
    workspace = _binding_id(workspace_id)
    if session is None or workspace is None:
        raise ValueError("Remote update binding requires canonical session and workspace ids")
    d_code = document.get("code") if isinstance(document.get("code"), str) else "unknown"
    e_code = D_TO_E_CODE.get(d_code, "unknown") if d_code in FLOW.CODES else "unknown"
    d_actions = _d_actions(document.get("actions"))
    e_actions = _e_actions(
        d_actions, e_code, pc_actions, known_d_code=d_code in FLOW.CODES
    )
    d_stage = document.get("stage") if isinstance(document.get("stage"), str) else "unknown"
    stage = d_stage if d_stage in PRESENTATION.STAGES else "unknown"
    mapped = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "code": e_code,
        "versions": _copy_map(document.get("versions"), FLOW.VERSION_FIELDS),
        "owner": _copy_map(document.get("owner"), FLOW.OWNER_FIELDS),
        "install": _copy_map(document.get("install"), FLOW.INSTALL_FIELDS),
        "backup": _copy_map(document.get("backup"), FLOW.BACKUP_FIELDS),
        "actions": list(e_actions),
    }
    e_snapshot = PRESENTATION.normalize_remote_update_snapshot(mapped)
    bound = pending or retained
    return RemoteUpdateProjection(
        snapshot=e_snapshot,
        binding=RemoteUpdateBinding(
            session_id=session,
            workspace_id=workspace,
            operation_id=_operation_id(bound),
            operation_kind=_operation_kind(bound),
            stage=d_stage,
            d_code=d_code,
            e_code=e_snapshot.code,
            d_actions=d_actions,
            e_actions=e_snapshot.actions,
        ),
        diagnostics=_f_diagnostics(document, e_snapshot.actions),
        d_document=document,
    )


def resolve_remote_update_action(
    admission: RemoteUpdateAdmission,
    projection: RemoteUpdateProjection,
    *,
    current: object,
    session_id: str,
    workspace_id: str,
) -> ResolvedRemoteUpdateOperation:
    """Resolve a click against the current flow. No effects are performed."""

    session = _binding_id(session_id)
    workspace = _binding_id(workspace_id)
    gated = _gate_admission(admission, projection, session, workspace)
    if gated is not None:
        return gated
    stale = _stale_reason(projection.binding, current, session, workspace)
    if stale is not None:
        return ResolvedRemoteUpdateOperation("ignored", reason=stale)
    return _dispatch_action(admission.request.operation, projection)


def _gate_admission(
    admission: RemoteUpdateAdmission,
    projection: RemoteUpdateProjection,
    session: str | None,
    workspace: str | None,
) -> ResolvedRemoteUpdateOperation | None:
    if admission.outcome == "unbound":
        return ResolvedRemoteUpdateOperation("unbound", reason="unbound")
    if session is None or workspace is None:
        return ResolvedRemoteUpdateOperation("ignored", reason="stale_binding")
    if session != projection.binding.session_id or workspace != projection.binding.workspace_id:
        return ResolvedRemoteUpdateOperation("ignored", reason="stale_binding")
    if admission.outcome == "close":
        return _retire(projection)
    if admission.outcome != "action" or admission.request is None:
        return ResolvedRemoteUpdateOperation("ignored", reason="ignored")
    return None


def _dispatch_action(
    action: str, projection: RemoteUpdateProjection
) -> ResolvedRemoteUpdateOperation:
    if action == "close":
        return _retire(projection)
    if not projection.binding.e_actions:
        return ResolvedRemoteUpdateOperation("ignored", reason="empty_actions")
    if action not in projection.binding.e_actions:
        return ResolvedRemoteUpdateOperation("ignored", reason="stale_actions")
    if not action_is_admissible(projection.snapshot, action):
        return ResolvedRemoteUpdateOperation("ignored", reason="not_admitted")
    host_kind = HOST_KIND.get(action)
    if host_kind is None:
        return ResolvedRemoteUpdateOperation("ignored", reason="unknown")
    read_only = action in READ_ONLY_ACTIONS or action in THIS_PC_ACTIONS
    return ResolvedRemoteUpdateOperation(
        "dispatch",
        host_kind=host_kind,
        e_action=action,
        d_operation=_d_operation_for(action, projection.binding.d_actions),
        operation_id=projection.binding.operation_id,
        operation_kind=projection.binding.operation_kind,
        session_id=projection.binding.session_id,
        workspace_id=projection.binding.workspace_id,
        mutation=not read_only and action != "restart",
        read_only=read_only,
    )


def _retire(projection: RemoteUpdateProjection) -> ResolvedRemoteUpdateOperation:
    return ResolvedRemoteUpdateOperation(
        "retire",
        host_kind="retire",
        e_action="close",
        d_operation=None,
        operation_id=projection.binding.operation_id,
        operation_kind=projection.binding.operation_kind,
        session_id=projection.binding.session_id,
        workspace_id=projection.binding.workspace_id,
        mutation=False,
        read_only=True,
        reason="close_retires_screen",
    )


def _stale_reason(
    binding: RemoteUpdateBinding,
    current: object,
    session_id: str,
    workspace_id: str,
) -> str | None:
    if session_id != binding.session_id or workspace_id != binding.workspace_id:
        return "stale_binding"
    snapshot, pending, retained = _flow_parts(current)
    document = _r6_document(snapshot)
    stage = document.get("stage") if isinstance(document.get("stage"), str) else "unknown"
    code = document.get("code") if isinstance(document.get("code"), str) else "unknown"
    if stage != binding.stage:
        return "stale_stage"
    if code != binding.d_code:
        return "stale_code"
    if _d_actions(document.get("actions")) != binding.d_actions:
        return "stale_actions"
    bound = pending or retained
    if _operation_id(bound) != binding.operation_id:
        return "stale_operation"
    return None


def _flow_parts(source: object) -> tuple[object, object | None, object | None]:
    pending = _call_optional(source, "pending_operation")
    retained = _call_optional(source, "retained_activation")
    snapshot = _call_optional(source, "snapshot")
    return (snapshot if snapshot is not None else source, pending, retained)


def _call_optional(source: object, name: str) -> object | None:
    method = getattr(source, name, None)
    if not callable(method):
        return None
    return method()


def _r6_document(snapshot: object) -> dict[str, object]:
    for name in ("to_document", "to_dict"):
        method = getattr(snapshot, name, None)
        if callable(method):
            document = method()
            if isinstance(document, dict):
                return _copy_r6(document)
    if isinstance(snapshot, dict):
        return _copy_r6(snapshot)
    return {}


def _copy_r6(document: dict[str, object]) -> dict[str, object]:
    copied: dict[str, object] = {}
    for key in R6_KEYS:
        if key in document:
            copied[key] = document[key]
    copied["schema_version"] = SCHEMA_VERSION
    return copied


def _copy_map(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    mapping = value if isinstance(value, dict) else {}
    return {name: mapping.get(name) for name in fields}


def _d_actions(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    actions: list[str] = []
    for item in value[:MAX_ACTIONS]:
        if item in FLOW.ACTIONS and item not in actions:
            actions.append(item)
    return tuple(actions)


def _e_actions(
    d_actions: tuple[str, ...],
    e_code: str,
    pc_actions: tuple[str, ...],
    *,
    known_d_code: bool,
) -> tuple[str, ...]:
    mapped: list[str] = []
    for action in pc_actions:
        if action in THIS_PC_ACTIONS and action not in mapped:
            mapped.append(action)
    if known_d_code:
        for action in d_actions:
            e_action = _e_action_for(action, e_code)
            if e_action is None or e_action in mapped:
                continue
            if e_action == "rollback_activation" and e_code == "unknown":
                continue
            mapped.append(e_action)
    return tuple(mapped[:MAX_ACTIONS])


def _e_action_for(d_action: str, e_code: str) -> str | None:
    """The E action for one offered D operation, in the condition it is offered.

    ``retry_stage`` is the flow's only retry offer, so the condition the offer
    is made in is what says which retry it is.  ``owner_live`` is reached from
    exactly one D code, ``stop_owner_still_live``, which is published only when
    an authenticated owner stop answered and found the owner still running;
    there the offer is the narrow stop retry, which re-runs that same stop.
    Every other failure keeps the general retry and the quiescence gate on it.
    """

    if d_action == "retry_stage" and e_code == STOP_RETRY_CODE:
        return "retry_stop"
    return D_TO_E_ACTION.get(d_action)


def _d_operation_for(e_action: str, d_actions: tuple[str, ...]) -> str | None:
    candidates = E_TO_D_OPERATION.get(e_action, ())
    for action in d_actions:
        if action in candidates:
            return action
    return None


def _f_diagnostics(
    document: dict[str, object], e_actions: tuple[str, ...]
) -> dict[str, object]:
    d_code = document.get("code")
    f_code = D_TO_F_CODE.get(d_code) if isinstance(d_code, str) else None
    payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": document.get("stage"),
        "code": f_code,
        "versions": _copy_map(document.get("versions"), FLOW.VERSION_FIELDS),
        "owner": _copy_map(document.get("owner"), FLOW.OWNER_FIELDS),
        "install": _copy_map(document.get("install"), FLOW.INSTALL_FIELDS),
        "backup": _copy_map(document.get("backup"), FLOW.BACKUP_FIELDS),
        "actions": list(e_actions),
    }
    return DIAGNOSTICS.normalize_view(payload)


def _binding_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if parsed.int == 0 or str(parsed) != value:
        return None
    return value


def _operation_id(bound: object) -> str | None:
    value = getattr(bound, "operation_id", None)
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    if any(character in value for character in "/\\: \t"):
        return None
    return value


def _operation_kind(bound: object) -> str | None:
    value = getattr(bound, "kind", None)
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    return value if value in FLOW.MUTATIONS else None
