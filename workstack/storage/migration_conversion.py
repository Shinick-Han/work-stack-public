"""Pure deterministic conversion from the frozen v3 SSOT to v4 values.

The converter deliberately has no filesystem dependency.  It accepts the nine
already-validated v3 documents and returns schema-valid candidate values plus
the deferred runtime/receipt evidence required by later migration waves.  It
does not back up, write, admit, or activate a repository.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .canonical import CanonicalJsonError, canonical_sha256
from .contracts import StorageContractError, require_valid_by_format
from .migration_idempotency import convert_v3_idempotency_ledger
from .semantic import (
    SemanticProjectionError,
    V4SemanticSource,
    snapshot_from_v3_documents,
    snapshot_from_v4,
)


RECORD_KINDS = ("tasks", "objectives", "captures", "replies", "notes")
STREAM_KINDS = ("planning-status", "activity", "worklog")


class V3ConversionError(ValueError):
    """Content-free refusal to convert an invalid or unrepresentable source."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class V4Conversion:
    """In-memory v4 candidate artifacts and deferred migration evidence."""

    store: Mapping[str, Any]
    workspace: Mapping[str, Any]
    records: Mapping[str, tuple[Mapping[str, Any], ...]]
    streams: Mapping[str, tuple[Mapping[str, Any], ...]]
    idempotency_ledger: Mapping[str, Any]
    generated_id_roster: tuple[Mapping[str, Any], ...]
    task_note_source_roster: tuple[Mapping[str, Any], ...]
    task_note_source_indexes: Mapping[str, int]
    legacy_store_metadata: Mapping[str, Any]
    source_snapshot_digest: str
    conversion_digest: str

    def semantic_idempotency_records(self) -> tuple[Mapping[str, Any], ...]:
        """Reverse-project runtime ledger references into frozen v3 semantics."""

        try:
            require_valid_by_format(self.idempotency_ledger)
            reply_ids = {
                record["uid"]: record["display_id"] for record in self.records["replies"]
            }
            projected: list[Mapping[str, Any]] = []
            for source in self.idempotency_ledger["records"]:
                record = {
                    key: copy.deepcopy(value)
                    for key, value in source.items()
                    if key != "expires_at"
                }
                reference = record.get("response_ref")
                if reference is not None:
                    record["response_ref"] = {
                        "kind": "reply",
                        "id": reply_ids[reference["record_uid"]],
                    }
                projected.append(record)
            return tuple(projected)
        except (KeyError, TypeError, StorageContractError, ValueError) as error:
            raise V3ConversionError("IDEMPOTENCY_LEDGER_INVALID") from error

    @property
    def idempotency_records(self) -> tuple[Mapping[str, Any], ...]:
        """Compatibility view derived from the normalized runtime ledger."""

        return self.semantic_idempotency_records()

    def semantic_source(self) -> V4SemanticSource:
        """Return a detached semantic source suitable for parity/read tests."""

        return V4SemanticSource(
            workspace=copy.deepcopy(dict(self.workspace)),
            tasks=copy.deepcopy(self.records["tasks"]),
            objectives=copy.deepcopy(self.records["objectives"]),
            captures=copy.deepcopy(self.records["captures"]),
            replies=copy.deepcopy(self.records["replies"]),
            notes=copy.deepcopy(self.records["notes"]),
            planning_events=copy.deepcopy(self.streams["planning-status"]),
            activity_events=copy.deepcopy(self.streams["activity"]),
            worklog_events=copy.deepcopy(self.streams["worklog"]),
            idempotency_records=copy.deepcopy(self.semantic_idempotency_records()),
            task_note_source_indexes=copy.deepcopy(dict(self.task_note_source_indexes)),
        )

    def artifact_values(self) -> dict[str, Any]:
        """Return a canonicalizable, detached package without filesystem paths."""

        return {
            "store": copy.deepcopy(dict(self.store)),
            "workspace": copy.deepcopy(dict(self.workspace)),
            "records": {
                kind: copy.deepcopy(list(self.records[kind])) for kind in RECORD_KINDS
            },
            "streams": {
                kind: copy.deepcopy(list(self.streams[kind])) for kind in STREAM_KINDS
            },
            "runtime": {
                "idempotency_ledger": copy.deepcopy(dict(self.idempotency_ledger)),
            },
            "migration_evidence": {
                "legacy_store_metadata": copy.deepcopy(dict(self.legacy_store_metadata)),
                "generated_id_roster": copy.deepcopy(list(self.generated_id_roster)),
                "task_note_source_roster": copy.deepcopy(list(self.task_note_source_roster)),
                "task_note_source_indexes": copy.deepcopy(dict(self.task_note_source_indexes)),
                "source_snapshot_digest": self.source_snapshot_digest,
            },
        }


