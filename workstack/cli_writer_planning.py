"""Graph note, Objective, and Key Result owner-write forwarding.

Request bodies, response projection, and revision CAS for these three
creates live here. Common owner binding and the shared post/replay
sequence stay in the owner and transport collaborators.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence
from urllib.parse import quote

from .cli_writer_owner import (
    CoordinatesReader,
    RequestJson,
    WriterTransportError,
    _preflight_get,
)
from .cli_writer_transport import _forward_write
from .store import MAX_REVISION

NOTES_PATH = "/api/v1/notes"
OBJECTIVES_PATH = "/api/v1/objectives"

# The exclusive-local Objective record, in its existing field order. The owner
# response carries an extra "revision" that the local path never printed, so the
# projection below drops it rather than widening the CLI contract.
# The exclusive-local Key Result record, in its existing field order. Ids are
# scoped per Objective, so they are never globally unique.
LEGACY_KEY_RESULT_FIELDS = ("id", "text", "target", "progress", "status")
LEGACY_OBJECTIVE_FIELDS = (
    "id",
    "quarter",
    "objective",
    "status",
    "key_results",
    "created",
    "updated_at",
)

def _note_from(payload: Mapping[str, object]) -> dict[str, object]:
    note = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(note, dict) or set(note) != {"id", "text", "links", "created"}:
        raise WriterTransportError("Work Stack server returned an invalid note response")
    return dict(note)


def _objective_from(payload: Mapping[str, object]) -> dict[str, object]:
    """Project the owner's Objective back onto the legacy stdout record.

    The owner adds ``revision`` to the seven fields the exclusive-local path
    produces. Rebuilding the record field by field, in the local order, keeps
    stdout identical between the two routes and refuses a response that is
    missing any legacy field or carries an unexpected extra one.
    """

    objective = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(objective, dict) or set(objective) - {"revision"} != set(
        LEGACY_OBJECTIVE_FIELDS
    ):
        raise WriterTransportError(
            "Work Stack server returned an invalid objective response"
        )
    return {field: objective[field] for field in LEGACY_OBJECTIVE_FIELDS}

def forward_note(
    store: object,
    owner_state: str,
    text: str,
    links: Sequence[str],
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Create one graph note through the running owner and return the raw note.

    The return value is the bare ``{id, text, links, created}`` record so the CLI
    prints exactly what the exclusive-local path prints, with no meta envelope.
    """

    # Same refusal as the local path, before any network contact.
    if not str(text or "").strip():
        raise ValueError("text is required")

    return _forward_write(
        store,
        owner_state,
        path=NOTES_PATH,
        body={"text": text, "links": list(links)},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=idempotency_key,
        project=_note_from,
        changed_message=(
            "Work Stack server runtime metadata changed before the note was sent"
        ),
        unknown_message="note commit is unknown; inspect the graph before retrying",
        refused_message="the running Work Stack server refused the note (HTTP {})",
    )


