"""Crash-safe JSON storage with one lock shared by every writer."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import threading
import uuid
import zipfile
from collections import deque

# The one typed change record kind carried beside the legacy sync records.
CHANGE_NOTICE_TYPE = "workstack.change.v1"

# The most events ONE successful _commit_prepared_locked can emit. Derived
# from the three successful branches of that method, not guessed:
#
#   1. the undisturbed branch reaches _write_committed_manifest_locked, which
#      emits one store.committed;
#   2. an unrelated external change seen by _commit_race_groups goes to
#      _commit_local_with_external_candidate_locked, which emits one
#      store.committed through _write_local_baseline_locked and then one
#      external-state observation through _inspect_sync_locked;
#   3. an unrelated external change arriving later makes
#      _write_committed_manifest_locked raise, which first emits an
#      external-state observation, and the resolution then runs branch 2.
#
# An external writer need not hold the process lock, so branches 2 and 3 are
# ordinary successful outcomes rather than faults. Capacity is reserved for
# the worst of them, and tests drive each real branch instead of asserting a
# constant.
MAX_COMMIT_EVENTS = 3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import store_rosters
from . import store_report_migration
from .planning_status import (
    PlanningStatusValidationError,
    append_bootstrap,
    validate_and_project,
)
from .store_document_validation import (
    ACTIVITY_DEFAULT,
    AUXILIARY_DEFAULTS,
    BACKLOG_DEFAULT,
    IDENTITY_STORES,
    MAX_REVISION,
    REPORTS_DEFAULT,
    StoreReadiness,
    _canonical_uuid,
    _compact_json,
    _require_task_display_id_authority,
    _stored_revision,
    admitted_tasks,
    decode_documents,
    supported_roster,
    validate_document_values,
    validate_workspace,
)
from .store_errors import (
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
)
from .task_display_id import (
    FIELD as TASK_DISPLAY_ID_HIGH_WATER,
    TaskDisplayIdError,
    admitted_high_water,
    read_optional_high_water,
    task_ids_from_records,
)
from .file_lease import StoreLockedError, _FileLease


# Schema 4 belongs to workstack.ssot, so the next collection-layout version is
# 5: the nine released documents plus reports.json.
STORE_SCHEMA_VERSION = 5


_WORKSPACE_REQUIRED_KEYS = frozenset({"version", "id", "name"})
_WORKSPACE_OPTIONAL_KEYS = frozenset({TASK_DISPLAY_ID_HIGH_WATER})


def _workspace_default() -> dict[str, Any]:
    return {"version": 2, "id": str(uuid.uuid4()), "name": "Work Stack"}


def _store_meta_default() -> dict[str, Any]:
    return {
        "version": 2,
        "store_schema_version": STORE_SCHEMA_VERSION,
        "migrations": {
            "identity": {
                "id": "workstack.store.v2",
                "origin": "fresh",
                "source_sha256": None,
            },
            "planning_status": {
                "id": "workstack.planning-status.v1",
                "origin": "fresh",
                "source_sha256": None,
            },
            "reports": {
                "id": "workstack.reports.v5",
                "origin": "fresh",
                "source_sha256": None,
            },
        },
    }


# Composed from the payload shapes store_document_validation owns, so a shape
# cannot drift between what this build writes and what the historical readers
# accept. The two identity documents are built per store and stay None here.
DEFAULTS: dict[str, dict[str, Any] | None] = {
    "workspace.json": None,
    "backlog.json": copy.deepcopy(BACKLOG_DEFAULT),
    "store-meta.json": None,
    **{name: copy.deepcopy(value) for name, value in AUXILIARY_DEFAULTS.items()},
    "activity.json": copy.deepcopy(ACTIVITY_DEFAULT),
    "reports.json": copy.deepcopy(REPORTS_DEFAULT),
}

# This build writes exactly the v5 roster. The check is here so a roster edit
# cannot silently teach the historical readers a document their version never
# had.
if frozenset(DEFAULTS) != store_rosters.V5_DOCUMENT_NAMES:
    raise RuntimeError("store defaults no longer match the frozen v5 roster")

JOURNAL_NAME = ".workstack-journal.json"
LOCK_NAME = ".workstack.lock"
SERVER_INFO_NAME = ".workstack-server.json"
CAPTURE_TOKEN_NAME = ".workstack-capture-token"
STORE_MANIFEST_NAME = ".workstack-store-manifest.json"
SYNC_ADOPTION_RECEIPT_NAME = ".workstack-sync-adoption-receipt.json"
SYNC_REBIND_RECEIPT_NAME = ".workstack-sync-rebind-receipt.json"
STORE_MANIFEST_VERSION = 1
# Rollback archives written before an upgrade commits. The directory is not a
# roster member and never becomes one, so an authority never treats its own
# backup as authoritative data.
MIGRATION_BACKUP_DIR = ".workstack-migration-backups"


def _utc_stamp() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _serialized_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _default_for(name: str) -> dict[str, Any]:
    value = DEFAULTS[name]
    if value is not None:
        return copy.deepcopy(value)
    if name == "workspace.json":
        return _workspace_default()
    if name == "store-meta.json":
        return _store_meta_default()
    raise ValueError("unknown dynamic default: {}".format(name))


def _validate_store_manifest_header(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Admit a baseline manifest and report the roster its version implies.

    A released store that has not been upgraded yet carries the manifest its
    own schema wrote, and that manifest is the baseline the upgrade has to
    honour: it names the bytes Work Stack last committed, and refusing it would
    make the owned migration impossible while doing nothing for safety. So a
    manifest is admitted at any collection version this build can validate, and
    judged against *that* version's roster. Schema 4, an unknown version and a
    version newer than this build are refused exactly as before.
    """

    expected = {
        "version",
        "workspace_id",
        "store_schema_version",
        "generation",
        "files",
        "tasks",
    }
    if set(manifest) != expected or manifest.get("version") != STORE_MANIFEST_VERSION:
        raise StoreCorruptError("store manifest schema is invalid")
    if type(manifest.get("generation")) is not int or manifest["generation"] < 0:
        raise StoreCorruptError("store manifest generation is invalid")
    try:
        roster = supported_roster(manifest.get("store_schema_version"))
    except StoreCorruptError as error:
        raise StoreCorruptError("store manifest schema version is invalid") from error
    _canonical_uuid(manifest.get("workspace_id"), "store_manifest.workspace_id")
    return roster


