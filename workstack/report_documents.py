"""Pure report-document model: validation, transition planning, ledger planning.

This is step 4 of `docs/REPORT-DOCUMENT-STORAGE-CONTRACT.md` §17 — the model,
the state machine and the validators, with no callers. Nothing here reads or
writes a Store, a repository, a path, a clock or a UUID generator: every
instant and every allocated identity arrives as an argument, so the same input
always plans the same output. `reports.json` is not created, read or saved by
this module, and schema 5 is not activated by importing it.

The transactional layer that arrives in step 5 is the only permitted caller,
and only in this order::

    parse and normalize the body
    prepare_report_replay
      -> replay hit: return the recorded status and body, save nothing
      -> miss:       plan_report_mutation
    compute any source_stale flag from the same held snapshot, build the body
    append_report_receipt
    save the final document through DocumentRepository exactly once

Every refusal is content-free: `ReportDocumentError` carries a fixed sentence
plus a machine-readable `code`, an optional structural `field` name from a
closed allowlist, and `retry_after_seconds` for ledger capacity alone. No
input value, identity, path, markdown, note, digest or response body ever
reaches a message. HTTP status mapping belongs to the transport layer.

No function mutates its arguments. Every returned value is a fresh tree of
built-in JSON types that shares no reference with the input. What a value has
to be before any of these rules apply lives in `workstack.report_json`.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .report_json import (
    MAX_REPORT_JSON_DEPTH,
    ReportDocumentError,
    _bounded,
    _canonical,
    _copy,
    _day,
    _encodable,
    _exact_dict,
    _exact_int,
    _exact_str,
    _instant,
    _json_safe,
    _matched,
    _moment,
    _refuse,
)
from .reporting import MAX_MARKDOWN_CHARS, TEMPLATE_DAILY_V1


__all__ = [
    "MAX_LEDGER_RECORD_BYTES",
    "MAX_REPORTS_BYTES",
    "MAX_REPORT_CONTENT_REVISIONS",
    "MAX_REPORT_DOCUMENTS",
    "MAX_REPORT_DOCUMENT_REVISION",
    "MAX_REPORT_JSON_DEPTH",
    "MAX_REPORT_NOTE_BYTES",
    "MAX_REPORT_NOTE_CHARS",
    "REPORTS_DOCUMENT_VERSION",
    "REPORT_LEDGER_MAX_RECORDS",
    "REPORT_LEDGER_RETENTION_DAYS",
    "ReportDocumentError",
    "append_report_receipt",
    "normalize_report_request",
    "plan_report_mutation",
    "prepare_report_replay",
    "validate_reports_document",
]

REPORTS_DOCUMENT_VERSION = 1
MAX_REPORT_CONTENT_REVISIONS = 20
MAX_REPORT_DOCUMENTS = 500
MAX_REPORTS_BYTES = 8 * 1024 * 1024
REPORT_LEDGER_MAX_RECORDS = 1_000
REPORT_LEDGER_RETENTION_DAYS = 30
MAX_LEDGER_RECORD_BYTES = 192 * 1024
# Repeated as a literal rather than imported from workstack.store, which would
# drag the whole released store into a module the contract requires to be pure.
# The test suite asserts the two stay equal.
MAX_REPORT_DOCUMENT_REVISION = 9_007_199_254_740_991
# The contract calls the note "bounded" without naming a number. These fix it
# once, so the later wire and repository layers reuse the same two caps instead
# of inventing their own.
MAX_REPORT_NOTE_CHARS = 240
MAX_REPORT_NOTE_BYTES = 1_024

_TOP_KEYS = frozenset({"version", "reports", "idempotency"})
_REPORT_KEYS = frozenset({
    "uid", "workspace_uid", "template", "period", "source_digest",
    "source_generated_at", "state", "revision", "archived_from_state",
    "archived_at", "archive_note", "revisions", "created_at", "updated_at",
})
_CONTENT_KEYS = frozenset({
    "content_revision", "document_revision", "markdown", "authored_at", "note",
})
_LEDGER_KEYS = frozenset({
    "key", "method", "path", "request_digest", "response_status",
    "response_body", "created_at",
})
_PERIOD_KEYS = frozenset({"kind", "date"})
_BODY_KEYS = frozenset({"data", "meta"})
_META_KEYS = frozenset({"replayed"})

_ARCHIVE_FIELDS = ("archived_from_state", "archived_at", "archive_note")
_STATES = frozenset({"draft", "finalized", "archived"})
_ARCHIVED_FROM = frozenset({"draft", "finalized"})
_LEDGER_STATUSES = frozenset({200, 201})
_LEDGER_METHOD = "POST"
_PERIOD_KIND = "day"

# Which states each transition may start from — contract §5.3, verbatim.
_ALLOWED_STATES = {
    "revise": frozenset({"draft", "finalized"}),
    "finalize": frozenset({"draft"}),
    "archive": frozenset({"draft", "finalized"}),
    "restore": frozenset({"archived"}),
}
_REQUEST_KEYS = {
    "create": frozenset({
        "workspace_uid", "template", "period", "source_digest",
        "source_generated_at", "markdown",
    }),
    "revise": frozenset({"workspace_uid", "expected_revision", "markdown", "note"}),
    "finalize": frozenset({"workspace_uid", "expected_revision"}),
    "archive": frozenset({"workspace_uid", "expected_revision", "note"}),
    "restore": frozenset({"workspace_uid", "expected_revision"}),
}

_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
# Any non-nil canonical lowercase RFC 4122 identity. The variant nibble carries
# both requirements: it is what makes the value RFC 4122, and no all-zero nil
# UUID can satisfy it. The version nibble stays open, because pinning it to the
# versions that existed today would refuse a valid authority tomorrow. Report
# identities are narrower and keep their own pattern above.
_UUID_ANY = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_LEDGER_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")

_RETENTION = dt.timedelta(days=REPORT_LEDGER_RETENTION_DAYS)


# --------------------------------------------------- report-specific scalars


def _note(value: Any) -> None:
    if value is not None:
        _bounded(value, "note", MAX_REPORT_NOTE_CHARS, MAX_REPORT_NOTE_BYTES)


def _markdown(value: Any) -> str:
    return _bounded(value, "markdown", MAX_MARKDOWN_CHARS)


def _template(value: Any) -> str:
    """A malformed template is a body defect; a well-formed unknown one is not."""

    text = _exact_str(value, "template")
    if text != TEMPLATE_DAILY_V1:
        _refuse("report_template_unsupported", "template")
    return text


def _period(value: Any) -> tuple[str, str]:
    period = _exact_dict(value, _PERIOD_KEYS, "period")
    if _exact_str(period["kind"], "period") != _PERIOD_KIND:
        _refuse("report_body_invalid", "period")
    return (_PERIOD_KIND, _day(period["date"], "period"))


def _route(value: Any) -> str:
    text = _exact_str(value, "idempotency")
    if not text or not _encodable(text):
        _refuse("report_body_invalid", "idempotency")
    return text


def _stamp_text(value: object) -> str:
    """The caller's instant, returned unchanged once proved to be canonical."""

    _moment(value, "now")
    return _exact_str(value, "now")


