"""Pure report read and list projections over an already-decoded document.

This is a query leaf for the later transactional service, not an HTTP surface
and not a Store caller. The held `reports.json` value is re-admitted through
`validate_reports_document`; nothing here reads a path, a clock, a UUID
generator or a source day. `source_stale` is the inequality between the
caller-supplied current digest and the digest already recorded on the
document. The caller computes that current digest inside the same held
snapshot. This module never hashes a preview, a markdown body or a ledger.

List order is frozen as `(updated_at DESC, uid ASC)`. `state` is a filter
alias (`active` means draft|finalized) and is never a stored value. The page
size is the builtin integer 50; booleans and other types are refused rather
than coerced. The list cursor is a canonical opaque token: version, a digest
binding of owner plus filter, and the exact `(updated_at, uid)` anchor of the
last emitted row. That digest is a binding, not authentication; owner
admission remains the caller's responsibility. A malformed, oversized,
non-canonical, filter-mismatched, owner-mismatched, missing or changed
anchor is `report_cursor_invalid` rather than a silent restart.

Authored markdown is projected verbatim. This module never generates HTML
and never rewrites facts.

`ReportQueryError` exists only for `invalid_query` and `report_cursor_invalid`.
Model refusals reuse `ReportDocumentError`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any, NoReturn

from .report_documents import ReportDocumentError, validate_reports_document
from .report_json import _copy


__all__ = [
    "ReportQueryError",
    "list_report_documents",
    "read_report_document",
]

LIST_PAGE_SIZE = 50
CURSOR_VERSION = 1
MIN_CURSOR_CHARS = 64
MAX_CURSOR_CHARS = 256
MAX_CURSOR_BYTES = 192

_FILTERS = {
    "active": frozenset({"draft", "finalized"}),
    "archived": frozenset({"archived"}),
    "all": frozenset({"draft", "finalized", "archived"}),
}
_CURSOR_KEYS = frozenset({"binding", "uid", "updated_at", "v"})
_QUERY_FIELDS = frozenset({"workspace_uid", "report_uid", "state", "limit", "cursor"})
_QUERY_MESSAGES = {
    "invalid_query": "report query is invalid",
    "report_cursor_invalid": "report list cursor is invalid",
}

_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_UUID_ANY = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ")
_BINDING = re.compile(r"[0-9a-f]{64}")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ReportQueryError(ValueError):
    """A content-free query refusal: a code, an optional field, no values."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(_QUERY_MESSAGES[code])
        self.code = code
        self.field = field


def list_report_documents(
    document: object,
    /,
    *,
    workspace_uid: object,
    state: object = "active",
    limit: object = LIST_PAGE_SIZE,
    cursor: object = None,
) -> dict[str, object]:
    """Return one bounded list page from a detached, already-decoded document."""

    owner = _admit_owner(workspace_uid)
    alias = _admit_state(state)
    _admit_limit(limit)
    token = _admit_cursor_arg(cursor)
    anchor = None if token is None else _decode_cursor(token, owner, alias)
    top = validate_reports_document(document, workspace_uid=owner)
    ordered = _ordered(top["reports"], _FILTERS[alias])
    return _paginate(ordered, _start_index(ordered, anchor), owner, alias)


def read_report_document(
    document: object,
    /,
    *,
    workspace_uid: object,
    report_uid: object,
    current_source_digest: object,
) -> dict[str, object]:
    """Return one report projection, its full history, and source staleness."""

    owner = _admit_owner(workspace_uid)
    target = _admit_report_uid(report_uid)
    digest = _admit_digest(current_source_digest)
    top = validate_reports_document(document, workspace_uid=owner)
    report = _find_report(top["reports"], target)
    item = _list_item(report)
    item["revisions"] = _copy(report["revisions"])
    item["source_stale"] = report["source_digest"] != digest
    return item


def _admit_owner(value: object) -> str:
    if type(value) is not str or _UUID_ANY.fullmatch(value) is None:
        _refuse_query("invalid_query", "workspace_uid")
    return value


def _admit_report_uid(value: object) -> str:
    if type(value) is not str or _UUID4.fullmatch(value) is None:
        _refuse_query("invalid_query", "report_uid")
    return value


def _admit_state(value: object) -> str:
    if type(value) is not str or value not in _FILTERS:
        _refuse_query("invalid_query", "state")
    return value


def _admit_limit(value: object) -> None:
    if type(value) is not int or value != LIST_PAGE_SIZE:
        _refuse_query("invalid_query", "limit")


