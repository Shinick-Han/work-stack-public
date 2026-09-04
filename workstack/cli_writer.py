"""Owner-aware writer transport for the ``note`` and ``okr add-objective`` commands.

The T-0002 vertical slices. When the local data directory carries running owner
metadata, these commands must not take the exclusive local Store path: the
running server owns the lease and a direct write would either fail or create a
second writer. This module routes them through the owning server's HTTP boundary
instead.

Both commands share one owner, preflight, revalidate, post and single-replay
sequence. Only the request path, the request body and the projection of the
response differ.

Failure is closed by construction. Every error raised here propagates to the CLI
top level, which reports exit 2. No path in this module falls back to a local
write, and none removes or repairs owner metadata.
"""

from __future__ import annotations

import hashlib
import datetime
import http.client
import json
import os
import re
import stat
import uuid
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

# (status, payload) transport, injected by the CLI so this module never imports
# it back, never grows its own HTTP stack, and never changes the shared helper.
RequestJson = Callable[..., tuple[int, dict[str, object]]]
# store -> (host, port) | None. Still accepted so the CLI call site and the
# frozen contract keep their exact shape, but the note route no longer reads
# owner coordinates through it: see _resolve_coordinates for why an unbounded
# helper cannot sit behind a byte bound.
CoordinatesReader = Callable[..., "tuple[str, int] | None"]

# Mirrors the states the store reports; anything else is an invalid response.
SYNC_STATES = frozenset({"external-change-detected", "in-sync", "invalid"})

NOTES_PATH = "/api/v1/notes"
OBJECTIVES_PATH = "/api/v1/objectives"
TASKS_PATH = "/api/v1/tasks"
# The four planning statuses the supported service accepts.
TASK_STATUS_VALUES = ("open", "started", "done", "dropped")
# Fields the supported projection adds to every Task it returns, so a
# success that lacks them is not a complete Task.
PROJECTED_TASK_FIELDS = ("scheduled", "estimate_minutes", "context_count")

# The exclusive-local Objective record, in its existing field order. The owner
# response carries an extra "revision" that the local path never printed, so the
# projection below drops it rather than widening the CLI contract.
# The exclusive-local Key Result record, in its existing field order. Ids are
# scoped per Objective, so they are never globally unique.
LEGACY_KEY_RESULT_FIELDS = ("id", "text", "target", "progress", "status")
# The exclusive-local Task note record, in the order the local path prints it.
LEGACY_TASK_NOTE_FIELDS = ("date", "text")

# The product's own supported revision bound, reused rather than restated, so a
# revision the owner could never hold is refused before any mutation is sent.
from .store import MAX_REVISION  # noqa: E402  (constant only; no Store is built)

LEGACY_OBJECTIVE_FIELDS = (
    "id",
    "quarter",
    "objective",
    "status",
    "key_results",
    "created",
    "updated_at",
)

# Owner metadata classification. Only ABSENT keeps the exclusive-local path.
OWNER_ABSENT = "absent"
OWNER_PRESENT = "present"
OWNER_INVALID = "invalid"

# Bounded reads. Owner metadata is a tiny fixed record; the store manifest lists
# files and tasks and is larger, but neither is unbounded input.
SERVER_INFO_READ_LIMIT = 64 * 1024
STORE_MANIFEST_READ_LIMIT = 4 * 1024 * 1024

# A response that never arrived, arrived truncated, or arrived unparseable after
# the request went out leaves the outcome unknown. http.client raises
# HTTPException subclasses such as IncompleteRead and BadStatusLine that are not
# OSError, so both hierarchies count.
AMBIGUOUS_TRANSPORT = (OSError, http.client.HTTPException)


class WriterTransportError(OSError):
    """Owner-route failure. Never fall back to a local write after this."""


class CommitUnknownError(WriterTransportError):
    """The note POST outcome stayed unknown after the one permitted replay."""