def _coverage(report: dict[str, Any]) -> tuple[str, str, str]:
    return (report["template"], *_period(report["period"]))


# ------------------------------------------------------- document validation


def _validate_archive_fields(report: dict[str, Any], state: str) -> None:
    if state != "archived":
        if any(report[name] is not None for name in _ARCHIVE_FIELDS):
            _refuse("report_body_invalid", "reports")
        return
    if _exact_str(report["archived_from_state"], "reports") not in _ARCHIVED_FROM:
        _refuse("report_body_invalid", "reports")
    _moment(report["archived_at"], "reports")
    _note(report["archive_note"])


def _validate_history(entries: Any, revision: int) -> None:
    """content_revision is exactly 1..N; document_revision strictly ascends."""

    if type(entries) is not list:
        _refuse("report_body_invalid", "reports")
    if not 1 <= len(entries) <= MAX_REPORT_CONTENT_REVISIONS:
        _refuse("report_body_invalid", "reports")
    previous = 0
    for index, item in enumerate(entries):
        entry = _exact_dict(item, _CONTENT_KEYS, "reports")
        if _exact_int(entry["content_revision"], "reports") != index + 1:
            _refuse("report_body_invalid", "reports")
        written = _exact_int(entry["document_revision"], "reports")
        if not previous < written <= revision:
            _refuse("report_body_invalid", "reports")
        previous = written
        _markdown(entry["markdown"])
        _moment(entry["authored_at"], "reports")
        _note(entry["note"])


