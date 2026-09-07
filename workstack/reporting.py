"""Deterministic Daily Markdown preview from a validated review projection.

O2 core: a pure function. It does not read or write Store, server, or CLI
state, does not call an editor or scheduler, and does not ingest a previously
generated preview as source evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from typing import Any


TEMPLATE_DAILY_V1 = "daily-v1"
MAX_PROJECTION_BYTES = 262144
MAX_MARKDOWN_CHARS = 100000
MAX_DAY_ENTRIES = 200
MAX_ITEMS_PER_LIST = 32
MAX_ITEM_CHARS = 1000
KNOWN_ENTRY_FIELDS = frozenset({
    "task_id", "task", "done", "next", "blockers", "session_id", "duration_seconds",
})
PREVIEW_KEYS = frozenset({
    "template", "period", "generated_at", "absence", "provenance", "markdown",
})
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_INSTANT = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)
_HTML = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# CommonMark ASCII punctuation. &<> are entities; the rest are backslash-escaped.
_PUNCT = frozenset("!\"#$%'()*+,-./:;=?@[\\]^_`{|}~")


class DailyReportPreviewError(ValueError):
    """Clear refusal for malformed, oversized, or unsupported preview inputs."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def preview_daily_report(
    *,
    projection: Mapping[str, Any],
    date: str,
    template: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build one bounded, JSON-compatible daily-v1 preview.

    ``period`` is the requested civil day and is never taken from
    ``generated_at``. Active review membership is whatever the caller already
    projected: this function does not restore superseded rows.
    """

    if template != TEMPLATE_DAILY_V1:
        raise DailyReportPreviewError(
            "template is unsupported",
            {"field": "template", "supported": [TEMPLATE_DAILY_V1]},
        )
    period_date = _require_date(date)
    instant = _require_instant(generated_at)
    day = _require_day(projection, period_date)
    facts = _day_facts(day, period_date)
    markdown = _render_markdown(period_date, instant, facts)
    if len(markdown) > MAX_MARKDOWN_CHARS:
        raise DailyReportPreviewError(
            "preview markdown is oversized",
            {"field": "markdown", "limit": MAX_MARKDOWN_CHARS},
        )
    return {
        "template": TEMPLATE_DAILY_V1,
        "period": {"kind": "day", "date": period_date},
        "generated_at": instant,
        "absence": None if facts["entries"] else "no records",
        "provenance": {
            "date": period_date,
            "task_ids": facts["task_ids"],
            "sources": facts["sources"],
            "weekly_range": _weekly_range(projection),
            "ignored_keys": _ignored_preview_keys(projection),
        },
        "markdown": markdown,
    }


def inert_text(value: str) -> str:
    """Render a user fact as literal CommonMark/GFM text.

    ``<>&`` become entities so tags cannot appear in the Markdown source.
    Every remaining ASCII punctuation character is backslash-escaped so
    headings, lists, emphasis, strike, tables, links, autolinks, and email
    autolinks stay visible characters.
    """

    parts: list[str] = []
    for char in value.replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            parts.append(" ")
        elif char in _HTML:
            parts.append(_HTML[char])
        elif char in _PUNCT:
            parts.append("\\" + char)
        else:
            parts.append(char)
    return "".join(parts)


def _require_date(value: Any) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise DailyReportPreviewError("date must use YYYY-MM-DD", {"field": "date"})
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise DailyReportPreviewError("date is invalid", {"field": "date"}) from error
    if parsed.isoformat() != value:
        raise DailyReportPreviewError("date must use YYYY-MM-DD", {"field": "date"})
    return value


def _require_instant(value: Any) -> str:
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        raise DailyReportPreviewError(
            "generated_at must be an RFC 3339 timestamp",
            {"field": "generated_at"},
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt.datetime.fromisoformat(candidate)
    except ValueError as error:
        raise DailyReportPreviewError(
            "generated_at must be an RFC 3339 timestamp",
            {"field": "generated_at"},
        ) from error
    return value


def _require_day(projection: Any, date: str) -> Mapping[str, Any]:
    if not isinstance(projection, Mapping):
        raise DailyReportPreviewError("projection is malformed", {"field": "projection"})
    try:
        encoded = json.dumps(projection, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise DailyReportPreviewError("projection is malformed", {"field": "projection"}) from error
    if len(encoded) > MAX_PROJECTION_BYTES:
        raise DailyReportPreviewError(
            "projection is oversized",
            {"field": "projection", "limit": MAX_PROJECTION_BYTES},
        )
    day = projection.get("day")
    if not isinstance(day, Mapping):
        raise DailyReportPreviewError("projection is malformed", {"field": "day"})
    if day.get("date") != date:
        raise DailyReportPreviewError(
            "projection date does not match the requested date",
            {"field": "date", "expected": date, "actual": day.get("date")},
        )
    return day


def _ignored_preview_keys(projection: Mapping[str, Any]) -> list[str]:
    return sorted(key for key in projection if key in PREVIEW_KEYS)


def _weekly_range(projection: Mapping[str, Any]) -> dict[str, Any] | None:
    weekly = projection.get("weekly")
    if not isinstance(weekly, Mapping):
        return None
    span = weekly.get("range")
    if not isinstance(span, Mapping):
        return None
    start, end, days = span.get("start"), span.get("end"), span.get("days")
    if not isinstance(start, str) or not isinstance(end, str) or type(days) is not int:
        return None
    return {"start": start, "end": end, "days": days}


def _day_facts(day: Mapping[str, Any], date: str) -> dict[str, Any]:
    raw_entries = day.get("entries")
    if not isinstance(raw_entries, list):
        raise DailyReportPreviewError("projection is malformed", {"field": "entries"})
    if len(raw_entries) > MAX_DAY_ENTRIES:
        raise DailyReportPreviewError(
            "projection is oversized",
            {"field": "entries", "limit": MAX_DAY_ENTRIES},
        )
    entries: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    task_ids: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        entry = _one_entry(raw, date, index)
        entries.append(entry)
        sources.append(entry["source"])
        task_id = entry["source"]["task_id"]
        if isinstance(task_id, str) and task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    return {
        "check_in": _check_in(day),
        "entries": entries,
        "sources": sources,
        "task_ids": task_ids,
    }


def _check_in(day: Mapping[str, Any]) -> dict[str, Any]:
    if "start_time" not in day:
        return {"status": "omitted", "value": None}
    value = day.get("start_time")
    if value is None:
        return {"status": "not recorded", "value": None}
    if not isinstance(value, str):
        return {"status": "unusable", "value": None}
    return {"status": "recorded", "value": value}


def _one_entry(raw: Any, date: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {
            "status": "unusable",
            "task_id": None,
            "task": None,
            "done": ("unusable", []),
            "next": ("unusable", []),
            "blockers": ("unusable", []),
            "session_id": None,
            "duration_seconds": None,
            "source": {
                "kind": "review.day.entry",
                "date": date,
                "index": index,
                "task_id": None,
                "unknown_fields": [],
            },
        }
    task_id = raw.get("task_id") if isinstance(raw.get("task_id"), str) else None
    task = raw.get("task") if isinstance(raw.get("task"), str) else None
    session_id = raw.get("session_id") if isinstance(raw.get("session_id"), str) else None
    duration = raw.get("duration_seconds")
    duration_seconds = duration if type(duration) is int else None
    return {
        "status": "recorded",
        "task_id": task_id,
        "task": task,
        "done": _item_list(raw.get("done"), "done" in raw),
        "next": _item_list(raw.get("next"), "next" in raw),
        "blockers": _item_list(raw.get("blockers"), "blockers" in raw),
        "session_id": session_id,
        "duration_seconds": duration_seconds,
        "source": {
            "kind": "review.day.entry",
            "date": date,
            "index": index,
            "task_id": task_id,
            "unknown_fields": sorted(str(key) for key in raw if key not in KNOWN_ENTRY_FIELDS),
        },
    }


def _item_list(value: Any, present: bool) -> tuple[str, list[str]]:
    if not present:
        return "omitted", []
    if not isinstance(value, list):
        return "unusable", []
    if len(value) > MAX_ITEMS_PER_LIST:
        raise DailyReportPreviewError(
            "projection is oversized",
            {"field": "items", "limit": MAX_ITEMS_PER_LIST},
        )
    status = "recorded"
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            status = "partial"
            continue
        if len(item) > MAX_ITEM_CHARS:
            raise DailyReportPreviewError(
                "projection is oversized",
                {"field": "item", "limit": MAX_ITEM_CHARS},
            )
        items.append(item)
    return status, items


def _render_markdown(date: str, generated_at: str, facts: Mapping[str, Any]) -> str:
    lines = [
        "# Daily review {}".format(date),
        "",
        "Period: {}".format(date),
        "Generated at: {}".format(inert_text(generated_at)),
        "",
        "## Check-in",
        "",
        _check_in_line(facts["check_in"]),
        "",
        "## Day record",
        "",
    ]
    entries = facts["entries"]
    if not entries:
        lines.append("No records.")
        lines.append("")
        return "\n".join(lines)
    for entry in entries:
        lines.extend(_entry_lines(entry))
        lines.append("")
    return "\n".join(lines)


def _check_in_line(check_in: Mapping[str, Any]) -> str:
    status = check_in["status"]
    if status == "recorded":
        return inert_text(check_in["value"])
    if status == "omitted":
        return "Omitted."
    if status == "unusable":
        return "Unusable."
    return "Not recorded."


def _entry_lines(entry: Mapping[str, Any]) -> list[str]:
    if entry["status"] == "unusable":
        return ["### Entry omitted", "", "Unusable."]
    heading = _entry_heading(entry["task_id"], entry["task"])
    lines = ["### {}".format(heading), ""]
    extras: list[str] = []
    if entry["session_id"] is not None:
        extras.append("session {}".format(inert_text(entry["session_id"])))
    if entry["duration_seconds"] is not None:
        extras.append("{}s".format(entry["duration_seconds"]))
    if extras:
        lines.append(" · ".join(extras))
        lines.append("")
    body: list[str] = []
    for field, label in (("done", "Done"), ("next", "Next"), ("blockers", "Blockers")):
        body.extend(_field_lines(label, entry[field]))
    if not body:
        body.append("Omitted facts.")
        body.append("")
    lines.extend(body)
    return lines


def _entry_heading(task_id: str | None, task: str | None) -> str:
    identity = inert_text(task_id) if task_id is not None else "Task omitted"
    if task is None:
        return "{} — title omitted".format(identity)
    return "{} — {}".format(identity, inert_text(task))


def _field_lines(label: str, payload: tuple[str, list[str]]) -> list[str]:
    status, items = payload
    if status == "omitted":
        return ["{}: omitted.".format(label), ""]
    if status == "unusable":
        return ["{}: unusable.".format(label), ""]
    if status == "recorded" and not items:
        return []
    lines = ["{}".format(label)]
    if status == "partial":
        lines.append("Partial list; non-text items were not treated as facts.")
    for item in items:
        lines.append("- {}".format(inert_text(item)))
    lines.append("")
    return lines
