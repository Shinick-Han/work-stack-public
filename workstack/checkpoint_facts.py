"""The one recorded checkpoint a saved Task resumes from, as pure facts.

This is the Python half of a behaviour that already exists and is already
tested in the GUI. ``frontend/src/features/tasks/taskResumeFacts.ts`` and
``frontend/src/domain/checkpointEntrySummary.ts`` are the oracle; this module
reproduces their selection, their summary semantics and their `version`
identity exactly, and ``contracts/checkpoint-facts-v1/cases.json`` locks the two
implementations to the same answers. When the two disagree the GUI is right.

Nothing here performs IO. It takes an audit snapshot the caller already read,
and returns a detached mapping. It never fetches a second snapshot, opens the
worklog file, resolves a Task, calls a provider or writes anything.

Three rules carry the honesty of the surface, and they are the GUI's own.

ONE RECORD. Done, Next and Blockers always come from the same checkpoint. A
newer record that cleared its blockers is the author saying nothing is in the
way; an older nonempty list is never carried over it.

THE LATEST IS THE LATEST. If the newest active record cannot be read, that is
reported as an unreadable latest. An older readable entry is never promoted
into its place, because a reader acting on a stale next step is worse off than
one told the record is broken.

THIS TASK ONLY. Selection is bound to the requested workspace and Task. A
legacy row predating checkpoint identity has no Task in its locator at all; it
is still offered, but its weaker binding is declared rather than hidden.

What the summary could not present is COUNTED and not reproduced. The recovered
Done/Next/Blockers text and the recorded Task title are stored, untrusted text
that may legitimately contain a URL or a path someone typed; it is preserved
verbatim and fenced, which is not permission to fetch or open it. No unknown
field value, raw audit fragment, transition history or filesystem metadata
leaves this module.
"""

from __future__ import annotations

import re
from datetime import date as _calendar_date
from typing import Any

from .checkpoint_facts_format import (
    CHECKPOINT_FACTS_CONTRACT,
    FACTS_MAX_BYTES,
    FORMAT_CHOICES,
    JSON_FORMAT,
    MARKDOWN_FORMAT,
    REASON_CODES,
    SUCCESS_STATUSES,
    checkpoint_facts_json,
    checkpoint_facts_markdown,
)

__all__ = (
    "CHECKPOINT_FACTS_CONTRACT",
    "FACTS_MAX_BYTES",
    "FORMAT_CHOICES",
    "JSON_FORMAT",
    "MARKDOWN_FORMAT",
    "REASON_CODES",
    "SUCCESS_STATUSES",
    "CheckpointFactsError",
    "project_checkpoint_facts",
    "render_checkpoint_facts",
    "validate_checkpoint_request",
)

# The same canonical spelling ordinary admission already requires: lowercase,
# hyphenated, a real RFC 4122 version and variant, and therefore never the nil
# UUID. Kept as this module's own literal so the frozen Agent CLI contract is
# not reached into.
_WORKSPACE_UID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_TASK_ID = re.compile(r"T-[0-9]{4,}")
_CHECKPOINT_ID = re.compile(r"CP-[0-9a-f]{64}")
_ENTRY_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MAX_SAFE_INTEGER = 9007199254740991
_STATES = ("active", "superseded")
_LOCATOR_FIELDS = ("workspace_uid", "task_id", "date", "ordinal", "entry_digest")
_KNOWN_ENTRY_FIELDS = ("task_id", "task", "done", "next", "blockers")

# ECMAScript WhiteSpace plus LineTerminator, so a blank check here agrees with
# the oracle's `String.prototype.trim` on every character, not just on ASCII.
# Python's own str.strip() would additionally eat U+0085, which JavaScript keeps.
_JS_BLANK = "".join(
    chr(code)
    for code in (
        0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680,
        *range(0x2000, 0x200B),
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
    )
)

_MESSAGES = {
    "invalid_request": "the checkpoint request is invalid",
    "invalid_audit": "the checkpoint audit cannot be read unambiguously",
    "invalid_facts": "the checkpoint facts could not be rendered",
    "output_too_large": "the checkpoint facts exceed the 32768 byte output bound",
}


class CheckpointFactsError(ValueError):
    """A content-free refusal.

    The message is one of this module's own fixed literals and the code is a
    closed label. No submitted value, payload fragment, position or path is
    ever interpolated, so a failure cannot become a side channel for the audit
    it refused.
    """

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code


def _refuse(code: str) -> CheckpointFactsError:
    return CheckpointFactsError(code)


