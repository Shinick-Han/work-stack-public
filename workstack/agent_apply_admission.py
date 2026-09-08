"""Admit legacy agent apply against existing authority before Store construction."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Callable

from workstack import owner_authority
from workstack.agent_authority import admit_authority
from workstack.cli_routing import refuse_authority_state


AGENT_APPLY_LIMIT = 32 * 1024
AGENT_TASK_FIELDS = frozenset({
    "title", "detail", "status", "priority", "due", "scheduled",
    "estimate_minutes", "tags", "objective_ids", "parent_id", "dependencies",
    "key_result_refs",
})
INTENT_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,128}")
ApplyFn = Callable[..., int]


def require_apply_locator(arguments: argparse.Namespace) -> tuple[Path, str]:
    data_dir = getattr(arguments, "data_dir", None)
    workspace_uid = getattr(arguments, "workspace_uid", None)
    if type(data_dir) is not str or not data_dir:
        raise ValueError("invalid_authority")
    if type(workspace_uid) is not str or not workspace_uid:
        raise ValueError("invalid_authority")
    return Path(data_dir), workspace_uid


def require_intent_id(intent_id: object) -> str:
    if type(intent_id) is not str or INTENT_PATTERN.fullmatch(intent_id) is None:
        raise ValueError("intent_id must be 8-128 safe identifier characters")
    return intent_id


def parse_apply_packet(raw: bytes) -> dict[str, object]:
    if len(raw) > AGENT_APPLY_LIMIT:
        raise ValueError("agent apply packet exceeds 32 KiB")
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeError, RecursionError, ValueError):
        raise ValueError("stdin must contain one UTF-8 JSON object") from None
    return _validated_apply_packet(packet)


def _validated_apply_packet(packet: object) -> dict[str, object]:
    if not isinstance(packet, dict) or set(packet) != {
        "workspace_id", "task_id", "expected_revision", "changes"
    }:
        raise ValueError(
            "agent apply requires only workspace_id, task_id, expected_revision, and changes"
        )
    _require_apply_workspace_id(packet["workspace_id"])
    _require_apply_task_id(packet["task_id"])
    _require_apply_revision(packet["expected_revision"])
    _require_apply_changes(packet["changes"])
    return packet


def _require_apply_workspace_id(workspace_id: object) -> None:
    try:
        parsed = uuid.UUID(str(workspace_id))
    except (ValueError, AttributeError) as error:
        raise ValueError("workspace_id must be a canonical UUID") from error
    if str(parsed) != workspace_id or parsed.int == 0:
        raise ValueError("workspace_id must be a canonical non-nil UUID")


def _require_apply_task_id(task_id: object) -> None:
    if not isinstance(task_id, str) or re.fullmatch(r"T-[0-9]{4,}", task_id) is None:
        raise ValueError("task_id must be a canonical Work Stack Task ID")


def _require_apply_revision(expected_revision: object) -> None:
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")


def _require_apply_changes(changes: object) -> None:
    if (
        not isinstance(changes, dict)
        or not changes
        or not set(changes) <= AGENT_TASK_FIELDS
    ):
        raise ValueError("changes must contain only supported mutable Task fields")


def require_packet_matches_uid(packet: dict[str, object], expected_workspace_uid: str) -> None:
    if packet["workspace_id"] != expected_workspace_uid:
        raise ValueError("workspace_mismatch")


def require_held_local_apply(store: object, expected_workspace_uid: object) -> None:
    journal_path = getattr(store, "journal_path", None)
    if journal_path is not None and Path(journal_path).exists():
        raise refuse_authority_state(owner_authority.RECOVERY_OR_SYNC_BLOCKED)
    loader = getattr(store, "load", None)
    workspace = loader("workspace.json") if callable(loader) else None
    if type(workspace) is not dict or workspace.get("id") != expected_workspace_uid:
        raise ValueError("agent apply workspace_id does not match this Store")
    reader = getattr(store, "sync_status", None)
    try:
        status = reader() if callable(reader) else None
    except (OSError, ValueError, RuntimeError):
        raise refuse_authority_state(owner_authority.RECOVERY_OR_SYNC_BLOCKED) from None
    if type(status) is not dict or status.get("state") != "in-sync":
        raise refuse_authority_state(owner_authority.RECOVERY_OR_SYNC_BLOCKED)


def dispatch_admitted_apply(
    arguments: argparse.Namespace,
    raw: bytes,
    intent_id: str,
    *,
    apply: ApplyFn,
) -> int:
    data_dir, expected_workspace_uid = require_apply_locator(arguments)
    require_intent_id(intent_id)
    packet = parse_apply_packet(raw)
    require_packet_matches_uid(packet, expected_workspace_uid)
    admission = admit_authority(
        data_dir=data_dir,
        expected_workspace_uid=expected_workspace_uid,
    )
    require_packet_matches_uid(packet, admission.workspace_uid)
    authority = owner_authority.acquire_owner_authority(
        data_dir=admission.data_dir,
        expected_workspace_uid=admission.workspace_uid,
        store_factory=owner_authority.Store,
    )
    if authority.state == owner_authority.EXCLUSIVE_LOCAL_HELD:
        result = authority.run(
            lambda store: apply(
                store, packet, intent_id, route="exclusive-local-store"
            )
        )
        return 0 if result is None else int(result)
    if authority.state == owner_authority.OWNER_ROUTE_REQUIRED:
        store = authority.store
        if store is None:
            raise refuse_authority_state(owner_authority.OWNER_ROUTE_REQUIRED)
        try:
            return apply(store, packet, intent_id, route="running-server")
        finally:
            authority.release()
    raise refuse_authority_state(authority.state)