def forward_objective(
    store: object,
    owner_state: str,
    text: str,
    quarter: str | None,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Create one Objective through the running owner and return the legacy record.

    An omitted or empty quarter is sent as the empty string, which is what makes
    the server apply its own ``current_quarter`` default; an explicit quarter is
    passed through exactly. The owner response carries a ``revision`` the
    exclusive-local path never printed, so the result is projected back onto the
    seven legacy fields.
    """

    # Same refusal, and the same message, as the local path's _required_text.
    if not str(text or "").strip():
        raise ValueError("objective is required")

    return _forward_write(
        store,
        owner_state,
        path=OBJECTIVES_PATH,
        body={"objective": text, "quarter": quarter or ""},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=idempotency_key,
        project=_objective_from,
        changed_message=(
            "Work Stack server runtime metadata changed before the objective was sent"
        ),
        unknown_message=(
            "objective commit is unknown; inspect the objectives before retrying"
        ),
        refused_message="the running Work Stack server refused the objective (HTTP {})",
    )


def _scoped_key_result_ids(roster: object, message: str) -> list[str]:
    """The complete scoped identity roster, or a refusal.

    Every entry must be an object carrying a string id, and the ids must be
    unique within the Objective. A malformed or duplicated entry is
    contradictory evidence about what the Objective contains, so it refuses
    rather than being filtered away. Nothing else about an existing record is
    constrained: its status, progress and text are the owner's to keep, and
    duplicate texts stay legitimate.
    """

    if not isinstance(roster, list):
        raise WriterTransportError(message)
    identities: list[str] = []
    for entry in roster:
        if not isinstance(entry, dict):
            raise WriterTransportError(message)
        identity = entry.get("id")
        if not isinstance(identity, str) or not identity:
            raise WriterTransportError(message)
        identities.append(identity)
    if len(set(identities)) != len(identities):
        raise WriterTransportError(message)
    return identities


def _objective_detail(
    request_json: RequestJson, host: str, port: int, normalized_id: str
) -> dict[str, object]:
    """Read one Objective from the same owner. Part of preflight, never retried."""

    detail = _preflight_get(
        request_json,
        host,
        port,
        "{}/{}".format(OBJECTIVES_PATH, quote(normalized_id, safe="")),
        "objective",
    )
    objective = detail.get("objective")
    if not isinstance(objective, dict):
        raise WriterTransportError(
            "Work Stack server returned an invalid objective response"
        )
    if objective.get("id") != normalized_id:
        raise WriterTransportError(
            "the running Work Stack server returned a different objective"
        )
    revision = objective.get("revision")
    # `type(...) is not int` rather than isinstance, so a bool cannot pass as a
    # revision. The supported range is the product's own, not an invented one.
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise WriterTransportError(
            "Work Stack server returned an unsupported objective revision"
        )
    if revision == MAX_REVISION:
        # The next revision is not representable, so the mutation cannot be
        # made. Refuse here rather than posting a write the owner must reject.
        raise WriterTransportError(
            "the objective revision cannot advance beyond the safe integer limit"
        )
    baseline = _scoped_key_result_ids(
        objective.get("key_results"),
        "Work Stack server returned an invalid key result roster",
    )
    return {"id": normalized_id, "revision": revision, "baseline": baseline}


def _valid_created_key_result(
    record: Mapping[str, object], expected_text: str, expected_target: str
) -> bool:
    """The newly created record must agree with this invocation's create intent."""

    if set(record) != set(LEGACY_KEY_RESULT_FIELDS):
        return False
    return (
        isinstance(record["id"], str)
        # Both create backends generate uppercase KR- followed by a positive
        # unpadded decimal number. Uniqueness is scoped by the roster above;
        # existing records are not subject to these creation-only constraints.
        and re.fullmatch(r"KR-[1-9][0-9]*", record["id"]) is not None
        and isinstance(record["text"], str)
        and record["text"] == expected_text
        and isinstance(record["target"], str)
        and record["target"] == expected_target
        and type(record["progress"]) is int
        and record["progress"] == 0
        and record["status"] == "active"
    )


def _key_result_from(
    payload: Mapping[str, object],
    normalized_id: str,
    baseline: Sequence[str],
    baseline_revision: int,
    expected_text: str,
    expected_target: str,
) -> dict[str, object]:
    """Project the KR this invocation created out of the updated Objective.

    The created record is identified by diffing against the roster observed
    during preflight, never by assuming the text is unique and never by taking
    whichever record happens to be last. A replay returns the frozen response,
    so the same diff yields the same record.

    Success additionally requires the response to be internally consistent with
    the write that was frozen: the returned revision must be exactly the frozen
    baseline plus one, every baseline identity must survive, the whole scoped
    roster must remain valid and unique, and the one new record must carry the
    five legacy fields with this invocation's normalized create values. A
    contradictory response is refused rather than filtered into a success.

    There is no refetch, revision refresh, retry, rollback or local fallback
    after the POST: this only decides whether the response can be reported.
    """

    invalid = "Work Stack server returned an invalid key result response"
    objective = _responded_objective(payload, normalized_id, invalid)
    _require_incremented_revision(objective, baseline_revision)
    created = _created_key_result_id(objective, baseline, invalid)

    record = next(
        entry
        for entry in objective["key_results"]
        if isinstance(entry, dict) and entry.get("id") == created
    )
    if not _valid_created_key_result(record, expected_text, expected_target):
        raise WriterTransportError(invalid)
    return {field: record[field] for field in LEGACY_KEY_RESULT_FIELDS}


def _responded_objective(
    payload: Mapping[str, object], normalized_id: str, invalid: str
) -> dict:
    """The Objective this response is about, or a refusal.

    Pure: it reads the payload and returns or raises. The identity check is
    exact, so a response describing some other Objective is refused rather than
    projected.
    """

    objective = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(objective, dict) or objective.get("id") != normalized_id:
        raise WriterTransportError(invalid)
    return objective


def _require_incremented_revision(objective: dict, baseline_revision: int) -> None:
    """The returned revision must be exactly the frozen baseline plus one.

    ``type(...) is int`` rather than isinstance, so a boolean cannot pass as a
    revision number.
    """

    revision = objective.get("revision")
    if type(revision) is not int or revision != baseline_revision + 1:
        raise WriterTransportError(
            "Work Stack server reported an impossible objective revision"
        )


def _created_key_result_id(
    objective: dict, baseline: Sequence[str], invalid: str
) -> str:
    """The single identity this invocation added, found by diffing the roster.

    The whole scoped roster must still be valid and unique, every baseline
    identity must survive, and exactly one identity may be new. The record is
    identified by that ID, never by assuming the text is unique and never by
    taking whichever record happens to be last.
    """

    identities = _scoped_key_result_ids(objective.get("key_results"), invalid)
    observed = set(identities)
    missing = [identity for identity in baseline if identity not in observed]
    if missing:
        raise WriterTransportError(
            "Work Stack server dropped an existing key result from the objective"
        )
    created = [identity for identity in identities if identity not in set(baseline)]
    if len(created) != 1:
        raise WriterTransportError(invalid)
    return created[0]


def forward_key_result(
    store: object,
    owner_state: str,
    objective_id: str,
    text: str,
    target: str | None,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Add one Key Result through the running owner and return the legacy record.

    The Objective is read once from the same owner to obtain the revision the
    strict compare-and-swap requires, so no new CLI input is introduced. The
    identifier is normalized the way the local lookup normalizes it and is
    URL-encoded rather than interpolated. Output stays the raw five-field record
    with no parent Objective, revision or meta envelope.
    """

    normalized_id = str(objective_id or "").strip().upper()

    def prepare(request, host, port):
        # The local path resolves the Objective before it validates the text, so
        # an unknown Objective still reports first.
        detail = _objective_detail(request, host, port, normalized_id)
        normalized_text = str(text or "").strip()
        normalized_target = str(target or "").strip()
        if not normalized_text:
            raise ValueError("text is required")
        baseline = tuple(detail["baseline"])
        baseline_revision = detail["revision"]
        body = {
            "text": text,
            # Same normalization the exclusive-local path applies.
            "target": normalized_target,
            "revision": baseline_revision,
        }
        path = "{}/{}/key-results".format(
            OBJECTIVES_PATH, quote(normalized_id, safe="")
        )

        def project(payload):
            return _key_result_from(
                payload, normalized_id, baseline, baseline_revision,
                normalized_text, normalized_target,
            )

        return path, body, project

    return _forward_write(
        store,
        owner_state,
        path=OBJECTIVES_PATH,
        body={},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=idempotency_key,
        project=_note_from,
        prepare=prepare,
        changed_message=(
            "Work Stack server runtime metadata changed before the key result was sent"
        ),
        unknown_message=(
            "key result commit is unknown; inspect the objective before retrying"
        ),
        refused_message="the running Work Stack server refused the key result (HTTP {})",
    )
