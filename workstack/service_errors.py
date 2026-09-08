"""Refusal identities shared by every Work Stack domain service module.

Every error the application boundary can raise is declared here once, so the
service facade, its domain modules and its transports all refuse with the SAME
class object. The two lookup tables map a repository-supplied code back to the
identity a released caller already catches.
"""

from __future__ import annotations

from typing import Any

from .snapshot import SnapshotValidationError


class DomainError(ValueError):
    code = "invalid_request"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class TaskDisplayIdAuthorityError(DomainError):
    """Content-free refusal of Task display-ID high-water admission."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotFoundError(DomainError):
    code = "not_found"


class RevisionConflictError(DomainError):
    code = "revision_conflict"


class RevisionExhaustedError(DomainError):
    code = "revision_exhausted"


class IdempotencyConflictError(DomainError):
    code = "idempotency_conflict"


class CheckpointTransitionConflictError(DomainError):
    """A pure history refusal, carried out with its closed transition code.

    Malformed input is an ordinary invalid_request; only the five history
    conflicts reach this type, and the message is constant so no input value
    or nested text can leak through it.
    """

    code = "checkpoint_transition_conflict"

    def __init__(self, transition_code: str) -> None:
        super().__init__(
            "the checkpoint transition conflicts with recorded history",
            {"transition_code": transition_code},
        )


class WorkSessionConflictError(DomainError):
    code = "work_session_conflict"


class StaleCaptureError(DomainError):
    code = "stale_capture"


class SourceRevisionConflictError(DomainError):
    code = "source_revision_conflict"


class ReplyReceiptConflictError(DomainError):
    code = "reply_receipt_conflict"


class SnapshotDisclosureRequiredError(DomainError):
    code = "snapshot_disclosure_required"


class SnapshotExportConflictError(DomainError):
    code = "snapshot_export_conflict"


class SnapshotStoreNotReadyError(DomainError):
    code = "SNAPSHOT_STORE_NOT_READY"


class SnapshotExportRefusedError(DomainError):
    code = "SNAPSHOT_EXPORT_REFUSED"

    def __init__(self, error: SnapshotValidationError) -> None:
        super().__init__("Snapshot export was refused.", error.as_dict())
        if error.public_code is not None:
            self.code = error.public_code


_CAPTURE_REPLY_ERROR_TYPES: dict[str, type[DomainError]] = {
    "capture_not_found": NotFoundError,
    "task_not_found": NotFoundError,
    "reply_not_found": NotFoundError,
    "not_found": NotFoundError,
    "revision_conflict": RevisionConflictError,
    "idempotency_conflict": IdempotencyConflictError,
    "stale_capture": StaleCaptureError,
    "source_revision_conflict": SourceRevisionConflictError,
    "reply_receipt_conflict": ReplyReceiptConflictError,
}

_OPTIONAL_COMMAND_ERROR_TYPES: dict[str, type[DomainError]] = {
    "TASK_NOT_FOUND": NotFoundError,
    "OBJECTIVE_NOT_FOUND": NotFoundError,
    "not_found": NotFoundError,
    "OBJECTIVE_REVISION_CONFLICT": RevisionConflictError,
    "revision_conflict": RevisionConflictError,
    "OBJECTIVE_REVISION_EXHAUSTED": RevisionExhaustedError,
    "IDEMPOTENCY_KEY_CONFLICT": IdempotencyConflictError,
    "idempotency_conflict": IdempotencyConflictError,
    "WORK_SESSION_NOT_FOUND": NotFoundError,
    "WORK_SESSION_ALREADY_ACTIVE": WorkSessionConflictError,
    "WORK_SESSION_TRANSITION_CONFLICT": WorkSessionConflictError,
    "WORK_SESSION_WORKLOG_CONFLICT": WorkSessionConflictError,
}
