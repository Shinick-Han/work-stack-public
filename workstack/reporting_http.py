"""Read-only daily-v1 preview HTTP adapter.

Query parsing, owning-Store identity/sync checks, and preview assembly live
here so ``server.py`` only registers the GET route. This module does not
adopt, initialize, rebind, or write Store documents, and it does not change
the ``preview_daily_report`` core.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any
from urllib.parse import parse_qs

# The digest itself is a pure foundation leaf: the preview answers with it, the
# report composer compares against it, and neither may import this adapter to
# reach it. Re-exported here so the released `day_source_digest` import path
# keeps naming the one function all three surfaces call.
from .report_context_catalog import build_context_catalog
from .report_source_digest import day_source_digest as day_source_digest
from .reporting import (
    TEMPLATE_DAILY_V1,
    DailyReportPreviewError,
    preview_daily_report,
)
from .store import StoreCorruptError


REQUIRED_QUERY_KEYS = ("date", "template", "workspace_uid")
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_INVALID_QUERY_MESSAGE = "daily preview query is invalid"
_UNAVAILABLE_MESSAGE = "daily report preview is unavailable"
_MISMATCH_MESSAGE = "workspace_uid does not match the owning Store"
_SYNC_MESSAGE = (
    "authoritative store changed outside Work Stack; review synchronization status"
)


class DailyPreviewHttpError(Exception):
    """Stable HTTP refusal for the daily preview GET."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def daily_preview_payload(stack: Any, query: str) -> dict[str, Any]:
    """Return the success ``data`` object for one daily-preview GET."""

    parsed = parse_daily_preview_query(query)
    generated_at = _utc_now()
    owner_uid = _require_admitted_owner(stack.store, parsed["workspace_uid"])
    _require_in_sync(stack.store)
    _after_precheck_race_hook()
    try:
        projection, captures = _held_preview_snapshot(
            stack, parsed["date"], parsed["workspace_uid"]
        )
    except StoreCorruptError:
        _require_in_sync(stack.store)
        raise
    try:
        preview = preview_daily_report(
            projection=projection,
            date=parsed["date"],
            template=parsed["template"],
            generated_at=generated_at,
        )
    except DailyReportPreviewError:
        raise DailyPreviewHttpError(
            "report_preview_unavailable",
            _UNAVAILABLE_MESSAGE,
            422,
        ) from None
    return {
        "workspace_uid": owner_uid,
        "source_digest": day_source_digest(date=parsed["date"], day=projection["day"]),
        "preview": preview,
        "context_catalog": build_context_catalog(
            captures=captures,
            provenance_task_ids=preview["provenance"]["task_ids"],
            captured_at=preview["generated_at"],
        ),
    }


def _after_precheck_race_hook() -> None:
    """Patch point after owner/sync precheck and before consistent_read."""


def _after_projection_race_hook() -> None:
    """Patch point after the held day/capture read and before post-read sync."""


def _held_preview_snapshot(
    stack: Any, date: str, workspace_uid: str
) -> tuple[Any, list[Any]]:
    with stack.store.consistent_read() as snapshot:
        _require_in_sync(stack.store)
        if snapshot.workspace_uid != workspace_uid:
            raise DailyPreviewHttpError("workspace_mismatch", _MISMATCH_MESSAGE, 409)
        projection = stack.review_projection(date, 1)
        captures = stack.list_captures(status="all")
        _after_projection_race_hook()
        _require_in_sync(stack.store)
        return projection, captures


def parse_daily_preview_query(query: str) -> dict[str, str]:
    parsed = parse_qs(query, keep_blank_values=True)
    if set(parsed) != set(REQUIRED_QUERY_KEYS):
        raise _invalid_query()
    values: dict[str, str] = {}
    for key in REQUIRED_QUERY_KEYS:
        items = parsed[key]
        if len(items) != 1 or items[0] == "":
            raise _invalid_query()
        values[key] = items[0]
    if not _canonical_date(values["date"]):
        raise _invalid_query()
    if values["template"] != TEMPLATE_DAILY_V1:
        raise _invalid_query()
    if not _canonical_workspace_uid(values["workspace_uid"]):
        raise _invalid_query()
    return values


class DailyReportPreviewHttpMixin:
    """Thin GET adapter; host/session/origin rules stay on the existing handler."""

    def _get_daily_report_preview(self, parsed: Any, match: Any) -> None:
        try:
            payload = daily_preview_payload(self.stack, parsed.query)
        except DailyPreviewHttpError as error:
            self.send_api_error(error.code, error.message, error.status, error.details)
            return
        self.send_json({"data": payload})


def _invalid_query() -> DailyPreviewHttpError:
    return DailyPreviewHttpError("invalid_query", _INVALID_QUERY_MESSAGE, 400)


def _require_admitted_owner(store: Any, workspace_uid: str) -> str:
    readiness = store.readiness
    if readiness is None:
        raise DailyPreviewHttpError(
            "report_preview_unavailable",
            _UNAVAILABLE_MESSAGE,
            422,
        )
    if readiness.workspace_uid != workspace_uid:
        raise DailyPreviewHttpError("workspace_mismatch", _MISMATCH_MESSAGE, 409)
    return readiness.workspace_uid


def _require_in_sync(store: Any) -> None:
    status = store.sync_status()
    if status["state"] == "in-sync":
        return
    raise DailyPreviewHttpError(
        "store_sync_required",
        _SYNC_MESSAGE,
        409,
        {
            "state": status["state"],
            "generation": status["generation"],
            "changed_files": status["changed_files"],
        },
    )


def _canonical_date(value: str) -> bool:
    if _DATE.fullmatch(value) is None:
        return False
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _canonical_workspace_uid(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.int != 0 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
