"""Pure builders for checkpoint identity, commit facts and the committed notice.

This module is deliberately inert: it constructs no Store, performs no IO, HTTP
or publication, reads no clock and allocates no event ID.  It only turns explicit
caller-supplied inputs into detached metadata dictionaries, so a later integrator
can append a ``worklog.recorded`` fact inside the existing transaction.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date as _date
from typing import Any

from .storage.canonical import canonical_json_bytes

__all__ = [
    "CheckpointChangeError",
    "derive_checkpoint_id",
    "build_checkpoint_facts",
    "build_committed_notice",
]

_MESSAGE = "invalid checkpoint input"
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_TASK_ID = re.compile(r"T-[0-9]{4,}")
_CHECKPOINT_ID = re.compile(r"CP-[0-9a-f]{64}")
_ENTRY_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_SAFE_INTEGER = 9007199254740991
_MAX_CATEGORY_ITEMS = 20
_MAX_ITEM_LENGTH = 1000
_ENTRY_FIELDS = ("task_id", "task", "done", "next", "blockers")
_CATEGORY_FIELDS = ("done", "next", "blockers")
_FACTS_FIELDS = ("recorded", "done_count", "next_count", "blocker_count", "first_for_task")
_RECORDED_FIELDS = (
    "type", "workspace_uid", "task_id", "checkpoint_id", "date", "ordinal",
    "entry_digest", "origin",
)
_RECORDED_TYPE = "worklog.recorded"
_NOTICE_KIND = "agent.checkpoint.committed"
_ATTRIBUTED_ORIGIN = "agent-cli-v1"


class CheckpointChangeError(ValueError):
    """Content-free refusal; no input value or nested text is ever interpolated."""

    code = "invalid_checkpoint_input"

    def __init__(self) -> None:
        super().__init__(_MESSAGE)


def _refuse() -> CheckpointChangeError:
    return CheckpointChangeError()


def _text(value: Any) -> str:
    """Require an exact built-in str that is representable as strict UTF-8."""

    if type(value) is not str:
        raise _refuse()
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _refuse() from error
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _refuse()
    return value


def _workspace_uid(value: Any) -> str:
    """Existing Store-domain rule: canonical, non-nil, RFC-4122, any version."""

    text = _text(value)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as error:
        raise _refuse() from error
    if str(parsed) != text or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise _refuse()
    return text


def _matched(value: Any, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if pattern.fullmatch(text) is None:
        raise _refuse()
    return text


def _canonical_date(value: Any) -> str:
    text = _text(value)
    try:
        parsed = _date.fromisoformat(text)
    except ValueError as error:
        raise _refuse() from error
    if parsed.isoformat() != text:
        raise _refuse()
    return text


def _origin(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value != _ATTRIBUTED_ORIGIN:
        raise _refuse()
    return value


def _exact_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Require an exact built-in dict whose keys are exact built-in strings.

    Set comparison alone is not enough: a ``str`` subclass key hashes and
    compares equal to its schema name, so the key type is checked first.
    """

    if type(value) is not dict:
        raise _refuse()
    if any(type(key) is not str for key in value) or set(value) != set(fields):
        raise _refuse()
    return value


def _category(value: Any) -> int:
    """Accept an already-normalized stored category list and return its length."""

    if type(value) is not list or len(value) > _MAX_CATEGORY_ITEMS:
        raise _refuse()
    for item in value:
        text = _text(item)
        if not text or text != text.strip() or len(text) > _MAX_ITEM_LENGTH:
            raise _refuse()
    return len(value)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise _refuse() from error


def _entry_counts(entry: dict[str, Any]) -> dict[str, int]:
    """Validate the exact stored entry and count each category."""

    _exact_fields(entry, _ENTRY_FIELDS)
    _matched(entry["task_id"], _TASK_ID)
    _text(entry["task"])
    counts = {field: _category(entry[field]) for field in _CATEGORY_FIELDS}
    if sum(counts.values()) < 1:
        raise _refuse()
    return counts


