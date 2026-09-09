"""Bounded remote maintenance operations over the supported Work Stack calls.

This is the remote-side adapter the desktop root executes through its already
approved SSH stdin transport.  It is not a daemon and opens no socket: one
process, one request document on stdin, one result document on stdout.

Four operation kinds, matching the update flow's backup port exactly:

* ``create_backup``   -> ``maintenance.backup_store``  (mutating)
* ``observe_backup``  -> read-only observation of that same operation
* ``restore_backup``  -> ``maintenance.restore_store`` (mutating)
* ``observe_restore`` -> read-only observation of that same operation

Only those supported calls, plus ``maintenance.verify_backup``, ever touch the
planning store.  They acquire the store's own writer lease; this module never
copies live SSOT bytes, never removes a lock, and never runs a generic recovery
that could steal the lease from a live writer.  An owner still holding the
lease is a bounded refusal, not something to force.

Every mutation is durably bound before it runs.  The attempt receipt names the
canonical workspace directory, the workspace UUID, the operation kind, the
operation UUID and — for a restore — the exact archive digest selected.  Coming
back to an operation that already has an attempt receipt and no completed
receipt means the completion is *unknown*: ``create_backup`` may reconcile only
a single, fully re-verified archive in that operation's own directory, and a
restore is never reissued and never inferred complete from data that merely
looks plausible.

The refusal boundary is the supported service call itself.  Anything that fails
before ``maintenance.restore_store`` runs — a held lease, a wrong workspace
UUID, an unverifiable archive — refuses with no effect and no attempt.  Once
that call has returned the destination has been rewritten, so every later
failure — measuring the restored schema, validating the returned receipt,
writing the completed receipt — is reported as unknown with
``attempted: true``.  A completed receipt is still written only for a result
that was actually verified.

The restore fact that matters for recovery: ``maintenance.restore_store``
writes the archive's documents through ``_restored_documents``, which upgrades
anything older to the schema *this* build writes.  A verified restore therefore
never establishes that an older application may run against the destination
again.  This module reports the measured destination schema and
``rollback_compatible: false``; it does not downgrade anything and does not
claim reader compatibility it cannot prove.

Raw paths are accepted as private, validated input because the supported
maintenance calls need real paths.  They never leave: the result document
carries statuses, bounded symbolic codes, digests and counts only, with no
path, token, or Task/Context body.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _shell_dir() -> str:
    source = globals().get("__file__")
    if type(source) is not str or not source:
        return ""
    try:
        return str(Path(source).resolve(strict=True).parent)
    except OSError:
        return ""


def _app_root(shell_dir: str) -> str:
    """The installed application root, i.e. the parent holding ``workstack``."""

    if not shell_dir:
        return ""
    for parent in [Path(shell_dir), *Path(shell_dir).parents][:5]:
        if (parent / "workstack" / "maintenance.py").is_file():
            return str(parent)
    return ""


_SHELL_DIR = _shell_dir()
if _SHELL_DIR and _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)
_APP_ROOT = _app_root(_SHELL_DIR)
if _APP_ROOT and _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import remote_update_maintenance_receipts as receipts  # noqa: E402
from workstack import maintenance  # noqa: E402
from workstack.store import Store, StoreLockedError  # noqa: E402


SCHEMA_VERSION = "remote-update-maintenance/1"
TOOL = "workstack-remote-maintenance/1"

KIND_CREATE_BACKUP = "create_backup"
KIND_OBSERVE_BACKUP = "observe_backup"
KIND_RESTORE_BACKUP = "restore_backup"
KIND_OBSERVE_RESTORE = "observe_restore"
KINDS = (
    KIND_CREATE_BACKUP,
    KIND_OBSERVE_BACKUP,
    KIND_RESTORE_BACKUP,
    KIND_OBSERVE_RESTORE,
)
# The kind an observation observes; the receipt binding always names this one,
# so an observation and its mutation share one durable identity.
OPERATION_KIND = {
    KIND_CREATE_BACKUP: KIND_CREATE_BACKUP,
    KIND_OBSERVE_BACKUP: KIND_CREATE_BACKUP,
    KIND_RESTORE_BACKUP: KIND_RESTORE_BACKUP,
    KIND_OBSERVE_RESTORE: KIND_RESTORE_BACKUP,
}
OBSERVATIONS = frozenset({KIND_OBSERVE_BACKUP, KIND_OBSERVE_RESTORE})

STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

COMMON_KEYS = (
    "backup_root",
    "kind",
    "operation_id",
    "state_root",
    "workspace_dir",
    "workspace_id",
)
SOURCE_KEYS = ("backup_digest", "source_operation_id")
REQUEST_KEYS = {
    KIND_CREATE_BACKUP: frozenset(COMMON_KEYS),
    KIND_OBSERVE_BACKUP: frozenset(COMMON_KEYS),
    KIND_RESTORE_BACKUP: frozenset(COMMON_KEYS + SOURCE_KEYS),
    KIND_OBSERVE_RESTORE: frozenset(COMMON_KEYS + SOURCE_KEYS),
}

CODE_BACKUP_VERIFIED = "backup_verified"
CODE_BACKUP_RECONCILED = "backup_reconciled"
CODE_BACKUP_ATTEMPT_UNRESOLVED = "backup_attempt_unresolved"
CODE_BACKUP_NOT_RUN = "backup_not_run"
CODE_RESTORE_VERIFIED = "restore_verified"
CODE_RESTORE_COMPLETION_UNKNOWN = "restore_completion_unknown"
CODE_RESTORE_NOT_RUN = "restore_not_run"
CODE_REQUEST_INVALID = "request_invalid"
CODE_REQUEST_KEY_UNKNOWN = "request_key_unknown"
CODE_REQUEST_KIND_UNKNOWN = "request_kind_unknown"
CODE_OPERATION_ID_INVALID = "operation_id_invalid"
CODE_PATH_INVALID = "path_invalid"
CODE_PATH_INSIDE_WORKSPACE = "path_inside_workspace"
CODE_DIGEST_INVALID = "digest_invalid"
CODE_WORKSPACE_HELD_BY_OWNER = "workspace_held_by_owner"
CODE_WORKSPACE_IDENTITY_MISMATCH = "workspace_identity_mismatch"
CODE_WORKSPACE_IDENTITY_UNKNOWN = "workspace_identity_unknown"
CODE_WORKSPACE_UNREADABLE = "workspace_unreadable"
CODE_RECEIPT_IO_UNKNOWN = "receipt_io_unknown"
CODE_RECEIPT_BINDING_MISMATCH = "receipt_binding_mismatch"
CODE_ARCHIVE_ABSENT = "backup_archive_absent"
CODE_ARCHIVE_AMBIGUOUS = "backup_archive_ambiguous"
CODE_ARCHIVE_UNVERIFIED = "backup_verification_failed"
CODE_DIGEST_MISMATCH = "backup_digest_mismatch"
CODE_MAINTENANCE_REFUSED = "maintenance_refused"
CODE_MAINTENANCE_IO_UNKNOWN = "maintenance_io_unknown"
CODE_UNEXPECTED_FAILURE = "unexpected_failure"

MAX_REQUEST_BYTES = 16 * 1024
MAX_PATH_CHARS = 1024
SAFETY_DIRNAME = "safety"
DIGEST_PREFIX = "sha256:"
DIGEST_CHARS = frozenset("0123456789abcdef")

RESULT_FIELDS = (
    "archive_schema_version",
    "attempted",
    "backup_digest",
    "backup_file_count",
    "observation",
    "pre_migration_data",
    "reconciled",
    "replayed",
    "rollback_compatible",
    "safety_backup_created",
    "schema_downgraded",
    "store_schema_version",
    "workspace_match",
)
RESULT_DEFAULTS: dict[str, Any] = {
    "archive_schema_version": None,
    "attempted": False,
    "backup_digest": None,
    "backup_file_count": None,
    "observation": False,
    "pre_migration_data": None,
    "reconciled": False,
    "replayed": False,
    "rollback_compatible": None,
    "safety_backup_created": None,
    "schema_downgraded": None,
    "store_schema_version": None,
    "workspace_match": None,
}


class MaintenanceRefused(Exception):
    """A bounded, established refusal: the effect did not happen."""

    def __init__(self, code: str, facts: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.facts = dict(facts or {})
        super().__init__(code)


class MaintenanceUnknown(Exception):
    """A bounded unknown: whether the effect happened was not established."""

    def __init__(self, code: str, facts: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.facts = dict(facts or {})
        super().__init__(code)


@dataclass(frozen=True)
class MaintenanceRequest:
    kind: str
    operation_id: str
    workspace_dir: Path
    workspace_id: str
    state_root: Path
    backup_root: Path
    source_operation_id: str | None
    backup_digest: str | None


def _canonical_uuid(value: object, code: str) -> str:
    if type(value) is not str:
        raise MaintenanceRefused(code)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise MaintenanceRefused(code) from error
    if str(parsed) != value:
        raise MaintenanceRefused(code)
    return value


def _validated_path(value: object) -> Path:
    """Admit one absolute, traversal-free private path and canonicalise it."""

    if type(value) is not str or not value or len(value) > MAX_PATH_CHARS:
        raise MaintenanceRefused(CODE_PATH_INVALID)
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise MaintenanceRefused(CODE_PATH_INVALID)
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise MaintenanceRefused(CODE_PATH_INVALID)
    try:
        return candidate.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise MaintenanceRefused(CODE_PATH_INVALID) from error


def _validated_digest(value: object) -> str:
    if type(value) is not str or not value.startswith(DIGEST_PREFIX):
        raise MaintenanceRefused(CODE_DIGEST_INVALID)
    body = value[len(DIGEST_PREFIX):]
    if len(body) != 64 or not set(body) <= DIGEST_CHARS:
        raise MaintenanceRefused(CODE_DIGEST_INVALID)
    return value


def _admitted_keys(payload: Mapping[str, Any], kind: str) -> None:
    allowed = REQUEST_KEYS[kind]
    present = set(payload)
    if present - allowed:
        raise MaintenanceRefused(CODE_REQUEST_KEY_UNKNOWN)
    if allowed - present:
        raise MaintenanceRefused(CODE_REQUEST_INVALID)


def parse_request(payload: object) -> MaintenanceRequest:
    """Admit exactly the configured fields for one operation kind, or refuse."""

    if type(payload) is not dict:
        raise MaintenanceRefused(CODE_REQUEST_INVALID)
    kind = payload.get("kind")
    if kind not in KINDS:
        raise MaintenanceRefused(CODE_REQUEST_KIND_UNKNOWN)
    _admitted_keys(payload, kind)
    workspace_dir = _validated_path(payload["workspace_dir"])
    state_root = _validated_path(payload["state_root"])
    backup_root = _validated_path(payload["backup_root"])
    for outside in (state_root, backup_root):
        if not receipts.is_outside(outside, workspace_dir):
            raise MaintenanceRefused(CODE_PATH_INSIDE_WORKSPACE)
    source_operation_id = None
    backup_digest = None
    if kind in (KIND_RESTORE_BACKUP, KIND_OBSERVE_RESTORE):
        source_operation_id = _canonical_uuid(
            payload["source_operation_id"], CODE_OPERATION_ID_INVALID
        )
        backup_digest = _validated_digest(payload["backup_digest"])
    return MaintenanceRequest(
        kind=kind,
        operation_id=_canonical_uuid(payload["operation_id"], CODE_OPERATION_ID_INVALID),
        workspace_dir=workspace_dir,
        workspace_id=_canonical_uuid(payload["workspace_id"], CODE_REQUEST_INVALID),
        state_root=state_root,
        backup_root=backup_root,
        source_operation_id=source_operation_id,
        backup_digest=backup_digest,
    )


def binding_of(request: MaintenanceRequest) -> dict[str, Any]:
    """The durable identity an effect is bound to before it may run."""

    return {
        "backup_digest": request.backup_digest,
        "backup_root": str(request.backup_root),
        "kind": OPERATION_KIND[request.kind],
        "operation_id": request.operation_id,
        "source_operation_id": request.source_operation_id,
        "workspace_dir": str(request.workspace_dir),
        "workspace_id": request.workspace_id,
    }


def _result(request: MaintenanceRequest, status: str, code: str, **facts: Any) -> dict[str, Any]:
    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "kind": OPERATION_KIND[request.kind],
        "operation_id": request.operation_id,
        "status": status,
        "code": code,
    }
    document.update(RESULT_DEFAULTS)
    document["observation"] = request.kind in OBSERVATIONS
    for name, value in facts.items():
        if name not in RESULT_DEFAULTS:
            raise KeyError(name)
        document[name] = value
    return document


def _bare_result(payload: object, status: str, code: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """A result for a request that never became a validated operation."""

    kind = payload.get("kind") if type(payload) is dict else None
    kind = kind if type(kind) is str and kind in KINDS else None
    raw_operation = payload.get("operation_id") if type(payload) is dict else None
    try:
        operation = _canonical_uuid(raw_operation, CODE_OPERATION_ID_INVALID)
    except MaintenanceRefused:
        operation = None
    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "kind": OPERATION_KIND[kind] if kind is not None else None,
        "operation_id": operation,
        "status": status,
        "code": code,
    }
    document.update(RESULT_DEFAULTS)
    document["observation"] = kind in OBSERVATIONS
    for name in RESULT_FIELDS:
        if name in facts:
            document[name] = facts[name]
    return document


def _operation_backup_dir(request: MaintenanceRequest) -> Path:
    return request.backup_root / request.operation_id


def _source_backup_dir(request: MaintenanceRequest) -> Path:
    return request.backup_root / str(request.source_operation_id)


def _unique_archive(directory: Path) -> Path:
    """The one archive an operation's own directory holds. No broad scan."""

    try:
        entries = sorted(item for item in directory.iterdir() if item.is_file())
    except FileNotFoundError as error:
        raise MaintenanceRefused(CODE_ARCHIVE_ABSENT) from error
    except OSError as error:
        raise MaintenanceUnknown(CODE_MAINTENANCE_IO_UNKNOWN) from error
    if not entries:
        raise MaintenanceRefused(CODE_ARCHIVE_ABSENT)
    if len(entries) > 1:
        raise MaintenanceRefused(CODE_ARCHIVE_AMBIGUOUS)
    return entries[0]


