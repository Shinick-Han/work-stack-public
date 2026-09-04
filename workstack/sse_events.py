"""Bounded, pure encoder for the legacy ``/api/v1/events`` sync stream.

The endpoint's job is to tell the GUI that its view is stale so it refetches.
That hint used to be collapsed: whatever the retained batch contained, one frame
carried only ``latest_event_id``, so a browser resuming from an older cursor
jumped straight past every retained record in between.

This module encodes one legacy ``event: sync`` frame per retained Store-assigned
id, in the original increasing order. It assigns no ids of its own, invents no
cursor, and adds no field: each frame carries exactly the ``generation`` and
``state`` refetch snapshot the single frame already carried.

Deliberate properties, so they are not mistaken for oversights:

* **The snapshot repeats.** Every frame reports the same current generation and
  state, because the Store exposes one current snapshot rather than a per-event
  one. The frames are a *hint that something changed*, in order, not an
  event-sourced history. A client must still refetch authoritatively.
* **Gaps are legitimate.** The Store keeps a bounded deque, so retained ids can
  start above the cursor or skip evicted records. A gap is not corruption and is
  not rejected.
* **Delivery stays process-local and bounded.** Nothing here makes it durable.
  Events lost to eviction or a process restart are not recoverable through this
  stream, and this module does not pretend otherwise.

The encoder is pure: it takes the payload the Store already returns and produces
bytes. It performs no IO, holds no state, and validates the whole batch before
returning anything, so a caller can never write a partial body and then discover
a bad record.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date as _date
from typing import Any, Mapping, Sequence

# The Store's own retention bound (workstack/store.py: deque(maxlen=128)). A
# batch larger than this cannot have come from that deque.
MAX_RETAINED_EVENTS = 128

# The state contract the product already publishes; mirrored rather than widened.
SUPPORTED_STATES = frozenset({"external-change-detected", "in-sync", "invalid"})

# Only these two fields have ever been on the wire, and only these two are sent.
SNAPSHOT_FIELDS = ("generation", "state")

RETRY_MILLISECONDS = 3000

# The one typed change frame carried beside the legacy sync frames. The event
# name and the twelve data fields are frozen: nothing else may enter this frame,
# so no title, prose, raw key, reason, digest, ordinal or generation appears.
CHANGE_EVENT_NAME = "workstack.change.v1"
CHANGE_RECORD_TYPE = CHANGE_EVENT_NAME
NOTICE_FIELDS = (
    "event_id", "kind", "workspace_uid", "task_id", "date", "checkpoint_id",
    "done_count", "next_count", "blocker_count", "first_for_task", "origin",
    "replayed",
)
NOTICE_KIND = "agent.checkpoint.committed"
NOTICE_ORIGIN = "agent-cli-v1"
MAX_CATEGORY_COUNT = 20
MAX_SAFE_INTEGER = 9007199254740991
_TASK_ID = re.compile(r"T-[0-9]{4,}")
_CHECKPOINT_ID = re.compile(r"CP-[0-9a-f]{64}")

# The D5 transition notice. It shares neither its field set nor its order with
# the committed notice above: it carries ordinal and entry_digest, names its
# revision transition_revision, and has no counts or first_for_task. Both are
# eleven or twelve exact fields and each variant is dispatched and validated
# separately. reason, explanation, the raw idempotency key and every other
# prose value are deliberately absent and must never be added.
# The transition payload SCHEMA name, used only to describe the payload in
# this module. It is deliberately NOT an SSE event name: both schemas travel
# under the approved CHANGE_EVENT_NAME.
TRANSITION_SCHEMA_NAME = "workstack.transition.v1"
TRANSITION_FIELDS = (
    "event_id", "kind", "workspace_uid", "task_id", "date", "checkpoint_id",
    "ordinal", "entry_digest", "state", "transition_revision", "origin",
)
TRANSITION_KINDS = {
    "superseded": "agent.checkpoint.superseded",
    "active": "agent.checkpoint.restored",
}
_ENTRY_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

# A comment frame: no event, no id, no data. It keeps the connection honest
# without advancing any cursor or triggering a client change callback.
HEARTBEAT = ": heartbeat\n\n"


class SseEncodingError(ValueError):
    """The batch is internally inconsistent and cannot be encoded.

    This is an internal fault, not a client mistake: the caller supplied it, not
    the request. Callers must translate it into a sanitized server error, and
    must not echo its text or the offending payload.
    """


def _strict_non_negative_int(value: object, field: str) -> int:
    # `type(...) is not int` rather than isinstance, so True cannot pass as 1.
    if type(value) is not int or value < 0:
        raise SseEncodingError("{} must be a non-negative integer".format(field))
    return value


def _validated_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    generation = _strict_non_negative_int(payload.get("generation"), "generation")
    state = payload.get("state")
    if type(state) is not str or state not in SUPPORTED_STATES:
        raise SseEncodingError("state is not a supported sync state")
    return {"generation": generation, "state": state}


def _validated_batch(payload: object) -> Mapping[str, Any]:
    """Classify the top-level batch shape before any mapping access.

    Reaching for a key first would raise ``AttributeError`` on a batch that is
    ``None``, a list or a string. That is not an ``SseEncodingError``, so the
    caller's narrow handler would not catch it and the connection would be
    dropped without a response instead of returning the sanitized error this
    module's contract promises. Classifying first keeps every malformed batch on
    the same, catchable path.
    """

    if not isinstance(payload, Mapping):
        raise SseEncodingError("sync event batch is not an object")
    return payload


def validated_event_ids(payload: Mapping[str, Any], after: int) -> list[int]:
    """The retained ids to emit, or raise before anything is written.

    Every id must be a strict integer, must lie strictly after the cursor, must
    not exceed the batch's own latest id, and the sequence must be strictly
    increasing, which rules out duplicates and disorder in one check. Gaps are
    allowed: the Store's deque is bounded and evicting old records is normal.
    """

    payload = _validated_batch(payload)
    _strict_non_negative_int(after, "cursor")
    latest = _strict_non_negative_int(payload.get("latest_event_id"), "latest_event_id")
    events = payload.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise SseEncodingError("events must be a sequence")
    if len(events) > MAX_RETAINED_EVENTS:
        raise SseEncodingError("events exceed the retained event bound")

    identifiers: list[int] = []
    previous = after
    for event in events:
        if not isinstance(event, Mapping):
            raise SseEncodingError("event record is not an object")
        identifier = _strict_non_negative_int(event.get("id"), "event id")
        if identifier <= after:
            raise SseEncodingError("event id is not after the requested cursor")
        if identifier > latest:
            raise SseEncodingError("event id is beyond the latest event id")
        if identifier <= previous:
            raise SseEncodingError("event ids are not strictly increasing")
        previous = identifier
        identifiers.append(identifier)
    return identifiers


def _strict_bounded_count(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CATEGORY_COUNT:
        raise SseEncodingError("{} is not a bounded count".format(field))
    return value


def _matched_text(value: object, pattern: Any, field: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise SseEncodingError("{} does not match its frozen shape".format(field))
    return value


def _validated_notice_fields(notice: object) -> dict[str, Any]:
    """Exactly the twelve frozen fields, with exact built-in string keys.

    A ``str`` subclass key compares equal to its schema name, so the key type is
    checked before the set comparison.
    """

    if type(notice) is not dict:
        raise SseEncodingError("change notice is not an object")
    if any(type(key) is not str for key in notice) or set(notice) != set(NOTICE_FIELDS):
        raise SseEncodingError("change notice does not carry exactly its frozen fields")
    return notice


def _validate_notice_literals(notice: dict[str, Any]) -> None:
    """The frozen enum values and the strict integer/boolean separation."""

    event_id = notice["event_id"]
    if type(event_id) is not int or not 1 <= event_id <= MAX_SAFE_INTEGER:
        raise SseEncodingError("change notice event_id is out of range")
    if type(notice["kind"]) is not str or notice["kind"] != NOTICE_KIND:
        raise SseEncodingError("change notice kind is not the frozen kind")
    if type(notice["origin"]) is not str or notice["origin"] != NOTICE_ORIGIN:
        raise SseEncodingError("change notice origin is not the frozen origin")
    if notice["replayed"] is not False:
        raise SseEncodingError("a replayed change notice is never published")
    if type(notice["first_for_task"]) is not bool:
        raise SseEncodingError("first_for_task is not a boolean")


def _validate_notice_workspace_uid(value: object) -> None:
    """Canonical, non-nil, RFC 4122, exactly as the Store domain requires."""

    if type(value) is not str:
        raise SseEncodingError("workspace_uid is not text")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise SseEncodingError("workspace_uid is not a canonical UUID") from error
    if str(parsed) != value or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise SseEncodingError("workspace_uid is not a canonical non-nil RFC 4122 UUID")


def _validate_notice_calendar_date(value: object) -> None:
    if type(value) is not str:
        raise SseEncodingError("date is not text")
    try:
        parsed_date = _date.fromisoformat(value)
    except ValueError as error:
        raise SseEncodingError("date is not a canonical calendar date") from error
    if parsed_date.isoformat() != value:
        raise SseEncodingError("date is not canonical")


def _validate_notice_counts(notice: dict[str, Any]) -> None:
    counts = [
        _strict_bounded_count(notice[field], field)
        for field in ("done_count", "next_count", "blocker_count")
    ]
    if sum(counts) < 1:
        raise SseEncodingError("a change notice carries no counted item")


def _validated_notice(notice: object) -> dict[str, Any]:
    """Validate the whole typed payload, or raise before anything is written.

    Decomposed only for measured complexity: every exact field, type, enum,
    identifier, calendar and count check the single function performed is still
    performed here, in the same order.
    """

    validated = _validated_notice_fields(notice)
    _validate_notice_literals(validated)
    _validate_notice_workspace_uid(validated["workspace_uid"])
    _matched_text(validated["task_id"], _TASK_ID, "task_id")
    _matched_text(validated["checkpoint_id"], _CHECKPOINT_ID, "checkpoint_id")
    _validate_notice_calendar_date(validated["date"])
    _validate_notice_counts(validated)
    return {field: validated[field] for field in NOTICE_FIELDS}


def _validate_transition_numbers(notice: dict[str, Any]) -> None:
    """The three strict integers, each in its own frozen range."""

    event_id = notice["event_id"]
    if type(event_id) is not int or not 1 <= event_id <= MAX_SAFE_INTEGER:
        raise SseEncodingError("transition notice event_id is out of range")
    revision = notice["transition_revision"]
    if type(revision) is not int or not 1 <= revision <= MAX_SAFE_INTEGER:
        raise SseEncodingError("transition_revision is out of range")
    ordinal = notice["ordinal"]
    if type(ordinal) is not int or not 0 <= ordinal <= MAX_SAFE_INTEGER:
        raise SseEncodingError("ordinal is out of range")


def _validate_transition_literals(notice: dict[str, Any]) -> None:
    """State, its announced kind, the frozen origin, and state/revision parity.

    Parity is part of the frozen contract, not a numeric range: a checkpoint
    starts active at revision zero and alternates, so an ACTIVE state can only
    carry an even positive revision and a SUPERSEDED state an odd one. A notice
    that violates it describes a history that cannot exist, so the batch is
    refused before any header rather than streamed.
    """

    state = notice["state"]
    if type(state) is not str or state not in TRANSITION_KINDS:
        raise SseEncodingError("transition state is not a frozen state")
    if notice["kind"] != TRANSITION_KINDS[state]:
        raise SseEncodingError("transition kind does not match its state")
    if type(notice["origin"]) is not str or notice["origin"] != NOTICE_ORIGIN:
        raise SseEncodingError("transition notice origin is not the frozen origin")
    revision = notice["transition_revision"]
    if (revision % 2 == 0) != (state == "active"):
        raise SseEncodingError("transition state and revision parity disagree")


def _validated_transition(notice: object) -> dict[str, Any]:
    """Validate the whole transition payload, or raise before any output.

    Exactly the eleven frozen fields with exact built-in string keys, and no
    prose of any kind. Decomposed only for measured complexity: the same checks
    run, in the same order.
    """

    if type(notice) is not dict:
        raise SseEncodingError("transition notice is not an object")
    if any(type(key) is not str for key in notice) or set(notice) != set(TRANSITION_FIELDS):
        raise SseEncodingError("transition notice does not carry exactly its frozen fields")
    _validate_transition_numbers(notice)
    _validate_transition_literals(notice)
    _validate_notice_workspace_uid(notice["workspace_uid"])
    _matched_text(notice["task_id"], _TASK_ID, "task_id")
    _matched_text(notice["checkpoint_id"], _CHECKPOINT_ID, "checkpoint_id")
    _matched_text(notice["entry_digest"], _ENTRY_DIGEST, "entry_digest")
    _validate_notice_calendar_date(notice["date"])
    return {field: notice[field] for field in TRANSITION_FIELDS}


def _validated_typed_notice(notice: object) -> tuple[str, dict[str, Any]]:
    """Choose the variant by its EXACT frozen field set, then validate it fully.

    The committed notice and the transition notice are separate schemas that
    share no field set, so the key set identifies the variant unambiguously and
    each is then validated by its own function. A payload matching neither is
    refused rather than coerced into the closer-looking one.
    """

    if type(notice) is not dict:
        raise SseEncodingError("typed notice is not an object")
    if any(type(key) is not str for key in notice):
        raise SseEncodingError("typed notice keys are not exact strings")
    keys = set(notice)
    if keys == set(NOTICE_FIELDS):
        return CHANGE_EVENT_NAME, _validated_notice(notice)
    if keys == set(TRANSITION_FIELDS):
        # The APPROVED transport event name carries both payload schemas. The
        # two are told apart by their frozen kind and field set, not by
        # renaming the event: one EventSource and one named listener already
        # depend on this name, and a backend rename would silently break them.
        return CHANGE_EVENT_NAME, _validated_transition(notice)
    raise SseEncodingError("typed notice matches no frozen schema")


def _validated_frames(payload: Mapping[str, Any], after: int) -> list[tuple[int, str, str]]:
    """Every frame to emit, fully validated before any byte is produced.

    Ordering, bounds and cursor rules are the existing ones. The only addition
    is that a record typed as a change notice is validated and rendered under
    its own event name instead of the legacy sync snapshot, so mixed records
    keep one strictly ascending id sequence and one retention bound.
    """

    identifiers = validated_event_ids(payload, after)
    snapshot = _validated_snapshot(payload)
    snapshot_data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    events = [event for event in payload["events"]]
    frames: list[tuple[int, str, str]] = []
    for identifier, event in zip(identifiers, events):
        if event.get("type") == CHANGE_RECORD_TYPE:
            name, notice = _validated_typed_notice(event.get("notice"))
            if notice["event_id"] != identifier:
                raise SseEncodingError("typed notice event_id does not match its id")
            frames.append((
                identifier,
                name,
                json.dumps(notice, ensure_ascii=False, separators=(",", ":")),
            ))
        else:
            frames.append((identifier, "sync", snapshot_data))
    return frames


def encode_sync_stream(payload: Mapping[str, Any], after: int) -> bytes:
    """Encode the whole batch, or raise without producing any output.

    Returns one ``event: sync`` frame per retained id in increasing order. When
    nothing is retained after the cursor, returns only a content-free heartbeat:
    no id, no event, no data, so no cursor advances and no client change
    callback fires.
    """

    # Classified once here so the snapshot read below is also safe; the
    # identity check inside validated_event_ids stays for direct callers.
    payload = _validated_batch(payload)
    frames = _validated_frames(payload, after)

    if not frames:
        # retry is neither an id, an event nor data; it keeps the browser's
        # reconnect interval defined while the frame stays content-free.
        return ("retry: {}\n{}".format(RETRY_MILLISECONDS, HEARTBEAT)).encode("utf-8")

    rendered = [
        "retry: {retry}\nid: {event_id}\nevent: {name}\ndata: {data}\n\n".format(
            retry=RETRY_MILLISECONDS, event_id=identifier, name=name, data=data
        )
        for identifier, name, data in frames
    ]
    return "".join(rendered).encode("utf-8")
