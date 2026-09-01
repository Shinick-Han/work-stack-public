"""Explicit opt-in v4 Task create and scalar-patch commands."""

from __future__ import annotations

import copy
import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from ..capture import canonical_digest
from .canonical import canonical_json_bytes
from .command_backend_support import (
    V4CommandBackendSupportError,
    commit_command_proposal,
    load_verified_command_baseline,
)
from .idempotency import stage_idempotency_ledger
from .journal import JournalTarget
from .migration_conversion import convert_v3_documents
from .read_repository import V4WorkspaceRepository
from .reader import V4ReadResult
from .records import stage_record_put
from .runtime import RuntimeAuthority
from .streams import stage_stream_appends
from .write_session import recover_write_session


_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_PRIORITIES = {"P0", "P1", "P2", "P3"}
_CREATE_FIELDS = {
    "title", "detail", "priority", "due", "scheduled", "estimate_minutes",
}
_PATCH_FIELDS = _CREATE_FIELDS | {"revision"}
Clock = Callable[[], str]
FaultHook = Callable[[str], None]


class TaskRepositoryError(ValueError):
    """Stable, content-free refusal at the v4 Task command boundary."""

    command_boundary = "task"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _local_date(instant: str) -> str:
    return instant[:10]


def _next_task_id(tasks: list[dict[str, Any]]) -> str:
    largest = 0
    for task in tasks:
        match = re.fullmatch(r"T-(\d+)", str(task.get("id", "")), re.I)
        if match:
            largest = max(largest, int(match.group(1)))
    return f"T-{largest + 1:04d}"


def _next_event_id(events: list[dict[str, Any]]) -> str:
    return f"E-{len(events) + 1:06d}"


def _next_fact_id(facts: list[dict[str, Any]]) -> str:
    return f"PS-{len(facts) + 1:06d}"


def _date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskRepositoryError(f"{field}_invalid")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise TaskRepositoryError(f"{field}_invalid") from error
    if parsed.isoformat() != value:
        raise TaskRepositoryError(f"{field}_invalid")
    return value


def _estimate(value: Any) -> int | None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1440
    ):
        raise TaskRepositoryError("estimate_minutes_invalid")
    return value


