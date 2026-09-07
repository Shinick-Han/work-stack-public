"""Admit ordinary CLI commands under one owner-authority lease.

Path tools, agent envelope/apply, checkpoint-state, capture ingest and
graph.serve keep their existing families. This module only admits the
stack/local-file commands: one acquire, held through the operation, released
on success or error, with no probe-then-reacquire and no local fallback after
an owner, mismatch, recovery or unknown-commit refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import Callable

from workstack import cli_capabilities as caps
from workstack import cli_reads, cli_writer
from workstack.owner_authority import (
    COMMIT_UNKNOWN,
    EXCLUSIVE_LOCAL_HELD,
    OWNER_ROUTE_REQUIRED,
    OWNER_UNAVAILABLE,
    RECOVERY_OR_SYNC_BLOCKED,
    WORKSPACE_MISMATCH,
    acquire_owner_authority,
)
from workstack.store import Store


CoordinatesReader = cli_writer.CoordinatesReader
RequestJson = cli_writer.RequestJson
OwnerWriter = Callable[[Store, str], dict[str, object]]
LocalRunner = Callable[[argparse.Namespace, Store], int]
Emitter = Callable[[object], None]

_IDENTITY_READ_LIMIT = 64 * 1024
_REFUSALS = {
    WORKSPACE_MISMATCH: "workspace identity does not match this data directory",
    RECOVERY_OR_SYNC_BLOCKED: (
        "this Work Stack store is blocked by recovery or synchronization"
    ),
    OWNER_UNAVAILABLE: "Work Stack writer authority is unavailable",
    COMMIT_UNKNOWN: "commit is unknown; inspect the store before retrying",
}


def planning_status(action: str) -> str:
    return {
        "start": "started",
        "done": "done",
        "drop": "dropped",
        "reopen": "open",
    }[action]


def resolve_data_dir(explicit: str | None) -> Path:
    """Resolve the selected data directory without creating it."""

    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("WORK_STACK_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "WorkStack" / "data").resolve()
    return (Path.home() / ".local" / "share" / "workstack").resolve()


def _canonical_workspace_uid(value: object) -> str:
    if type(value) is not str:
        raise OSError("Work Stack workspace identity is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise OSError("Work Stack workspace identity is invalid") from error
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise OSError("Work Stack workspace identity is invalid")
    return value


def read_workspace_identity(data_dir: Path) -> str:
    """Read workspace.json identity. Directory name is never an identity."""

    path = data_dir / "workspace.json"
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_IDENTITY_READ_LIMIT + 1)
    except OSError as error:
        raise OSError("Work Stack workspace identity is unavailable") from error
    if len(raw) > _IDENTITY_READ_LIMIT:
        raise OSError("Work Stack workspace identity is unavailable")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError("Work Stack workspace identity is invalid") from error
    if type(payload) is not dict:
        raise OSError("Work Stack workspace identity is invalid")
    return _canonical_workspace_uid(payload.get("id"))


def expected_identity(arguments: argparse.Namespace, data_dir: Path) -> str:
    actual = read_workspace_identity(data_dir)
    pin = getattr(arguments, "workspace_uid", None)
    if pin in (None, ""):
        return actual
    return _canonical_workspace_uid(pin)


def refuse_authority_state(state: str) -> OSError:
    return OSError(
        _REFUSALS.get(state, "Work Stack writer authority is unavailable")
    )


def _refuse_dispatch(capability: caps.CliCapability, state: str) -> OSError:
    """Map a refused authority state onto the command's public error."""

    if state == RECOVERY_OR_SYNC_BLOCKED and capability.command_key.startswith(
        "snapshot."
    ):
        return OSError(
            "SNAPSHOT_STORE_NOT_READY: Snapshot export requires a fully ready "
            "store with no pending recovery."
        )
    return refuse_authority_state(state)


