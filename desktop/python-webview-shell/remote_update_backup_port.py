"""The update flow's real ``BackupPort``, backed by the remote maintenance helper.

``remote_update_flow`` asks a ``BackupPort`` for four things: create a verified
backup, observe that same backup, restore it, and observe that same restore.
This module answers all four by running the frozen remote adapter
(``remote_update_maintenance.py``) once per call over the bounded SSH exchange
in ``remote_update_maintenance_transport``.  There is no second archive
verifier here, no hand-rolled unpacking, no credential read, no daemon, and no
raw SSOT write: every effect is the adapter's, which is in turn one of the
supported ``workstack.maintenance`` calls taking the store's own writer lease.

**One identity, four calls.**  The adapter binds an attempt receipt to the
operation before any effect runs, and it derives that binding from the request,
so ``create_verified(op)`` and ``observe(op)`` name the same durable operation
and so do ``restore(op)`` and ``observe_restore(op)``.  This port never invents
a second identity for work that may already be out.

**Refusal is pre-effect or it is not a refusal.**  ``PortRefusal`` is raised
only where nothing can have happened: a target or identity that will not
validate, a restore with no verified source to restore from, a channel that was
never started or never accepted the payload, and any failure of the read-only
preflight observation each mutation runs first.  That preflight is what makes
an absent helper honest -- a remote application directory installed before this
adapter existed answers nothing, and the port refuses the mutation outright
instead of leaving the flow a phantom identity to reconcile forever.  From the
moment the mutating request has left, everything else is ``LostResponse`` on
the same operation: a lost channel, an unrecognised reply, a helper answer of
``unknown``, and any answer carrying ``attempted`` -- the adapter's own word
that the operation is durably bound and its completion is not established.

**What a restore does and does not establish.**  ``maintenance.restore_store``
writes the archive's documents through the current upgrade planner, so a
returned restore leaves the destination at *this* build's schema whatever the
archive held.  The adapter reports that honestly as
``rollback_compatible: false`` with ``pre_migration_data`` unset, and this port
preserves it: ``RestoreOutcome.pre_migration_data`` stays ``None``.  The flow's
``classify_backup_restore`` requires a true answer there before it will call a
restore verified, so a real restore through this port settles as
``restore_unknown``, never ``restore_verified``.  That is the correct reading --
the pre-migration data is demonstrably *not* back -- and this port will not
manufacture the answer that would hide it.

Nothing that leaves this module carries a path, an argv token, a host alias, an
interpreter, or any adapter text: only the typed outcomes the flow contract
defines and bounded symbolic codes drawn from a closed set.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_update_flow_contract import (  # noqa: E402
    BackupOutcome,
    LostResponse,
    PortRefusal,
    RestoreOutcome,
)
from remote_update_maintenance_transport import (  # noqa: E402
    CODE_CHANNEL_LOST,
    CODE_CHANNEL_NOT_STARTED,
    CODE_CHANNEL_NO_STDIN,
    CODE_REPLY_UNRECOGNISED,
    CODE_REQUEST_UNBUILDABLE,
    CODE_TARGET_INVALID,
    DEFAULT_TIMEOUT_SECONDS,
    EFFECT_NONE,
    KIND_CREATE_BACKUP,
    KIND_OBSERVE_BACKUP,
    KIND_OBSERVE_RESTORE,
    KIND_RESTORE_BACKUP,
    SOURCED_KINDS,
    MaintenanceReply,
    MaintenanceTarget,
    MaintenanceTargetError,
    build_request,
    exchange,
    validated_digest,
    validated_target,
    validated_timeout,
)

STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"
STATUS_NOT_RUN = "not_run"
STATUS_UNKNOWN = "unknown"

#: The adapter's own codes that say an operation has no durable binding at all,
#: so the identity is settled as never run rather than kept open.
CODE_BACKUP_NOT_RUN = "backup_not_run"
CODE_RESTORE_NOT_RUN = "restore_not_run"

#: A restore needs the identity and digest of the archive it restores.  Losing
#: that is a pre-effect refusal, never a reason to restore something else.
CODE_RESTORE_SOURCE_UNKNOWN = "restore_source_unknown"
#: A helper answer whose code is outside the closed set below.
CODE_UNRECOGNISED = "maintenance_code_unrecognised"

#: Every code this port may publish.  The adapter's codes are copied here so a
#: far side that has drifted cannot push an unbounded string into the flow.
ADAPTER_CODES = frozenset({
    "backup_verified", "backup_reconciled", "backup_attempt_unresolved",
    "backup_not_run", "restore_verified", "restore_completion_unknown",
    "restore_not_run", "request_invalid", "request_key_unknown",
    "request_kind_unknown", "operation_id_invalid", "path_invalid",
    "path_inside_workspace", "digest_invalid", "workspace_held_by_owner",
    "workspace_identity_mismatch", "workspace_identity_unknown",
    "workspace_unreadable", "receipt_io_unknown", "receipt_binding_mismatch",
    "backup_archive_absent", "backup_archive_ambiguous",
    "backup_verification_failed", "backup_digest_mismatch",
    "maintenance_refused", "maintenance_io_unknown", "unexpected_failure",
})
TRANSPORT_CODES = frozenset({
    CODE_CHANNEL_LOST, CODE_CHANNEL_NOT_STARTED, CODE_CHANNEL_NO_STDIN,
    CODE_REPLY_UNRECOGNISED, CODE_REQUEST_UNBUILDABLE, CODE_TARGET_INVALID,
    CODE_RESTORE_SOURCE_UNKNOWN, CODE_UNRECOGNISED,
})
PUBLISHABLE_CODES = ADAPTER_CODES | TRANSPORT_CODES

#: Adapter codes meaning the operation has no attempt receipt at all, so it was
#: never issued and the identity settles instead of staying open.
_NOT_ISSUED_CODES = frozenset({CODE_BACKUP_NOT_RUN, CODE_RESTORE_NOT_RUN})


@dataclass(frozen=True)
class RestoreSource:
    """The verified archive a restore is bound to, and nothing else.

    The port learns this from its own verified create or observation.  It is
    the one piece of state a desktop restart cannot re-derive -- the archive
    lives under the *backup's* operation directory, not the restore's -- so the
    host persists it and hands it back through the constructor.
    """

    operation_id: str
    backup_digest: str


def bounded_code(value: object) -> str:
    """Reduce any answer's code to one member of the closed publishable set."""

    return value if value in PUBLISHABLE_CODES else CODE_UNRECOGNISED


