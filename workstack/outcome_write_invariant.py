"""Pure Task Objective/KR write invariant (plan §6 K2 / §8.4).

Authoritative mutation remains ``WorkStack.patch_task``. This module does not
touch storage; it canonicalizes refs, fail-closes unknown/ambiguous/wrong-parent
targets, and appends missing parent Objective IDs exactly once.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


class OutcomeWriteInvariantError(ValueError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def serialize_task_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Omit unset ``key_result_refs``; keep an explicit empty list as a clear."""

    body = dict(patch)
    if "key_result_refs" in body and body["key_result_refs"] is None:
        del body["key_result_refs"]
    return body


def canonicalize_key_result_refs(
    refs: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Drop duplicate pairs, then sort. Unrelated Objective IDs are not touched."""

    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for ref in refs:
        pair = (ref["objective_id"], ref["key_result_id"])
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    unique.sort()
    return [
        {"objective_id": objective_id, "key_result_id": key_result_id}
        for objective_id, key_result_id in unique
    ]


def ensure_parent_objective_ids(
    objective_ids: Sequence[str],
    refs: Sequence[Mapping[str, str]],
) -> list[str]:
    """Keep first-seen Objective order; append missing KR parents exactly once."""

    seen: set[str] = set()
    result: list[str] = []
    for item in objective_ids:
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
    for ref in refs:
        parent = ref["objective_id"]
        if parent in seen:
            continue
        result.append(parent)
        seen.add(parent)
    return result


def _key_result_match_count(objective: Mapping[str, Any], key_result_id: str) -> int:
    return sum(
        1
        for item in objective.get("key_results", [])
        if isinstance(item, dict)
        and str(item.get("id", "")).strip().upper() == key_result_id
    )


def validate_key_result_ref_roster(
    ref: Mapping[str, str],
    objectives_by_id: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Fail closed unless the Objective exists uniquely and owns the KR."""

    objective_id = ref["objective_id"]
    key_result_id = ref["key_result_id"]
    matches = objectives_by_id.get(objective_id, [])
    if not matches:
        raise OutcomeWriteInvariantError(
            "unknown key result reference",
            {"objective_id": objective_id},
        )
    if len(matches) != 1:
        raise OutcomeWriteInvariantError(
            "key result reference objective is not uniquely resolvable",
            {"objective_id": objective_id},
        )
    owned = _key_result_match_count(matches[0], key_result_id)
    if owned == 1:
        return
    if owned != 0:
        raise OutcomeWriteInvariantError(
            "unknown key result reference",
            {"key_result_id": key_result_id},
        )
    other_owners = [
        other_id
        for other_id, records in objectives_by_id.items()
        if other_id != objective_id
        and len(records) == 1
        and _key_result_match_count(records[0], key_result_id) == 1
    ]
    if other_owners:
        raise OutcomeWriteInvariantError(
            "key result is not owned by the referenced objective",
            {"objective_id": objective_id, "key_result_id": key_result_id},
        )
    raise OutcomeWriteInvariantError(
        "unknown key result reference",
        {"key_result_id": key_result_id},
    )


def validate_resulting_key_result_refs(
    refs: Any,
    objective_ids: Any,
    objectives_by_id: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Refuse leftover refs whose parent is unaligned or unresolvable."""

    if refs is None:
        return
    aligned = set(objective_ids or [])
    for ref in refs:
        objective_id = ref["objective_id"]
        if objective_id not in aligned:
            raise OutcomeWriteInvariantError(
                "key result reference parent is not aligned",
                {"objective_id": objective_id},
            )
        matches = objectives_by_id.get(objective_id, [])
        if len(matches) != 1:
            raise OutcomeWriteInvariantError(
                "key result reference objective is not uniquely resolvable",
                {"objective_id": objective_id},
            )
        key_result_id = ref["key_result_id"]
        owned = _key_result_match_count(matches[0], key_result_id)
        if owned == 1:
            continue
        if owned != 0:
            raise OutcomeWriteInvariantError(
                "unknown key result reference",
                {"key_result_id": key_result_id},
            )
        other_owners = [
            other_id
            for other_id, records in objectives_by_id.items()
            if other_id != objective_id
            and len(records) == 1
            and _key_result_match_count(records[0], key_result_id) == 1
        ]
        if other_owners:
            raise OutcomeWriteInvariantError(
                "key result is not owned by the referenced objective",
                {"objective_id": objective_id, "key_result_id": key_result_id},
            )
        raise OutcomeWriteInvariantError(
            "unknown key result reference",
            {"key_result_id": key_result_id},
        )


def apply_task_outcome_write_invariant(
    changes: dict[str, Any],
    task: Mapping[str, Any],
    objectives_by_id: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Mutate ``changes``: auto-align KR parents; never write on fail-closed refs."""

    if "key_result_refs" in changes and changes["key_result_refs"]:
        for ref in changes["key_result_refs"]:
            validate_key_result_ref_roster(ref, objectives_by_id)
        current_ids = list(task.get("objective_ids") or [])
        base_ids = (
            list(changes["objective_ids"])
            if "objective_ids" in changes
            else current_ids
        )
        aligned = ensure_parent_objective_ids(base_ids, changes["key_result_refs"])
        if "objective_ids" in changes or aligned != current_ids:
            changes["objective_ids"] = aligned

    validate_resulting_key_result_refs(
        changes.get("key_result_refs", task.get("key_result_refs")),
        changes.get("objective_ids", task.get("objective_ids", [])),
        objectives_by_id,
    )