def owner_metadata_state(store: object) -> str:
    """Classify the owner metadata entry by its actual filesystem kind.

    ``is_file()`` collapses "missing" and "exists but is a directory, symlink or
    otherwise not a regular file" into one false answer. Only the first is
    absence; the rest are an invalid owner state that must refuse rather than
    silently take the local write path. ``lstat`` is used rather than ``stat``
    so a symlink is judged as a symlink instead of its target.

    A ``server_info_path`` that is not a filesystem path at all yields
    ``OWNER_ABSENT``: there is no entry to observe. That is the single
    accommodation here, and it is not used to swallow errors about a real entry.
    """

    path = getattr(store, "server_info_path", None)
    if path is None:
        return OWNER_ABSENT
    try:
        target = os.fspath(path)
    except TypeError:
        return OWNER_ABSENT
    try:
        info = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return OWNER_ABSENT
    except (OSError, ValueError):
        # Permission denied, a bad name, a broken mount: the entry may exist and
        # cannot be read. Refusing is the only safe reading.
        return OWNER_INVALID
    return OWNER_PRESENT if stat.S_ISREG(info.st_mode) else OWNER_INVALID


def _read_bounded(path: object, limit: int, label: str) -> bytes:
    """Read at most ``limit`` bytes, and never quote the path in the error."""

    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as error:
        raise WriterTransportError("Work Stack {} is unreadable".format(label)) from error
    if len(raw) > limit:
        raise WriterTransportError("Work Stack {} exceeds the supported size".format(label))
    return raw