def _validate_report(record: Any, owner: str) -> tuple[str, tuple[str, str, str] | None]:
    """Return the report's uid, and the period it occupies while not archived."""

    report = _exact_dict(record, _REPORT_KEYS, "reports")
    uid = _matched(_UUID4, report["uid"], "reports")
    if _matched(_UUID_ANY, report["workspace_uid"], "workspace_uid") != owner:
        _refuse("report_body_invalid", "workspace_uid")
    _template(report["template"])
    _matched(_DIGEST, report["source_digest"], "source_digest")
    _instant(report["source_generated_at"], "reports")
    state = _exact_str(report["state"], "reports")
    if state not in _STATES:
        _refuse("report_body_invalid", "reports")
    revision = _exact_int(report["revision"], "reports")
    if not 1 <= revision <= MAX_REPORT_DOCUMENT_REVISION:
        _refuse("report_body_invalid", "reports")
    _validate_archive_fields(report, state)
    _validate_history(report["revisions"], revision)
    created = _moment(report["created_at"], "reports")
    if _moment(report["updated_at"], "reports") < created:
        _refuse("report_body_invalid", "reports")
    return uid, None if state == "archived" else _coverage(report)


def _holds_history(value: Any) -> bool:
    """A stored receipt must not carry the full authored history."""

    if type(value) is dict:
        if type(value.get("revisions")) is list:
            return True
        return any(_holds_history(item) for item in value.values())
    if type(value) is list:
        return any(_holds_history(item) for item in value)
    return False


def _validate_response_body(value: Any) -> None:
    body = _exact_dict(value, _BODY_KEYS, "response_body")
    if type(body["data"]) is not dict:
        _refuse("report_body_invalid", "response_body")
    meta = _exact_dict(body["meta"], _META_KEYS, "response_body")
    if meta["replayed"] is not False:
        _refuse("report_body_invalid", "response_body")
    if _holds_history(body["data"]):
        _refuse("report_body_invalid", "response_body")


def _validate_receipt(record: Any) -> str:
    receipt = _exact_dict(record, _LEDGER_KEYS, "idempotency")
    key = _matched(_LEDGER_KEY, receipt["key"], "key")
    if _exact_str(receipt["method"], "idempotency") != _LEDGER_METHOD:
        _refuse("report_body_invalid", "idempotency")
    _route(receipt["path"])
    _matched(_DIGEST, receipt["request_digest"], "idempotency")
    if _exact_int(receipt["response_status"], "idempotency") not in _LEDGER_STATUSES:
        _refuse("report_body_invalid", "idempotency")
    _validate_response_body(receipt["response_body"])
    _moment(receipt["created_at"], "idempotency")
    if len(_canonical(receipt, "response_body")) > MAX_LEDGER_RECORD_BYTES:
        _refuse("report_body_invalid", "response_body")
    return key


def _validate_reports(reports: Any, owner: str) -> None:
    if type(reports) is not list or len(reports) > MAX_REPORT_DOCUMENTS:
        _refuse("report_body_invalid", "reports")
    identities: set[str] = set()
    occupied: set[tuple[str, str, str]] = set()
    for record in reports:
        uid, coverage = _validate_report(record, owner)
        if uid in identities:
            _refuse("report_body_invalid", "reports")
        identities.add(uid)
        if coverage is None:
            continue
        if coverage in occupied:
            _refuse("report_body_invalid", "reports")
        occupied.add(coverage)


def _validate_ledger(ledger: Any) -> None:
    if type(ledger) is not list or len(ledger) > REPORT_LEDGER_MAX_RECORDS:
        _refuse("report_body_invalid", "idempotency")
    keys: set[str] = set()
    for record in ledger:
        key = _validate_receipt(record)
        if key in keys:
            _refuse("report_body_invalid", "idempotency")
        keys.add(key)