def _text(value: Any, field: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise TaskRepositoryError(f"{field}_invalid")
    result = value.strip()
    if required and not result:
        raise TaskRepositoryError(f"{field}_invalid")
    return result


def _canonical_create(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - _CREATE_FIELDS:
        raise TaskRepositoryError("task_create_invalid")
    if "title" not in body:
        raise TaskRepositoryError("title_invalid")
    priority = body.get("priority", "P2")
    if priority not in _PRIORITIES:
        raise TaskRepositoryError("priority_invalid")
    return {
        "title": _text(body["title"], "title", required=True),
        "detail": _text(body.get("detail", ""), "detail", required=False),
        "priority": priority,
        "due": _date(body.get("due"), "due"),
        "scheduled": _date(body.get("scheduled"), "scheduled"),
        "estimate_minutes": _estimate(body.get("estimate_minutes")),
    }


def _canonical_patch(patch: Any) -> tuple[int, dict[str, Any]]:
    if not isinstance(patch, dict) or set(patch) - _PATCH_FIELDS:
        raise TaskRepositoryError("task_patch_invalid")
    revision = patch.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise TaskRepositoryError("revision_invalid")
    changes: dict[str, Any] = {}
    if "title" in patch:
        changes["title"] = _text(patch["title"], "title", required=True)
    if "detail" in patch:
        changes["detail"] = _text(patch["detail"], "detail", required=False)
    if "priority" in patch:
        if patch["priority"] not in _PRIORITIES:
            raise TaskRepositoryError("priority_invalid")
        changes["priority"] = patch["priority"]
    for field in ("due", "scheduled"):
        if field in patch:
            changes[field] = _date(patch[field], field)
    if "estimate_minutes" in patch:
        changes["estimate_minutes"] = _estimate(patch["estimate_minutes"])
    return revision, changes


def _project_task(task: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(task))
    result.pop("status_fact_id", None)
    result.setdefault("scheduled", None)
    result.setdefault("estimate_minutes", None)
    result["context_count"] = 0
    return result


def _idempotency_replay(
    documents: Mapping[str, Mapping[str, Any]], key: str, digest: str, path: str
) -> dict[str, Any] | None:
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise TaskRepositoryError("invalid_idempotency_key")
    for record in documents["activity.json"].get("idempotency", []):
        if record.get("key") != key:
            continue
        matches = (
            record.get("method") == "POST"
            and record.get("path") == path
            and record.get("request_digest") == digest
        )
        if not matches:
            raise TaskRepositoryError("idempotency_conflict")
        body = copy.deepcopy(record["response_body"])
        body.setdefault("meta", {})["replayed"] = True
        return {"status": 200, "body": body}
    return None


class V4TaskRepository:
    """Normalized Task writer; relationship and planning transitions are excluded."""

    def __init__(
        self, authority_root: Path | str, runtime: RuntimeAuthority, *,
        task_note_source_indexes: Mapping[str, int] | None = None,
        clock: Clock | None = None, fault_hook: FaultHook | None = None,
        enable_v4_task_commands: bool = False,
    ) -> None:
        if enable_v4_task_commands is not True:
            raise TaskRepositoryError("v4_task_commands_not_enabled")
        self.authority_root = Path(authority_root).resolve(strict=False)
        self.runtime = runtime
        self.task_note_source_indexes = copy.deepcopy(task_note_source_indexes)
        self.clock = clock or _utc_now
        self.fault_hook = fault_hook

    def _load(self) -> tuple[V4ReadResult, dict[str, Any], dict[str, dict[str, Any]], int]:
        recover_write_session(self.runtime, fault_hook=self.fault_hook)
        try:
            baseline = load_verified_command_baseline(self.authority_root, self.runtime)
        except V4CommandBackendSupportError as error:
            raise TaskRepositoryError(error.code) from error
        ledger = dict(baseline.ledger)
        read = V4WorkspaceRepository(
            self.authority_root, idempotency_ledger=ledger,
            task_note_source_indexes=self.task_note_source_indexes,
            generation=baseline.generation,
        ).read()
        return (
            baseline.physical,
            ledger,
            read.snapshot.to_v3_documents(),
            baseline.generation,
        )

    def state_documents(self) -> dict[str, dict[str, Any]]:
        return self._load()[2]

    def create_task_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/tasks",
    ) -> dict[str, Any]:
        canonical = _canonical_create(body)
        digest = canonical_digest(canonical)
        current, ledger, documents, generation = self._load()
        replay = _idempotency_replay(documents, idempotency_key, digest, path)
        if replay:
            return replay
        now = self.clock()
        tasks = documents["backlog.json"]["tasks"]
        display_id = _next_task_id(tasks)
        task = {
            "id": display_id,
            "uid": str(uuid.uuid5(uuid.UUID(self.runtime.workspace_uid), display_id)),
            **canonical, "status": "open", "tags": [], "objective_ids": [],
            "parent_id": None, "dependencies": [], "subtasks": [], "notes": [],
            "created": _local_date(now), "updated_at": _local_date(now), "revision": 0,
        }
        facts = documents["activity.json"].setdefault("planning_status", [])
        fact = {
            "id": _next_fact_id(facts), "type": "task.planning_status",
            "task_id": display_id, "task_uid": task["uid"], "previous_fact_id": None,
            "prior_revision": None, "new_revision": 0, "prior_status": None,
            "status": "open", "created_at": now, "actor": "local.user",
            "provenance": "api.v1",
        }
        facts.append(fact)
        task["status_fact_id"] = fact["id"]
        tasks.append(task)
        response_body = {"data": _project_task(task), "meta": {"replayed": False}}
        documents["activity.json"].setdefault("idempotency", []).append({
            "key": idempotency_key, "method": "POST", "path": path,
            "request_digest": digest, "response_status": 201,
            "created_at": now, "response_body": copy.deepcopy(response_body),
        })
        self._commit(current, ledger, documents, generation, now, f"task-create-{idempotency_key}", True)
        return {"status": 201, "body": response_body}

    def patch_task(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        expected_revision, changes = _canonical_patch(patch)
        current, ledger, documents, generation = self._load()
        task = next(
            (item for item in documents["backlog.json"]["tasks"] if item["id"].upper() == task_id.upper()),
            None,
        )
        if task is None:
            raise TaskRepositoryError("not_found")
        if task["revision"] != expected_revision:
            raise TaskRepositoryError("revision_conflict")
        if not changes:
            return _project_task(task)
        now = self.clock()
        task.update(changes)
        task["updated_at"] = _local_date(now)
        task["revision"] += 1
        events = documents["activity.json"].setdefault("activity", [])
        events.append({
            "id": _next_event_id(events), "type": "task.updated",
            "created_at": now, "details": {"fields": sorted(changes)},
            "task_id": task["id"],
        })
        self._commit(
            current, ledger, documents, generation, now,
            f"task-patch-{task['id']}-r{task['revision']}", False,
        )
        return _project_task(task)

    def _commit(
        self, current: V4ReadResult, ledger: Mapping[str, Any],
        documents: dict[str, dict[str, Any]], generation: int, now: str,
        operation_id: str, ledger_changed: bool,
    ) -> None:
        conversion = convert_v3_documents(documents, candidate_created_at=now)
        targets = self._task_targets(current, conversion)
        targets.extend(self._stream_targets(current, conversion))
        if ledger_changed:
            targets.append(stage_idempotency_ledger(
                conversion.idempotency_ledger,
                current_body=canonical_json_bytes(dict(ledger)),
            ))
        commit_command_proposal(
            self.authority_root, self.runtime, targets, generation=generation + 1,
            operation_id=operation_id, created_at=now, fault_hook=self.fault_hook,
            proposal_prefix="task-proposal-",
        )

    @staticmethod
    def _task_targets(current, conversion) -> list[JournalTarget]:
        existing = {str(item["uid"]): item for item in current.records["tasks"]}
        digests = {item.artifact: item.sha256 for item in current.artifacts}
        targets = []
        for source in conversion.records["tasks"]:
            proposed = dict(source)
            prior = existing.get(str(proposed["uid"]))
            if prior is not None and dict(prior) == proposed:
                continue
            artifact = f"records/tasks/{proposed['uid'][:2]}/{proposed['uid']}.json"
            staged = stage_record_put(
                "tasks", proposed, current=prior,
                expected_revision=None if prior is None else int(prior["revision"]),
                expected_digest=None if prior is None else digests[artifact],
            )
            targets.append(JournalTarget.replace(
                staged.artifact, staged.body or b"", expected_digest=staged.expected_digest
            ))
        return targets

    @staticmethod
    def _stream_targets(current, conversion) -> list[JournalTarget]:
        additions = []
        for kind in ("planning-status", "activity"):
            start = len(current.streams[kind])
            for source in conversion.streams[kind][start:]:
                event = {key: copy.deepcopy(value) for key, value in source.items()
                         if key not in {"sequence", "previous_event_digest", "event_digest"}}
                additions.append((kind, event))
        digests = {
            item.artifact: item.sha256 for item in current.artifacts
            if item.category == "stream"
        }
        return [JournalTarget.replace(
            staged.artifact, staged.body, expected_digest=staged.expected_digest
        ) for staged in stage_stream_appends(
            current.streams, additions, current_artifact_digests=digests
        )]
