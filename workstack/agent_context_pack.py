"""Opt-in `planning-v1` projection for `agent context`.

This module is pure. It performs no I/O, opens no store, reads no clock and
issues no request: a caller hands it material it has already read under its own
transaction or its own pair of owner GETs, and gets back the bounded planning
blocks or a ValueError.

Two properties are structural rather than reviewed:

* Every rendered field is COPIED BY NAME from an allowlist. An unknown field on
  an Objective, a Task or a Capture is never read, so it cannot be rendered even
  when the record carries one. Raw and normalized bodies, locators, URLs,
  recipients, provenance, actions, attachments, notes and work sessions have no
  name here at all.
* Nothing is invented. A relationship or Objective whose referenced record is
  absent from the projection is dropped, never rendered with a placeholder
  title, because a fabricated relationship is worse than a missing one.

`core-v1` stays the default everywhere and never reaches this module.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

__all__ = (
    "CORE_VIEW",
    "DISPLAY_TITLE_MAX",
    "LINK_REASONS",
    "OBJECTIVES_CAP",
    "PLANNING_BLOCKS",
    "PLANNING_DATA_FIELDS",
    "PLANNING_OMITTED_CATEGORIES",
    "PLANNING_VIEW",
    "RELATIONSHIP_KINDS",
    "RELATIONSHIPS_CAP",
    "RESOURCE_TYPE_MAX",
    "SOURCE_PROVIDERS",
    "SOURCES_CAP",
    "VIEWS",
    "apply_planning_contract_fixture",
    "build_planning_blocks",
    "overflow_marker",
    "planning_omitted",
    "shrink_planning_data",
    "validate_planning_data",
)


CORE_VIEW = "core-v1"
PLANNING_VIEW = "planning-v1"
VIEWS = (CORE_VIEW, PLANNING_VIEW)

OBJECTIVES_CAP = 5
RELATIONSHIPS_CAP = 10
SOURCES_CAP = 5
DISPLAY_TITLE_MAX = 500
RESOURCE_TYPE_MAX = 1024
LINK_REASONS_MAX = 2

# Presentation order is also the trim order below: the block furthest from the
# selected Task's own identity is the first to give up room.
PLANNING_BLOCKS = ("objectives", "relationships", "sources")

RELATIONSHIP_KINDS = ("parent", "dependency", "child", "dependent")
OBJECTIVE_STATUSES = ("active", "done", "dropped")
RELATIONSHIP_STATUSES = ("open", "started", "done", "dropped")
SOURCE_STATUSES = ("inbox", "linked", "converted", "dismissed")
SOURCE_PROVIDERS = (
    "manual",
    "microsoft-outlook",
    "microsoft-sharepoint",
    "microsoft-teams",
)
LINK_REASONS = ("capture-conversion", "capture-link")

OBJECTIVE_ID_RE = re.compile(r"^O-[1-9][0-9]*$")
TASK_ID_RE = re.compile(r"^T-[0-9]{4,}$")
CAPTURE_ID_RE = re.compile(r"^C-[0-9]{4,}$")

# What planning-v1 still refuses to carry. Named positively so a reader can see
# the boundary without diffing two allowlists.
PLANNING_OMITTED_CATEGORIES = (
    "actions",
    "attachments",
    "capture_bodies",
    "capture_locators",
    "notes",
    "provenance",
    "work_sessions",
)

_OVERFLOW_SUFFIX = "_overflow"

PLANNING_VIEW_CHOICES = {"default": CORE_VIEW, "values": [CORE_VIEW, PLANNING_VIEW]}
PLANNING_BACKEND = {
    "held_local_view": (
        "Store transaction already held; Task, Objectives and relationships "
        "from one consistent view"
    ),
    "keys": ["context", "objectives", "tasks"],
    "related_objects": (
        "non-atomic; Objectives, related Tasks and Captures are read at "
        "their own moments"
    ),
    "running_sequence": "detail -> workspace -> 31 reviews -> final detail",
    "selected_task_identity": ["id", "uid", "revision"],
}
PLANNING_ENVELOPE = {
    "additive": True,
    "core_keys_unchanged": ["omitted", "recent_worklog", "task", "workspace_uid"],
    "display_title_max_characters": DISPLAY_TITLE_MAX,
    "id_patterns": {
        "objectives": "O-[1-9][0-9]*",
        "relationships": "T-[0-9]{4,}",
        "sources": "C-[0-9]{4,}",
    },
    "keys": ["objectives", "relationships", "sources"],
    "link_reasons": list(LINK_REASONS),
    "link_reasons_max": LINK_REASONS_MAX,
    "nested_fields": {
        "objectives": ["id", "quarter", "status", "title"],
        "relationships": ["id", "kind", "status", "title"],
        "sources": [
            "display_title",
            "id",
            "link_reasons",
            "provider",
            "resource_type",
            "status",
        ],
    },
    "nullable": {"quarter": True},
    "omitted_categories": list(PLANNING_OMITTED_CATEGORIES),
    "overflow_caps": {
        "objectives": OBJECTIVES_CAP,
        "relationships": RELATIONSHIPS_CAP,
        "sources": SOURCES_CAP,
    },
    "overflow_markers": [
        "objectives_overflow", "relationships_overflow", "sources_overflow",
        "recent_worklog_overflow",
    ],
    "ordering": {
        "objectives": "id ascending",
        "sources": "id ascending",
        "relationships": "relationship_kinds order then id ascending",
    },
    "providers": list(SOURCE_PROVIDERS),
    "relationship_kinds": list(RELATIONSHIP_KINDS),
    "relationship_uniqueness": ["kind", "id"],
    "resource_type": {"max_characters": RESOURCE_TYPE_MAX, "required": True},
    "statuses": {
        "objectives": list(OBJECTIVE_STATUSES),
        "relationships": list(RELATIONSHIP_STATUSES),
        "sources": list(SOURCE_STATUSES),
    },
    "title_bound": "envelope 32KiB plus whole-item shrink; no per-field 500",
}
PLANNING_LIMITS = {
    "context_view_default": CORE_VIEW,
    "context_views": [CORE_VIEW, PLANNING_VIEW],
    "planning_display_title_max_characters": DISPLAY_TITLE_MAX,
    "planning_objectives_cap": OBJECTIVES_CAP,
    "planning_relationships_cap": RELATIONSHIPS_CAP,
    "planning_resource_type_max_characters": RESOURCE_TYPE_MAX,
    "planning_sources_cap": SOURCES_CAP,
}
PLANNING_TRANSPORT = {
    "planning_local_view": (
        "held transaction; Task, Objectives and relationships from one "
        "consistent view"
    ),
    "planning_running_reads": {
        "related_objects_non_atomic": True,
        "selected_task_identity_fields": ["id", "uid", "revision"],
        "sequence": [
            "task-detail",
            "workspace",
            "daily-review-x31",
            "task-detail-reconfirm",
        ],
    },
}


def apply_planning_contract_fixture(sections: dict[str, Any]) -> dict[str, Any]:
    """Overlay additive planning-v1 onto the frozen core fixture sections."""

    projected = copy.deepcopy(sections)
    context = projected["backend_results"]["context"]
    context["planning"] = copy.deepcopy(PLANNING_BACKEND)
    context["view"] = copy.deepcopy(PLANNING_VIEW_CHOICES)
    projected["commands"]["context_view"] = copy.deepcopy(PLANNING_VIEW_CHOICES)
    projected["envelope"]["data_shapes"]["agent.context"]["planning_v1"] = copy.deepcopy(
        PLANNING_ENVELOPE
    )
    projected["limits"].update(PLANNING_LIMITS)
    projected["transport_rules"].update(copy.deepcopy(PLANNING_TRANSPORT))
    return projected


def is_planning_view(view: object) -> bool:
    """True only for the exact opt-in view token."""

    return view == PLANNING_VIEW


def is_known_view(view: object) -> bool:
    """False for anything but the two exact view tokens."""

    return view in VIEWS


def overflow_marker(block: str) -> str:
    """The omission marker naming one capped planning block."""

    if block not in PLANNING_BLOCKS:
        raise ValueError("unknown planning block")
    return block + _OVERFLOW_SUFFIX


def planning_omitted(*, overflowed: tuple[str, ...]) -> list[str]:
    """The planning omission list: fixed categories plus any capped block."""

    markers = [overflow_marker(block) for block in PLANNING_BLOCKS if block in overflowed]
    return list(PLANNING_OMITTED_CATEGORIES) + sorted(markers)


def _nonempty(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError("invalid planning {}".format(label))
    return value


def _bounded(value: object, label: str, maximum: int) -> str:
    text = _nonempty(value, label)
    if len(text) > maximum:
        raise ValueError("invalid planning {}".format(label))
    return text


def _pattern(value: object, compiled: re.Pattern[str], label: str) -> str:
    text = _nonempty(value, label)
    if compiled.fullmatch(text) is None:
        raise ValueError("invalid planning {}".format(label))
    return text


def _enum(value: object, allowed: tuple[str, ...], label: str) -> str:
    text = _nonempty(value, label)
    if text not in allowed:
        raise ValueError("invalid planning {}".format(label))
    return text


def _optional_quarter(value: object) -> str | None:
    if value is None:
        return None
    return _nonempty(value, "Objective quarter")


def _records(value: object, label: str) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("invalid planning {}".format(label))
    for item in value:
        if type(item) is not dict:
            raise ValueError("invalid planning {}".format(label))
    return value


def _identifiers(
    value: object, label: str, compiled: re.Pattern[str]
) -> list[str]:
    if value is None:
        return []
    if type(value) is not list:
        raise ValueError("invalid planning {}".format(label))
    return [_pattern(item, compiled, label) for item in value]


def _by_id(
    records: list[dict[str, Any]], label: str, compiled: re.Pattern[str]
) -> dict[str, dict[str, Any]]:
    """Index by id, refusing a duplicate rather than picking a winner.

    Two records sharing an id make the projection ambiguous, and choosing the
    first would render one record's title under another's identity.
    """

    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = _pattern(record.get("id"), compiled, label + " id")
        if identifier in indexed:
            raise ValueError("duplicate planning {} id".format(label))
        indexed[identifier] = record
    return indexed


def _validate_link_reasons(value: object) -> list[str]:
    if type(value) is not list or not value or len(value) > LINK_REASONS_MAX:
        raise ValueError("invalid planning source link reasons")
    reasons = [_enum(item, LINK_REASONS, "Capture link reason") for item in value]
    unique = sorted(set(reasons))
    if reasons != unique:
        raise ValueError("invalid planning source link reasons")
    return unique


def _project_objective(record: dict[str, Any]) -> dict[str, Any]:
    # `objective` is the stored title field; `quarter` may legitimately be absent.
    return {
        "id": _pattern(record.get("id"), OBJECTIVE_ID_RE, "Objective id"),
        "quarter": _optional_quarter(record.get("quarter")),
        "status": _enum(record.get("status"), OBJECTIVE_STATUSES, "Objective status"),
        "title": _nonempty(record.get("objective"), "Objective title"),
    }


def _project_relationship(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _pattern(record.get("id"), TASK_ID_RE, "relationship id"),
        "kind": kind,
        "status": _enum(
            record.get("status"), RELATIONSHIP_STATUSES, "relationship status"
        ),
        "title": _nonempty(record.get("title"), "relationship title"),
    }


def _project_source(record: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    source = record.get("source")
    if type(source) is not dict:
        raise ValueError("invalid planning Capture source")
    return {
        "display_title": _bounded(
            source.get("display_title"), "Capture display title", DISPLAY_TITLE_MAX
        ),
        "id": _pattern(record.get("id"), CAPTURE_ID_RE, "Capture id"),
        "link_reasons": reasons,
        "provider": _enum(
            source.get("provider"), SOURCE_PROVIDERS, "Capture provider"
        ),
        "resource_type": _bounded(
            source.get("resource_type"), "Capture resource type", RESOURCE_TYPE_MAX
        ),
        "status": _enum(record.get("status"), SOURCE_STATUSES, "Capture status"),
    }


def _objectives(task: dict[str, Any], objectives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = _by_id(objectives, "Objective", OBJECTIVE_ID_RE)
    resolved = [
        _project_objective(indexed[identifier])
        for identifier in _identifiers(
            task.get("objective_ids"), "Objective id", OBJECTIVE_ID_RE
        )
        if identifier in indexed
    ]
    return sorted(resolved, key=lambda item: item["id"])


def _relationship_pairs(
    task_id: str, task: dict[str, Any], tasks: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Every (kind, related id) this projection can actually resolve, unsorted."""

    pairs: list[tuple[str, str]] = []
    parent = task.get("parent_id")
    if parent is not None:
        pairs.append(("parent", _pattern(parent, TASK_ID_RE, "relationship id")))
    pairs.extend(
        ("dependency", identifier)
        for identifier in _identifiers(
            task.get("dependencies"), "relationship id", TASK_ID_RE
        )
    )
    for record in tasks:
        related = _pattern(record.get("id"), TASK_ID_RE, "relationship id")
        if related == task_id:
            continue
        if record.get("parent_id") == task_id:
            pairs.append(("child", related))
        if task_id in _identifiers(
            record.get("dependencies"), "relationship id", TASK_ID_RE
        ):
            pairs.append(("dependent", related))
    return pairs