def _validate_store_manifest_files(
    files: Any, roster: tuple[str, ...] | None = None
) -> None:
    """Judge a manifest roster, against this build's unless told otherwise.

    The released callers outside this module ask the only question they have
    ever asked — is this the roster this build writes — so omitting `roster`
    keeps their answer unchanged. The store passes the roster the manifest's
    own version implies, because a baseline left by an older schema is a
    smaller roster and still a valid baseline.
    """

    if roster is None:
        roster = tuple(sorted(DEFAULTS))
    if not isinstance(files, dict) or set(files) != set(roster):
        raise StoreCorruptError("store manifest file roster is invalid")
    if any(
        not isinstance(value, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
        for value in files.values()
    ):
        raise StoreCorruptError("store manifest file digest is invalid")


def _validate_store_manifest_task(task_id: Any, task: Any) -> None:
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"T-[0-9]{4,}", task_id)
        or not isinstance(task, dict)
        or set(task) != {"revision", "digest"}
        or type(task.get("revision")) is not int
        or not 0 <= task["revision"] <= MAX_REVISION
        or not isinstance(task.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", task["digest"])
    ):
        raise StoreCorruptError("store manifest task baseline is invalid")


def _task_semantics(backlog: Any) -> dict[str, dict[str, Any]]:
    """The task baseline a manifest records, computed from one backlog value.

    This is the whole of that computation, so the manifest a commit writes and
    the manifest an upgrade checks are produced by the same rule. It takes the
    decoded backlog rather than reading one, which is what lets a caller judge
    the exact documents it already holds instead of whatever the path says a
    moment later.
    """

    tasks = backlog.get("tasks") if isinstance(backlog, dict) else None
    if not isinstance(tasks, list):
        raise StoreCorruptError("backlog.tasks must be an array")
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise StoreCorruptError("backlog task semantic baseline is invalid")
        result[task["id"]] = {
            "revision": _stored_revision(
                task.get("revision"), "{}.revision".format(task["id"])
            ),
            "digest": "sha256:" + hashlib.sha256(_compact_json(task)).hexdigest(),
        }
    return result


def _validate_store_manifest_tasks(tasks: Any) -> None:
    if not isinstance(tasks, dict):
        raise StoreCorruptError("store manifest task baseline is invalid")
    for task_id, task in tasks.items():
        _validate_store_manifest_task(task_id, task)


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