def _pattern(value: object, pattern: re.Pattern[str], code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _refuse(code)
    return value


def _optional_pattern(
    value: object, pattern: re.Pattern[str], code: str
) -> str | None:
    return None if value is None else _pattern(value, pattern, code)


def _bounded_int(value: object) -> int:
    # ``type(...) is not int`` rejects True/False: a JSON boolean is not an
    # ordinal, and bool is an int subclass.
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise _refuse("invalid_audit")
    return value


def validate_checkpoint_request(*, workspace_uid: object, task_id: object) -> None:
    """Accept only a canonical workspace UUID and an ASCII Task ID.

    This is request syntax alone. It proves nothing about existence, ownership
    or admission, and it reads nothing.
    """

    _pattern(workspace_uid, _WORKSPACE_UID, "invalid_request")
    _pattern(task_id, _TASK_ID, "invalid_request")


def _blank(value: str) -> bool:
    return value.strip(_JS_BLANK) == ""


def _text_value(value: object) -> str | None:
    """The oracle's `textValue`: a nonblank string, or nothing at all."""

    return value if type(value) is str and not _blank(value) else None


def _text_list(value: object) -> list[str]:
    """Tolerates the legacy single-string form and drops blank slots.

    Whatever this does not return is counted by :func:`_extras_count`, never
    silently lost.
    """

    single = _text_value(value)
    if single is not None:
        return [single]
    if type(value) is not list:
        return []
    return [item for item in value if _text_value(item) is not None]


def _carries_nothing(value: object) -> bool:
    """A WHOLE field that is missing, null or blank never happened."""

    return value is None or (type(value) is str and _blank(value))


def _fact_field_extras(value: object) -> int:
    """Every list SLOT the bullets did not take, holes and blanks included."""

    # A legacy single string became the one bullet; nothing is left over.
    if _carries_nothing(value) or type(value) is str:
        return 0
    if type(value) is not list:
        return 1
    return sum(1 for item in value if _text_value(item) is None)


def _extras_count(source: dict[str, Any]) -> int:
    """How many values the readable summary could not present.

    Only the COUNT is kept. The labels and values stay inside the audit the
    caller already holds; reproducing them here would publish exactly the
    unknown-field content this view refuses to carry.
    """

    total = 0
    for key, value in source.items():
        if key not in _KNOWN_ENTRY_FIELDS:
            # An unknown key is itself the finding, so it counts even when its
            # value is empty.
            total += 1
        elif key in ("task_id", "task"):
            if not _carries_nothing(value) and _text_value(value) is None:
                total += 1
        else:
            total += _fact_field_extras(value)
    return total


class _Summary:
    """The oracle's CheckpointEntrySummary, minus the values it never emits."""

    def __init__(
        self,
        *,
        readable: bool,
        task_id: str | None = None,
        task_title: str | None = None,
        done: list[str] | None = None,
        next_items: list[str] | None = None,
        blockers: list[str] | None = None,
        extras: int = 0,
    ) -> None:
        self.readable = readable
        self.task_id = task_id
        self.task_title = task_title
        self.done = done or []
        self.next = next_items or []
        self.blockers = blockers or []
        self.extras = extras


def _summarize_entry(entry: object) -> _Summary:
    """Readable projection of the opaque worklog payload; total, never raising.

    A malformed, legacy or hostile shape resolves to a summary. The one
    asymmetry is deliberate and is the oracle's: an absent field says nothing,
    while a slot inside a list is a position its author wrote.
    """

    if type(entry) is list:
        return _Summary(readable=False, extras=len(entry))
    if type(entry) is not dict:
        # None, a legacy free-text row and a bare scalar are all already their
        # own last-resort text, and none of them carries a readable field.
        return _Summary(readable=False)
    summary = _Summary(
        readable=True,
        task_id=_text_value(entry.get("task_id")),
        task_title=_text_value(entry.get("task")),
        done=_text_list(entry.get("done")),
        next_items=_text_list(entry.get("next")),
        blockers=_text_list(entry.get("blockers")),
        extras=_extras_count(entry),
    )
    recovered = (
        summary.task_id is not None
        or summary.task_title is not None
        or summary.done
        or summary.next
        or summary.blockers
    )
    if recovered:
        return summary
    # No known field survived, so the whole payload is leftover: unreadable,
    # with every field still counted.
    return _Summary(readable=False, extras=summary.extras)


def _locator(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("locator")
    if type(value) is not dict or set(value) != set(_LOCATOR_FIELDS):
        raise _refuse("invalid_audit")
    return {
        "workspace_uid": _pattern(value["workspace_uid"], _WORKSPACE_UID, "invalid_audit"),
        "task_id": _optional_pattern(value["task_id"], _TASK_ID, "invalid_audit"),
        "date": _audit_date(value["date"]),
        "ordinal": _bounded_int(value["ordinal"]),
        "entry_digest": _optional_pattern(
            value["entry_digest"], _ENTRY_DIGEST, "invalid_audit"
        ),
    }


def _audit_date(value: object) -> str:
    """A real calendar day in the one spelling that also sorts as text."""

    text = _pattern(value, _DATE, "invalid_audit")
    try:
        _calendar_date.fromisoformat(text)
    except ValueError as error:
        raise _refuse("invalid_audit") from error
    return text


def _origin(entry: dict[str, Any]) -> str | None:
    recorded = entry.get("recorded")
    if recorded is None:
        return None
    if type(recorded) is not dict:
        raise _refuse("invalid_audit")
    origin = recorded.get("origin")
    if origin is not None and type(origin) is not str:
        raise _refuse("invalid_audit")
    return origin


def _audit_entry(value: object) -> dict[str, Any]:
    """One audit row, with every field selection depends on proved.

    The opaque `entry` payload is deliberately NOT validated: a malformed
    payload is a real recorded state and maps through the summary semantics
    above. Everything the selection itself reads — which workspace, which Task,
    which day, which slot, active or superseded — must be unambiguous or this
    refuses rather than guessing.
    """

    if type(value) is not dict:
        raise _refuse("invalid_audit")
    state = value.get("state")
    if state not in _STATES:
        raise _refuse("invalid_audit")
    return {
        "locator": _locator(value),
        "checkpoint_id": _optional_pattern(
            value.get("checkpoint_id"), _CHECKPOINT_ID, "invalid_audit"
        ),
        "entry": value.get("entry"),
        "origin": _origin(value),
        "state": state,
        "revision": _bounded_int(value.get("revision")),
    }


def _audit_entries(audit: object, workspace_uid: str) -> list[dict[str, Any]]:
    """Validate the snapshot envelope and every row it carries.

    Unrelated extra top-level audit fields are ignored and never serialized.
    The envelope's own workspace binding is checked, because a snapshot taken
    for another workspace is not a snapshot of this one, however empty it would
    look after per-row filtering.
    """

    if type(audit) is not dict:
        raise _refuse("invalid_audit")
    if audit.get("workspace_uid") != workspace_uid:
        raise _refuse("invalid_audit")
    entries = audit.get("entries")
    if type(entries) is not list:
        raise _refuse("invalid_audit")
    return [_audit_entry(entry) for entry in entries]


def _binding(entry: dict[str, Any], task_id: str) -> str | None:
    """The Task binding, or nothing when this row belongs to another Task.

    A recorded row names its Task in the locator and that is the end of it.
    Only a legacy row falls back to the claim inside its own payload, which is
    the only place that era stored the Task at all.
    """

    locator_task = entry["locator"]["task_id"]
    if locator_task is not None:
        return "locator" if locator_task == task_id else None
    claimed = _summarize_entry(entry["entry"]).task_id
    return "entry-payload" if claimed == task_id else None


def _is_later(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    """A later recorded day, then a later slot within that day.

    ISO-8601 days sort correctly as text and were already proved to be real
    calendar days. Checkpoint identifiers are never parsed for order.
    """

    left, right = candidate["locator"], incumbent["locator"]
    if left["date"] != right["date"]:
        return left["date"] > right["date"]
    return left["ordinal"] > right["ordinal"]


class _Records:
    """What one pass over the snapshot found for this workspace and Task."""

    def __init__(self) -> None:
        #: The selected row and the binding it was selected through, together,
        #: so a record can never be reported under a binding it did not match.
        self.latest: tuple[dict[str, Any], str] | None = None
        self.active = 0
        self.superseded = 0


def _collect(entries: list[dict[str, Any]], workspace_uid: str, task_id: str) -> _Records:
    """One pass over the snapshot.

    The workspace filter is applied per row rather than trusting the envelope,
    so a row that somehow belongs to another workspace contributes nothing
    instead of leaking into this Task.
    """

    records = _Records()
    for entry in entries:
        if entry["locator"]["workspace_uid"] != workspace_uid:
            continue
        binding = _binding(entry, task_id)
        if binding is None:
            continue
        if entry["state"] == "superseded":
            records.superseded += 1
            continue
        records.active += 1
        if records.latest is None or _is_later(entry, records.latest[0]):
            records.latest = (entry, binding)
    return records


def _provenance(
    entry: dict[str, Any], binding: str, summary: _Summary
) -> dict[str, Any]:
    """Identity of the selected record. Every field is stored, never derived."""

    return {
        "checkpoint_id": entry["checkpoint_id"],
        "entry_digest": entry["locator"]["entry_digest"],
        "date": entry["locator"]["date"],
        "ordinal": entry["locator"]["ordinal"],
        "revision": entry["revision"],
        "origin": entry["origin"],
        "binding": binding,
        "recorded_task_id": summary.task_id,
        "recorded_task_title": summary.task_title,
    }


def _version(
    status: str, workspace_uid: str, task_id: str, provenance: dict[str, Any] | None
) -> str:
    """The freshness identity a prepared brief is frozen against.

    Byte-for-byte the GUI's own `version`. It is built only from THIS Task's
    selected record, so another Task's activity cannot spuriously stale a
    brief, while a new checkpoint on this Task changes the identity even though
    the Task's own revision did not move.
    """

    binding = "v1:{}:{}:{}".format(status, workspace_uid, task_id)
    if provenance is None:
        return binding
    identity = provenance["checkpoint_id"]
    if identity is None:
        identity = "legacy@{}#{}".format(provenance["date"], provenance["ordinal"])
    digest = provenance["entry_digest"] or "no-digest"
    return "{}:{}:{}:r{}".format(binding, identity, digest, provenance["revision"])


def _reasons(summary: _Summary, binding: str, task_id: str) -> list[str]:
    """The fixed-order explanation of what is missing, never of what it said.

    A reason may explain partial information; it never alters the status or the
    version, and it never carries a value recovered from the payload.
    """

    codes: list[str] = []
    if summary.extras > 0:
        codes.append("unpresented_values")
    disputed = (
        binding == "locator"
        and summary.task_id is not None
        and summary.task_id != task_id
    )
    if disputed:
        codes.append("recorded_task_mismatch")
    if not summary.readable:
        codes.append("no_readable_summary")
    return codes


def _facts(
    *,
    status: str,
    workspace_uid: str,
    task_id: str,
    records: _Records,
    provenance: dict[str, Any] | None = None,
    summary: _Summary | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    read = summary or _Summary(readable=True)
    return {
        "contract": CHECKPOINT_FACTS_CONTRACT,
        "status": status,
        "workspace_uid": workspace_uid,
        "task_id": task_id,
        "provenance": provenance,
        "done": list(read.done),
        "next": list(read.next),
        "blockers": list(read.blockers),
        "reasons": reasons or [],
        "active_record_count": records.active,
        "superseded_record_count": records.superseded,
        "version": _version(status, workspace_uid, task_id, provenance),
    }


def _select(workspace_uid: str, task_id: str, records: _Records) -> dict[str, Any]:
    selected = records.latest
    if selected is None:
        return _facts(
            status="empty",
            workspace_uid=workspace_uid,
            task_id=task_id,
            records=records,
        )
    entry, binding = selected
    summary = _summarize_entry(entry["entry"])
    provenance = _provenance(entry, binding, summary)
    reasons = _reasons(summary, binding, task_id)
    if not summary.readable:
        # The newest record stays the newest record. History is still reachable
        # through the audit; an older entry is never relabelled as the latest,
        # and none of its text is presented here.
        return _facts(
            status="unreadable",
            workspace_uid=workspace_uid,
            task_id=task_id,
            records=records,
            provenance=provenance,
            reasons=reasons,
        )
    partial = "unpresented_values" in reasons or "recorded_task_mismatch" in reasons
    return _facts(
        status="partial" if partial else "ready",
        workspace_uid=workspace_uid,
        task_id=task_id,
        records=records,
        provenance=provenance,
        summary=summary,
        reasons=reasons,
    )


def project_checkpoint_facts(
    audit: object, *, workspace_uid: str, task_id: str
) -> dict[str, object]:
    """The resume snapshot for one workspace and one saved Task.

    Deterministic: the same snapshot and the same binding always produce the
    same facts and the same `version`. A syntactically valid Task with no
    matching active record returns the empty snapshot — never an invented
    not-found result, and never an older record standing in for a newer one.
    """

    validate_checkpoint_request(workspace_uid=workspace_uid, task_id=task_id)
    entries = _audit_entries(audit, workspace_uid)
    return _select(workspace_uid, task_id, _collect(entries, workspace_uid, task_id))


def render_checkpoint_facts(
    facts: dict[str, object], *, format: str = JSON_FORMAT
) -> str:
    """The complete document, or nothing at all.

    The byte bound is checked on the finished text, so an oversized answer
    fails before a caller can write a partial one. Facts are never truncated,
    dropped or summarized further to fit.
    """

    if format not in FORMAT_CHOICES:
        raise _refuse("invalid_facts")
    try:
        if format == JSON_FORMAT:
            text = checkpoint_facts_json(facts)
        else:
            text = checkpoint_facts_markdown(facts)
        encoded = text.encode("utf-8")
    except ValueError as error:
        raise _refuse("invalid_facts") from error
    if len(encoded) > FACTS_MAX_BYTES:
        raise _refuse("output_too_large")
    return text
