"""Exact backup and preview-token binding for the v3 deletion transaction.

The two durable side effects a commit takes before its single journaled
``save_many`` generation live here: the verified zip backup of the untouched
documents, and the store-held preview binding a commit must still match. The
injected pre-commit fault seam is honoured from one place so preview, commit
and hard delete cannot drift apart.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from .deletion_v3_documents import V3_DOCUMENT_NAMES, TaskDeletionTransactionError


PREVIEW_TTL_SECONDS = 900
_PREVIEW_ATTR = "_v3_deletion_previews"
_FAULT_ATTR = "_v3_deletion_before_commit"


def previews(store: Any) -> dict[str, dict[str, Any]]:
    bucket = getattr(store, _PREVIEW_ATTR, None)
    if bucket is None:
        bucket = {}
        setattr(store, _PREVIEW_ATTR, bucket)
    return bucket


def run_before_commit_fault(store: Any) -> None:
    """Fire the injected pre-commit fault, if the store carries one."""

    fault: Callable[[], None] | None = getattr(store, _FAULT_ATTR, None)
    if callable(fault):
        fault()


def backup_dir(store: Any) -> Path:
    return Path(store.runtime_root) / "task-deletion-backups"


def write_exact_backup(store: Any, task_id: str, digest: str) -> dict[str, str]:
    directory = backup_dir(store)
    directory.mkdir(parents=True, exist_ok=True)
    filename = "task-deletion-{}-{}.zip".format(task_id, digest[-16:])
    destination = directory / filename
    buffer = io.BytesIO()
    bodies: dict[str, bytes] = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in V3_DOCUMENT_NAMES:
            body = store.path(name).read_bytes()
            bodies[name] = body
            archive.writestr(name, body)
    payload = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        for name, body in bodies.items():
            if archive.read(name) != body:
                raise TaskDeletionTransactionError("backup_failed", "backup_failed", 409)
    destination.write_bytes(payload)
    return {
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "location": str(destination),
    }


def bind_preview(
    store: Any,
    token: str,
    *,
    client_request_id: str,
    plan_digest: str,
    revision: int,
    store_digest: str,
    task_id: str,
    task_uid: str,
    workspace_uid: str,
) -> None:
    previews(store)[token] = {
        "client_request_id": client_request_id,
        "expires_at": time.time() + PREVIEW_TTL_SECONDS,
        "plan_digest": plan_digest,
        "revision": revision,
        "store_digest": store_digest,
        "task_id": task_id,
        "task_uid": task_uid,
        "workspace_uid": workspace_uid,
    }


def _invalid_preview_token() -> TaskDeletionTransactionError:
    return TaskDeletionTransactionError(
        "invalid_preview_token",
        "invalid_preview_token",
        409,
        {"preview_token": True},
    )


def validate_token(
    store: Any,
    token: object,
    *,
    task_id: str,
    task_uid: str,
    revision: int,
    workspace_uid: str,
    store_digest: str,
    plan_digest: str,
) -> str:
    if not isinstance(token, str) or not token:
        raise _invalid_preview_token()
    binding = previews(store).get(token)
    if binding is None:
        raise _invalid_preview_token()
    if float(binding.get("expires_at", 0)) < time.time():
        raise TaskDeletionTransactionError("preview_expired", "preview_expired", 409)
    expected = {
        "task_id": task_id,
        "task_uid": task_uid,
        "revision": revision,
        "workspace_uid": workspace_uid,
        "store_digest": store_digest,
        "plan_digest": plan_digest,
    }
    for field, value in expected.items():
        actual = binding.get(field)
        if isinstance(actual, str) and isinstance(value, str):
            if not hmac.compare_digest(actual, value):
                raise TaskDeletionTransactionError("preview_stale", "preview_stale", 409)
        elif actual != value:
            raise TaskDeletionTransactionError("preview_stale", "preview_stale", 409)
    return token