def _forward_note(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_note(
            store,
            owner_state,
            arguments.text,
            arguments.link,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_objective(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_objective(
            store,
            owner_state,
            arguments.text,
            arguments.quarter,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_task_note(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_task_note(
            store,
            owner_state,
            arguments.id,
            arguments.text,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_task_status(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_task_status(
            store,
            owner_state,
            arguments.id,
            planning_status(arguments.action),
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_subtask_add(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_subtask(
            store,
            owner_state,
            arguments.task,
            arguments.subtask_or_title,
            arguments.priority,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_subtask_status(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_subtask_status(
            store,
            owner_state,
            arguments.task,
            arguments.subtask_or_title,
            planning_status(arguments.operation),
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_key_result(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_key_result(
            store,
            owner_state,
            arguments.objective,
            arguments.text,
            arguments.target,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_checkin(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_checkin(
            store,
            owner_state,
            arguments.time,
            arguments.date,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_worklog_entry(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_worklog_entry(
            store,
            owner_state,
            arguments.task,
            arguments.date,
            arguments.done,
            arguments.next_items,
            arguments.blocker,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_backlog_add(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_backlog_add(
            store,
            owner_state,
            arguments.title,
            arguments.detail,
            arguments.priority,
            arguments.due,
            arguments.tag,
            arguments.objective,
            arguments.parent,
            arguments.depends_on,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_okr_link(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_okr_link(
            store,
            owner_state,
            arguments.objective,
            arguments.task,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


def _forward_okr_progress(arguments, coordinates_reader, request_json) -> OwnerWriter:
    def forward(store: Store, owner_state: str) -> dict[str, object]:
        return cli_writer.forward_okr_progress(
            store,
            owner_state,
            arguments.objective,
            arguments.key_result,
            arguments.value,
            coordinates_reader=coordinates_reader,
            request_json=request_json,
        )

    return forward


_OWNER_WRITERS: dict[str, Callable[..., OwnerWriter]] = {
    "note": _forward_note,
    "okr.add-objective": _forward_objective,
    "okr.add-key-result": _forward_key_result,
    "okr.link": _forward_okr_link,
    "okr.progress": _forward_okr_progress,
    "backlog.add": _forward_backlog_add,
    "backlog.note": _forward_task_note,
    "backlog.start": _forward_task_status,
    "backlog.done": _forward_task_status,
    "backlog.drop": _forward_task_status,
    "backlog.reopen": _forward_task_status,
    "backlog.subtask.add": _forward_subtask_add,
    "backlog.subtask.start": _forward_subtask_status,
    "backlog.subtask.done": _forward_subtask_status,
    "backlog.subtask.drop": _forward_subtask_status,
    "backlog.subtask.reopen": _forward_subtask_status,
    "worklog.checkin": _forward_checkin,
    "worklog.add": _forward_worklog_entry,
}


def owner_writer_keys() -> frozenset[str]:
    return frozenset(_OWNER_WRITERS)


def owner_writer(
    arguments: argparse.Namespace,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
) -> OwnerWriter | None:
    """Return the owner-route writer for an admitted mutation, or None."""

    key = caps.command_key_from_parsed(arguments)
    factory = _OWNER_WRITERS.get(key)
    if factory is None:
        return None
    return factory(arguments, coordinates_reader, request_json)


def _dispatch_owner_route(
    arguments: argparse.Namespace,
    capability: caps.CliCapability,
    store: Store,
    owner_writer_for: Callable[[argparse.Namespace], OwnerWriter | None],
    emit: Emitter,
) -> int:
    writer = owner_writer_for(arguments)
    if writer is not None:
        emit(writer(store, cli_writer.owner_metadata_state(store)))
        return 0
    if cli_reads.owner_read_is_supported(capability):
        raise cli_reads.owner_read_refusal()
    raise cli_reads.owner_read_refusal()


def dispatch_ordinary(
    arguments: argparse.Namespace,
    capability: caps.CliCapability,
    *,
    run_local: LocalRunner,
    owner_writer_for: Callable[[argparse.Namespace], OwnerWriter | None],
    emit: Emitter,
) -> int:
    """Acquire once, run locally or forward, and release the held lease."""

    data_dir = resolve_data_dir(getattr(arguments, "data_dir", None))
    expected = expected_identity(arguments, data_dir)
    authority = acquire_owner_authority(
        data_dir=data_dir,
        expected_workspace_uid=expected,
    )
    if authority.state == EXCLUSIVE_LOCAL_HELD:
        result = authority.run(lambda store: run_local(arguments, store))
        return 0 if result is None else int(result)
    if authority.state == OWNER_ROUTE_REQUIRED:
        if authority.store is None:
            raise refuse_authority_state(OWNER_ROUTE_REQUIRED)
        return _dispatch_owner_route(
            arguments, capability, authority.store, owner_writer_for, emit
        )
    raise _refuse_dispatch(capability, authority.state)
