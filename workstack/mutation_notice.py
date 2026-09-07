"""Pure mutation-notice contract and revision-guarded Undo builder.

Durable data and eligibility/compensation logic only. This module does not
persist, publish, render, or execute compensation, and it performs no I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

FORMAT = "workstack.mutation-notice"
COMPENSATION_FORMAT = "workstack.mutation-compensation"
SCHEMA_VERSION = 1
SCHEMA_ID = "https://workstack.local/contracts/workstack-mutation-notice-v1.schema.json"
COMMITTED = "committed"
COMMIT_UNKNOWN = "commit_unknown"
TASK_STATUS_OPERATION = "task.status"
PERMANENT_DELETE_OPERATION = "task.permanent_delete"
MAX_SAFE_INTEGER = 9007199254740991
SUMMARY_MAX = 160
ACTOR_MAX = 64
IDEMPOTENCY_MAX = 128
IDEMPOTENCY_MIN = 8

NOTICE_FIELDS = (
    "format",
    "schema_version",
    "notice_id",
    "workspace_uid",
    "mutation_uid",
    "entity_kind",
    "entity_uid",
    "operation",
    "before_revision",
    "after_revision",
    "source",
    "actor",
    "idempotency_key",
    "commit_state",
    "summary",
    "undoable",
    "status_before",
    "status_after",
)
COMPENSATION_FIELDS = (
    "format",
    "schema_version",
    "notice_id",
    "workspace_uid",
    "entity_kind",
    "entity_uid",
    "operation",
    "expected_revision",
    "requested_status",
    "idempotency_key",
)
TASK_STATUSES = frozenset({"open", "started", "done", "dropped"})
SOURCES = frozenset({"cli", "gui", "agent"})
COMMIT_STATES = frozenset({COMMITTED, COMMIT_UNKNOWN})
ENTITY_KINDS = frozenset({
    "task",
    "subtask",
    "objective",
    "key_result",
    "worklog",
    "capture",
    "workspace",
    "profile",
    "storage",
})
OPERATION_ENTITY = {
    "task.status": "task",
    "task.create": "task",
    "task.note": "task",
    "task.permanent_delete": "task",
    "subtask.status": "subtask",
    "subtask.create": "subtask",
    "objective.create": "objective",
    "key_result.create": "key_result",
    "okr.link": "objective",
    "okr.progress": "key_result",
    "worklog.append": "worklog",
    "capture.ingest": "capture",
    "authority.change": "workspace",
    "profile.change": "profile",
    "storage.migration": "storage",
    "storage.backup": "storage",
    "storage.restore": "storage",
}
REVERSIBLE_OPERATIONS = frozenset({TASK_STATUS_OPERATION})
OPERATIONS = frozenset(OPERATION_ENTITY)
OPERATION_SUMMARY = {
    "task.create": "Task created",
    "task.note": "Task note appended",
    "task.permanent_delete": "Permanent deletion is not undoable",
    "subtask.status": "Subtask status changed",
    "subtask.create": "Subtask created",
    "objective.create": "Objective created",
    "key_result.create": "Key result created",
    "okr.link": "Objective linked",
    "okr.progress": "Key-result progress recorded",
    "worklog.append": "Worklog entry appended",
    "capture.ingest": "Capture ingested",
    "authority.change": "Authority change recorded",
    "profile.change": "Profile change recorded",
    "storage.migration": "Storage migration recorded",
    "storage.backup": "Storage backup recorded",
    "storage.restore": "Storage restore recorded",
}

_ERROR_MESSAGE = "invalid mutation notice"
_NOTICE_NS = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://workstack.local/contracts/workstack-mutation-notice-v1",
)
_MUTATION_NS = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://workstack.local/contracts/workstack-mutation-notice-v1/mutation",
)
_IDEMPOTENCY = re.compile(r"\A[A-Za-z0-9._:-]{8,128}\Z")
_ACTOR = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")
_SUMMARY = re.compile(r"\A[A-Za-z0-9 .:_-]{1,160}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SECRET_VALUE = re.compile(
    r"(?i)(?:password|passwd|secret|access[_-]?token|api[_-]?key)\s*[:=]"
)
_PERSONAL_PATH = re.compile(
    r"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/]"
    r"|/(?:home|Users|u)/)[^\\/\s]+[\\/]"
)
_COMMAND_LINE = re.compile(
    r"(?i)(?:\b(?:cmd(?:\.exe)?|powershell|pwsh|bash|python(?:w)?)\b\s+\S"
    r"|traceback \(most recent call last\))"
)
_SECRET_NAMES = frozenset({
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "path",
    "command",
    "traceback",
    "title",
    "detail",
    "body",
    "prompt",
    "context",
    "retryable",
    "retry_suggestion",
})
_RETRY_HINT = re.compile(r"(?i)\bretry\b")


class MutationNoticeError(ValueError):
    """Content-free refusal; input text is never interpolated into the message."""

    def __init__(self, code: str) -> None:
        super().__init__(_ERROR_MESSAGE)
        self.code = code


def _fail(code: str) -> MutationNoticeError:
    return MutationNoticeError(code)


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail("malformed")
    return value


def _text(value: object) -> str:
    if type(value) is not str:
        raise _fail("malformed")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _fail("malformed") from error
    return value


def _canonical_json(value: object) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        check_circular=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text.encode("utf-8", errors="strict")


def _scan_text(text: str) -> None:
    if _SECRET_VALUE.search(text) or _PERSONAL_PATH.search(text) or _COMMAND_LINE.search(text):
        raise _fail("secret_bearing")
    if _CONTROL.search(text):
        raise _fail("malformed")


def _exact_fields(value: object, expected: tuple[str, ...]) -> dict[str, object]:
    mapping = _mapping(value)
    names = []
    for key in mapping:
        if type(key) is not str:
            raise _fail("malformed")
        _scan_text(key)
        if key in _SECRET_NAMES:
            raise _fail("secret_bearing")
        names.append(key)
    extra = set(names) - set(expected)
    if extra:
        raise _fail("unknown_field")
    missing = set(expected) - set(names)
    if missing:
        raise _fail("missing")
    return mapping


def _workspace_uid(value: object) -> str:
    text = _text(value)
    _scan_text(text)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError, TypeError) as error:
        raise _fail("malformed") from error
    if str(parsed) != text or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise _fail("malformed")
    return text


def _bounded_token(value: object, pattern: re.Pattern[str], maximum: int) -> str:
    text = _text(value)
    if len(text) > maximum:
        raise _fail("oversized")
    _scan_text(text)
    if pattern.fullmatch(text) is None:
        raise _fail("malformed")
    return text


def _revision(value: object) -> int:
    if type(value) is not int:
        raise _fail("malformed")
    if not 0 <= value <= MAX_SAFE_INTEGER:
        raise _fail("oversized")
    return value


def _enum(value: object, allowed: frozenset[str]) -> str:
    text = _text(value)
    if text not in allowed:
        raise _fail("malformed")
    return text


def _status_or_null(value: object) -> str | None:
    if value is None:
        return None
    return _enum(value, TASK_STATUSES)


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise _fail("malformed")
    return value


def derive_notice_id(*, workspace_uid: str, idempotency_key: str) -> str:
    canonical_uid = _workspace_uid(workspace_uid)
    key = _bounded_token(idempotency_key, _IDEMPOTENCY, IDEMPOTENCY_MAX)
    if len(key) < IDEMPOTENCY_MIN:
        raise _fail("malformed")
    return str(uuid.uuid5(_NOTICE_NS, "{}:{}".format(canonical_uid, key)))


def derive_mutation_uid(*, workspace_uid: str, idempotency_key: str) -> str:
    canonical_uid = _workspace_uid(workspace_uid)
    key = _bounded_token(idempotency_key, _IDEMPOTENCY, IDEMPOTENCY_MAX)
    return str(uuid.uuid5(_MUTATION_NS, "{}:{}".format(canonical_uid, key)))


def _summary_for(
    *,
    operation: str,
    commit_state: str,
    status_before: str | None,
    status_after: str | None,
) -> str:
    if commit_state == COMMIT_UNKNOWN:
        text = "Mutation outcome is unverifiable"
    elif operation == TASK_STATUS_OPERATION:
        text = "Task status {} to {}".format(status_before, status_after)
    else:
        text = OPERATION_SUMMARY[operation]
    if len(text) > SUMMARY_MAX or _RETRY_HINT.search(text) is not None:
        raise _fail("malformed")
    return _bounded_token(text, _SUMMARY, SUMMARY_MAX)


def _undoable_for(
    *,
    operation: str,
    commit_state: str,
    after_revision: int | None,
    status_before: str | None,
    status_after: str | None,
) -> bool:
    if commit_state != COMMITTED or after_revision is None:
        return False
    if operation not in REVERSIBLE_OPERATIONS:
        return False
    return status_before is not None and status_after is not None and status_before != status_after


def _paired_status(
    *,
    operation: str,
    status_before: object,
    status_after: object,
) -> tuple[str | None, str | None]:
    before = _status_or_null(status_before)
    after = _status_or_null(status_after)
    if operation == TASK_STATUS_OPERATION:
        if before is None or after is None or before == after:
            raise _fail("malformed")
        return before, after
    if before is not None or after is not None:
        raise _fail("malformed")
    return None, None


def _commit_pair(commit_state: str, after_revision: object) -> tuple[str, int | None]:
    if commit_state == COMMIT_UNKNOWN:
        if after_revision is not None:
            raise _fail("malformed")
        return commit_state, None
    if commit_state != COMMITTED:
        raise _fail("malformed")
    return commit_state, _revision(after_revision)


def validate_notice(value: object) -> dict[str, object]:
    mapping = _exact_fields(value, NOTICE_FIELDS)
    if mapping["format"] != FORMAT:
        raise _fail("malformed")
    if type(mapping["schema_version"]) is not int:
        raise _fail("malformed")
    if mapping["schema_version"] != SCHEMA_VERSION:
        raise _fail("unsupported_version")
    workspace_uid = _workspace_uid(mapping["workspace_uid"])
    idempotency_key = _bounded_token(
        mapping["idempotency_key"], _IDEMPOTENCY, IDEMPOTENCY_MAX
    )
    operation = _enum(mapping["operation"], OPERATIONS)
    entity_kind = _enum(mapping["entity_kind"], ENTITY_KINDS)
    if OPERATION_ENTITY[operation] != entity_kind:
        raise _fail("malformed")
    commit_state, after_revision = _commit_pair(
        _enum(mapping["commit_state"], COMMIT_STATES), mapping["after_revision"]
    )
    status_before, status_after = _paired_status(
        operation=operation,
        status_before=mapping["status_before"],
        status_after=mapping["status_after"],
    )
    undoable = _bool(mapping["undoable"])
    expected_undoable = _undoable_for(
        operation=operation,
        commit_state=commit_state,
        after_revision=after_revision,
        status_before=status_before,
        status_after=status_after,
    )
    if undoable != expected_undoable:
        raise _fail("malformed")
    summary = _bounded_token(mapping["summary"], _SUMMARY, SUMMARY_MAX)
    if _RETRY_HINT.search(summary) is not None:
        raise _fail("malformed")
    expected_summary = _summary_for(
        operation=operation,
        commit_state=commit_state,
        status_before=status_before,
        status_after=status_after,
    )
    if summary != expected_summary:
        raise _fail("malformed")
    notice_id = _workspace_uid(mapping["notice_id"])
    mutation_uid = _workspace_uid(mapping["mutation_uid"])
    if notice_id != derive_notice_id(
        workspace_uid=workspace_uid, idempotency_key=idempotency_key
    ):
        raise _fail("malformed")
    if mutation_uid != derive_mutation_uid(
        workspace_uid=workspace_uid, idempotency_key=idempotency_key
    ):
        raise _fail("malformed")
    return {
        "actor": _bounded_token(mapping["actor"], _ACTOR, ACTOR_MAX),
        "after_revision": after_revision,
        "before_revision": _revision(mapping["before_revision"]),
        "commit_state": commit_state,
        "entity_kind": entity_kind,
        "entity_uid": _workspace_uid(mapping["entity_uid"]),
        "format": FORMAT,
        "idempotency_key": idempotency_key,
        "mutation_uid": mutation_uid,
        "notice_id": notice_id,
        "operation": operation,
        "schema_version": SCHEMA_VERSION,
        "source": _enum(mapping["source"], SOURCES),
        "status_after": status_after,
        "status_before": status_before,
        "summary": summary,
        "undoable": undoable,
        "workspace_uid": workspace_uid,
    }


def build_notice(
    *,
    workspace_uid: object,
    entity_kind: object,
    entity_uid: object,
    operation: object,
    before_revision: object,
    after_revision: object,
    source: object,
    actor: object,
    idempotency_key: object,
    commit_state: object,
    status_before: object = None,
    status_after: object = None,
) -> dict[str, object]:
    canonical_uid = _workspace_uid(workspace_uid)
    key = _bounded_token(idempotency_key, _IDEMPOTENCY, IDEMPOTENCY_MAX)
    op = _enum(operation, OPERATIONS)
    kind = _enum(entity_kind, ENTITY_KINDS)
    if OPERATION_ENTITY[op] != kind:
        raise _fail("malformed")
    state, after = _commit_pair(_enum(commit_state, COMMIT_STATES), after_revision)
    before_status, after_status = _paired_status(
        operation=op, status_before=status_before, status_after=status_after
    )
    return validate_notice(
        {
            "actor": actor,
            "after_revision": after,
            "before_revision": before_revision,
            "commit_state": state,
            "entity_kind": kind,
            "entity_uid": entity_uid,
            "format": FORMAT,
            "idempotency_key": key,
            "mutation_uid": derive_mutation_uid(
                workspace_uid=canonical_uid, idempotency_key=key
            ),
            "notice_id": derive_notice_id(
                workspace_uid=canonical_uid, idempotency_key=key
            ),
            "operation": op,
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "status_after": after_status,
            "status_before": before_status,
            "summary": _summary_for(
                operation=op,
                commit_state=state,
                status_before=before_status,
                status_after=after_status,
            ),
            "undoable": _undoable_for(
                operation=op,
                commit_state=state,
                after_revision=after,
                status_before=before_status,
                status_after=after_status,
            ),
            "workspace_uid": canonical_uid,
        }
    )


def serialize_notice(value: object) -> bytes:
    return _canonical_json(validate_notice(value))


def validate_compensation(value: object) -> dict[str, object]:
    mapping = _exact_fields(value, COMPENSATION_FIELDS)
    if mapping["format"] != COMPENSATION_FORMAT:
        raise _fail("malformed")
    if mapping["schema_version"] != SCHEMA_VERSION:
        raise _fail("unsupported_version")
    operation = _enum(mapping["operation"], REVERSIBLE_OPERATIONS)
    entity_kind = _enum(mapping["entity_kind"], frozenset({"task"}))
    return {
        "entity_kind": entity_kind,
        "entity_uid": _workspace_uid(mapping["entity_uid"]),
        "expected_revision": _revision(mapping["expected_revision"]),
        "format": COMPENSATION_FORMAT,
        "idempotency_key": _bounded_token(
            mapping["idempotency_key"], _IDEMPOTENCY, IDEMPOTENCY_MAX
        ),
        "notice_id": _workspace_uid(mapping["notice_id"]),
        "operation": operation,
        "requested_status": _enum(mapping["requested_status"], TASK_STATUSES),
        "schema_version": SCHEMA_VERSION,
        "workspace_uid": _workspace_uid(mapping["workspace_uid"]),
    }


def serialize_compensation(value: object) -> bytes:
    return _canonical_json(validate_compensation(value))


def _compensation_key(notice_id: str) -> str:
    digest = hashlib.sha256(notice_id.encode("ascii")).hexdigest()[:32]
    return "undo:{}".format(digest)


def build_compensation(
    notice: object,
    *,
    current_workspace_uid: object,
    current_entity_uid: object,
    current_revision: object,
) -> dict[str, object]:
    validated = validate_notice(notice)
    if validated["operation"] == PERMANENT_DELETE_OPERATION:
        raise _fail("permanent_deletion")
    if validated["commit_state"] == COMMIT_UNKNOWN:
        raise _fail("commit_unknown")
    if not validated["undoable"] or validated["operation"] not in REVERSIBLE_OPERATIONS:
        raise _fail("not_undoable")
    workspace_uid = _workspace_uid(current_workspace_uid)
    entity_uid = _workspace_uid(current_entity_uid)
    revision = _revision(current_revision)
    if workspace_uid != validated["workspace_uid"]:
        raise _fail("workspace_mismatch")
    if entity_uid != validated["entity_uid"]:
        raise _fail("entity_mismatch")
    if revision != validated["after_revision"]:
        raise _fail("revision_mismatch")
    return validate_compensation(
        {
            "entity_kind": "task",
            "entity_uid": entity_uid,
            "expected_revision": revision,
            "format": COMPENSATION_FORMAT,
            "idempotency_key": _compensation_key(validated["notice_id"]),
            "notice_id": validated["notice_id"],
            "operation": TASK_STATUS_OPERATION,
            "requested_status": validated["status_before"],
            "schema_version": SCHEMA_VERSION,
            "workspace_uid": workspace_uid,
        }
    )
