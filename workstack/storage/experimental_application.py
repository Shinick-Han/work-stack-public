"""Default-off application adapter for in-process v4 HTTP canaries."""

from __future__ import annotations

import copy
import os
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping

from ..store import (
    CAPTURE_TOKEN_NAME,
    DEFAULTS,
    SERVER_INFO_NAME,
    StoreLockedError,
    StoreReadiness,
)
from .canonical import canonical_json_bytes
from .domain_v4_composition import (
    ExperimentalV4Domain,
    V4DomainCompositionError,
    compose_experimental_v4_domain,
)
from .idempotency import IdempotencyLedgerError, parse_idempotency_ledger
from .manifest_store import RuntimeManifestError, read_runtime_manifest
from .read_repository import RepositoryReadError, V4WorkspaceRepository
from .runtime import RuntimeAuthority
from .lease import StorageLeaseError, StorageWriterLease
from .write_session import FaultHook


class ExperimentalV4ApplicationError(RuntimeError):
    """Content-free refusal at the inactive application adapter boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExperimentalV4Application:
    domain: ExperimentalV4Domain
    store: "ExperimentalV4StoreAdapter"


@dataclass(frozen=True)
class _VirtualDocumentPath:
    size: int

    def stat(self) -> SimpleNamespace:
        return SimpleNamespace(st_size=self.size)


class ExperimentalV4StoreAdapter:
    """Dynamic v3-shaped reads over v4; canonical writes are impossible here."""

    def __init__(
        self,
        domain: ExperimentalV4Domain,
        *,
        enable_v4_application_adapter: bool = False,
        task_note_source_indexes: Mapping[str, int] | None = None,
    ) -> None:
        if enable_v4_application_adapter is not True:
            raise ExperimentalV4ApplicationError("V4_APPLICATION_ADAPTER_OPT_IN_REQUIRED")
        self.domain = domain
        self.root = domain.coordinate.authority_root
        self.runtime_root = domain.coordinate.runtime_root
        self._indexes = copy.deepcopy(task_note_source_indexes)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._server_lease: StorageWriterLease | None = None
        self._documents: dict[str, dict[str, Any]] = {}
        self._document_sizes: dict[str, int] = {}
        self._generation = -1
        self._manifest_digest = ""
        self._readiness: StoreReadiness | None = None
        self._event_sequence = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=128)
        self._event_condition = threading.Condition(self._lock)

    @property
    def capture_token_path(self) -> Path:
        return self.runtime_root / CAPTURE_TOKEN_NAME

    @property
    def server_info_path(self) -> Path:
        return self.runtime_root / SERVER_INFO_NAME

    @property
    def readiness(self) -> StoreReadiness | None:
        with self._lock:
            self._refresh_locked()
            return self._readiness

    @property
    def generation(self) -> int:
        with self._lock:
            self._refresh_locked()
            return self._generation

    def initialize(self) -> StoreReadiness:
        with self._lock:
            self._refresh_locked()
            if self._readiness is None:
                raise ExperimentalV4ApplicationError("V4_APPLICATION_NOT_READY")
            return self._readiness

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            depth = int(getattr(self._local, "depth", 0))
            if depth == 0:
                self._refresh_locked()
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth

    @contextmanager
    def consistent_read(self) -> Iterator[StoreReadiness]:
        with self.transaction():
            if self._readiness is None:
                raise ExperimentalV4ApplicationError("V4_APPLICATION_NOT_READY")
            yield self._readiness

    def load(self, name: str) -> dict[str, Any]:
        if name not in DEFAULTS:
            raise ExperimentalV4ApplicationError("V4_APPLICATION_DOCUMENT_UNKNOWN")
        with self._lock:
            if int(getattr(self._local, "depth", 0)) == 0:
                self._refresh_locked()
            return copy.deepcopy(self._documents[name])

    def path(self, name: str) -> _VirtualDocumentPath:
        if name not in DEFAULTS:
            raise ExperimentalV4ApplicationError("V4_APPLICATION_DOCUMENT_UNKNOWN")
        with self._lock:
            self._refresh_locked()
            return _VirtualDocumentPath(self._document_sizes[name])

    def save(self, _name: str, _value: object) -> None:
        raise ExperimentalV4ApplicationError("V4_ADAPTER_CANONICAL_WRITE_FORBIDDEN")

    def save_many(
        self, _writes: Mapping[str, object], operation_id: str | None = None
    ) -> None:
        del operation_id
        raise ExperimentalV4ApplicationError("V4_ADAPTER_CANONICAL_WRITE_FORBIDDEN")

    @contextmanager
    def server_lease(self) -> Iterator[None]:
        with self._lock:
            if self._server_lease is not None:
                raise StoreLockedError("this adapter already owns the server lease")
            lease = StorageWriterLease(self.runtime_root / "application-server.lock")
            try:
                lease.acquire()
            except StorageLeaseError as error:
                raise StoreLockedError("another Work Stack server owns this runtime") from error
            self._server_lease = lease
        try:
            yield
        finally:
            with self._lock:
                self._server_lease = None
                lease.release()

    def write_runtime_secret(self, value: str) -> None:
        self._atomic_runtime_write(self.capture_token_path, (value + "\n").encode("utf-8"))
        try:
            os.chmod(self.capture_token_path, 0o600)
        except OSError:
            pass

    def write_server_info(self, host: str, port: int) -> None:
        self._atomic_runtime_write(
            self.server_info_path,
            canonical_json_bytes({"version": 1, "host": host, "port": port}),
        )

    def clear_server_runtime(self) -> None:
        with self._lock:
            self.server_info_path.unlink(missing_ok=True)
            self.capture_token_path.unlink(missing_ok=True)

    def sync_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            return {
                "state": "in-sync",
                "workspace_id": self.domain.coordinate.workspace_uid,
                "candidate_workspace_id": self.domain.coordinate.workspace_uid,
                "generation": self._generation,
                "manifest_digest": self._manifest_digest,
                "changed_files": [],
                "reason": None,
                "rebind_available": False,
            }

    def sync_events(self, after: int = 0) -> dict[str, Any]:
        if type(after) is not int or after < 0:
            raise ValueError("event cursor must be a non-negative integer")
        with self._lock:
            self._refresh_locked()
            return {
                "delivery": "bounded-process-local",
                "latest_event_id": self._event_sequence,
                "generation": self._generation,
                "state": "in-sync",
                "events": [copy.deepcopy(item) for item in self._events if item["id"] > after],
            }

    def wait_for_sync_events(self, after: int, timeout: float = 15.0) -> dict[str, Any]:
        if type(after) is not int or after < 0:
            raise ValueError("event cursor must be a non-negative integer")
        if timeout < 0 or timeout > 30:
            raise ValueError("event wait timeout is invalid")
        with self._event_condition:
            self._refresh_locked()
            if self._event_sequence <= after:
                self._event_condition.wait(timeout)
            return self.sync_events(after)

    def _refresh_locked(self) -> None:
        try:
            state = read_runtime_manifest(self.domain.admission.runtime.manifest_path)
            if state is None:
                raise ExperimentalV4ApplicationError("V4_APPLICATION_MANIFEST_MISSING")
            ledger = parse_idempotency_ledger(
                self.domain.admission.runtime.idempotency_path.read_bytes(),
                expected_workspace_uid=self.domain.coordinate.workspace_uid,
            )
            read = V4WorkspaceRepository(
                self.root,
                idempotency_ledger=ledger,
                task_note_source_indexes=self._indexes,
                generation=state.generation,
            ).read()
        except ExperimentalV4ApplicationError:
            raise
        except (OSError, IdempotencyLedgerError, RepositoryReadError, RuntimeManifestError) as error:
            code = str(getattr(error, "code", "V4_APPLICATION_REFRESH_FAILED"))
            raise ExperimentalV4ApplicationError(code) from error
        stamp = read.stamp
        if stamp.workspace_uid != self.domain.coordinate.workspace_uid:
            raise ExperimentalV4ApplicationError("V4_APPLICATION_WORKSPACE_MISMATCH")
        if stamp.authority_manifest_digest != state.manifest.digest:
            raise ExperimentalV4ApplicationError("V4_APPLICATION_AUTHORITY_STALE")
        if (stamp.generation, stamp.authority_manifest_digest) == (
            self._generation,
            self._manifest_digest,
        ):
            return
        documents = read.snapshot.to_v3_documents()
        prior_generation = self._generation
        self._documents = documents
        self._document_sizes = {
            name: len(canonical_json_bytes(value)) for name, value in documents.items()
        }
        self._generation = stamp.generation
        self._manifest_digest = stamp.authority_manifest_digest
        self._readiness = StoreReadiness(
            schema_version=4,
            workspace_uid=stamp.workspace_uid,
            task_count=len(documents["backlog.json"]["tasks"]),
            migration_origin="normalized-v4-canary",
        )
        if prior_generation >= 0:
            self._emit_refresh_event()

    def _emit_refresh_event(self) -> None:
        self._event_sequence += 1
        self._events.append(
            {
                "id": self._event_sequence,
                "type": "store-committed",
                "workspace_id": self.domain.coordinate.workspace_uid,
                "generation": self._generation,
                "changed_files": sorted(DEFAULTS),
            }
        )
        self._event_condition.notify_all()

    def _atomic_runtime_write(self, path: Path, body: bytes) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)


def create_experimental_v4_application(
    authority_root: Path | str,
    runtime: RuntimeAuthority | None,
    *,
    enable_v4_application: bool = False,
    clock: Callable[[], str],
    uid_factory: Callable[[], str],
    today: Callable[[], str] | None = None,
    projection_root: Path | str | None = None,
    task_note_source_indexes: Mapping[str, int] | None = None,
    fault_hook: FaultHook | None = None,
) -> ExperimentalV4Application:
    """Compose an HTTP-capable v4 canary without touching released startup."""

    if enable_v4_application is not True:
        raise ExperimentalV4ApplicationError("V4_APPLICATION_OPT_IN_REQUIRED")
    try:
        domain = compose_experimental_v4_domain(
            authority_root,
            runtime,
            enable_v4_domain=True,
            clock=clock,
            uid_factory=uid_factory,
            today=today,
            projection_root=projection_root,
            task_note_source_indexes=task_note_source_indexes,
            fault_hook=fault_hook,
        )
    except V4DomainCompositionError as error:
        raise ExperimentalV4ApplicationError(error.code) from error
    store = ExperimentalV4StoreAdapter(
        domain,
        enable_v4_application_adapter=True,
        task_note_source_indexes=task_note_source_indexes,
    )
    return ExperimentalV4Application(domain, store)
