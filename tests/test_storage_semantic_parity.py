from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from workstack.service import WorkStack
from workstack.store import Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.contracts import require_valid_by_format
from workstack.storage.reader import read_v4
from workstack.storage.repository import V4ReadOnlyRepository
from workstack.storage.semantic import (
    SemanticProjectionError,
    V4SemanticSource,
    semantic_source_from_v4_read,
    snapshot_from_v3_documents,
    snapshot_from_v4,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "store-v3"
EXPECTED_DIGEST = "sha256:cca698f3d4137f0f4220eaa22102c6625a1e0de439cea364e9c0bcca0f15b36f"


def _load(name: str) -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURES / name).glob("*.json"))
    }


def _uid(namespace: str, name: str) -> str:
    return str(uuid.uuid5(uuid.UUID(namespace), name))


def _record_envelope(format_name: str, workspace_uid: str, uid: str, source: dict) -> dict:
    return {
        "format": format_name,
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "uid": uid,
        "revision": source.get("revision", 0),
        "created_at": source.get("created", source.get("created_at")),
        "updated_at": source.get("updated_at", source.get("created", source.get("created_at"))),
    }


def _event_envelope(
    format_name: str, workspace_uid: str, event_uid: str, sequence: int,
    record_uid: str | None, created_at: str, actor: str | None, provenance: str,
) -> dict:
    return {
        "format": format_name, "schema_version": 1, "workspace_uid": workspace_uid,
        "event_uid": event_uid, "sequence": sequence, "record_uid": record_uid,
        "created_at": created_at, "actor": actor, "provenance": provenance,
    }


