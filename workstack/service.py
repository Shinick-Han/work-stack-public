"""Domain logic shared by the CLI and web API.

``WorkStack`` is the one facade every caller already imports. It owns nothing
but composition: the transaction-holding Store, the document repository, the
optional command backends, and the domain mixins that carry the actual
behaviour. The domain is split by responsibility, not by mechanism:

===========================  ==================================================
``service_errors``           every refusal identity, declared once
``service_contracts``        the backend-neutral command Protocols
``service_domain``           enumerations, limits, id/record/revision primitives
``service_text``             text and reference admission for remote material
``service_task_rules``       Task create and patch rules, store-free
``service_capture_rules``    capture review and reply-receipt rules, store-free
``service_graph``            weekly and snapshot projections over documents
``service_composition``      admitted storage compositions and backend dispatch
``service_task_commands``    Task, subtask, note and status writes
``service_task_reads``       Task reads, projection and snapshot export
``service_objectives``       Objective and Key Result commands and reads
``service_cli_commands``     CLI owner-route framing on the owner composition
``service_review``           check-in, worklog entries, review and weekly reads
``service_work_sessions``    work session validation, projection, transitions
``service_checkpoints``      the ACTIVE Worklog view and checkpoint transitions
``service_idempotency``      Activity events and the keyed-intent receipt ledger
``service_search``           the cached local search index
``service_workspace``        graph notes, workspace projection, graph snapshot
``service_captures``         capture ingestion, linking and Task conversion
``service_replies``          reply approval and provider receipts
===========================  ==================================================

The clock stays HERE. ``utc_now``, ``today`` and ``current_quarter`` are the
long-standing patch seam (``workstack.service.today`` and friends), so every
domain mixin reads the clock through the ``_utc_now``/``_today``/
``_current_quarter`` accessors below, which resolve these module globals at
call time. A mixin that imported the functions directly would bind them once
and quietly escape the seam.
"""

from __future__ import annotations

import datetime as dt
from functools import wraps
from typing import Any

from .planning_status import append_transition, validate_and_project
from .service_captures import CaptureServiceMixin
from .service_checkpoints import CheckpointServiceMixin
from .service_cli_commands import CliOwnerCommandsMixin
from .service_idempotency import IdempotencyMixin
from .service_objectives import ObjectiveServiceMixin
from .service_replies import ReplyServiceMixin
from .service_review import ReviewServiceMixin
from .service_search import SearchProjectionMixin
from .service_task_commands import TaskCommandsMixin
from .service_task_reads import TaskReadMixin
from .service_work_sessions import WorkSessionServiceMixin
from .service_workspace import WorkspaceProjectionMixin
from .service_composition import (
    _optional_command,
    _require_released_composition_for_refs,
    _transactional,
)
from .service_task_rules import (
    _apply_task_outcome_write_invariant,
    _objective_records_by_id,
    _patch_change_set,
    _patch_changed_fields,
)
from .storage.document_repository import WorkspaceDocument

# Re-exported so every existing consumer keeps importing these from
# ``workstack.service``; the definitions moved, the import path did not.
from .service_contracts import (
    CaptureReplyCommands,
    IntentCommands,
    ObjectiveCommands,
    PlanningCommands,
    QueryCommands,
    RelationshipCommands,
    TaskCommands,
    WorkSessionCommands,
)
from .service_domain import (
    CAPTURE_STATUSES,
    ERROR_CODE_RE,
    HTML_TAG_RE,
    KEY_RESULT_REF_FIELDS,
    MAIL_HEADER_RE,
    MICROSOFT_WEB_URL_MAX,
    OBJECTIVE_STATUSES,
    PRIORITIES,
    QUOTED_REPLY_RE,
    QUOTE_LINE_RE,
    RAW_CANARY_RE,
    REMOTE_HEADER_PREFIX_RE,
    REMOTE_MESSAGE_REF_MAX,
    REMOTE_MESSAGE_REF_RE,
    REPLY_BODY_MAX,
    REPLY_CAPABILITIES,
    REPLY_OUTCOMES,
    REPLY_RECEIPT_OPTIONAL_FIELDS,
    REPLY_RECEIPT_REQUIRED_FIELDS,
    REPLY_STATES,
    REPLY_TARGET_FIELDS,
    REPLY_TARGET_REF_MAX,
    SECRET_TEXT_RE,
    TASK_CREATE_FIELDS,
    TASK_PATCH_FIELDS,
    TASK_STATUSES,
    _find,
    _guard_revision,
    _next_id,
    _next_revision,
    _required_text,
    _revision,
    _task_uid,
)
from .service_errors import (
    CheckpointTransitionConflictError,
    CaptureUnlinkUndoConflictError,
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    ReplyReceiptConflictError,
    RevisionConflictError,
    RevisionExhaustedError,
    SnapshotDisclosureRequiredError,
    SnapshotExportConflictError,
    SnapshotExportRefusedError,
    SnapshotStoreNotReadyError,
    SourceRevisionConflictError,
    StaleCaptureError,
    TaskDisplayIdAuthorityError,
    WorkSessionConflictError,
)
from .maintenance import BackupDownload
from .mutation_service import MutationNoticeMixin
from .report_repository_service import ReportRepositoryServiceMixin
from .snapshot_export import SnapshotArtifact
from .storage.document_repository import DocumentRepository, StoreDocumentRepository  # noqa: F401
from .storage.task_deletion_transaction import TaskDeletionTransactionError
from .store import Store


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return dt.date.today().isoformat()


