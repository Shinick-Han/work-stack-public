"""Pure Task deletion inventory / purge plan.

Deterministic. No filesystem, lock, journal, backup, clock, randomness,
or network. Never mutates caller-provided documents/records.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_json_bytes


PLAN_SCHEMA = "workstack.task-deletion-plan.v1"
PREVIEW_SCHEMA = "workstack.task-deletion-preview.v1"
TASK_DISPLAY_RE = re.compile(r"^T-\d{4,}$", re.IGNORECASE)

_ALLOWED_TASK_REF_KEYS = frozenset(
    {
        "id",
        "uid",
        "display_id",
        "parent_id",
        "parent_uid",
        "dependencies",
        "dependency_uids",
        "references",
        "reference_uids",
        "task_id",
        "task_uid",
        "task_display_id",
        "linked_task_ids",
        "linked_task_uids",
        "converted_task_ids",
        "converted_task_uids",
        "task_hints",
        "links",
        "record_uid",
        "event_uid",
        "capture_uid",
        "reply_uid",
        "note_uid",
        "workspace_uid",
        "previous_event_uid",
    }
)
KNOWN_NON_REFERENCE_BODY_KEYS = frozenset(
    {
        "title",
        "detail",
        "text",
        "body",
        "summary",
        "context",
        "task",
        "task_title",
        "done",
        "next",
        "blockers",
        "name",
        "objective",
        "display_title",
    }
)
_BODY_KEYS = KNOWN_NON_REFERENCE_BODY_KEYS
_V3_TASK_LIST_FIELDS = ("dependencies", "references")
_V4_TASK_LIST_FIELDS = ("dependency_uids", "reference_uids")
_V3_CAPTURE_LIST_FIELDS = ("linked_task_ids", "converted_task_ids", "task_hints")
_V4_CAPTURE_UID_FIELDS = ("linked_task_uids", "converted_task_uids")


class TaskDeletionPlanError(ValueError):
    """Stable, content-free refusal at the pure deletion-plan boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_task_display_id(value: object) -> bool:
    return isinstance(value, str) and TASK_DISPLAY_RE.fullmatch(value) is not None


def _as_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskDeletionPlanError("malformed_reference_container")
    return dict(value)


def _as_sequence(value: object) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TaskDeletionPlanError("malformed_reference_container")
    return list(value)


def _require_id_list(value: object) -> tuple[str, ...]:
    items = _as_sequence(value)
    normalized: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TaskDeletionPlanError("malformed_reference_container")
        normalized.append(item)
    return tuple(normalized)


def _require_int(value: object) -> int:
    if type(value) is not int:
        raise TaskDeletionPlanError("malformed_reference_container")
    return value


def _normalize_display(value: str) -> str:
    return value.strip().upper()


def _high_water(display_ids: Iterable[str]) -> int:
    water = 0
    for item in display_ids:
        match = re.fullmatch(r"T-(\d+)", _normalize_display(item))
        if match:
            water = max(water, int(match.group(1)))
    return water


def _sort_ops(operations: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    decorated = []
    for item in operations:
        payload = {str(key): item[key] for key in sorted(item)}
        decorated.append((canonical_json_bytes(payload), payload))
    decorated.sort(key=lambda pair: pair[0])
    return tuple(payload for _key, payload in decorated)


def _holds_display_ids(value: object) -> bool:
    if _is_task_display_id(value):
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_is_task_display_id(item) for item in value)
    return False


def _is_prose_value(value: object) -> bool:
    if isinstance(value, str) or value is None:
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return all(isinstance(item, str) for item in value)
    return False


def _string_is_exact_token(value: str, tokens: frozenset[str]) -> bool:
    if value in tokens:
        return True
    return _is_task_display_id(value) and _normalize_display(value) in tokens


def _contains_exact_token(value: object, tokens: frozenset[str]) -> bool:
    if isinstance(value, str):
        return _string_is_exact_token(value, tokens)
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            if isinstance(key, str) and _string_is_exact_token(key, tokens):
                return True
            if _contains_exact_token(value[key], tokens):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_exact_token(item, tokens) for item in value)
    return False