def _canonical_workspace_uid(value: object) -> str:
    if type(value) is not str:
        raise WriterTransportError("workspace identity is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise WriterTransportError("workspace identity is invalid") from error
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise WriterTransportError("workspace identity is invalid")
    return value


def expected_workspace_uid(store: object) -> str:
    """Read the selected workspace identity without taking the Store lease."""

    path = getattr(store, "store_manifest_path", None)
    if path is None:
        raise WriterTransportError("Work Stack store manifest is unavailable")
    raw = _read_bounded(path, STORE_MANIFEST_READ_LIMIT, "store manifest")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WriterTransportError("Work Stack store manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise WriterTransportError("Work Stack store manifest is invalid")
    return _canonical_workspace_uid(manifest.get("workspace_id"))


def new_idempotency_key() -> str:
    """One key per CLI invocation.

    Deliberately random rather than content-derived: repeating the same
    ``work-stack note`` arguments in a new invocation is a new intent and must
    create a second note, not replay the first.
    """

    return "cli-note-{}".format(uuid.uuid4().hex)


def _origin(host: str, port: int) -> str:
    origin_host = "[{}]".format(host) if ":" in host else host
    return "http://{}:{}".format(origin_host, port)


def _preflight_get(
    request_json: RequestJson, host: str, port: int, path: str, label: str
) -> dict[str, object]:
    """One preflight read. Never retried, and never leaks the server's own text."""

    try:
        status, payload = request_json(host, port, "GET", path)
    except AMBIGUOUS_TRANSPORT as error:
        raise WriterTransportError(
            "the running Work Stack server {} could not be read".format(label)
        ) from error
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if status != 200 or not isinstance(data, dict):
        raise WriterTransportError(
            "Work Stack server {} response is invalid".format(label)
        )
    return data


def _preflight(request_json: RequestJson, host: str, port: int, expected_uid: str) -> str:
    """Session, identity and readiness. A preflight failure is final, never retried."""

    session = _preflight_get(request_json, host, port, "/api/v1/session", "session")
    csrf = session.get("csrf_token")
    if type(csrf) is not str or not csrf:
        raise WriterTransportError("Work Stack server session could not be established")

    storage = _preflight_get(request_json, host, port, "/api/v1/storage", "storage")
    if _canonical_workspace_uid(storage.get("workspace_id")) != expected_uid:
        raise WriterTransportError(
            "the running Work Stack server owns a different workspace identity"
        )

    sync = _preflight_get(request_json, host, port, "/api/v1/sync/status", "sync status")
    state = sync.get("state")
    if type(state) is not str or state not in SYNC_STATES:
        raise WriterTransportError("Work Stack server sync status response is invalid")
    if state != "in-sync":
        raise WriterTransportError(
            "the running Work Stack store is not in-sync; resolve synchronization first"
        )
    return csrf


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


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def read_owner_binding(store: object) -> tuple[str, int, str]:
    """Read the owner advertisement under a hard byte bound and validate it.

    The shared ``_server_coordinates`` helper is the Agent transport's contract
    and reads the whole entry; the note route must not inherit that. An owner
    advertisement is a tiny fixed record, so anything above
    ``SERVER_INFO_READ_LIMIT`` is refused as oversized *before* the note path
    makes any HTTP contact, rather than being parsed because it happens to be
    valid JSON.

    Returns ``(host, port, binding)``. ``binding`` is a digest of the exact
    bytes observed, so the same advertisement can be proven unchanged later
    without keeping its contents around.
    """

    path = getattr(store, "server_info_path", None)
    if path is None:
        raise WriterTransportError(
            "Work Stack server runtime metadata became unavailable; refusing to write locally"
        )
    state = owner_metadata_state(store)
    if state == OWNER_ABSENT:
        # Presence was already observed once, so an absent entry now means it
        # vanished mid-flight. That is never permission to write locally.
        raise WriterTransportError(
            "Work Stack server runtime metadata became unavailable; refusing to write locally"
        )
    if state != OWNER_PRESENT:
        raise WriterTransportError(
            "Work Stack server runtime metadata is not a readable regular file"
        )

    raw = _read_bounded(path, SERVER_INFO_READ_LIMIT, "server runtime metadata")
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WriterTransportError(
            "Work Stack server runtime metadata is invalid"
        ) from error
    if not isinstance(info, dict):
        raise WriterTransportError("Work Stack server runtime metadata is invalid")
    version = info.get("version")
    host = info.get("host")
    port = info.get("port")
    # Types are checked before the operations that depend on them. Equality
    # alone would accept True and 1.0 for the version, because both compare
    # equal to 1, and a set membership test on an unhashable host would raise
    # TypeError out of this module instead of refusing. `type(...) is int`
    # rather than isinstance also excludes bool, which is an int subclass.
    if (
        type(version) is not int
        or version != 1
        or type(host) is not str
        or host not in LOOPBACK_HOSTS
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise WriterTransportError("Work Stack server runtime metadata is invalid")
    return str(info["host"]), port, hashlib.sha256(raw).hexdigest()


def _resolve_coordinates(
    store: object, coordinates_reader: CoordinatesReader
) -> tuple[str, int, str]:
    """Resolve the owner coordinates from the one bounded, validated snapshot.

    ``read_owner_binding`` is the only authority for the note route's
    coordinates. The injected ``coordinates_reader`` is deliberately NOT
    consulted: it is the Agent transport's helper and reads the entry whole, so
    calling it would put an unbounded read behind the bound. A file that grows
    between the two observations defeats a size check placed in front of an
    unbounded read, which is exactly the gap this avoids. The Agent helper and
    its own callers are untouched; only the note route stops relying on it.

    Presence has been established before this point, so a vanished or
    unreadable entry here is an invalid owner state, not permission to write
    locally, and it is never cleaned up.
    """

    return read_owner_binding(store)


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
            {"status": status, "revision": baseline_revision},
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


def _checkin_result(
    status: int, payload: Mapping[str, object], body: dict[str, object],
) -> dict[str, object]:
    message = "checkin commit is unknown; inspect the worklog before retrying"
    if status != 200 or type(payload) is not dict or set(payload) != {"data"}:
        raise CommitUnknownError(message)
    data = payload["data"]
    if type(data) is not dict or list(data) != ["date", "start_time"]:
        raise CommitUnknownError(message)
    for field, sent in (("date", "date"), ("start_time", "time")):
        if type(data[field]) is not str or data[field] != body[sent]:
            raise CommitUnknownError(message)
    return data


def forward_checkin(
    store: object, owner_state: str, time: str | None, date: str | None,
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    """Freeze CLI clock defaults, then invoke the owner's unchanged checkin."""
    frozen_date = date or datetime.date.today().isoformat()
    frozen_time = datetime.datetime.now().strftime("%H:%M") if time is None else time
    body = {"date": frozen_date, "time": frozen_time}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/worklog/checkin", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False,
        project=lambda payload: {},
        project_result=lambda status, payload: _checkin_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the checkin was sent",
        unknown_message="checkin commit is unknown; inspect the worklog before retrying",
        refused_message="the running Work Stack server refused the checkin (HTTP {})",
    )


def _worklog_entry_categories_match(data: dict, body: dict) -> bool:
    """Compare a response view without normalizing the request or returned data."""
    for field, sent in (("done", "done"), ("next", "next_items"), ("blockers", "blockers")):
        values = data[field]
        if type(values) is not list or any(type(item) is not str for item in values):
            return False
        expected = [item.strip() for item in body[sent] if item.strip()]
        if values != expected:
            return False
    return True


def _worklog_entry_result(status: int, payload: Mapping[str, object], body: dict) -> dict[str, object]:
    message = "worklog entry commit is unknown; inspect the worklog before retrying"
    if status != 200 or type(payload) is not dict or set(payload) != {"data"}:
        raise CommitUnknownError(message)
    data = payload["data"]
    if type(data) is not dict or list(data) != ["date", "task_id", "task", "done", "next", "blockers"]:
        raise CommitUnknownError(message)
    if any(type(data[field]) is not str for field in ("date", "task_id", "task")):
        raise CommitUnknownError(message)
    if data["date"] != body["date"] or data["task_id"] != body["task_id"].strip().upper():
        raise CommitUnknownError(message)
    if not _worklog_entry_categories_match(data, body):
        raise CommitUnknownError(message)
    return data


def forward_worklog_entry(
    store: object, owner_state: str, task_id: str, date: str | None,
    done: Sequence[str], next_items: Sequence[str], blockers: Sequence[str],
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"task_id": task_id, "date": date or datetime.date.today().isoformat(),
            "done": list(done), "next_items": list(next_items), "blockers": list(blockers)}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/worklog/add", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False,
        project=lambda payload: {},
        project_result=lambda status, payload: _worklog_entry_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the worklog entry was sent",
        unknown_message="worklog entry commit is unknown; inspect the worklog before retrying",
        refused_message="the running Work Stack server refused the worklog entry (HTTP {})",
    )


def _cli_result_data(status: int, payload: Mapping[str, object], message: str) -> dict:
    if status != 200 or type(payload) is not dict or set(payload) != {"data"}:
        raise CommitUnknownError(message)
    if type(payload["data"]) is not dict:
        raise CommitUnknownError(message)
    return payload["data"]


def _cli_calendar_date(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _cli_record_uid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


def _cli_string_list(value: object) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def _backlog_identity_matches(data: dict, workspace_uid: str) -> bool:
    if type(data["id"]) is not str or re.fullmatch(r"T-[0-9]{4,}", data["id"]) is None:
        return False
    if not _cli_record_uid(data["uid"]):
        return False
    if data["uid"] != str(uuid.uuid5(uuid.UUID(workspace_uid), data["id"])):
        return False
    fact = data["status_fact_id"]
    return type(fact) is str and re.fullmatch(r"PS-[0-9]{6,}", fact) is not None


def _backlog_values_match(data: dict, body: dict) -> bool:
    expected = {
        "title": body["title"].strip(), "detail": body["detail"].strip(),
        "priority": body["priority"], "due": body["due"] or None,
        "parent_id": body["parent_id"].strip().upper() if body["parent_id"] else None,
        "status": "open", "revision": 0, "scheduled": None, "estimate_minutes": None,
        "subtasks": [], "notes": [],
    }
    if any(type(data[field]) is not type(value) or data[field] != value for field, value in expected.items()):
        return False
    return _cli_calendar_date(data["created"]) and _cli_calendar_date(data["updated_at"])


def _backlog_collections_match(data: dict, body: dict) -> bool:
    """Bind a received view; leave original input and output values untouched."""
    for field in ("tags", "objective_ids", "dependencies"):
        if not _cli_string_list(data[field]):
            return False
        stripped = [item.strip() for item in body[field] if item.strip()]
        expected = stripped if field == "tags" else [item.upper() for item in stripped]
        if data[field] != sorted(set(expected)):
            return False
    return True


def _backlog_add_result(status: int, payload: Mapping[str, object], body: dict, workspace_uid: str) -> dict:
    message = "backlog add commit is unknown; inspect the backlog before retrying"
    data = _cli_result_data(status, payload, message)
    fields = ["id", "uid", "title", "detail", "status", "priority", "due", "scheduled",
              "estimate_minutes", "tags", "objective_ids", "parent_id", "dependencies",
              "subtasks", "notes", "created", "updated_at", "revision", "status_fact_id"]
    if list(data) != fields:
        raise CommitUnknownError(message)
    if not (_backlog_identity_matches(data, workspace_uid) and _backlog_values_match(data, body)
            and _backlog_collections_match(data, body)):
        raise CommitUnknownError(message)
    return data


def forward_backlog_add(
    store: object, owner_state: str, title: str, detail: str, priority: str, due: str | None,
    tags: Sequence[str], objective_ids: Sequence[str], parent_id: str | None, dependencies: Sequence[str],
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"title": title, "detail": detail, "priority": priority, "due": due, "tags": list(tags),
            "objective_ids": list(objective_ids), "parent_id": parent_id, "dependencies": list(dependencies)}
    workspace_uid = expected_workspace_uid(store)
    return _forward_write(
        store, owner_state, path="/api/v1/cli/backlog/add", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False, project=lambda payload: {},
        project_result=lambda status, payload: _backlog_add_result(status, payload, body, workspace_uid),
        changed_message="Work Stack server runtime metadata changed before the backlog add was sent",
        unknown_message="backlog add commit is unknown; inspect the backlog before retrying",
        refused_message="the running Work Stack server refused the backlog add (HTTP {})",
    )


def _okr_link_identity_matches(data: dict, body: dict) -> bool:
    task_id = data.get("id")
    if type(task_id) is not str or task_id != body["task_id"].strip().upper():
        return False
    if not _cli_record_uid(data.get("uid")):
        return False
    fact = data.get("status_fact_id")
    return type(fact) is str and re.fullmatch(r"PS-[0-9]{6,}", fact) is not None


def _okr_link_result(status: int, payload: Mapping[str, object], body: dict) -> dict:
    message = "OKR link commit is unknown; inspect the task before retrying"
    data = _cli_result_data(status, payload, message)
    if not _okr_link_identity_matches(data, body):
        raise CommitUnknownError(message)
    revision = data.get("revision")
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise CommitUnknownError(message)
    if not _cli_calendar_date(data.get("updated_at")):
        raise CommitUnknownError(message)
    objectives = data.get("objective_ids")
    if not _cli_string_list(objectives) or objectives != sorted(set(objectives)):
        raise CommitUnknownError(message)
    if body["objective_id"].strip().upper() not in objectives:
        raise CommitUnknownError(message)
    # The envelope binds this link; the actual one-step revision and unchanged
    # history are established by the owner operation, not a prior client GET.
    return data


def forward_okr_link(
    store: object, owner_state: str, objective_id: str, task_id: str,
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"objective_id": objective_id, "task_id": task_id}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/okr/link", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False, project=lambda payload: {},
        project_result=lambda status, payload: _okr_link_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the OKR link was sent",
        unknown_message="OKR link commit is unknown; inspect the task before retrying",
        refused_message="the running Work Stack server refused the OKR link (HTTP {})",
    )


def _okr_progress_result(status: int, payload: Mapping[str, object], body: dict) -> dict:
    message = "OKR progress commit is unknown; inspect the objective before retrying"
    data = _cli_result_data(status, payload, message)
    if "id" not in data or str(data["id"]).upper() != body["key_result_id"].strip().upper():
        raise CommitUnknownError(message)
    expected = max(0, min(100, body["progress"]))
    if type(data.get("progress")) is not int or data["progress"] != expected:
        raise CommitUnknownError(message)
    expected_status = "done" if expected == 100 else "active"
    if type(data.get("status")) is not str or data["status"] != expected_status:
        raise CommitUnknownError(message)
    # The raw KR carries neither owning Objective identity nor revision. Do not
    # invent those fields or perform another read to infer an unknown commit.
    return data


def forward_okr_progress(
    store: object, owner_state: str, objective_id: str, key_result_id: str, progress: int,
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"objective_id": objective_id, "key_result_id": key_result_id, "progress": progress}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/okr/progress", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False, project=lambda payload: {},
        project_result=lambda status, payload: _okr_progress_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the OKR progress was sent",
        unknown_message="OKR progress commit is unknown; inspect the objective before retrying",
        refused_message="the running Work Stack server refused the OKR progress (HTTP {})",
    )


def _validate_keyless_post(method, keyless_post, idempotency_key, replay, extra_headers):
    if keyless_post and (
        method != "POST" or idempotency_key is not None or replay or extra_headers
    ):
        raise ValueError("keyless POST requires one attempt, no key and no extra headers")


def _write_headers(host, port, csrf, method, idempotency_key, extra_headers, keyless_post):
    headers = {"Origin": _origin(host, port), "X-WorkStack-CSRF": csrf}
    if method == "POST" and not keyless_post:
        headers["Idempotency-Key"] = idempotency_key or new_idempotency_key()
        if extra_headers:
            headers.update(extra_headers)
    return headers


def _forward_write(
    store: object,
    owner_state: str,
    *,
    path: str,
    body: dict[str, object],
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None,
    method: str = "POST",
    extra_headers: Mapping[str, str] | None = None,
    replay: bool = True,
    keyless_post: bool = False,
    project_result: Callable[[int, Mapping[str, object]], dict[str, object]] | None = None,
    project: Callable[[Mapping[str, object]], dict[str, object]],
    changed_message: str,
    unknown_message: str,
    refused_message: str,
    prepare: Callable[..., tuple[str, dict[str, object], Callable[..., dict[str, object]]]]
    | None = None,
) -> dict[str, object]:
    """The one owner/preflight/revalidate/post/replay sequence for a CLI write.

    Lifted unchanged out of the admitted note route so each command reuses it
    instead of growing a parallel transport. Only the path, the body, the
    response projection and the three diagnostics differ; every command keeps
    the exact wording it had.

    ``prepare`` is for a command that must read from the same owner before it
    writes. It runs after preflight and BEFORE the final same-advertisement
    revalidation, so its reads cannot widen the window between the last check
    and the first mutation, and it returns the final path, body and projection.
    Its reads are part of preflight and are never retried.
    """

    _validate_keyless_post(method, keyless_post, idempotency_key, replay, extra_headers)
    if owner_state == OWNER_INVALID:
        raise WriterTransportError(
            "Work Stack server runtime metadata is not a readable regular file"
        )
    if owner_state != OWNER_PRESENT:
        raise WriterTransportError("Work Stack server runtime metadata is not available")

    host, port, binding = _resolve_coordinates(store, coordinates_reader)
    expected_uid = expected_workspace_uid(store)
    csrf = _preflight(request_json, host, port, expected_uid)

    if prepare is not None:
        path, body, project = prepare(request_json, host, port)

    # Preflight takes several round trips, and the owner can stop, be replaced
    # or have its advertisement removed during them. Re-observe the same
    # binding immediately before the first mutation: a vanished, unreadable,
    # oversized or replaced advertisement refuses here instead of posting to an
    # owner that no longer exists. This never redirects to a different owner
    # and never repairs or removes the metadata.
    revalidated_host, revalidated_port, revalidated_binding = read_owner_binding(store)
    if (revalidated_host, revalidated_port, revalidated_binding) != (host, port, binding):
        raise WriterTransportError(changed_message)

    headers = _write_headers(
        host, port, csrf, method, idempotency_key, extra_headers, keyless_post
    )

    try:
        status, payload = request_json(
            host, port, method, path, body=body, headers=headers
        )
    except AMBIGUOUS_TRANSPORT as error:
        if method != "POST" or not replay:
            # No idempotency record exists on this route, so a second attempt
            # could not be recognised as a replay: it would either be refused
            # as stale or, for a no-op, silently applied again. One attempt
            # only, and the outcome stays unknown rather than being guessed.
            raise CommitUnknownError(unknown_message) from error
        # The request went out and the outcome is unknown: a lost connection, a
        # truncated read, or an unparseable body all leave the record possibly
        # created. Replay the identical bytes under the identical key exactly
        # once and let the server's idempotency record decide.
        try:
            status, payload = request_json(
                host, port, "POST", path, body=body, headers=headers
            )
        except AMBIGUOUS_TRANSPORT as error:
            raise CommitUnknownError(unknown_message) from error

    if 200 <= status < 300:
        if project_result is not None:
            # A caller that must judge the status as well as the body, such as
            # a route whose only success pairings are 201/false and 200/true.
            # Its refusal is raised as CommitUnknownError by the caller itself,
            # because the request DID reach the owner: reporting a determinate
            # refusal after a possible commit would be a false claim. Older
            # callers keep the existing determinate error policy.
            return project_result(status, payload)
        return project(payload)
    # A determinate HTTP status is an answer, not an ambiguity. Do not retry,
    # and do not surface the server's raw error text.
    raise WriterTransportError(refused_message.format(status))