def current_quarter(day: dt.date | None = None) -> str:
    day = day or dt.date.today()
    return "{}-Q{}".format(day.year, ((day.month - 1) // 3) + 1)


# ---- Task patch routing ---------------------------------------------------
#
# A Task patch is the ONE call whose dispatch spans three optional command
# slices, so the routing lives beside the facade that owns those slots rather
# than with the single-slice decorators in ``service_composition``. Which
# fields the patch carries decides the slice: scalars go to the Task slice,
# the two relationship fields to the relationship slice, a lone status to the
# planning slice - and a drop is delegated as a relationship delete. A patch
# spanning slices while any of them is configured is refused rather than
# silently split across backends.

_TASK_SCALAR_PATCH_FIELDS = {
    "title", "detail", "priority", "due", "scheduled", "estimate_minutes"
}
_TASK_RELATIONSHIP_PATCH_FIELDS = {"parent_id", "dependencies"}


def _relationship_task_projection(
    stack: Any, task_id: str, receipt: Any
) -> dict[str, Any]:
    task = stack._project_task(
        stack.get_task(task_id), planning_status=str(receipt.status)
    )
    task.update(
        {
            "revision": int(receipt.revision),
            "status": str(receipt.status),
            "parent_id": receipt.parent_id,
            "dependencies": list(receipt.dependencies),
        }
    )
    return task


def _task_patch_backends(method):
    @wraps(method)
    def wrapped(self: Any, task_id: str, patch: dict[str, Any]):
        if not isinstance(patch, dict):
            return method(self, task_id, patch)
        fields = set(patch) - {"revision"}
        if not fields and self.task_commands is not None:
            return _optional_command(
                lambda: self.task_commands.patch_task(task_id, patch), "task"
            )
        if fields and fields <= _TASK_SCALAR_PATCH_FIELDS:
            if self.task_commands is not None:
                return _optional_command(
                    lambda: self.task_commands.patch_task(task_id, patch), "task"
                )
            return method(self, task_id, patch)
        if fields and fields <= _TASK_RELATIONSHIP_PATCH_FIELDS:
            if self.relationship_commands is not None:
                receipt = _optional_command(
                    lambda: self.relationship_commands.patch_relationships(
                        task_id, patch
                    ),
                    "relationship",
                )
                return _relationship_task_projection(self, task_id, receipt)
            return method(self, task_id, patch)
        if fields == {"status"}:
            return _status_patch_backend(self, method, task_id, patch)
        if any(
            command is not None
            for command in (
                self.task_commands,
                self.relationship_commands,
                self.planning_commands,
            )
        ):
            raise DomainError("task patch spans an inactive command slice")
        return method(self, task_id, patch)

    return wrapped


def _status_patch_backend(
    stack: Any, method, task_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    if patch.get("status") == "dropped" and stack.relationship_commands is not None:
        receipt = _optional_command(
            lambda: stack.relationship_commands.delete_task(
                task_id, patch.get("revision")
            ),
            "relationship",
        )
        return _relationship_task_projection(stack, task_id, receipt)
    if stack.planning_commands is not None:
        return _optional_command(
            lambda: stack.planning_commands.patch_status(
                task_id, patch.get("status"), patch.get("revision")
            ),
            "task",
        )
    return method(stack, task_id, patch)


class WorkStack(
    TaskCommandsMixin,
    TaskReadMixin,
    ObjectiveServiceMixin,
    CliOwnerCommandsMixin,
    ReviewServiceMixin,
    WorkSessionServiceMixin,
    CheckpointServiceMixin,
    CaptureServiceMixin,
    ReplyServiceMixin,
    SearchProjectionMixin,
    WorkspaceProjectionMixin,
    IdempotencyMixin,
    ReportRepositoryServiceMixin,
    MutationNoticeMixin,
):
    """The application facade: one Store, one document repository, one clock.

    The domain mixins are listed before the two long-standing report and
    mutation mixins so the resolution order is exactly the one the single-class
    facade had, where a method defined in this file won over both of them.
    """

    def __init__(
        self,
        store: Store | None = None,
        *,
        initialize: bool = True,
        capture_reply_commands: CaptureReplyCommands | None = None,
        intent_commands: IntentCommands | None = None,
        objective_commands: ObjectiveCommands | None = None,
        task_commands: TaskCommands | None = None,
        relationship_commands: RelationshipCommands | None = None,
        planning_commands: PlanningCommands | None = None,
        work_session_commands: WorkSessionCommands | None = None,
        query_commands: QueryCommands | None = None,
        document_repository: DocumentRepository | None = None,
    ) -> None:
        self.store = store or Store()
        self.documents = document_repository or StoreDocumentRepository(self.store)
        self.capture_reply_commands = capture_reply_commands
        self.intent_commands = intent_commands
        self.objective_commands = objective_commands
        self.task_commands = task_commands
        self.relationship_commands = relationship_commands
        self.planning_commands = planning_commands
        self.work_session_commands = work_session_commands
        self.query_commands = query_commands
        self._search_index_generation = -1
        self._search_entries: list[dict[str, Any]] = []
        self.store_readiness = self.store.initialize() if initialize else None

    # ---- the clock seam -------------------------------------------------
    #
    # Resolved from THIS module's globals at call time, so the long-standing
    # ``workstack.service.utc_now`` / ``workstack.service.today`` substitution
    # keeps reaching every domain mixin. They are classmethods so the two
    # ledger writers, which are class-bound, can read the clock as well.

    @classmethod
    def _utc_now(cls) -> str:
        return utc_now()

    @classmethod
    def _today(cls) -> str:
        return today()

    @classmethod
    def _current_quarter(cls) -> str:
        return current_quarter()

    # The same reasoning covers ``workstack.service.dt``: substituting the
    # datetime module here must still reach the domain, so the three clock
    # READS the extracted modules perform are named methods that resolve ``dt``
    # from this module. Pure parsing (``dt.date.fromisoformat`` and friends)
    # stays where it is used - it reads no clock.

    @classmethod
    def _local_time(cls) -> str:
        """The local wall-clock HH:MM a check-in defaults to."""

        return dt.datetime.now().strftime("%H:%M")

    @classmethod
    def _local_day(cls) -> dt.date:
        """The local calendar day a weekly range ends on by default."""

        return dt.date.today()

    @classmethod
    def _generated_at(cls) -> str:
        """The UTC instant stamped on a graph snapshot."""

        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    @_task_patch_backends
    @_transactional
    def patch_task(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise DomainError("task patch must be an object")
        unknown = sorted(set(patch) - TASK_PATCH_FIELDS)
        if unknown:
            raise DomainError("unknown task fields", {"fields": unknown})
        expected_revision = patch.get("revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise DomainError("revision is required and must be a non-negative integer")
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog.get("tasks", []), task_id, "task")
        current_revision = _revision(task)
        if expected_revision != current_revision:
            raise RevisionConflictError(
                "task revision is stale",
                {"expected": current_revision, "received": expected_revision},
            )
        _require_released_composition_for_refs(self, patch)
        tasks_by_id = {item["id"]: item for item in backlog.get("tasks", [])}
        objectives_by_id = _objective_records_by_id(
            self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])
        )
        objectives = set(objectives_by_id)
        changes, requested_status = _patch_change_set(
            patch, task, tasks_by_id, objectives
        )
        _apply_task_outcome_write_invariant(changes, task, objectives_by_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        current_status = validate_and_project(backlog, activity)[task["id"]]
        changed_fields = _patch_changed_fields(
            changes, requested_status, current_status
        )
        if not changed_fields:
            return self._project_task(task, planning_status=current_status)
        next_revision = _next_revision(task)
        task.update(changes)
        projected_status = current_status
        if requested_status is not None and requested_status != current_status:
            append_transition(
                activity,
                task,
                prior_status=current_status,
                status=requested_status,
                prior_revision=current_revision,
                new_revision=next_revision,
                created_at=self._utc_now(),
                actor="local.user",
                provenance="api.v1",
            )
            projected_status = requested_status
            self._record_task_status_notice(activity, task, current_status, requested_status, current_revision, next_revision, self._status_notice_key(task, current_revision), "gui")
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self._event(
            activity,
            "task.updated",
            task_id=task["id"],
            details={"fields": changed_fields},
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: backlog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-patch-{}-r{}".format(task["id"], task["revision"]),
        )
        return self._project_task(task, planning_status=projected_status)