def _validate_recovery_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise StoreCorruptError("recovery journal created_at is invalid")
    try:
        parsed = dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        raise StoreCorruptError("recovery journal created_at is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreCorruptError("recovery journal created_at must include a timezone")


def _recovery_writes(journal: dict[str, Any]) -> list[dict[str, Any]]:
    if set(journal) != {"version", "operation_id", "created_at", "writes"}:
        raise StoreCorruptError("recovery journal has unknown or missing fields")
    if type(journal["version"]) is not int or journal["version"] != 1:
        raise StoreCorruptError("unsupported recovery journal version")
    operation_id = journal["operation_id"]
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
        raise StoreCorruptError("recovery journal operation_id is invalid")
    _validate_recovery_timestamp(journal["created_at"])
    writes = journal["writes"]
    if not isinstance(writes, list) or not writes:
        raise StoreCorruptError("recovery journal writes must be a non-empty array")
    return writes


def _validate_recovery_write(write: Any, seen: set[str]) -> None:
    if not isinstance(write, dict) or set(write) != {"name", "value", "sha256"}:
        raise StoreCorruptError("recovery journal write entry is invalid")
    name = write["name"]
    if name not in DEFAULTS or name in seen:
        raise StoreCorruptError("recovery journal target is unknown or repeated")
    seen.add(name)
    if not isinstance(write["value"], dict):
        raise StoreCorruptError("recovery journal target value must be an object")
    expected = "sha256:" + hashlib.sha256(_compact_json(write["value"])).hexdigest()
    if not secrets.compare_digest(str(write["sha256"]), expected):
        raise StoreCorruptError("recovery journal value digest mismatch")


class Store:
    def __init__(self, root: Path | str | None = None) -> None:
        configured = root or os.environ.get("WORK_STACK_HOME")
        if configured:
            self.root = Path(configured).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                self.root = (Path(local_app_data) / "WorkStack" / "data").resolve()
            else:
                self.root = (Path.home() / ".local" / "share" / "workstack").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        runtime_override = os.environ.get("WORK_STACK_RUNTIME")
        if runtime_override:
            runtime_base = Path(runtime_override).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                runtime_base = (Path(local_app_data) / "WorkStack" / "runtime").resolve()
            else:
                runtime_base = (Path.home() / ".local" / "state" / "workstack").resolve()
        root_key = hashlib.sha256(os.path.normcase(str(self.root)).encode("utf-8")).hexdigest()[:20]
        self.runtime_root = runtime_base / root_key
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._process_lock = threading.RLock()
        self._local = threading.local()
        self._server_lease: _FileLease | None = None
        self._readiness: StoreReadiness | None = None
        self._generation = 0
        self._sync_state = "in-sync"
        self._sync_fingerprint: str | None = None
        self._event_sequence = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=128)
        self._event_condition = threading.Condition(self._process_lock)
        self._recovered_files: list[str] = []

    @property
    def readiness(self) -> StoreReadiness | None:
        return self._readiness

    @property
    def generation(self) -> int:
        """Process-local committed-store generation for disposable read caches."""

        return self._generation

    @property
    def journal_path(self) -> Path:
        return self.root / JOURNAL_NAME

    @property
    def server_info_path(self) -> Path:
        return self.runtime_root / SERVER_INFO_NAME

    @property
    def capture_token_path(self) -> Path:
        return self.runtime_root / CAPTURE_TOKEN_NAME

    @property
    def store_manifest_path(self) -> Path:
        return self.runtime_root / STORE_MANIFEST_NAME

    @property
    def sync_adoption_receipt_path(self) -> Path:
        return self.runtime_root / SYNC_ADOPTION_RECEIPT_NAME

    @property
    def sync_rebind_receipt_path(self) -> Path:
        return self.runtime_root / SYNC_REBIND_RECEIPT_NAME

    def path(self, name: str) -> Path:
        if name not in DEFAULTS:
            raise ValueError("unknown store: {}".format(name))
        return self.root / name

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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the process lock and the data directory's writer lease."""

        with self._process_lock:
            depth = int(getattr(self._local, "depth", 0))
            temporary_lease: _FileLease | None = None
            if depth == 0 and self._server_lease is None:
                temporary_lease = _FileLease(self.root / LOCK_NAME)
                temporary_lease.acquire()
            try:
                if depth == 0:
                    self._recover_locked()
                elif self.journal_path.exists():
                    raise StoreCorruptError(
                        "a recovery journal is pending; leave the failed transaction before retrying"
                    )
                self._local.depth = depth + 1
                yield
            finally:
                self._local.depth = depth
                if temporary_lease is not None:
                    temporary_lease.release()

    @contextmanager
    def consistent_read(self) -> Iterator[StoreReadiness]:
        """Hold the store lease for a validated read without recovery or migration."""

        with self._process_lock:
            depth = int(getattr(self._local, "depth", 0))
            temporary_lease: _FileLease | None = None
            if depth == 0 and self._server_lease is None:
                temporary_lease = _FileLease(self.root / LOCK_NAME)
                temporary_lease.acquire()
            try:
                if self.journal_path.exists():
                    raise StoreCorruptError(
                        "a recovery journal is pending; snapshot export cannot recover it"
                    )
                readiness = self._validate_ready_state_locked()
                self._readiness = readiness
                self._local.depth = depth + 1
                yield readiness
            finally:
                self._local.depth = depth
                if temporary_lease is not None:
                    temporary_lease.release()

    def try_acquire_writer_lease(self) -> _FileLease | None:
        """Acquire the writer lease once, non-blocking, and retain that handle."""

        with self._process_lock:
            if self._server_lease is not None:
                raise StoreLockedError("this Store already owns the writer lease")
            lease = _FileLease(self.root / LOCK_NAME)
            try:
                lease.acquire()
            except StoreLockedError:
                return None
            self._server_lease = lease
            return lease

    def release_writer_lease(self, handle: _FileLease | None) -> None:
        """Release a retained writer lease exactly once."""

        if handle is None:
            return
        with self._process_lock:
            if self._server_lease is handle:
                self._server_lease = None
                handle.release()
                return
            if handle.file is not None:
                raise ValueError("writer lease handle is not held by this Store")

    @contextmanager
    def server_lease(self) -> Iterator[None]:
        """Hold the only-writer lease for the complete HTTP server lifetime."""

        with self._process_lock:
            if self._server_lease is not None:
                raise StoreLockedError("this Store already owns the server lease")
            lease = _FileLease(self.root / LOCK_NAME)
            lease.acquire()
            self._server_lease = lease
            try:
                self._recover_locked()
            except BaseException:
                self._server_lease = None
                lease.release()
                raise
        try:
            yield
        finally:
            with self._process_lock:
                self._server_lease = None
                lease.release()

    def _read_json_locked(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreCorruptError(
                "invalid JSON preserved at {}; repair or quarantine it before startup".format(path)
            ) from error
        if not isinstance(value, dict):
            raise StoreCorruptError("{} must contain a JSON object".format(path))
        return value

    def load(self, name: str) -> dict[str, Any]:
        path = self.path(name)
        with self.transaction():
            try:
                value = self._read_json_locked(path)
            except FileNotFoundError:
                if any(self.path(candidate).exists() for candidate in DEFAULTS):
                    raise StoreCorruptError(
                        "required store is missing: {}".format(path)
                    )
                return _default_for(name)
            if self._readiness is not None:
                status = self._inspect_sync_locked()
                if status["status"] == "external-change-invalid":
                    raise StoreExternalChangeError(status)
                self._readiness = self._validate_ready_state_locked()
            return value

    def _atomic_write_locked(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            expectations = getattr(self._local, "replace_expectations", {})
            if path.name in expectations:
                expected = expectations[path.name]
                current = (
                    "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    if path.is_file()
                    else None
                )
                if current != expected:
                    raise StoreCorruptError(
                        "local commit target changed before replacement; journal retained"
                    )
            os.replace(str(temporary), str(path))
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_write_text_locked(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary), str(path))
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_write_bytes_locked(self, path: Path, value: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary), str(path))
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, name: str, value: dict[str, Any]) -> None:
        self.path(name)
        if not isinstance(value, dict):
            raise ValueError("store value must be a JSON object")
        self.save_many({name: value})

    def _commit_baseline_locked(
        self, prepared: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, str | None]]:
        baseline = self._read_manifest_locked()
        if baseline is not None:
            expected = dict(baseline["files"])
        else:
            expected = {
                name: "sha256:" + hashlib.sha256(self.path(name).read_bytes()).hexdigest()
                for name in DEFAULTS
                if self.path(name).is_file()
            }
        original = {name: expected.get(name) for name in DEFAULTS}
        for write in prepared:
            expected[write["name"]] = "sha256:" + hashlib.sha256(
                _serialized_json_bytes(write["value"])
            ).hexdigest()
        return baseline, expected, original

    @staticmethod
    def _commit_race_groups(
        actual: Mapping[str, str],
        expected: Mapping[str, str],
        changed_files: list[str],
        has_baseline: bool,
    ) -> tuple[list[str], list[str]]:
        targets = [
            name for name in changed_files if actual.get(name) != expected.get(name)
        ]
        unrelated = [
            name
            for name in DEFAULTS
            if has_baseline
            and name not in changed_files
            and actual.get(name) != expected.get(name)
        ]
        return targets, unrelated

    def _commit_local_with_external_candidate_locked(
        self,
        baseline: Mapping[str, Any],
        expected_hashes: Mapping[str, str],
        changed_files: list[str],
    ) -> None:
        tasks = self._local_baseline_tasks_locked(
            baseline, expected_hashes, changed_files
        )
        self._write_local_baseline_locked(
            baseline, expected_hashes, tasks, changed_files
        )
        self.journal_path.unlink()
        self._inspect_sync_locked()

    def _resolve_late_external_change_locked(
        self,
        baseline: Mapping[str, Any] | None,
        expected_hashes: Mapping[str, str],
        changed_files: list[str],
    ) -> None:
        final_hashes = self._authoritative_hashes_locked()
        targets_match = all(
            final_hashes[name] == expected_hashes[name] for name in changed_files
        )
        if baseline is None or not targets_match:
            raise StoreExternalChangeError(self._inspect_sync_locked())
        self._commit_local_with_external_candidate_locked(
            baseline, expected_hashes, changed_files
        )

    def _commit_prepared_locked(
        self, prepared: list[dict[str, Any]], journal: dict[str, Any]
    ) -> None:
        self._assert_writable_locked()
        if self.journal_path.exists():
            raise StoreCorruptError("refusing to overwrite a pending recovery journal")
        baseline, expected_hashes, original_hashes = self._commit_baseline_locked(prepared)
        self._atomic_write_locked(self.journal_path, journal)
        self._local.replace_expectations = {
            write["name"]: original_hashes[write["name"]] for write in prepared
        }
        try:
            for write in prepared:
                self._atomic_write_locked(self.path(write["name"]), write["value"])
        finally:
            self._local.replace_expectations = {}
        changed_files = sorted(write["name"] for write in prepared)
        targets, unrelated = self._commit_race_groups(
            self._authoritative_hashes_locked(),
            expected_hashes,
            changed_files,
            baseline is not None,
        )
        if targets:
            raise StoreCorruptError(
                "local commit target changed concurrently; recovery journal retained"
            )
        if unrelated and baseline is not None:
            self._commit_local_with_external_candidate_locked(
                baseline, expected_hashes, changed_files
            )
            return
        try:
            self._write_committed_manifest_locked(
                changed_files, expected_hashes=expected_hashes
            )
        except StoreExternalChangeError:
            self._resolve_late_external_change_locked(
                baseline, expected_hashes, changed_files
            )
            return
        self.journal_path.unlink()

    def save_many(
        self,
        writes: Mapping[str, dict[str, Any]],
        operation_id: str | None = None,
    ) -> None:
        """Commit complete target values through a replayable recovery journal."""

        if not writes:
            return
        prepared: list[dict[str, Any]] = []
        for name, value in writes.items():
            self.path(name)
            if not isinstance(value, dict):
                raise ValueError("store value must be a JSON object")
            prepared.append({
                "name": name,
                "value": copy.deepcopy(value),
                "sha256": "sha256:" + hashlib.sha256(_compact_json(value)).hexdigest(),
            })
        journal = {
            "version": 1,
            "operation_id": operation_id or str(uuid.uuid4()),
            "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "writes": prepared,
        }
        with self.transaction():
            self._commit_prepared_locked(prepared, journal)

    def _validate_journal(self, journal: dict[str, Any]) -> list[dict[str, Any]]:
        writes = _recovery_writes(journal)
        seen: set[str] = set()
        for write in writes:
            _validate_recovery_write(write, seen)
        return writes

    def _assert_recovery_targets_safe_locked(
        self, manifest: Mapping[str, Any] | None, writes: list[dict[str, Any]]
    ) -> None:
        """Refuse a replay whose targets hold bytes Work Stack never wrote.

        A target the baseline does not name yet — the tenth document, during an
        interrupted upgrade — is safe only while it is absent or already holds
        exactly what the journal intends. Any other content under that name is
        an unowned change and is refused rather than laundered into a
        completed migration.
        """

        if manifest is None:
            return
        for write in writes:
            path = self.path(write["name"])
            current = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
            intended = "sha256:" + hashlib.sha256(
                _serialized_json_bytes(write["value"])
            ).hexdigest()
            if current not in {manifest["files"].get(write["name"]), intended}:
                raise StoreCorruptError(
                    "recovery target changed outside Work Stack; journal retained"
                )

    @staticmethod
    def _replayable_baseline(
        manifest: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """The baseline a replay may extend, or None when it must be rebuilt.

        A manifest an older schema left behind still names the bytes recovery
        is allowed to replace, and it is used for exactly that. It cannot
        describe the roster the journal completes, so carrying it forward would
        publish a baseline claiming the old version over the new generation.
        The manifest is neither deleted nor ignored: publication is deferred to
        `_finish_initialization_locked`, which rebuilds it from the generation
        that actually committed.
        """

        if manifest is None:
            return None
        if manifest["store_schema_version"] != STORE_SCHEMA_VERSION:
            return None
        return manifest

    def _recover_locked(self) -> None:
        if not self.journal_path.exists():
            return
        journal = self._read_json_locked(self.journal_path)
        writes = self._validate_journal(journal)
        baseline = self._read_manifest_locked()
        self._assert_recovery_targets_safe_locked(baseline, writes)
        manifest = self._replayable_baseline(baseline)
        self._local.replace_expectations = {
            write["name"]: (
                "sha256:" + hashlib.sha256(self.path(write["name"]).read_bytes()).hexdigest()
                if self.path(write["name"]).is_file()
                else None
            )
            for write in writes
        }
        try:
            for write in writes:
                self._atomic_write_locked(self.path(write["name"]), write["value"])
        finally:
            self._local.replace_expectations = {}
        recovered_files = sorted(write["name"] for write in writes)
        if manifest is not None:
            expected_hashes = dict(manifest["files"])
            for write in writes:
                expected_hashes[write["name"]] = "sha256:" + hashlib.sha256(
                    _serialized_json_bytes(write["value"])
                ).hexdigest()
            tasks = self._local_baseline_tasks_locked(
                manifest, expected_hashes, recovered_files
            )
            self._write_local_baseline_locked(
                manifest, expected_hashes, tasks, recovered_files
            )
            self.journal_path.unlink()
            self._inspect_sync_locked()
            self._recovered_files = []
            return
        self.journal_path.unlink()
        self._generation += 1
        self._recovered_files = recovered_files


    def _read_documents_locked(self, roster: tuple[str, ...]) -> dict[str, bytes]:
        """The one acquisition: every document of a roster, read exactly once."""

        bodies: dict[str, bytes] = {}
        for name in roster:
            try:
                bodies[name] = self.path(name).read_bytes()
            except FileNotFoundError as error:
                raise StoreCorruptError(
                    "required store is missing: {}".format(self.path(name))
                ) from error
        return bodies

    @staticmethod
    def _decoded_documents(
        bodies: Mapping[str, bytes],
    ) -> dict[str, dict[str, Any]]:
        """Decode held document bytes through the one shared decoder."""

        return decode_documents(bodies)

    @staticmethod
    def validate_document_values(
        values: object, /, *, schema_version: object
    ) -> StoreReadiness:
        """Judge already-decoded documents as exactly one schema version.

        Opens nothing, initializes nothing and mutates nothing. The archive
        verifier and the ready-state check are both real callers, so a rule
        cannot hold for a live store and not for a backup of one.
        """

        return validate_document_values(values, schema_version=schema_version)

    def _load_required_store_values_locked(self) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for name in DEFAULTS:
            try:
                values[name] = self._read_json_locked(self.path(name))
            except FileNotFoundError as error:
                raise StoreCorruptError(
                    "required store is missing: {}".format(self.path(name))
                ) from error
        return values

    def _validate_ready_state_locked(self) -> StoreReadiness:
        return validate_document_values(
            self._load_required_store_values_locked(),
            schema_version=STORE_SCHEMA_VERSION,
        )

    def _initialize_fresh_locked(self) -> StoreReadiness:
        workspace = _workspace_default()
        fresh = {
            name: (
                workspace
                if name == "workspace.json"
                else _store_meta_default()
                if name == "store-meta.json"
                else _default_for(name)
            )
            for name in DEFAULTS
        }
        self.save_many(fresh, operation_id="store-initialize-v5")
        return self._validate_ready_state_locked()

    @staticmethod
    def _detected_schema(
        existing: set[str], values: Mapping[str, dict[str, Any]]
    ) -> int:
        """Which collection version this directory already is.

        v2 and v3 hold the same nine names, so only the metadata record they
        actually carry separates them; that is the signature the released store
        already used.
        """

        if existing == set(store_rosters.V5_DOCUMENT_NAMES):
            return 5
        if existing == set(store_rosters.V1_DOCUMENT_NAMES):
            if (
                values["workspace.json"].get("version") != 1
                or values["backlog.json"].get("version") != 1
            ):
                raise StoreCorruptError("store migration is partial or missing evidence")
            return 1
        if existing != set(store_rosters.V3_DOCUMENT_NAMES):
            missing = sorted(set(DEFAULTS) - existing)
            raise StoreCorruptError(
                "required store roster is incomplete: {}".format(", ".join(missing))
            )
        metadata = values["store-meta.json"]
        is_v2 = (
            metadata.get("version") == 1
            and metadata.get("store_schema_version") == 2
            and values["backlog.json"].get("version") == 2
            and values["activity.json"].get("version") == 1
        )
        return 2 if is_v2 else 3

    def _migration_backup_dir(self) -> Path:
        return self.root / MIGRATION_BACKUP_DIR

    def _prebackup_target_locked(
        self, detected: int, bodies: Mapping[str, bytes]
    ) -> Path:
        """Where this exact source becomes a rollback archive, deterministically."""

        digest = store_report_migration.source_digest(self._decoded_documents(bodies))
        directory = self._migration_backup_dir()
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise StoreCorruptError("migration backup directory is not a directory")
        target = directory / "workstack-premigration-v{}-{}.zip".format(
            detected, digest[7:23]
        )
        if target.is_symlink():
            raise StoreCorruptError("migration backup path is a symlink")
        return target

    def _verify_prebackup_locked(
        self,
        target: Path,
        detected: int,
        bodies: Mapping[str, bytes],
        readiness: StoreReadiness,
        expected_digest: str,
    ) -> str:
        """Prove the rollback archive from the path it would be rolled back from.

        Verification is the shared read-only archive verifier, which takes one
        image of the path and answers every question from it: the container's
        directory, the manifest header, the roster the detected version
        implies, every member digest, and the documents admitted under that
        *old* schema. Nothing is initialized and no authoritative document is
        re-read, so the archive is judged against the very bytes still held
        from the one acquisition. A promise of a verified rollback that was
        never read back is not a verified rollback.

        The verdict is then bound to `expected_digest`, the digest of the bytes
        this migration packed. Validating one image and rolling back from
        another is the same failure as never reading the archive at all, so the
        image that verified must be the image this attempt produced, and the
        digest that proves it is returned for the journal to revalidate.
        """

        try:
            verified = store_report_migration.verify_archive_file(target)
        except ValueError as error:
            raise StoreCorruptError(
                "migration rollback backup did not verify"
            ) from error
        if (
            verified.store_schema_version != detected
            or verified.workspace_id != readiness.workspace_uid
            or verified.bodies != dict(bodies)
            or not secrets.compare_digest(verified.digest, expected_digest)
        ):
            raise StoreCorruptError("migration rollback backup did not verify")
        return verified.digest

    def _persist_prebackup_locked(
        self, detected: int, bodies: Mapping[str, bytes], readiness: StoreReadiness
    ) -> tuple[Path, str]:
        """Write and verify the rollback archive before the journal exists.

        The name is derived from the detected version and the digest of the
        detected bytes, so a retry after a crash lands on the same file instead
        of leaving a new one behind on every attempt. An existing file is
        reused only when it is byte-identical; anything else present under that
        name is a refusal, never an overwrite. Either way the file is then read
        back and verified, so a reused archive is held to the same contract as
        one this attempt wrote. Any refusal here happens before `save_many`, so
        the old generation and every authoritative byte are left as they were.

        The path and the digest of the image that actually verified are both
        returned, because the journal has to be able to say that the archive it
        is about to depend on is still the one that passed.
        """

        target = self._prebackup_target_locked(detected, bodies)
        packed = store_report_migration.pack_backup_archive(
            dict(bodies),
            workspace_id=readiness.workspace_uid,
            store_schema_version=detected,
            created=dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc),
        )
        if target.exists():
            if target.read_bytes() != packed["body"]:
                raise StoreCorruptError(
                    "an unrelated migration backup already holds this name"
                )
        else:
            self._atomic_write_bytes_locked(target, packed["body"])
        digest = self._verify_prebackup_locked(
            target, detected, bodies, readiness, packed["digest"]
        )
        return target, digest

    def _rebind_prebackup_locked(self, target: Path, verified_digest: str) -> None:
        """Say the archive about to be depended on is still the one that passed.

        Verification held one image and reported its digest; this reads the
        final path once more and refuses unless it is still that same image.
        The journal is what makes the upgrade authoritative, so the last thing
        established before it is that the rollback the journal presumes still
        exists byte for byte at the path a rollback would read.
        """

        try:
            image = target.read_bytes()
        except OSError as error:
            raise StoreCorruptError(
                "migration rollback backup did not verify"
            ) from error
        current = "sha256:" + hashlib.sha256(image).hexdigest()
        if not secrets.compare_digest(current, verified_digest):
            raise StoreCorruptError("migration rollback backup did not verify")

    def _upgrade_locked(
        self,
        detected: int,
        bodies: Mapping[str, bytes],
        values: Mapping[str, dict[str, Any]],
        readiness: StoreReadiness,
    ) -> StoreReadiness:
        digest = store_report_migration.source_digest(values)
        target, verified_digest = self._persist_prebackup_locked(
            detected, bodies, readiness
        )
        writes, operation_id = store_report_migration.plan_upgrade(
            detected, values, digest, now=_utc_stamp()
        )
        self._rebind_prebackup_locked(target, verified_digest)
        self.save_many(writes, operation_id=operation_id)
        return self._validate_ready_state_locked()

    @staticmethod
    def _assert_upgrade_source_bytes_owned(
        manifest: Mapping[str, Any], bodies: Mapping[str, bytes]
    ) -> None:
        """Every held document must still be the byte the manifest recorded."""

        for name, body in bodies.items():
            recorded = manifest["files"].get(name)
            if recorded is None:
                continue
            if recorded != "sha256:" + hashlib.sha256(body).hexdigest():
                raise StoreCorruptError(
                    "upgrade source changed outside Work Stack; "
                    "store left at its detected version"
                )

    @staticmethod
    def _assert_upgrade_source_tasks_owned(
        manifest: Mapping[str, Any], values: Mapping[str, dict[str, Any]]
    ) -> None:
        """The manifest's task baseline must be the one the held backlog means.

        File digests say the bytes are unchanged; they say nothing about the
        baseline recorded beside them. A manifest whose task revisions or task
        digests describe some other backlog is not the baseline of this
        generation, and carrying it into the upgrade would publish revisions
        the held documents never had.

        A backlog whose tasks cannot yield a baseline under this rule is a
        legacy shape that predates the baseline, so it is consistent only with
        a manifest that claims none.
        """

        try:
            expected = _task_semantics(values["backlog.json"])
        except StoreCorruptError:
            expected = {}
        if manifest["tasks"] != expected:
            raise StoreCorruptError(
                "upgrade source task baseline does not match its manifest; "
                "store left at its detected version"
            )

    def _assert_upgrade_source_owned_locked(
        self,
        bodies: Mapping[str, bytes],
        values: Mapping[str, dict[str, Any]],
        readiness: StoreReadiness,
    ) -> None:
        """Refuse to upgrade a generation that already drifted from its baseline.

        The manifest a released build left behind names the workspace Work
        Stack owned, the bytes it last committed and the task baseline those
        bytes meant. All three have to hold. A manifest whose workspace is not
        the one the held documents declare is a baseline for a different store,
        and this migration would silently republish the identity of whichever
        one it happened to read; hash-equal files do not make two workspaces
        the same workspace. When a document this migration just read differs
        from the recorded byte, or the recorded task baseline is not what the
        held backlog means, the difference is an unowned change, and upgrading
        would rewrite it into a new schema, a new journal and new evidence —
        laundering it into accepted history.

        Detection belongs here, before the rollback archive and before the
        journal, so a drifted or foreign store is left exactly as it was found
        and a retry refuses the same way. Every question is answered from
        `bodies` and `values`, the documents this attempt already holds, so the
        generation admitted is the generation judged.
        """

        manifest = self._read_manifest_locked()
        if manifest is None:
            return
        if manifest["store_schema_version"] != readiness.schema_version:
            raise StoreCorruptError(
                "upgrade source manifest schema does not match the detected source; "
                "store left at its detected version"
            )
        if manifest["workspace_id"] != readiness.workspace_uid:
            raise StoreCorruptError(
                "upgrade source manifest belongs to another workspace; "
                "store left at its detected version"
            )
        self._assert_upgrade_source_bytes_owned(manifest, bodies)
        self._assert_upgrade_source_tasks_owned(manifest, values)

    def _admit_or_upgrade_locked(self, existing: set[str]) -> StoreReadiness:
        bodies = self._read_documents_locked(tuple(sorted(existing)))
        values = self._decoded_documents(bodies)
        detected = self._detected_schema(existing, values)
        readiness = validate_document_values(values, schema_version=detected)
        if detected == STORE_SCHEMA_VERSION:
            return readiness
        self._assert_upgrade_source_owned_locked(bodies, values, readiness)
        return self._upgrade_locked(detected, bodies, values, readiness)

    def initialize(self) -> StoreReadiness:
        with self.transaction():
            self._recover_locked()
            existing = {
                name
                for name in store_rosters.V5_DOCUMENT_NAMES
                if self.path(name).exists()
            }
            if not existing:
                self._readiness = self._initialize_fresh_locked()
            else:
                self._readiness = self._admit_or_upgrade_locked(existing)
            return self._finish_initialization_locked()

    def _finish_initialization_locked(self) -> StoreReadiness:
        if self._recovered_files:
            self._write_committed_manifest_locked(self._recovered_files)
            self._recovered_files = []
        self._inspect_sync_locked()
        assert self._readiness is not None
        return self._readiness

    def seed_demo(self, source_root: Path | str) -> bool:
        """Copy tracked demo fixtures only into a wholly empty runtime core."""

        source = Path(source_root).resolve()
        names_and_keys = {
            "backlog.json": "tasks",
            "okr.json": "objectives",
            "worklog.json": "days",
            "notes.json": "notes",
        }
        with self.transaction():
            current = {name: self.load(name) for name in names_and_keys}
            if any(current[name].get(key) for name, key in names_and_keys.items()):
                raise ValueError("demo seed refused because runtime data is not empty")
            fixtures: dict[str, dict[str, Any]] = {}
            for name in names_and_keys:
                fixture_path = source / name
                fixtures[name] = self._read_json_locked(fixture_path)
            fixture_backlog = fixtures["backlog.json"]
            if fixture_backlog.get("version") == 1:
                workspace_uid = validate_workspace(
                    self.load("workspace.json"), 2
                )
                tasks = admitted_tasks(
                    fixture_backlog,
                    workspace_uid,
                    version=1,
                    migrate_legacy=True,
                )
                activity = self.load("activity.json")
                created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                for task in tasks:
                    append_bootstrap(
                        activity,
                        task,
                        created_at=created_at,
                        actor="workstack.seed",
                        provenance="demo.fixture",
                    )
                fixtures["backlog.json"] = {
                    "version": 3,
                    "tasks": tasks,
                }
                fixtures["activity.json"] = activity
            self.save_many(fixtures, operation_id="seed-demo-" + str(uuid.uuid4()))
            self._readiness = self._validate_ready_state_locked()
            return True

    def write_runtime_secret(self, value: str) -> None:
        with self._process_lock:
            self._atomic_write_text_locked(self.capture_token_path, value + "\n")
            try:
                os.chmod(self.capture_token_path, 0o600)
            except OSError:
                pass

    def write_server_info(self, host: str, port: int) -> None:
        with self._process_lock:
            self._atomic_write_locked(
                self.server_info_path, {"version": 1, "host": host, "port": port}
            )

    def clear_server_runtime(self) -> None:
        with self._process_lock:
            self.server_info_path.unlink(missing_ok=True)
            self.capture_token_path.unlink(missing_ok=True)
            try:
                self.runtime_root.rmdir()
            except OSError:
                pass
