"""Crash-safe JSON storage with one lock shared by every writer."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .planning_status import (
    PlanningStatusValidationError,
    append_bootstrap,
    validate_and_project,
)


STORE_SCHEMA_VERSION = 3
MAX_REVISION = 9_007_199_254_740_991
IDENTITY_STORES = ("workspace.json", "backlog.json", "store-meta.json", "activity.json")


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
        },
    }


DEFAULTS: dict[str, dict[str, Any] | None] = {
    "workspace.json": None,
    "backlog.json": {"version": 3, "tasks": []},
    "store-meta.json": None,
    "okr.json": {"version": 1, "objectives": []},
    "worklog.json": {"version": 1, "days": {}},
    "notes.json": {"version": 1, "notes": []},
    "captures.json": {"version": 1, "captures": []},
    "replies.json": {"version": 1, "replies": []},
    "activity.json": {
        "version": 2,
        "activity": [],
        "idempotency": [],
        "planning_status": [],
    },
}

JOURNAL_NAME = ".workstack-journal.json"
LOCK_NAME = ".workstack.lock"
SERVER_INFO_NAME = ".workstack-server.json"
CAPTURE_TOKEN_NAME = ".workstack-capture-token"
STORE_MANIFEST_NAME = ".workstack-store-manifest.json"
STORE_MANIFEST_VERSION = 1


class StoreLockedError(OSError):
    """Raised when another process owns the data-directory writer lease."""


class StoreCorruptError(ValueError):
    """Raised when persisted state cannot be safely interpreted."""


class StoreExternalChangeError(RuntimeError):
    """Raised when an unowned SSOT change freezes normal mutations."""

    def __init__(self, status: Mapping[str, Any]) -> None:
        super().__init__(
            "authoritative store changed outside Work Stack; review synchronization status"
        )
        self.status = dict(status)


@dataclass(frozen=True)
class StoreReadiness:
    schema_version: int
    workspace_uid: str
    task_count: int
    migration_origin: str


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StoreCorruptError("{} must be a canonical UUID string".format(label))
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise StoreCorruptError("{} must be a canonical UUID string".format(label)) from error
    if parsed.int == 0 or str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise StoreCorruptError(
            "{} must be a non-nil lowercase canonical RFC 4122 UUID".format(label)
        )
    return value


def _stored_revision(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise StoreCorruptError(
            "{} must be an integer between 0 and {}".format(label, MAX_REVISION)
        )
    return value


def _default_for(name: str) -> dict[str, Any]:
    value = DEFAULTS[name]
    if value is not None:
        return copy.deepcopy(value)
    if name == "workspace.json":
        return _workspace_default()
    if name == "store-meta.json":
        return _store_meta_default()
    raise ValueError("unknown dynamic default: {}".format(name))


def _compact_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _validate_auxiliary_store(name: str, value: dict[str, Any]) -> None:
    expected = DEFAULTS[name]
    if expected is None or name in IDENTITY_STORES:
        raise ValueError("auxiliary store validator received an identity store")
    if set(value) != set(expected) or value.get("version") != expected["version"]:
        raise StoreCorruptError("{} schema is invalid".format(name))
    for key, default_value in expected.items():
        if key == "version":
            continue
        if isinstance(default_value, list) and not isinstance(value.get(key), list):
            raise StoreCorruptError("{}.{} must be an array".format(name, key))
        if isinstance(default_value, dict) and not isinstance(value.get(key), dict):
            raise StoreCorruptError("{}.{} must be an object".format(name, key))


class _FileLease:
    """Small non-blocking cross-platform exclusive file lease."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as error:
            handle.close()
            raise StoreLockedError(
                "the Work Stack data directory is already owned by another writer"
            ) from error
        self.file = handle

    def release(self) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


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
        if manifest.get("store_schema_version") != STORE_SCHEMA_VERSION:
            raise StoreCorruptError("store manifest schema version is invalid")
        _canonical_uuid(manifest.get("workspace_id"), "store_manifest.workspace_id")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(DEFAULTS):
            raise StoreCorruptError("store manifest file roster is invalid")
        if any(not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in files.values()):
            raise StoreCorruptError("store manifest file digest is invalid")
        tasks = manifest.get("tasks")
        if not isinstance(tasks, dict):
            raise StoreCorruptError("store manifest task baseline is invalid")
        for task_id, task in tasks.items():
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
        return manifest

    def _manifest_digest(self, manifest: Mapping[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(_compact_json(manifest)).hexdigest()

    def _task_semantics_locked(self) -> dict[str, dict[str, Any]]:
        backlog = self._read_json_locked(self.path("backlog.json"))
        tasks = backlog.get("tasks")
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

    def _write_committed_manifest_locked(
        self, changed_files: list[str], *, event_type: str = "store.committed"
    ) -> None:
        hashes_before = self._authoritative_hashes_locked()
        tasks_before = self._task_semantics_locked()
        readiness = self._validate_ready_state_locked()
        hashes_after = self._authoritative_hashes_locked()
        tasks_after = self._task_semantics_locked()
        if hashes_before != hashes_after or tasks_before != tasks_after:
            raise StoreCorruptError("authoritative store changed while committing its manifest")
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

    def _inspect_sync_locked(self) -> dict[str, Any]:
        manifest = self._read_manifest_locked()
        if manifest is None:
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

        self._generation = max(self._generation, manifest["generation"])
        changed_files: list[str]
        validation_error: str | None = None
        candidate_workspace_id = manifest["workspace_id"]
        try:
            current_hashes = self._authoritative_hashes_locked()
            changed_files = sorted(
                name for name in DEFAULTS if current_hashes[name] != manifest["files"][name]
            )
            if changed_files:
                readiness = self._validate_ready_state_locked()
                candidate_workspace_id = readiness.workspace_uid
                if readiness.workspace_uid != manifest["workspace_id"]:
                    raise StoreCorruptError("external candidate workspace identity changed")
                candidate_tasks = self._task_semantics_locked()
                baseline_tasks = manifest["tasks"]
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
                if self._authoritative_hashes_locked() != current_hashes:
                    raise StoreCorruptError("external candidate changed during validation")
        except StoreCorruptError as error:
            current_hashes = {}
            changed_files = sorted(
                name for name in DEFAULTS
                if not self.path(name).is_file()
                or manifest["files"].get(name)
                != ("sha256:" + hashlib.sha256(self.path(name).read_bytes()).hexdigest())
            )
            validation_error = str(error)

        if validation_error is not None:
            state = "external-change-invalid"
        elif changed_files:
            state = "external-change-detected"
        else:
            state = "in-sync"
        fingerprint = "{}:{}".format(
            state,
            hashlib.sha256(_compact_json({"files": current_hashes, "changed": changed_files})).hexdigest(),
        )
        if state != "in-sync" and fingerprint != self._sync_fingerprint:
            self._emit_event_locked("store." + state, candidate_workspace_id, changed_files)
        self._sync_state = state
        self._sync_fingerprint = None if state == "in-sync" else fingerprint
        return {
            "status": state,
            "writes_allowed": state == "in-sync",
            "workspace_id": manifest["workspace_id"],
            "store_schema_version": manifest["store_schema_version"],
            "generation": manifest["generation"],
            "manifest_digest": self._manifest_digest(manifest),
            "files": copy.deepcopy(manifest["files"]),
            "changed_files": changed_files,
            "validation_error": validation_error,
            "candidate_digest": (
                "sha256:"
                + hashlib.sha256(
                    _compact_json(
                        {
                            "workspace_id": candidate_workspace_id,
                            "store_schema_version": manifest["store_schema_version"],
                            "generation": manifest["generation"],
                            "files": current_hashes,
                            "tasks": (
                                self._task_semantics_locked()
                                if validation_error is None
                                else {}
                            ),
                        }
                    )
                ).hexdigest()
                if validation_error is None and changed_files
                else None
            ),
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
                "generation": status["generation"],
                "manifest_digest": status["candidate_digest"] or status["manifest_digest"],
                "changed_files": status["changed_files"],
                "reason": (
                    "authoritative store candidate failed validation"
                    if status["validation_error"] is not None
                    else None
                ),
            }

    def adopt_external_change(
        self, expected_generation: int, expected_manifest_digest: str
    ) -> dict[str, Any]:
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("expected_generation must be a non-negative integer")
        if not isinstance(expected_manifest_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_manifest_digest
        ):
            raise ValueError("manifest_digest is invalid")
        with self.transaction():
            status = self._inspect_sync_locked()
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

    @staticmethod
    def _validate_workspace(value: dict[str, Any], version: int) -> str:
        if set(value) != {"version", "id", "name"} or value.get("version") != version:
            raise StoreCorruptError("workspace identity schema is invalid")
        workspace_uid = _canonical_uuid(value.get("id"), "workspace.id")
        if not isinstance(value.get("name"), str) or not value["name"].strip():
            raise StoreCorruptError("workspace.name must be a non-empty string")
        return workspace_uid

    @staticmethod
    def _validate_task_identities(
        backlog: dict[str, Any],
        workspace_uid: str,
        *,
        version: int,
        migrate_legacy: bool = False,
    ) -> list[dict[str, Any]]:
        if set(backlog) != {"version", "tasks"} or backlog.get("version") != version:
            raise StoreCorruptError("backlog identity schema is invalid")
        tasks = backlog.get("tasks")
        if not isinstance(tasks, list):
            raise StoreCorruptError("backlog.tasks must be an array")
        seen_ids: set[str] = set()
        seen_uids: set[str] = {workspace_uid}
        migrated: list[dict[str, Any]] = []
        for index, source in enumerate(tasks):
            label = "backlog.tasks[{}]".format(index)
            if not isinstance(source, dict):
                raise StoreCorruptError("{} must be an object".format(label))
            task_id = source.get("id")
            if not isinstance(task_id, str) or not re.fullmatch(r"T-[0-9]{4,}", task_id):
                raise StoreCorruptError("{}.id is invalid".format(label))
            if task_id in seen_ids:
                raise StoreCorruptError("duplicate task id: {}".format(task_id))
            seen_ids.add(task_id)
            task = copy.deepcopy(source)
            if "uid" in task:
                task_uid = _canonical_uuid(task["uid"], "{}.uid".format(label))
            elif migrate_legacy:
                task_uid = str(uuid.uuid5(uuid.UUID(workspace_uid), task_id))
                task["uid"] = task_uid
            else:
                raise StoreCorruptError("{}.uid is missing".format(label))
            if task_uid in seen_uids:
                raise StoreCorruptError("duplicate persisted UUID: {}".format(task_uid))
            seen_uids.add(task_uid)
            if "revision" in task:
                _stored_revision(task["revision"], "{}.revision".format(label))
            elif migrate_legacy:
                task["revision"] = 0
            else:
                raise StoreCorruptError("{}.revision is missing".format(label))
            if version == 3:
                status_fact_id = task.get("status_fact_id")
                if not isinstance(status_fact_id, str) or not re.fullmatch(
                    r"PS-[0-9]{6,}", status_fact_id
                ):
                    raise StoreCorruptError("{}.status_fact_id is invalid".format(label))
            migrated.append(task)
        return migrated

    def _validate_ready_state_locked(self) -> StoreReadiness:
        values: dict[str, dict[str, Any]] = {}
        for name in DEFAULTS:
            try:
                values[name] = self._read_json_locked(self.path(name))
            except FileNotFoundError as error:
                raise StoreCorruptError(
                    "required store is missing: {}".format(self.path(name))
                ) from error
        workspace_uid = self._validate_workspace(values["workspace.json"], 2)
        tasks = self._validate_task_identities(
            values["backlog.json"], workspace_uid, version=3
        )
        metadata = values["store-meta.json"]
        if set(metadata) != {"version", "store_schema_version", "migrations"}:
            raise StoreCorruptError("store metadata has unknown or missing fields")
        if metadata.get("version") != 2:
            raise StoreCorruptError("store metadata version is unsupported")
        schema_version = metadata.get("store_schema_version")
        if schema_version != STORE_SCHEMA_VERSION:
            if type(schema_version) is int and schema_version > STORE_SCHEMA_VERSION:
                raise StoreCorruptError("store schema is newer than this Work Stack build")
            raise StoreCorruptError("store schema version is invalid")
        migrations = metadata.get("migrations")
        if not isinstance(migrations, dict) or set(migrations) != {
            "identity",
            "planning_status",
        }:
            raise StoreCorruptError("store migration evidence is invalid")
        identity = migrations.get("identity")
        planning = migrations.get("planning_status")
        expected_evidence = {"id", "origin", "source_sha256"}
        if (
            not isinstance(identity, dict)
            or set(identity) != expected_evidence
            or not isinstance(planning, dict)
            or set(planning) != expected_evidence
        ):
            raise StoreCorruptError("store migration evidence is invalid")
        origin = identity.get("origin")
        source_sha256 = identity.get("source_sha256")
        if origin == "fresh":
            if identity.get("id") != "workstack.store.v2" or source_sha256 is not None:
                raise StoreCorruptError("fresh store migration evidence is invalid")
        elif origin == "migrated_v1":
            if identity.get("id") != "workstack.store.v1-to-v2" or not (
                isinstance(source_sha256, str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", source_sha256)
            ):
                raise StoreCorruptError("v1 migration evidence is invalid")
        else:
            raise StoreCorruptError("store migration origin is invalid")
        planning_origin = planning.get("origin")
        planning_digest = planning.get("source_sha256")
        if planning.get("id") != "workstack.planning-status.v1":
            raise StoreCorruptError("planning-status migration evidence is invalid")
        if planning_origin == "fresh":
            if planning_digest is not None:
                raise StoreCorruptError("fresh planning-status evidence is invalid")
        elif planning_origin in {"migrated_v1", "migrated_v2"}:
            if not (
                isinstance(planning_digest, str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", planning_digest)
            ):
                raise StoreCorruptError("planning-status migration evidence is invalid")
        else:
            raise StoreCorruptError("planning-status migration origin is invalid")
        for name in DEFAULTS:
            if name not in IDENTITY_STORES:
                _validate_auxiliary_store(name, values[name])
        activity = values["activity.json"]
        expected_activity = DEFAULTS["activity.json"]
        if (
            not isinstance(expected_activity, dict)
            or set(activity) != set(expected_activity)
            or activity.get("version") != 2
            or not isinstance(activity.get("activity"), list)
            or not isinstance(activity.get("idempotency"), list)
            or not isinstance(activity.get("planning_status"), list)
        ):
            raise StoreCorruptError("activity.json schema is invalid")
        try:
            validate_and_project(values["backlog.json"], activity)
        except PlanningStatusValidationError as error:
            raise StoreCorruptError(str(error)) from error
        return StoreReadiness(
            schema_version=STORE_SCHEMA_VERSION,
            workspace_uid=workspace_uid,
            task_count=len(tasks),
            migration_origin=origin,
        )

    def _migrate_v1_locked(
        self,
        workspace: dict[str, Any],
        backlog: dict[str, Any],
        legacy_values: Mapping[str, dict[str, Any]],
    ) -> StoreReadiness:
        workspace_uid = self._validate_workspace(workspace, 1)
        tasks = self._validate_task_identities(
            backlog, workspace_uid, version=1, migrate_legacy=True
        )
        source = {
            name: legacy_values[name]
            for name in DEFAULTS
            if name != "store-meta.json"
        }
        source_sha256 = "sha256:" + hashlib.sha256(_compact_json(source)).hexdigest()
        migrated_workspace = copy.deepcopy(workspace)
        migrated_workspace["version"] = 2
        migrated_activity = copy.deepcopy(legacy_values["activity.json"])
        if set(migrated_activity) != {"version", "activity", "idempotency"} or migrated_activity.get("version") != 1:
            raise StoreCorruptError("legacy activity schema is invalid")
        migrated_activity["version"] = 2
        migrated_activity["planning_status"] = []
        created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for task in tasks:
            append_bootstrap(
                migrated_activity,
                task,
                created_at=created_at,
                actor="workstack.migration",
                provenance="store.v1",
            )
        migrated_backlog = {"version": 3, "tasks": tasks}
        metadata = {
            "version": 2,
            "store_schema_version": STORE_SCHEMA_VERSION,
            "migrations": {
                "identity": {
                    "id": "workstack.store.v1-to-v2",
                    "origin": "migrated_v1",
                    "source_sha256": source_sha256,
                },
                "planning_status": {
                    "id": "workstack.planning-status.v1",
                    "origin": "migrated_v1",
                    "source_sha256": source_sha256,
                },
            },
        }
        writes = {
            name: copy.deepcopy(value)
            for name, value in legacy_values.items()
            if name != "store-meta.json"
        }
        writes.update({
            "workspace.json": migrated_workspace,
            "backlog.json": migrated_backlog,
            "activity.json": migrated_activity,
            "store-meta.json": metadata,
        })
        self.save_many(
            writes,
            operation_id="store-migrate-v1-v3-{}".format(source_sha256[7:23]),
        )
        return self._validate_ready_state_locked()

    def _migrate_v2_locked(
        self, values: Mapping[str, dict[str, Any]]
    ) -> StoreReadiness:
        workspace_uid = self._validate_workspace(values["workspace.json"], 2)
        tasks = self._validate_task_identities(
            values["backlog.json"], workspace_uid, version=2
        )
        metadata_v2 = values["store-meta.json"]
        if (
            set(metadata_v2) != {"version", "store_schema_version", "migration"}
            or metadata_v2.get("version") != 1
            or metadata_v2.get("store_schema_version") != 2
            or not isinstance(metadata_v2.get("migration"), dict)
        ):
            raise StoreCorruptError("v2 store migration evidence is invalid")
        identity = copy.deepcopy(metadata_v2["migration"])
        if set(identity) != {"id", "origin", "source_sha256"}:
            raise StoreCorruptError("v2 store migration evidence is invalid")
        if identity.get("origin") == "fresh":
            if identity.get("id") != "workstack.store.v2" or identity.get("source_sha256") is not None:
                raise StoreCorruptError("v2 identity evidence is invalid")
        elif identity.get("origin") == "migrated_v1":
            if identity.get("id") != "workstack.store.v1-to-v2" or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(identity.get("source_sha256", ""))
            ):
                raise StoreCorruptError("v2 identity evidence is invalid")
        else:
            raise StoreCorruptError("v2 identity evidence is invalid")

        activity = copy.deepcopy(values["activity.json"])
        if (
            set(activity) != {"version", "activity", "idempotency"}
            or activity.get("version") != 1
            or not isinstance(activity.get("activity"), list)
            or not isinstance(activity.get("idempotency"), list)
        ):
            raise StoreCorruptError("v2 activity schema is invalid")
        for name in DEFAULTS:
            if name not in {"workspace.json", "backlog.json", "store-meta.json", "activity.json"}:
                _validate_auxiliary_store(name, values[name])
        source_sha256 = "sha256:" + hashlib.sha256(_compact_json(dict(values))).hexdigest()
        activity["version"] = 2
        activity["planning_status"] = []
        created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for task in tasks:
            append_bootstrap(
                activity,
                task,
                created_at=created_at,
                actor="workstack.migration",
                provenance="store.v2",
            )
        backlog = {"version": 3, "tasks": tasks}
        metadata = {
            "version": 2,
            "store_schema_version": STORE_SCHEMA_VERSION,
            "migrations": {
                "identity": identity,
                "planning_status": {
                    "id": "workstack.planning-status.v1",
                    "origin": "migrated_v2",
                    "source_sha256": source_sha256,
                },
            },
        }
        self.save_many(
            {
                "backlog.json": backlog,
                "activity.json": activity,
                "store-meta.json": metadata,
            },
            operation_id="store-migrate-v2-v3-{}".format(source_sha256[7:23]),
        )
        return self._validate_ready_state_locked()

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

    def save(self, name: str, value: dict[str, Any]) -> None:
        self.path(name)
        if not isinstance(value, dict):
            raise ValueError("store value must be a JSON object")
        self.save_many({name: value})

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
            self._assert_writable_locked()
            if self.journal_path.exists():
                raise StoreCorruptError(
                    "refusing to overwrite a pending recovery journal"
                )
            self._atomic_write_locked(self.journal_path, journal)
            for write in prepared:
                self._atomic_write_locked(self.path(write["name"]), write["value"])
            self._write_committed_manifest_locked(
                sorted(write["name"] for write in prepared)
            )
            self.journal_path.unlink()

    def _validate_journal(self, journal: dict[str, Any]) -> list[dict[str, Any]]:
        if set(journal) != {"version", "operation_id", "created_at", "writes"}:
            raise StoreCorruptError("recovery journal has unknown or missing fields")
        if type(journal["version"]) is not int or journal["version"] != 1:
            raise StoreCorruptError("unsupported recovery journal version")
        if (
            not isinstance(journal["operation_id"], str)
            or not 1 <= len(journal["operation_id"]) <= 200
        ):
            raise StoreCorruptError("recovery journal operation_id is invalid")
        if not isinstance(journal["created_at"], str) or not journal["created_at"]:
            raise StoreCorruptError("recovery journal created_at is invalid")
        try:
            created_at = journal["created_at"]
            parsed = dt.datetime.fromisoformat(
                created_at[:-1] + "+00:00" if created_at.endswith("Z") else created_at
            )
        except ValueError as error:
            raise StoreCorruptError("recovery journal created_at is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise StoreCorruptError("recovery journal created_at must include a timezone")
        writes = journal["writes"]
        if not isinstance(writes, list) or not writes:
            raise StoreCorruptError("recovery journal writes must be a non-empty array")
        seen: set[str] = set()
        for write in writes:
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
        return writes

    def _recover_locked(self) -> None:
        if not self.journal_path.exists():
            return
        journal = self._read_json_locked(self.journal_path)
        writes = self._validate_journal(journal)
        for write in writes:
            self._atomic_write_locked(self.path(write["name"]), write["value"])
        self.journal_path.unlink()
        self._generation += 1
        self._recovered_files = sorted(write["name"] for write in writes)

    def initialize(self) -> StoreReadiness:
        with self.transaction():
            self._recover_locked()
            existing = {name for name in DEFAULTS if self.path(name).exists()}
            if not existing:
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
                self.save_many(fresh, operation_id="store-initialize-v3")
                self._readiness = self._validate_ready_state_locked()
                if self._recovered_files:
                    self._write_committed_manifest_locked(self._recovered_files)
                    self._recovered_files = []
                self._inspect_sync_locked()
                return self._readiness

            required_legacy = set(DEFAULTS) - {"store-meta.json"}
            if existing not in (set(DEFAULTS), required_legacy):
                missing = sorted(set(DEFAULTS) - existing)
                raise StoreCorruptError(
                    "required store roster is incomplete: {}".format(", ".join(missing))
                )
            values = {
                name: self._read_json_locked(self.path(name))
                for name in existing
            }
            for name in required_legacy - set(IDENTITY_STORES):
                _validate_auxiliary_store(name, values[name])
            workspace = values["workspace.json"]
            backlog = values["backlog.json"]
            metadata_exists = "store-meta.json" in existing
            if not metadata_exists:
                if workspace.get("version") != 1 or backlog.get("version") != 1:
                    raise StoreCorruptError("store migration is partial or missing evidence")
                self._readiness = self._migrate_v1_locked(workspace, backlog, values)
            else:
                metadata = values["store-meta.json"]
                if (
                    metadata.get("version") == 1
                    and metadata.get("store_schema_version") == 2
                    and backlog.get("version") == 2
                    and values["activity.json"].get("version") == 1
                ):
                    self._readiness = self._migrate_v2_locked(values)
                else:
                    self._readiness = self._validate_ready_state_locked()
            if self._recovered_files:
                self._write_committed_manifest_locked(self._recovered_files)
                self._recovered_files = []
            self._inspect_sync_locked()
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
                workspace_uid = self._validate_workspace(
                    self.load("workspace.json"), 2
                )
                tasks = self._validate_task_identities(
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
