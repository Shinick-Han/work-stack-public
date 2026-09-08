"""Subtask append and subtask-status owner-write forwarding.

Parent snapshot coherence, ordered history, and the local setter's first
match rule stay here. Shared Task detail reads come from the task
collaborator; the post/replay sequence stays in transport.
"""

from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import quote

from .cli_writer_owner import CoordinatesReader, RequestJson, WriterTransportError
from .cli_writer_tasks import (
    TASK_STATUS_VALUES,
    TASKS_PATH,
    _complete_projected_task,
    _same_json,
    _task_detail,
)
from .cli_writer_transport import _forward_write

SUBTASK_KEYS = ("id", "title", "priority", "status")
# The allocator formats a new subtask id as an ASCII "S-<n>" with no leading
# zero and a first index of 1 (workstack.service._next_id, verified). "\\d"
# would also accept non-ASCII decimal digits and "$" would accept a trailing
# newline, so neither is used here.
SUBTASK_ID = re.compile(r"S-[1-9][0-9]*")


def _subtask_baseline(task: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """The parent's complete ordered subtask history.

    An absent list is the legacy empty default. An explicit null, a non-list or
    a non-object entry is refused before the POST rather than filtered away, so
    a malformed baseline can never be silently rewritten by an append.
    """

    if "subtasks" not in task:
        return ()
    records = task.get("subtasks")
    if not isinstance(records, list):
        raise WriterTransportError("Work Stack server returned an invalid subtask list")
    baseline = []
    for record in records:
        if not isinstance(record, dict):
            raise WriterTransportError("Work Stack server returned an invalid subtask")
        baseline.append(dict(record))
    return tuple(baseline)


def _valid_created_subtask(
    record: Mapping[str, object],
    expected_title: str,
    expected_priority: str,
    used_ids: frozenset[str],
) -> bool:
    """Exactly the four frozen fields, with a fresh scoped id.

    The set is checked rather than the key order: the owner's idempotency
    ledger returns a replayed body with its keys sorted, so the identical
    admitted replay legitimately arrives in a different order. The frozen
    output order is applied by this writer when it projects the record.
    """

    if set(record) != set(SUBTASK_KEYS):
        return False
    identifier = record.get("id")
    if not isinstance(identifier, str) or not SUBTASK_ID.fullmatch(identifier):
        return False
    if identifier in used_ids:
        return False
    return (
        record.get("title") == expected_title
        and record.get("priority") == expected_priority
        and record.get("status") == "open"
    )


# What an append may legitimately move on the parent, plus the field the
# projection strips. Every other known baseline value must survive unchanged.
SANCTIONED_APPEND_EFFECTS = frozenset(
    {"revision", "updated_at", "subtasks", "context_count", "status", "status_fact_id"}
)


def _parent_values_preserved(
    task: Mapping[str, object], baseline_task: Mapping[str, object]
) -> bool:
    """Does the answer still carry the parent values an append cannot change?

    Only the fields the baseline actually had are compared, so nothing absent
    is invented, and the comparison is structural with booleans kept distinct
    from numbers. Identity, status and revision are checked separately.
    """

    for field, value in baseline_task.items():
        if field in SANCTIONED_APPEND_EFFECTS:
            continue
        if field not in task or not _same_json(task[field], value):
            return False
    return True


def _appended_subtask(
    records: tuple[dict[str, object], ...],
    baseline: tuple[dict[str, object], ...],
    expected_title: str,
    expected_priority: str,
) -> Mapping[str, object]:
    """Exactly one new record on top of the complete, unchanged history."""

    invalid = "Work Stack server returned an invalid subtask response"
    if len(records) != len(baseline) + 1:
        raise WriterTransportError(invalid)
    # The ordered history survives whole, compared with booleans kept distinct
    # from numbers so a nested true cannot pass as 1.
    if not _same_json(
        [dict(record) for record in records[: len(baseline)]],
        [dict(record) for record in baseline],
    ):
        raise WriterTransportError("Work Stack server changed an existing subtask")
    created = records[-1]
    used = frozenset(
        str(record.get("id")) for record in baseline if isinstance(record.get("id"), str)
    )
    if not _valid_created_subtask(created, expected_title, expected_priority, used):
        raise WriterTransportError(invalid)
    return created


def _subtask_from(
    payload: Mapping[str, object],
    normalized_id: str,
    uid: str,
    baseline: tuple[dict[str, object], ...],
    baseline_revision: int,
    baseline_status: object,
    baseline_task: Mapping[str, object],
    expected_title: str,
    expected_priority: str,
) -> dict[str, object]:
    """Validate the parent the owner returned and project only the new record."""

    invalid = "Work Stack server returned an invalid subtask response"
    task = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(task, dict) or task.get("id") != normalized_id:
        raise WriterTransportError(invalid)
    if task.get("uid") != uid:
        raise WriterTransportError("the running Work Stack server returned a different task")
    revision = task.get("revision")
    if type(revision) is not int or revision != baseline_revision + 1:
        raise WriterTransportError("Work Stack server reported an impossible task revision")
    if task.get("status") != baseline_status:
        raise WriterTransportError(invalid)
    if not _complete_projected_task(task, frozenset(baseline_task)):
        raise WriterTransportError(invalid)
    if not _parent_values_preserved(task, baseline_task):
        raise WriterTransportError("Work Stack server changed the parent task")

    created = _appended_subtask(
        _subtask_baseline(task), baseline, expected_title, expected_priority
    )
    return {field: created[field] for field in SUBTASK_KEYS}


def forward_subtask(
    store: object,
    owner_state: str,
    task_id: str,
    title: str,
    priority: str,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Append one subtask through the running owner and return the new record.

    The parent is read once from the same owner for its identity, canonical
    UID, strict revision and complete ordered subtask history, and the request
    is frozen before the final advertisement revalidation. Output stays the
    legacy {id, title, priority, status} record with no parent envelope.
    """

    normalized_id = str(task_id or "").strip().upper()

    def prepare(request, host, port):
        # The local path resolves the Task and its revision before it validates
        # the title, so an unknown Task and an exhausted revision still report
        # before a blank title does.
        detail = _task_detail(request, host, port, normalized_id)
        baseline = _subtask_baseline(detail["task"])
        normalized_title = str(title or "").strip()
        if not normalized_title:
            raise ValueError("title is required")
        baseline_revision = detail["revision"]
        uid = str(detail["uid"])
        baseline_status = detail["status"]
        baseline_task = detail["task"]
        path = "{}/{}/subtasks".format(TASKS_PATH, quote(normalized_id, safe=""))
        return (
            path,
            {
                "title": normalized_title,
                "priority": priority,
                "revision": baseline_revision,
            },
            lambda payload: _subtask_from(
                payload,
                normalized_id,
                uid,
                baseline,
                baseline_revision,
                baseline_status,
                baseline_task,
                normalized_title,
                priority,
            ),
        )

    return _forward_write(
        store,
        owner_state,
        path="{}/{}/subtasks".format(TASKS_PATH, quote(normalized_id, safe="")),
        body={},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=idempotency_key,
        project=lambda payload: {},
        changed_message=(
            "Work Stack server runtime metadata changed before the subtask was sent"
        ),
        unknown_message="subtask commit is unknown; inspect the task before retrying",
        refused_message="the running Work Stack server refused the subtask (HTTP {})",
        prepare=prepare,
    )


# What this setter legitimately moves on the parent, plus the planning status
# which has its own equality check. Every other known baseline value must
# survive, subject only to the GET-versus-raw projection differences below.
SUBTASK_STATUS_PARENT_EFFECTS = frozenset(
    {"revision", "updated_at", "subtasks", "status", "context_count"}
)
# The detail projection injects these as None when the stored record has no
# value, so a raw PATCH parent may legitimately omit them - but only then.
PROJECTION_INJECTED_NONE = ("scheduled", "estimate_minutes")


def _located_subtask(
    baseline: tuple[dict[str, object], ...], wanted: str
) -> tuple[int, dict[str, object]] | None:
    """The setter's own rule: the FIRST record whose upper-cased id matches.

    Duplicates keep first-match and later records are untouched. No allocation
    grammar is imposed, so legacy ids such as S-03, S-0, numeric and non-ASCII
    ones stay routeable exactly as the local setter routes them.
    """

    for index, record in enumerate(baseline):
        if str(record.get("id", "")).upper() == wanted:
            return index, record
    return None


def _parent_survived_subtask_status(
    task: Mapping[str, object], baseline_task: Mapping[str, object]
) -> bool:
    """Known parent values this setter cannot change are still present."""

    for field, value in baseline_task.items():
        if field in SUBTASK_STATUS_PARENT_EFFECTS:
            continue
        if field not in task:
            # Absence is permissible only for the injected-None possibility.
            if field in PROJECTION_INJECTED_NONE and value is None:
                continue
            return False
        if not _same_json(task[field], value):
            return False
    return True


def _subtask_status_from(
    payload: Mapping[str, object],
    normalized_id: str,
    uid: str,
    baseline_task: Mapping[str, object],
    baseline: tuple[dict[str, object], ...],
    index: int,
    expected_record: Mapping[str, object],
) -> dict[str, object]:
    """Validate the whole parent the owner returned around one changed record."""

    invalid = "Work Stack server returned an invalid subtask status response"
    task = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(task, dict) or task.get("id") != normalized_id:
        raise WriterTransportError(invalid)
    if task.get("uid") != uid:
        raise WriterTransportError("the running Work Stack server returned a different task")
    revision = task.get("revision")
    if type(revision) is not int or revision != _revision_of(baseline_task) + 1:
        raise WriterTransportError("Work Stack server reported an impossible task revision")
    if task.get("status") != baseline_task.get("status"):
        raise WriterTransportError(invalid)
    if not _parent_survived_subtask_status(task, baseline_task):
        raise WriterTransportError("Work Stack server changed the parent task")

    records = _subtask_baseline(task)
    if len(records) != len(baseline):
        raise WriterTransportError(invalid)
    for position, (returned, original) in enumerate(zip(records, baseline)):
        expected = expected_record if position == index else original
        if not _same_json(dict(returned), dict(expected)):
            raise WriterTransportError("Work Stack server changed an existing subtask")
    return dict(expected_record)


def _revision_of(task: Mapping[str, object]) -> int:
    revision = task.get("revision")
    if type(revision) is not int:
        raise WriterTransportError("Work Stack server returned an unsupported task revision")
    return revision


def forward_subtask_status(
    store: object,
    owner_state: str,
    task_id: str,
    subtask_id: str,
    status: str,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
) -> dict[str, object]:
    """Set one subtask's status through the running owner.

    The parent is read once from the same owner and the target is resolved with
    the local setter's own rule. Unlike the Task-status route there is no no-op
    exception: this setter takes the next revision BEFORE it looks the subtask
    up, so the same status is still a real revision+1 write and an exhausted
    revision refuses before the lookup. One PATCH, no idempotency key, no
    replay, refetch or local fallback.
    """

    normalized_id = str(task_id or "").strip().upper()
    if status not in TASK_STATUS_VALUES:
        raise ValueError("invalid task status")

    def prepare(request, host, port):
        detail = _task_detail(request, host, port, normalized_id)
        baseline_task = detail["task"]
        baseline = _subtask_baseline(baseline_task)
        wanted = str(subtask_id or "").strip().upper()
        if not wanted:
            # An empty path segment cannot address a record over HTTP. The
            # owner path refuses instead of inventing a sentinel id; a genuinely
            # absent owner still reaches the unchanged local setter.
            raise WriterTransportError(
                "a subtask identifier is required to reach the running Work Stack server"
            )
        located = _located_subtask(baseline, wanted)
        if located is None:
            raise WriterTransportError("unknown subtask: {}".format(wanted))
        index, target = located
        # The legacy record with only its status assigned: the original key
        # order survives and status is appended last when it was absent.
        expected_record = dict(target)
        expected_record["status"] = status
        return (
            "{}/{}/subtasks/{}".format(
                TASKS_PATH, quote(normalized_id, safe=""), quote(wanted, safe="")
            ),
            {"status": status, "revision": detail["revision"]},
            lambda payload: _subtask_status_from(
                payload,
                normalized_id,
                str(detail["uid"]),
                baseline_task,
                baseline,
                index,
                expected_record,
            ),
        )

    return _forward_write(
        store,
        owner_state,
        path="{}/{}/subtasks".format(TASKS_PATH, quote(normalized_id, safe="")),
        body={},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=None,
        method="PATCH",
        project=lambda payload: {},
        changed_message=(
            "Work Stack server runtime metadata changed before the subtask status was sent"
        ),
        unknown_message="subtask status commit is unknown; inspect the task before retrying",
        refused_message=(
            "the running Work Stack server refused the subtask status (HTTP {})"
        ),
        prepare=prepare,
    )
