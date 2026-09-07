"""Deterministic Weekly Markdown preview from a validated review projection.

A pure function. It does not read or write Store, server, or CLI state, does
not call an editor or scheduler, and does not ingest a previously generated
preview as source evidence. The caller already aggregated the week.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from typing import Any

from workstack.reporting import (
    MAX_ITEM_CHARS,
    MAX_MARKDOWN_CHARS,
    MAX_PROJECTION_BYTES,
    inert_text,
)

TEMPLATE_WEEKLY_V1 = "weekly-v1"
MAX_PROJECTS = 200
MAX_OBJECTIVES = 200
MAX_OBJECTIVES_PER_PROJECT = 200
MAX_ITEMS_PER_FIELD = 224
MAX_DATES_PER_PROJECT = 7
KNOWN_TOPLEVEL = frozenset({"day", "weekly"})
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_INSTANT = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)


class WeeklyReportPreviewError(ValueError):
    """Clear refusal for malformed, oversized, or unsupported preview inputs."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def preview_weekly_report(
    *,
    projection: Mapping[str, Any],
    end_date: str,
    template: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build one bounded, JSON-compatible weekly-v1 preview."""

    if template != TEMPLATE_WEEKLY_V1:
        raise WeeklyReportPreviewError(
            "template is unsupported",
            {"field": "template", "supported": [TEMPLATE_WEEKLY_V1]},
        )
    period_end = _require_date(end_date, "end_date")
    instant = _require_instant(generated_at)
    mapping = _preflight_projection(projection)
    _require_day_date(mapping, period_end)
    weekly = _require_weekly(mapping)
    span = _require_range(weekly, period_end)
    facts = _project_facts(weekly, span)
    coverage = _coverage(span, facts["record_dates"])
    markdown = _render_markdown(span, instant, coverage, facts)
    if len(markdown) > MAX_MARKDOWN_CHARS:
        raise WeeklyReportPreviewError(
            "preview markdown is oversized",
            {"field": "markdown", "limit": MAX_MARKDOWN_CHARS},
        )
    return {
        "template": TEMPLATE_WEEKLY_V1,
        "period": {"kind": "week", "start": span["start"], "end": span["end"], "days": 7},
        "generated_at": instant,
        "absence": None if facts["projects"] else "no records",
        "provenance": {
            "range": {"start": span["start"], "end": span["end"], "days": 7},
            "task_ids": facts["task_ids"],
            "sources": facts["sources"],
            "coverage": {
                "record_dates": coverage["record_dates"],
                "no_record_dates": coverage["no_record_dates"],
            },
            "ignored_keys": _ignored_keys(mapping),
        },
        "markdown": markdown,
    }


def _preflight_projection(projection: Any) -> Mapping[str, Any]:
    if not isinstance(projection, Mapping):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "projection"})
    try:
        encoded = json.dumps(projection, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise WeeklyReportPreviewError(
            "projection is malformed",
            {"field": "projection"},
        ) from error
    if len(encoded) > MAX_PROJECTION_BYTES:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": "projection", "limit": MAX_PROJECTION_BYTES},
        )
    return projection


def _require_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise WeeklyReportPreviewError("date must use YYYY-MM-DD", {"field": field})
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise WeeklyReportPreviewError("date is invalid", {"field": field}) from error
    if parsed.isoformat() != value:
        raise WeeklyReportPreviewError("date must use YYYY-MM-DD", {"field": field})
    return value


def _require_instant(value: Any) -> str:
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        raise WeeklyReportPreviewError(
            "generated_at must be an RFC 3339 timestamp",
            {"field": "generated_at"},
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt.datetime.fromisoformat(candidate)
    except ValueError as error:
        raise WeeklyReportPreviewError(
            "generated_at must be an RFC 3339 timestamp",
            {"field": "generated_at"},
        ) from error
    return value


def _require_day_date(projection: Mapping[str, Any], end_date: str) -> None:
    day = projection.get("day")
    if not isinstance(day, Mapping):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "day"})
    actual = day.get("date")
    if actual != end_date:
        raise WeeklyReportPreviewError(
            "projection date does not match the requested date",
            {"field": "date", "expected": end_date, "actual": actual},
        )


def _require_weekly(projection: Mapping[str, Any]) -> Mapping[str, Any]:
    weekly = projection.get("weekly")
    if not isinstance(weekly, Mapping):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "weekly"})
    return weekly


def _require_range(weekly: Mapping[str, Any], end_date: str) -> dict[str, str]:
    span = weekly.get("range")
    if not isinstance(span, Mapping):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "range"})
    start = _require_date(span.get("start"), "range")
    end = _require_date(span.get("end"), "range")
    days = span.get("days")
    if type(days) is not int or days != 7:
        raise WeeklyReportPreviewError(
            "weekly range must cover 7 days",
            {"field": "range", "expected": 7, "actual": days},
        )
    if end != end_date:
        raise WeeklyReportPreviewError(
            "projection date does not match the requested date",
            {"field": "date", "expected": end_date, "actual": end},
        )
    expected_start = _shift_civil_date(end, -6)
    if start != expected_start:
        raise WeeklyReportPreviewError(
            "weekly range start must be the inclusive end minus 6 days",
            {"field": "range", "expected": expected_start, "actual": start},
        )
    return {"start": start, "end": end}


def _ignored_keys(projection: Mapping[str, Any]) -> list[str]:
    return sorted(str(key) for key in projection if key not in KNOWN_TOPLEVEL)


def _shift_civil_date(value: str, days: int) -> str:
    origin = dt.date.fromisoformat(value)
    try:
        shifted = origin + dt.timedelta(days=days)
    except OverflowError as error:
        raise WeeklyReportPreviewError(
            "weekly range start is unrepresentable",
            {"field": "range"},
        ) from error
    return shifted.isoformat()


def _week_days(start: str) -> list[str]:
    return [_shift_civil_date(start, offset) for offset in range(7)]


def _coverage(span: Mapping[str, str], record_dates: list[str]) -> dict[str, list[str]]:
    record_set = set(record_dates)
    all_days = _week_days(span["start"])
    return {
        "all_days": all_days,
        "record_dates": [day for day in all_days if day in record_set],
        "no_record_dates": [day for day in all_days if day not in record_set],
    }


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise WeeklyReportPreviewError("projection is malformed", {"field": field})
    if len(value) > MAX_ITEM_CHARS:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": field, "limit": MAX_ITEM_CHARS},
        )
    return value


def _require_duration(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise WeeklyReportPreviewError(
            "duration_seconds must be a nonnegative integer",
            {"field": "duration_seconds"},
        )
    return value


def _dedupe_items(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise WeeklyReportPreviewError("projection is malformed", {"field": field})
    if len(value) > MAX_ITEMS_PER_FIELD:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": field, "limit": MAX_ITEMS_PER_FIELD},
        )
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _require_text(item, "item")
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _require_dates(value: Any, span: Mapping[str, str]) -> list[str]:
    if not isinstance(value, list):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "dates"})
    if len(value) > MAX_DATES_PER_PROJECT:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": "dates", "limit": MAX_DATES_PER_PROJECT},
        )
    start = dt.date.fromisoformat(span["start"])
    end = dt.date.fromisoformat(span["end"])
    seen: set[str] = set()
    dates: list[str] = []
    for item in value:
        date = _require_date(item, "dates")
        if date in seen:
            raise WeeklyReportPreviewError("dates must be distinct", {"field": "dates"})
        parsed = dt.date.fromisoformat(date)
        if not start <= parsed <= end:
            raise WeeklyReportPreviewError(
                "date is out of range",
                {"field": "dates", "actual": date},
            )
        seen.add(date)
        dates.append(date)
    if not dates:
        raise WeeklyReportPreviewError(
            "project dates must include at least one in-range date",
            {"field": "dates"},
        )
    return sorted(dates)


def _sorted_unique_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise WeeklyReportPreviewError("projection is malformed", {"field": field})
    if len(value) > MAX_OBJECTIVES_PER_PROJECT:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": field, "limit": MAX_OBJECTIVES_PER_PROJECT},
        )
    seen: set[str] = set()
    items: list[str] = []
    for item in value:
        text = _require_text(item, field)
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
    return sorted(items)


def _require_key(raw: Mapping[str, Any], field: str) -> Any:
    if field not in raw:
        raise WeeklyReportPreviewError("projection is malformed", {"field": field})
    return raw[field]


def _objective_titles(weekly: Mapping[str, Any]) -> dict[str, str]:
    raw = _require_key(weekly, "objectives")
    if not isinstance(raw, list):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "objectives"})
    if len(raw) > MAX_OBJECTIVES:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": "objectives", "limit": MAX_OBJECTIVES},
        )
    titles: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise WeeklyReportPreviewError("projection is malformed", {"field": "objectives"})
        objective_id = _require_text(item.get("id"), "objectives")
        if objective_id in titles:
            continue
        titles[objective_id] = _require_text(item.get("objective"), "objectives")
    return titles


def _one_project(
    raw: Any,
    span: Mapping[str, str],
    seen: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "projects"})
    task_id = _require_text(raw.get("task_id"), "task_id")
    if task_id in seen:
        raise WeeklyReportPreviewError(
            "duplicate project task_id",
            {"field": "task_id", "actual": task_id},
        )
    seen.add(task_id)
    title = raw.get("task")
    return {
        "task_id": task_id,
        "task": None if title is None else _require_text(title, "task"),
        "objective_ids": _sorted_unique_ids(_require_key(raw, "objective_ids"), "objective_ids"),
        "done": _dedupe_items(_require_key(raw, "done"), "done"),
        "next": _dedupe_items(_require_key(raw, "next"), "next"),
        "blockers": _dedupe_items(_require_key(raw, "blockers"), "blockers"),
        "dates": _require_dates(_require_key(raw, "dates"), span),
        "duration_seconds": _require_duration(_require_key(raw, "duration_seconds")),
    }


def _project_facts(weekly: Mapping[str, Any], span: Mapping[str, str]) -> dict[str, Any]:
    raw = weekly.get("projects")
    if not isinstance(raw, list):
        raise WeeklyReportPreviewError("projection is malformed", {"field": "projects"})
    if len(raw) > MAX_PROJECTS:
        raise WeeklyReportPreviewError(
            "projection is oversized",
            {"field": "projects", "limit": MAX_PROJECTS},
        )
    seen: set[str] = set()
    projects = [_one_project(item, span, seen) for item in raw]
    projects.sort(key=lambda project: project["task_id"])
    record_dates = sorted({
        date for project in projects for date in project["dates"]
    })
    return {
        "projects": projects,
        "task_ids": [project["task_id"] for project in projects],
        "sources": [
            {
                "kind": "review.weekly.project",
                "task_id": project["task_id"],
                "dates": list(project["dates"]),
            }
            for project in projects
        ],
        "record_dates": record_dates,
        "titles": _objective_titles(weekly),
    }


def _project_heading(task_id: str, task: str | None) -> str:
    identity = inert_text(task_id)
    if task is None:
        return "{} — title omitted".format(identity)
    return "{} — {}".format(identity, inert_text(task))


def _field_lines(label: str, items: list[str]) -> list[str]:
    if not items:
        return []
    lines = [label]
    for item in items:
        lines.append("- {}".format(inert_text(item)))
    lines.append("")
    return lines


def _objective_line(objective_ids: list[str], titles: Mapping[str, str]) -> str | None:
    if not objective_ids:
        return None
    rendered: list[str] = []
    for objective_id in objective_ids:
        text = titles.get(objective_id)
        if text:
            rendered.append("{} — {}".format(inert_text(objective_id), inert_text(text)))
        else:
            rendered.append(inert_text(objective_id))
    return "Objectives: {}".format("; ".join(rendered))


def _project_lines(project: Mapping[str, Any], titles: Mapping[str, str]) -> list[str]:
    lines = ["### {}".format(_project_heading(project["task_id"], project["task"])), ""]
    objectives = _objective_line(project["objective_ids"], titles)
    if objectives is not None:
        lines.append(objectives)
        lines.append("")
    lines.append("Duration: {}s".format(project["duration_seconds"]))
    lines.append("")
    for field, label in (("done", "Done"), ("next", "Next"), ("blockers", "Blockers")):
        lines.extend(_field_lines(label, project[field]))
    return lines


def _render_markdown(
    span: Mapping[str, str],
    generated_at: str,
    coverage: Mapping[str, list[str]],
    facts: Mapping[str, Any],
) -> str:
    start, end = span["start"], span["end"]
    record_set = set(coverage["record_dates"])
    lines = [
        "# Weekly review {} → {}".format(start, end),
        "",
        "Period: {} → {}".format(start, end),
        "Generated at: {}".format(inert_text(generated_at)),
        "",
        "## Coverage",
        "",
    ]
    for date in coverage["all_days"]:
        status = "Records." if date in record_set else "No records."
        lines.append("- {}: {}".format(date, status))
    lines.append("")
    projects = facts["projects"]
    if not projects:
        return "\n".join(lines)
    lines.append("## Projects")
    lines.append("")
    for project in projects:
        lines.extend(_project_lines(project, facts["titles"]))
        lines.append("")
    return "\n".join(lines)
