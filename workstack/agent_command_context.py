from __future__ import annotations

import datetime
import json
from typing import Any

from workstack.agent_cli_contract import (
    AgentBackend,
    AgentOutcome,
    CONTEXT_COMMAND,
    ContextRequest,
    render_outcome,
)
from workstack.agent_context_pack import (
    PLANNING_V2_VIEW,
    build_planning_blocks,
    is_known_view,
    needs_planning_material,
    planning_omitted,
    shrink_planning_data,
    validate_planning_data,
)

__all__ = ("handle_context",)


COMMAND = "agent.{}".format(CONTEXT_COMMAND)
TASK_ALLOWLIST = frozenset(
    {"detail", "due", "id", "priority", "revision", "status", "title", "uid"}
)
OMITTED_CATEGORIES = (
    "attachments",
    "captures",
    "objectives",
    "relationships",
    "work_sessions",
)
OVERFLOW_MARKER = "recent_worklog_overflow"
PLANNING_MATERIAL_KEYS = ("context", "objectives", "tasks")
CONTEXT_TOO_LARGE_MSG = "the Task core projection alone exceeds the envelope bound"
INTERNAL_ERROR_MSG = "unexpected exception; envelope is content-free"
LOOKBACK_DAYS = 30
MAX_ENTRIES = 5
ENVELOPE_MAX_BYTES = 32768


