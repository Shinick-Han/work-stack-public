"""Pure checkpoint-transition contract for D5.

Every function here is pure: no clock, event-counter allocation, IO, Store,
transaction, replay ledger, HTTP or publication. Arguments are deliberately
object-typed so malformed values reach content-free validation rather than
raising an incidental Python error.

A fresh import of this module loads exactly ``workstack`` and
``workstack.checkpoint_transition`` as its workstack-prefixed set, and no storage
module. A caller that additionally imports the canonical serializer, as the
contract test does, transitively loads storage; that is a separate measurement
and no Store is constructed by either.
"""

from __future__ import annotations

import re
from datetime import date as _date
from uuid import UUID

MAX = 9007199254740991

_ERROR_MESSAGE = "invalid checkpoint transition input"

_TASK_ID = re.compile(r"\AT-[0-9]{4,}\Z", re.ASCII)
_CHECKPOINT_ID = re.compile(r"\ACP-[0-9a-f]{64}\Z", re.ASCII)
_ENTRY_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z", re.ASCII)

_ORIGIN = "agent-cli-v1"
_STATES = ("active", "superseded")
_SUPERSEDED_CODES = ("incorrect", "duplicate", "obsolete")
_ACTIVE_CODES = ("restore",)

_RECORDED_KEYS = (
    "type",
    "workspace_uid",
    "task_id",
    "checkpoint_id",
    "date",
    "ordinal",
    "entry_digest",
    "origin",
)
_LOCATOR_KEYS = ("workspace_uid", "task_id", "date", "ordinal", "entry_digest")
_EVENT_KEYS = (
    "type",
    "workspace_uid",
    "task_id",
    "checkpoint_id",
    "date",
    "ordinal",
    "entry_digest",
    "state",
    "revision",
    "reason",
    "origin",
)


class CheckpointTransitionError(ValueError):
    """Content-free refusal carrying one of the six closed codes."""

    def __init__(self, code: str) -> None:
        super().__init__(_ERROR_MESSAGE)
        self.code = code


def _fail(code: str) -> "CheckpointTransitionError":
    return CheckpointTransitionError(code)


# ---------------------------------------------------------------------------
# Exact built-in domain helpers
# ---------------------------------------------------------------------------


def _exact_dict(value: object) -> dict:
    if type(value) is not dict:
        raise _fail("malformed")
    return value


def _exact_list(value: object) -> list:
    if type(value) is not list:
        raise _fail("malformed")
    return value


def _keys(value: dict, expected: tuple[str, ...]) -> None:
    for key in value:
        # Keys must be exact built-in str before any set comparison.
        if type(key) is not str:
            raise _fail("malformed")
    if set(value) != set(expected):
        raise _fail("malformed")


def _fields(value: object, expected: tuple[str, ...]) -> dict:
    mapping = _exact_dict(value)
    _keys(mapping, expected)
    return mapping


def _text(value: object) -> str:
    if type(value) is not str:
        raise _fail("malformed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:  # lone surrogates and friends
        raise _fail("malformed") from error
    return value


def _int_in(value: object, low: int, high: int) -> int:
    if type(value) is not int:
        raise _fail("malformed")
    if not low <= value <= high:
        raise _fail("malformed")
    return value


def _workspace_uid(value: object) -> str:
    text = _text(value)
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError, TypeError) as error:
        raise _fail("malformed") from error
    if parsed.int == 0 or parsed.variant != "specified in RFC 4122":
        raise _fail("malformed")
    if str(parsed) != text:
        raise _fail("malformed")
    return text


