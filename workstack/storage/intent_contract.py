"""Backend-neutral contracts for the first normalized intent-mutation slice.

These helpers preserve the released v3 request canonicalization and response
shapes for standalone notes, daily check-ins, and review worklog entries.  They
do not read or write either storage format.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .canonical import canonical_sha256


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_DISPLAY_ID = re.compile(r"N-(\d+)", re.IGNORECASE)
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class IntentContractError(ValueError):
    """Content-free refusal to normalize or replay one intent."""

    command_boundary = "intent"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NormalizedIntent:
    path: str
    request_digest: str
    response_data: Mapping[str, Any]
    authority_value: Mapping[str, Any]


def validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise IntentContractError("IDEMPOTENCY_KEY_INVALID")
    return value


def normalize_note_intent(
    body: Mapping[str, Any],
    display_ids: Sequence[str],
    *,
    created_date: str,
    path: str = "/api/v1/notes",
) -> NormalizedIntent:
    text = str(body.get("text") or "").strip()
    if not text:
        raise IntentContractError("NOTE_TEXT_REQUIRED")
    links = body.get("links")
    if not isinstance(links, list):
        raise IntentContractError("NOTE_LINKS_INVALID")
    created = _date(created_date)
    note = {
        "id": _next_note_display_id(display_ids),
        "text": text,
        "links": sorted(
            {str(link).strip().upper() for link in links if str(link).strip()}
        ),
        "created": created,
    }
    return NormalizedIntent(
        path,
        canonical_sha256(dict(body)),
        copy.deepcopy(note),
        copy.deepcopy(note),
    )


def normalize_checkin_intent(
    body: Mapping[str, Any], *, path: str = "/api/v1/review/checkin"
) -> NormalizedIntent:
    work_date = _date(body.get("date"))
    start_time = body.get("time")
    if not isinstance(start_time, str) or _TIME.fullmatch(start_time) is None:
        raise IntentContractError("CHECKIN_TIME_INVALID")
    canonical = {"date": work_date, "time": start_time}
    response = {"date": work_date, "start_time": start_time}
    return NormalizedIntent(
        path, canonical_sha256(canonical), response, canonical
    )


def normalize_worklog_intent(
    body: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    path: str = "/api/v1/review/entries",
) -> NormalizedIntent:
    display_id = task.get("id")
    title = task.get("title")
    if not isinstance(display_id, str) or not isinstance(title, str) or not title:
        raise IntentContractError("TASK_IDENTITY_INVALID")
    requested = body.get("task_id")
    if not isinstance(requested, str) or requested.strip().upper() != display_id:
        raise IntentContractError("TASK_IDENTITY_MISMATCH")
    canonical = {
        "date": _date(body.get("date")),
        "task_id": display_id,
        "done": _items(body.get("done"), "WORKLOG_DONE_INVALID"),
        "next": _items(body.get("next"), "WORKLOG_NEXT_INVALID"),
        "blockers": _items(body.get("blockers"), "WORKLOG_BLOCKERS_INVALID"),
    }
    if not any(canonical[field] for field in ("done", "next", "blockers")):
        raise IntentContractError("WORKLOG_ITEMS_REQUIRED")
    entry = {
        "task_id": display_id,
        "task": title,
        "done": canonical["done"],
        "next": canonical["next"],
        "blockers": canonical["blockers"],
    }
    response = {"date": canonical["date"], **copy.deepcopy(entry)}
    authority = {"date": canonical["date"], **entry}
    return NormalizedIntent(
        path, canonical_sha256(canonical), response, authority
    )


def replay_response(
    records: Sequence[Mapping[str, Any]],
    key: str,
    *,
    method: str,
    path: str,
    request_digest: str,
) -> dict[str, Any] | None:
    validate_idempotency_key(key)
    for record in records:
        if record.get("key") != key:
            continue
        coordinate = (record.get("method"), record.get("path"), record.get("request_digest"))
        if coordinate != (method, path, request_digest):
            raise IntentContractError("IDEMPOTENCY_KEY_CONFLICT")
        body = copy.deepcopy(record.get("response_body"))
        if not isinstance(body, dict) or not isinstance(body.get("data"), (dict, list)):
            raise IntentContractError("IDEMPOTENCY_RESPONSE_INVALID")
        metadata = body.setdefault("meta", {})
        if not isinstance(metadata, dict):
            raise IntentContractError("IDEMPOTENCY_RESPONSE_INVALID")
        metadata["replayed"] = True
        return {"status": 200, "body": body}
    return None


def success_response(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": 201,
        "body": {"data": copy.deepcopy(dict(data)), "meta": {"replayed": False}},
    }


def _date(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise IntentContractError("DATE_INVALID")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise IntentContractError("DATE_INVALID") from error
    if parsed.isoformat() != value:
        raise IntentContractError("DATE_INVALID")
    return value


def _items(value: object, code: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise IntentContractError(code)
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > 1000:
            raise IntentContractError(code)
        if item.strip():
            output.append(item.strip())
    return output


def _next_note_display_id(values: Sequence[str]) -> str:
    largest = 0
    for value in values:
        match = _DISPLAY_ID.fullmatch(str(value))
        if match:
            largest = max(largest, int(match.group(1)))
    return f"N-{largest + 1:04d}"