def _verified_archive(
    path: Path, workspace_id: str, expected_digest: str | None
) -> maintenance.BackupArtifact:
    """Re-verify one archive where it lies, and bind it to this workspace."""

    try:
        artifact = maintenance.verify_backup(path)
    except maintenance.BackupValidationError as error:
        raise MaintenanceRefused(CODE_ARCHIVE_UNVERIFIED) from error
    except OSError as error:
        raise MaintenanceUnknown(CODE_MAINTENANCE_IO_UNKNOWN) from error
    if artifact.workspace_id != workspace_id:
        raise MaintenanceRefused(CODE_WORKSPACE_IDENTITY_MISMATCH)
    if expected_digest is not None and artifact.digest != expected_digest:
        raise MaintenanceRefused(CODE_DIGEST_MISMATCH)
    return artifact


def _measure_workspace(request: MaintenanceRequest) -> int:
    """Prove the destination is this workspace, under the store's own lease.

    ``consistent_read`` is the store's read boundary: it takes the writer
    lease, refuses a pending journal, and never recovers or migrates. A live
    owner therefore refuses here instead of being forced aside.
    """

    try:
        with Store(request.workspace_dir).consistent_read() as readiness:
            observed, schema = readiness.workspace_uid, readiness.schema_version
    except StoreLockedError as error:
        raise MaintenanceRefused(CODE_WORKSPACE_HELD_BY_OWNER) from error
    except FileNotFoundError as error:
        raise MaintenanceRefused(CODE_WORKSPACE_IDENTITY_UNKNOWN) from error
    except OSError as error:
        raise MaintenanceUnknown(CODE_MAINTENANCE_IO_UNKNOWN) from error
    except ValueError as error:
        raise MaintenanceRefused(CODE_WORKSPACE_UNREADABLE) from error
    if observed != request.workspace_id:
        raise MaintenanceRefused(CODE_WORKSPACE_IDENTITY_MISMATCH)
    return schema


