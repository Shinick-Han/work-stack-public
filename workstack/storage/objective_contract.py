"""Backend-neutral Objective and key-result intent normalization."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from .canonical import canonical_sha256
from .intent_contract import IntentContractError, NormalizedIntent


MAX_REVISION = 9_007_199_254_740_991


def normalize_objective_create(
    body: Mapping[str, Any],
    display_ids: Sequence[str],
    *,
    created_date: str,
    current_quarter: str,
    path: str = "/api/v1/objectives",
) -> NormalizedIntent:
    title = _required_text(body.get("objective"), "OBJECTIVE_TEXT_REQUIRED")
    quarter = body.get("quarter") or current_quarter
    if not isinstance(quarter, str):
        raise IntentContractError("OBJECTIVE_QUARTER_INVALID")
    objective = {
        "id": _next_id(display_ids, "O"),
        "quarter": quarter,
        "objective": title,
        "status": "active",
        "key_results": [],
        "created": created_date,
        "updated_at": created_date,
        "revision": 0,
    }
    return NormalizedIntent(
        path,
        canonical_sha256(dict(body)),
        copy.deepcopy(objective),
        copy.deepcopy(objective),
    )


def normalize_key_result_create(
    body: Mapping[str, Any],
    objective: Mapping[str, Any],
    *,
    updated_date: str,
    path: str,
) -> NormalizedIntent:
    current = objective.get("revision", 0)
    received = body.get("revision")
    if type(current) is not int or not 0 <= current <= MAX_REVISION:
        raise IntentContractError("OBJECTIVE_REVISION_INVALID")
    if type(received) is not int or received < 0:
        raise IntentContractError("REVISION_REQUIRED")
    if received != current:
        raise IntentContractError("OBJECTIVE_REVISION_CONFLICT")
    if current == MAX_REVISION:
        raise IntentContractError("OBJECTIVE_REVISION_EXHAUSTED")
    key_results = copy.deepcopy(objective.get("key_results"))
    if not isinstance(key_results, list):
        raise IntentContractError("KEY_RESULT_ROSTER_INVALID")
    target = body.get("target")
    if not isinstance(target, str):
        raise IntentContractError("KEY_RESULT_TARGET_INVALID")
    key_result = {
        "id": _next_id([str(item.get("id")) for item in key_results], "KR"),
        "text": _required_text(body.get("text"), "KEY_RESULT_TEXT_REQUIRED"),
        "target": target.strip(),
        "progress": 0,
        "status": "active",
    }
    updated = copy.deepcopy(dict(objective))
    updated["key_results"] = [*key_results, key_result]
    updated["revision"] = current + 1
    updated["updated_at"] = updated_date
    return NormalizedIntent(
        path,
        canonical_sha256(dict(body)),
        copy.deepcopy(updated),
        updated,
    )


def _required_text(value: object, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise IntentContractError(code)
    return text


def _next_id(values: Sequence[str], prefix: str) -> str:
    pattern = re.compile(rf"{re.escape(prefix)}-(\d+)", re.IGNORECASE)
    largest = 0
    for value in values:
        match = pattern.fullmatch(value)
        if match:
            largest = max(largest, int(match.group(1)))
    return f"{prefix}-{largest + 1}"
