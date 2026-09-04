"""In-memory adapter between stored documents and the pure D5 contract.

This module owns nothing durable. It constructs no ``Store``, performs no IO,
opens no transaction, reads no clock and allocates no counter or event id. The
service loads the documents inside its existing transaction and passes them in;
everything here is a pure rearrangement of those values into the exact context
shapes ``workstack.checkpoint_transition`` already validates.

Two rules shape the whole file:

* **Nothing is filtered before the pure contract has validated everything.**
  Date, Task and window filtering are the caller's business and happen only
  after a whole-history projection succeeds, so a malformed record anywhere is
  refused rather than silently skipped.
* **Multiplicity and identity are preserved.** Every physical Worklog entry
  becomes exactly one wrapper in physical order, and every recognized recorded
  or transition record is carried through. Nothing is collapsed into a dict
  keyed by checkpoint, no malformed recognized record is dropped, no orphan is
  discarded, no history is re-sorted and no last-record-wins rule is applied:
  those are precisely the decisions the pure contract must be allowed to make.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import date as _date
from typing import Any, Iterable, Mapping

from .checkpoint_transition import (
    CheckpointTransitionError,
    build_audit_view,
    project_active_entries,
    verify_locator,
)
from .storage.canonical import canonical_json_bytes

__all__ = [
    "RECORDED_TYPE",
    "TRANSITION_TYPES",
    "build_projection_context",
    "build_audit",
    "active_worklog_document",
    "physical_locator_for",
]

RECORDED_TYPE = "worklog.recorded"
TRANSITION_TYPES = ("worklog.superseded", "worklog.restored")

# The pure contract's own canonical Task identifier shape, mirrored so an
# unrecorded opaque row is not tightened into a known identity it never had.
_TASK_ID = re.compile(r"T-[0-9]{4,}")

_ENTRY_FIELDS = ("task_id", "task", "done", "next", "blockers")


def _refuse(code: str) -> CheckpointTransitionError:
    """A content-free refusal carrying one of the pure contract's codes."""

    return CheckpointTransitionError(code)


