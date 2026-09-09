"""Pure Capture unlink CAS and status policy.

Shared by the legacy service mixin and the injected V4 CaptureReplyRepository
so unlink, revision CAS and inbox-reversion cannot drift. Callers own lookup,
idempotency, events and persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .store_document_validation import MAX_REVISION


UNLINK_INVALID_REVISION = "invalid_revision"
UNLINK_REVISION_CONFLICT = "revision_conflict"
UNLINK_UNDO_CONFLICT = "capture_unlink_undo_conflict"
UNLINK_REVISION_EXHAUSTED = "revision_exhausted"


class CaptureUnlinkPolicyError(ValueError):
    """Closed unlink-policy refusal. Callers map this onto their error type."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CaptureUnlinkPlan:
    duplicate: bool
    linked_task_ids: tuple[str, ...]
    status: str
    mutate: bool


def admit_displayed_capture_revision(value: Any) -> int:
    """Admit a displayed Capture revision: nonnegative safe integer, not bool."""

    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise CaptureUnlinkPolicyError(UNLINK_INVALID_REVISION)
    return value


def plan_capture_unlink(
    *,
    linked_task_ids: Sequence[str],
    converted_task_ids: Sequence[str],
    status: str,
    task_id: str,
    stored_revision: int,
    displayed_revision: int,
) -> CaptureUnlinkPlan:
    """Decide the Capture row after removing one explicit Task link.

    CAS runs even when the link is already absent. Status becomes inbox only
    when it is currently linked and both link lists would be empty. Converted
    ids, action references and every other field stay the caller's to preserve.
    """

    if displayed_revision != stored_revision:
        raise CaptureUnlinkPolicyError(UNLINK_REVISION_CONFLICT)
    current = tuple(linked_task_ids)
    if task_id not in current:
        return CaptureUnlinkPlan(
            duplicate=True,
            linked_task_ids=current,
            status=status,
            mutate=False,
        )
    remaining = tuple(item for item in current if item != task_id)
    next_status = status
    if status == "linked" and not remaining and not converted_task_ids:
        next_status = "inbox"
    return CaptureUnlinkPlan(
        duplicate=False,
        linked_task_ids=remaining,
        status=next_status,
        mutate=True,
    )


@dataclass(frozen=True)
class CaptureUnlinkUndoPlan:
    linked_task_ids: tuple[str, ...]
    status: str


def advance_capture_revision(stored_revision: int) -> int:
    """The next Capture revision, or a closed refusal at the safe integer ceiling."""

    admitted = admit_displayed_capture_revision(stored_revision)
    if admitted >= MAX_REVISION:
        raise CaptureUnlinkPolicyError(UNLINK_REVISION_EXHAUSTED)
    return admitted + 1


def plan_capture_unlink_undo(
    *,
    linked_task_ids: Sequence[str],
    stored_revision: int,
    displayed_revision: int,
    after_revision: int,
    stored_digest: str,
    after_digest: str,
    task_id: str,
    status_before: str,
) -> CaptureUnlinkUndoPlan:
    """Decide the Capture row for one exact unlink inverse.

    The recorded post-image and the removed link must still be current. An
    already-present link is a conflict, not a silent duplicate.
    """

    if displayed_revision != stored_revision or stored_revision != after_revision:
        raise CaptureUnlinkPolicyError(UNLINK_REVISION_CONFLICT)
    if stored_digest != after_digest:
        raise CaptureUnlinkPolicyError(UNLINK_UNDO_CONFLICT)
    current = tuple(linked_task_ids)
    if task_id in current:
        raise CaptureUnlinkPolicyError(UNLINK_UNDO_CONFLICT)
    return CaptureUnlinkUndoPlan(
        linked_task_ids=tuple(sorted((*current, task_id))),
        status=status_before,
    )
