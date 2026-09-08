"""Request refusal type and the single exception-to-HTTP mapping.

The mapping is the one the loopback handler has always applied: the order of
the branches decides which envelope a domain error receives, so the sequence
here is the sequence that shipped. An exception this table does not name is
re-raised unchanged rather than flattened into a 500.
"""

from __future__ import annotations

import json
from typing import Any

from .capture import CaptureValidationError
from .mutation_service import MutationReceiptError
from .service import (
    CheckpointTransitionConflictError,
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    ReplyReceiptConflictError,
    RevisionConflictError,
    SnapshotExportConflictError,
    SourceRevisionConflictError,
    StaleCaptureError,
    TaskDeletionTransactionError,
    WorkSessionConflictError,
)
from .store import StoreAdoptionConflictError, StoreExternalChangeError


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


_CONFLICT_ERRORS = (
    RevisionConflictError,
    IdempotencyConflictError,
    ReplyReceiptConflictError,
    SnapshotExportConflictError,
    StaleCaptureError,
    SourceRevisionConflictError,
    WorkSessionConflictError,
)


def _external_change_details(error: StoreExternalChangeError) -> dict[str, Any]:
    return {
        "state": (
            "invalid"
            if error.status.get("status") == "external-change-invalid"
            else error.status.get("status")
        ),
        "generation": error.status.get("generation"),
        "changed_files": error.status.get("changed_files", []),
    }


class ErrorDispatchMixin:
    """The frozen exception taxonomy every verb entry point funnels into."""

    def _dispatch_error(self, error: BaseException) -> None:
        if isinstance(error, (MutationReceiptError, TaskDeletionTransactionError)):
            self.send_api_error(error.code, str(error), error.status, error.details)
        elif isinstance(error, RequestError):
            self.send_api_error(error.code, str(error), error.status, error.details)
        elif isinstance(error, NotFoundError):
            self.send_api_error(error.code, str(error), 404, error.details)
        elif isinstance(error, CheckpointTransitionConflictError):
            # The closed pure code travels in details; the message is constant.
            self.send_api_error(error.code, str(error), 409, error.details)
        elif isinstance(error, _CONFLICT_ERRORS):
            self.send_api_error(error.code, str(error), 409, error.details)
        elif isinstance(error, CaptureValidationError):
            self.send_api_error(error.code, str(error), 400, error.details)
        elif isinstance(error, StoreExternalChangeError):
            self.send_api_error(
                "store_sync_required",
                str(error),
                409,
                _external_change_details(error),
            )
        elif isinstance(error, StoreAdoptionConflictError):
            self.send_api_error("idempotency_conflict", str(error), 409)
        elif isinstance(error, DomainError):
            self.send_api_error(error.code, str(error), 400, error.details)
        elif isinstance(error, (ValueError, json.JSONDecodeError)):
            self.send_api_error("invalid_request", str(error), 400)
        else:
            raise error


__all__ = ("ErrorDispatchMixin", "RequestError")