def _record(request: MaintenanceRequest, binding: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Write the completed receipt only after a verifiable service result."""

    try:
        receipts.complete(request.state_root, binding, result)
    except receipts.ReceiptUnavailable as error:
        raise MaintenanceUnknown(CODE_RECEIPT_IO_UNKNOWN, {"attempted": True}) from error
    return dict(result)


def _replayed(request: MaintenanceRequest, completed: Mapping[str, Any]) -> dict[str, Any]:
    """Answer from the completed receipt instead of repeating the effect."""

    recorded = receipts.recorded_result(completed)
    if recorded is None:
        raise MaintenanceUnknown(CODE_RECEIPT_IO_UNKNOWN, {"attempted": True})
    document = dict(recorded)
    document["replayed"] = True
    document["observation"] = request.kind in OBSERVATIONS
    return document


def _reconcile_backup(request: MaintenanceRequest) -> dict[str, Any]:
    """A lost create_backup reply reconciles only to a fully verified archive.

    Anything else — no archive, more than one, or one that does not verify
    against this workspace — stays unknown. This never re-runs the backup.
    """

    try:
        archive = _unique_archive(_operation_backup_dir(request))
        artifact = _verified_archive(archive, request.workspace_id, None)
    except (MaintenanceRefused, MaintenanceUnknown):
        return _result(
            request,
            STATUS_UNKNOWN,
            CODE_BACKUP_ATTEMPT_UNRESOLVED,
            attempted=True,
        )
    return _result(
        request,
        STATUS_VERIFIED,
        CODE_BACKUP_RECONCILED,
        attempted=True,
        reconciled=True,
        workspace_match=True,
        backup_digest=artifact.digest,
        backup_file_count=artifact.file_count,
    )


def _create_backup(request: MaintenanceRequest, binding: Mapping[str, Any]) -> dict[str, Any]:
    completed = receipts.read_completed(request.state_root, binding)
    if completed is not None:
        return _replayed(request, completed)
    if receipts.read_attempt(request.state_root, binding) is not None:
        # Already attempted: reconcile what is there, never run a second
        # backup, and never require the owner's lease to answer.
        return _reconcile_backup(request)
    schema = _measure_workspace(request)
    if receipts.bind_attempt(request.state_root, binding) == receipts.ALREADY_ATTEMPTED:
        return _reconcile_backup(request)
    artifact = _run_backup_store(request)
    verified = _verified_archive(artifact.path, request.workspace_id, artifact.digest)
    return _record(
        request,
        binding,
        _result(
            request,
            STATUS_VERIFIED,
            CODE_BACKUP_VERIFIED,
            attempted=True,
            workspace_match=True,
            backup_digest=verified.digest,
            backup_file_count=verified.file_count,
            store_schema_version=schema,
        ),
    )


def _run_backup_store(request: MaintenanceRequest) -> maintenance.BackupArtifact:
    """The one supported backup call. It takes the store's lease itself."""

    try:
        return maintenance.backup_store(request.workspace_dir, _operation_backup_dir(request))
    except maintenance.BackupValidationError as error:
        raise MaintenanceRefused(CODE_MAINTENANCE_REFUSED, {"attempted": True}) from error
    except StoreLockedError as error:
        raise MaintenanceRefused(CODE_WORKSPACE_HELD_BY_OWNER, {"attempted": True}) from error
    except FileExistsError as error:
        raise MaintenanceUnknown(CODE_BACKUP_ATTEMPT_UNRESOLVED, {"attempted": True}) from error
    except OSError as error:
        raise MaintenanceUnknown(CODE_MAINTENANCE_IO_UNKNOWN, {"attempted": True}) from error
    except ValueError as error:
        raise MaintenanceRefused(CODE_MAINTENANCE_REFUSED, {"attempted": True}) from error


def _observe_backup(request: MaintenanceRequest, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only observation of one create_backup operation. Writes nothing."""

    completed = receipts.read_completed(request.state_root, binding)
    if completed is not None:
        return _replayed(request, completed)
    if receipts.read_attempt(request.state_root, binding) is None:
        return _result(request, STATUS_UNKNOWN, CODE_BACKUP_NOT_RUN)
    return _reconcile_backup(request)


def _unresolved_restore(request: MaintenanceRequest) -> dict[str, Any]:
    """An attempted restore is never reissued and never inferred complete.

    Restored data that looks plausible is not evidence: the interrupted request
    may have written all of it, none of it, or been refused before it started.
    Only the completed receipt settles this.
    """

    return _result(
        request,
        STATUS_UNKNOWN,
        CODE_RESTORE_COMPLETION_UNKNOWN,
        attempted=True,
        backup_digest=request.backup_digest,
    )


def _post_effect_unknown(request: MaintenanceRequest, error: Exception) -> MaintenanceUnknown:
    """Reclassify anything raised after the supported restore already returned.

    Once ``maintenance.restore_store`` has returned, the destination has been
    rewritten. A competing owner taking the lease back, a read error, a result
    that cannot be validated, or a receipt that cannot be written are all
    post-effect: none of them establishes that nothing happened, so none may be
    published as a zero-effect refusal. The bounded code is kept, the status
    becomes unknown, and the attempt stays visible as this same operation.
    """

    if isinstance(error, (MaintenanceRefused, MaintenanceUnknown)):
        code, facts = error.code, dict(error.facts)
    elif isinstance(error, receipts.BindingMismatch):
        code, facts = CODE_RECEIPT_BINDING_MISMATCH, {}
    elif isinstance(error, receipts.ReceiptUnavailable):
        code, facts = CODE_RECEIPT_IO_UNKNOWN, {}
    else:
        code, facts = CODE_UNEXPECTED_FAILURE, {}
    facts["attempted"] = True
    facts.setdefault("backup_digest", request.backup_digest)
    return MaintenanceUnknown(code, facts)


def _restore_backup(request: MaintenanceRequest, binding: Mapping[str, Any]) -> dict[str, Any]:
    completed = receipts.read_completed(request.state_root, binding)
    if completed is not None:
        return _replayed(request, completed)
    if receipts.read_attempt(request.state_root, binding) is not None:
        return _unresolved_restore(request)
    archive = _unique_archive(_source_backup_dir(request))
    verified = _verified_archive(archive, request.workspace_id, request.backup_digest)
    _measure_workspace(request)
    if receipts.bind_attempt(request.state_root, binding) == receipts.ALREADY_ATTEMPTED:
        return _unresolved_restore(request)
    receipt = _run_restore_store(request, archive)
    # From here the effect has happened; nothing below may deny the attempt.
    try:
        return _record(request, binding, _restore_result(request, verified, receipt))
    except Exception as error:  # noqa: BLE001 - reclassified, never re-raised raw
        raise _post_effect_unknown(request, error) from error


def _run_restore_store(request: MaintenanceRequest, archive: Path) -> maintenance.RestoreReceipt:
    """The one supported restore call. It takes the store's lease itself."""

    safety = _operation_backup_dir(request) / SAFETY_DIRNAME
    try:
        return maintenance.restore_store(
            archive,
            request.workspace_dir,
            replace=True,
            safety_backup_dir=safety,
        )
    except maintenance.BackupValidationError as error:
        raise MaintenanceRefused(CODE_MAINTENANCE_REFUSED, {"attempted": True}) from error
    except StoreLockedError as error:
        raise MaintenanceRefused(CODE_WORKSPACE_HELD_BY_OWNER, {"attempted": True}) from error
    except OSError as error:
        raise MaintenanceUnknown(CODE_MAINTENANCE_IO_UNKNOWN, {"attempted": True}) from error
    except ValueError as error:
        raise MaintenanceRefused(CODE_MAINTENANCE_REFUSED, {"attempted": True}) from error


def _restore_result(
    request: MaintenanceRequest,
    verified: maintenance.BackupArtifact,
    receipt: maintenance.RestoreReceipt,
) -> dict[str, Any]:
    """What a returned restore actually established, and what it did not.

    ``restore_store`` writes the archive's documents through the current
    upgrade planner, so the destination is this build's schema whatever the
    archive held. That is reported as the measured schema with
    ``rollback_compatible: false``; whether an older reader could open this
    data is not something this adapter can prove, and it is never guessed.
    """

    if receipt.workspace_id != request.workspace_id:
        raise MaintenanceRefused(CODE_WORKSPACE_IDENTITY_MISMATCH, {"attempted": True})
    if receipt.backup_digest != verified.digest:
        raise MaintenanceRefused(CODE_DIGEST_MISMATCH, {"attempted": True})
    return _result(
        request,
        STATUS_VERIFIED,
        CODE_RESTORE_VERIFIED,
        attempted=True,
        workspace_match=True,
        backup_digest=verified.digest,
        backup_file_count=verified.file_count,
        store_schema_version=_measure_workspace(request),
        schema_downgraded=False,
        rollback_compatible=False,
        pre_migration_data=None,
        safety_backup_created=receipt.safety_backup is not None,
    )


def _observe_restore(request: MaintenanceRequest, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only observation of one restore. Never infers a completion."""

    completed = receipts.read_completed(request.state_root, binding)
    if completed is not None:
        return _replayed(request, completed)
    if receipts.read_attempt(request.state_root, binding) is None:
        return _result(request, STATUS_UNKNOWN, CODE_RESTORE_NOT_RUN)
    return _unresolved_restore(request)


_HANDLERS = {
    KIND_CREATE_BACKUP: _create_backup,
    KIND_OBSERVE_BACKUP: _observe_backup,
    KIND_RESTORE_BACKUP: _restore_backup,
    KIND_OBSERVE_RESTORE: _observe_restore,
}


def run_operation(payload: object) -> dict[str, Any]:
    """Run one bounded maintenance operation and report only bounded facts."""

    try:
        request = parse_request(payload)
        return _HANDLERS[request.kind](request, binding_of(request))
    except MaintenanceRefused as error:
        return _bare_result(payload, STATUS_FAILED, error.code, error.facts)
    except MaintenanceUnknown as error:
        return _bare_result(payload, STATUS_UNKNOWN, error.code, error.facts)
    except receipts.BindingMismatch:
        return _bare_result(payload, STATUS_FAILED, CODE_RECEIPT_BINDING_MISMATCH, {})
    except receipts.ReceiptUnavailable:
        return _bare_result(payload, STATUS_UNKNOWN, CODE_RECEIPT_IO_UNKNOWN, {})
    except Exception:  # noqa: BLE001 - no raw exception text ever leaves here
        return _bare_result(payload, STATUS_UNKNOWN, CODE_UNEXPECTED_FAILURE, {})


def _decode_request(body: bytes) -> object:
    if len(body) > MAX_REQUEST_BYTES:
        raise MaintenanceRefused(CODE_REQUEST_INVALID)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise MaintenanceRefused(CODE_REQUEST_INVALID) from error


def _stdin_bytes() -> bytes:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return os.read(0, MAX_REQUEST_BYTES + 1)
    return stream.read(MAX_REQUEST_BYTES + 1) or b""


def main(argv: Sequence[str] | None = None) -> int:
    """One request document on stdin, one result document on stdout."""

    parser = argparse.ArgumentParser(description="Bounded remote Work Stack maintenance", allow_abbrev=False)
    parser.parse_args(list(argv) if argv is not None else None)
    try:
        payload = _decode_request(_stdin_bytes())
    except MaintenanceRefused as error:
        result = _bare_result(None, STATUS_FAILED, error.code, {})
    except OSError:
        result = _bare_result(None, STATUS_UNKNOWN, CODE_MAINTENANCE_IO_UNKNOWN, {})
    else:
        result = run_operation(payload)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == STATUS_VERIFIED else 2


if __name__ == "__main__":
    raise SystemExit(main())