def validate_reports_document(
    document: object, /, *, workspace_uid: object
) -> dict[str, object]:
    """Admit a persisted `reports.json` value and return a detached copy.

    Nothing is repaired, defaulted, sorted, pruned or normalized. A document
    that does not already satisfy the contract is refused, so a caller can
    never write back a silently corrected version of what it read.
    """

    owner = _matched(_UUID_ANY, workspace_uid, "workspace_uid")
    _json_safe(document, "document")
    top = _exact_dict(document, _TOP_KEYS, "document")
    if _exact_int(top["version"], "document") != REPORTS_DOCUMENT_VERSION:
        _refuse("report_body_invalid", "document")
    _validate_reports(top["reports"], owner)
    _validate_ledger(top["idempotency"])
    if len(_canonical(top, "document")) > MAX_REPORTS_BYTES:
        _refuse("report_storage_full", "document")
    return _copy(top)


# --------------------------------------------------------- request admission


def _operation(value: object) -> str:
    name = _exact_str(value, "operation")
    if name not in _REQUEST_KEYS:
        _refuse("report_body_invalid", "operation")
    return name


def normalize_report_request(operation: object, request: object, /) -> dict[str, object]:
    """Admit one mutation body and return a detached copy of exactly its keys.

    `markdown` belongs to create and revise only, and `note` to revise and
    archive only; anywhere else they are unknown keys and the body is refused.
    """

    name = _operation(operation)
    body = _exact_dict(request, _REQUEST_KEYS[name], "request")
    _json_safe(body, "request")
    _matched(_UUID_ANY, body["workspace_uid"], "workspace_uid")
    if name == "create":
        _template(body["template"])
        _period(body["period"])
        _matched(_DIGEST, body["source_digest"], "source_digest")
        _instant(body["source_generated_at"], "request")
        _markdown(body["markdown"])
        return _copy(body)
    expected = _exact_int(body["expected_revision"], "expected_revision")
    if not 1 <= expected <= MAX_REPORT_DOCUMENT_REVISION:
        _refuse("report_body_invalid", "expected_revision")
    if name == "revise":
        _markdown(body["markdown"])
    if name in ("revise", "archive"):
        _note(body["note"])
    return _copy(body)


# ------------------------------------------------------------ mutation plans


def _result(
    top: dict[str, Any],
    report: dict[str, Any],
    entry: dict[str, Any] | None,
    reopened: bool,
) -> dict[str, object]:
    summary = {key: _copy(value) for key, value in report.items() if key != "revisions"}
    return {
        "document": top,
        "report": summary,
        "content_entry": None if entry is None else _copy(entry),
        "reopened": reopened,
    }


def _plan_create(
    top: dict[str, Any], body: dict[str, Any], target: str, asserted: object, now: str
) -> dict[str, object]:
    reports = top["reports"]
    if any(item["uid"] == target for item in reports):
        _refuse("report_body_invalid", "report_uid")
    current = _matched(_DIGEST, asserted, "source_digest")
    occupied = {_coverage(item) for item in reports if item["state"] != "archived"}
    if (body["template"], *_period(body["period"])) in occupied:
        _refuse("report_duplicate_period", "period")
    if len(reports) >= MAX_REPORT_DOCUMENTS:
        _refuse("report_document_limit", "reports")
    if body["source_digest"] != current:
        _refuse("report_source_changed", "source_digest")
    entry = {
        "content_revision": 1,
        "document_revision": 1,
        "markdown": body["markdown"],
        "authored_at": now,
        "note": None,
    }
    report = {
        "uid": target,
        "workspace_uid": body["workspace_uid"],
        "template": body["template"],
        "period": _copy(body["period"]),
        "source_digest": body["source_digest"],
        "source_generated_at": body["source_generated_at"],
        "state": "draft",
        "revision": 1,
        "archived_from_state": None,
        "archived_at": None,
        "archive_note": None,
        "revisions": [_copy(entry)],
        "created_at": now,
        "updated_at": now,
    }
    top["reports"] = reports + [report]
    return _result(top, report, entry, False)


def _locate(reports: list[dict[str, Any]], target: str) -> int:
    for index, item in enumerate(reports):
        if item["uid"] == target:
            return index
    _refuse("report_not_found", "report_uid")


def _appended_entry(
    updated: dict[str, Any], body: dict[str, Any], now: str
) -> dict[str, Any]:
    """Authored history is never dropped, so a full history refuses the write."""

    history = updated["revisions"]
    if len(history) >= MAX_REPORT_CONTENT_REVISIONS:
        _refuse("report_revision_limit", "reports")
    entry = {
        "content_revision": len(history) + 1,
        "document_revision": updated["revision"],
        "markdown": body["markdown"],
        "authored_at": now,
        "note": body["note"],
    }
    history.append(_copy(entry))
    return entry


