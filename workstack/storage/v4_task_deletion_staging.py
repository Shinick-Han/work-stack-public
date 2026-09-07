"""Pure v4 Task deletion staging for a later write-session transaction.

Filesystem-free. Consumes an accepted v4 plan, captured artifact digests, and
canonical ledger bytes. Returns JournalTarget CAS rows. Does not backup, lease,
activate an adapter, or invent a display-ID high-water field.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Callable, Mapping, Sequence

from .canonical import CanonicalJsonError, canonical_json_bytes
from .idempotency import IdempotencyLedgerError, parse_idempotency_ledger, stage_idempotency_ledger
from .journal import JournalTarget
from .layout import RECORD_KINDS, STREAM_KINDS
from .records import V4RecordStagingError, stage_record_delete, stage_record_put
from .task_deletion_plan import TaskDeletionPlan, TaskDeletionPlanError, plan_v4_task_deletion
from .v4_stream_rewrite import (
    V4StreamDeletion,
    V4StreamRewriteError,
    rewrite_v4_deletion_streams,
)


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTENT_FREE_BODY = {"data": {"purged": True}}
_TASK_FIELDS = frozenset({"parent_uid", "dependency_uids", "reference_uids"})
_CAPTURE_FIELDS = frozenset({"linked_task_uids", "converted_task_uids", "task_hints"})
_OP_FIELDS: dict[str, frozenset[str]] = {
    "remove_task_record": frozenset({"display_id", "op", "uid"}),
    "remove_task_owned_note": frozenset({"note_uid", "op", "source"}),
    "rewrite_task_field": frozenset({"field", "op", "remove", "subject_display_id"}),
    "rewrite_note_links": frozenset({"note_id", "op", "remove"}),
    "remove_reply": frozenset({"op", "reply_id", "reply_uid"}),
    "unlink_capture_field": frozenset({"capture_id", "field", "op", "remove"}),
    "unlink_capture_action": frozenset({"action_id", "capture_id", "op"}),
    "remove_activity_event": frozenset({"event_id", "op"}),
    "remove_planning_event": frozenset({"fact_id", "op"}),
    "remove_worklog_entry": frozenset({"event_uid", "op", "work_date"}),
    "remove_work_session": frozenset({"event_uid", "op", "session_id", "work_date"}),
    "content_free_idempotency": frozenset({"key", "op"}),
}


class V4DeletionStagingError(ValueError):
    """Stable, content-free refusal before any usable target roster."""

    def __init__(self, code: str = "invalid_plan") -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str = "invalid_plan") -> None:
    raise V4DeletionStagingError(code)


def _require(condition: bool, code: str = "invalid_plan") -> None:
    if not condition:
        _fail(code)


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _text(value: object) -> str:
    _require(isinstance(value, str) and bool(value))
    return value


def _record_artifact(kind: str, uid: str) -> str:
    return f"records/{kind}/{uid[:2]}/{uid}.json"


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


class _Staging:
    def __init__(
        self,
        physical: Mapping[str, Any],
        plan: TaskDeletionPlan,
        artifact_digests: Mapping[str, str],
        ledger_bytes: bytes,
    ) -> None:
        self.plan = plan
        self.digests = dict(artifact_digests)
        self.ledger_bytes = ledger_bytes
        self.working: dict[tuple[str, str], dict[str, Any]] = {}
        self.originals: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_display: dict[str, dict[str, tuple[str, str]]] = {
            "tasks": {},
            "notes": {},
            "captures": {},
            "replies": {},
        }
        self.deleted: set[tuple[str, str]] = set()
        self.dirty: set[tuple[str, str]] = set()
        self.activity_ids: list[str] = []
        self.planning_ids: list[str] = []
        self.worklog_entries: list[tuple[str, str]] = []
        self.worklog_sessions: list[tuple[str, str, str]] = []
        self.ledger_keys: list[str] = []
        _fill_records(self, physical)
        self.streams = {
            kind: [copy.deepcopy(dict(item)) for item in _sequence(physical.get("streams", {}), kind)]
            for kind in STREAM_KINDS
        }
        self.ledger = _copy_mapping(_require_mapping(physical.get("idempotency_ledger")))


def _require_mapping(value: object) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping))
    return value  # type: ignore[return-value]


def _sequence(container: object, key: str) -> list[Any]:
    mapping = _require_mapping(container)
    raw = mapping.get(key, ())
    _require(isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)))
    return list(raw)


def _index_one(_staging: _Staging, kind: str, record: Mapping[str, Any]) -> None:
    _require(kind in RECORD_KINDS)
    payload = _copy_mapping(record)
    uid = _text(payload.get("uid"))
    key = (kind, uid)
    _require(key not in _staging.working)
    _staging.working[key] = payload
    _staging.originals[key] = copy.deepcopy(payload)
    display = payload.get("display_id")
    if kind in _staging.by_display and isinstance(display, str) and display:
        table = _staging.by_display[kind]
        _require(display not in table)
        table[display] = key


def _fill_records(staging: _Staging, physical: Mapping[str, Any]) -> None:
    records = _require_mapping(physical.get("records"))
    extra = set(records) - set(RECORD_KINDS)
    _require(not extra)
    for kind in RECORD_KINDS:
        for item in _sequence(records, kind):
            _require(isinstance(item, Mapping))
            _index_one(staging, kind, item)


def _verify_plan(
    physical: Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int,
    plan: TaskDeletionPlan,
) -> TaskDeletionPlan:
    _require(isinstance(plan, TaskDeletionPlan))
    try:
        recomputed = plan_v4_task_deletion(
            physical, task_id=task_id, expected_revision=expected_revision
        )
    except TaskDeletionPlanError as error:
        raise V4DeletionStagingError("invalid_plan") from error
    _require(plan.layout == "v4")
    _require(recomputed.layout == "v4")
    _require(plan.target_display_id == recomputed.target_display_id)
    _require(plan.target_uid == recomputed.target_uid)
    _require(plan.expected_revision == recomputed.expected_revision)
    _require(plan.canonical_bytes() == recomputed.canonical_bytes())
    _require(plan.target_display_id == task_id)
    _require(plan.expected_revision == expected_revision)
    return recomputed


def _op_name(operation: Mapping[str, Any]) -> str:
    _require(isinstance(operation, Mapping))
    name = operation.get("op")
    _require(isinstance(name, str) and name in _OP_FIELDS)
    _require(set(operation) == _OP_FIELDS[name])
    return name


def _lookup(staging: _Staging, kind: str, display: str) -> tuple[str, str]:
    table = staging.by_display[kind]
    _require(display in table)
    return table[display]


def _working(staging: _Staging, key: tuple[str, str]) -> dict[str, Any]:
    _require(key in staging.working)
    return staging.working[key]


def _delete(staging: _Staging, key: tuple[str, str]) -> None:
    _require(key in staging.working)
    _require(key not in staging.deleted)
    staging.deleted.add(key)


def _remove_parent(record: dict[str, Any], remove: str) -> None:
    _require(record.get("parent_uid") == remove)
    record["parent_uid"] = None


def _remove_list_item(record: dict[str, Any], field: str, remove: str) -> None:
    values = record.get(field)
    _require(isinstance(values, list))
    _require(remove in values)
    record[field] = [item for item in values if item != remove]


def _display_token_matches(value: object, wanted: str) -> bool:
    return isinstance(value, str) and value.strip().upper() == wanted


def _remove_display_tokens(record: dict[str, Any], field: str, wanted: str) -> None:
    values = record.get(field)
    _require(isinstance(values, list))
    kept: list[Any] = []
    matched = 0
    for item in values:
        if _display_token_matches(item, wanted):
            matched += 1
            continue
        kept.append(item)
    _require(matched >= 1)
    record[field] = kept


def _apply_remove_task_record(operation: Mapping[str, Any], staging: _Staging) -> None:
    display = _text(operation.get("display_id"))
    uid = _text(operation.get("uid"))
    _require(display == staging.plan.target_display_id)
    _require(uid == staging.plan.target_uid)
    key = ("tasks", uid)
    _require(key in staging.working)
    original = staging.originals[key]
    _require(original.get("revision") == staging.plan.expected_revision)
    _delete(staging, key)


def _apply_remove_task_owned_note(operation: Mapping[str, Any], staging: _Staging) -> None:
    _require(operation.get("source") == "records.notes")
    uid = _text(operation.get("note_uid"))
    key = ("notes", uid)
    _require(key in staging.working)
    record = staging.working[key]
    _require(record.get("note_kind") == "task_annotation")
    _require(record.get("task_uid") == staging.plan.target_uid)
    _delete(staging, key)


def _apply_rewrite_task_field(operation: Mapping[str, Any], staging: _Staging) -> None:
    field = _text(operation.get("field"))
    _require(field in _TASK_FIELDS)
    remove = _text(operation.get("remove"))
    _require(remove == staging.plan.target_uid)
    display = _text(operation.get("subject_display_id"))
    _require(display != staging.plan.target_display_id)
    key = _lookup(staging, "tasks", display)
    record = _working(staging, key)
    if field == "parent_uid":
        _remove_parent(record, remove)
    else:
        _remove_list_item(record, field, remove)
    staging.dirty.add(key)


def _apply_rewrite_note_links(operation: Mapping[str, Any], staging: _Staging) -> None:
    display = _text(operation.get("note_id"))
    remove = _text(operation.get("remove"))
    _require(remove == staging.plan.target_display_id)
    key = _lookup(staging, "notes", display)
    record = _working(staging, key)
    _remove_display_tokens(record, "links", remove)
    staging.dirty.add(key)


def _apply_remove_reply(operation: Mapping[str, Any], staging: _Staging) -> None:
    reply_id = _text(operation.get("reply_id"))
    reply_uid = _text(operation.get("reply_uid"))
    key = _lookup(staging, "replies", reply_id)
    _require(key == ("replies", reply_uid))
    record = staging.working[key]
    _require(record.get("task_uid") == staging.plan.target_uid)
    _delete(staging, key)


def _apply_unlink_capture_field(operation: Mapping[str, Any], staging: _Staging) -> None:
    field = _text(operation.get("field"))
    _require(field in _CAPTURE_FIELDS)
    capture_id = _text(operation.get("capture_id"))
    remove = _text(operation.get("remove"))
    key = _lookup(staging, "captures", capture_id)
    record = _working(staging, key)
    if field == "task_hints":
        _require(remove == staging.plan.target_display_id)
        _remove_display_tokens(record, field, remove)
    else:
        _require(remove == staging.plan.target_uid)
        _remove_list_item(record, field, remove)
    staging.dirty.add(key)


def _action_display_allowed(display: object, wanted: str) -> bool:
    if display is None:
        return True
    if not isinstance(display, str):
        return False
    return display.strip().upper() == wanted


def _apply_unlink_capture_action(operation: Mapping[str, Any], staging: _Staging) -> None:
    capture_id = _text(operation.get("capture_id"))
    action_id = _text(operation.get("action_id"))
    key = _lookup(staging, "captures", capture_id)
    record = _working(staging, key)
    normalized = record.get("normalized")
    _require(isinstance(normalized, dict))
    items = normalized.get("action_items")
    _require(isinstance(items, list))
    matched = 0
    for item in items:
        _require(isinstance(item, dict))
        if item.get("id") != action_id:
            continue
        _require(item.get("task_uid") == staging.plan.target_uid)
        _require(_action_display_allowed(item.get("task_display_id"), staging.plan.target_display_id))
        item["task_uid"] = None
        item["task_display_id"] = None
        matched += 1
    _require(matched == 1)
    staging.dirty.add(key)


def _apply_remove_activity_event(operation: Mapping[str, Any], staging: _Staging) -> None:
    staging.activity_ids.append(_text(operation.get("event_id")))


def _apply_remove_planning_event(operation: Mapping[str, Any], staging: _Staging) -> None:
    staging.planning_ids.append(_text(operation.get("fact_id")))


def _apply_remove_worklog_entry(operation: Mapping[str, Any], staging: _Staging) -> None:
    staging.worklog_entries.append(
        (_text(operation.get("event_uid")), _text(operation.get("work_date")))
    )


def _apply_remove_work_session(operation: Mapping[str, Any], staging: _Staging) -> None:
    staging.worklog_sessions.append(
        (
            _text(operation.get("event_uid")),
            _text(operation.get("work_date")),
            _text(operation.get("session_id")),
        )
    )


def _apply_content_free_idempotency(operation: Mapping[str, Any], staging: _Staging) -> None:
    staging.ledger_keys.append(_text(operation.get("key")))


_HANDLERS: dict[str, Callable[[Mapping[str, Any], _Staging], None]] = {
    "remove_task_record": _apply_remove_task_record,
    "remove_task_owned_note": _apply_remove_task_owned_note,
    "rewrite_task_field": _apply_rewrite_task_field,
    "rewrite_note_links": _apply_rewrite_note_links,
    "remove_reply": _apply_remove_reply,
    "unlink_capture_field": _apply_unlink_capture_field,
    "unlink_capture_action": _apply_unlink_capture_action,
    "remove_activity_event": _apply_remove_activity_event,
    "remove_planning_event": _apply_remove_planning_event,
    "remove_worklog_entry": _apply_remove_worklog_entry,
    "remove_work_session": _apply_remove_work_session,
    "content_free_idempotency": _apply_content_free_idempotency,
}


def _consume_operations(staging: _Staging) -> None:
    seen: set[bytes] = set()
    for operation in staging.plan.operations:
        payload = {str(key): operation[key] for key in operation}
        encoded = canonical_json_bytes(payload)
        _require(encoded not in seen)
        seen.add(encoded)
        name = _op_name(payload)
        _HANDLERS[name](payload, staging)
    _require(len(seen) == len(staging.plan.operations))


def _expected_digest(staging: _Staging, artifact: str, body: bytes) -> str:
    supplied = staging.digests.get(artifact)
    _require(isinstance(supplied, str) and _SHA256.fullmatch(supplied) is not None, "stale_digest")
    actual = _sha256(body)
    _require(supplied == actual, "stale_digest")
    return supplied


def _journal_delete(staging: _Staging, key: tuple[str, str]) -> JournalTarget:
    kind, uid = key
    original = staging.originals[key]
    artifact = _record_artifact(kind, uid)
    try:
        body = canonical_json_bytes(original)
        digest = _expected_digest(staging, artifact, body)
        staged = stage_record_delete(
            kind,
            original,
            expected_revision=int(original["revision"]),
            expected_digest=digest,
        )
    except (V4RecordStagingError, CanonicalJsonError, TypeError, ValueError, KeyError) as error:
        raise V4DeletionStagingError("invalid_plan") from error
    _require(staged.body is None)
    _require(staged.expected_digest is not None)
    return JournalTarget.delete(
        staged.artifact, expected_digest=staged.expected_digest, scope="authority"
    )


def _same_bytes(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return canonical_json_bytes(dict(left)) == canonical_json_bytes(dict(right))


def _journal_replace(staging: _Staging, key: tuple[str, str]) -> JournalTarget | None:
    kind, _uid = key
    original = staging.originals[key]
    proposed = copy.deepcopy(staging.working[key])
    if _same_bytes(proposed, original):
        return None
    revision = original.get("revision")
    _require(type(revision) is int)
    proposed["revision"] = revision + 1
    artifact = _record_artifact(kind, original["uid"])
    try:
        body = canonical_json_bytes(original)
        digest = _expected_digest(staging, artifact, body)
        staged = stage_record_put(
            kind,
            proposed,
            current=original,
            expected_revision=revision,
            expected_digest=digest,
        )
    except (V4RecordStagingError, CanonicalJsonError, TypeError, ValueError, KeyError) as error:
        raise V4DeletionStagingError("invalid_plan") from error
    _require(staged.body is not None)
    return JournalTarget.replace(
        staged.artifact,
        staged.body,
        expected_digest=staged.expected_digest,
        scope="authority",
    )


def _record_targets(staging: _Staging) -> list[JournalTarget]:
    targets: list[JournalTarget] = []
    for key in sorted(staging.deleted):
        targets.append(_journal_delete(staging, key))
    rewritten = staging.dirty - staging.deleted
    for key in sorted(rewritten):
        target = _journal_replace(staging, key)
        if target is not None:
            targets.append(target)
    return targets


def _segment_digests(staging: _Staging) -> dict[str, str]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for kind in STREAM_KINDS:
        for event in staging.streams[kind]:
            created = event.get("created_at")
            _require(isinstance(created, str) and len(created) >= 7)
            artifact = f"streams/{kind}/{created[:7]}.ndjson"
            grouped.setdefault(artifact, []).append(event)
    digests: dict[str, str] = {}
    for artifact, events in grouped.items():
        ordered = sorted(events, key=lambda item: int(item["sequence"]))
        body = b"\n".join(canonical_json_bytes(dict(event)) for event in ordered) + b"\n"
        digests[artifact] = _expected_digest(staging, artifact, body)
    return digests


def _stream_targets(staging: _Staging) -> tuple[JournalTarget, ...]:
    deletion = V4StreamDeletion(
        activity_ids=tuple(staging.activity_ids),
        planning_ids=tuple(staging.planning_ids),
        worklog_entries=tuple(staging.worklog_entries),
        worklog_sessions=tuple(staging.worklog_sessions),
    )
    try:
        return rewrite_v4_deletion_streams(
            staging.streams,
            deletion,
            segment_digests=_segment_digests(staging),
        )
    except V4StreamRewriteError as error:
        raise V4DeletionStagingError("invalid_stream") from error


def _rewrite_ledger_record(record: Mapping[str, Any]) -> dict[str, Any]:
    rewritten = _copy_mapping(record)
    rewritten.pop("response_ref", None)
    rewritten.pop("response_meta", None)
    rewritten["response_body"] = copy.deepcopy(_CONTENT_FREE_BODY)
    return rewritten


def _apply_ledger_keys(staging: _Staging) -> dict[str, Any]:
    ledger = copy.deepcopy(staging.ledger)
    records = ledger.get("records")
    _require(isinstance(records, list))
    remaining = list(staging.ledger_keys)
    rewritten: list[dict[str, Any]] = []
    for record in records:
        _require(isinstance(record, Mapping))
        payload = _copy_mapping(record)
        key = payload.get("key")
        if key in remaining:
            payload = _rewrite_ledger_record(payload)
            remaining.remove(key)
        rewritten.append(payload)
    _require(not remaining)
    ledger["records"] = rewritten
    return ledger


def _ledger_target(staging: _Staging) -> JournalTarget | None:
    try:
        parsed = parse_idempotency_ledger(staging.ledger_bytes)
        canonical = canonical_json_bytes(dict(staging.ledger))
        _require(canonical == staging.ledger_bytes, "invalid_ledger")
        _require(canonical_json_bytes(parsed) == staging.ledger_bytes, "invalid_ledger")
        rewritten = _apply_ledger_keys(staging)
        body = canonical_json_bytes(rewritten)
        if body == staging.ledger_bytes:
            return None
        expected = _expected_digest(staging, "idempotency-ledger.v1.json", staging.ledger_bytes)
        target = stage_idempotency_ledger(rewritten, current_body=staging.ledger_bytes)
    except (
        IdempotencyLedgerError,
        CanonicalJsonError,
        V4DeletionStagingError,
        TypeError,
        ValueError,
    ) as error:
        if isinstance(error, V4DeletionStagingError):
            raise
        raise V4DeletionStagingError("invalid_ledger") from error
    _require(target.expected_digest == expected, "stale_digest")
    _require(target.scope == "runtime")
    return target


def _sorted_targets(targets: Sequence[JournalTarget]) -> tuple[JournalTarget, ...]:
    items = list(targets)
    artifacts = [(item.scope, item.artifact) for item in items]
    _require(len(artifacts) == len(set(artifacts)))
    items.sort(key=lambda item: (item.scope, item.artifact, item.action))
    return tuple(items)


def stage_v4_task_deletion(
    physical: Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int,
    plan: TaskDeletionPlan,
    artifact_digests: Mapping[str, str],
    ledger_bytes: bytes,
) -> tuple[JournalTarget, ...]:
    """Stage CAS targets for one v4 Task deletion without touching storage."""

    _require(isinstance(physical, Mapping))
    _require(isinstance(artifact_digests, Mapping))
    _require(isinstance(ledger_bytes, bytes))
    snapshot = copy.deepcopy(dict(physical))
    _verify_plan(
        snapshot,
        task_id=task_id,
        expected_revision=expected_revision,
        plan=plan,
    )
    staging = _Staging(snapshot, plan, artifact_digests, ledger_bytes)
    _consume_operations(staging)
    targets = _record_targets(staging)
    targets.extend(_stream_targets(staging))
    ledger = _ledger_target(staging)
    if ledger is not None:
        targets.append(ledger)
    return _sorted_targets(targets)
