"""Task note append and planning-status owner-write forwarding.

Same-owner Task reads, revision CAS, and projected-Task validation for
the note and status routes live here. Subtask commands have their own
collaborator so this file stays within the line limit.
"""

from __future__ import annotations

import datetime
from typing import Mapping, Sequence
from urllib.parse import quote

from .cli_writer_owner import (
    CoordinatesReader,
    RequestJson,
    WriterTransportError,
    _canonical_workspace_uid,
    _preflight_get,
)
from .cli_writer_transport import _forward_write
from .outcome_write_invariant import serialize_task_patch
from .store import MAX_REVISION

TASKS_PATH = "/api/v1/tasks"
# The four planning statuses the supported service accepts.
TASK_STATUS_VALUES = ("open", "started", "done", "dropped")
# Fields the supported projection adds to every Task it returns, so a
# success that lacks them is not a complete Task.
PROJECTED_TASK_FIELDS = ("scheduled", "estimate_minutes", "context_count")
# The exclusive-local Task note record, in the order the local path prints it.
LEGACY_TASK_NOTE_FIELDS = ("date", "text")


def _same_json(left: object, right: object) -> bool:
    """Structural JSON equality that keeps booleans distinct from numbers.

    ``==`` alone is not enough for a historical record: Python treats
    ``True == 1`` and ``False == 0``, at every nesting level, so a reply that
    turned a stored JSON boolean into a number would compare equal and pass
    as unchanged history.

    Only that distinction is added. Every other value keeps the comparison it
    already had, so two legitimate numbers that are equal in JSON terms still
    match: this is not a numeric canonicalization or int-versus-float policy.
    """

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_same_json(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json(one, other) for one, other in zip(left, right)
        )
    if isinstance(left, (Mapping, list)) or isinstance(right, (Mapping, list)):
        return False
    return left == right


