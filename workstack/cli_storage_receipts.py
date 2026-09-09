"""The machine-readable receipts the storage and maintenance commands emit.

Each function is pure: it projects an artifact the storage layer already
returned into the emitted document. The commands themselves stay in
`workstack/cli.py`, which is where the storage authority is called.
"""

from __future__ import annotations


def backup_receipt(artifact: object) -> dict[str, object]:
    return {
        "path": str(artifact.path),
        "workspace_id": artifact.workspace_id,
        "created_at": artifact.created_at,
        "digest": artifact.digest,
        "file_count": artifact.file_count,
    }


def restore_receipt(receipt: object) -> dict[str, object]:
    return {
        "destination": str(receipt.destination),
        "workspace_id": receipt.workspace_id,
        "backup_digest": receipt.backup_digest,
        "safety_backup": str(receipt.safety_backup) if receipt.safety_backup else None,
    }


def initialize_receipt(receipt: object) -> dict[str, object]:
    return {
        "destination": str(receipt.destination),
        "workspace_id": receipt.workspace_id,
        "store_schema_version": receipt.store_schema_version,
    }


def relocate_receipt(receipt: object) -> dict[str, object]:
    return {
        "destination": str(receipt.destination),
        "workspace_id": receipt.workspace_id,
        "backup_digest": receipt.backup_digest,
        "source_preserved": True,
    }


def _migration_paths(value: object) -> dict[str, object]:
    paths = value.paths
    return {
        "source_path": str(paths.source_root),
        "candidate_path": str(paths.candidate_root),
        "backup_path": str(paths.backup_path),
    }


def migration_plan_receipt(plan: object) -> dict[str, object]:
    return {
        "status": "planned",
        **_migration_paths(plan),
        "source_digest": plan.frozen.aggregate_digest,
        "source_file_count": len(plan.frozen.artifacts),
        "activated": False,
    }


def migration_preview_receipt(preview: object) -> dict[str, object]:
    return {
        "status": "previewed",
        **_migration_paths(preview),
        "receipt_path": str(preview.receipt_path),
        "source_digest": preview.frozen.aggregate_digest,
        "source_semantic_digest": preview.conversion.source_snapshot_digest,
        "conversion_digest": preview.conversion.conversion_digest,
        "record_count": sum(len(items) for items in preview.conversion.records.values()),
        "stream_event_count": sum(len(items) for items in preview.conversion.streams.values()),
        "activated": False,
    }


def migration_execute_receipt(execution: object) -> dict[str, object]:
    return {
        "status": "verified_candidate",
        **_migration_paths(execution.preview),
        "receipt_path": str(execution.receipt_path),
        "source_digest": execution.preview.frozen.aggregate_digest,
        "conversion_digest": execution.preview.conversion.conversion_digest,
        "candidate_digest": execution.candidate_manifest.digest,
        "backup_digest": execution.backup.archive_digest,
        "source_unchanged": True,
        "activated": False,
    }


def migration_resume_receipt(execution: object) -> dict[str, object]:
    return {
        "status": "verified_candidate",
        **_migration_paths(execution.preview),
        "receipt_path": str(execution.receipt_path),
        "candidate_digest": execution.candidate_manifest.digest,
        "backup_digest": execution.backup.archive_digest,
        "resumed": True,
        "activated": False,
    }


def migration_verify_receipt(receipt: dict) -> dict[str, object]:
    return {
        "status": "verified",
        "migration_uid": receipt["migration_uid"],
        "workspace_uid": receipt["workspace_uid"],
        "source_digest": receipt["source"]["authority_digest"],
        "candidate_digest": receipt["candidate"]["authority_digest"],
        "activated": receipt["state"] == "activated",
    }


def v4_backup_receipt(status: str, artifact: object) -> dict[str, object]:
    return {
        "status": status,
        "archive_path": str(artifact.path),
        "backup_digest": artifact.digest,
        "authority_digest": artifact.authority_digest,
        "workspace_uid": artifact.workspace_uid,
        "file_count": artifact.file_count,
        "activated": False,
    }


def v4_restore_receipt(receipt: object) -> dict[str, object]:
    return {
        "status": "restored",
        "destination": str(receipt.destination),
        "backup_digest": receipt.backup_digest,
        "authority_digest": receipt.authority_digest,
        "workspace_uid": receipt.workspace_uid,
        "file_count": receipt.file_count,
        "activated": False,
    }


def validation_receipt(report: object) -> dict[str, object]:
    return {
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
    }


def refusal_receipt(error: object, default_code: str, *, activated: bool | None = None) -> dict[str, object]:
    """The content-free refusal a storage command prints instead of a receipt."""

    payload: dict[str, object] = {"status": "refused", "code": getattr(error, "code", default_code)}
    if activated is not None:
        payload["activated"] = activated
    return payload