def _admit_cursor_arg(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _refuse_query("invalid_query", "cursor")
    return value


def _admit_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ReportDocumentError("report_body_invalid", "source_digest")
    return value


def _ordered(reports: list[dict[str, Any]], wanted: frozenset[str]) -> list[dict[str, Any]]:
    chosen = [item for item in reports if item["state"] in wanted]
    chosen.sort(key=lambda item: item["uid"])
    chosen.sort(key=lambda item: item["updated_at"], reverse=True)
    return chosen


def _start_index(
    ordered: list[dict[str, Any]], anchor: tuple[str, str] | None
) -> int:
    if anchor is None:
        return 0
    stamp, target = anchor
    for index, item in enumerate(ordered):
        if item["updated_at"] == stamp and item["uid"] == target:
            return index + 1
    _refuse_query("report_cursor_invalid", "cursor")


def _paginate(
    ordered: list[dict[str, Any]], start: int, owner: str, alias: str
) -> dict[str, object]:
    window = ordered[start : start + LIST_PAGE_SIZE]
    omitted = len(ordered) - start - len(window)
    token = None
    if omitted > 0:
        last = window[-1]
        token = _encode_cursor(owner, alias, last["updated_at"], last["uid"])
    return {
        "workspace_uid": owner,
        "reports": [_list_item(item) for item in window],
        "omitted_count": omitted,
        "cursor": token,
    }


def _list_item(report: dict[str, Any]) -> dict[str, object]:
    history = report["revisions"]
    return {
        "uid": report["uid"],
        "template": report["template"],
        "period": _copy(report["period"]),
        "state": report["state"],
        "revision": report["revision"],
        "content_revision": history[-1]["content_revision"],
        "source_digest": report["source_digest"],
        "archived_from_state": report["archived_from_state"],
        "created_at": report["created_at"],
        "updated_at": report["updated_at"],
    }


def _find_report(reports: list[dict[str, Any]], target: str) -> dict[str, Any]:
    for item in reports:
        if item["uid"] == target:
            return item
    raise ReportDocumentError("report_not_found", "report_uid")


def _binding(owner: str, state: str) -> str:
    material = json.dumps(
        {"state": state, "workspace_uid": owner},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _encode_payload(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _encode_cursor(owner: str, state: str, updated_at: str, uid: str) -> str:
    return _encode_payload(
        {
            "binding": _binding(owner, state),
            "uid": uid,
            "updated_at": updated_at,
            "v": CURSOR_VERSION,
        }
    )


def _decode_cursor(cursor: str, owner: str, alias: str) -> tuple[str, str]:
    payload = _admit_cursor(cursor)
    if payload["binding"] != _binding(owner, alias):
        _refuse_query("report_cursor_invalid", "cursor")
    return payload["updated_at"], payload["uid"]


def _admit_cursor(cursor: str) -> dict[str, Any]:
    raw = _cursor_bytes(cursor)
    payload = _payload_from_raw(raw)
    _require_payload_fields(payload)
    if _encode_payload(payload) != cursor:
        _refuse_query("report_cursor_invalid", "cursor")
    return payload


def _cursor_bytes(cursor: str) -> bytes:
    if not MIN_CURSOR_CHARS <= len(cursor) <= MAX_CURSOR_CHARS:
        _refuse_query("report_cursor_invalid", "cursor")
    if _CURSOR_RE.fullmatch(cursor) is None:
        _refuse_query("report_cursor_invalid", "cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError):
        _refuse_query("report_cursor_invalid", "cursor")
    if not 1 <= len(raw) <= MAX_CURSOR_BYTES:
        _refuse_query("report_cursor_invalid", "cursor")
    return raw


def _payload_from_raw(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        _refuse_query("report_cursor_invalid", "cursor")
    if type(payload) is not dict or set(payload) != _CURSOR_KEYS:
        _refuse_query("report_cursor_invalid", "cursor")
    return payload


def _require_payload_fields(payload: dict[str, Any]) -> None:
    if type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION:
        _refuse_query("report_cursor_invalid", "cursor")
    if type(payload["binding"]) is not str or _BINDING.fullmatch(payload["binding"]) is None:
        _refuse_query("report_cursor_invalid", "cursor")
    if type(payload["uid"]) is not str or _UUID4.fullmatch(payload["uid"]) is None:
        _refuse_query("report_cursor_invalid", "cursor")
    if type(payload["updated_at"]) is not str or _STAMP.fullmatch(payload["updated_at"]) is None:
        _refuse_query("report_cursor_invalid", "cursor")


def _refuse_query(code: str, field: str) -> NoReturn:
    if field not in _QUERY_FIELDS:
        raise KeyError("refusal field is not on the allowlist")
    raise ReportQueryError(code, field)
