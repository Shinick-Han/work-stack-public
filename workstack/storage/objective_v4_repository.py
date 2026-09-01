"""Explicit-opt-in v4 repository backend for Objective creation intents."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any, Callable, Mapping

from .canonical import canonical_sha256
from .intent_contract import NormalizedIntent, replay_response, validate_idempotency_key
from .intent_v4_repository import V4IntentRepository
from .journal import JournalTarget
from .objective_contract import normalize_key_result_create, normalize_objective_create
from .reader import V4ReadResult
from .records import stage_record_put
from .runtime import RuntimeAuthority


class V4ObjectiveRepository(V4IntentRepository):
    """Stage Objective records, Activity events, and replay metadata atomically."""

    def __init__(
        self,
        runtime: RuntimeAuthority,
        *,
        enable_v4_objectives: bool = False,
        now: Callable[[], str],
        uid_factory: Callable[[], str],
    ) -> None:
        super().__init__(
            runtime,
            enable_v4_intents=enable_v4_objectives,
            now=now,
            uid_factory=uid_factory,
        )

    def create_objective(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/objectives",
    ) -> dict[str, Any]:
        physical, ledger, ledger_body, generation = self._baseline()
        replay = self._raw_replay(ledger, idempotency_key, body, path)
        if replay is not None:
            return replay
        work_date = self._timestamp()[:10]
        intent = normalize_objective_create(
            body,
            [str(item["display_id"]) for item in physical.records["objectives"]],
            created_date=work_date,
            current_quarter=_quarter(work_date),
            path=path,
        )
        objective_uid = self._uid()
        record = _objective_record(physical.workspace_uid, objective_uid, intent)
        record_target = _record_create_target(record)
        event = self._activity_event(
            physical,
            record_uid=objective_uid,
            event_type="objective.created",
            details={"objective_id": record["display_id"], "revision": 0},
        )
        stream_targets = self._stream_targets(physical, (("activity", event),))
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            (record_target, *stream_targets),
            operation_id=f"objective-create-{idempotency_key}",
        )

    def add_key_result(
        self,
        objective_id: str,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        physical, ledger, ledger_body, generation = self._baseline()
        replay = self._raw_replay(ledger, idempotency_key, body, path)
        if replay is not None:
            return replay
        current = _objective(physical, objective_id)
        intent = normalize_key_result_create(
            body, _legacy_objective(current), updated_date=self._timestamp()[:10], path=path
        )
        proposed = _updated_objective(current, intent, self._uid())
        record_target = _record_update_target(physical, current, proposed)
        key_result = proposed["key_results"][-1]
        event = self._activity_event(
            physical,
            record_uid=str(current["uid"]),
            event_type="key_result.created",
            details={
                "objective_id": current["display_id"],
                "key_result_id": key_result["display_id"],
                "revision": proposed["revision"],
            },
        )
        stream_targets = self._stream_targets(physical, (("activity", event),))
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            (record_target, *stream_targets),
            operation_id=f"key-result-create-{idempotency_key}",
        )

    @staticmethod
    def _raw_replay(
        ledger: Mapping[str, Any],
        key: str,
        body: Mapping[str, Any],
        path: str,
    ) -> dict[str, Any] | None:
        return replay_response(
            ledger["records"],
            validate_idempotency_key(key),
            method="POST",
            path=path,
            request_digest=canonical_sha256(dict(body)),
        )

    def _activity_event(
        self,
        physical: V4ReadResult,
        *,
        record_uid: str,
        event_type: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "format": "workstack.activity-event",
            "schema_version": 1,
            "workspace_uid": physical.workspace_uid,
            "event_uid": self._uid(),
            "record_uid": record_uid,
            "created_at": self._timestamp(),
            "actor": "local.user",
            "provenance": "workstack.objective-v4",
            "legacy_event_id": _next_event_id(physical),
            "event_type": event_type,
            "details": copy.deepcopy(dict(details)),
            "capture_uid": None,
            "task_uid": None,
            "reply_uid": None,
        }


def _quarter(work_date: str) -> str:
    value = dt.date.fromisoformat(work_date)
    return f"{value.year}-Q{((value.month - 1) // 3) + 1}"


def _objective_record(
    workspace_uid: str, uid: str, intent: NormalizedIntent
) -> dict[str, Any]:
    objective = intent.authority_value
    return {
        "format": "workstack.objective",
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "uid": uid,
        "revision": 0,
        "created_at": objective["created"],
        "updated_at": objective["updated_at"],
        "display_id": objective["id"],
        "title": objective["objective"],
        "status": objective["status"],
        "quarter": objective["quarter"],
        "key_results": [],
        "revision_origin": "explicit",
    }


def _legacy_objective(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record["display_id"],
        "quarter": record["quarter"],
        "objective": record["title"],
        "status": record["status"],
        "key_results": [
            {
                "id": item["display_id"],
                "text": item["title"],
                "target": item["target"],
                "progress": item["progress"],
                "status": item["status"],
            }
            for item in record["key_results"]
        ],
        "created": record["created_at"],
        "updated_at": record["updated_at"],
        "revision": record["revision"],
    }


def _updated_objective(
    current: Mapping[str, Any], intent: NormalizedIntent, key_result_uid: str
) -> dict[str, Any]:
    legacy = intent.authority_value
    key_results = []
    prior = {item["display_id"]: item for item in current["key_results"]}
    for item in legacy["key_results"]:
        existing = prior.get(item["id"])
        key_results.append(
            {
                "uid": key_result_uid if existing is None else existing["uid"],
                "display_id": item["id"],
                "title": item["text"],
                "status": item["status"],
                "target": item["target"],
                "progress": item["progress"],
            }
        )
    proposed = copy.deepcopy(dict(current))
    proposed["revision"] = legacy["revision"]
    proposed["updated_at"] = legacy["updated_at"]
    proposed["key_results"] = key_results
    proposed["revision_origin"] = "explicit"
    return proposed


def _objective(physical: V4ReadResult, display_id: str) -> Mapping[str, Any]:
    wanted = display_id.strip().upper() if isinstance(display_id, str) else ""
    matches = [
        item for item in physical.records["objectives"] if item["display_id"] == wanted
    ]
    if len(matches) != 1:
        from .intent_contract import IntentContractError

        raise IntentContractError("OBJECTIVE_NOT_FOUND")
    return matches[0]


def _record_create_target(record: Mapping[str, Any]) -> JournalTarget:
    staged = stage_record_put(
        "objectives", record, current=None, expected_revision=None, expected_digest=None
    )
    return JournalTarget.replace(staged.artifact, staged.body or b"", expected_digest=None)


def _record_update_target(
    physical: V4ReadResult,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> JournalTarget:
    artifact = next(
        item for item in physical.artifacts
        if item.category == "record" and item.kind == "objectives" and str(current["uid"]) in item.artifact
    )
    staged = stage_record_put(
        "objectives",
        proposed,
        current=current,
        expected_revision=int(current["revision"]),
        expected_digest=artifact.sha256,
    )
    return JournalTarget.replace(
        staged.artifact, staged.body or b"", expected_digest=staged.expected_digest
    )


def _next_event_id(physical: V4ReadResult) -> str:
    largest = max(
        (int(str(event["legacy_event_id"])[2:]) for event in physical.streams["activity"]),
        default=0,
    )
    return f"E-{largest + 1:06d}"
