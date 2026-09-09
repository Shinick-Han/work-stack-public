"""Crash-safe JSON storage with one lock shared by every writer.

``Store`` is the whole of that guarantee: one writer lease per data directory,
one reentrant transaction depth per thread, one atomic replace that every write
goes through, and one commit that turns prepared values into a recovery journal,
the documents and the committed manifest. Those primitives stay here because
everything else is a question asked *of* them.

The domain answers are composed on as mixins -- external-change detection and
adoption, workspace-identity rebind, recovery-journal replay, and schema
detection and upgrade. Composition rather than delegation is deliberate: a
collaborator reached through ``self`` shares this instance's lease, transaction
depth and event sequence, so there is still exactly one of each per Store, and a
test that patches a Store method still intercepts every internal caller of it.
"""

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

# The layout, the record validators and the four domain collaborators. The names
# re-exported here are the module surface released callers already import from
# ``workstack.store``; they keep resolving from this module after the split.
from .store_layout import (
    CAPTURE_TOKEN_NAME,
    CHANGE_NOTICE_TYPE,
    DEFAULTS,
    JOURNAL_NAME,
    LOCK_NAME,
    MAX_COMMIT_EVENTS,
    MIGRATION_BACKUP_DIR,
    SERVER_INFO_NAME,
    STORE_MANIFEST_NAME,
    STORE_MANIFEST_VERSION,
    STORE_SCHEMA_VERSION,
    SYNC_ADOPTION_RECEIPT_NAME,
    SYNC_REBIND_RECEIPT_NAME,
    _WORKSPACE_OPTIONAL_KEYS,
    _WORKSPACE_REQUIRED_KEYS,
    _default_for,
    _serialized_json_bytes,
    _store_meta_default,
    _utc_stamp,
    _workspace_default,
)
from .store_manifest_validation import (
    _task_semantics,
    _validate_recovery_timestamp,
    _validate_store_manifest_files,
    _validate_store_manifest_header,
    _validate_store_manifest_task,
    _validate_store_manifest_tasks,
)
from .store_rebind import (
    StoreRebindMixin,
    _validated_rebind_artifact_name,
    _validated_rebind_file_records,
)
from .store_recovery import (
    StoreRecoveryMixin,
    _recovery_writes,
    _validate_recovery_write,
)
from .store_capture_observations import StoreCaptureObservationMixin
from .store_schema_upgrade import StoreSchemaUpgradeMixin
from .store_sync import StoreSyncMixin


class Store(
    StoreSyncMixin,
    StoreRebindMixin,
    StoreRecoveryMixin,
    StoreSchemaUpgradeMixin,
    StoreCaptureObservationMixin,
):
    """The single writer: its lease, its transaction and its atomic writes.

    Every collaborator reaches the store through this instance, so the lease is
    acquired once, ``transaction`` still nests by depth on the same thread-local
    counter, and the bounded event sequence stays the one sequence a client
    cursors over.
    """

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
        self.save_many(fresh, operation_id="store-initialize-v6")
        return self._validate_ready_state_locked()

    def initialize(self) -> StoreReadiness:
        with self.transaction():
            self._recover_locked()
            existing = {
                name
                for name in store_rosters.V6_DOCUMENT_NAMES
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
