"""Rebinding a manifest onto the workspace identity its documents declare.

A store whose documents name one workspace under a manifest that names another
is refused by sync inspection, and no amount of retrying resolves it. Rebind is
the only authorized way out, and every step exists to make it reversible: the
candidate is archived and read back before the manifest moves, the displaced
manifest is quarantined rather than dropped, and a receipt records the exact
coordinate so a repeat of the same request replays instead of acting twice.

The receipt validators sit here rather than beside the manifest ones because
nothing else writes or reads this record, and the replay path has to prove the
archived evidence still matches it byte for byte.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import re
import secrets
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .store_document_validation import StoreReadiness, _canonical_uuid, _compact_json
from .store_errors import (
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
)
from .store_layout import DEFAULTS, STORE_MANIFEST_VERSION
from .store_manifest_validation import (
    _validate_recovery_timestamp,
    _validate_store_manifest_files,
    _validate_store_manifest_header,
    _validate_store_manifest_tasks,
)

__all__ = [
    "StoreRebindMixin",
    "_validated_rebind_artifact_name",
    "_validated_rebind_file_records",
]


def _validated_rebind_file_records(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(DEFAULTS):
        raise StoreCorruptError("sync rebind receipt is invalid")
    records: dict[str, dict[str, Any]] = {}
    for record in value:
        if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
            raise StoreCorruptError("sync rebind receipt is invalid")
        name = record.get("name")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(name, str) or name not in DEFAULTS or name in records:
            raise StoreCorruptError("sync rebind receipt is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise StoreCorruptError("sync rebind receipt is invalid")
        if not isinstance(digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ) is None:
            raise StoreCorruptError("sync rebind receipt is invalid")
        records[name] = record
    if set(records) != set(DEFAULTS):
        raise StoreCorruptError("sync rebind receipt is invalid")
    return records


def _validated_rebind_artifact_name(
    value: Any, prefix: str, suffix: str
) -> str:
    if not isinstance(value, str):
        raise StoreCorruptError("sync rebind receipt is invalid")
    if not value.startswith(prefix) or not value.endswith(suffix):
        raise StoreCorruptError("sync rebind receipt is invalid")
    if "/" in value or "\\" in value or Path(value).name != value:
        raise StoreCorruptError("sync rebind receipt is invalid")
    return value


class StoreRebindMixin:
    """Workspace-identity rebind for the composed ``Store``.

    Collaborators the composed Store owns: ``path``, ``runtime_root``,
    ``store_manifest_path``, ``sync_rebind_receipt_path``,
    ``_read_json_locked``, ``_atomic_write_locked``,
    ``_atomic_write_bytes_locked``, ``_process_lock``, and the sync
    collaborators ``_inspect_sync_locked``, ``_read_manifest_locked``,
    ``_manifest_digest``, ``_task_semantics_locked``,
    ``_validate_ready_state_locked``, ``_validate_adoption_key`` and
    ``_emit_event_locked``.
    """

    def _workspace_rebind_candidate_locked(
        self,
    ) -> tuple[dict[str, Any], StoreReadiness, dict[str, bytes], dict[str, Any]]:
        status = self._inspect_sync_locked()
        if (
            status["status"] != "external-change-invalid"
            or status["validation_error"]
            != "external candidate workspace identity changed"
        ):
            raise StoreExternalChangeError(status)
        readiness = self._validate_ready_state_locked()
        bodies = {name: self.path(name).read_bytes() for name in sorted(DEFAULTS)}
        files = [
            {
                "name": name,
                "size": len(body),
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            }
            for name, body in bodies.items()
        ]
        candidate_coordinate = {
            "workspace_id": readiness.workspace_uid,
            "store_schema_version": readiness.schema_version,
            "files": files,
        }
        candidate_digest = "sha256:" + hashlib.sha256(
            _compact_json(candidate_coordinate)
        ).hexdigest()
        if any(self.path(name).read_bytes() != body for name, body in bodies.items()):
            raise StoreExternalChangeError(self._inspect_sync_locked())
        preview = {
            "state": "workspace-identity-mismatch",
            "manifest_workspace_id": status["workspace_id"],
            "candidate_workspace_id": readiness.workspace_uid,
            "manifest_digest": status["manifest_digest"],
            "candidate_digest": candidate_digest,
            "changed_files": status["changed_files"],
        }
        return status, readiness, bodies, preview

    def workspace_rebind_preview(self) -> dict[str, Any]:
        with self._process_lock:
            return copy.deepcopy(self._workspace_rebind_candidate_locked()[3])

    @staticmethod
    def _candidate_backup_bytes(
        bodies: Mapping[str, bytes], preview: Mapping[str, Any]
    ) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(bodies):
                archive.writestr(name, bodies[name])
            archive.writestr(
                "recovery-manifest.json",
                _compact_json(
                    {
                        "schema_version": 1,
                        "operation": "workspace-rebind-candidate-backup",
                        "candidate_workspace_id": preview["candidate_workspace_id"],
                        "candidate_digest": preview["candidate_digest"],
                    }
                ),
            )
        result = buffer.getvalue()
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            for name, body in bodies.items():
                if archive.read(name) != body:
                    raise StoreCorruptError("workspace rebind backup verification failed")
        return result

    def _read_rebind_receipt_locked(self) -> dict[str, Any] | None:
        try:
            receipt = self._read_json_locked(self.sync_rebind_receipt_path)
        except FileNotFoundError:
            return None
        required = {
            "schema_version",
            "operation",
            "idempotency_key",
            "previous_workspace_id",
            "candidate_workspace_id",
            "manifest_digest",
            "candidate_digest",
            "result_manifest_digest",
            "authoritative_files",
            "backup_file",
            "backup_digest",
            "quarantined_manifest_file",
            "quarantined_manifest_digest",
            "created_at",
            "planning_mutated",
        }
        if (
            set(receipt) != required
            or receipt.get("schema_version") != 1
            or receipt.get("operation") != "workspace-rebind"
            or receipt.get("planning_mutated") is not False
            or not isinstance(receipt.get("idempotency_key"), str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", receipt["idempotency_key"])
            is None
            or not isinstance(receipt.get("authoritative_files"), list)
        ):
            raise StoreCorruptError("sync rebind receipt is invalid")
        for field in (
            "manifest_digest",
            "candidate_digest",
            "result_manifest_digest",
            "backup_digest",
            "quarantined_manifest_digest",
        ):
            if not isinstance(receipt.get(field), str) or re.fullmatch(
                r"sha256:[0-9a-f]{64}", receipt[field]
            ) is None:
                raise StoreCorruptError("sync rebind receipt is invalid")
        _canonical_uuid(
            receipt.get("previous_workspace_id"),
            "sync_rebind_receipt.previous_workspace_id",
        )
        _canonical_uuid(
            receipt.get("candidate_workspace_id"),
            "sync_rebind_receipt.candidate_workspace_id",
        )
        _validated_rebind_file_records(receipt["authoritative_files"])
        _validated_rebind_artifact_name(
            receipt.get("backup_file"), "workstack-rebind-candidate-", ".zip"
        )
        _validated_rebind_artifact_name(
            receipt.get("quarantined_manifest_file"),
            ".workstack-store-manifest.quarantine-",
            ".json",
        )
        _validate_recovery_timestamp(receipt.get("created_at"))
        return receipt

    @staticmethod
    def _verified_rebind_artifact_body(
        path: Path, expected_digest: str, label: str
    ) -> bytes:
        try:
            body = path.read_bytes()
        except FileNotFoundError as error:
            raise StoreCorruptError(
                "workspace rebind {} is missing".format(label)
            ) from error
        actual_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if not secrets.compare_digest(actual_digest, expected_digest):
            raise StoreCorruptError(
                "workspace rebind {} digest mismatch".format(label)
            )
        return body

    def _verify_rebind_recovery_artifacts_locked(
        self, receipt: Mapping[str, Any]
    ) -> dict[str, bytes]:
        backup_body = self._verified_rebind_artifact_body(
            self.runtime_root / receipt["backup_file"],
            receipt["backup_digest"],
            "candidate backup",
        )
        backup_bodies = self._verified_rebind_backup_bodies(receipt, backup_body)
        self._verify_rebind_quarantined_manifest(receipt)
        return backup_bodies

    @staticmethod
    def _verified_rebind_backup_bodies(
        receipt: Mapping[str, Any], backup_body: bytes
    ) -> dict[str, bytes]:
        records = _validated_rebind_file_records(receipt["authoritative_files"])
        expected_members = set(DEFAULTS) | {"recovery-manifest.json"}
        try:
            with zipfile.ZipFile(io.BytesIO(backup_body)) as archive:
                members = archive.namelist()
                if len(members) != len(set(members)) or set(members) != expected_members:
                    raise StoreCorruptError(
                        "workspace rebind candidate backup members are invalid"
                    )
                backup_bodies = {name: archive.read(name) for name in DEFAULTS}
                recovery_manifest = json.loads(
                    archive.read("recovery-manifest.json").decode("utf-8")
                )
        except (KeyError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
            raise StoreCorruptError(
                "workspace rebind candidate backup is invalid"
            ) from error
        if recovery_manifest != {
            "schema_version": 1,
            "operation": "workspace-rebind-candidate-backup",
            "candidate_workspace_id": receipt["candidate_workspace_id"],
            "candidate_digest": receipt["candidate_digest"],
        }:
            raise StoreCorruptError(
                "workspace rebind candidate backup manifest is invalid"
            )
        for name, body in backup_bodies.items():
            record = records[name]
            if len(body) != record["size"] or not secrets.compare_digest(
                "sha256:" + hashlib.sha256(body).hexdigest(), record["sha256"]
            ):
                raise StoreCorruptError(
                    "workspace rebind candidate backup evidence mismatch"
                )
        return backup_bodies

    def _verify_rebind_quarantined_manifest(
        self, receipt: Mapping[str, Any]
    ) -> None:
        quarantined_body = self._verified_rebind_artifact_body(
            self.runtime_root / receipt["quarantined_manifest_file"],
            receipt["quarantined_manifest_digest"],
            "quarantined manifest",
        )
        try:
            quarantined_manifest = json.loads(quarantined_body.decode("utf-8"))
            if not isinstance(quarantined_manifest, dict):
                raise ValueError("manifest must be an object")
            roster = _validate_store_manifest_header(quarantined_manifest)
            _validate_store_manifest_files(quarantined_manifest.get("files"), roster)
            _validate_store_manifest_tasks(quarantined_manifest.get("tasks"))
        except (UnicodeError, ValueError) as error:
            raise StoreCorruptError(
                "workspace rebind quarantined manifest is invalid"
            ) from error
        if (
            quarantined_manifest.get("workspace_id")
            != receipt["previous_workspace_id"]
            or not secrets.compare_digest(
                self._manifest_digest(quarantined_manifest),
                receipt["manifest_digest"],
            )
        ):
            raise StoreCorruptError(
                "workspace rebind quarantined manifest evidence mismatch"
            )

    def _rebind_result_locked(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        receipt_body = self.sync_rebind_receipt_path.read_bytes()
        return {
            "state": "in-sync",
            "workspace_id": receipt["candidate_workspace_id"],
            "generation": 0,
            "recovery": {
                "backup_path": str(self.runtime_root / receipt["backup_file"]),
                "backup_digest": receipt["backup_digest"],
                "receipt_path": str(self.sync_rebind_receipt_path),
                "receipt_digest": "sha256:" + hashlib.sha256(receipt_body).hexdigest(),
                "quarantined_manifest_path": str(
                    self.runtime_root / receipt["quarantined_manifest_file"]
                ),
                "quarantined_manifest_digest": receipt[
                    "quarantined_manifest_digest"
                ],
                "planning_mutated": False,
            },
        }

    def _validate_rebind_request(
        self,
        confirmed: bool,
        manifest_workspace_id: str,
        candidate_workspace_id: str,
        manifest_digest: str,
        candidate_digest: str,
        idempotency_key: str,
    ) -> tuple[str, str, str, str]:
        if confirmed is not True:
            raise ValueError("workspace rebind requires explicit confirmation")
        manifest_workspace_id = _canonical_uuid(
            manifest_workspace_id, "expected_manifest_workspace_id"
        )
        candidate_workspace_id = _canonical_uuid(
            candidate_workspace_id, "expected_candidate_workspace_id"
        )
        for label, digest in (
            ("manifest_digest", manifest_digest),
            ("candidate_digest", candidate_digest),
        ):
            if not isinstance(digest, str) or re.fullmatch(
                r"sha256:[0-9a-f]{64}", digest
            ) is None:
                raise ValueError("{} is invalid".format(label))
        self._validate_adoption_key(idempotency_key)
        if idempotency_key is None:
            raise ValueError("idempotency_key is required")
        return (
            manifest_workspace_id,
            candidate_workspace_id,
            manifest_digest,
            candidate_digest,
        )

    def _rebind_replay_locked(
        self,
        coordinate: tuple[str, str, str, str],
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        receipt = self._read_rebind_receipt_locked()
        if receipt is None or receipt.get("idempotency_key") != idempotency_key:
            return None
        receipt_coordinate = (
            receipt.get("previous_workspace_id"),
            receipt.get("candidate_workspace_id"),
            receipt.get("manifest_digest"),
            receipt.get("candidate_digest"),
        )
        if receipt_coordinate != coordinate:
            raise StoreAdoptionConflictError(
                "Idempotency-Key was already used for a different workspace rebind"
            )
        backup_bodies = self._verify_rebind_recovery_artifacts_locked(receipt)
        manifest = self._read_manifest_locked()
        if manifest is None or manifest.get("workspace_id") != coordinate[1]:
            return None
        if not secrets.compare_digest(
            receipt.get("result_manifest_digest", ""), self._manifest_digest(manifest)
        ):
            return None
        try:
            authoritative_bodies = {
                name: self.path(name).read_bytes() for name in sorted(DEFAULTS)
            }
        except FileNotFoundError:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        if authoritative_bodies != backup_bodies:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        return self._rebind_result_locked(receipt)

    def _commit_workspace_rebind_locked(
        self,
        coordinate: tuple[str, str, str, str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        manifest_workspace_id, candidate_workspace_id, manifest_digest, candidate_digest = coordinate
        status, readiness, bodies, preview = self._workspace_rebind_candidate_locked()
        actual = (
            preview["manifest_workspace_id"],
            preview["candidate_workspace_id"],
            preview["manifest_digest"],
            preview["candidate_digest"],
        )
        if actual != coordinate:
            raise StoreExternalChangeError(status)

        old_manifest_body = self.store_manifest_path.read_bytes()
        old_manifest_raw_digest = "sha256:" + hashlib.sha256(old_manifest_body).hexdigest()
        previous_manifest = self._read_manifest_locked()
        if previous_manifest is None or not secrets.compare_digest(
            self._manifest_digest(previous_manifest), manifest_digest
        ):
            raise StoreExternalChangeError(self._inspect_sync_locked())
        timestamp = dt.datetime.now(dt.timezone.utc)
        suffix = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        backup_name = "workstack-rebind-candidate-{}.zip".format(suffix)
        quarantine_name = ".workstack-store-manifest.quarantine-{}.json".format(suffix)
        backup_body = self._candidate_backup_bytes(bodies, preview)
        backup_digest = "sha256:" + hashlib.sha256(backup_body).hexdigest()
        files = [
            {
                "name": name,
                "size": len(body),
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            }
            for name, body in bodies.items()
        ]
        replacement = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": candidate_workspace_id,
            "store_schema_version": readiness.schema_version,
            "generation": 0,
            "files": {record["name"]: record["sha256"] for record in files},
            "tasks": self._task_semantics_locked(),
        }
        receipt = {
            "schema_version": 1,
            "operation": "workspace-rebind",
            "idempotency_key": idempotency_key,
            "previous_workspace_id": manifest_workspace_id,
            "candidate_workspace_id": candidate_workspace_id,
            "manifest_digest": manifest_digest,
            "candidate_digest": candidate_digest,
            "result_manifest_digest": self._manifest_digest(replacement),
            "authoritative_files": files,
            "backup_file": backup_name,
            "backup_digest": backup_digest,
            "quarantined_manifest_file": quarantine_name,
            "quarantined_manifest_digest": old_manifest_raw_digest,
            "created_at": timestamp.replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            ),
            "planning_mutated": False,
        }

        self._atomic_write_bytes_locked(self.runtime_root / backup_name, backup_body)
        self._atomic_write_bytes_locked(
            self.runtime_root / quarantine_name, old_manifest_body
        )
        self._atomic_write_locked(self.sync_rebind_receipt_path, receipt)
        _, _, final_bodies, final_preview = self._workspace_rebind_candidate_locked()
        if final_preview["candidate_digest"] != candidate_digest or final_bodies != bodies:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        self._atomic_write_locked(self.store_manifest_path, replacement)
        try:
            post_replace_bodies = {
                name: self.path(name).read_bytes() for name in sorted(DEFAULTS)
            }
        except FileNotFoundError:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        if post_replace_bodies != bodies:
            self._sync_fingerprint = None
            raise StoreExternalChangeError(self._inspect_sync_locked())
        self._generation = 0
        self._readiness = readiness
        self._sync_fingerprint = None
        self._sync_state = "in-sync"
        self._emit_event_locked("store.workspace-rebound", candidate_workspace_id, [])
        return self._rebind_result_locked(receipt)

    def rebind_workspace_identity(
        self,
        *,
        confirmed: bool,
        expected_manifest_workspace_id: str,
        expected_candidate_workspace_id: str,
        expected_manifest_digest: str,
        expected_candidate_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        coordinate = self._validate_rebind_request(
            confirmed,
            expected_manifest_workspace_id,
            expected_candidate_workspace_id,
            expected_manifest_digest,
            expected_candidate_digest,
            idempotency_key,
        )

        with self._process_lock:
            replay = self._rebind_replay_locked(coordinate, idempotency_key)
            return replay or self._commit_workspace_rebind_locked(
                coordinate, idempotency_key
            )