def _relationships(
    task_id: str, task: dict[str, Any], tasks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    indexed = _by_id(tasks, "relationship", TASK_ID_RE)
    seen: set[tuple[str, str]] = set()
    resolved: list[dict[str, Any]] = []
    for kind, related in _relationship_pairs(task_id, task, tasks):
        # An unresolved reference is dropped rather than rendered with an
        # invented title; a duplicate edge is rendered once.
        if related not in indexed or (kind, related) in seen:
            continue
        seen.add((kind, related))
        resolved.append(_project_relationship(kind, indexed[related]))
    order = {kind: index for index, kind in enumerate(RELATIONSHIP_KINDS)}
    return sorted(resolved, key=lambda item: (order[item["kind"]], item["id"]))


def _link_reasons(item: dict[str, Any], task_id: str) -> list[str] | None:
    connections = item.get("connections")
    if type(connections) is not list:
        raise ValueError("invalid planning Capture connections")
    for connection in connections:
        if type(connection) is not dict:
            raise ValueError("invalid planning Capture connections")
        target = connection.get("target")
        if type(target) is not dict:
            raise ValueError("invalid planning Capture connections")
        if target.get("kind") != "task" or target.get("id") != task_id:
            continue
        reasons = connection.get("reasons")
        if type(reasons) is not list:
            raise ValueError("invalid planning Capture link reason")
        if not reasons:
            return None
        if any(type(item) is not str for item in reasons):
            raise ValueError("invalid planning Capture link reason")
        return _validate_link_reasons(sorted(set(reasons)))
    return None


def _sources(task_id: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for item in context:
        reference = item.get("ref")
        if type(reference) is not dict or reference.get("kind") != "capture":
            continue
        reasons = _link_reasons(item, task_id)
        if reasons is None:
            continue
        resolved.append(_project_source(item, reasons))
    return sorted(resolved, key=lambda item: item["id"])


def build_planning_blocks(
    *,
    task_id: str,
    objectives: object,
    tasks: object,
    context: object,
) -> tuple[dict[str, list[dict[str, Any]]], tuple[str, ...]]:
    """The three capped planning blocks plus the names of those that overflowed.

    The selected Task's own links are read from `tasks`, the workspace projection
    the caller already cross-checked, not from the core Task allowlist, which
    deliberately carries no parent, dependency or Objective field.
    """

    records = _records(tasks, "Tasks")
    selected = _by_id(records, "Task", TASK_ID_RE).get(task_id)
    if selected is None:
        raise ValueError("selected Task is absent from the planning projection")
    resolved = {
        "objectives": _objectives(selected, _records(objectives, "Objectives")),
        "relationships": _relationships(task_id, selected, records),
        "sources": _sources(task_id, _records(context, "Capture context")),
    }
    caps = {
        "objectives": OBJECTIVES_CAP,
        "relationships": RELATIONSHIPS_CAP,
        "sources": SOURCES_CAP,
    }
    overflowed = tuple(
        block for block in PLANNING_BLOCKS if len(resolved[block]) > caps[block]
    )
    blocks = {block: resolved[block][: caps[block]] for block in PLANNING_BLOCKS}
    return blocks, overflowed


def _drop_one(data: dict[str, Any], overflowed: set[str]) -> str | None:
    """Give up the last item of the furthest block that still has one."""

    for block in reversed(PLANNING_BLOCKS):
        if data[block]:
            data[block].pop()
            overflowed.add(block)
            return block
    if data["recent_worklog"]:
        data["recent_worklog"].pop()
        return "recent_worklog"
    return None


def shrink_planning_data(
    *,
    data: dict[str, Any],
    overflowed: tuple[str, ...],
    core_overflow_marker: str,
    core_overflowed: bool,
    size: Callable[[dict[str, Any]], int],
    limit: int,
) -> bool:
    """Trim planning blocks, then recent worklog, until the envelope fits.

    Planning blocks give up room before the core worklog does, because core-v1 is
    the guaranteed part of the answer. Returns False when even an emptied planning
    projection is still too large, so the caller can refuse content-free.
    """

    dropped = set(overflowed)
    worklog_overflowed = core_overflowed
    while size(data) > limit:
        block = _drop_one(data, dropped)
        if block is None:
            return False
        if block == "recent_worklog":
            worklog_overflowed = True
        markers = planning_omitted(overflowed=tuple(sorted(dropped)))
        if worklog_overflowed:
            markers.append(core_overflow_marker)
        data["omitted"] = markers
    return True


PLANNING_DATA_FIELDS = frozenset(
    {"omitted", "recent_worklog", "task", "workspace_uid"} | set(PLANNING_BLOCKS)
)
_BLOCK_FIELDS = {
    "objectives": frozenset({"id", "quarter", "status", "title"}),
    "relationships": frozenset({"id", "kind", "status", "title"}),
    "sources": frozenset(
        {"display_title", "id", "link_reasons", "provider", "resource_type", "status"}
    ),
}
_BLOCK_CAPS = {
    "objectives": OBJECTIVES_CAP,
    "relationships": RELATIONSHIPS_CAP,
    "sources": SOURCES_CAP,
}


def _require_item(item: object, block: str) -> dict[str, Any]:
    if type(item) is not dict or set(item) != _BLOCK_FIELDS[block]:
        raise ValueError("invalid planning {} item".format(block))
    return item


def _validate_objective_item(item: dict[str, Any]) -> str:
    identifier = _pattern(item.get("id"), OBJECTIVE_ID_RE, "Objective id")
    _optional_quarter(item.get("quarter"))
    _enum(item.get("status"), OBJECTIVE_STATUSES, "Objective status")
    _nonempty(item.get("title"), "Objective title")
    return identifier


def _validate_relationship_item(item: dict[str, Any]) -> tuple[str, str]:
    identifier = _pattern(item.get("id"), TASK_ID_RE, "relationship id")
    kind = _enum(item.get("kind"), RELATIONSHIP_KINDS, "relationship kind")
    _enum(item.get("status"), RELATIONSHIP_STATUSES, "relationship status")
    _nonempty(item.get("title"), "relationship title")
    return kind, identifier


def _validate_source_item(item: dict[str, Any]) -> str:
    identifier = _pattern(item.get("id"), CAPTURE_ID_RE, "source id")
    _bounded(item.get("display_title"), "Capture display title", DISPLAY_TITLE_MAX)
    _validate_link_reasons(item.get("link_reasons"))
    _enum(item.get("provider"), SOURCE_PROVIDERS, "Capture provider")
    _bounded(item.get("resource_type"), "Capture resource type", RESOURCE_TYPE_MAX)
    _enum(item.get("status"), SOURCE_STATUSES, "Capture status")
    return identifier


def _validate_relationships(items: list[object]) -> None:
    keys: list[tuple[str, str]] = []
    order = {kind: index for index, kind in enumerate(RELATIONSHIP_KINDS)}
    for item in items:
        keys.append(_validate_relationship_item(_require_item(item, "relationships")))
    if len(keys) != len(set(keys)):
        raise ValueError("planning relationships identifiers repeat")
    expected = sorted(keys, key=lambda pair: (order[pair[0]], pair[1]))
    if keys != expected:
        raise ValueError("planning relationships is not sorted")


def _validate_sorted_block(data: dict[str, Any], block: str) -> None:
    items = data[block]
    if type(items) is not list or len(items) > _BLOCK_CAPS[block]:
        raise ValueError("invalid planning {}".format(block))
    if block == "relationships":
        _validate_relationships(items)
        return
    identifiers = []
    for item in items:
        record = _require_item(item, block)
        if block == "objectives":
            identifiers.append(_validate_objective_item(record))
        else:
            identifiers.append(_validate_source_item(record))
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("planning {} identifiers repeat".format(block))
    if identifiers != sorted(identifiers):
        raise ValueError("planning {} is not sorted".format(block))


def validate_planning_data(data: dict[str, Any], *, core_overflow_marker: str) -> None:
    """The depth check the frozen renderer delegates to this helper.

    The contract validates the core half and the planning key set; everything the
    planning blocks and their omission markers must satisfy is checked here, before
    the envelope is rendered.
    """

    if type(data) is not dict or set(data) != PLANNING_DATA_FIELDS:
        raise ValueError("invalid planning context data")
    for block in PLANNING_BLOCKS:
        _validate_sorted_block(data, block)
    omitted = data["omitted"]
    allowed = set(PLANNING_OMITTED_CATEGORIES) | {core_overflow_marker} | {
        overflow_marker(block) for block in PLANNING_BLOCKS
    }
    if (
        type(omitted) is not list
        or any(type(item) is not str for item in omitted)
        or len(omitted) != len(set(omitted))
        or not set(PLANNING_OMITTED_CATEGORIES).issubset(omitted)
        or not set(omitted).issubset(allowed)
    ):
        raise ValueError("invalid planning omission markers")
