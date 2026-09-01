"""Command-line interface for the portable work-stack."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

from .server import serve
from .service import DomainError, WorkStack
from .maintenance import backup_store, relocate_store, restore_store, verify_backup
from .snapshot_export import write_snapshot_file
from .store import Store
from .storage.migration import (
    StorageMigrationError,
    execute_v3_migration,
    load_migration_receipt,
    plan_v3_migration,
    preview_v3_migration,
    resume_v3_migration,
    verify_v3_migration_artifacts,
)
from .storage.validation import validate_storage_path
from .storage.v4_backup import (
    V4BackupError,
    restore_v4_backup,
    verify_v4_backup,
    write_v4_backup,
)


PROJECT_DATA = Path(__file__).resolve().parents[1] / "data"
AGENT_APPLY_LIMIT = 32 * 1024
AGENT_TASK_FIELDS = frozenset({
    "title", "detail", "status", "priority", "due", "scheduled",
    "estimate_minutes", "tags", "objective_ids", "parent_id", "dependencies",
})


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="work-stack")
    root.add_argument("--data-dir", help="override the local JSON data directory")
    sub = root.add_subparsers(dest="domain", required=True)

    backlog = sub.add_parser("backlog", help="manage projects and tasks")
    backlog_sub = backlog.add_subparsers(dest="action", required=True)
    add = backlog_sub.add_parser("add")
    add.add_argument("title")
    add.add_argument("--detail", default="")
    add.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")
    add.add_argument("--due")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--objective", action="append", default=[])
    add.add_argument("--parent")
    add.add_argument("--depends-on", action="append", default=[])
    listing = backlog_sub.add_parser("list")
    listing.add_argument("--status", default="active")
    show = backlog_sub.add_parser("show")
    show.add_argument("id")
    for action in ("start", "done", "drop", "reopen"):
        command = backlog_sub.add_parser(action)
        command.add_argument("id")
    note = backlog_sub.add_parser("note")
    note.add_argument("id")
    note.add_argument("text")
    subtask = backlog_sub.add_parser("subtask")
    subtask.add_argument("operation", choices=("add", "start", "done", "drop", "reopen"))
    subtask.add_argument("task")
    subtask.add_argument("subtask_or_title")
    subtask.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")

    okr = sub.add_parser("okr", help="manage objectives and key results")
    okr_sub = okr.add_subparsers(dest="action", required=True)
    add_objective = okr_sub.add_parser("add-objective")
    add_objective.add_argument("text")
    add_objective.add_argument("--quarter")
    add_key_result = okr_sub.add_parser("add-key-result")
    add_key_result.add_argument("objective")
    add_key_result.add_argument("text")
    add_key_result.add_argument("--target", default="")
    okr_list = okr_sub.add_parser("list")
    okr_list.add_argument("--status", default="active")
    link = okr_sub.add_parser("link")
    link.add_argument("objective")
    link.add_argument("task")
    progress = okr_sub.add_parser("progress")
    progress.add_argument("objective")
    progress.add_argument("key_result")
    progress.add_argument("value", type=int)
    okr_sub.add_parser("rollup")

    worklog = sub.add_parser("worklog", help="record daily progress")
    worklog_sub = worklog.add_subparsers(dest="action", required=True)
    checkin = worklog_sub.add_parser("checkin")
    checkin.add_argument("--time")
    checkin.add_argument("--date")
    worklog_add = worklog_sub.add_parser("add")
    worklog_add.add_argument("task")
    worklog_add.add_argument("--done", action="append", default=[])
    worklog_add.add_argument("--next", dest="next_items", action="append", default=[])
    worklog_add.add_argument("--blocker", action="append", default=[])
    worklog_add.add_argument("--date")
    worklog_list = worklog_sub.add_parser("list")
    worklog_list.add_argument("--date")

    weekly = sub.add_parser("weekly", help="aggregate daily records")
    weekly.add_argument("--end")
    weekly.add_argument("--days", type=int, default=7)

    notes = sub.add_parser("note", help="add a graph note")
    notes.add_argument("text")
    notes.add_argument("--link", action="append", default=[])

    capture = sub.add_parser("capture", help="send sanitized Capture Packet v1 data")
    capture_sub = capture.add_subparsers(dest="action", required=True)
    ingest = capture_sub.add_parser("ingest")
    ingest.add_argument("--stdin", action="store_true", required=True)
    ingest.add_argument("--idempotency-key")

    agent = sub.add_parser(
        "agent",
        help="apply one revision-guarded agent update without editing Store files",
    )
    agent_sub = agent.add_subparsers(dest="action", required=True)
    agent_apply = agent_sub.add_parser("apply")
    agent_apply.add_argument("--stdin", action="store_true", required=True)
    agent_apply.add_argument("--intent-id", required=True)

    snapshot = sub.add_parser("snapshot", help="review and export one planning snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="action", required=True)
    snapshot_preview = snapshot_sub.add_parser("preview")
    snapshot_preview.add_argument("task")
    snapshot_export = snapshot_sub.add_parser("export")
    snapshot_export.add_argument("task")
    snapshot_export.add_argument("--out", required=True)
    snapshot_export.add_argument("--expected-revision", required=True, type=int)
    snapshot_export.add_argument("--expected-digest", required=True)
    snapshot_export.add_argument("--confirm-disclosure", action="store_true")

    storage = sub.add_parser("storage", help="inspect a local SSOT without modifying it")
    storage_sub = storage.add_subparsers(dest="action", required=True)
    storage_validate = storage_sub.add_parser("validate")
    storage_validate.add_argument("path", help="candidate v3 or v4 SSOT directory")
    migration = storage_sub.add_parser("migration", help="plan or verify an explicit v3-to-v4 copy")
    migration_sub = migration.add_subparsers(dest="migration_action", required=True)
    migration_plan = migration_sub.add_parser("plan")
    migration_plan.add_argument("source")
    migration_plan.add_argument("--candidate")
    migration_plan.add_argument("--backup")
    migration_preview = migration_sub.add_parser("preview")
    migration_preview.add_argument("source")
    migration_preview.add_argument("--candidate-created-at", required=True)
    migration_preview.add_argument("--candidate")
    migration_preview.add_argument("--backup")
    migration_execute = migration_sub.add_parser("execute")
    migration_execute.add_argument("source")
    migration_execute.add_argument("--candidate-created-at", required=True)
    migration_execute.add_argument("--candidate")
    migration_execute.add_argument("--backup")
    migration_execute.add_argument("--expected-source-digest", required=True)
    migration_execute.add_argument("--expected-conversion-digest", required=True)
    migration_verify = migration_sub.add_parser("verify")
    migration_verify.add_argument("--source", required=True)
    migration_verify.add_argument("--candidate", required=True)
    migration_verify.add_argument("--backup", required=True)
    migration_verify.add_argument("--receipt", required=True)
    migration_receipt = migration_sub.add_parser("receipt")
    migration_receipt.add_argument("path")
    migration_resume = migration_sub.add_parser("resume")
    migration_resume.add_argument("source")
    migration_resume.add_argument("--candidate-created-at", required=True)
    migration_resume.add_argument("--candidate", required=True)
    migration_resume.add_argument("--backup", required=True)
    migration_resume.add_argument("--expected-source-digest", required=True)
    migration_resume.add_argument("--expected-conversion-digest", required=True)
    v4_backup = storage_sub.add_parser(
        "v4-backup", help="explicitly create, verify, or restore an inactive v4 authority"
    )
    v4_backup_sub = v4_backup.add_subparsers(dest="v4_backup_action", required=True)
    v4_backup_create = v4_backup_sub.add_parser("create")
    v4_backup_create.add_argument("source")
    v4_backup_create.add_argument("--out", required=True)
    v4_backup_verify = v4_backup_sub.add_parser("verify")
    v4_backup_verify.add_argument("archive")
    v4_backup_restore = v4_backup_sub.add_parser("restore")
    v4_backup_restore.add_argument("archive")
    v4_backup_restore.add_argument("--to", required=True)

    maintenance = sub.add_parser("maintenance", help="verify, back up, restore, or relocate local data")
    maintenance_sub = maintenance.add_subparsers(dest="action", required=True)
    backup = maintenance_sub.add_parser("backup")
    backup.add_argument("--out", required=True, help="backup directory")
    verify = maintenance_sub.add_parser("verify")
    verify.add_argument("archive")
    restore = maintenance_sub.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--to", required=True, help="destination data directory")
    restore.add_argument("--replace", action="store_true")
    restore.add_argument("--safety-backups", help="required when replacing an existing store")
    relocate = maintenance_sub.add_parser("relocate")
    relocate.add_argument("--to", required=True, help="empty destination data directory")

    graph = sub.add_parser("graph", help="export or serve the web dashboard")
    graph_sub = graph.add_subparsers(dest="action", required=True)
    export = graph_sub.add_parser("export")
    export.add_argument("--out", default="graph-data.json")
    server = graph_sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument(
        "--public-port",
        type=int,
        help="additional loopback Host port accepted behind a local SSH forward",
    )
    server.add_argument(
        "--exit-with-parent",
        action="store_true",
        help="on Linux, stop the server when its SSH session parent exits",
    )
    server.add_argument(
        "--seed-demo",
        action="store_true",
        help="copy tracked demo fixtures only when runtime data is empty",
    )
    return root


def forward_capture(store: Store, raw: bytes, idempotency_key: str | None) -> int:
    if len(raw) > 64 * 1024:
        raise ValueError("capture packet exceeds 64 KiB")
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stdin must contain one UTF-8 JSON object") from error
    if not isinstance(packet, dict):
        raise ValueError("stdin must contain one JSON object")
    canonical = json.dumps(
        packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    outgoing = canonical
    key = idempotency_key or ("cli:" + hashlib.sha256(canonical).hexdigest())
    if not store.server_info_path.is_file() or not store.capture_token_path.is_file():
        raise OSError("Work Stack server is not running for this data directory")
    try:
        info = json.loads(store.server_info_path.read_text(encoding="utf-8"))
        token = store.capture_token_path.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError("Work Stack server runtime metadata is invalid") from error
    if (
        not isinstance(info, dict)
        or info.get("version") != 1
        or info.get("host") not in ("127.0.0.1", "::1", "localhost")
        or not isinstance(info.get("port"), int)
        or not 1 <= info["port"] <= 65535
        or not token
    ):
        raise OSError("Work Stack server runtime metadata is invalid")
    connection = http.client.HTTPConnection(info["host"], info["port"], timeout=10)
    try:
        connection.request(
            "POST",
            "/api/v1/captures",
            body=outgoing,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        )
        response = connection.getresponse()
        status = response.status
        result = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
    emit(result)
    return 0 if 200 <= status < 300 else 2


def _agent_apply_packet(raw: bytes) -> dict[str, object]:
    if len(raw) > AGENT_APPLY_LIMIT:
        raise ValueError("agent apply packet exceeds 32 KiB")
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stdin must contain one UTF-8 JSON object") from error
    if not isinstance(packet, dict) or set(packet) != {
        "workspace_id", "task_id", "expected_revision", "changes"
    }:
        raise ValueError(
            "agent apply requires only workspace_id, task_id, expected_revision, and changes"
        )
    workspace_id = packet["workspace_id"]
    try:
        parsed_workspace_id = uuid.UUID(str(workspace_id))
    except (ValueError, AttributeError) as error:
        raise ValueError("workspace_id must be a canonical UUID") from error
    if str(parsed_workspace_id) != workspace_id or parsed_workspace_id.int == 0:
        raise ValueError("workspace_id must be a canonical non-nil UUID")
    task_id = packet["task_id"]
    if not isinstance(task_id, str) or re.fullmatch(r"T-[0-9]{4,}", task_id) is None:
        raise ValueError("task_id must be a canonical Work Stack Task ID")
    expected_revision = packet["expected_revision"]
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    changes = packet["changes"]
    if (
        not isinstance(changes, dict)
        or not changes
        or not set(changes) <= AGENT_TASK_FIELDS
    ):
        raise ValueError("changes must contain only supported mutable Task fields")
    return packet


def _server_coordinates(store: Store) -> tuple[str, int] | None:
    if not store.server_info_path.is_file():
        return None
    try:
        info = json.loads(store.server_info_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError("Work Stack server runtime metadata is invalid") from error
    if (
        not isinstance(info, dict)
        or info.get("version") != 1
        or info.get("host") not in ("127.0.0.1", "::1", "localhost")
        or not isinstance(info.get("port"), int)
        or isinstance(info.get("port"), bool)
        or not 1 <= info["port"] <= 65535
    ):
        raise OSError("Work Stack server runtime metadata is invalid")
    return info["host"], info["port"]


def _request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    outgoing = None
    request_headers = dict(headers or {})
    if body is not None:
        outgoing = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(method, path, body=outgoing, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OSError("Work Stack server returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise OSError("Work Stack server returned an invalid response")
        return response.status, payload
    finally:
        connection.close()


def _task_from_detail(payload: dict[str, object]) -> dict[str, object] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    task = data.get("task", data)
    return task if isinstance(task, dict) else None


def _matches_agent_result(
    task: dict[str, object], expected_revision: int, changes: dict[str, object]
) -> bool:
    return task.get("revision") == expected_revision + 1 and all(
        task.get(field) == value for field, value in changes.items()
    )


def _forward_agent_apply(
    store: Store,
    packet: dict[str, object],
    intent_id: str,
    coordinates: tuple[str, int],
) -> int:
    host, port = coordinates
    status, session = _request_json(host, port, "GET", "/api/v1/session")
    session_data = session.get("data")
    if (
        status != 200
        or not isinstance(session_data, dict)
        or not isinstance(session_data.get("csrf_token"), str)
    ):
        raise OSError("Work Stack server session could not be established")
    status, storage = _request_json(host, port, "GET", "/api/v1/storage")
    storage_data = storage.get("data")
    if status != 200 or not isinstance(storage_data, dict):
        raise OSError("Work Stack storage identity could not be verified")
    if storage_data.get("workspace_id") != packet["workspace_id"]:
        raise ValueError("agent apply workspace_id does not match the running server")
    origin_host = "[{}]".format(host) if ":" in host else host
    headers = {
        "Origin": "http://{}:{}".format(origin_host, port),
        "X-WorkStack-CSRF": session_data["csrf_token"],
        "X-WorkStack-Agent-Intent": intent_id,
    }
    task_id = str(packet["task_id"])
    expected_revision = int(packet["expected_revision"])
    changes = dict(packet["changes"])  # validated by _agent_apply_packet
    request_body = {**changes, "revision": expected_revision}
    path = "/api/v1/tasks/{}".format(quote(task_id, safe=""))
    try:
        status, result = _request_json(
            host, port, "PATCH", path, body=request_body, headers=headers
        )
    except (OSError, TimeoutError):
        # A lost response is commit-unknown. Reread once and accept only an exact
        # next-revision match; never replay the mutation automatically.
        verify_status, verified = _request_json(host, port, "GET", path)
        task = _task_from_detail(verified) if verify_status == 200 else None
        if task is not None and _matches_agent_result(task, expected_revision, changes):
            emit({
                "data": task,
                "meta": {
                    "intent_id": intent_id,
                    "mode": "running-server",
                    "verified_after_transport_loss": True,
                },
            })
            return 0
        raise OSError(
            "agent apply commit is unknown; inspect the Task revision before retrying"
        )
    if 200 <= status < 300:
        task = _task_from_detail(result)
        if task is None:
            raise OSError("Work Stack server returned an invalid Task response")
        emit({
            "data": task,
            "meta": {
                "intent_id": intent_id,
                "mode": "running-server",
                "verified_after_transport_loss": False,
            },
        })
        return 0
    emit(result)
    return 2


def apply_agent_update(store: Store, raw: bytes, intent_id: str) -> int:
    if re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", intent_id) is None:
        raise ValueError("intent_id must be 8-128 safe identifier characters")
    packet = _agent_apply_packet(raw)
    coordinates = _server_coordinates(store)
    if coordinates is not None:
        return _forward_agent_apply(store, packet, intent_id, coordinates)

    stack = WorkStack(store)
    if store.load("workspace.json")["id"] != packet["workspace_id"]:
        raise ValueError("agent apply workspace_id does not match this Store")
    task = stack.patch_task(
        str(packet["task_id"]),
        {**dict(packet["changes"]), "revision": packet["expected_revision"]},
    )
    emit({
        "data": task,
        "meta": {
            "intent_id": intent_id,
            "mode": "exclusive-local-store",
            "verified_after_transport_loss": False,
        },
    })
    return 0


def _backup_payload(artifact: object) -> dict[str, object]:
    return {
        "path": str(artifact.path),
        "workspace_id": artifact.workspace_id,
        "created_at": artifact.created_at,
        "digest": artifact.digest,
        "file_count": artifact.file_count,
    }


def _run_maintenance(arguments: argparse.Namespace, store: Store) -> None:
    if arguments.action == "backup":
        emit(_backup_payload(backup_store(store.root, arguments.out)))
        return
    if arguments.action == "verify":
        emit(_backup_payload(verify_backup(arguments.archive)))
        return
    if arguments.action == "restore":
        receipt = restore_store(
            arguments.archive,
            arguments.to,
            replace=arguments.replace,
            safety_backup_dir=arguments.safety_backups,
        )
        emit({
            "destination": str(receipt.destination),
            "workspace_id": receipt.workspace_id,
            "backup_digest": receipt.backup_digest,
            "safety_backup": str(receipt.safety_backup) if receipt.safety_backup else None,
        })
        return
    receipt = relocate_store(store.root, arguments.to)
    emit({
        "destination": str(receipt.destination),
        "workspace_id": receipt.workspace_id,
        "backup_digest": receipt.backup_digest,
        "source_preserved": True,
    })


def _migration_paths_payload(value: object) -> dict[str, object]:
    paths = value.paths
    return {
        "source_path": str(paths.source_root),
        "candidate_path": str(paths.candidate_root),
        "backup_path": str(paths.backup_path),
    }


def _run_storage_migration(arguments: argparse.Namespace) -> int:
    action = arguments.migration_action
    if action == "plan":
        plan = plan_v3_migration(
            arguments.source,
            candidate_override=arguments.candidate,
            backup_override=arguments.backup,
        )
        emit({
            "status": "planned",
            **_migration_paths_payload(plan),
            "source_digest": plan.frozen.aggregate_digest,
            "source_file_count": len(plan.frozen.artifacts),
            "activated": False,
        })
        return 0
    if action == "preview":
        preview = preview_v3_migration(
            arguments.source,
            candidate_created_at=arguments.candidate_created_at,
            candidate_override=arguments.candidate,
            backup_override=arguments.backup,
        )
        emit({
            "status": "previewed",
            **_migration_paths_payload(preview),
            "receipt_path": str(preview.receipt_path),
            "source_digest": preview.frozen.aggregate_digest,
            "source_semantic_digest": preview.conversion.source_snapshot_digest,
            "conversion_digest": preview.conversion.conversion_digest,
            "record_count": sum(len(items) for items in preview.conversion.records.values()),
            "stream_event_count": sum(len(items) for items in preview.conversion.streams.values()),
            "activated": False,
        })
        return 0
    if action == "execute":
        execution = execute_v3_migration(
            arguments.source,
            candidate_created_at=arguments.candidate_created_at,
            candidate_override=arguments.candidate,
            backup_override=arguments.backup,
            expected_source_digest=arguments.expected_source_digest,
            expected_conversion_digest=arguments.expected_conversion_digest,
        )
        emit({
            "status": "verified_candidate",
            **_migration_paths_payload(execution.preview),
            "receipt_path": str(execution.receipt_path),
            "source_digest": execution.preview.frozen.aggregate_digest,
            "conversion_digest": execution.preview.conversion.conversion_digest,
            "candidate_digest": execution.candidate_manifest.digest,
            "backup_digest": execution.backup.archive_digest,
            "source_unchanged": True,
            "activated": False,
        })
        return 0
    if action == "resume":
        execution = resume_v3_migration(
            arguments.source,
            candidate_created_at=arguments.candidate_created_at,
            candidate_path=arguments.candidate,
            backup_path=arguments.backup,
            expected_source_digest=arguments.expected_source_digest,
            expected_conversion_digest=arguments.expected_conversion_digest,
        )
        emit({
            "status": "verified_candidate",
            **_migration_paths_payload(execution.preview),
            "receipt_path": str(execution.receipt_path),
            "candidate_digest": execution.candidate_manifest.digest,
            "backup_digest": execution.backup.archive_digest,
            "resumed": True,
            "activated": False,
        })
        return 0
    if action == "verify":
        receipt = verify_v3_migration_artifacts(
            arguments.source,
            candidate_root=arguments.candidate,
            backup_path=arguments.backup,
            receipt_path=arguments.receipt,
        )
        emit({
            "status": "verified",
            "migration_uid": receipt["migration_uid"],
            "workspace_uid": receipt["workspace_uid"],
            "source_digest": receipt["source"]["authority_digest"],
            "candidate_digest": receipt["candidate"]["authority_digest"],
            "activated": receipt["state"] == "activated",
        })
        return 0
    receipt = load_migration_receipt(arguments.path)
    emit(receipt)
    return 0


def _v4_backup_artifact_payload(status: str, artifact: object) -> dict[str, object]:
    return {
        "status": status,
        "archive_path": str(artifact.path),
        "backup_digest": artifact.digest,
        "authority_digest": artifact.authority_digest,
        "workspace_uid": artifact.workspace_uid,
        "file_count": artifact.file_count,
        "activated": False,
    }


def _run_v4_backup(arguments: argparse.Namespace) -> int:
    if arguments.v4_backup_action == "create":
        artifact = write_v4_backup(arguments.source, arguments.out)
        emit(_v4_backup_artifact_payload("backed_up", artifact))
        return 0
    if arguments.v4_backup_action == "verify":
        artifact = verify_v4_backup(arguments.archive)
        emit(_v4_backup_artifact_payload("verified", artifact))
        return 0
    receipt = restore_v4_backup(arguments.archive, arguments.to)
    emit({
        "status": "restored",
        "destination": str(receipt.destination),
        "backup_digest": receipt.backup_digest,
        "authority_digest": receipt.authority_digest,
        "workspace_uid": receipt.workspace_uid,
        "file_count": receipt.file_count,
        "activated": False,
    })
    return 0


def _run_storage(arguments: argparse.Namespace) -> int:
    if arguments.action == "v4-backup":
        try:
            return _run_v4_backup(arguments)
        except (V4BackupError, ValueError, OSError) as error:
            emit({
                "status": "refused",
                "code": getattr(error, "code", "V4_BACKUP_REFUSED"),
                "activated": False,
            })
            return 2
    if arguments.action == "migration":
        try:
            return _run_storage_migration(arguments)
        except (StorageMigrationError, ValueError, OSError) as error:
            emit({
                "status": "refused",
                "code": getattr(error, "code", "STORAGE_MIGRATION_REFUSED"),
            })
            return 2
    report = validate_storage_path(arguments.path)
    emit({
        "status": "valid" if report.valid else "invalid",
        "format_version": report.format_version,
        "workspace_uid": report.workspace_uid,
        "record_count": report.record_count,
        "issues": [
            {
                "code": issue.code,
                "artifact": issue.artifact,
                "instance_path": issue.instance_path,
                "keyword": issue.keyword,
            }
            for issue in report.issues
        ],
    })
    return 0 if report.valid else 2


def _planning_status(action: str) -> str:
    return {
        "start": "started",
        "done": "done",
        "drop": "dropped",
        "reopen": "open",
    }[action]


def _run_backlog(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "add":
        result = stack.add_task(
            arguments.title,
            arguments.detail,
            arguments.priority,
            arguments.due,
            arguments.tag,
            arguments.objective,
            arguments.parent,
            arguments.depends_on,
        )
    elif arguments.action == "list":
        result = stack.list_tasks(arguments.status)
    elif arguments.action == "show":
        result = stack.get_task(arguments.id)
    elif arguments.action == "note":
        result = stack.add_task_note(arguments.id, arguments.text)
    elif arguments.action == "subtask":
        if arguments.operation == "add":
            result = stack.add_subtask(
                arguments.task,
                arguments.subtask_or_title,
                arguments.priority,
            )
        else:
            result = stack.set_subtask_status(
                arguments.task,
                arguments.subtask_or_title,
                _planning_status(arguments.operation),
            )
    else:
        result = stack.set_task_status(arguments.id, _planning_status(arguments.action))
    emit(result)


def _run_okr(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "add-objective":
        result = stack.add_objective(arguments.text, arguments.quarter)
    elif arguments.action == "add-key-result":
        result = stack.add_key_result(arguments.objective, arguments.text, arguments.target)
    elif arguments.action == "list":
        result = stack.list_objectives(arguments.status)
    elif arguments.action == "link":
        result = stack.link_task(arguments.objective, arguments.task)
    elif arguments.action == "progress":
        result = stack.set_key_result_progress(
            arguments.objective,
            arguments.key_result,
            arguments.value,
        )
    else:
        result = stack.objective_rollup()
    emit(result)


def _run_worklog(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "checkin":
        result = stack.checkin(arguments.time, arguments.date)
    elif arguments.action == "add":
        result = stack.add_worklog(
            arguments.task,
            arguments.done,
            arguments.next_items,
            arguments.blocker,
            arguments.date,
        )
    else:
        result = stack.list_worklog(arguments.date)
    emit(result)


def _run_weekly(arguments: argparse.Namespace, stack: WorkStack) -> None:
    emit(stack.weekly_report(arguments.end, arguments.days))


def _run_note(arguments: argparse.Namespace, stack: WorkStack) -> None:
    emit(stack.add_note(arguments.text, arguments.link))


def _run_snapshot(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "preview":
        artifact = stack.planning_snapshot(arguments.task)
        emit({
            "snapshot": artifact.snapshot,
            "digest": artifact.digest,
            "filename": artifact.filename,
            "omissions": list(artifact.omissions),
        })
        return
    artifact = stack.confirmed_snapshot_export(
        arguments.task,
        arguments.expected_revision,
        arguments.expected_digest,
        arguments.confirm_disclosure,
    )
    output = write_snapshot_file(arguments.out, artifact.canonical_bytes)
    emit({
        "path": str(output),
        "digest": artifact.digest,
        "revision": artifact.snapshot["revision"],
    })


def _run_graph(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "export":
        output = Path(arguments.out).resolve()
        output.write_text(
            json.dumps(stack.snapshot(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output)
        return
    if arguments.seed_demo:
        stack.store.seed_demo(PROJECT_DATA)
    if getattr(arguments, "exit_with_parent", False):
        _bind_linux_process_lifetime_to_parent()
    public_port = getattr(arguments, "public_port", None)
    if public_port is None:
        serve(stack, arguments.host, arguments.port)
        return
    serve(stack, arguments.host, arguments.port, public_port=public_port)


def _bind_linux_process_lifetime_to_parent() -> None:
    """Fail closed with the owning SSH session instead of becoming an orphan."""

    if not sys.platform.startswith("linux"):
        raise OSError("--exit-with-parent requires Linux")
    import ctypes
    import signal

    parent_pid = os.getppid()
    if parent_pid <= 1:
        raise OSError("--exit-with-parent requires a live parent process")

    def terminate_with_parent(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate_with_parent)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error_number = ctypes.get_errno()
        raise OSError(error_number, "could not bind the server lifetime to its parent")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


STACK_COMMANDS = {
    "backlog": _run_backlog,
    "okr": _run_okr,
    "worklog": _run_worklog,
    "weekly": _run_weekly,
    "note": _run_note,
    "snapshot": _run_snapshot,
    "graph": _run_graph,
}


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.domain == "storage":
        return _run_storage(arguments)
    store = Store(arguments.data_dir) if arguments.data_dir else Store()
    try:
        if arguments.domain == "capture":
            return forward_capture(store, sys.stdin.buffer.read(64 * 1024 + 1), arguments.idempotency_key)
        if arguments.domain == "agent":
            return apply_agent_update(
                store,
                sys.stdin.buffer.read(AGENT_APPLY_LIMIT + 1),
                arguments.intent_id,
            )
        if arguments.domain == "maintenance":
            _run_maintenance(arguments, store)
            return 0
        stack = WorkStack(store, initialize=arguments.domain != "snapshot")
        STACK_COMMANDS[arguments.domain](arguments, stack)
        return 0
    except DomainError as error:
        print("error: {}: {}".format(error.code, error), file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2
