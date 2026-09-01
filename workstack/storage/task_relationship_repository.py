"""Backend-neutral Task relationship and logical-deletion mutations.

The released v3 product treats Delete Task as a revision-guarded transition to
``dropped``.  It does not erase the Task or record an idempotency entry.  The
v4 task schema has no tombstone field, so this compatibility layer preserves
that append-only meaning and explicitly refuses physical Task deletion.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import datetime as dt
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Mapping, Protocol
import uuid

from .journal import JournalTarget
from .manifest import build_v4_manifest
from .mutation_repository import (
    V4WritableRepositorySession,
    admit_experimental_v4_mutation_repository,
)
from .reader import V4ReadResult, read_v4
from .records import stage_record_put
from .runtime import RuntimeAuthority
from .streams import stage_stream_appends


Clock = Callable[[], str]
_PATCH_FIELDS = {"revision", "parent_id", "dependencies", "references"}


class TaskRelationshipError(ValueError):
    """Stable, content-free refusal at the relationship command boundary."""

    command_boundary = "relationship"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TaskMutationReceipt:
    task_id: str
    task_uid: str
    revision: int
    status: str
    parent_id: str | None
    dependencies: tuple[str, ...]
    references: tuple[str, ...]
    changed_fields: tuple[str, ...]
    activity_appended: bool
    planning_appended: bool
    idempotency_recorded: bool = False
    replayed: bool = False

    @property
    def logically_deleted(self) -> bool:
        return self.status == "dropped"


class TaskRelationshipRepository(Protocol):
    def patch_relationships(
        self, task_id: str, request: Mapping[str, Any]
    ) -> TaskMutationReceipt: ...


class LegacyTaskStore(Protocol):
    def load(self, name: str) -> dict[str, Any]: ...


class LegacyTaskService(Protocol):
    store: LegacyTaskStore

    def patch_task(
        self, task_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]: ...

    def delete_task(
        self, task_id: str, expected_revision: int
    ) -> TaskMutationReceipt: ...


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TaskRelationshipError("invalid_request")
    return value


def _normalized_id(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise TaskRelationshipError("invalid_request")
    normalized = value.strip().upper()
    if not normalized:
        if nullable:
            return None
        raise TaskRelationshipError("invalid_request")
    return normalized


def _normalized_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TaskRelationshipError("invalid_request")
    return tuple(sorted({item.strip().upper() for item in value if item.strip()}))


def _canonical_patch(request: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    if not isinstance(request, Mapping) or set(request) - _PATCH_FIELDS:
        raise TaskRelationshipError("invalid_request")
    revision = _revision(request.get("revision"))
    changes: dict[str, Any] = {}
    if "parent_id" in request:
        changes["parent_id"] = _normalized_id(request["parent_id"], nullable=True)
    for field in ("dependencies", "references"):
        if field in request:
            changes[field] = _normalized_ids(request[field])
    return revision, changes


def _receipt(
    task: Mapping[str, Any],
    *,
    status: str,
    references: tuple[str, ...],
    changed_fields: tuple[str, ...],
    activity_appended: bool,
    planning_appended: bool,
) -> TaskMutationReceipt:
    return TaskMutationReceipt(
        task_id=str(task["id"]),
        task_uid=str(task["uid"]),
        revision=int(task["revision"]),
        status=status,
        parent_id=task.get("parent_id"),
        dependencies=tuple(task.get("dependencies", ())),
        references=references,
        changed_fields=changed_fields,
        activity_appended=activity_appended,
        planning_appended=planning_appended,
    )


class V3TaskRelationshipAdapter:
    """Characterized v3 behavior exposed through the neutral contract."""

    def __init__(self, stack: LegacyTaskService) -> None:
        self.stack = stack

    def patch_relationships(
        self, task_id: str, request: Mapping[str, Any]
    ) -> TaskMutationReceipt:
        revision, changes = _canonical_patch(request)
        if "references" in changes:
            raise TaskRelationshipError("references_unsupported")
        activity = self.stack.store.load("activity.json")
        activity_count = len(activity["activity"])
        planning_count = len(activity["planning_status"])
        try:
            task = self.stack.patch_task(
                task_id, {"revision": revision, **_v3_changes(changes)}
            )
        except ValueError as error:
            _raise_legacy_error(error)
        after = self.stack.store.load("activity.json")
        changed = _latest_changed_fields(after, activity_count)
        return _receipt(
            task,
            status=str(task["status"]),
            references=(),
            changed_fields=changed,
            activity_appended=len(after["activity"]) > activity_count,
            planning_appended=len(after["planning_status"]) > planning_count,
        )

    def delete_task(
        self, task_id: str, expected_revision: int
    ) -> TaskMutationReceipt:
        revision = _revision(expected_revision)
        activity = self.stack.store.load("activity.json")
        activity_count = len(activity["activity"])
        planning_count = len(activity["planning_status"])
        try:
            task = self.stack.patch_task(
                task_id, {"revision": revision, "status": "dropped"}
            )
        except ValueError as error:
            _raise_legacy_error(error)
        after = self.stack.store.load("activity.json")
        return _receipt(
            task,
            status=str(task["status"]),
            references=(),
            changed_fields=_latest_changed_fields(after, activity_count),
            activity_appended=len(after["activity"]) > activity_count,
            planning_appended=len(after["planning_status"]) > planning_count,
        )

    @staticmethod
    def hard_delete_task(_task_id: str, _expected_revision: int) -> None:
        raise TaskRelationshipError("task_hard_delete_unsupported")


def _v3_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if "parent_id" in changes:
        result["parent_id"] = changes["parent_id"]
    if "dependencies" in changes:
        result["dependencies"] = list(changes["dependencies"])
    return result


def _raise_legacy_error(error: ValueError) -> None:
    code = getattr(error, "code", "invalid_request")
    if code not in {"not_found", "revision_conflict"}:
        code = "invalid_request"
    raise TaskRelationshipError(code) from error


def _latest_changed_fields(
    activity: Mapping[str, Any], previous_count: int
) -> tuple[str, ...]:
    events = activity["activity"]
    if len(events) == previous_count:
        return ()
    return tuple(events[-1]["details"]["fields"])


def _task_index(physical: V4ReadResult) -> dict[str, dict[str, Any]]:
    return {
        str(task["display_id"]).upper(): copy.deepcopy(dict(task))
        for task in physical.records["tasks"]
    }


def _status_head(physical: V4ReadResult, task_uid: str) -> dict[str, Any]:
    matches = [
        dict(event)
        for event in physical.streams["planning-status"]
        if event["task_uid"] == task_uid
    ]
    if not matches:
        raise TaskRelationshipError("planning_status_missing")
    return max(matches, key=lambda event: int(event["sequence"]))


def _relationship_reaches(
    tasks: Mapping[str, Mapping[str, Any]],
    starts: tuple[str, ...],
    target_uid: str,
    field: str,
) -> bool:
    pending = list(starts)
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target_uid:
            return True
        if current in visited:
            continue
        visited.add(current)
        task = tasks.get(current)
        if task is None:
            continue
        value = task.get(field)
        if field == "parent_uid":
            if isinstance(value, str):
                pending.append(value)
        elif isinstance(value, list):
            pending.extend(str(item) for item in value)
    return False


def _display_to_uid(
    tasks: Mapping[str, Mapping[str, Any]], values: tuple[str, ...]
) -> tuple[str, ...]:
    missing = [value for value in values if value not in tasks]
    if missing:
        raise TaskRelationshipError("invalid_request")
    return tuple(str(tasks[value]["uid"]) for value in values)


def _validated_v4_changes(
    task: Mapping[str, Any],
    tasks_by_display: Mapping[str, Mapping[str, Any]],
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    tasks_by_uid = {str(value["uid"]): value for value in tasks_by_display.values()}
    target_uid = str(task["uid"])
    proposed: dict[str, Any] = {}
    if "parent_id" in changes:
        parent_id = changes["parent_id"]
        parent_uids = () if parent_id is None else _display_to_uid(tasks_by_display, (parent_id,))
        parent_uid = parent_uids[0] if parent_uids else None
        if parent_uid == target_uid or (
            parent_uid
            and _relationship_reaches(tasks_by_uid, (parent_uid,), target_uid, "parent_uid")
        ):
            raise TaskRelationshipError("invalid_request")
        proposed["parent_uid"] = parent_uid
    if "dependencies" in changes:
        dependencies = _display_to_uid(tasks_by_display, changes["dependencies"])
        if target_uid in dependencies or any(
            _relationship_reaches(
                tasks_by_uid, (dependency,), target_uid, "dependency_uids"
            )
            for dependency in dependencies
        ):
            raise TaskRelationshipError("invalid_request")
        proposed["dependency_uids"] = list(dependencies)
    if "references" in changes:
        proposed["reference_uids"] = list(
            _display_to_uid(tasks_by_display, changes["references"])
        )
    return proposed


class V4TaskRelationshipRepository:
    """Explicit-opt-in v4 relationship and append-only deletion writer."""

    def __init__(
        self,
        session: V4WritableRepositorySession,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.session = session
        self.clock = clock or _utc_now

    def patch_relationships(
        self, task_id: str, request: Mapping[str, Any]
    ) -> TaskMutationReceipt:
        revision, changes = _canonical_patch(request)
        physical = read_v4(self.session.runtime.authority_root)
        tasks = _task_index(physical)
        task = tasks.get(task_id.strip().upper())
        if task is None:
            raise TaskRelationshipError("not_found")
        _require_revision(task, revision)
        if not changes:
            return self._receipt_from_v4(physical, task, (), False, False)
        v4_changes = _validated_v4_changes(task, tasks, changes)
        changed_fields = tuple(sorted(changes))
        return self._commit_task(
            physical,
            task,
            v4_changes,
            changed_fields,
            status=None,
        )

    def delete_task(
        self, task_id: str, expected_revision: int
    ) -> TaskMutationReceipt:
        revision = _revision(expected_revision)
        physical = read_v4(self.session.runtime.authority_root)
        task = _task_index(physical).get(task_id.strip().upper())
        if task is None:
            raise TaskRelationshipError("not_found")
        _require_revision(task, revision)
        status = str(_status_head(physical, str(task["uid"]))["status"])
        if status == "dropped":
            return self._receipt_from_v4(physical, task, (), False, False)
        return self._commit_task(
            physical, task, {}, ("status",), status="dropped"
        )

    @staticmethod
    def hard_delete_task(_task_id: str, _expected_revision: int) -> None:
        raise TaskRelationshipError("task_hard_delete_unsupported")

    def _commit_task(
        self,
        physical: V4ReadResult,
        task: dict[str, Any],
        changes: Mapping[str, Any],
        changed_fields: tuple[str, ...],
        *,
        status: str | None,
    ) -> TaskMutationReceipt:
        now = self.clock()
        prior_status = str(_status_head(physical, str(task["uid"]))["status"])
        proposed = copy.deepcopy(task)
        proposed.update(changes)
        proposed["revision"] = int(task["revision"]) + 1
        proposed["updated_at"] = now[:10]
        targets = self._targets(
            physical, task, proposed, changed_fields, now, prior_status, status
        )
        manifest = self._proposal_manifest(targets, self.session.generation + 1)
        self.session.commit(
            targets,
            manifest,
            operation_id=f"task-rel-{task['display_id']}-r{proposed['revision']}",
            created_at=now,
        )
        current = read_v4(self.session.runtime.authority_root)
        return self._receipt_from_v4(
            current, proposed, changed_fields, True, status is not None
        )

    def _targets(
        self,
        physical: V4ReadResult,
        current: Mapping[str, Any],
        proposed: Mapping[str, Any],
        changed_fields: tuple[str, ...],
        now: str,
        prior_status: str,
        status: str | None,
    ) -> list[JournalTarget]:
        digests = {artifact.artifact: artifact.sha256 for artifact in physical.artifacts}
        artifact = f"records/tasks/{current['uid'][:2]}/{current['uid']}.json"
        staged_record = stage_record_put(
            "tasks",
            proposed,
            current=current,
            expected_revision=int(current["revision"]),
            expected_digest=digests[artifact],
        )
        targets = [
            JournalTarget.replace(
                staged_record.artifact,
                staged_record.body or b"",
                expected_digest=staged_record.expected_digest,
            )
        ]
        additions = self._events(
            physical, current, proposed, changed_fields, now, prior_status, status
        )
        stream_digests = {
            artifact.artifact: artifact.sha256
            for artifact in physical.artifacts
            if artifact.category == "stream"
        }
        for staged in stage_stream_appends(
            physical.streams,
            additions,
            current_artifact_digests=stream_digests,
        ):
            targets.append(
                JournalTarget.replace(
                    staged.artifact,
                    staged.body,
                    expected_digest=staged.expected_digest,
                )
            )
        return targets

    def _events(
        self,
        physical: V4ReadResult,
        current: Mapping[str, Any],
        proposed: Mapping[str, Any],
        changed_fields: tuple[str, ...],
        now: str,
        prior_status: str,
        status: str | None,
    ) -> list[tuple[str, Mapping[str, Any]]]:
        additions: list[tuple[str, Mapping[str, Any]]] = []
        if status is not None:
            additions.append(
                (
                    "planning-status",
                    _planning_event(
                        physical, current, proposed, now, prior_status, status
                    ),
                )
            )
        additions.append(
            (
                "activity",
                _activity_event(physical, proposed, now, changed_fields),
            )
        )
        return additions

    def _proposal_manifest(
        self, targets: list[JournalTarget], generation: int
    ):
        root = self.session.runtime.authority_root
        parent = self.session.runtime.runtime_root
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="task-rel-proposal-", dir=parent))
        proposal = temporary / "authority"
        try:
            shutil.copytree(root, proposal)
            for target in targets:
                path = proposal / target.artifact
                path.parent.mkdir(parents=True, exist_ok=True)
                if target.action == "delete":
                    path.unlink()
                else:
                    path.write_bytes(target.proposed_bytes or b"")
            return build_v4_manifest(read_v4(proposal), generation=generation)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _receipt_from_v4(
        physical: V4ReadResult,
        task: Mapping[str, Any],
        changed_fields: tuple[str, ...],
        activity_appended: bool,
        planning_appended: bool,
    ) -> TaskMutationReceipt:
        by_uid = {
            str(value["uid"]): str(value["display_id"])
            for value in physical.records["tasks"]
        }
        status = str(_status_head(physical, str(task["uid"]))["status"])
        return TaskMutationReceipt(
            task_id=str(task["display_id"]),
            task_uid=str(task["uid"]),
            revision=int(task["revision"]),
            status=status,
            parent_id=(
                None
                if task["parent_uid"] is None
                else by_uid[str(task["parent_uid"])]
            ),
            dependencies=tuple(by_uid[str(uid)] for uid in task["dependency_uids"]),
            references=tuple(by_uid[str(uid)] for uid in task["reference_uids"]),
            changed_fields=changed_fields,
            activity_appended=activity_appended,
            planning_appended=planning_appended,
        )


def _require_revision(task: Mapping[str, Any], expected: int) -> None:
    if int(task["revision"]) != expected:
        raise TaskRelationshipError("revision_conflict")


def _next_legacy_id(events: tuple[Mapping[str, Any], ...], field: str, prefix: str) -> str:
    largest = max(
        (
            int(str(event[field]).split("-", 1)[1])
            for event in events
            if str(event.get(field, "")).startswith(prefix + "-")
        ),
        default=0,
    )
    return f"{prefix}-{largest + 1:06d}"


def _event_uid(workspace_uid: str, identity: str) -> str:
    return str(uuid.uuid5(uuid.UUID(workspace_uid), identity))


def _planning_event(
    physical: V4ReadResult,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    now: str,
    prior_status: str,
    status: str,
) -> dict[str, Any]:
    events = physical.streams["planning-status"]
    legacy_id = _next_legacy_id(events, "legacy_fact_id", "PS")
    previous = _status_head(physical, str(current["uid"]))
    workspace_uid = str(physical.store["workspace_uid"])
    return {
        "format": "workstack.planning-status-event",
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "event_uid": _event_uid(workspace_uid, f"planning-status:{legacy_id}"),
        "record_uid": current["uid"],
        "created_at": now,
        "actor": "local.user",
        "provenance": "api.v1",
        "legacy_fact_id": legacy_id,
        "task_uid": current["uid"],
        "task_display_id": current["display_id"],
        "previous_event_uid": previous["event_uid"],
        "previous_legacy_fact_id": previous["legacy_fact_id"],
        "prior_revision": current["revision"],
        "new_revision": proposed["revision"],
        "prior_status": prior_status,
        "status": status,
    }


def _activity_event(
    physical: V4ReadResult,
    task: Mapping[str, Any],
    now: str,
    changed_fields: tuple[str, ...],
) -> dict[str, Any]:
    events = physical.streams["activity"]
    legacy_id = _next_legacy_id(events, "legacy_event_id", "E")
    workspace_uid = str(physical.store["workspace_uid"])
    return {
        "format": "workstack.activity-event",
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "event_uid": _event_uid(workspace_uid, f"activity:{legacy_id}"),
        "record_uid": task["uid"],
        "created_at": now,
        "actor": "local.user",
        "provenance": "api.v1",
        "legacy_event_id": legacy_id,
        "event_type": "task.updated",
        "details": {"fields": list(changed_fields)},
        "capture_uid": None,
        "task_uid": task["uid"],
        "reply_uid": None,
    }


def admit_experimental_v4_task_relationship_repository(
    authority_root: Path | str,
    runtime: RuntimeAuthority | None,
    *,
    allow_v4_task_relationships: bool = False,
    clock: Clock | None = None,
) -> V4TaskRelationshipRepository:
    session = admit_experimental_v4_mutation_repository(
        authority_root,
        runtime,
        allow_v4_mutation=allow_v4_task_relationships,
    )
    return V4TaskRelationshipRepository(session, clock=clock)
