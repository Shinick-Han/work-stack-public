"""Normalized (v4) record-tree validation.

Read-only. Walks ``records/<kind>/<bucket>/<uid>.json``, checks each record
against its schema and its own identity, then checks the semantics that only
the whole record set can answer: duplicate embedded uids, dangling
references, reply receipts, note kinds and relationship cycles.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .validation_primitives import (
    StorageValidationIssue,
    canonical_issues,
    list_reference_issues,
    read_json,
    reference_issue,
    relative,
    schema_issues,
)


RECORD_KINDS = {
    "captures": ("capture.schema.json", "workstack.capture"),
    "notes": ("note.schema.json", "workstack.note"),
    "objectives": ("objective.schema.json", "workstack.objective"),
    "replies": ("reply.schema.json", "workstack.reply"),
    "tasks": ("task.schema.json", "workstack.task"),
}
_UID_BUCKET = re.compile(r"^[0-9a-f]{2}$")


def _record_files(
    root: Path,
) -> tuple[list[tuple[str, Path, str]], list[StorageValidationIssue]]:
    records_root = root / "records"
    if not records_root.exists():
        return [], []
    if not records_root.is_dir():
        return [], [StorageValidationIssue("RECORD_LAYOUT_INVALID", "records")]
    records: list[tuple[str, Path, str]] = []
    issues: list[StorageValidationIssue] = []
    for kind_path in sorted(records_root.iterdir(), key=lambda item: item.name):
        if kind_path.name not in RECORD_KINDS:
            issues.append(StorageValidationIssue("UNKNOWN_RECORD_KIND", relative(kind_path, root)))
            continue
        if not kind_path.is_dir():
            issues.append(StorageValidationIssue("RECORD_LAYOUT_INVALID", relative(kind_path, root)))
            continue
        _collect_kind_records(root, kind_path, records, issues)
    return records, issues


def _collect_kind_records(
    root: Path,
    kind_path: Path,
    records: list[tuple[str, Path, str]],
    issues: list[StorageValidationIssue],
) -> None:
    for bucket_path in sorted(kind_path.iterdir(), key=lambda item: item.name):
        if not bucket_path.is_dir() or not _UID_BUCKET.fullmatch(bucket_path.name):
            issues.append(StorageValidationIssue("RECORD_BUCKET_INVALID", relative(bucket_path, root)))
            continue
        for record_path in sorted(bucket_path.iterdir(), key=lambda item: item.name):
            if not record_path.is_file() or record_path.suffix != ".json":
                issues.append(StorageValidationIssue("RECORD_LAYOUT_INVALID", relative(record_path, root)))
                continue
            records.append((kind_path.name, record_path, bucket_path.name))


def _validate_record(
    root: Path,
    kind: str,
    path: Path,
    bucket: str,
    workspace_uid: str | None,
    seen_uids: set[str],
    max_json_bytes: int,
) -> tuple[dict[str, Any] | None, list[StorageValidationIssue]]:
    artifact = relative(path, root)
    value, issues = read_json(path, root, max_json_bytes)
    if value is None:
        return None, issues
    schema_name, expected_format = RECORD_KINDS[kind]
    issues.extend(schema_issues(schema_name, value, artifact))
    issues.extend(canonical_issues(value, artifact))
    uid = value.get("uid")
    if value.get("format") != expected_format:
        issues.append(StorageValidationIssue("RECORD_KIND_MISMATCH", artifact))
    issues.extend(_record_uid_issues(uid, path, bucket, artifact, seen_uids))
    if workspace_uid is not None and value.get("workspace_uid") != workspace_uid:
        issues.append(StorageValidationIssue("WORKSPACE_UID_MISMATCH", artifact))
    return value, issues


def _record_uid_issues(
    uid: Any,
    path: Path,
    bucket: str,
    artifact: str,
    seen_uids: set[str],
) -> list[StorageValidationIssue]:
    if not isinstance(uid, str):
        return []
    issues: list[StorageValidationIssue] = []
    if path.name != uid + ".json" or bucket != uid[:2]:
        issues.append(StorageValidationIssue("UID_PATH_MISMATCH", artifact))
    if uid in seen_uids:
        issues.append(StorageValidationIssue("DUPLICATE_UID", artifact))
    seen_uids.add(uid)
    return issues


def _embedded_uid_issues(
    records: dict[str, dict[str, dict[str, Any]]],
    seen_uids: set[str],
) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    for kind, field in (("tasks", "subtasks"), ("objectives", "key_results")):
        for record in records[kind].values():
            artifact = str(record["_artifact"])
            for index, child in enumerate(record.get(field, [])):
                uid = child.get("uid") if isinstance(child, dict) else None
                if isinstance(uid, str) and uid in seen_uids:
                    issues.append(StorageValidationIssue("DUPLICATE_UID", artifact, f"/{field}/{index}/uid"))
                if isinstance(uid, str):
                    seen_uids.add(uid)
    return issues


def _task_reference_issues(
    task: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    objectives: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    artifact = str(task["_artifact"])
    issues = reference_issue(artifact, "/parent_uid", task.get("parent_uid"), tasks)
    issues.extend(list_reference_issues(artifact, "/dependency_uids", task.get("dependency_uids"), tasks))
    issues.extend(list_reference_issues(artifact, "/reference_uids", task.get("reference_uids"), tasks))
    issues.extend(list_reference_issues(artifact, "/objective_uids", task.get("objective_uids"), objectives))
    return issues


def _capture_reference_issues(
    capture: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> list[StorageValidationIssue]:
    artifact = str(capture["_artifact"])
    issues = list_reference_issues(artifact, "/linked_task_uids", capture.get("linked_task_uids"), tasks)
    issues.extend(
        list_reference_issues(artifact, "/converted_task_uids", capture.get("converted_task_uids"), tasks)
    )
    action_items = capture.get("normalized", {}).get("action_items", [])
    for index, item in enumerate(action_items):
        uid = item.get("task_uid") if isinstance(item, dict) else None
        issues.extend(reference_issue(artifact, f"/normalized/action_items/{index}/task_uid", uid, tasks))
    return issues


def _reply_semantic_issues(
    reply: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    captures: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    artifact = str(reply["_artifact"])
    issues = reference_issue(artifact, "/task_uid", reply.get("task_uid"), tasks)
    issues.extend(reference_issue(artifact, "/capture_uid", reply.get("capture_uid"), captures))
    capture = captures.get(reply.get("capture_uid"))
    if capture is not None and isinstance(reply.get("capture_revision"), int):
        if reply["capture_revision"] > capture.get("revision", -1):
            issues.append(StorageValidationIssue("REFERENCED_REVISION_MISSING", artifact, "/capture_revision"))
    expected_capability = {
        "microsoft-outlook": "outlook.reply",
        "microsoft-teams": "teams.reply",
    }.get(reply.get("provider"))
    if expected_capability is not None and reply.get("capability") != expected_capability:
        issues.append(StorageValidationIssue("REPLY_CAPABILITY_MISMATCH", artifact, "/capability"))
    issues.extend(_receipt_issues(reply, artifact))
    return issues


def _receipt_issues(reply: dict[str, Any], artifact: str) -> list[StorageValidationIssue]:
    receipt = reply.get("receipt")
    state = reply.get("state")
    if state == "approved" and receipt is not None:
        return [StorageValidationIssue("REPLY_RECEIPT_STATE_MISMATCH", artifact, "/receipt")]
    if state in {"sent", "failed", "unknown"} and not isinstance(receipt, dict):
        return [StorageValidationIssue("REPLY_RECEIPT_MISSING", artifact, "/receipt")]
    if not isinstance(receipt, dict):
        return []
    issues: list[StorageValidationIssue] = []
    compared = {
        "reply_display_id": "display_id",
        "provider": "provider",
        "outcome": "state",
        "body_digest": "body_digest",
        "target_digest": "target_digest",
    }
    for receipt_field, reply_field in compared.items():
        if receipt.get(receipt_field) != reply.get(reply_field):
            issues.append(StorageValidationIssue("REPLY_RECEIPT_MISMATCH", artifact, f"/receipt/{receipt_field}"))
    return issues


def _note_semantic_issues(
    note: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> list[StorageValidationIssue]:
    artifact = str(note["_artifact"])
    task_uid = note.get("task_uid")
    issues = reference_issue(artifact, "/task_uid", task_uid, tasks)
    if (note.get("note_kind") == "standalone") != (task_uid is None):
        issues.append(StorageValidationIssue("NOTE_KIND_MISMATCH", artifact, "/task_uid"))
    return issues


def _cycle_nodes(tasks: dict[str, dict[str, Any]], field: str) -> set[str]:
    graph: dict[str, list[str]] = {}
    for uid, task in tasks.items():
        raw = task.get(field)
        values = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
        graph[uid] = [value for value in values if value in tasks]
    stack: list[str] = []
    positions: dict[str, int] = {}
    visited: set[str] = set()
    cyclic: set[str] = set()

    def visit(uid: str) -> None:
        if uid in positions:
            cyclic.update(stack[positions[uid] :])
            return
        if uid in visited:
            return
        positions[uid] = len(stack)
        stack.append(uid)
        for target in graph[uid]:
            visit(target)
        stack.pop()
        positions.pop(uid)
        visited.add(uid)

    for uid in graph:
        visit(uid)
    return cyclic


def record_semantic_issues(
    records: dict[str, dict[str, dict[str, Any]]], seen_uids: set[str]
) -> list[StorageValidationIssue]:
    issues = _embedded_uid_issues(records, seen_uids)
    tasks = records["tasks"]
    objectives = records["objectives"]
    captures = records["captures"]
    for task in tasks.values():
        issues.extend(_task_reference_issues(task, tasks, objectives))
    for capture in captures.values():
        issues.extend(_capture_reference_issues(capture, tasks))
    for reply in records["replies"].values():
        issues.extend(_reply_semantic_issues(reply, tasks, captures))
    for note in records["notes"].values():
        issues.extend(_note_semantic_issues(note, tasks))
    for field in ("parent_uid", "dependency_uids"):
        for uid in _cycle_nodes(tasks, field):
            issues.append(StorageValidationIssue("TASK_RELATIONSHIP_CYCLE", str(tasks[uid]["_artifact"]), f"/{field}"))
    return issues


def load_v4_records(
    root: Path,
    workspace_uid: str | None,
    seen_uids: set[str],
    max_json_bytes: int,
) -> tuple[dict[str, dict[str, dict[str, Any]]], int, list[StorageValidationIssue]]:
    records = {kind: {} for kind in RECORD_KINDS}
    paths, issues = _record_files(root)
    for kind, path, bucket in paths:
        value, record_issues = _validate_record(
            root, kind, path, bucket, workspace_uid, seen_uids, max_json_bytes
        )
        issues.extend(record_issues)
        if value is not None and isinstance(value.get("uid"), str):
            value["_artifact"] = relative(path, root)
            records[kind][value["uid"]] = value
    return records, len(paths), issues
