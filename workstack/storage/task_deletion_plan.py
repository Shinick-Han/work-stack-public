"""Pure Task deletion inventory / purge plan.

Deterministic. No filesystem, lock, journal, backup, clock, randomness,
or network. Never mutates caller-provided documents/records.

The shared refusal identity, coercion helpers and unsafe-reference scanners
live in ``deletion_plan_primitives``; the per-layout collectors live in
``deletion_plan_v3`` and ``deletion_plan_v4``. This module owns the plan
record itself and the two entrypoints that order the collectors.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import deletion_plan_v3 as v3
from . import deletion_plan_v4 as v4
from .canonical import canonical_json_bytes
from .deletion_plan_primitives import (
    KNOWN_NON_REFERENCE_BODY_KEYS,
    TASK_DISPLAY_RE,
    TaskDeletionPlanError,
    as_mapping,
    as_sequence,
    high_water,
    idempotency_ops,
    index_tasks,
    require_display_id,
    require_expected_revision,
    require_target_revision,
    scan_unknown_target_identity,
    scan_unknown_task_refs,
    sort_ops,
    token_set,
)


PLAN_SCHEMA = "workstack.task-deletion-plan.v1"
PREVIEW_SCHEMA = "workstack.task-deletion-preview.v1"

__all__ = [
    "KNOWN_NON_REFERENCE_BODY_KEYS",
    "PLAN_SCHEMA",
    "PREVIEW_SCHEMA",
    "TASK_DISPLAY_RE",
    "TaskDeletionPlan",
    "TaskDeletionPlanError",
    "plan_v3_task_deletion",
    "plan_v4_task_deletion",
]


@dataclass(frozen=True)
class TaskDeletionPlan:
    """Content-minimal immutable deletion inventory."""

    layout: str
    target_display_id: str
    target_uid: str
    expected_revision: int
    display_id_high_water: int
    operations: tuple[Mapping[str, Any], ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "display_id_high_water": self.display_id_high_water,
            "expected_revision": self.expected_revision,
            "layout": self.layout,
            "operations": [dict(item) for item in self.operations],
            "schema": PLAN_SCHEMA,
            "target_display_id": self.target_display_id,
            "target_uid": self.target_uid,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def preview(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rewritten_tasks: set[str] = set()
        for item in self.operations:
            op = str(item["op"])
            counts[op] = counts.get(op, 0) + 1
            subject = item.get("subject_display_id")
            if op == "rewrite_task_field" and isinstance(subject, str):
                rewritten_tasks.add(subject)
        return {
            "display_id_high_water": self.display_id_high_water,
            "expected_revision": self.expected_revision,
            "layout": self.layout,
            "operation_counts": {key: counts[key] for key in sorted(counts)},
            "rewritten_task_display_ids": sorted(rewritten_tasks),
            "schema": PREVIEW_SCHEMA,
            "target_display_id": self.target_display_id,
            "target_uid": self.target_uid,
        }

    def preview_canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.preview())


def _finish_plan(
    *,
    layout: str,
    wanted: str,
    uid: str,
    expected: int,
    all_ids: Sequence[str],
    operations: Sequence[Mapping[str, Any]],
) -> TaskDeletionPlan:
    return TaskDeletionPlan(
        layout=layout,
        target_display_id=wanted,
        target_uid=uid,
        expected_revision=expected,
        display_id_high_water=high_water(all_ids),
        operations=sort_ops(operations),
    )


def plan_v3_task_deletion(
    documents: Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int,
) -> TaskDeletionPlan:
    snapshot = copy.deepcopy(dict(documents))
    scan_unknown_task_refs(snapshot)
    wanted = require_display_id(task_id)
    expected = require_expected_revision(expected_revision)
    backlog = as_mapping(snapshot.get("backlog.json", {}))
    tasks = as_sequence(backlog.get("tasks", []))
    target, all_ids = index_tasks(tasks, wanted, "id")
    uid = require_target_revision(target, expected)
    scan_unknown_target_identity(snapshot, frozenset({wanted, uid}))
    operations: list[dict[str, Any]] = [
        {"display_id": wanted, "op": "remove_task_record", "uid": uid}
    ]
    operations.extend(v3.owned_note_ops(target, wanted))
    operations.extend(v3.task_reference_ops(tasks, wanted))
    operations.extend(v3.note_link_ops(snapshot, wanted))
    reply_ids, reply_ops = v3.reply_ops(snapshot, wanted)
    operations.extend(reply_ops)
    operations.extend(v3.worklog_ops(snapshot, wanted))
    operations.extend(v3.capture_ops(snapshot, wanted))
    activity_doc = as_mapping(snapshot.get("activity.json", {}))
    operations.extend(v3.activity_ops(activity_doc, wanted))
    operations.extend(v3.planning_ops(activity_doc, wanted))
    operations.extend(
        idempotency_ops(
            activity_doc.get("idempotency", []),
            token_set(wanted, uid, *reply_ids),
        )
    )
    return _finish_plan(
        layout="v3",
        wanted=wanted,
        uid=uid,
        expected=expected,
        all_ids=all_ids,
        operations=operations,
    )


def plan_v4_task_deletion(
    physical: Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int,
) -> TaskDeletionPlan:
    snapshot = copy.deepcopy(dict(physical))
    scan_unknown_task_refs(snapshot)
    wanted = require_display_id(task_id)
    expected = require_expected_revision(expected_revision)
    records = as_mapping(snapshot.get("records", {}))
    streams = as_mapping(snapshot.get("streams", {}))
    tasks = as_sequence(records.get("tasks", []))
    target, all_ids = index_tasks(tasks, wanted, "display_id")
    uid = require_target_revision(target, expected)
    scan_unknown_target_identity(snapshot, frozenset({wanted, uid}))
    operations: list[dict[str, Any]] = [
        {"display_id": wanted, "op": "remove_task_record", "uid": uid}
    ]
    operations.extend(v4.task_reference_ops(tasks, wanted, uid))
    operations.extend(v4.note_ops(records, wanted, uid))
    reply_ids, reply_uids, reply_ops = v4.reply_ops(records, uid)
    operations.extend(reply_ops)
    operations.extend(v4.capture_ops(records, wanted, uid))
    operations.extend(v4.activity_ops(streams, uid))
    operations.extend(v4.planning_ops(streams, uid))
    operations.extend(v4.worklog_ops(streams, uid))
    operations.extend(
        idempotency_ops(
            v4.ledger_records(snapshot),
            token_set(wanted, uid, *reply_ids, *reply_uids),
        )
    )
    return _finish_plan(
        layout="v4",
        wanted=wanted,
        uid=uid,
        expected=expected,
        all_ids=all_ids,
        operations=operations,
    )