def _direct_holds_token(value: object, tokens: frozenset[str]) -> bool:
    if isinstance(value, str):
        return _string_is_exact_token(value, tokens)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(
            _string_is_exact_token(item, tokens)
            for item in value
            if isinstance(item, str)
        )
    return False


def _scan_unknown_task_refs(value: object) -> None:
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise TaskDeletionPlanError("malformed_reference_container")
            child = value[key]
            if key in _BODY_KEYS and _is_prose_value(child):
                continue
            if (
                key not in _ALLOWED_TASK_REF_KEYS
                and key not in _BODY_KEYS
                and _holds_display_ids(child)
            ):
                raise TaskDeletionPlanError("unknown_unsafe_reference")
            _scan_unknown_task_refs(child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _scan_unknown_task_refs(child)


def _scan_unknown_target_identity(value: object, tokens: frozenset[str]) -> None:
    if not tokens:
        return
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise TaskDeletionPlanError("malformed_reference_container")
            child = value[key]
            if key in _BODY_KEYS and _is_prose_value(child):
                continue
            if key not in _ALLOWED_TASK_REF_KEYS and key not in _BODY_KEYS:
                if _direct_holds_token(child, tokens):
                    raise TaskDeletionPlanError("unknown_unsafe_reference")
            _scan_unknown_target_identity(child, tokens)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _scan_unknown_target_identity(child, tokens)


def _idempotency_mentions(entry: Mapping[str, Any], tokens: frozenset[str]) -> bool:
    return _contains_exact_token(entry, tokens)


def _require_display_id(task_id: object) -> str:
    if not _is_task_display_id(task_id):
        raise TaskDeletionPlanError("malformed_reference_container")
    return _normalize_display(str(task_id))


def _require_expected_revision(expected_revision: object) -> int:
    return _require_int(expected_revision)


def _string_field(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise TaskDeletionPlanError("malformed_reference_container")
    return value


@dataclass(frozen=True)
class TaskDeletionPlan:
    """Content-minimal immutable deletion inventory."""

    layout: str
    target_display_id: str
    target_uid: str
    expected_revision: int
    display_id_high_water: int
    operations: tuple[Mapping[str, Any], ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "display_id_high_water": self.display_id_high_water,
            "expected_revision": self.expected_revision,
            "layout": self.layout,
            "operations": [dict(item) for item in self.operations],
            "schema": PLAN_SCHEMA,
            "target_display_id": self.target_display_id,
            "target_uid": self.target_uid,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def preview(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rewritten_tasks: set[str] = set()
        for item in self.operations:
            op = str(item["op"])
            counts[op] = counts.get(op, 0) + 1
            subject = item.get("subject_display_id")
            if op == "rewrite_task_field" and isinstance(subject, str):
                rewritten_tasks.add(subject)
        return {
            "display_id_high_water": self.display_id_high_water,
            "expected_revision": self.expected_revision,
            "layout": self.layout,
            "operation_counts": {key: counts[key] for key in sorted(counts)},
            "rewritten_task_display_ids": sorted(rewritten_tasks),
            "schema": PREVIEW_SCHEMA,
            "target_display_id": self.target_display_id,
            "target_uid": self.target_uid,
        }

    def preview_canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.preview())


def _display_matches(value: object, wanted: str) -> bool:
    return isinstance(value, str) and _normalize_display(value) == wanted


def _any_display(values: Sequence[str], wanted: str) -> bool:
    for item in values:
        if _normalize_display(item) == wanted:
            return True
    return False


def _token_set(*parts: str) -> frozenset[str]:
    tokens: list[str] = []
    for item in parts:
        if item:
            tokens.append(item)
    return frozenset(tokens)


def _index_tasks(
    tasks: Sequence[Any],
    wanted: str,
    display_key: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    seen_ids: set[str] = set()
    seen_uids: set[str] = set()
    target: dict[str, Any] | None = None
    all_ids: list[str] = []
    for task in tasks:
        record = _as_mapping(task)
        display = _require_display_id(record.get(display_key))
        uid = _string_field(record, "uid")
        if display in seen_ids or uid in seen_uids:
            raise TaskDeletionPlanError("duplicate_identity")
        seen_ids.add(display)
        seen_uids.add(uid)
        all_ids.append(display)
        if display == wanted:
            target = record
    return target, all_ids


def _require_target_revision(
    target: dict[str, Any] | None,
    expected: int,
) -> str:
    if target is None:
        raise TaskDeletionPlanError("not_found")
    revision = target.get("revision")
    if type(revision) is not int or revision != expected:
        raise TaskDeletionPlanError("revision_mismatch")
    return _string_field(target, "uid")


def _rewrite_task_field_op(
    *,
    field: str,
    remove: str,
    subject_display_id: str,
) -> dict[str, Any]:
    return {
        "field": field,
        "op": "rewrite_task_field",
        "remove": remove,
        "subject_display_id": subject_display_id,
    }


def _v3_owned_note_ops(target: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for index, _note in enumerate(_as_sequence(target.get("notes", []))):
        operations.append(
            {
                "index": index,
                "op": "remove_task_owned_note",
                "source": "task.notes",
                "task_display_id": wanted,
            }
        )
    return operations


def _v3_require_target_lists(record: Mapping[str, Any]) -> None:
    for field in _V3_TASK_LIST_FIELDS:
        if field in record:
            _require_id_list(record.get(field))


def _v3_task_reference_ops(
    tasks: Sequence[Any],
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for task in tasks:
        record = _as_mapping(task)
        display = _normalize_display(str(record["id"]))
        if display == wanted:
            _v3_require_target_lists(record)
            continue
        parent = record.get("parent_id")
        if parent is not None and not isinstance(parent, str):
            raise TaskDeletionPlanError("malformed_reference_container")
        if _display_matches(parent, wanted):
            operations.append(
                _rewrite_task_field_op(
                    field="parent_id",
                    remove=wanted,
                    subject_display_id=display,
                )
            )
        operations.extend(_v3_inbound_list_ops(record, display, wanted))
    return operations


def _v3_inbound_list_ops(
    record: Mapping[str, Any],
    display: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in _V3_TASK_LIST_FIELDS:
        if field not in record:
            continue
        values = _require_id_list(record.get(field))
        if _any_display(values, wanted):
            operations.append(
                _rewrite_task_field_op(
                    field=field,
                    remove=wanted,
                    subject_display_id=display,
                )
            )
    return operations


def _v3_note_link_ops(snapshot: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    notes_doc = _as_mapping(snapshot.get("notes.json", {}))
    for note in _as_sequence(notes_doc.get("notes", [])):
        record = _as_mapping(note)
        note_id = _string_field(record, "id")
        if "links" not in record:
            continue
        links = _require_id_list(record.get("links"))
        if _any_display(links, wanted):
            operations.append(
                {"note_id": note_id, "op": "rewrite_note_links", "remove": wanted}
            )
    return operations


def _v3_reply_ops(
    snapshot: Mapping[str, Any],
    wanted: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    reply_ids: list[str] = []
    operations: list[dict[str, Any]] = []
    replies_doc = _as_mapping(snapshot.get("replies.json", {}))
    for reply in _as_sequence(replies_doc.get("replies", [])):
        record = _as_mapping(reply)
        reply_id = _string_field(record, "id")
        if _display_matches(record.get("task_id"), wanted):
            reply_ids.append(reply_id)
            operations.append({"op": "remove_reply", "reply_id": reply_id})
    return reply_ids, operations


def _v3_worklog_day_ops(
    day: Mapping[str, Any],
    work_date: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for index, entry in enumerate(_as_sequence(day.get("entries", []))):
        record = _as_mapping(entry)
        if _display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "index": index,
                    "op": "remove_worklog_entry",
                    "work_date": work_date,
                }
            )
    for session in _as_sequence(day.get("sessions", [])):
        record = _as_mapping(session)
        if _display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "op": "remove_work_session",
                    "session_id": _string_field(record, "id"),
                    "work_date": work_date,
                }
            )
    return operations


def _v3_worklog_ops(snapshot: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    worklog_doc = _as_mapping(snapshot.get("worklog.json", {}))
    days = worklog_doc.get("days", {})
    if days is None:
        days = {}
    days_map = _as_mapping(days)
    for work_date in sorted(days_map):
        day = _as_mapping(days_map[work_date])
        operations.extend(_v3_worklog_day_ops(day, work_date, wanted))
    return operations


def _v3_capture_field_ops(
    record: Mapping[str, Any],
    capture_id: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in _V3_CAPTURE_LIST_FIELDS:
        if field not in record:
            continue
        values = _require_id_list(record.get(field))
        if _any_display(values, wanted):
            operations.append(
                {
                    "capture_id": capture_id,
                    "field": field,
                    "op": "unlink_capture_field",
                    "remove": wanted,
                }
            )
    return operations


def _v3_capture_action_ops(
    record: Mapping[str, Any],
    capture_id: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    normalized = record.get("normalized")
    if not isinstance(normalized, Mapping):
        return operations
    for action in _as_sequence(normalized.get("action_items", [])):
        item = _as_mapping(action)
        if _display_matches(item.get("task_id"), wanted):
            operations.append(
                {
                    "action_id": _string_field(item, "id"),
                    "capture_id": capture_id,
                    "op": "unlink_capture_action",
                }
            )
    return operations


def _v3_capture_ops(snapshot: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    captures_doc = _as_mapping(snapshot.get("captures.json", {}))
    for capture in _as_sequence(captures_doc.get("captures", [])):
        record = _as_mapping(capture)
        capture_id = _string_field(record, "id")
        operations.extend(_v3_capture_field_ops(record, capture_id, wanted))
        operations.extend(_v3_capture_action_ops(record, capture_id, wanted))
    return operations


def _v3_activity_ops(
    activity_doc: Mapping[str, Any],
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in _as_sequence(activity_doc.get("activity", [])):
        record = _as_mapping(event)
        if _display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "event_id": _string_field(record, "id"),
                    "op": "remove_activity_event",
                }
            )
    return operations


def _v3_planning_ops(
    activity_doc: Mapping[str, Any],
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for fact in _as_sequence(activity_doc.get("planning_status", [])):
        record = _as_mapping(fact)
        if _display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "fact_id": _string_field(record, "id"),
                    "op": "remove_planning_event",
                }
            )
    return operations


def _idempotency_ops(entries: object, tokens: frozenset[str]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for entry in _as_sequence(entries):
        record = _as_mapping(entry)
        if _idempotency_mentions(record, tokens):
            operations.append(
                {
                    "key": _string_field(record, "key"),
                    "op": "content_free_idempotency",
                }
            )
    return operations


def _finish_plan(
    *,
    layout: str,
    wanted: str,
    uid: str,
    expected: int,
    all_ids: Sequence[str],
    operations: Sequence[Mapping[str, Any]],
) -> TaskDeletionPlan:
    return TaskDeletionPlan(
        layout=layout,
        target_display_id=wanted,
        target_uid=uid,
        expected_revision=expected,
        display_id_high_water=_high_water(all_ids),
        operations=_sort_ops(operations),
    )


def _v4_require_target_lists(record: Mapping[str, Any]) -> None:
    for field in _V4_TASK_LIST_FIELDS:
        if field in record:
            _require_id_list(record.get(field))


def _v4_inbound_uid_list_ops(
    record: Mapping[str, Any],
    display: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in _V4_TASK_LIST_FIELDS:
        if field not in record:
            continue
        values = _require_id_list(record.get(field))
        if uid in values:
            operations.append(
                _rewrite_task_field_op(
                    field=field,
                    remove=uid,
                    subject_display_id=display,
                )
            )
    return operations


def _v4_task_reference_ops(
    tasks: Sequence[Any],
    wanted: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for task in tasks:
        record = _as_mapping(task)
        display = _normalize_display(str(record["display_id"]))
        if display == wanted:
            _v4_require_target_lists(record)
            continue
        parent = record.get("parent_uid")
        if parent is not None and not isinstance(parent, str):
            raise TaskDeletionPlanError("malformed_reference_container")
        if parent == uid:
            operations.append(
                _rewrite_task_field_op(
                    field="parent_uid",
                    remove=uid,
                    subject_display_id=display,
                )
            )
        operations.extend(_v4_inbound_uid_list_ops(record, display, uid))
    return operations


def _v4_note_ops(
    records: Mapping[str, Any],
    wanted: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for note in _as_sequence(records.get("notes", [])):
        record = _as_mapping(note)
        if record.get("note_kind") == "task_annotation" and record.get("task_uid") == uid:
            operations.append(
                {
                    "note_uid": _string_field(record, "uid"),
                    "op": "remove_task_owned_note",
                    "source": "records.notes",
                }
            )
        if "links" not in record:
            continue
        links = _require_id_list(record.get("links"))
        if _any_display(links, wanted):
            operations.append(
                {
                    "note_id": _string_field(record, "display_id"),
                    "op": "rewrite_note_links",
                    "remove": wanted,
                }
            )
    return operations


def _v4_reply_ops(
    records: Mapping[str, Any],
    uid: str,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    reply_ids: list[str] = []
    reply_uids: list[str] = []
    operations: list[dict[str, Any]] = []
    for reply in _as_sequence(records.get("replies", [])):
        record = _as_mapping(reply)
        if record.get("task_uid") == uid:
            reply_id = _string_field(record, "display_id")
            reply_uid = _string_field(record, "uid")
            reply_ids.append(reply_id)
            reply_uids.append(reply_uid)
            operations.append(
                {
                    "op": "remove_reply",
                    "reply_id": reply_id,
                    "reply_uid": reply_uid,
                }
            )
    return reply_ids, reply_uids, operations


def _v4_capture_uid_field_ops(
    record: Mapping[str, Any],
    capture_id: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in _V4_CAPTURE_UID_FIELDS:
        if field not in record:
            continue
        values = _require_id_list(record.get(field))
        if uid in values:
            operations.append(
                {
                    "capture_id": capture_id,
                    "field": field,
                    "op": "unlink_capture_field",
                    "remove": uid,
                }
            )
    return operations


def _v4_capture_hint_ops(
    record: Mapping[str, Any],
    capture_id: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if "task_hints" not in record:
        return operations
    hints = _require_id_list(record.get("task_hints"))
    if _any_display(hints, wanted):
        operations.append(
            {
                "capture_id": capture_id,
                "field": "task_hints",
                "op": "unlink_capture_field",
                "remove": wanted,
            }
        )
    return operations


def _v4_capture_action_ops(
    record: Mapping[str, Any],
    capture_id: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    normalized = record.get("normalized")
    if not isinstance(normalized, Mapping):
        return operations
    for action in _as_sequence(normalized.get("action_items", [])):
        item = _as_mapping(action)
        if item.get("task_uid") == uid:
            operations.append(
                {
                    "action_id": _string_field(item, "id"),
                    "capture_id": capture_id,
                    "op": "unlink_capture_action",
                }
            )
    return operations


def _v4_capture_ops(
    records: Mapping[str, Any],
    wanted: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for capture in _as_sequence(records.get("captures", [])):
        record = _as_mapping(capture)
        capture_id = _string_field(record, "display_id")
        operations.extend(_v4_capture_uid_field_ops(record, capture_id, uid))
        operations.extend(_v4_capture_hint_ops(record, capture_id, wanted))
        operations.extend(_v4_capture_action_ops(record, capture_id, uid))
    return operations


def _v4_activity_ops(streams: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in _as_sequence(streams.get("activity", [])):
        record = _as_mapping(event)
        if record.get("task_uid") == uid:
            event_key = "legacy_event_id" if record.get("legacy_event_id") else "event_uid"
            operations.append(
                {
                    "event_id": _string_field(record, event_key),
                    "op": "remove_activity_event",
                }
            )
    return operations


def _v4_planning_ops(streams: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in _as_sequence(streams.get("planning-status", [])):
        record = _as_mapping(event)
        if record.get("task_uid") == uid:
            fact_key = "legacy_fact_id" if record.get("legacy_fact_id") else "event_uid"
            operations.append(
                {"fact_id": _string_field(record, fact_key), "op": "remove_planning_event"}
            )
    return operations


def _v4_worklog_session_op(record: Mapping[str, Any], event_uid: str) -> dict[str, Any]:
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise TaskDeletionPlanError("malformed_reference_container")
    return {
        "event_uid": event_uid,
        "op": "remove_work_session",
        "session_id": session_id,
        "work_date": record.get("work_date"),
    }


def _v4_worklog_ops(streams: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in _as_sequence(streams.get("worklog", [])):
        record = _as_mapping(event)
        if record.get("task_uid") != uid:
            continue
        kind = record.get("kind")
        event_uid = _string_field(record, "event_uid")
        if kind == "session":
            operations.append(_v4_worklog_session_op(record, event_uid))
        elif kind == "entry":
            operations.append(
                {
                    "event_uid": event_uid,
                    "op": "remove_worklog_entry",
                    "work_date": record.get("work_date"),
                }
            )
    return operations


def _v4_ledger_records(snapshot: Mapping[str, Any]) -> list[Any]:
    ledger = snapshot.get("idempotency_ledger", {})
    if isinstance(ledger, Mapping) and "records" in ledger:
        return _as_sequence(ledger.get("records", []))
    if isinstance(ledger, Sequence) and not isinstance(ledger, (str, bytes, bytearray)):
        return list(ledger)
    return []


def plan_v3_task_deletion(
    documents: Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int,
) -> TaskDeletionPlan:
    snapshot = copy.deepcopy(dict(documents))
    _scan_unknown_task_refs(snapshot)
    wanted = _require_display_id(task_id)
    expected = _require_expected_revision(expected_revision)
    backlog = _as_mapping(snapshot.get("backlog.json", {}))
    tasks = _as_sequence(backlog.get("tasks", []))
    target, all_ids = _index_tasks(tasks, wanted, "id")
    uid = _require_target_revision(target, expected)
    _scan_unknown_target_identity(snapshot, frozenset({wanted, uid}))
    operations: list[dict[str, Any]] = [
        {"display_id": wanted, "op": "remove_task_record", "uid": uid}
    ]
    operations.extend(_v3_owned_note_ops(target, wanted))
    operations.extend(_v3_task_reference_ops(tasks, wanted))
    operations.extend(_v3_note_link_ops(snapshot, wanted))
    reply_ids, reply_ops = _v3_reply_ops(snapshot, wanted)
    operations.extend(reply_ops)
    operations.extend(_v3_worklog_ops(snapshot, wanted))
    operations.extend(_v3_capture_ops(snapshot, wanted))
    activity_doc = _as_mapping(snapshot.get("activity.json", {}))
    operations.extend(_v3_activity_ops(activity_doc, wanted))
    operations.extend(_v3_planning_ops(activity_doc, wanted))
    operations.extend(
        _idempotency_ops(
            activity_doc.get("idempotency", []),
            _token_set(wanted, uid, *reply_ids),
        )
    )
    return _finish_plan(
        layout="v3",
        wanted=wanted,
        uid=uid,
        expected=expected,
        all_ids=all_ids,
        operations=operations,
    )


def plan_v4_task_deletion(
    physical: Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int,
) -> TaskDeletionPlan:
    snapshot = copy.deepcopy(dict(physical))
    _scan_unknown_task_refs(snapshot)
    wanted = _require_display_id(task_id)
    expected = _require_expected_revision(expected_revision)
    records = _as_mapping(snapshot.get("records", {}))
    streams = _as_mapping(snapshot.get("streams", {}))
    tasks = _as_sequence(records.get("tasks", []))
    target, all_ids = _index_tasks(tasks, wanted, "display_id")
    uid = _require_target_revision(target, expected)
    _scan_unknown_target_identity(snapshot, frozenset({wanted, uid}))
    operations: list[dict[str, Any]] = [
        {"display_id": wanted, "op": "remove_task_record", "uid": uid}
    ]
    operations.extend(_v4_task_reference_ops(tasks, wanted, uid))
    operations.extend(_v4_note_ops(records, wanted, uid))
    reply_ids, reply_uids, reply_ops = _v4_reply_ops(records, uid)
    operations.extend(reply_ops)
    operations.extend(_v4_capture_ops(records, wanted, uid))
    operations.extend(_v4_activity_ops(streams, uid))
    operations.extend(_v4_planning_ops(streams, uid))
    operations.extend(_v4_worklog_ops(streams, uid))
    operations.extend(
        _idempotency_ops(
            _v4_ledger_records(snapshot),
            _token_set(wanted, uid, *reply_ids, *reply_uids),
        )
    )
    return _finish_plan(
        layout="v4",
        wanted=wanted,
        uid=uid,
        expected=expected,
        all_ids=all_ids,
        operations=operations,
    )