def _v4_candidate(documents: dict[str, dict]) -> V4SemanticSource:
    workspace = documents["workspace.json"]
    workspace_uid = workspace["id"]
    task_uid_by_id = {item["id"]: item["uid"] for item in documents["backlog.json"]["tasks"]}
    objective_uid_by_id = {
        item["id"]: _uid(workspace_uid, f"objective:{item['id']}")
        for item in documents["okr.json"]["objectives"]
    }
    capture_uid_by_id = {
        item["id"]: _uid(workspace_uid, f"capture:{item['id']}")
        for item in documents["captures.json"]["captures"]
    }
    reply_uid_by_id = {
        item["id"]: _uid(workspace_uid, f"reply:{item['id']}")
        for item in documents["replies.json"]["replies"]
    }
    workspace_record = {
        **_record_envelope("workstack.workspace", workspace_uid, workspace_uid, {}),
        "display_id": workspace_uid,
        "name": workspace["name"],
    }

    objectives = []
    for source in documents["okr.json"]["objectives"]:
        objective_uid = objective_uid_by_id[source["id"]]
        objectives.append({
            **_record_envelope("workstack.objective", workspace_uid, objective_uid, source),
            "display_id": source["id"], "title": source["objective"],
            "status": source["status"], "quarter": source["quarter"],
            "revision_origin": "explicit" if "revision" in source else "legacy_missing",
            "key_results": [{
                "uid": _uid(objective_uid, f"key-result:{child['id']}"),
                "display_id": child["id"], "title": child["text"],
                "status": child["status"], "target": child["target"],
                "progress": child["progress"],
            } for child in source["key_results"]],
        })

    task_note_indexes: dict[str, int] = {}
    notes = []
    tasks = []
    for source in documents["backlog.json"]["tasks"]:
        task_uid = source["uid"]
        record = {
            **_record_envelope("workstack.task", workspace_uid, task_uid, source),
            "display_id": source["id"], "title": source["title"], "detail": source["detail"],
            "priority": source["priority"], "due": source["due"], "scheduled": source["scheduled"],
            "estimate_minutes": source["estimate_minutes"], "tags": copy.deepcopy(source["tags"]),
            "objective_uids": [objective_uid_by_id[item] for item in source["objective_ids"]],
            "parent_uid": None if source["parent_id"] is None else task_uid_by_id[source["parent_id"]],
            "dependency_uids": [task_uid_by_id[item] for item in source["dependencies"]],
            "reference_uids": [],
            "subtasks": [{
                "uid": _uid(task_uid, f"subtask:{child['id']}"),
                "display_id": child["id"], "title": child["title"],
                "priority": child["priority"], "status": child["status"],
            } for child in source["subtasks"]],
        }
        tasks.append(record)
        for index, child in enumerate(source["notes"]):
            note_uid = _uid(task_uid, f"task-note:{index}")
            task_note_indexes[note_uid] = index
            notes.append({
                **_record_envelope("workstack.note", workspace_uid, note_uid, {
                    "created": child["date"], "updated_at": child["date"],
                }),
                "display_id": f"N-{9000 + len(notes):04d}", "note_kind": "task_annotation",
                "task_uid": task_uid, "text": child["text"], "links": [], "created_by": "migration.v3",
            })

    for source in documents["notes.json"]["notes"]:
        note_uid = _uid(workspace_uid, f"note:{source['id']}")
        notes.append({
            **_record_envelope("workstack.note", workspace_uid, note_uid, source),
            "display_id": source["id"], "note_kind": "standalone", "task_uid": None,
            "text": source["text"], "links": copy.deepcopy(source["links"]),
            "created_by": "migration.v3",
        })

    captures = []
    for source in documents["captures.json"]["captures"]:
        normalized = copy.deepcopy(source["normalized"])
        for item in normalized["action_items"]:
            display_id = item.pop("task_id", None)
            item["task_display_id"] = display_id
            item["task_uid"] = None if display_id is None else task_uid_by_id[display_id]
        captures.append({
            **_record_envelope("workstack.capture", workspace_uid, capture_uid_by_id[source["id"]], source),
            "display_id": source["id"], "capture_packet_schema_version": source["schema_version"],
            "status": source["status"], "source_key": source["source_key"],
            "source": copy.deepcopy(source["source"]), "normalized": normalized,
            "task_hints": copy.deepcopy(source["task_hints"]), "provenance": copy.deepcopy(source["provenance"]),
            "linked_task_uids": [task_uid_by_id[item] for item in source["linked_task_ids"]],
            "converted_task_uids": [task_uid_by_id[item] for item in source["converted_task_ids"]],
            "recent_revisions": copy.deepcopy(source["recent_revisions"]),
        })

    replies = []
    for source in documents["replies.json"]["replies"]:
        receipt = copy.deepcopy(source["receipt"])
        if receipt is not None:
            receipt["reply_display_id"] = receipt.pop("reply_id")
        replies.append({
            **_record_envelope("workstack.reply", workspace_uid, reply_uid_by_id[source["id"]], source),
            "display_id": source["id"], "task_uid": task_uid_by_id[source["task_id"]],
            "capture_uid": capture_uid_by_id[source["capture_id"]],
            "capture_revision": source["capture_revision"], "provider": source["provider"],
            "capability": source["capability"], "target": copy.deepcopy(source["target"]),
            "body": source["body"], "body_digest": source["body_digest"],
            "target_digest": source["target_digest"], "state": source["state"],
            "approved_at": source["approved_at"], "receipt": receipt,
        })

    planning_events = []
    fact_uid_by_id: dict[str, str] = {}
    for sequence, source in enumerate(documents["activity.json"]["planning_status"], 1):
        event_uid = _uid(workspace_uid, f"planning-status:{source['id']}")
        fact_uid_by_id[source["id"]] = event_uid
        planning_events.append({
            **_event_envelope("workstack.planning-status-event", workspace_uid, event_uid, sequence,
                              source["task_uid"], source["created_at"], source["actor"], source["provenance"]),
            "legacy_fact_id": source["id"], "task_uid": source["task_uid"],
            "task_display_id": source["task_id"],
            "previous_event_uid": None if source["previous_fact_id"] is None else fact_uid_by_id[source["previous_fact_id"]],
            "previous_legacy_fact_id": source["previous_fact_id"], "prior_revision": source["prior_revision"],
            "new_revision": source["new_revision"], "prior_status": source["prior_status"],
            "status": source["status"],
        })

    activity_events = []
    for sequence, source in enumerate(documents["activity.json"]["activity"], 1):
        activity_events.append({
            **_event_envelope("workstack.activity-event", workspace_uid,
                              _uid(workspace_uid, f"activity:{source['id']}"), sequence, None,
                              source["created_at"], None, "migration.v3"),
            "legacy_event_id": source["id"], "event_type": source["type"],
            "details": copy.deepcopy(source["details"]),
            "capture_uid": capture_uid_by_id.get(source.get("capture_id")),
            "task_uid": task_uid_by_id.get(source.get("task_id")),
            "reply_uid": reply_uid_by_id.get(source.get("reply_id")),
        })

    worklog_events = []
    sequence = 0
    for date, day in sorted(documents["worklog.json"]["days"].items()):
        if "start_time" in day:
            sequence += 1
            worklog_events.append({
                **_event_envelope("workstack.worklog-event", workspace_uid,
                                  _uid(workspace_uid, f"worklog:{date}:check-in"), sequence,
                                  None, date, "local.user", "migration.v3"),
                "kind": "check-in", "work_date": date, "start_time": day["start_time"],
            })
        for index, source in enumerate(day.get("entries", [])):
            sequence += 1
            event = {
                **_event_envelope("workstack.worklog-event", workspace_uid,
                                  _uid(workspace_uid, f"worklog:{date}:entry:{index}"), sequence,
                                  task_uid_by_id[source["task_id"]], date, "local.user", "migration.v3"),
                "kind": "entry", "work_date": date, "task_uid": task_uid_by_id[source["task_id"]],
                "task_display_id": source["task_id"], "task_title": source["task"],
                "done": copy.deepcopy(source["done"]), "next": copy.deepcopy(source["next"]),
                "blockers": copy.deepcopy(source["blockers"]),
            }
            for optional in ("session_id", "duration_seconds"):
                if optional in source:
                    event[optional] = source[optional]
            worklog_events.append(event)
        for source in day.get("sessions", []):
            sequence += 1
            worklog_events.append({
                **_event_envelope("workstack.worklog-event", workspace_uid,
                                  _uid(workspace_uid, f"worklog:{date}:session:{source['id']}"), sequence,
                                  task_uid_by_id[source["task_id"]], source["updated_at"],
                                  "local.user", "migration.v3"),
                "kind": "session", "session_id": source["id"], "work_date": date,
                "task_uid": task_uid_by_id[source["task_id"]], "task_display_id": source["task_id"],
                "task_title": source["task"], "state": source["state"],
                "started_at": source["started_at"], "updated_at": source["updated_at"],
                "segments": copy.deepcopy(source["segments"]), "worklog_state": source["worklog_state"],
            })

    for record in [workspace_record, *tasks, *objectives, *captures, *replies, *notes,
                   *planning_events, *activity_events, *worklog_events]:
        require_valid_by_format(record)
    return V4SemanticSource(
        workspace=workspace_record, tasks=tasks, objectives=objectives, captures=captures,
        replies=replies, notes=notes, planning_events=planning_events,
        activity_events=activity_events, worklog_events=worklog_events,
        idempotency_records=documents["activity.json"]["idempotency"],
        task_note_source_indexes=task_note_indexes,
    )


