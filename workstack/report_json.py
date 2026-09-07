"""What a report value has to be before any report rule is applied to it.

`workstack.report_documents` decides what a report *means*: which states follow
which, what a period may cover, when a receipt may be replayed. This module
decides the question underneath that one — whether the value in hand is
something the feature can carry at all: an exact built-in JSON tree, encodable
as UTF-8, finite, acyclic, no deeper than `MAX_REPORT_JSON_DEPTH`, and shaped
like the scalar the caller asked for.

Keeping the two apart means a shape rule is never entangled with a meaning
rule, and it lets this layer stay stdlib-only. It imports nothing from the
product, performs no IO, reads no clock and allocates no identity, so it can
sit underneath every other module without a cycle.

`ReportDocumentError` lives here because this layer raises it, and
`workstack.report_documents` re-exports it: the public refusal type of the
report feature is the one class, wherever it was raised. Everything else here
is private to the two modules.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, NoReturn


# Scalars are depth 0 and a root container is depth 1, so 16 admits a deeply
# nested response body while refusing anything that would recurse without end.
MAX_REPORT_JSON_DEPTH = 16

# Structural names only. A field names a position, never a value, so putting
# one in a refusal leaks nothing about what the caller sent.
_FIELDS = frozenset({
    "document", "reports", "idempotency", "request", "operation", "response_body",
    "markdown", "note", "period", "template", "source_digest", "expected_revision",
    "workspace_uid", "report_uid", "key", "now",
})
_MESSAGES = {
    "report_body_invalid": "report request or document is invalid",
    "report_template_unsupported": "report template is not supported by this build",
    "report_not_found": "report was not found in this workspace",
    "report_state_invalid": "report transition is not allowed from its current state",
    "report_revision_conflict": "report revision does not match the expected revision",
    "report_duplicate_period": "another active report already covers this period",
    "report_revision_limit": "report revision history is full",
    "report_document_limit": "this workspace holds the maximum number of reports",
    "report_storage_full": "report storage is full",
    "report_source_changed": "report source changed since the preview was taken",
    "idempotency_conflict": "idempotency key was already used for a different request",
    "report_idempotency_capacity": "idempotency receipts are exhausted for now",
}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ")
# The daily preview's generated_at contract: an RFC 3339 instant with an
# explicit offset, optionally carrying fractional seconds.
_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_INFINITY = float("inf")


class ReportDocumentError(ValueError):
    """A refusal that names what failed without quoting anything that failed.

    `code` and `field` are the machine-readable half; `retry_after_seconds` is
    populated for `report_idempotency_capacity` alone. Mapping any of this onto
    an HTTP status is the transport layer's job, not this one's.
    """

    def __init__(
        self,
        code: str,
        field: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.field = field
        self.retry_after_seconds = retry_after_seconds


def _refuse(
    code: str, field: str | None = None, retry_after_seconds: int | None = None
) -> NoReturn:
    if field is not None and field not in _FIELDS:
        raise KeyError("refusal field is not on the allowlist")
    raise ReportDocumentError(code, field, retry_after_seconds)


def _encodable(value: str) -> bool:
    """False for a lone surrogate, which is a str Python holds but JSON cannot."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _walk_leaf(value: Any, field: str) -> bool:
    """Judge one non-container, and say whether it was one.

    Only the exact built-in JSON scalars pass, so a bool never counts as a
    number and a str subclass never counts as a string.
    """

    if value is None or type(value) is bool or type(value) is int:
        return True
    if type(value) is float:
        if value != value or value == _INFINITY or value == -_INFINITY:
            _refuse("report_body_invalid", field)
        return True
    if type(value) is str:
        if not _encodable(value):
            _refuse("report_body_invalid", field)
        return True
    if type(value) is not dict and type(value) is not list:
        _refuse("report_body_invalid", field)
    return False


def _walk(value: Any, field: str, path: tuple[int, ...]) -> None:
    """Reject anything JSON cannot carry, and bound depth while recursing.

    Depth is checked on the way down rather than afterwards, so a pathological
    input cannot exhaust the interpreter stack before it is refused, and the
    path of open containers doubles as the cycle detector.
    """

    if _walk_leaf(value, field):
        return
    if id(value) in path:
        _refuse("report_body_invalid", field)
    inner = path + (id(value),)
    if len(inner) > MAX_REPORT_JSON_DEPTH:
        _refuse("report_body_invalid", field)
    items: Any = value
    if type(value) is dict:
        for key in value:
            if type(key) is not str or not _encodable(key):
                _refuse("report_body_invalid", field)
        items = value.values()
    for item in items:
        _walk(item, field, inner)


def _json_safe(value: Any, field: str) -> None:
    _walk(value, field, ())


def _canonical(value: Any, field: str) -> bytes:
    """The one serialization every byte cap in this feature is measured against."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _refuse("report_body_invalid", field)


def _copy(value: Any) -> Any:
    """Rebuild an already checked tree so the result shares nothing with it."""

    if type(value) is dict:
        return {key: _copy(item) for key, item in value.items()}
    if type(value) is list:
        return [_copy(item) for item in value]
    return value


def _exact_dict(value: Any, keys: frozenset[str], field: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _refuse("report_body_invalid", field)
    return value


def _exact_str(value: Any, field: str) -> str:
    if type(value) is not str:
        _refuse("report_body_invalid", field)
    return value


def _exact_int(value: Any, field: str) -> int:
    """`type(True) is int` is False, so a bool never passes as a number here."""

    if type(value) is not int:
        _refuse("report_body_invalid", field)
    return value


def _matched(pattern: re.Pattern[str], value: Any, field: str) -> str:
    text = _exact_str(value, field)
    if pattern.fullmatch(text) is None:
        _refuse("report_body_invalid", field)
    return text


def _moment(value: Any, field: str) -> dt.datetime:
    """A canonical second-precision UTC stamp, parsed only so it can be compared."""

    text = _matched(_STAMP, value, field)
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _refuse("report_body_invalid", field)
    return parsed.replace(tzinfo=dt.timezone.utc)


def _instant(value: Any, field: str) -> str:
    """An RFC 3339 instant as the shipped daily preview already writes them."""

    text = _matched(_INSTANT, value, field)
    try:
        dt.datetime.fromisoformat(text)
    except ValueError:
        _refuse("report_body_invalid", field)
    return text


def _day(value: Any, field: str) -> str:
    text = _matched(_DATE, value, field)
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError:
        _refuse("report_body_invalid", field)
    if parsed.isoformat() != text:
        _refuse("report_body_invalid", field)
    return text


def _bounded(value: Any, field: str, chars: int, octets: int | None = None) -> str:
    """Authored text is preserved verbatim; only its size is judged."""

    text = _exact_str(value, field)
    if len(text) > chars or not _encodable(text):
        _refuse("report_body_invalid", field)
    if octets is not None and len(text.encode("utf-8")) > octets:
        _refuse("report_body_invalid", field)
    return text