def _flag(value: object) -> bool | None:
    return value if type(value) is bool else None


def _status_of(document: Mapping[str, Any]) -> str:
    status = document.get("status")
    return status if status in (STATUS_VERIFIED, STATUS_FAILED, STATUS_UNKNOWN) else STATUS_UNKNOWN


def _attempted(document: Mapping[str, Any]) -> bool:
    return document.get("attempted") is True


def _digest_of(document: Mapping[str, Any]) -> str | None:
    try:
        return validated_digest(document.get("backup_digest"))
    except MaintenanceTargetError:
        return None


class RemoteMaintenanceBackupPort:
    """A ``BackupPort`` whose every effect is one supported maintenance call.

    The target names the currently selected remote runtime plus the explicitly
    checked location of the adapter on it.  This port places nothing there and
    verifies nothing about the file itself; supplying and verifying that helper
    payload is the host's job, and a helper that is not present simply makes
    every call refuse before any mutation is issued.
    """

    def __init__(
        self,
        target: MaintenanceTarget,
        *,
        ssh_executable: str,
        restore_source: RestoreSource | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        process_factory: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self._target = validated_target(target)
        self._ssh_executable = ssh_executable
        self._timeout = validated_timeout(timeout)
        self._process_factory = process_factory
        self._lock = threading.Lock()
        self._restore_source = _admitted_source(restore_source)

    @property
    def restore_source(self) -> RestoreSource | None:
        """The verified backup identity this port would restore from."""

        with self._lock:
            return self._restore_source

    # -- BackupPort ------------------------------------------------------

    def create_verified(self, operation_id: str) -> BackupOutcome:
        """Take one verified backup of the remote store, exactly once."""

        settled = self._preflight(KIND_OBSERVE_BACKUP, operation_id)
        if settled is not None:
            return self._backup_outcome(operation_id, settled)
        reply = self._call(KIND_CREATE_BACKUP, operation_id)
        return self._backup_outcome(operation_id, self._answer(operation_id, reply))

    def observe(self, operation_id: str) -> BackupOutcome:
        """Read what became of that same backup operation. Writes nothing."""

        reply = self._call(KIND_OBSERVE_BACKUP, operation_id)
        return self._backup_outcome(operation_id, self._observed(reply))

    def restore(self, operation_id: str) -> RestoreOutcome:
        """Restore the verified backup through the supported restore call."""

        settled = self._preflight(KIND_OBSERVE_RESTORE, operation_id)
        if settled is not None:
            return _restore_outcome(settled)
        reply = self._call(KIND_RESTORE_BACKUP, operation_id)
        return _restore_outcome(self._answer(operation_id, reply))

    def observe_restore(self, operation_id: str) -> RestoreOutcome:
        """Read what became of that same restore. Never infers a completion."""

        reply = self._call(KIND_OBSERVE_RESTORE, operation_id)
        return _restore_outcome(self._observed(reply))

    # -- one exchange ----------------------------------------------------

    def _call(self, kind: str, operation_id: str) -> MaintenanceReply:
        """Build and run one request, refusing before any process exists."""

        source = self.restore_source
        if kind in SOURCED_KINDS and source is None:
            raise PortRefusal(CODE_RESTORE_SOURCE_UNKNOWN)
        sourced = kind in SOURCED_KINDS
        try:
            request = build_request(
                self._target,
                kind=kind,
                operation_id=operation_id,
                source_operation_id=source.operation_id if sourced else None,
                backup_digest=source.backup_digest if sourced else None,
            )
        except MaintenanceTargetError as error:
            raise PortRefusal(bounded_code(error.code)) from None
        return exchange(
            self._target,
            request,
            ssh_executable=self._ssh_executable,
            timeout=self._timeout,
            process_factory=self._process_factory,
        )

    def _preflight(self, observe_kind: str, operation_id: str) -> Mapping[str, Any] | None:
        """Ask, read-only, before issuing the mutation this identity names.

        An observation takes no lease and writes nothing, so its failure proves
        the mutation was never issued and is a plain refusal.  A completion
        already on record is returned instead of running a second effect, and a
        binding already on record is a lost response to reconcile, not a
        licence to issue again.  ``None`` means the operation has no binding
        yet, which is the only condition under which the mutation may run.
        """

        reply = self._call(observe_kind, operation_id)
        if not reply.answered:
            raise PortRefusal(bounded_code(reply.code))
        document = reply.document
        assert document is not None
        if _status_of(document) == STATUS_VERIFIED:
            return document
        if document.get("code") in _NOT_ISSUED_CODES:
            return None
        raise LostResponse(operation_id)

    def _answer(self, operation_id: str, reply: MaintenanceReply) -> Mapping[str, Any]:
        """Classify the mutating call's own reply against the effect boundary."""

        if not reply.answered:
            if reply.effect == EFFECT_NONE:
                raise PortRefusal(bounded_code(reply.code))
            raise LostResponse(operation_id)
        document = reply.document
        assert document is not None
        status = _status_of(document)
        if status == STATUS_VERIFIED:
            return document
        if status == STATUS_FAILED and not _attempted(document):
            # The adapter refuses before its supported call only; a refusal it
            # did not mark attempted committed nothing and bound nothing.
            raise PortRefusal(bounded_code(document.get("code")))
        raise LostResponse(operation_id)

    def _observed(self, reply: MaintenanceReply) -> Mapping[str, Any] | None:
        """An observation that did not answer settles nothing and commits nothing."""

        if not reply.answered:
            raise PortRefusal(bounded_code(reply.code))
        return reply.document

    # -- typed outcomes --------------------------------------------------

    def _backup_outcome(
        self, operation_id: str, document: Mapping[str, Any] | None
    ) -> BackupOutcome:
        """A verified backup also hands the port the source a restore needs."""

        if document is None:
            return BackupOutcome(status=STATUS_UNKNOWN)
        if _status_of(document) == STATUS_VERIFIED:
            self._remember_source(operation_id, _digest_of(document))
            return BackupOutcome(status=STATUS_VERIFIED)
        if document.get("code") == CODE_BACKUP_NOT_RUN:
            return BackupOutcome(status=STATUS_NOT_RUN)
        return BackupOutcome(status=STATUS_UNKNOWN)

    def _remember_source(self, operation_id: str, digest: str | None) -> None:
        if digest is None:
            return
        with self._lock:
            self._restore_source = RestoreSource(operation_id, digest)


def _admitted_source(value: object) -> RestoreSource | None:
    """A retained restore source is re-validated before it can bind anything."""

    if value is None:
        return None
    if not isinstance(value, RestoreSource):
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    return RestoreSource(value.operation_id, validated_digest(value.backup_digest))


def _restore_outcome(document: Mapping[str, Any] | None) -> RestoreOutcome:
    """What a restore established, keeping the pre-migration answer honest.

    ``pre_migration_data`` is whatever the adapter measured, which for a real
    ``restore_store`` is nothing: the data came back through the current
    upgrade planner, so an older application still may not run against it.
    """

    if document is None:
        return RestoreOutcome(status=STATUS_UNKNOWN)
    status = _status_of(document)
    if status == STATUS_VERIFIED:
        return RestoreOutcome(
            status=STATUS_VERIFIED,
            pre_migration_data=_flag(document.get("pre_migration_data")),
        )
    if document.get("code") == CODE_RESTORE_NOT_RUN:
        return RestoreOutcome(status=STATUS_FAILED)
    return RestoreOutcome(status=STATUS_UNKNOWN)


__all__ = [
    "ADAPTER_CODES",
    "CODE_RESTORE_SOURCE_UNKNOWN",
    "CODE_UNRECOGNISED",
    "PUBLISHABLE_CODES",
    "RemoteMaintenanceBackupPort",
    "RestoreSource",
    "TRANSPORT_CODES",
    "bounded_code",
]