def _write_v4_candidate(root: Path, source: V4SemanticSource) -> None:
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(value))

    workspace_uid = source.workspace["workspace_uid"]
    write_json(
        root / "store.json",
        {
            "format": "workstack.ssot",
            "schema_version": 4,
            "schema_set": "workstack.ssot.v4",
            "workspace_uid": workspace_uid,
            "created_at": "2026-09-01T00:00:00Z",
        },
    )
    write_json(root / "workspace.json", source.workspace)
    record_groups = {
        "tasks": source.tasks,
        "objectives": source.objectives,
        "captures": source.captures,
        "replies": source.replies,
        "notes": source.notes,
    }
    for kind, records in record_groups.items():
        for record in records:
            uid = record["uid"]
            write_json(root / "records" / kind / uid[:2] / f"{uid}.json", record)

    stream_groups = {
        "planning-status": source.planning_events,
        "activity": source.activity_events,
        "worklog": source.worklog_events,
    }
    ordered = sorted(
        (
            (event["created_at"], kind, event["sequence"], event["event_uid"], event)
            for kind, events in stream_groups.items()
            for event in events
        ),
    )
    segments: dict[tuple[str, str], list[dict]] = {}
    for sequence, (created_at, kind, _old_sequence, _event_uid, event) in enumerate(ordered, 1):
        migrated = {**event, "sequence": sequence}
        segments.setdefault((kind, created_at[:7]), []).append(migrated)
    for (kind, month), events in sorted(segments.items()):
        path = root / "streams" / kind / f"{month}.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(canonical_json_bytes(event) + b"\n" for event in events))