def _recognized_records(records: Iterable[Any]) -> list[tuple[str, Any]]:
    """Every record the product recognizes by type, in stored order.

    Recognition is by the OUTER Activity type alone. Nothing is filtered here:
    a recognized record that turns out to be unusable is refused later, never
    dropped, because dropping it would turn a refusal into a silent omission.
    """

    found: list[tuple[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        outer = record.get("type")
        if outer in (RECORDED_TYPE,) + TRANSITION_TYPES:
            found.append((outer, copy.deepcopy(record.get("details"))))
    return found


def _validate_fact_syntax(details: dict[str, Any]) -> None:
    """Full recorded-fact syntax, decided by the FROZEN policy.

    ``verify_locator`` validates the complete recorded schema, so every field
    name, type, pattern, range and calendar rule is the pure contract's, not a
    copy of it. It is called SELF-CONSISTENTLY -- the fact's own workspace and
    checkpoint, and a locator rebuilt from the fact's own claimed coordinates --
    so the only thing that can fail here is syntax. Workspace binding is a
    separate, later phase, which is what keeps a binding fault from masking a
    malformed record elsewhere.

    Every value is read with ``get``: a missing required key must reach the
    contract's content-free refusal, never a KeyError.
    """

    verify_locator({
        "workspace_uid": details.get("workspace_uid"),
        "checkpoint_id": details.get("checkpoint_id"),
        "recorded": details,
        "actual_locator": {
            "workspace_uid": details.get("workspace_uid"),
            "task_id": details.get("task_id"),
            "date": details.get("date"),
            "ordinal": details.get("ordinal"),
            "entry_digest": details.get("entry_digest"),
        },
    })


def _validate_event_syntax(details: dict[str, Any], workspace_uid: str) -> None:
    """Full transition-event syntax, decided by the FROZEN policy.

    ``build_audit_view`` validates every supplied event's syntax in its first
    phase, before it binds anything, so running it over this one event with no
    rows reaches exactly that check. With no rows the event cannot bind, so a
    syntactically sound event always ends in a binding or history verdict; only
    a ``malformed`` verdict is about the event's own syntax, and only that is
    re-raised here.

    NOTE, reported rather than worked around: the module exposes no public seam
    that validates ONE transition event's syntax on its own. Its ``_event``
    validator (workstack/checkpoint_transition.py:333) is private, and
    ``build_transition_notice`` additionally demands an attributed origin and
    parity, so it cannot validate an ordinary null-origin event. This probe uses
    the frozen policy to make the decision rather than restating its rules here.
    """

    try:
        build_audit_view(
            {"workspace_uid": workspace_uid, "entries": [], "transitions": [details]}
        )
    except CheckpointTransitionError as error:
        if error.code == "malformed":
            raise


def _validate_recognized_syntax(
    recognized: list[tuple[str, Any]], workspace_uid: str
) -> None:
    """ALL known syntax, for EVERY recognized record, before any binding.

    This runs to completion over the whole list first, so a malformed record
    anywhere outranks a binding fault anywhere, which is the frozen global
    precedence: syntax, then binding, then history.
    """

    for outer, details in recognized:
        if type(details) is not dict or not details:
            raise _refuse("malformed")
        if details.get("type") != outer:
            # An outer Activity type that disagrees with its own payload is a
            # forged or corrupted record, not a record about something else.
            raise _refuse("malformed")
        if outer == RECORDED_TYPE:
            _validate_fact_syntax(details)
        else:
            _validate_event_syntax(details, workspace_uid)


def _validate_recognized_binding(
    recognized: list[tuple[str, Any]], workspace_uid: str
) -> None:
    """ALL binding, after all syntax and before any association decision.

    Syntax has already run, so every required key is present and well shaped
    and can be read directly.
    """

    for outer, details in recognized:
        if details["workspace_uid"] != workspace_uid:
            raise _refuse("locator_mismatch")


def _validate_worklog_envelope(worklog: Mapping[str, Any]) -> None:
    """The known physical envelope: day keys, day objects and entry lists."""

    days = worklog.get("days")
    if days is None:
        return
    if not isinstance(days, Mapping):
        raise _refuse("malformed")
    for date, day in days.items():
        if type(date) is not str:
            raise _refuse("malformed")
        try:
            parsed = _date.fromisoformat(date)
        except (TypeError, ValueError) as error:
            raise _refuse("malformed") from error
        if parsed.isoformat() != date:
            raise _refuse("malformed")
        if not isinstance(day, Mapping):
            raise _refuse("malformed")
        if "entries" in day and type(day["entries"]) is not list:
            # A present-but-unusable entries value is a malformed known
            # envelope. Only an absent key means "this day holds none".
            raise _refuse("malformed")


def _entry_digest(entry: Any) -> str | None:
    """The canonical digest of one stored entry, or None if it cannot be one."""

    try:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(entry)).hexdigest()
    except (TypeError, ValueError):
        return None


def _physical_task_id(entry: Any) -> str | None:
    """The Task coordinate of the ACTUAL physical row, or None if unusable.

    This is read from the stored entry and never from a recorded fact's claim.
    A fact claiming a different Task than the row it points at is a locator
    mismatch, not a relabelling of the row: copying the claim here would let a
    corrupted fact suppress one Task's entry while appearing to compensate
    another.

    A row nobody recorded is opaque, so its ``task_id`` may be a number, a
    boolean or a historical non-canonical string. Those become null known
    coordinates rather than being forced into a canonical shape, and the entry
    object itself is never altered.
    """

    if not isinstance(entry, Mapping):
        return None
    task_id = entry.get("task_id")
    if type(task_id) is not str or _TASK_ID.fullmatch(task_id) is None:
        return None
    return task_id


def _physical_rows(worklog: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every physical Worklog entry, in date then stored order, with its ordinal.

    The ordinal is the entry's date-local physical index, which is the same
    number the recorded fact froze. Days are visited in sorted date order purely
    so the result is deterministic; no entry is reordered inside its day and no
    day is dropped, including a day whose entries are all superseded. The
    envelope has already been validated, so nothing is skipped here.
    """

    rows: list[dict[str, Any]] = []
    days = worklog.get("days")
    if not isinstance(days, Mapping):
        return rows
    for date in sorted(days):
        entries = days[date].get("entries")
        if entries is None:
            # Absent, not malformed: the envelope check already refused
            # every present-but-unusable value.
            continue
        for ordinal, entry in enumerate(entries):
            rows.append({"date": date, "ordinal": ordinal, "entry": entry})
    return rows


def _claims_by_slot(
    recognized: list[tuple[str, Any]], rows: list[dict[str, Any]]
) -> tuple[dict[tuple, list[Any]], list[Any]]:
    """Group recorded facts by the physical slot each one claims.

    This decides NOTHING. It neither merges two facts that claim one slot nor
    prefers either of them; it only reports what each fact points at, so the
    frozen policy can compare every recorded and event binding before any
    uniqueness or sequence rule is allowed to speak.

    Facts pointing at a slot that does not exist are returned separately,
    because there is no wrapper that could carry them.
    """

    slots = {(row["date"], row["ordinal"]) for row in rows}
    claims: dict[tuple, list[Any]] = {}
    orphans: list[Any] = []
    for outer, details in recognized:
        if outer != RECORDED_TYPE:
            continue
        slot = (details["date"], details["ordinal"])
        if slot in slots:
            claims.setdefault(slot, []).append(details)
        else:
            orphans.append(details)
    return claims, orphans


def build_projection_context(
    *,
    workspace_uid: str,
    worklog: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> dict[str, Any]:
    """The exact context the pure whole-history functions consume."""

    records = activity.get("activity")
    records = records if isinstance(records, list) else []
    recognized = _recognized_records(records)

    # Frozen global precedence: all known syntax first, across every record and
    # the physical envelope, and only then any association.
    _validate_recognized_syntax(recognized, workspace_uid)
    _validate_worklog_envelope(worklog)
    _validate_recognized_binding(recognized, workspace_uid)

    rows = _physical_rows(worklog)
    claims, orphans = _claims_by_slot(recognized, rows)

    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = row["entry"]
        locator = {
            "workspace_uid": workspace_uid,
            # Always the physical row, recorded or not. A recorded fact that
            # claims a different Task will fail the frozen locator comparison
            # rather than rename the row.
            "task_id": _physical_task_id(entry),
            "date": row["date"],
            "ordinal": row["ordinal"],
            "entry_digest": _entry_digest(entry),
        }
        # One wrapper per CLAIM, all carrying the row's ACTUAL physical
        # coordinates. A doubly claimed row therefore reaches the frozen policy
        # as two wrappers on the same real slot: every recorded and event
        # binding is compared first, and only then does its uniqueness rule
        # report the duplicate. Nothing is merged, dropped or given a fabricated
        # ordinal to slip past that rule, and a row nobody claimed still
        # produces exactly one wrapper with a null recorded fact.
        for recorded in claims.get((row["date"], row["ordinal"]), [None]):
            entries.append(
                {"locator": dict(locator), "recorded": recorded, "entry": entry}
            )

    context = {
        "workspace_uid": workspace_uid,
        "entries": entries,
        "transitions": [
            details for outer, details in recognized if outer in TRANSITION_TYPES
        ],
    }

    if orphans:
        # A fact pointing at a slot that does not exist cannot be carried by any
        # wrapper, so the frozen policy can never see it. Before reporting that
        # association failure, hand it everything that IS representable, so a
        # binding fault anywhere still outranks this verdict.
        build_audit_view(context)
        raise _refuse("history_invalid")
    return context


def build_audit(
    *,
    workspace_uid: str,
    worklog: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> dict[str, Any]:
    """The frozen complete audit view over the WHOLE validated history."""

    return build_audit_view(
        build_projection_context(
            workspace_uid=workspace_uid, worklog=worklog, activity=activity
        )
    )


def active_worklog_document(
    *,
    workspace_uid: str,
    worklog: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> dict[str, Any]:
    """The Worklog every active reader sees, with superseded entries removed.

    The whole history is validated first; only then are entries filtered. Day
    metadata is preserved verbatim and a day whose entries are all superseded
    remains present with an empty list, because an absent day and an empty day
    are different facts. Physical writers keep loading the raw document, so
    future ordinals and first-for-task still count superseded rows.
    """

    active = project_active_entries(
        build_projection_context(
            workspace_uid=workspace_uid, worklog=worklog, activity=activity
        )
    )
    identities = {id(entry) for entry in active}
    projected = copy.deepcopy(dict(worklog))
    days = projected.get("days")
    if not isinstance(days, Mapping):
        return projected
    source_days = worklog.get("days")
    for date, day in days.items():
        source_day = source_days[date] if isinstance(source_days, Mapping) else None
        source_entries = source_day.get("entries") if isinstance(source_day, Mapping) else None
        if not isinstance(day, Mapping) or not isinstance(source_entries, list):
            continue
        day["entries"] = [
            copy.deepcopy(entry)
            for entry in source_entries
            if id(entry) in identities
        ]
    return projected


def physical_locator_for(
    *,
    workspace_uid: str,
    checkpoint_id: str,
    worklog: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Derive the ACTUAL physical locator and recorded fact for one checkpoint.

    The locator is recomputed from the stored Worklog row rather than copied
    from the recorded fact, so recorded metadata is never treated as evidence
    of the physical row it claims. The audit is built first, which means the
    whole history has already been validated before this lookup binds anything.
    """

    audit = build_audit(
        workspace_uid=workspace_uid, worklog=worklog, activity=activity
    )
    matches = [
        entry for entry in audit["entries"] if entry["checkpoint_id"] == checkpoint_id
    ]
    if len(matches) != 1:
        raise CheckpointTransitionError("locator_mismatch")
    match = matches[0]
    return dict(match["locator"]), match