def _prior_task_ids(prior_entries: Any) -> list[str]:
    """Collect linked Task IDs from every prior physical entry, unfiltered."""

    if type(prior_entries) is not list:
        raise _refuse()
    linked: list[str] = []
    for item in prior_entries:
        if type(item) is not dict:
            raise _refuse()
        task_id = item.get("task_id")
        if task_id is None:
            continue
        linked.append(_matched(task_id, _TASK_ID))
    return linked


def derive_checkpoint_id(*, workspace_uid: str, idempotency_key: str) -> str:
    """Derive the CP identity from the workspace and the verbatim original key."""

    canonical_uid = _workspace_uid(workspace_uid)
    key = _matched(idempotency_key, _IDEMPOTENCY_KEY)
    preimage = _canonical_bytes(["workstack.checkpoint.v1", canonical_uid, key])
    return "CP-" + hashlib.sha256(preimage).hexdigest()


def build_checkpoint_facts(
    *,
    workspace_uid: str,
    idempotency_key: str,
    date: str,
    entry: dict[str, object],
    ordinal: int,
    prior_entries: list[dict[str, object]],
    origin: str | None,
) -> dict[str, object]:
    """Build the detached commit facts for one accepted physical Worklog entry."""

    checkpoint_id = derive_checkpoint_id(
        workspace_uid=workspace_uid, idempotency_key=idempotency_key
    )
    canonical_date = _canonical_date(date)
    counts = _entry_counts(entry)
    physical_ordinal = _integer(ordinal, 0, _MAX_SAFE_INTEGER)
    prior_task_ids = _prior_task_ids(prior_entries)
    canonical_origin = _origin(origin)
    digest = hashlib.sha256(_canonical_bytes(entry)).hexdigest()
    return {
        "recorded": {
            "type": _RECORDED_TYPE,
            "workspace_uid": _workspace_uid(workspace_uid),
            "task_id": entry["task_id"],
            "checkpoint_id": checkpoint_id,
            "date": canonical_date,
            "ordinal": physical_ordinal,
            "entry_digest": "sha256:" + digest,
            "origin": canonical_origin,
        },
        "done_count": counts["done"],
        "next_count": counts["next"],
        "blocker_count": counts["blockers"],
        "first_for_task": entry["task_id"] not in prior_task_ids,
    }


def _validated_counts(facts: dict[str, Any]) -> dict[str, int]:
    counts = {
        field: _integer(facts[field], 0, _MAX_CATEGORY_ITEMS)
        for field in ("done_count", "next_count", "blocker_count")
    }
    if sum(counts.values()) < 1:
        raise _refuse()
    return counts


def _validated_recorded(facts: dict[str, Any]) -> dict[str, Any]:
    recorded = _exact_fields(facts["recorded"], _RECORDED_FIELDS)
    if _text(recorded["type"]) != _RECORDED_TYPE or _origin(recorded["origin"]) is None:
        raise _refuse()
    _workspace_uid(recorded["workspace_uid"])
    _matched(recorded["task_id"], _TASK_ID)
    _matched(recorded["checkpoint_id"], _CHECKPOINT_ID)
    _matched(recorded["entry_digest"], _ENTRY_DIGEST)
    _canonical_date(recorded["date"])
    _integer(recorded["ordinal"], 0, _MAX_SAFE_INTEGER)
    return recorded


def build_committed_notice(
    *, facts: dict[str, object], event_id: int
) -> dict[str, object]:
    """Build the attributed committed notice from fully revalidated facts."""

    _exact_fields(facts, _FACTS_FIELDS)
    recorded = _validated_recorded(facts)
    counts = _validated_counts(facts)
    if type(facts["first_for_task"]) is not bool:
        raise _refuse()
    return {
        "event_id": _integer(event_id, 1, _MAX_SAFE_INTEGER),
        "kind": _NOTICE_KIND,
        "workspace_uid": recorded["workspace_uid"],
        "task_id": recorded["task_id"],
        "date": recorded["date"],
        "checkpoint_id": recorded["checkpoint_id"],
        "done_count": counts["done_count"],
        "next_count": counts["next_count"],
        "blocker_count": counts["blocker_count"],
        "first_for_task": facts["first_for_task"],
        "origin": _ATTRIBUTED_ORIGIN,
        "replayed": False,
    }
