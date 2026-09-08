"""External-change detection, the committed manifest, and the event stream.

One question runs through this module: is what is on disk still the generation
Work Stack committed? The manifest answers it, so writing that manifest,
re-reading it, comparing an external candidate against it and publishing what
changed all belong together -- split apart, the rule that admits a candidate
would be free to drift from the rule that commits one.

Adoption is here for the same reason. It is the one operation that turns a
detected external change into the committed baseline, and it does so through the
very inspection that detected it, re-run immediately before the manifest
advances.
"""

from __future__ import annotations

import copy
import hashlib
import re
import secrets
from typing import Any, Mapping

from .store_document_validation import _canonical_uuid, _compact_json
from .store_errors import (
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
)
from .store_layout import (
    CHANGE_NOTICE_TYPE,
    DEFAULTS,
    MAX_COMMIT_EVENTS,
    STORE_MANIFEST_VERSION,
)
from .store_manifest_validation import (
    _task_semantics,
    _validate_store_manifest_files,
    _validate_store_manifest_header,
    _validate_store_manifest_tasks,
)

__all__ = ["CHANGE_NOTICE_TYPE", "MAX_COMMIT_EVENTS", "StoreSyncMixin"]


class StoreSyncMixin:
    """Manifest, sync inspection and adoption for the composed ``Store``.

    Collaborators the composed Store owns: ``path``, ``store_manifest_path``,
    ``sync_adoption_receipt_path``, ``_read_json_locked``,
    ``_atomic_write_locked``, ``_validate_ready_state_locked``, ``transaction``,
    ``_process_lock``, ``_event_condition``, ``_events``, ``_event_sequence``,
    ``_generation``, ``_readiness``, ``_sync_state`` and ``_sync_fingerprint``.
    """

    def _authoritative_hashes_locked(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for name in sorted(DEFAULTS):
            try:
                body = self.path(name).read_bytes()
            except FileNotFoundError as error:
                raise StoreCorruptError(
                    "required store is missing: {}".format(self.path(name))
                ) from error
            hashes[name] = "sha256:" + hashlib.sha256(body).hexdigest()
        return hashes

    def _read_manifest_locked(self) -> dict[str, Any] | None:
        try:
            manifest = self._read_json_locked(self.store_manifest_path)
        except FileNotFoundError:
            return None
        roster = _validate_store_manifest_header(manifest)
        _validate_store_manifest_files(manifest.get("files"), roster)
        _validate_store_manifest_tasks(manifest.get("tasks"))
        return manifest

    def _manifest_digest(self, manifest: Mapping[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(_compact_json(manifest)).hexdigest()

    def _task_semantics_locked(self) -> dict[str, dict[str, Any]]:
        return _task_semantics(self._read_json_locked(self.path("backlog.json")))

    def _emit_event_locked(
        self, event_type: str, workspace_id: str, changed_files: list[str]
    ) -> None:
        self._event_sequence += 1
        self._events.append({
            "id": self._event_sequence,
            "type": event_type,
            "workspace_id": workspace_id,
            "generation": self._generation,
            "changed_files": changed_files,
        })
        self._event_condition.notify_all()

    def projected_change_event_id(
        self, *, pending_commit_events: int = MAX_COMMIT_EVENTS
    ) -> int:
        """A conservative ceiling for the id a post-commit notice could receive.

        A committed save does NOT always emit exactly one event: an external
        writer that does not hold the process lock makes the commit resolve
        through a late-external branch that still succeeds while emitting two or
        three. ``MAX_COMMIT_EVENTS`` records where those come from, so the
        default reserves room for the worst successful branch rather than the
        best one. The real published id is never higher than this ceiling, which
        is what makes it safe to preflight against.

        Callers that commit nothing may pass zero. Reading under the lock keeps
        this consistent with the sequence the publisher will use; it allocates
        nothing and advances nothing.
        """

        if type(pending_commit_events) is not int or pending_commit_events < 0:
            raise ValueError("pending commit events must be a non-negative integer")
        with self._process_lock:
            return self._event_sequence + pending_commit_events + 1

    def publish_change_notice(self, build: Any) -> int:
        """Append one typed change record to the SAME bounded event sequence.

        Callers hold the outer transaction lock already. ``_process_lock`` is an
        RLock, so this reentrant acquisition is the same holder: the record
        therefore lands while that transaction is still held and before the
        caller serializes any HTTP response.

        The id comes from the one Store sequence the legacy sync records use, so
        ids stay unique and strictly ascending across both kinds and both share
        the existing 128-record retention bound. The sequence is advanced only
        after ``build`` returns, so a rejected notice neither consumes an id nor
        leaves a partial record in the deque.
        """

        with self._process_lock:
            sequence = self._event_sequence + 1
            notice = build(sequence)
            if not isinstance(notice, Mapping):
                raise ValueError("change notice must be a mapping")
            record = {
                "id": sequence,
                "type": CHANGE_NOTICE_TYPE,
                "notice": dict(notice),
            }
            self._event_sequence = sequence
            self._events.append(record)
            self._event_condition.notify_all()
            return sequence

    def _write_committed_manifest_locked(
        self,
        changed_files: list[str],
        *,
        event_type: str = "store.committed",
        expected_hashes: Mapping[str, str] | None = None,
    ) -> None:
        hashes_before = self._authoritative_hashes_locked()
        if expected_hashes is not None and hashes_before != expected_hashes:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        tasks_before = self._task_semantics_locked()
        readiness = self._validate_ready_state_locked()
        hashes_after = self._authoritative_hashes_locked()
        tasks_after = self._task_semantics_locked()
        if hashes_before != hashes_after or tasks_before != tasks_after:
            raise StoreCorruptError("authoritative store changed while committing its manifest")
        if expected_hashes is not None and hashes_after != expected_hashes:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        previous = self._read_manifest_locked()
        persisted_generation = previous["generation"] if previous is not None else 0
        self._generation = max(self._generation, persisted_generation) + 1
        manifest = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": readiness.workspace_uid,
            "store_schema_version": readiness.schema_version,
            "generation": self._generation,
            "files": hashes_after,
            "tasks": tasks_after,
        }
        self._atomic_write_locked(self.store_manifest_path, manifest)
        self._readiness = readiness
        self._sync_state = "in-sync"
        self._sync_fingerprint = None
        self._emit_event_locked(event_type, readiness.workspace_uid, changed_files)

    def _write_local_baseline_locked(
        self,
        previous: Mapping[str, Any],
        files: Mapping[str, str],
        tasks: Mapping[str, Any],
        changed_files: list[str],
    ) -> None:
        """Commit only declared local bytes while leaving unrelated bytes external."""

        persisted_generation = previous["generation"]
        self._generation = max(self._generation, persisted_generation) + 1
        manifest = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": previous["workspace_id"],
            "store_schema_version": previous["store_schema_version"],
            "generation": self._generation,
            "files": dict(files),
            "tasks": copy.deepcopy(tasks),
        }
        self._atomic_write_locked(self.store_manifest_path, manifest)
        self._sync_state = "in-sync"
        self._sync_fingerprint = None
        self._emit_event_locked(
            "store.committed", previous["workspace_id"], changed_files
        )

    def _local_baseline_tasks_locked(
        self,
        previous: Mapping[str, Any],
        expected_hashes: Mapping[str, str],
        changed_files: list[str],
    ) -> Mapping[str, Any]:
        before = self._authoritative_hashes_locked()
        if any(before[name] != expected_hashes[name] for name in changed_files):
            raise StoreCorruptError(
                "local commit target changed concurrently; recovery journal retained"
            )
        tasks = (
            self._task_semantics_locked()
            if "backlog.json" in changed_files
            else previous["tasks"]
        )
        after = self._authoritative_hashes_locked()
        if any(after[name] != expected_hashes[name] for name in changed_files):
            raise StoreCorruptError(
                "local commit target changed concurrently; recovery journal retained"
            )
        return tasks

    def _ensure_sync_manifest_locked(self) -> dict[str, Any]:
        manifest = self._read_manifest_locked()
        if manifest is not None:
            return manifest
        readiness = self._validate_ready_state_locked()
        self._generation = max(self._generation, 0)
        manifest = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": readiness.workspace_uid,
            "store_schema_version": readiness.schema_version,
            "generation": self._generation,
            "files": self._authoritative_hashes_locked(),
            "tasks": self._task_semantics_locked(),
        }
        self._atomic_write_locked(self.store_manifest_path, manifest)
        self._readiness = readiness
        return manifest

    @staticmethod
    def _changed_manifest_files(
        manifest: Mapping[str, Any], current_hashes: Mapping[str, str]
    ) -> list[str]:
        return sorted(
            name
            for name in DEFAULTS
            if current_hashes[name] != manifest["files"].get(name)
        )

    @staticmethod
    def _validate_candidate_tasks(
        baseline_tasks: Mapping[str, Any], candidate_tasks: Mapping[str, Any]
    ) -> None:
        removed = sorted(set(baseline_tasks) - set(candidate_tasks))
        if removed:
            raise StoreCorruptError("external candidate removes existing Tasks")
        for task_id, candidate in candidate_tasks.items():
            baseline = baseline_tasks.get(task_id)
            if baseline is None:
                if candidate["revision"] != 0:
                    raise StoreCorruptError(
                        "external candidate new Task revision is invalid"
                    )
            elif (
                candidate["digest"] != baseline["digest"]
                and candidate["revision"] <= baseline["revision"]
            ):
                raise StoreCorruptError(
                    "external candidate Task revision did not advance"
                )

    def _validate_external_candidate_locked(
        self,
        manifest: Mapping[str, Any],
        current_hashes: Mapping[str, str],
        candidate_workspace_id: str,
    ) -> None:
        if candidate_workspace_id != manifest["workspace_id"]:
            raise StoreCorruptError("external candidate workspace identity changed")
        self._validate_candidate_tasks(
            manifest["tasks"], self._task_semantics_locked()
        )
        if self._authoritative_hashes_locked() != current_hashes:
            raise StoreCorruptError("external candidate changed during validation")

    def _invalid_candidate_changed_files_locked(
        self, manifest: Mapping[str, Any]
    ) -> list[str]:
        return sorted(
            name
            for name in DEFAULTS
            if not self.path(name).is_file()
            or manifest["files"].get(name)
            != ("sha256:" + hashlib.sha256(self.path(name).read_bytes()).hexdigest())
        )

    def _inspect_candidate_locked(
        self, manifest: Mapping[str, Any]
    ) -> tuple[str, dict[str, str], list[str], str | None]:
        candidate_workspace_id = manifest["workspace_id"]
        try:
            current_hashes = self._authoritative_hashes_locked()
            changed_files = self._changed_manifest_files(manifest, current_hashes)
            if changed_files:
                readiness = self._validate_ready_state_locked()
                candidate_workspace_id = readiness.workspace_uid
                self._validate_external_candidate_locked(
                    manifest, current_hashes, candidate_workspace_id
                )
        except StoreCorruptError as error:
            current_hashes = {}
            changed_files = self._invalid_candidate_changed_files_locked(manifest)
            return candidate_workspace_id, current_hashes, changed_files, str(error)
        return candidate_workspace_id, current_hashes, changed_files, None

    @staticmethod
    def _sync_inspection_state(
        changed_files: list[str], validation_error: str | None
    ) -> str:
        if validation_error is not None:
            return "external-change-invalid"
        return "external-change-detected" if changed_files else "in-sync"

    @staticmethod
    def _sync_candidate_fingerprint(
        state: str, current_hashes: Mapping[str, str], changed_files: list[str]
    ) -> str:
        return "{}:{}".format(
            state,
            hashlib.sha256(_compact_json({"files": current_hashes, "changed": changed_files})).hexdigest(),
        )

    def _candidate_manifest_digest_locked(
        self,
        manifest: Mapping[str, Any],
        candidate_workspace_id: str,
        current_hashes: Mapping[str, str],
        changed_files: list[str],
        validation_error: str | None,
    ) -> str | None:
        if validation_error is not None or not changed_files:
            return None
        candidate = {
            "workspace_id": candidate_workspace_id,
            "store_schema_version": manifest["store_schema_version"],
            "generation": manifest["generation"],
            "files": current_hashes,
            "tasks": self._task_semantics_locked(),
        }
        return "sha256:" + hashlib.sha256(_compact_json(candidate)).hexdigest()

    def _inspect_sync_locked(self) -> dict[str, Any]:
        manifest = self._ensure_sync_manifest_locked()
        self._generation = max(self._generation, manifest["generation"])
        (
            candidate_workspace_id,
            current_hashes,
            changed_files,
            validation_error,
        ) = self._inspect_candidate_locked(manifest)
        state = self._sync_inspection_state(changed_files, validation_error)
        fingerprint = self._sync_candidate_fingerprint(
            state, current_hashes, changed_files
        )
        if state != "in-sync" and fingerprint != self._sync_fingerprint:
            self._emit_event_locked("store." + state, candidate_workspace_id, changed_files)
        self._sync_state = state
        self._sync_fingerprint = None if state == "in-sync" else fingerprint
        candidate_digest = self._candidate_manifest_digest_locked(
            manifest,
            candidate_workspace_id,
            current_hashes,
            changed_files,
            validation_error,
        )
        return {
            "status": state,
            "writes_allowed": state == "in-sync",
            "workspace_id": manifest["workspace_id"],
            "candidate_workspace_id": candidate_workspace_id,
            "store_schema_version": manifest["store_schema_version"],
            "generation": manifest["generation"],
            "manifest_digest": self._manifest_digest(manifest),
            "files": copy.deepcopy(manifest["files"]),
            "changed_files": changed_files,
            "validation_error": validation_error,
            "candidate_digest": candidate_digest,
        }

    def sync_status(self) -> dict[str, Any]:
        with self._process_lock:
            status = self._inspect_sync_locked()
            return {
                "state": (
                    "invalid"
                    if status["status"] == "external-change-invalid"
                    else status["status"]
                ),
                "workspace_id": status["workspace_id"],
                "candidate_workspace_id": status["candidate_workspace_id"],
                "generation": status["generation"],
                "manifest_digest": status["candidate_digest"] or status["manifest_digest"],
                "changed_files": status["changed_files"],
                "reason": (
                    "authoritative store candidate failed validation"
                    if status["validation_error"] is not None
                    else None
                ),
                "rebind_available": (
                    status["status"] == "external-change-invalid"
                    and status["validation_error"]
                    == "external candidate workspace identity changed"
                ),
            }

    def _read_adoption_receipt_locked(self) -> dict[str, Any] | None:
        try:
            receipt = self._read_json_locked(self.sync_adoption_receipt_path)
        except FileNotFoundError:
            return None
        expected = {
            "version",
            "idempotency_key",
            "expected_generation",
            "expected_manifest_digest",
            "result_generation",
            "result_manifest_digest",
            "workspace_id",
        }
        valid_key = receipt.get("idempotency_key") is None or (
            isinstance(receipt.get("idempotency_key"), str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", receipt["idempotency_key"])
        )
        valid_digest = all(
            isinstance(receipt.get(field), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", receipt[field])
            for field in ("expected_manifest_digest", "result_manifest_digest")
        )
        if (
            set(receipt) != expected
            or receipt.get("version") != 1
            or not valid_key
            or type(receipt.get("expected_generation")) is not int
            or receipt["expected_generation"] < 0
            or type(receipt.get("result_generation")) is not int
            or receipt["result_generation"] < 0
            or not valid_digest
        ):
            raise StoreCorruptError("sync adoption receipt is invalid")
        _canonical_uuid(receipt.get("workspace_id"), "sync_adoption_receipt.workspace_id")
        return receipt

    @staticmethod
    def _validate_adoption_key(idempotency_key: str | None) -> None:
        if idempotency_key is not None and re.fullmatch(
            r"[A-Za-z0-9._:-]{8,128}", idempotency_key
        ) is None:
            raise ValueError("idempotency_key is invalid")

    def _guard_adoption_key_locked(
        self,
        receipt: Mapping[str, Any] | None,
        idempotency_key: str | None,
        expected_generation: int,
        expected_manifest_digest: str,
    ) -> None:
        if (
            receipt is not None
            and idempotency_key is not None
            and receipt.get("idempotency_key") == idempotency_key
            and (
                receipt.get("expected_generation") != expected_generation
                or not secrets.compare_digest(
                    receipt.get("expected_manifest_digest", ""),
                    expected_manifest_digest,
                )
            )
        ):
            raise StoreAdoptionConflictError(
                "Idempotency-Key was already used for a different sync candidate"
            )

    def _adoption_replay_locked(
        self,
        receipt: Mapping[str, Any] | None,
        status: Mapping[str, Any],
        expected_generation: int,
        expected_manifest_digest: str,
    ) -> bool:
        if status["status"] != "in-sync":
            return False
        manifest = self._read_manifest_locked()
        receipt_match = bool(
            receipt is not None
            and manifest is not None
            and receipt.get("workspace_id") == manifest["workspace_id"]
            and receipt.get("expected_generation") == expected_generation
            and secrets.compare_digest(
                receipt.get("expected_manifest_digest", ""),
                expected_manifest_digest,
            )
            and receipt.get("result_generation") == manifest["generation"]
            and secrets.compare_digest(
                receipt.get("result_manifest_digest", ""),
                self._manifest_digest(manifest),
            )
        )
        if receipt_match:
            return True
        if manifest is None or manifest["generation"] != expected_generation + 1:
            return False
        reconstructed_candidate = {
            "workspace_id": manifest["workspace_id"],
            "store_schema_version": manifest["store_schema_version"],
            "generation": expected_generation,
            "files": manifest["files"],
            "tasks": manifest["tasks"],
        }
        reconstructed_digest = "sha256:" + hashlib.sha256(
            _compact_json(reconstructed_candidate)
        ).hexdigest()
        return secrets.compare_digest(
            reconstructed_digest, expected_manifest_digest
        )

    def adopt_external_change(
        self,
        expected_generation: int,
        expected_manifest_digest: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("expected_generation must be a non-negative integer")
        if not isinstance(expected_manifest_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_manifest_digest
        ):
            raise ValueError("manifest_digest is invalid")
        self._validate_adoption_key(idempotency_key)
        with self.transaction():
            status = self._inspect_sync_locked()
            receipt = self._read_adoption_receipt_locked()
            self._guard_adoption_key_locked(
                receipt,
                idempotency_key,
                expected_generation,
                expected_manifest_digest,
            )
            if self._adoption_replay_locked(
                receipt,
                status,
                expected_generation,
                expected_manifest_digest,
            ):
                return self.sync_status()
            public_digest = status["candidate_digest"] or status["manifest_digest"]
            if (
                status["status"] != "external-change-detected"
                or status["generation"] != expected_generation
                or not secrets.compare_digest(public_digest, expected_manifest_digest)
            ):
                raise StoreExternalChangeError(status)
            changed_files = list(status["changed_files"])
            # Re-hash and revalidate immediately before advancing the committed
            # manifest. External writers do not honor our lease, so a changed
            # candidate is never silently adopted.
            current = self._inspect_sync_locked()
            if current["candidate_digest"] != status["candidate_digest"]:
                raise StoreExternalChangeError(current)
            self._write_committed_manifest_locked(
                changed_files, event_type="store.external-change-adopted"
            )
            manifest = self._read_manifest_locked()
            if manifest is None:
                raise StoreCorruptError("committed sync manifest is missing")
            self._atomic_write_locked(
                self.sync_adoption_receipt_path,
                {
                    "version": 1,
                    "idempotency_key": idempotency_key,
                    "expected_generation": expected_generation,
                    "expected_manifest_digest": expected_manifest_digest,
                    "result_generation": manifest["generation"],
                    "result_manifest_digest": self._manifest_digest(manifest),
                    "workspace_id": manifest["workspace_id"],
                },
            )
            return self.sync_status()

    def sync_events(self, after: int = 0) -> dict[str, Any]:
        if type(after) is not int or after < 0:
            raise ValueError("event cursor must be a non-negative integer")
        with self._process_lock:
            status = self._inspect_sync_locked()
            events = [copy.deepcopy(event) for event in self._events if event["id"] > after]
            return {
                "delivery": "bounded-process-local",
                "latest_event_id": self._event_sequence,
                "generation": status["generation"],
                "state": (
                    "invalid"
                    if status["status"] == "external-change-invalid"
                    else status["status"]
                ),
                "events": events,
            }

    def wait_for_sync_events(self, after: int, timeout: float = 15.0) -> dict[str, Any]:
        if type(after) is not int or after < 0:
            raise ValueError("event cursor must be a non-negative integer")
        if timeout < 0 or timeout > 30:
            raise ValueError("event wait timeout is invalid")
        with self._event_condition:
            self._inspect_sync_locked()
            if self._event_sequence <= after:
                self._event_condition.wait(timeout)
            return self.sync_events(after)

    def _assert_writable_locked(self) -> None:
        if self._readiness is None:
            return
        status = self._inspect_sync_locked()
        if not status["writes_allowed"]:
            raise StoreExternalChangeError(status)