def _build_envelope_bytes(
    *,
    data: dict[str, Any],
    task_id: str,
    transport: str,
    workspace_uid: str,
) -> int:
    envelope = {
        "contract": "workstack.cli.v1",
        "data": data,
        "meta": {
            "command": COMMAND,
            "task_id": task_id,
            "transport": transport,
            "workspace_uid": workspace_uid,
        },
    }
    raw = json.dumps(
        envelope,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(raw.encode("utf-8")) + 1


def _project_task(raw: dict[str, Any]) -> dict[str, Any]:
    projected = {key: raw[key] for key in TASK_ALLOWLIST if key in raw}
    if set(projected) != TASK_ALLOWLIST:
        raise ValueError("backend Task projection is incomplete")
    return projected


def _project_entry(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": raw["date"],
        "done": raw.get("done", []),
        "next": raw.get("next", []),
        "blockers": raw.get("blockers", []),
    }


def _filter_entries(
    entries: list[dict[str, Any]],
    task_id: str,
    today: datetime.date,
) -> list[dict[str, Any]]:
    cutoff = today - datetime.timedelta(days=LOOKBACK_DAYS)
    result: list[dict[str, Any]] = []
    for e in entries:
        if e.get("task_id") != task_id:
            continue
        try:
            value = e["date"]
            if not isinstance(value, str):
                continue
            d = datetime.date.fromisoformat(value)
        except (ValueError, TypeError, KeyError):
            continue
        if d.isoformat() != value:
            continue
        if cutoff <= d <= today:
            result.append(e)
    result.sort(key=lambda e: e["date"], reverse=True)
    return result


def _make_outcome(
    *,
    data: dict[str, Any] | None,
    error_code: str | None,
    error_message: str | None,
    task_id: str | None,
    transport: str | None,
    workspace_uid: str | None,
) -> AgentOutcome:
    return AgentOutcome(
        command=COMMAND,
        commit_state=None,
        data=data,
        error_code=error_code,
        error_details={},
        error_message=error_message,
        intent_id=None,
        replayed=None,
        retryable=None,
        task_id=task_id,
        transport=transport,
        workspace_uid=workspace_uid,
    )


def _failure(*, code: str, message: str) -> AgentOutcome:
    return _make_outcome(
        data=None,
        error_code=code,
        error_message=message,
        task_id=None,
        transport=None,
        workspace_uid=None,
    )


def _context_size(
    *,
    data: dict[str, Any],
    task_id: str,
    transport: str,
    workspace_uid: str,
) -> int:
    return _build_envelope_bytes(
        data=data,
        task_id=task_id,
        transport=transport,
        workspace_uid=workspace_uid,
    )


def _planning_material(raw: dict[str, Any]) -> dict[str, Any]:
    """The extra read material a planning view backend must have supplied."""

    material = raw.get("planning")
    if type(material) is not dict or set(material) != set(PLANNING_MATERIAL_KEYS):
        raise ValueError("backend planning material is missing or malformed")
    return material


def _require_backend_context(
    raw: object,
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    """Admit one backend mapping or raise; the caller maps that to internal_error."""

    if not isinstance(raw, dict):
        raise ValueError("backend context result must be a mapping")
    workspace_uid = raw["workspace_uid"]
    transport = raw["transport"]
    raw_task = raw["task"]
    raw_entries = raw["entries"]
    if (
        not isinstance(workspace_uid, str)
        or not isinstance(transport, str)
        or not isinstance(raw_task, dict)
        or not isinstance(raw_entries, list)
        or any(not isinstance(entry, dict) for entry in raw_entries)
    ):
        raise ValueError("backend context result has an invalid shape")
    return workspace_uid, transport, raw_task, raw_entries


def _planning_outcome(
    *,
    raw: dict[str, Any],
    request: ContextRequest,
    task: dict[str, Any],
    entries: list[dict[str, Any]],
    overflow: bool,
    transport: str,
    workspace_uid: str,
) -> AgentOutcome:
    """The opt-in planning answer: core plus three capped, bounded blocks."""

    include_evidence = request.view == PLANNING_V2_VIEW
    material = _planning_material(raw)
    blocks, overflowed = build_planning_blocks(
        task_id=request.task_id,
        objectives=material["objectives"],
        tasks=material["tasks"],
        context=material["context"],
        include_evidence=include_evidence,
    )
    omitted = planning_omitted(overflowed=overflowed)
    if overflow:
        omitted.append(OVERFLOW_MARKER)
    data: dict[str, Any] = {
        "workspace_uid": workspace_uid,
        "task": task,
        "recent_worklog": entries,
        "omitted": omitted,
    }
    data.update(blocks)
    if include_evidence:
        data["view"] = PLANNING_V2_VIEW

    def size(candidate: dict[str, Any]) -> int:
        return _context_size(
            data=candidate,
            task_id=request.task_id,
            transport=transport,
            workspace_uid=workspace_uid,
        )

    if not shrink_planning_data(
        data=data,
        overflowed=overflowed,
        core_overflow_marker=OVERFLOW_MARKER,
        core_overflowed=overflow,
        size=size,
        limit=ENVELOPE_MAX_BYTES,
    ):
        return _failure(code="context_too_large", message=CONTEXT_TOO_LARGE_MSG)
    # Nothing planning-shaped is rendered unvalidated: the frozen contract module
    # validates the core half and the key set, this validates the blocks in full.
    validate_planning_data(data, core_overflow_marker=OVERFLOW_MARKER)
    outcome = _make_outcome(
        data=data,
        error_code=None,
        error_message=None,
        task_id=request.task_id,
        transport=transport,
        workspace_uid=workspace_uid,
    )
    render_outcome(outcome=outcome)
    return outcome


def _core_outcome(
    *,
    request: ContextRequest,
    task: dict[str, Any],
    entries: list[dict[str, Any]],
    overflow: bool,
    transport: str,
    workspace_uid: str,
) -> AgentOutcome:
    """The default core-v1 answer: Task allowlist plus the bounded worklog."""

    omitted = list(OMITTED_CATEGORIES)
    if overflow:
        omitted.append(OVERFLOW_MARKER)
    data: dict[str, Any] = {
        "workspace_uid": workspace_uid,
        "task": task,
        "recent_worklog": entries,
        "omitted": omitted,
    }
    core = dict(data)
    core["recent_worklog"] = []
    if (
        _context_size(
            data=core,
            task_id=request.task_id,
            transport=transport,
            workspace_uid=workspace_uid,
        )
        > ENVELOPE_MAX_BYTES
    ):
        return _failure(code="context_too_large", message=CONTEXT_TOO_LARGE_MSG)

    while (
        _context_size(
            data=data,
            task_id=request.task_id,
            transport=transport,
            workspace_uid=workspace_uid,
        )
        > ENVELOPE_MAX_BYTES
    ):
        if not entries:
            return _failure(code="context_too_large", message=CONTEXT_TOO_LARGE_MSG)
        entries.pop()
        if not overflow:
            overflow = True
            data["omitted"] = list(OMITTED_CATEGORIES) + [OVERFLOW_MARKER]

    outcome = _make_outcome(
        data=data,
        error_code=None,
        error_message=None,
        task_id=request.task_id,
        transport=transport,
        workspace_uid=workspace_uid,
    )
    render_outcome(outcome=outcome)
    return outcome


def handle_context(
    *,
    request: ContextRequest,
    backend: AgentBackend,
    today: datetime.date,
) -> AgentOutcome:
    # A view this build does not implement is refused BEFORE any backend read.
    # The CLI already rejects an unknown --view at the parser, so reaching here
    # means a direct caller; silently answering core-v1 would hide that.
    if not is_known_view(getattr(request, "view", None)):
        return _failure(code="internal_error", message=INTERNAL_ERROR_MSG)
    try:
        raw = backend.context(request=request, today=today)
        workspace_uid, transport, raw_task, raw_entries = _require_backend_context(raw)
        task = _project_task(raw_task)
        filtered = _filter_entries(raw_entries, request.task_id, today)
        projected = [_project_entry(entry) for entry in filtered]
        entries = projected[:MAX_ENTRIES]
        overflow = len(projected) > MAX_ENTRIES
        if needs_planning_material(request.view):
            return _planning_outcome(
                raw=raw,
                request=request,
                task=task,
                entries=entries,
                overflow=overflow,
                transport=transport,
                workspace_uid=workspace_uid,
            )
        return _core_outcome(
            request=request,
            task=task,
            entries=entries,
            overflow=overflow,
            transport=transport,
            workspace_uid=workspace_uid,
        )
    except Exception:
        return _failure(code="internal_error", message=INTERNAL_ERROR_MSG)