def _require_free_period(reports: list[dict[str, Any]], report: dict[str, Any]) -> None:
    coverage = _coverage(report)
    for item in reports:
        if item["uid"] == report["uid"] or item["state"] == "archived":
            continue
        if _coverage(item) == coverage:
            _refuse("report_duplicate_period", "period")


def _apply_transition(
    top: dict[str, Any],
    name: str,
    body: dict[str, Any],
    report: dict[str, Any],
    now: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    state = report["state"]
    updated = _copy(report)
    updated["revision"] = report["revision"] + 1
    updated["updated_at"] = now
    if name == "revise":
        entry = _appended_entry(updated, body, now)
        updated["state"] = "draft"
        return updated, entry, state == "finalized"
    if name == "finalize":
        updated["state"] = "finalized"
        return updated, None, False
    if name == "archive":
        updated["state"] = "archived"
        updated["archived_from_state"] = state
        updated["archived_at"] = now
        updated["archive_note"] = body["note"]
        return updated, None, False
    _require_free_period(top["reports"], report)
    updated["state"] = report["archived_from_state"]
    updated["archived_from_state"] = None
    updated["archived_at"] = None
    updated["archive_note"] = None
    return updated, None, False


def _plan_transition(
    top: dict[str, Any], name: str, body: dict[str, Any], target: str, now: str
) -> dict[str, object]:
    reports = top["reports"]
    index = _locate(reports, target)
    report = reports[index]
    if report["revision"] != body["expected_revision"]:
        _refuse("report_revision_conflict", "expected_revision")
    if report["state"] not in _ALLOWED_STATES[name]:
        _refuse("report_state_invalid", "document")
    if report["revision"] >= MAX_REPORT_DOCUMENT_REVISION:
        _refuse("report_revision_limit", "reports")
    if _moment(now, "now") < _moment(report["updated_at"], "reports"):
        _refuse("report_body_invalid", "now")
    updated, entry, reopened = _apply_transition(top, name, body, report, now)
    reports[index] = updated
    return _result(top, updated, entry, reopened)


def plan_report_mutation(
    document: object,
    operation: object,
    request: object,
    /,
    *,
    report_uid: object = None,
    current_source_digest: object = None,
    now: object,
) -> dict[str, object]:
    """Plan one state transition and return the document it would produce.

    The document and the request are re-admitted here, so calling this without
    `normalize_report_request` first is safe; the transactional layer still
    normalizes first, because the contract admits the body before it consults
    the ledger.

    `report_uid` is the caller-allocated UUIDv4 for create and the target UID
    the route selected for every other operation. `current_source_digest` is
    the digest computed from the held snapshot and is create-only; passing one
    anywhere else is refused rather than ignored.

    The exact-revision check runs before the state check, so a caller holding a
    stale revision is told that, not that its transition was impossible. No
    `source_stale` flag, response envelope, status, request digest or receipt
    is produced here.
    """

    name = _operation(operation)
    body = normalize_report_request(name, request)
    top = validate_reports_document(document, workspace_uid=body["workspace_uid"])
    stamp = _stamp_text(now)
    target = _matched(_UUID4, report_uid, "report_uid")
    if name == "create":
        return _plan_create(top, body, target, current_source_digest, stamp)
    if current_source_digest is not None:
        _refuse("report_body_invalid", "source_digest")
    return _plan_transition(top, name, body, target, stamp)


# --------------------------------------------------------------- ledger plans


def _identity(
    workspace_uid: object,
    key: object,
    method: object,
    path: object,
    request_digest: object,
    now: object,
) -> tuple[str, str, str, str, str, dt.datetime]:
    owner = _matched(_UUID_ANY, workspace_uid, "workspace_uid")
    wanted = _matched(_LEDGER_KEY, key, "key")
    if _exact_str(method, "idempotency") != _LEDGER_METHOD:
        _refuse("report_body_invalid", "idempotency")
    route = _route(path)
    digest = _matched(_DIGEST, request_digest, "idempotency")
    return owner, wanted, _LEDGER_METHOD, route, digest, _moment(now, "now")


def _surviving(top: dict[str, Any], moment: dt.datetime) -> list[dict[str, Any]]:
    """Drop only receipts whose retention window has already closed.

    Age is measured by subtracting the stamps rather than by adding the window
    to one of them: a receipt written near the end of the representable
    calendar makes that addition overflow, and a value inside the range this
    contract already admits must never raise anything but a refusal. Both
    forms agree everywhere else, including a receipt stamped in the future,
    which is younger than nothing and therefore survives.
    """

    return [
        record
        for record in top["idempotency"]
        if moment - _moment(record["created_at"], "idempotency") < _RETENTION
    ]


def prepare_report_replay(
    document: object,
    /,
    *,
    workspace_uid: object,
    key: object,
    method: object,
    path: object,
    request_digest: object,
    now: object,
) -> dict[str, object]:
    """Decide whether this key has already been answered, and prune expiry.

    The returned document is a pruned candidate, not a saved one. A replay
    returns exactly what was recorded, with `meta.replayed` flipped to true and
    nothing else touched, so a retry after ten later mutations still gets its
    original answer. Capacity is not judged here: an exact replay must succeed
    even when the ledger is full.
    """

    owner, wanted, verb, route, digest, moment = _identity(
        workspace_uid, key, method, path, request_digest, now
    )
    top = validate_reports_document(document, workspace_uid=owner)
    top["idempotency"] = _surviving(top, moment)
    replay: dict[str, object] | None = None
    for record in top["idempotency"]:
        if record["key"] != wanted:
            continue
        recorded = (record["method"], record["path"], record["request_digest"])
        if recorded != (verb, route, digest):
            _refuse("idempotency_conflict", "key")
        body = _copy(record["response_body"])
        body["meta"]["replayed"] = True
        replay = {"response_status": record["response_status"], "response_body": body}
        break
    return {"document": top, "replay": replay}


def _retry_after(records: list[dict[str, Any]], moment: dt.datetime) -> int:
    """Seconds until the oldest surviving receipt leaves the retention window.

    Derived as the window minus the age, for the reason `_surviving` subtracts,
    and summed from the timedelta's own fields so no float rounding stands
    between the caller and a whole number of seconds.
    """

    earliest = min(_moment(record["created_at"], "idempotency") for record in records)
    remaining = _RETENTION - (moment - earliest)
    return max(1, remaining.days * 86_400 + remaining.seconds)


def append_report_receipt(
    document: object,
    /,
    *,
    workspace_uid: object,
    key: object,
    method: object,
    path: object,
    request_digest: object,
    response_status: object,
    response_body: object,
    now: object,
) -> dict[str, object]:
    """Record one immutable receipt and return the document that would be saved.

    A full ledger of unexpired receipts refuses the mutation rather than
    deleting a fresh receipt to make room, because dropping one would turn a
    replayable retry into a duplicate write. Nothing already recorded is
    reordered, rewritten or removed except by expiry.
    """

    owner, wanted, verb, route, digest, moment = _identity(
        workspace_uid, key, method, path, request_digest, now
    )
    top = validate_reports_document(document, workspace_uid=owner)
    status = _exact_int(response_status, "idempotency")
    if status not in _LEDGER_STATUSES:
        _refuse("report_body_invalid", "idempotency")
    _json_safe(response_body, "response_body")
    _validate_response_body(response_body)
    surviving = _surviving(top, moment)
    if any(record["key"] == wanted for record in surviving):
        _refuse("idempotency_conflict", "key")
    if len(surviving) >= REPORT_LEDGER_MAX_RECORDS:
        _refuse(
            "report_idempotency_capacity", "idempotency", _retry_after(surviving, moment)
        )
    receipt = {
        "key": wanted,
        "method": verb,
        "path": route,
        "request_digest": digest,
        "response_status": status,
        "response_body": _copy(response_body),
        "created_at": _stamp_text(now),
    }
    if len(_canonical(receipt, "response_body")) > MAX_LEDGER_RECORD_BYTES:
        _refuse("report_body_invalid", "response_body")
    top["idempotency"] = surviving + [receipt]
    if len(_canonical(top, "document")) > MAX_REPORTS_BYTES:
        _refuse("report_storage_full", "document")
    return top