def _task_note_baseline(task: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """The ordered note baseline, preserved whole.

    An ABSENT ``notes`` field is an empty baseline. An explicitly null field is
    not absence: it is a shape the owner should never send, so it refuses here
    rather than being silently read as empty and letting a write through. Any
    other non-list shape refuses for the same reason.

    Historical records are kept exactly as the owner returned them, including
    supported additional fields. The local append preserves the whole existing
    list, so imposing the new-record two-key shape on history would refuse
    legitimate data. Only the appended record is held to the created-note shape.
    """

    if "notes" not in task:
        return ()
    records = task.get("notes")
    if not isinstance(records, list):
        raise WriterTransportError("Work Stack server returned an invalid task note list")
    baseline = []
    for record in records:
        if not isinstance(record, dict):
            raise WriterTransportError("Work Stack server returned an invalid task note")
        baseline.append(dict(record))
    return tuple(baseline)


def _valid_created_note(record: Mapping[str, object], expected_text: str) -> bool:
    """The appended record must be the legitimate local {date, text} shape.

    The date is checked as a real ISO calendar date, so an empty string or an
    impossible day such as 2026-02-30 is refused rather than printed. It is
    never compared with, or substituted by, a client clock: the owner's own
    date is preserved exactly when it is well formed.
    """

    if set(record) != set(LEGACY_TASK_NOTE_FIELDS):
        return False
    date = record.get("date")
    if not isinstance(date, str):
        return False
    try:
        parsed = datetime.date.fromisoformat(date)
    except ValueError:
        return False
    # fromisoformat also accepts other ISO forms; require the canonical date.
    if parsed.isoformat() != date:
        return False
    return record.get("text") == expected_text


def _task_detail(
    request_json: RequestJson,
    host: str,
    port: int,
    normalized_id: str,
    *,
    require_advance: bool = True,
) -> dict[str, object]:
    """Read one Task from the same owner. Part of preflight, never retried.

    ``require_advance`` is the caller's own statement about whether its write
    needs a next revision. An append always does, so it stays the default; a
    caller that may turn out to be a no-op asks for the read without that
    guard and refuses for itself once it knows.
    """

    detail = _preflight_get(
        request_json,
        host,
        port,
        "{}/{}".format(TASKS_PATH, quote(normalized_id, safe="")),
        "task",
    )
    task = detail.get("task")
    if not isinstance(task, dict):
        raise WriterTransportError("Work Stack server returned an invalid task response")
    if task.get("id") != normalized_id:
        raise WriterTransportError("the running Work Stack server returned a different task")
    uid = _canonical_workspace_uid(task.get("uid"))
    revision = task.get("revision")
    # `type(...) is not int` rather than isinstance, so a bool cannot pass as a
    # revision. The supported range is the product's own.
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise WriterTransportError("Work Stack server returned an unsupported task revision")
    if require_advance and revision == MAX_REVISION:
        # The next revision is not representable, so refuse before the POST
        # rather than sending a write the owner must reject.
        raise WriterTransportError(
            "the task revision cannot advance beyond the safe integer limit"
        )
    return {
        "id": normalized_id,
        "uid": uid,
        "revision": revision,
        # The owner's own projected planning status. Callers that do not need
        # it ignore it; the status route validates it before it writes.
        "status": task.get("status"),
        # The field names this Task actually carried on the same-owner read.
        # A status-only write does not legitimately drop any of them, so the
        # response is checked against what was really there rather than
        # against an invented universal schema.
        "fields": frozenset(task),
        # The whole record as the owner returned it. Callers that need a
        # different baseline than the note one read it from here.
        "task": task,
        "baseline": _task_note_baseline(task),
    }


def _task_note_from(
    payload: Mapping[str, object],
    normalized_id: str,
    uid: str,
    baseline: Sequence[Mapping[str, object]],
    baseline_revision: int,
    expected_text: str,
) -> dict[str, object]:
    """Project the note this invocation appended out of the updated Task.

    The appended record is identified by proving the entire ordered baseline
    survives as a prefix and exactly one record follows it, never by matching
    text and never by taking whichever record happens to be last: the same text
    may legitimately be written twice as two distinct intents.

    Success additionally requires the response to be internally consistent with
    the write that was frozen: same Task identity and UID, revision exactly the
    frozen baseline plus one, and the new record carrying this invocation's
    trimmed text with the owner's own date. There is no refetch, revision
    refresh, retry, rollback or local fallback after the POST.
    """

    invalid = "Work Stack server returned an invalid task note response"
    task = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(task, dict) or task.get("id") != normalized_id:
        raise WriterTransportError(invalid)
    if task.get("uid") != uid:
        raise WriterTransportError("the running Work Stack server returned a different task")

    revision = task.get("revision")
    if type(revision) is not int or revision != baseline_revision + 1:
        raise WriterTransportError("Work Stack server reported an impossible task revision")

    notes = _task_note_baseline(task)
    if len(notes) != len(baseline) + 1:
        raise WriterTransportError(invalid)
    if not _same_json(
        [dict(record) for record in notes[:len(baseline)]],
        [dict(record) for record in baseline],
    ):
        raise WriterTransportError(
            "Work Stack server changed an existing task note"
        )

    created = notes[-1]
    if not _valid_created_note(created, expected_text):
        raise WriterTransportError(invalid)
    return {field: created[field] for field in LEGACY_TASK_NOTE_FIELDS}


def forward_task_note(
    store: object,
    owner_state: str,
    task_id: str,
    text: str,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Append one Task note through the running owner and return the raw record.

    The Task is read once from the same owner to obtain the revision the strict
    compare-and-swap requires, so no new CLI input is introduced. The identifier
    is normalized the way the local lookup normalizes it and is URL-encoded
    rather than interpolated. Output stays the legacy {date, text} pair with no
    Task envelope, revision, UID or meta.
    """

    normalized_id = str(task_id or "").strip().upper()

    def prepare(request, host, port):
        # The local path resolves the Task before it validates the text, so an
        # unknown Task still reports first.
        detail = _task_detail(request, host, port, normalized_id)
        # Trim the ends only: internal whitespace and Unicode are preserved.
        normalized_text = str(text or "").strip()
        if not normalized_text:
            raise ValueError("text is required")
        baseline = tuple(detail["baseline"])
        baseline_revision = detail["revision"]
        uid = str(detail["uid"])
        body = {"text": normalized_text, "revision": baseline_revision}
        path = "{}/{}/notes".format(TASKS_PATH, quote(normalized_id, safe=""))
        return (
            path,
            body,
            lambda payload: _task_note_from(
                payload, normalized_id, uid, baseline, baseline_revision, normalized_text
            ),
        )

    return _forward_write(
        store,
        owner_state,
        path="{}/{}/notes".format(TASKS_PATH, quote(normalized_id, safe="")),
        body={},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=idempotency_key,
        project=lambda payload: {},
        changed_message="Work Stack server runtime metadata changed before the task note was sent",
        unknown_message="task note commit is unknown; inspect the task before retrying",
        refused_message="the running Work Stack server refused the task note (HTTP {})",
        prepare=prepare,
    )

def _complete_projected_task(
    task: Mapping[str, object], baseline_fields: frozenset[str]
) -> bool:
    """Is this success a whole projected Task rather than a fragment of one?

    Every field the same-owner read actually carried must still be present.
    ``status_fact_id`` is the one field the projection legitimately removes.
    Values are not compared: status, revision and updated_at are the write's
    own effects, and the owner's remaining values and key order pass through
    untouched. A field the baseline never had is never required or invented,
    so a legitimate legacy Task missing an optional creation field still
    succeeds.
    """

    if any(field not in task for field in PROJECTED_TASK_FIELDS):
        return False
    count = task.get("context_count")
    # A derived count cannot be negative, and `type(...) is int` keeps a bool
    # from passing as one.
    if type(count) is not int or count < 0:
        return False
    if "status_fact_id" in task:
        return False
    required = baseline_fields - {"status_fact_id"}
    return all(field in task for field in required)


def _task_status_from(
    payload: Mapping[str, object],
    normalized_id: str,
    uid: str,
    baseline_revision: int,
    target_status: str,
    no_op: bool,
    baseline_fields: frozenset[str],
) -> dict[str, object]:
    """Validate the owner's answer against the transition that was frozen.

    The owner's own record is returned unchanged, so the legacy stdout shape,
    field order and every legitimate owner value survive. A no-op keeps the
    baseline revision because the supported service returns the projected task
    without advancing it; a real transition must advance by exactly one.
    """

    invalid = "Work Stack server returned an invalid task status response"
    task = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(task, dict) or task.get("id") != normalized_id:
        raise WriterTransportError(invalid)
    if task.get("uid") != uid:
        raise WriterTransportError("the running Work Stack server returned a different task")
    revision = task.get("revision")
    if type(revision) is not int:
        raise WriterTransportError("Work Stack server reported an impossible task revision")
    if revision != (baseline_revision if no_op else baseline_revision + 1):
        raise WriterTransportError("Work Stack server reported an impossible task revision")
    if task.get("status") != target_status:
        raise WriterTransportError(invalid)
    # A success must be the owner's full projected Task, not a fragment of one.
    # These are the fields the projection guarantees on every Task it returns;
    # everything else the owner sends through, including optional and legacy
    # extras, is preserved untouched and in its own order.
    if not _complete_projected_task(task, baseline_fields):
        raise WriterTransportError(invalid)
    return dict(task)


def forward_task_status(
    store: object,
    owner_state: str,
    task_id: str,
    status: str,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
) -> dict[str, object]:
    """Set one Task's planning status through the running owner.

    The Task is read once from the same owner for its identity, canonical UID,
    strict revision and current projected status. The request is frozen before
    the final advertisement revalidation and sent as ONE PATCH: this route has
    no idempotency ledger, so there is no key and no replay, and an ambiguous
    outcome is reported as unknown rather than guessed.
    """

    normalized_id = str(task_id or "").strip().upper()
    if status not in TASK_STATUS_VALUES:
        raise ValueError("invalid task status")

    def prepare(request, host, port):
        detail = _task_detail(
            request, host, port, normalized_id, require_advance=False
        )
        current = detail["status"]
        if not isinstance(current, str) or current not in TASK_STATUS_VALUES:
            raise WriterTransportError(
                "Work Stack server returned an invalid task status"
            )
        baseline_revision = detail["revision"]
        uid = str(detail["uid"])
        baseline_fields = detail["fields"]
        no_op = current == status
        if not no_op and baseline_revision == MAX_REVISION:
            # Only a real transition needs the next revision. The supported
            # service returns the existing projected Task for a same-status
            # request without advancing, so exhaustion refuses transitions
            # here and leaves the no-op alone.
            raise WriterTransportError(
                "the task revision cannot advance beyond the safe integer limit"
            )
        return (
            "{}/{}".format(TASKS_PATH, quote(normalized_id, safe="")),
            serialize_task_patch({"status": status, "revision": baseline_revision}),
            lambda payload: _task_status_from(
                payload,
                normalized_id,
                uid,
                baseline_revision,
                status,
                no_op,
                baseline_fields,
            ),
        )

    return _forward_write(
        store,
        owner_state,
        path="{}/{}".format(TASKS_PATH, quote(normalized_id, safe="")),
        body={},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=None,
        method="PATCH",
        project=lambda payload: {},
        changed_message=(
            "Work Stack server runtime metadata changed before the task status was sent"
        ),
        unknown_message="task status commit is unknown; inspect the task before retrying",
        refused_message="the running Work Stack server refused the task status (HTTP {})",
        prepare=prepare,
    )
