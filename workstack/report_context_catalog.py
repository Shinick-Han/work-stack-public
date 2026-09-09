"""Pure related-context catalogue beside a daily report preview.

The catalogue is a bounded snapshot of Captures whose explicit Task links
intersect the already-validated preview provenance. It does not read Store,
does not write, and does not change Markdown, provenance, or the day digest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

CONTEXT_CATALOG_ITEM_LIMIT = 32
_NATURAL_ID = re.compile(r"\A([A-Za-z]+)-(\d+)\Z")


def natural_id_sort_key(value: str) -> tuple[int, str, int, str]:
    """Deterministic natural order for admitted Capture/Task display IDs."""

    match = _NATURAL_ID.fullmatch(value)
    if match is None:
        return (1, value, 0, value)
    return (0, match.group(1).upper(), int(match.group(2)), value)


def build_context_catalog(
    *,
    captures: Iterable[Mapping[str, Any]],
    provenance_task_ids: Sequence[Any],
    captured_at: str,
) -> dict[str, Any]:
    """Return the closed context_catalog object for one successful preview."""

    wanted = {item for item in provenance_task_ids if isinstance(item, str)}
    qualifying: list[dict[str, Any]] = []
    for capture in captures:
        item = _catalog_item(capture, wanted)
        if item is not None:
            qualifying.append(item)
    qualifying.sort(key=lambda item: natural_id_sort_key(item["capture_id"]))
    return {
        "captured_at": captured_at,
        "items": qualifying[:CONTEXT_CATALOG_ITEM_LIMIT],
        "omitted_count": max(0, len(qualifying) - CONTEXT_CATALOG_ITEM_LIMIT),
    }


def _catalog_item(
    capture: Mapping[str, Any], wanted: set[str]
) -> dict[str, Any] | None:
    if not isinstance(capture, Mapping):
        return None
    capture_id = capture.get("id")
    revision = capture.get("revision")
    status = capture.get("status")
    title = _display_title(capture)
    if not isinstance(capture_id, str) or not capture_id:
        return None
    if type(revision) is not int:
        return None
    if not isinstance(status, str) or not status:
        return None
    if title is None:
        return None
    linked = _intersection(capture.get("linked_task_ids"), wanted)
    if not linked:
        return None
    return {
        "capture_id": capture_id,
        "capture_revision": revision,
        "title": title,
        "linked_task_ids": linked,
        "status": status,
    }


def _display_title(capture: Mapping[str, Any]) -> str | None:
    source = capture.get("source")
    if not isinstance(source, Mapping):
        return None
    title = source.get("display_title")
    if not isinstance(title, str):
        return None
    return title


def _intersection(linked: Any, wanted: set[str]) -> list[str]:
    if not isinstance(linked, (list, tuple)):
        return []
    unique = {
        item for item in linked if isinstance(item, str) and item in wanted
    }
    return sorted(unique, key=natural_id_sort_key)
