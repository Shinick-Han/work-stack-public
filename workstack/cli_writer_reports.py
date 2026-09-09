"""Owner-required daily report create: preview once, then POST one draft.

Preparation runs after owner preflight and before the final advertisement
revalidation. It never retries the preview GET, never copies unrelated
preview siblings into the mutation, and never mints a new idempotency key
inside the shared identical POST replay.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Mapping
from urllib.parse import urlencode

from .cli_writer_owner import (
    CommitUnknownError,
    CoordinatesReader,
    RequestJson,
    WriterTransportError,
    _preflight_get,
    expected_workspace_uid,
)
from .cli_writer_transport import _forward_write
from .report_documents import (
    REPORTS_DOCUMENT_VERSION,
    ReportDocumentError,
    normalize_report_request,
    validate_reports_document,
)


TEMPLATE = "daily-v1"
PREVIEW_ROUTE = "/api/v1/reports/daily-preview"
CREATE_ROUTE = "/api/v1/reports"
UNKNOWN = "report create commit is unknown; inspect the reports before retrying"
CREATE_DATA_KEYS = frozenset({
    "uid",
    "workspace_uid",
    "template",
    "period",
    "source_digest",
    "source_generated_at",
    "state",
    "revision",
    "archived_from_state",
    "archived_at",
    "archive_note",
    "created_at",
    "updated_at",
    "content_entry",
    "source_stale",
})
CONTENT_KEYS = frozenset({
    "content_revision",
    "document_revision",
    "markdown",
    "authored_at",
    "note",
})


def new_report_idempotency_key() -> str:
    """One random report-prefixed key per explicit invocation."""

    return "cli-report-{}".format(uuid.uuid4().hex)


def _canonical_day(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _unknown() -> CommitUnknownError:
    return CommitUnknownError(UNKNOWN)


def _preview_path(date: str, workspace_uid: str) -> str:
    return PREVIEW_ROUTE + "?" + urlencode(
        {"date": date, "template": TEMPLATE, "workspace_uid": workspace_uid}
    )


def _create_path(workspace_uid: str) -> str:
    return CREATE_ROUTE + "?" + urlencode({"workspace_uid": workspace_uid})


def _create_body_from_preview(
    payload: Mapping[str, object], workspace_uid: str, date: str
) -> dict[str, object]:
    """Build the frozen six-field create body from one daily-preview GET."""

    if payload.get("workspace_uid") != workspace_uid:
        raise WriterTransportError(
            "the running Work Stack server owns a different workspace identity"
        )
    preview = payload.get("preview")
    if type(preview) is not dict:
        raise WriterTransportError("Work Stack server daily preview response is invalid")
    expected_period = {"kind": "day", "date": date}
    if preview.get("template") != TEMPLATE or preview.get("period") != expected_period:
        raise WriterTransportError("Work Stack server daily preview response is invalid")
    return {
        "workspace_uid": workspace_uid,
        "template": TEMPLATE,
        "period": {"kind": "day", "date": date},
        "source_digest": payload.get("source_digest"),
        "source_generated_at": preview.get("generated_at"),
        "markdown": preview.get("markdown"),
    }


def _admit_create(body: dict[str, object]) -> dict[str, object]:
    try:
        return normalize_report_request("create", body)
    except ReportDocumentError as error:
        raise WriterTransportError(
            "Work Stack server daily preview response is invalid"
        ) from error


def _valid_envelope(
    status: int, payload: Mapping[str, object]
) -> tuple[dict[str, object], bool]:
    if not isinstance(payload, Mapping) or set(payload) != {"data", "meta"}:
        raise _unknown()
    data = payload.get("data")
    meta = payload.get("meta")
    if type(data) is not dict or type(meta) is not dict:
        raise _unknown()
    if set(meta) != {"replayed"}:
        raise _unknown()
    replayed = meta["replayed"]
    if type(replayed) is not bool:
        raise _unknown()
    if (status, replayed) not in ((201, False), (201, True)):
        raise _unknown()
    return data, replayed


def _report_uid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return (
        parsed.version == 4
        and parsed.variant == uuid.RFC_4122
        and str(parsed) == value
    )


def _content_bound(entry: object, markdown: object) -> bool:
    if type(entry) is not dict or set(entry) != CONTENT_KEYS:
        return False
    return entry["markdown"] == markdown and entry["note"] is None


def _admit_created_document(
    data: Mapping[str, object], workspace_uid: object
) -> None:
    """Reuse the persisted report model for create-success scalars.

    The HTTP create body is the stored report with ``content_entry`` instead of
    ``revisions`` and with ``source_stale``. Projecting one report through the
    public validator admits exact-integer revisions, null draft archive
    fields, and the model's created/updated/authored time domain.
    """

    report = {
        key: value
        for key, value in data.items()
        if key not in ("content_entry", "source_stale")
    }
    report["revisions"] = [data["content_entry"]]
    try:
        validate_reports_document(
            {
                "version": REPORTS_DOCUMENT_VERSION,
                "reports": [report],
                "idempotency": [],
            },
            workspace_uid=workspace_uid,
        )
    except ReportDocumentError as error:
        raise _unknown() from error


def _created_report(
    status: int, payload: Mapping[str, object], body: Mapping[str, object]
) -> dict[str, object]:
    data, replayed = _valid_envelope(status, payload)
    if set(data) != CREATE_DATA_KEYS:
        raise _unknown()
    if not _report_uid(data.get("uid")):
        raise _unknown()
    if data["workspace_uid"] != body["workspace_uid"]:
        raise _unknown()
    if data["template"] != body["template"] or data["period"] != body["period"]:
        raise _unknown()
    if data["source_digest"] != body["source_digest"]:
        raise _unknown()
    if data["source_generated_at"] != body["source_generated_at"]:
        raise _unknown()
    if data["state"] != "draft":
        raise _unknown()
    if data["source_stale"] is not False:
        raise _unknown()
    if not _content_bound(data.get("content_entry"), body["markdown"]):
        raise _unknown()
    _admit_created_document(data, body["workspace_uid"])
    if data["revision"] != 1:
        raise _unknown()
    return {
        "uid": data["uid"],
        "workspace_uid": data["workspace_uid"],
        "template": data["template"],
        "period": data["period"],
        "state": data["state"],
        "revision": data["revision"],
        "source_digest": data["source_digest"],
        "replayed": replayed,
    }


def forward_report_create(
    store: object,
    owner_state: str,
    date: str,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Preview the explicit day once, then create one daily-v1 draft."""

    if not _canonical_day(date):
        raise ValueError("date must be a canonical civil day (YYYY-MM-DD)")
    workspace_uid = expected_workspace_uid(store)
    key = idempotency_key or new_report_idempotency_key()
    prepared: dict[str, dict[str, object]] = {}

    def prepare(request, host, port):
        payload = _preflight_get(
            request, host, port, _preview_path(date, workspace_uid), "daily preview"
        )
        admitted = _admit_create(
            _create_body_from_preview(payload, workspace_uid, date)
        )
        prepared["body"] = admitted
        path = _create_path(workspace_uid)
        return path, admitted, lambda _payload: {}

    def project_result(status, payload):
        return _created_report(status, payload, prepared["body"])

    return _forward_write(
        store,
        owner_state,
        path=CREATE_ROUTE,
        body={},
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=key,
        project=lambda payload: {},
        project_result=project_result,
        prepare=prepare,
        changed_message=(
            "Work Stack server runtime metadata changed before the report was sent"
        ),
        unknown_message=UNKNOWN,
        refused_message="the running Work Stack server refused the report (HTTP {})",
    )


__all__ = ("forward_report_create", "new_report_idempotency_key")