def _uid(namespace: str, name: str) -> str:
    return str(uuid.uuid5(uuid.UUID(namespace), name))


def _record_envelope(
    format_name: str,
    workspace_uid: str,
    uid: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    created = source.get("created", source.get("created_at"))
    return {
        "format": format_name,
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "uid": uid,
        "revision": source.get("revision", 0),
        "created_at": created,
        "updated_at": source.get("updated_at", created),
    }


def _event_envelope(
    format_name: str,
    workspace_uid: str,
    event_uid: str,
    record_uid: str | None,
    created_at: str,
    actor: str | None,
    provenance: str,
) -> dict[str, Any]:
    return {
        "format": format_name,
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "event_uid": event_uid,
        "sequence": 0,
        "record_uid": record_uid,
        "created_at": created_at,
        "actor": actor,
        "provenance": provenance,
    }


def _identity_indexes(documents: Mapping[str, Mapping[str, Any]], workspace_uid: str) -> dict[str, dict[str, str]]:
    return {
        "task": {item["id"]: item["uid"] for item in documents["backlog.json"]["tasks"]},
        "objective": {
            item["id"]: _uid(workspace_uid, f"objective:{item['id']}")
            for item in documents["okr.json"]["objectives"]
        },
        "capture": {
            item["id"]: _uid(workspace_uid, f"capture:{item['id']}")
            for item in documents["captures.json"]["captures"]
        },
        "reply": {
            item["id"]: _uid(workspace_uid, f"reply:{item['id']}")
            for item in documents["replies.json"]["replies"]
        },
    }


def _semantic_ordered_documents(
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Normalize only record-set order; stream and embedded order stay semantic."""

    ordered = copy.deepcopy(dict(documents))
    for document, field in (
        ("backlog.json", "tasks"),
        ("okr.json", "objectives"),
        ("notes.json", "notes"),
        ("captures.json", "captures"),
        ("replies.json", "replies"),
    ):
        ordered[document][field].sort(key=lambda item: item["id"])
    return ordered


def _generated_id(
    entity_kind: str, source_identity: str, generated_uid: str
) -> dict[str, Any]:
    return {
        "entity_kind": entity_kind,
        "source_identity_digest": canonical_sha256(
            {"entity_kind": entity_kind, "source_identity": source_identity}
        ),
        "generated_uid": generated_uid,
    }


def _generated_id_roster(
    documents: Mapping[str, Mapping[str, Any]],
    workspace_uid: str,
    indexes: Mapping[str, Mapping[str, str]],
) -> tuple[Mapping[str, Any], ...]:
    roster = [
        *_objective_generated_ids(documents, indexes),
        *_record_generated_ids(documents, workspace_uid, indexes),
        *_stream_generated_ids(documents, workspace_uid),
    ]
    return tuple(sorted(roster, key=lambda item: (item["entity_kind"], item["source_identity_digest"])))


def _objective_generated_ids(
    documents: Mapping[str, Mapping[str, Any]],
    indexes: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    for objective in documents["okr.json"]["objectives"]:
        objective_id = objective["id"]
        objective_uid = indexes["objective"][objective_id]
        roster.append(_generated_id("objective", f"objective:{objective_id}", objective_uid))
        for child in objective["key_results"]:
            identity = f"key-result:{child['id']}"
            roster.append(_generated_id("key_result", f"{objective_id}:{identity}", _uid(objective_uid, identity)))
    return roster


def _record_generated_ids(
    documents: Mapping[str, Mapping[str, Any]],
    workspace_uid: str,
    indexes: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    for kind, document, field, prefix in (
        ("capture", "captures.json", "captures", "capture"),
        ("reply", "replies.json", "replies", "reply"),
    ):
        for source in documents[document][field]:
            roster.append(_generated_id(kind, f"{prefix}:{source['id']}", indexes[kind][source["id"]]))
    for source in documents["notes.json"]["notes"]:
        identity = f"note:{source['id']}"
        roster.append(_generated_id("standalone_note", identity, _uid(workspace_uid, identity)))
    for task in documents["backlog.json"]["tasks"]:
        for child in task["subtasks"]:
            identity = f"subtask:{child['id']}"
            roster.append(_generated_id("subtask", f"{task['uid']}:{identity}", _uid(task["uid"], identity)))
    return roster


def _stream_generated_ids(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str
) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    for source in documents["activity.json"]["planning_status"]:
        identity = f"planning-status:{source['id']}"
        roster.append(_generated_id("planning_status_event", identity, _uid(workspace_uid, identity)))
    for source in documents["activity.json"]["activity"]:
        identity = f"activity:{source['id']}"
        roster.append(_generated_id("activity_event", identity, _uid(workspace_uid, identity)))
    for work_date, day in sorted(documents["worklog.json"]["days"].items()):
        if "start_time" in day:
            identity = f"worklog:{work_date}:check-in"
            roster.append(_generated_id("worklog_check_in", identity, _uid(workspace_uid, identity)))
        for source_index, _source in enumerate(day.get("entries", [])):
            identity = f"worklog:{work_date}:entry:{source_index}"
            roster.append(_generated_id("worklog_entry", identity, _uid(workspace_uid, identity)))
        for source in day.get("sessions", []):
            identity = f"worklog:{work_date}:session:{source['id']}"
            roster.append(_generated_id("worklog_session", identity, _uid(workspace_uid, identity)))
    return roster


def _convert_objectives(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str, indexes: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in sorted(documents["okr.json"]["objectives"], key=lambda item: item["id"]):
        objective_uid = indexes["objective"][source["id"]]
        records.append({
            **_record_envelope("workstack.objective", workspace_uid, objective_uid, source),
            "display_id": source["id"],
            "title": source["objective"],
            "status": source["status"],
            "quarter": source["quarter"],
            "key_results": [
                {
                    "uid": _uid(objective_uid, f"key-result:{child['id']}"),
                    "display_id": child["id"],
                    "title": child["text"],
                    "status": child["status"],
                    "target": child["target"],
                    "progress": child["progress"],
                }
                for child in source["key_results"]
            ],
            "revision_origin": "explicit" if "revision" in source else "legacy_missing",
        })
    return records


def _convert_tasks_and_annotations(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str, indexes: Mapping[str, Mapping[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    tasks: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    source_indexes: dict[str, int] = {}
    for source in sorted(documents["backlog.json"]["tasks"], key=lambda item: item["id"]):
        task_uid = source["uid"]
        task = {
            **_record_envelope("workstack.task", workspace_uid, task_uid, source),
            "display_id": source["id"],
            "title": source["title"],
            "detail": source["detail"],
            "priority": source["priority"],
            "due": source["due"],
            "tags": copy.deepcopy(source["tags"]),
            "objective_uids": [indexes["objective"][item] for item in source["objective_ids"]],
            "parent_uid": None if source["parent_id"] is None else indexes["task"][source["parent_id"]],
            "dependency_uids": [indexes["task"][item] for item in source["dependencies"]],
            "reference_uids": [],
            "subtasks": [
                {
                    "uid": _uid(task_uid, f"subtask:{child['id']}"),
                    "display_id": child["id"],
                    "title": child["title"],
                    "priority": child["priority"],
                    "status": child["status"],
                }
                for child in source["subtasks"]
            ],
        }
        for optional in ("scheduled", "estimate_minutes"):
            if optional in source:
                task[optional] = copy.deepcopy(source[optional])
        tasks.append(task)
        for source_index, annotation in enumerate(source["notes"]):
            note_uid = _uid(task_uid, f"task-note:{source_index}")
            source_indexes[note_uid] = source_index
            notes.append({
                **_record_envelope(
                    "workstack.note",
                    workspace_uid,
                    note_uid,
                    {"created": annotation["date"], "updated_at": annotation["date"]},
                ),
                "display_id": f"N-{uuid.UUID(note_uid).int}",
                "note_kind": "task_annotation",
                "task_uid": task_uid,
                "text": annotation["text"],
                "links": [],
                "created_by": "migration.v3",
            })
    return tasks, notes, source_indexes


def _convert_standalone_notes(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str
) -> list[dict[str, Any]]:
    records = []
    for source in sorted(documents["notes.json"]["notes"], key=lambda item: item["id"]):
        note_uid = _uid(workspace_uid, f"note:{source['id']}")
        records.append({
            **_record_envelope("workstack.note", workspace_uid, note_uid, source),
            "display_id": source["id"],
            "note_kind": "standalone",
            "task_uid": None,
            "text": source["text"],
            "links": copy.deepcopy(source["links"]),
            "created_by": "migration.v3",
        })
    return records


def _convert_captures(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str, indexes: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    records = []
    for source in sorted(documents["captures.json"]["captures"], key=lambda item: item["id"]):
        normalized = copy.deepcopy(source["normalized"])
        for item in normalized["action_items"]:
            display_id = item.pop("task_id", None)
            item["task_display_id"] = display_id
            item["task_uid"] = None if display_id is None else indexes["task"][display_id]
        records.append({
            **_record_envelope("workstack.capture", workspace_uid, indexes["capture"][source["id"]], source),
            "display_id": source["id"],
            "capture_packet_schema_version": source["schema_version"],
            "status": source["status"],
            "source_key": source["source_key"],
            "source": copy.deepcopy(source["source"]),
            "normalized": normalized,
            "task_hints": copy.deepcopy(source["task_hints"]),
            "provenance": copy.deepcopy(source["provenance"]),
            "linked_task_uids": [indexes["task"][item] for item in source["linked_task_ids"]],
            "converted_task_uids": [indexes["task"][item] for item in source["converted_task_ids"]],
            "recent_revisions": copy.deepcopy(source["recent_revisions"]),
        })
    return records


def _convert_replies(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str, indexes: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    records = []
    for source in sorted(documents["replies.json"]["replies"], key=lambda item: item["id"]):
        receipt = copy.deepcopy(source["receipt"])
        if receipt is not None:
            receipt["reply_display_id"] = receipt.pop("reply_id")
        records.append({
            **_record_envelope("workstack.reply", workspace_uid, indexes["reply"][source["id"]], source),
            "display_id": source["id"],
            "task_uid": indexes["task"][source["task_id"]],
            "capture_uid": indexes["capture"][source["capture_id"]],
            "capture_revision": source["capture_revision"],
            "provider": source["provider"],
            "capability": source["capability"],
            "target": copy.deepcopy(source["target"]),
            "body": source["body"],
            "body_digest": source["body_digest"],
            "target_digest": source["target_digest"],
            "state": source["state"],
            "approved_at": source["approved_at"],
            "receipt": receipt,
        })
    return records


def _convert_planning_events(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str
) -> list[dict[str, Any]]:
    records = []
    event_uid_by_id: dict[str, str] = {}
    sources = sorted(documents["activity.json"]["planning_status"], key=lambda item: item["id"])
    for source in sources:
        event_uid = _uid(workspace_uid, f"planning-status:{source['id']}")
        event_uid_by_id[source["id"]] = event_uid
        previous_id = source["previous_fact_id"]
        records.append({
            **_event_envelope(
                "workstack.planning-status-event", workspace_uid, event_uid,
                source["task_uid"], source["created_at"], source["actor"], source["provenance"],
            ),
            "legacy_fact_id": source["id"],
            "task_uid": source["task_uid"],
            "task_display_id": source["task_id"],
            "previous_event_uid": None if previous_id is None else event_uid_by_id[previous_id],
            "previous_legacy_fact_id": previous_id,
            "prior_revision": source["prior_revision"],
            "new_revision": source["new_revision"],
            "prior_status": source["prior_status"],
            "status": source["status"],
        })
    return records


def _convert_activity_events(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str, indexes: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    records = []
    for source in sorted(documents["activity.json"]["activity"], key=lambda item: item["id"]):
        task_uid = indexes["task"].get(source.get("task_id"))
        capture_uid = indexes["capture"].get(source.get("capture_id"))
        reply_uid = indexes["reply"].get(source.get("reply_id"))
        records.append({
            **_event_envelope(
                "workstack.activity-event", workspace_uid,
                _uid(workspace_uid, f"activity:{source['id']}"),
                task_uid or capture_uid or reply_uid, source["created_at"], None, "migration.v3",
            ),
            "legacy_event_id": source["id"],
            "event_type": source["type"],
            "details": copy.deepcopy(source["details"]),
            "capture_uid": capture_uid,
            "task_uid": task_uid,
            "reply_uid": reply_uid,
        })
    return records


def _convert_worklog_events(
    documents: Mapping[str, Mapping[str, Any]], workspace_uid: str, indexes: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    records = []
    for work_date, day in sorted(documents["worklog.json"]["days"].items()):
        if "start_time" in day:
            records.append({
                **_event_envelope(
                    "workstack.worklog-event", workspace_uid,
                    _uid(workspace_uid, f"worklog:{work_date}:check-in"),
                    None, work_date, "local.user", "migration.v3",
                ),
                "kind": "check-in", "work_date": work_date, "start_time": day["start_time"],
            })
        for source_index, source in enumerate(day.get("entries", [])):
            event = {
                **_event_envelope(
                    "workstack.worklog-event", workspace_uid,
                    _uid(workspace_uid, f"worklog:{work_date}:entry:{source_index}"),
                    indexes["task"][source["task_id"]], work_date, "local.user", "migration.v3",
                ),
                "kind": "entry", "work_date": work_date,
                "task_uid": indexes["task"][source["task_id"]],
                "task_display_id": source["task_id"], "task_title": source["task"],
                "done": copy.deepcopy(source["done"]), "next": copy.deepcopy(source["next"]),
                "blockers": copy.deepcopy(source["blockers"]),
            }
            for optional in ("session_id", "duration_seconds"):
                if optional in source:
                    event[optional] = copy.deepcopy(source[optional])
            records.append(event)
        for source in sorted(day.get("sessions", []), key=lambda item: item["id"]):
            records.append({
                **_event_envelope(
                    "workstack.worklog-event", workspace_uid,
                    _uid(workspace_uid, f"worklog:{work_date}:session:{source['id']}"),
                    indexes["task"][source["task_id"]], source["updated_at"],
                    "local.user", "migration.v3",
                ),
                "kind": "session", "session_id": source["id"], "work_date": work_date,
                "task_uid": indexes["task"][source["task_id"]],
                "task_display_id": source["task_id"], "task_title": source["task"],
                "state": source["state"], "started_at": source["started_at"],
                "updated_at": source["updated_at"], "segments": copy.deepcopy(source["segments"]),
                "worklog_state": source["worklog_state"],
            })
    return records


def _sequence_and_chain(
    groups: Mapping[str, Sequence[dict[str, Any]]]
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    # A fixed kind roster plus each converter's stable source order preserves
    # planning predecessor order even if legacy timestamps are non-monotonic.
    ordered = [
        (kind, copy.deepcopy(event))
        for kind in STREAM_KINDS
        for event in groups[kind]
    ]
    previous_digest: str | None = None
    result: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in STREAM_KINDS}
    for sequence, (kind, event) in enumerate(ordered, 1):
        event["sequence"] = sequence
        if previous_digest is not None:
            event["previous_event_digest"] = previous_digest
        event["event_digest"] = canonical_sha256(event)
        previous_digest = event["event_digest"]
        result[kind].append(event)
    return {kind: tuple(result[kind]) for kind in STREAM_KINDS}


def _validate_artifacts(workspace: Mapping[str, Any], records: Mapping[str, Sequence[Mapping[str, Any]]], streams: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    require_valid_by_format(workspace)
    for kind in RECORD_KINDS:
        for record in records[kind]:
            require_valid_by_format(record)
    for kind in STREAM_KINDS:
        for event in streams[kind]:
            require_valid_by_format(event)


def convert_v3_documents(
    documents: Mapping[str, Mapping[str, Any]], *, candidate_created_at: str
) -> V4Conversion:
    """Convert validated v3 documents into deterministic in-memory v4 values.

    ``candidate_created_at`` is explicit because v3 has no candidate-package
    creation instant.  Reusing the same source and instant yields byte-identical
    canonical artifacts and the same conversion digest.
    """

    try:
        source_documents = copy.deepcopy(dict(documents))
        semantic_documents = _semantic_ordered_documents(source_documents)
        v3_snapshot = snapshot_from_v3_documents(semantic_documents)
        workspace_source = source_documents["workspace.json"]
        workspace_uid = workspace_source["id"]
        indexes = _identity_indexes(source_documents, workspace_uid)
        objectives = _convert_objectives(source_documents, workspace_uid, indexes)
        tasks, task_notes, note_indexes = _convert_tasks_and_annotations(
            source_documents, workspace_uid, indexes
        )
        notes = task_notes + _convert_standalone_notes(source_documents, workspace_uid)
        captures = _convert_captures(source_documents, workspace_uid, indexes)
        replies = _convert_replies(source_documents, workspace_uid, indexes)
        workspace = {
            **_record_envelope("workstack.workspace", workspace_uid, workspace_uid, {}),
            "display_id": workspace_uid,
            "name": workspace_source["name"],
        }
        records = {
            "tasks": tuple(sorted(tasks, key=lambda item: item["uid"])),
            "objectives": tuple(sorted(objectives, key=lambda item: item["uid"])),
            "captures": tuple(sorted(captures, key=lambda item: item["uid"])),
            "replies": tuple(sorted(replies, key=lambda item: item["uid"])),
            "notes": tuple(sorted(notes, key=lambda item: item["uid"])),
        }
        streams = _sequence_and_chain({
            "planning-status": _convert_planning_events(source_documents, workspace_uid),
            "activity": _convert_activity_events(source_documents, workspace_uid, indexes),
            "worklog": _convert_worklog_events(source_documents, workspace_uid, indexes),
        })
        store = {
            "format": "workstack.ssot", "schema_version": 4,
            "schema_set": "workstack.ssot.v4", "workspace_uid": workspace_uid,
            "created_at": candidate_created_at,
        }
        require_valid_by_format(store)
        _validate_artifacts(workspace, records, streams)
        idempotency_ledger = convert_v3_idempotency_ledger(
            source_documents["activity.json"]["idempotency"],
            workspace_uid=workspace_uid,
            updated_at=candidate_created_at,
            replies=records["replies"],
        )
        source_digest = v3_snapshot.digest
        generated_roster = _generated_id_roster(source_documents, workspace_uid, indexes)
        task_note_roster = tuple(sorted((
            {
                "task_uid": note["task_uid"],
                "source_index": note_indexes[note["uid"]],
                "generated_note_uid": note["uid"],
            }
            for note in task_notes
        ), key=lambda item: (item["task_uid"], item["source_index"])))
        payload = {
            "store": store, "workspace": workspace,
            "records": {kind: list(records[kind]) for kind in RECORD_KINDS},
            "streams": {kind: list(streams[kind]) for kind in STREAM_KINDS},
            "runtime": {"idempotency_ledger": idempotency_ledger},
            "migration_evidence": {
                "legacy_store_metadata": source_documents["store-meta.json"],
                "generated_id_roster": list(generated_roster),
                "task_note_source_roster": list(task_note_roster),
                "task_note_source_indexes": note_indexes,
                "source_snapshot_digest": source_digest,
            },
        }
        conversion = V4Conversion(
            store=store, workspace=workspace, records=records, streams=streams,
            idempotency_ledger=copy.deepcopy(idempotency_ledger),
            generated_id_roster=generated_roster,
            task_note_source_roster=task_note_roster,
            task_note_source_indexes=dict(sorted(note_indexes.items())),
            legacy_store_metadata=copy.deepcopy(source_documents["store-meta.json"]),
            source_snapshot_digest=source_digest,
            conversion_digest=canonical_sha256(payload),
        )
        if snapshot_from_v4(conversion.semantic_source()).to_dict() != v3_snapshot.to_dict():
            raise V3ConversionError("SEMANTIC_PARITY_MISMATCH")
        return conversion
    except V3ConversionError:
        raise
    except (CanonicalJsonError, SemanticProjectionError, StorageContractError, KeyError, TypeError, ValueError) as error:
        raise V3ConversionError("INVALID_V3_SOURCE") from error