def _pattern(value: object, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if pattern.match(text) is None:
        raise _fail("malformed")
    return text


def _calendar_date(value: object) -> str:
    text = _text(value)
    try:
        parsed = _date.fromisoformat(text)
    except ValueError as error:
        raise _fail("malformed") from error
    if parsed.isoformat() != text or not 1 <= parsed.year <= 9999:
        raise _fail("malformed")
    return text


def _origin(value: object) -> object:
    if value is None:
        return None
    if type(value) is not str or value != _ORIGIN:
        raise _fail("malformed")
    return value


def _state(value: object) -> str:
    text = _text(value)
    if text not in _STATES:
        raise _fail("malformed")
    return text


def _reason(value: object, *, normalize: bool) -> dict[str, object]:
    reason = _fields(value, ("code", "explanation"))
    code = _text(reason["code"])
    explanation = _text(reason["explanation"])
    if code not in _SUPERSEDED_CODES + _ACTIVE_CODES:
        raise _fail("malformed")
    if normalize:
        explanation = explanation.strip()
    elif explanation != explanation.strip():
        # Persisted reasons must already be canonical, never silently repaired.
        raise _fail("malformed")
    if not 1 <= len(explanation) <= 240:
        raise _fail("malformed")
    if len(explanation.encode("utf-8")) > 1024:
        raise _fail("malformed")
    return {"code": code, "explanation": explanation}


def _reason_for_state(reason: dict[str, object], state: str) -> None:
    allowed = _SUPERSEDED_CODES if state == "superseded" else _ACTIVE_CODES
    if reason["code"] not in allowed:
        raise _fail("malformed")


def _parity(state: str, revision: int) -> None:
    expected = "active" if revision % 2 == 0 else "superseded"
    if state != expected:
        raise _fail("history_invalid")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def normalize_transition_request(request: object, /) -> dict[str, object]:
    """Validate an incoming request and return fresh canonical metadata."""
    fields = _fields(request, ("state", "revision", "reason"))
    state = _state(fields["state"])
    revision = _int_in(fields["revision"], 0, MAX)
    reason = _reason(fields["reason"], normalize=True)
    _reason_for_state(reason, state)
    return {"state": state, "revision": revision, "reason": reason}


def next_transition(context: object, /) -> dict[str, object]:
    """Decide the next state without deciding anything about persistence."""
    fields = _fields(context, ("current", "request"))
    current = _fields(fields["current"], ("state", "revision"))
    current_state = _state(current["state"])
    current_revision = _int_in(current["revision"], 0, MAX)
    request = normalize_transition_request(fields["request"])

    # Current must be consistent with contiguous cycles from the initial state.
    _parity(current_state, current_revision)

    if request["revision"] < current_revision:
        raise _fail("stale_revision")
    if request["revision"] > current_revision:
        raise _fail("stale_revision")
    if request["state"] == current_state:
        raise _fail("same_state")
    if current_revision == MAX:
        raise _fail("exhausted")

    return {
        "state": request["state"],
        "revision": current_revision + 1,
        "reason": dict(request["reason"]),
    }


def _locator(value: object, *, legacy: bool = False) -> dict[str, object]:
    fields = _fields(value, _LOCATOR_KEYS)
    task_id = fields["task_id"]
    entry_digest = fields["entry_digest"]
    if legacy and task_id is None:
        task_id = None
    else:
        task_id = _pattern(task_id, _TASK_ID)
    if legacy and entry_digest is None:
        entry_digest = None
    else:
        entry_digest = _pattern(entry_digest, _ENTRY_DIGEST)
    return {
        "workspace_uid": _workspace_uid(fields["workspace_uid"]),
        "task_id": task_id,
        "date": _calendar_date(fields["date"]),
        "ordinal": _int_in(fields["ordinal"], 0, MAX),
        "entry_digest": entry_digest,
    }


def _recorded(value: object) -> dict[str, object]:
    fields = _fields(value, _RECORDED_KEYS)
    if _text(fields["type"]) != "worklog.recorded":
        raise _fail("malformed")
    return {
        "type": "worklog.recorded",
        "workspace_uid": _workspace_uid(fields["workspace_uid"]),
        "task_id": _pattern(fields["task_id"], _TASK_ID),
        "checkpoint_id": _pattern(fields["checkpoint_id"], _CHECKPOINT_ID),
        "date": _calendar_date(fields["date"]),
        "ordinal": _int_in(fields["ordinal"], 0, MAX),
        "entry_digest": _pattern(fields["entry_digest"], _ENTRY_DIGEST),
        "origin": _origin(fields["origin"]),
    }


def _recorded_locator(recorded: dict[str, object]) -> dict[str, object]:
    return {key: recorded[key] for key in _LOCATOR_KEYS}


def verify_locator(context: object, /) -> dict[str, object]:
    """Bind recorded metadata to a caller-derived physical locator."""
    fields = _fields(context, ("workspace_uid", "checkpoint_id", "recorded", "actual_locator"))
    workspace_uid = _workspace_uid(fields["workspace_uid"])
    checkpoint_id = _pattern(fields["checkpoint_id"], _CHECKPOINT_ID)
    recorded = _recorded(fields["recorded"])
    actual = _locator(fields["actual_locator"])

    if workspace_uid != recorded["workspace_uid"] or checkpoint_id != recorded["checkpoint_id"]:
        raise _fail("locator_mismatch")
    expected = _recorded_locator(recorded)
    if any(expected[key] != actual[key] for key in _LOCATOR_KEYS):
        raise _fail("locator_mismatch")
    return dict(expected)


def build_transition_event(context: object, /) -> dict[str, object]:
    """Construct transition metadata; it proves no recorded fact exists."""
    fields = _fields(
        context, ("workspace_uid", "checkpoint_id", "locator", "transition", "origin")
    )
    workspace_uid = _workspace_uid(fields["workspace_uid"])
    checkpoint_id = _pattern(fields["checkpoint_id"], _CHECKPOINT_ID)
    locator = _locator(fields["locator"])
    origin = _origin(fields["origin"])

    transition = _fields(fields["transition"], ("state", "revision", "reason"))
    state = _state(transition["state"])
    revision = _int_in(transition["revision"], 1, MAX)
    reason = _reason(transition["reason"], normalize=False)
    _reason_for_state(reason, state)

    # Global precedence: all known syntax, then binding, then history rules.
    if workspace_uid != locator["workspace_uid"]:
        raise _fail("locator_mismatch")
    _parity(state, revision)

    return {
        "type": "worklog.superseded" if state == "superseded" else "worklog.restored",
        "workspace_uid": workspace_uid,
        "task_id": locator["task_id"],
        "checkpoint_id": checkpoint_id,
        "date": locator["date"],
        "ordinal": locator["ordinal"],
        "entry_digest": locator["entry_digest"],
        "state": state,
        "revision": revision,
        "reason": dict(reason),
        "origin": origin,
    }


def _event(value: object) -> dict[str, object]:
    """Validate event syntax only; parity is a history rule applied later."""
    fields = _fields(value, _EVENT_KEYS)
    state = _state(fields["state"])
    expected_type = "worklog.superseded" if state == "superseded" else "worklog.restored"
    if _text(fields["type"]) != expected_type:
        raise _fail("malformed")
    revision = _int_in(fields["revision"], 1, MAX)
    reason = _reason(fields["reason"], normalize=False)
    _reason_for_state(reason, state)
    return {
        "type": expected_type,
        "workspace_uid": _workspace_uid(fields["workspace_uid"]),
        "task_id": _pattern(fields["task_id"], _TASK_ID),
        "checkpoint_id": _pattern(fields["checkpoint_id"], _CHECKPOINT_ID),
        "date": _calendar_date(fields["date"]),
        "ordinal": _int_in(fields["ordinal"], 0, MAX),
        "entry_digest": _pattern(fields["entry_digest"], _ENTRY_DIGEST),
        "state": state,
        "revision": revision,
        "reason": reason,
        "origin": _origin(fields["origin"]),
    }


def build_transition_notice(event: object, event_id: object, /) -> dict[str, object]:
    """Attributed-only notice; an ordinary null-origin event has none."""
    validated = _event(event)
    identifier = _int_in(event_id, 1, MAX)
    if validated["origin"] != _ORIGIN:
        raise _fail("malformed")
    # Parity is a history rule, so it follows every syntax check including the
    # caller-supplied event id.
    _parity(validated["state"], validated["revision"])
    state = validated["state"]
    return {
        "event_id": identifier,
        "kind": "agent.checkpoint.superseded"
        if state == "superseded"
        else "agent.checkpoint.restored",
        "workspace_uid": validated["workspace_uid"],
        "task_id": validated["task_id"],
        "date": validated["date"],
        "checkpoint_id": validated["checkpoint_id"],
        "ordinal": validated["ordinal"],
        "entry_digest": validated["entry_digest"],
        "state": state,
        "transition_revision": validated["revision"],
        "origin": validated["origin"],
    }


# ---------------------------------------------------------------------------
# Projection context
# ---------------------------------------------------------------------------


def _wrapper_row(wrapper: object) -> dict[str, object]:
    """Validate one entry wrapper's syntax; binding is a later phase."""
    fields = _fields(wrapper, ("locator", "recorded", "entry"))
    recorded_value = fields["recorded"]
    legacy = recorded_value is None
    return {
        "locator": _locator(fields["locator"], legacy=legacy),
        "recorded": None if legacy else _recorded(recorded_value),
        "entry": fields["entry"],
        "checkpoint_id": None if legacy else _recorded(recorded_value)["checkpoint_id"],
    }


def _bind_rows(rows: list[dict[str, object]], workspace_uid: str) -> None:
    """Every known row binds to the outer workspace and its recorded fact."""
    for row in rows:
        locator = row["locator"]
        # Applies to recorded and legacy rows alike, even with no transitions:
        # an internally consistent foreign row is still a mismatch.
        if locator["workspace_uid"] != workspace_uid:
            raise _fail("locator_mismatch")
        recorded = row["recorded"]
        if recorded is None:
            continue
        expected = _recorded_locator(recorded)
        if any(expected[key] != locator[key] for key in _LOCATOR_KEYS):
            raise _fail("locator_mismatch")


def _check_uniqueness(rows: list[dict[str, object]]) -> None:
    """Physical slot and checkpoint identity are unique across all wrappers."""
    slots: set[tuple[str, str, int]] = set()
    checkpoints: set[str] = set()
    for row in rows:
        locator = row["locator"]
        slot = (locator["workspace_uid"], locator["date"], locator["ordinal"])
        if slot in slots:
            raise _fail("history_invalid")
        slots.add(slot)
        checkpoint_id = row["checkpoint_id"]
        if checkpoint_id is not None:
            if checkpoint_id in checkpoints:
                raise _fail("history_invalid")
            checkpoints.add(checkpoint_id)


def _bind_transitions(
    events: list[dict[str, object]],
    rows: list[dict[str, object]],
    workspace_uid: str,
) -> dict[str, list[dict[str, object]]]:
    """Bind each validated event to exactly one recorded wrapper."""
    by_checkpoint: dict[str, list[dict[str, object]]] = {}
    for event in events:
        if event["workspace_uid"] != workspace_uid:
            raise _fail("locator_mismatch")
        # Duplicate checkpoints are a history error, so candidates are searched
        # rather than collapsed into one lookup: an event that matches any row
        # carrying its checkpoint is bound, and the duplicate is caught later.
        candidates = [
            row for row in rows if row["checkpoint_id"] == event["checkpoint_id"]
        ]
        matched = [
            row
            for row in candidates
            if all(event[key] == row["locator"][key] for key in _LOCATOR_KEYS)
        ]
        if not matched:
            raise _fail("locator_mismatch")
        by_checkpoint.setdefault(event["checkpoint_id"], []).append(event)
    return by_checkpoint


def _check_history(by_checkpoint: dict[str, list[dict[str, object]]]) -> None:
    """Encounter order must be contiguous 1..n alternating from superseded."""
    for history in by_checkpoint.values():
        for index, event in enumerate(history):
            expected_revision = index + 1
            expected_state = "superseded" if expected_revision % 2 == 1 else "active"
            if event["revision"] != expected_revision or event["state"] != expected_state:
                raise _fail("history_invalid")


def _wrappers(context: object) -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
    fields = _fields(context, ("workspace_uid", "entries", "transitions"))
    workspace_uid = _workspace_uid(fields["workspace_uid"])
    entries = _exact_list(fields["entries"])
    transitions = _exact_list(fields["transitions"])

    # Phase 1: all known syntax across every argument, before any decision.
    rows = [_wrapper_row(wrapper) for wrapper in entries]
    events = [_event(transition) for transition in transitions]

    # Phase 2: all workspace, recorded, locator and event binding.
    _bind_rows(rows, workspace_uid)
    by_checkpoint = _bind_transitions(events, rows, workspace_uid)

    # Phase 3: slot and checkpoint uniqueness, then sequence and parity.
    _check_uniqueness(rows)
    _check_history(by_checkpoint)

    for row in rows:
        checkpoint_id = row["checkpoint_id"]
        history = by_checkpoint.get(checkpoint_id) if checkpoint_id else None
        if history:
            row["state"] = history[-1]["state"]
            row["revision"] = history[-1]["revision"]
            row["transitions"] = history
        else:
            row["state"] = "active"
            row["revision"] = 0
            row["transitions"] = []
    return workspace_uid, rows, events


def project_active_entries(context: object, /) -> list[object]:
    """Return the exact opaque entry objects that are currently active."""
    _workspace, rows, _events = _wrappers(context)
    return [row["entry"] for row in rows if row["state"] == "active"]


def build_audit_view(context: object, /) -> dict[str, object]:
    """Complete detached audit metadata with opaque entry identity preserved."""
    workspace_uid, rows, _events = _wrappers(context)
    entries: list[dict[str, object]] = []
    for row in rows:
        recorded = row["recorded"]
        entries.append(
            {
                "locator": dict(row["locator"]),
                "checkpoint_id": row["checkpoint_id"],
                "entry": row["entry"],
                "recorded": None if recorded is None else dict(recorded),
                "state": row["state"],
                "revision": row["revision"],
                "transitions": [
                    {
                        key: dict(event[key]) if key == "reason" else event[key]
                        for key in _EVENT_KEYS
                    }
                    for event in row["transitions"]
                ],
            }
        )
    return {"workspace_uid": workspace_uid, "entries": entries}