class StorageSemanticParityTest(unittest.TestCase):
    def test_populated_v3_and_schema_valid_v4_have_identical_semantics(self) -> None:
        documents = _load("populated")
        v3 = snapshot_from_v3_documents(documents)
        v4 = snapshot_from_v4(_v4_candidate(documents))

        self.assertEqual(v3.to_dict(), v4.to_dict())
        self.assertEqual(v3.digest, EXPECTED_DIGEST)
        self.assertEqual(v4.digest, EXPECTED_DIGEST)
        self.assertEqual(v3.relation_edges(), v4.relation_edges())
        self.assertEqual(v3.search_documents(), v4.search_documents())

    def test_empty_v3_and_v4_are_equivalent(self) -> None:
        documents = _load("empty")
        self.assertEqual(
            snapshot_from_v3_documents(documents).to_dict(),
            snapshot_from_v4(_v4_candidate(documents)).to_dict(),
        )

    def test_input_order_does_not_change_v4_projection(self) -> None:
        source = _v4_candidate(_load("populated"))
        reversed_source = V4SemanticSource(
            **{**source.__dict__, "tasks": tuple(reversed(source.tasks)),
               "planning_events": tuple(reversed(source.planning_events)),
               "activity_events": tuple(reversed(source.activity_events))}
        )
        self.assertEqual(snapshot_from_v4(source).digest, snapshot_from_v4(reversed_source).digest)

    def test_task_annotation_requires_receipt_source_index(self) -> None:
        source = _v4_candidate(_load("populated"))
        without_roster = V4SemanticSource(**{**source.__dict__, "task_note_source_indexes": {}})
        with self.assertRaisesRegex(SemanticProjectionError, "TASK_NOTE_SOURCE_INDEX_REQUIRED"):
            snapshot_from_v4(without_roster)

    def test_physical_reader_repository_input_preserves_populated_semantics(self) -> None:
        documents = _load("populated")
        candidate = _v4_candidate(documents)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            v3_root = base / "v3"
            v4_root = base / "v4"
            shutil.copytree(FIXTURES / "populated", v3_root)
            _write_v4_candidate(v4_root, candidate)
            read_result = read_v4(v4_root)
            physical = semantic_source_from_v4_read(
                read_result,
                idempotency_records=candidate.idempotency_records,
                task_note_source_indexes=candidate.task_note_source_indexes,
            )
            self.assertEqual(
                snapshot_from_v3_documents(documents).to_dict(),
                snapshot_from_v4(physical).to_dict(),
            )

            v3_application = WorkStack(Store(v3_root))
            repository = V4ReadOnlyRepository(v4_root, read_result)
            v4_application = WorkStack(
                repository.legacy_store(
                    idempotency_records=candidate.idempotency_records,
                    task_note_source_indexes=candidate.task_note_source_indexes,
                ),
                initialize=False,
            )
            self.assertEqual(
                v3_application.workspace_projection(),
                v4_application.workspace_projection(),
            )
            review_date = sorted(documents["worklog.json"]["days"])[-1]
            self.assertEqual(
                v3_application.review_projection(review_date),
                v4_application.review_projection(review_date),
            )
            self.assertEqual(
                v3_application.search_projection("deployment"),
                v4_application.search_projection("deployment"),
            )


if __name__ == "__main__":
    unittest.main()
