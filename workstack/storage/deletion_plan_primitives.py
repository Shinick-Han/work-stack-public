"""Shared vocabulary for the pure Task deletion planners.

Deterministic. No filesystem, lock, journal, backup, clock, randomness,
or network. Never mutates caller-provided documents/records. Holds the
refusal identity, the reference-key rosters, the coercion helpers and the
unsafe-reference scanners that both the v3 and the v4 planner share.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_json_bytes


TASK_DISPLAY_RE = re.compile(r"^T-\d{4,}$", re.IGNORECASE)

ALLOWED_TASK_REF_KEYS = frozenset(
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
V3_TASK_LIST_FIELDS = ("dependencies", "references")
V4_TASK_LIST_FIELDS = ("dependency_uids", "reference_uids")
V3_CAPTURE_LIST_FIELDS = ("linked_task_ids", "converted_task_ids", "task_hints")
V4_CAPTURE_UID_FIELDS = ("linked_task_uids", "converted_task_uids")


class TaskDeletionPlanError(ValueError):
    """Stable, content-free refusal at the pure deletion-plan boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_task_display_id(value: object) -> bool:
    return isinstance(value, str) and TASK_DISPLAY_RE.fullmatch(value) is not None


def as_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskDeletionPlanError("malformed_reference_container")
    return dict(value)


def as_sequence(value: object) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TaskDeletionPlanError("malformed_reference_container")
    return list(value)


def require_id_list(value: object) -> tuple[str, ...]:
    items = as_sequence(value)
    normalized: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TaskDeletionPlanError("malformed_reference_container")
        normalized.append(item)
    return tuple(normalized)


def require_int(value: object) -> int:
    if type(value) is not int:
        raise TaskDeletionPlanError("malformed_reference_container")
    return value


def normalize_display(value: str) -> str:
    return value.strip().upper()


def high_water(display_ids: Iterable[str]) -> int:
    water = 0
    for item in display_ids:
        match = re.fullmatch(r"T-(\d+)", normalize_display(item))
        if match:
            water = max(water, int(match.group(1)))
    return water


def sort_ops(operations: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    decorated = []
    for item in operations:
        payload = {str(key): item[key] for key in sorted(item)}
        decorated.append((canonical_json_bytes(payload), payload))
    decorated.sort(key=lambda pair: pair[0])
    return tuple(payload for _key, payload in decorated)


def _holds_display_ids(value: object) -> bool:
    if is_task_display_id(value):
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(is_task_display_id(item) for item in value)
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
    return is_task_display_id(value) and normalize_display(value) in tokens


def contains_exact_token(value: object, tokens: frozenset[str]) -> bool:
    if isinstance(value, str):
        return _string_is_exact_token(value, tokens)
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            if isinstance(key, str) and _string_is_exact_token(key, tokens):
                return True
            if contains_exact_token(value[key], tokens):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_exact_token(item, tokens) for item in value)
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


def scan_unknown_task_refs(value: object) -> None:
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise TaskDeletionPlanError("malformed_reference_container")
            child = value[key]
            if key in _BODY_KEYS and _is_prose_value(child):
                continue
            if (
                key not in ALLOWED_TASK_REF_KEYS
                and key not in _BODY_KEYS
                and _holds_display_ids(child)
            ):
                raise TaskDeletionPlanError("unknown_unsafe_reference")
            scan_unknown_task_refs(child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            scan_unknown_task_refs(child)


def scan_unknown_target_identity(value: object, tokens: frozenset[str]) -> None:
    if not tokens:
        return
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise TaskDeletionPlanError("malformed_reference_container")
            child = value[key]
            if key in _BODY_KEYS and _is_prose_value(child):
                continue
            if key not in ALLOWED_TASK_REF_KEYS and key not in _BODY_KEYS:
                if _direct_holds_token(child, tokens):
                    raise TaskDeletionPlanError("unknown_unsafe_reference")
            scan_unknown_target_identity(child, tokens)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            scan_unknown_target_identity(child, tokens)


def _idempotency_mentions(entry: Mapping[str, Any], tokens: frozenset[str]) -> bool:
    return contains_exact_token(entry, tokens)


def require_display_id(task_id: object) -> str:
    if not is_task_display_id(task_id):
        raise TaskDeletionPlanError("malformed_reference_container")
    return normalize_display(str(task_id))


def require_expected_revision(expected_revision: object) -> int:
    return require_int(expected_revision)


def string_field(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise TaskDeletionPlanError("malformed_reference_container")
    return value


def display_matches(value: object, wanted: str) -> bool:
    return isinstance(value, str) and normalize_display(value) == wanted


def any_display(values: Sequence[str], wanted: str) -> bool:
    for item in values:
        if normalize_display(item) == wanted:
            return True
    return False


def token_set(*parts: str) -> frozenset[str]:
    tokens: list[str] = []
    for item in parts:
        if item:
            tokens.append(item)
    return frozenset(tokens)


def index_tasks(
    tasks: Sequence[Any],
    wanted: str,
    display_key: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    seen_ids: set[str] = set()
    seen_uids: set[str] = set()
    target: dict[str, Any] | None = None
    all_ids: list[str] = []
    for task in tasks:
        record = as_mapping(task)
        display = require_display_id(record.get(display_key))
        uid = string_field(record, "uid")
        if display in seen_ids or uid in seen_uids:
            raise TaskDeletionPlanError("duplicate_identity")
        seen_ids.add(display)
        seen_uids.add(uid)
        all_ids.append(display)
        if display == wanted:
            target = record
    return target, all_ids


def require_target_revision(
    target: dict[str, Any] | None,
    expected: int,
) -> str:
    if target is None:
        raise TaskDeletionPlanError("not_found")
    revision = target.get("revision")
    if type(revision) is not int or revision != expected:
        raise TaskDeletionPlanError("revision_mismatch")
    return string_field(target, "uid")


def rewrite_task_field_op(
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


def idempotency_ops(entries: object, tokens: frozenset[str]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for entry in as_sequence(entries):
        record = as_mapping(entry)
        if _idempotency_mentions(record, tokens):
            operations.append(
                {
                    "key": string_field(record, "key"),
                    "op": "content_free_idempotency",
                }
            )
    return operations
