"""Domain logic shared by the CLI and web API."""

from __future__ import annotations

import datetime as dt
import copy
import re
import secrets
import unicodedata
import uuid
from functools import wraps
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlsplit

from . import REMOTE_PROTOCOL_VERSION, __version__
from .context_projection import group_context_by_task
from .capture import (
    EMAIL_RE,
    PercentDecodingLimitError,
    RECIPIENT_ASSIGNMENT_RE,
    SHA256_RE,
    canonical_digest,
    credential_material_in_decoded_text,
    credential_material_in_decoded_url,
    decoded_for_validation,
    is_allowed_microsoft_hostname,
    parse_rfc3339,
    validate_capture_packet,
)
import re as _re

_CHECKPOINT_ID = _re.compile(r"CP-[0-9a-f]{64}")

from .checkpoint_projection import (
    active_worklog_document,
    build_audit,
    physical_locator_for,
)
from .checkpoint_transition import (
    CheckpointTransitionError,
    build_transition_event,
    build_transition_notice,
    next_transition,
    normalize_transition_request,
    verify_locator,
)
from .checkpoint_change import (
    CheckpointChangeError,
    build_checkpoint_facts,
    build_committed_notice,
)
from .planning_status import (
    append_bootstrap,
    append_transition,
    task_facts,
    validate_and_project,
)
from .maintenance import BackupDownload, create_backup_download
from .snapshot import SnapshotValidationError
from .snapshot_export import SnapshotArtifact, create_snapshot_artifact
from .storage.document_repository import (
    DocumentRepository,
    StoreDocumentRepository,
    WorkspaceDocument,
)
from .store import MAX_REVISION, Store, StoreCorruptError, StoreLockedError


TASK_STATUSES = ("open", "started", "done", "dropped")
OBJECTIVE_STATUSES = ("active", "done", "dropped")
PRIORITIES = ("P0", "P1", "P2", "P3")
CAPTURE_STATUSES = ("inbox", "linked", "converted", "dismissed")
REPLY_CAPABILITIES = {
    "microsoft-outlook": "outlook.reply",
    "microsoft-teams": "teams.reply",
}
REPLY_STATES = ("approved", "sent", "failed", "unknown")
REPLY_OUTCOMES = REPLY_STATES[1:]
REPLY_TARGET_FIELDS = (
    "resource_type",
    "connection_ref",
    "container_ref",
    "object_ref",
    "version_ref",
)
REPLY_BODY_MAX = 12_000
REPLY_TARGET_REF_MAX = 512
REMOTE_MESSAGE_REF_MAX = 512
MICROSOFT_WEB_URL_MAX = 4096
ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
REMOTE_MESSAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:@/+%=-]{0,511}$")
REMOTE_HEADER_PREFIX_RE = re.compile(
    r"(?i)^(?:from|to|cc|bcc|subject|sent|date):"
)
HTML_TAG_RE = re.compile(r"<\/?[A-Za-z][^>]*>")
MAIL_HEADER_RE = re.compile(r"(?im)^(?:from|to|cc|bcc|subject|sent|date):\s*.+$")
QUOTED_REPLY_RE = re.compile(r"(?im)^on .{1,240} wrote:\s*$")
QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
SECRET_TEXT_RE = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"\b(?:access_token|refresh_token|id_token)\b\s*[:=]\s*[^\s&]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b)"
)
RAW_CANARY_RE = re.compile(r"(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE", re.I)
TASK_PATCH_FIELDS = frozenset({
    "title",
    "detail",
    "status",
    "priority",
    "due",
    "scheduled",
    "estimate_minutes",
    "tags",
    "objective_ids",
    "parent_id",
    "dependencies",
    "key_result_refs",
    "revision",
})


class DomainError(ValueError):
    code = "invalid_request"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


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


def _capture_reply_command(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except ValueError as error:
        if getattr(error, "command_boundary", None) != "capture-reply":
            raise
        repository_code = str(getattr(error, "code", "invalid_request"))
        error_type = _CAPTURE_REPLY_ERROR_TYPES.get(repository_code, DomainError)
        raise error_type(
            "capture/reply command was refused",
            {"repository_code": repository_code},
        ) from error


def _capture_reply_backend(method):
    @wraps(method)
    def wrapped(self: "WorkStack", *args: Any, **kwargs: Any):
        commands = self.capture_reply_commands
        if commands is not None:
            command = getattr(commands, method.__name__)
            return _capture_reply_command(lambda: command(*args, **kwargs))
        return method(self, *args, **kwargs)

    return wrapped


def _optional_command(
    action: Callable[[], Any], boundary: str
) -> Any:
    try:
        return action()
    except (ValueError, RuntimeError) as error:
        if getattr(error, "command_boundary", None) != boundary:
            raise
        repository_code = str(getattr(error, "code", "invalid_request"))
        error_type = _OPTIONAL_COMMAND_ERROR_TYPES.get(repository_code, DomainError)
        raise error_type(
            "{} command was refused".format(boundary),
            {"repository_code": repository_code},
        ) from error


def _optional_command_backend(attribute: str, backend_method: str, boundary: str):
    def decorate(method):
        @wraps(method)
        def wrapped(self: "WorkStack", *args: Any, **kwargs: Any):
            commands = getattr(self, attribute)
            if commands is not None:
                command = getattr(commands, backend_method)
                return _optional_command(
                    lambda: command(*args, **kwargs), boundary
                )
            return method(self, *args, **kwargs)

        return wrapped

    return decorate


_TASK_SCALAR_PATCH_FIELDS = {
    "title", "detail", "priority", "due", "scheduled", "estimate_minutes"
}
_TASK_RELATIONSHIP_PATCH_FIELDS = {"parent_id", "dependencies"}


def _relationship_task_projection(
    stack: "WorkStack", task_id: str, receipt: Any
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
    def wrapped(self: "WorkStack", task_id: str, patch: dict[str, Any]):
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
    stack: "WorkStack", method, task_id: str, patch: dict[str, Any]
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


def _query_search_backend(method):
    @wraps(method)
    def wrapped(self: "WorkStack", query: str, limit: int = 30):
        if self.query_commands is None:
            return method(self, query, limit)
        result = _optional_command(
            lambda: self.query_commands.search(query, limit=limit), "query"
        )
        return result.to_released_projection()

    return wrapped


def _query_graph_backend(method):
    @wraps(method)
    def wrapped(self: "WorkStack"):
        projection = method(self)
        if self.query_commands is None:
            return projection
        result = _optional_command(lambda: self.query_commands.graph(), "query")
        projection["edges"] = [
            {"kind": kind, "source": source, "target": target}
            for kind, source, target in result.edges
        ]
        return projection

    return wrapped


def _attributed_released_v3_only(attribute: str, backend_method: str, boundary: str):
    """Dispatch like ``_optional_command_backend``, but refuse an ATTRIBUTED call.

    ``_optional_command_backend`` runs before ``_transactional``, so a configured
    backend replaces the released-v3 implementation entirely. That backend's
    ``add_worklog`` accepts no provenance, so forwarding an attributed write
    there would silently drop the attribution and publish nothing while still
    reporting success.

    Method presence is never treated as evidence of capability: ANY configured
    backend refuses an attributed call, before the backend call and before any
    document write. Unattributed calls keep ordinary backend behaviour exactly,
    with the released-only keyword removed so the backend signature is unchanged.
    """

    def decorate(method):
        @wraps(method)
        def wrapped(self: "WorkStack", *args: Any, **kwargs: Any):
            # The document composition is checked FIRST, before any backend
            # dispatch and before any document write. An injected repository
            # that merely satisfies the DocumentRepository protocol is not the
            # admitted released composition: protocol method presence is not
            # capability, and the existing exact-adapter seam is what decides.
            if kwargs.get("origin") is not None and not _attributed_released_composition(self):
                raise DomainError(
                    "attributed review entries are not supported by this storage composition"
                )
            commands = getattr(self, attribute)
            if commands is not None:
                if kwargs.get("origin") is not None:
                    raise DomainError(
                        "attributed review entries are not supported by this backend"
                    )
                forwarded = {key: value for key, value in kwargs.items() if key != "origin"}
                command = getattr(commands, backend_method)
                return _optional_command(
                    lambda: command(*args, **forwarded), boundary
                )
            return method(self, *args, **kwargs)

        return wrapped

    return decorate


def _transactional(method):
    @wraps(method)
    def wrapped(self: "WorkStack", *args: Any, **kwargs: Any):
        with self.store.transaction():
            return method(self, *args, **kwargs)

    return wrapped


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return dt.date.today().isoformat()


def current_quarter(day: dt.date | None = None) -> str:
    day = day or dt.date.today()
    return "{}-Q{}".format(day.year, ((day.month - 1) // 3) + 1)


def _next_id(records: Iterable[dict[str, Any]], prefix: str, width: int = 0) -> str:
    pattern = re.compile(r"^{}-(\d+)$".format(re.escape(prefix)), re.I)
    largest = 0
    for record in records:
        match = pattern.match(str(record.get("id", "")))
        if match:
            largest = max(largest, int(match.group(1)))
    number = largest + 1
    return "{}-{:0{width}d}".format(prefix.upper(), number, width=width) if width else "{}-{}".format(prefix.upper(), number)


def _capture_review_digest(capture: dict[str, Any]) -> str:
    """Digest reviewed fields while ignoring server-owned action links."""

    normalized = copy.deepcopy(capture.get("normalized", {}))
    for action in normalized.get("action_items", []):
        if isinstance(action, dict):
            action.pop("task_id", None)
    return canonical_digest({
        "normalized": normalized,
        "task_hints": copy.deepcopy(capture.get("task_hints", [])),
    })


def _require_matching_capture_review(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> None:
    if _capture_review_digest(existing) != _capture_review_digest(incoming):
        raise SourceRevisionConflictError(
            "the same source fingerprint has different reviewed capture content"
        )


def _capture_task_intent_id(task_fields: dict[str, Any]) -> str | None:
    value = task_fields.get("intent_id")
    if value is None:
        return None
    try:
        normalized = str(uuid.UUID(value)) if isinstance(value, str) else ""
        valid = normalized == value and uuid.UUID(normalized).int != 0
    except ValueError:
        valid = False
    if not valid:
        raise DomainError(
            "intent_id must be a canonical non-nil UUID",
            {"field": "intent_id"},
        )
    return normalized


def _validate_capture_task_fields(
    task_fields: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    allowed = {
        "intent_id", "title", "detail", "priority", "due", "tags",
        "objective_ids", "parent_id", "dependencies",
    }
    unknown = sorted(set(task_fields) - allowed)
    if unknown:
        raise DomainError("unknown task fields", {"fields": unknown})
    if "title" not in task_fields or not isinstance(task_fields["title"], str):
        raise DomainError("title is required and must be a string", {"field": "title"})
    _validate_capture_task_scalars(task_fields)
    _validate_capture_task_arrays(task_fields)
    intent_id = _capture_task_intent_id(task_fields)
    task_input = {field: value for field, value in task_fields.items() if field != "intent_id"}
    return intent_id, task_input


def _validate_capture_task_scalars(task_fields: dict[str, Any]) -> None:
    for field in ("detail", "priority"):
        if field in task_fields and not isinstance(task_fields[field], str):
            raise DomainError("{} must be a string".format(field), {"field": field})
    due = task_fields.get("due")
    if "due" in task_fields and due is not None and not isinstance(due, str):
        raise DomainError("due must be an ISO date or null", {"field": "due"})
    parent_id = task_fields.get("parent_id")
    if "parent_id" in task_fields and parent_id is not None and not isinstance(parent_id, str):
        raise DomainError("parent_id must be a task ID or null", {"field": "parent_id"})


def _validate_capture_task_arrays(task_fields: dict[str, Any]) -> None:
    for field in ("tags", "objective_ids", "dependencies"):
        value = task_fields.get(field)
        if field in task_fields and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            raise DomainError("{} must be an array of strings".format(field), {"field": field})


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("{} is required".format(field))
    return text


def _find(records: Iterable[dict[str, Any]], record_id: str, label: str) -> dict[str, Any]:
    wanted = record_id.strip().upper()
    for record in records:
        if str(record.get("id", "")).upper() == wanted:
            return record
    raise NotFoundError("unknown {}: {}".format(label, record_id), {"id": record_id})


def _relationship_reaches(
    tasks_by_id: dict[str, dict[str, Any]],
    start_ids: Iterable[str],
    target_id: str,
    field: str,
) -> bool:
    """Return whether following one relationship kind reaches ``target_id``."""

    pending = list(start_ids)
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        task = tasks_by_id.get(current)
        if task is None:
            continue
        if field == "parent_id":
            parent = task.get("parent_id")
            if isinstance(parent, str) and parent:
                pending.append(parent)
        else:
            pending.extend(
                dependency
                for dependency in task.get("dependencies", [])
                if isinstance(dependency, str) and dependency
            )
    return False


def _validate_patch_local_date(value: Any, field: str) -> None:
    if value is None:
        return
    message = "{} must be an ISO date or null".format(field)
    if not isinstance(value, str):
        raise DomainError(message)
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise DomainError(message) from error
    if parsed.isoformat() != value:
        raise DomainError(message)


def _normalize_patch_title(changes: dict[str, Any]) -> None:
    if "title" not in changes:
        return
    if not isinstance(changes["title"], str):
        raise DomainError("title must be a string")
    changes["title"] = _required_text(changes["title"], "title")


def _normalize_patch_detail(changes: dict[str, Any]) -> None:
    if "detail" not in changes:
        return
    if not isinstance(changes["detail"], str):
        raise DomainError("detail must be a string")
    changes["detail"] = changes["detail"].strip()


def _validate_patch_enums(changes: dict[str, Any]) -> None:
    if "status" in changes and changes["status"] not in TASK_STATUSES:
        raise DomainError("invalid task status")
    if "priority" in changes and changes["priority"] not in PRIORITIES:
        raise DomainError("invalid task priority")


def _validate_patch_estimate(changes: dict[str, Any]) -> None:
    if "estimate_minutes" not in changes or changes["estimate_minutes"] is None:
        return
    estimate = changes["estimate_minutes"]
    if (
        not isinstance(estimate, int)
        or isinstance(estimate, bool)
        or not 1 <= estimate <= 1440
    ):
        raise DomainError(
            "estimate_minutes must be null or an integer from 1 to 1440"
        )


def _normalize_patch_scalar_fields(changes: dict[str, Any]) -> None:
    _normalize_patch_title(changes)
    _normalize_patch_detail(changes)
    _validate_patch_enums(changes)
    for field in ("due", "scheduled"):
        if field in changes:
            _validate_patch_local_date(changes[field], field)
    _validate_patch_estimate(changes)


def _require_patch_array(changes: dict[str, Any], field: str) -> None:
    if field in changes and not isinstance(changes[field], list):
        raise DomainError("{} must be an array".format(field))


def _normalize_patch_tags(changes: dict[str, Any]) -> None:
    if "tags" not in changes:
        return
    if any(not isinstance(item, str) for item in changes["tags"]):
        raise DomainError("tags entries must be strings")
    changes["tags"] = sorted(
        {item.strip() for item in changes["tags"] if item.strip()}
    )


def _normalize_patch_objectives(
    changes: dict[str, Any], objectives: set[str]
) -> None:
    if "objective_ids" not in changes:
        return
    if any(not isinstance(item, str) for item in changes["objective_ids"]):
        raise DomainError("objective_ids entries must be strings")
    changes["objective_ids"] = sorted(
        {item.strip().upper() for item in changes["objective_ids"] if item.strip()}
    )
    missing = sorted(set(changes["objective_ids"]) - objectives)
    if missing:
        raise DomainError("unknown objective ids", {"ids": missing})


KEY_RESULT_REF_FIELDS = frozenset({"objective_id", "key_result_id"})


def _normalized_ref_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DomainError("key_result_refs {} must be a string".format(field))
    normalized = value.strip().upper()
    if not normalized:
        raise DomainError("key_result_refs {} must not be blank".format(field))
    return normalized


def _normalized_key_result_ref(item: Any) -> dict[str, str]:
    if not isinstance(item, dict) or set(item) != KEY_RESULT_REF_FIELDS:
        raise DomainError("key_result_refs entries must be exact scoped pairs")
    return {
        "objective_id": _normalized_ref_id(item["objective_id"], "objective_id"),
        "key_result_id": _normalized_ref_id(item["key_result_id"], "key_result_id"),
    }


def _normalize_patch_key_result_refs(changes: dict[str, Any]) -> None:
    if "key_result_refs" not in changes:
        return
    pairs = [
        (ref["objective_id"], ref["key_result_id"])
        for ref in (_normalized_key_result_ref(item) for item in changes["key_result_refs"])
    ]
    if len(set(pairs)) != len(pairs):
        raise DomainError("key_result_refs entries must be unique")
    changes["key_result_refs"] = [
        {"objective_id": objective_id, "key_result_id": key_result_id}
        for objective_id, key_result_id in sorted(pairs)
    ]


def _require_single_key_result(objective: dict[str, Any], key_result_id: str) -> None:
    matches = [
        item
        for item in objective.get("key_results", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip().upper() == key_result_id
    ]
    if len(matches) != 1:
        raise DomainError(
            "unknown key result reference", {"key_result_id": key_result_id}
        )


def _resolve_reference_objective(
    objective_id: str, aligned: set[str], objectives_by_id: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Resolve exactly one aligned Objective record, keeping duplicate multiplicity."""

    if objective_id not in aligned:
        raise DomainError(
            "key result reference parent is not aligned",
            {"objective_id": objective_id},
        )
    matches = objectives_by_id.get(objective_id, [])
    if len(matches) != 1:
        raise DomainError(
            "key result reference objective is not uniquely resolvable",
            {"objective_id": objective_id},
        )
    return matches[0]


def _validate_key_result_refs_state(
    refs: Any, objective_ids: Any, objectives_by_id: dict[str, list[dict[str, Any]]]
) -> None:
    """Refuse any resulting reference whose scoped target is unaligned or unresolvable."""

    if refs is None:
        return
    aligned = set(objective_ids or [])
    for ref in refs:
        objective = _resolve_reference_objective(
            ref["objective_id"], aligned, objectives_by_id
        )
        _require_single_key_result(objective, ref["key_result_id"])


def _objective_records_by_id(
    records: Iterable[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Group Objective records by ID, preserving duplicate multiplicity."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["id"], []).append(record)
    return grouped


def _released_v3_document_composition(stack: "WorkStack") -> bool:
    """Recognize only the released v3 document composition, by exact adapter type.

    ``StoreDocumentRepository`` is the released adapter that alone knows how the
    released physical store represents these documents, and it wraps the released
    ``Store``.  Any other object satisfying the ``DocumentRepository`` protocol is
    an unadmitted composition: protocol method presence is not capability, and the
    protocol exposes no capability predicate to ask.
    """

    documents = stack.documents
    return (
        type(documents) is StoreDocumentRepository
        and type(getattr(documents, "_store", None)) is Store
    )


def _attributed_released_composition(stack: "WorkStack") -> bool:
    """The released composition AND the Store that actually transacts and publishes.

    Exact adapter and Store types are not enough. ``WorkStack.__init__`` accepts
    an independently supplied repository, so a released adapter can legitimately
    wrap a DIFFERENT Store: the documents would then be written in one Store
    while the outer transaction, the workspace identity behind the recorded fact
    and the typed publication all belong to another. Attribution requires those
    to be the same object, checked by identity through the existing seams rather
    than inferred from any capability surface.
    """

    return (
        _released_v3_document_composition(stack)
        and getattr(stack.documents, "_store", None) is stack.store
    )


def _released_v3_attributed_composition_owner(stack: "WorkStack") -> bool:
    """The narrow released composition bound to the transacting Store itself.

    Every new D5 write uses this, INCLUDING a null-origin browser write: a
    transition is durable evidence, so it may only be produced by the admitted
    composition writing through the same Store that transacts and publishes.
    Unrelated ordinary commands keep their existing behaviour.
    """

    documents = stack.documents
    return (
        type(documents) is StoreDocumentRepository
        and type(getattr(documents, "_store", None)) is Store
        and documents._store is stack.store
    )


def _require_released_composition_for_refs(
    stack: "WorkStack", patch: dict[str, Any]
) -> None:
    """Refuse a scoped-ref mutation, including [], on an unadmitted composition."""

    if "key_result_refs" in patch and not _released_v3_document_composition(stack):
        raise DomainError(
            "key result references are not supported by this storage composition"
        )


def _normalize_patch_dependencies(changes: dict[str, Any]) -> None:
    if "dependencies" not in changes:
        return
    if any(not isinstance(item, str) for item in changes["dependencies"]):
        raise DomainError("dependencies entries must be strings")
    changes["dependencies"] = sorted(
        {item.strip().upper() for item in changes["dependencies"] if item.strip()}
    )


def _normalize_patch_collection_fields(
    changes: dict[str, Any], objectives: set[str]
) -> None:
    for field in ("tags", "objective_ids", "dependencies", "key_result_refs"):
        _require_patch_array(changes, field)
    _normalize_patch_tags(changes)
    _normalize_patch_objectives(changes, objectives)
    _normalize_patch_dependencies(changes)
    _normalize_patch_key_result_refs(changes)


def _validate_patch_dependencies(
    changes: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    if "dependencies" not in changes:
        return
    missing = sorted(set(changes["dependencies"]) - set(tasks_by_id))
    if missing or task["id"] in changes["dependencies"]:
        raise DomainError("invalid dependency ids", {"ids": missing})
    cyclic_dependencies = [
        dependency
        for dependency in changes["dependencies"]
        if _relationship_reaches(
            tasks_by_id, [dependency], task["id"], "dependencies"
        )
    ]
    if cyclic_dependencies:
        raise DomainError(
            "dependency relationship would create a cycle",
            {"ids": cyclic_dependencies},
        )


def _normalize_and_validate_patch_parent(
    changes: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    if "parent_id" not in changes:
        return
    parent = changes["parent_id"]
    if parent is not None and not isinstance(parent, str):
        raise DomainError("parent_id must be a task ID or null")
    parent = parent.strip().upper() if parent else None
    if parent == task["id"] or (parent and parent not in tasks_by_id):
        raise DomainError("invalid parent task")
    if parent and _relationship_reaches(
        tasks_by_id, [parent], task["id"], "parent_id"
    ):
        raise DomainError(
            "parent relationship would create a cycle",
            {"id": parent},
        )
    changes["parent_id"] = parent


def _normalize_and_validate_patch_relationships(
    changes: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    _validate_patch_dependencies(changes, task, tasks_by_id)
    _normalize_and_validate_patch_parent(changes, task, tasks_by_id)


def _patch_change_set(
    patch: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
    objectives: set[str],
) -> tuple[dict[str, Any], Any]:
    changes = {key: value for key, value in patch.items() if key != "revision"}
    _normalize_patch_scalar_fields(changes)
    _normalize_patch_collection_fields(changes, objectives)
    _normalize_and_validate_patch_relationships(changes, task, tasks_by_id)
    return changes, changes.pop("status", None)


def _patch_changed_fields(
    changes: dict[str, Any], requested_status: Any, current_status: str
) -> list[str]:
    fields = sorted(changes)
    if requested_status is not None and requested_status != current_status:
        fields.append("status")
    return fields


def _task_uid(workspace_id: str, task_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(workspace_id), task_id))


def _revision(record: dict[str, Any]) -> int:
    if "revision" not in record:
        raise StoreCorruptError("persisted task revision is missing")
    value = record["revision"]
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise StoreCorruptError("persisted task revision is invalid")
    return value


def _guard_revision(task: dict[str, Any], expected_revision: int | None) -> int:
    current_revision = _revision(task)
    if expected_revision is None:
        return _next_revision(task)
    if type(expected_revision) is not int or expected_revision < 0:
        raise DomainError("revision is required and must be a non-negative integer")
    if expected_revision != current_revision:
        raise RevisionConflictError(
            "task revision is stale",
            {"expected": current_revision, "received": expected_revision},
        )
    return _next_revision(task)


def _next_revision(record: dict[str, Any]) -> int:
    current = _revision(record)
    if current == MAX_REVISION:
        raise RevisionExhaustedError(
            "task revision cannot advance beyond the safe integer limit",
            {"maximum": MAX_REVISION},
        )
    return current + 1


def _reject_controls(value: str, field: str, *, multiline: bool) -> str:
    allowed = {"\n", "\r", "\t"} if multiline else set()
    if any(
        character not in allowed and unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise DomainError("{} contains control characters".format(field), {"field": field})
    if not multiline and ("\n" in value or "\r" in value):
        raise DomainError("{} must be a single line".format(field), {"field": field})
    return value


def _approved_plain_text(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainError("body must be a string", {"field": "body"})
    if not value.strip():
        raise DomainError("body is required", {"field": "body"})
    if len(value) > REPLY_BODY_MAX:
        raise DomainError(
            "body exceeds {} characters".format(REPLY_BODY_MAX),
            {"field": "body", "maximum": REPLY_BODY_MAX},
        )
    _reject_controls(value, "body", multiline=True)
    if (
        HTML_TAG_RE.search(value)
        or RAW_CANARY_RE.search(value)
        or QUOTED_REPLY_RE.search(value)
        or len(MAIL_HEADER_RE.findall(value)) >= 2
        or len(QUOTE_LINE_RE.findall(value)) >= 4
    ):
        raise DomainError("body contains HTML or unsafe raw content", {"field": "body"})
    try:
        decoded = decoded_for_validation(value)
    except PercentDecodingLimitError as error:
        raise DomainError(
            "body exceeds the percent-encoding validation depth", {"field": "body"}
        ) from error
    if SECRET_TEXT_RE.search(decoded) or credential_material_in_decoded_text(decoded):
        raise DomainError("body appears to contain an authentication token", {"field": "body"})
    return value


def _reference_views(value: Any, field: str, maximum: int) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise DomainError("{} must be a non-empty string".format(field), {"field": field})
    if len(value) > maximum:
        raise DomainError(
            "{} exceeds {} characters".format(field, maximum),
            {"field": field, "maximum": maximum},
        )
    try:
        decoded = decoded_for_validation(value)
    except PercentDecodingLimitError as error:
        raise DomainError(
            "{} exceeds the percent-encoding validation depth".format(field),
            {"field": field},
        ) from error
    _reject_controls(decoded, field, multiline=False)
    if (
        HTML_TAG_RE.search(decoded)
        or RAW_CANARY_RE.search(decoded)
        or RECIPIENT_ASSIGNMENT_RE.search(decoded)
    ):
        raise DomainError("{} contains unsafe content".format(field), {"field": field})
    if SECRET_TEXT_RE.search(decoded) or credential_material_in_decoded_text(decoded):
        raise DomainError("{} appears to contain an authentication token".format(field), {"field": field})
    return value, decoded


def _opaque_reference(value: Any, field: str, maximum: int) -> str:
    reference, _ = _reference_views(value, field, maximum)
    return reference


def _remote_message_reference(value: Any) -> str:
    reference, decoded = _reference_views(
        value, "remote_message_ref", REMOTE_MESSAGE_REF_MAX
    )
    if (
        not REMOTE_MESSAGE_REF_RE.fullmatch(reference)
        or not REMOTE_MESSAGE_REF_RE.fullmatch(decoded)
        or "://" in decoded
        or EMAIL_RE.search(decoded)
        or REMOTE_HEADER_PREFIX_RE.search(decoded)
        or RECIPIENT_ASSIGNMENT_RE.search(decoded)
        or HTML_TAG_RE.search(decoded)
        or RAW_CANARY_RE.search(decoded)
    ):
        raise DomainError(
            "remote_message_ref must be an opaque Microsoft message identifier",
            {"field": "remote_message_ref"},
        )
    return reference


def _microsoft_web_url(value: Any) -> str:
    url, decoded = _reference_views(value, "web_url", MICROSOFT_WEB_URL_MAX)
    if EMAIL_RE.search(decoded) or RECIPIENT_ASSIGNMENT_RE.search(decoded):
        raise DomainError(
            "web_url must not contain recipient material", {"field": "web_url"}
        )
    if credential_material_in_decoded_url(decoded):
        raise DomainError(
            "web_url appears to contain an authentication token", {"field": "web_url"}
        )
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
        invalid = (
            parsed.scheme.casefold() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or (parsed.port is not None and parsed.port != 443)
            or not is_allowed_microsoft_hostname(hostname)
        )
    except ValueError:
        invalid = True
    if invalid:
        raise DomainError(
            "web_url must be a token-free HTTPS URL on an allowed Microsoft host",
            {"field": "web_url"},
        )
    return url


REPLY_RECEIPT_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "reply_id",
    "provider",
    "outcome",
    "occurred_at",
    "body_digest",
    "target_digest",
})


class CaptureReplyCommands(Protocol):
    """Backend-neutral completed capture/reply command slice."""

    def ingest_capture(
        self,
        packet: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        method: str = "POST",
        path: str = "/api/v1/captures",
    ) -> dict[str, Any]: ...

    def link_capture(
        self,
        capture_id: str,
        task_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...

    def approve_reply(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str = "/api/v1/replies",
    ) -> dict[str, Any]: ...

    def apply_reply_receipt(
        self,
        reply_id: str,
        receipt: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...


class IntentCommands(Protocol):
    def create_note(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def checkin(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def add_worklog(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...


class ObjectiveCommands(Protocol):
    def create_objective(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def add_key_result(
        self,
        objective_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]: ...


class TaskCommands(Protocol):
    def create_task_v1(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def patch_task(
        self, task_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]: ...


class RelationshipCommands(Protocol):
    def patch_relationships(
        self, task_id: str, request: dict[str, Any]
    ) -> Any: ...

    def delete_task(self, task_id: str, expected_revision: int) -> Any: ...


class PlanningCommands(Protocol):
    def set_task_status(
        self,
        task_id: str,
        status: str,
        expected_revision: int | None = None,
        *,
        provenance: str = "cli",
    ) -> dict[str, Any]: ...

    def patch_status(
        self, task_id: str, status: str, expected_revision: int
    ) -> dict[str, Any]: ...


class WorkSessionCommands(Protocol):
    def projection(self) -> dict[str, Any]: ...

    def start(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def transition(
        self,
        session_id: str,
        action: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...

    def record_worklog(
        self,
        session_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...


class QueryCommands(Protocol):
    def search(self, query: str, *, limit: int = 50) -> Any: ...

    def graph(self) -> Any: ...
REPLY_RECEIPT_OPTIONAL_FIELDS = frozenset({
    "remote_message_ref", "web_url", "error_code"
})


def _validate_reply_receipt_shape(receipt: dict[str, Any]) -> None:
    unknown = sorted(
        set(receipt) - REPLY_RECEIPT_REQUIRED_FIELDS - REPLY_RECEIPT_OPTIONAL_FIELDS
    )
    missing = sorted(REPLY_RECEIPT_REQUIRED_FIELDS - set(receipt))
    if unknown or missing:
        raise DomainError(
            "reply receipt has unknown or missing fields",
            {"missing": missing, "unknown": unknown},
        )


def _reply_receipt_provider(receipt: dict[str, Any]) -> str:
    provider = receipt["provider"]
    if not isinstance(provider, str) or provider not in REPLY_CAPABILITIES:
        raise DomainError("provider is not supported", {"field": "provider"})
    return provider


def _reply_receipt_outcome(receipt: dict[str, Any]) -> str:
    outcome = receipt["outcome"]
    if outcome not in REPLY_OUTCOMES:
        raise DomainError(
            "outcome must be sent, failed, or unknown", {"field": "outcome"}
        )
    return outcome


def _reply_receipt_occurred_at(receipt: dict[str, Any]) -> str:
    occurred_at = receipt["occurred_at"]
    if not isinstance(occurred_at, str):
        raise DomainError("occurred_at must be a string", {"field": "occurred_at"})
    try:
        parse_rfc3339(occurred_at, "occurred_at")
    except ValueError as error:
        raise DomainError(
            "occurred_at must be strict RFC3339", {"field": "occurred_at"}
        ) from error
    return occurred_at


def _reply_receipt_digests(receipt: dict[str, Any]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for field in ("body_digest", "target_digest"):
        value = receipt[field]
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise DomainError(
                "{} must be canonical SHA-256".format(field), {"field": field}
            )
        digests[field] = value
    return digests


def _project_reply_receipt_optional_fields(
    receipt: dict[str, Any], projected: dict[str, Any]
) -> None:
    if "remote_message_ref" in receipt:
        projected["remote_message_ref"] = _remote_message_reference(
            receipt["remote_message_ref"]
        )
    if "web_url" in receipt:
        projected["web_url"] = _microsoft_web_url(receipt["web_url"])
    if "error_code" in receipt:
        error_code = receipt["error_code"]
        if not isinstance(error_code, str) or not ERROR_CODE_RE.fullmatch(error_code):
            raise DomainError(
                "error_code must be a bounded symbolic code",
                {"field": "error_code"},
            )
        projected["error_code"] = error_code


def _reply_receipt_mismatches(
    receipt: dict[str, Any], reply: dict[str, Any]
) -> list[str]:
    mismatched: list[str] = []
    if receipt["reply_id"] != reply["id"]:
        mismatched.append("reply_id")
    if receipt["provider"] != reply["provider"]:
        mismatched.append("provider")
    for field in ("body_digest", "target_digest"):
        if not secrets.compare_digest(receipt[field], reply[field]):
            mismatched.append(field)
    return mismatched


def _apply_terminal_reply_state(
    reply: dict[str, Any], receipt: dict[str, Any]
) -> tuple[bool, dict[str, Any] | None]:
    stored_receipt = reply.get("receipt")
    if stored_receipt is not None:
        if stored_receipt != receipt:
            raise ReplyReceiptConflictError(
                "reply already has a different terminal receipt",
                {"reply_id": reply["id"], "state": reply.get("state")},
            )
        return True, None
    if reply.get("state") != "approved":
        raise ReplyReceiptConflictError(
            "reply is already terminal",
            {"reply_id": reply["id"], "state": reply.get("state")},
        )
    reply["state"] = receipt["outcome"]
    reply["receipt"] = receipt
    reply["updated_at"] = utc_now()
    event_details: dict[str, Any] = {
        "provider": reply["provider"],
        "state": reply["state"],
    }
    if "error_code" in receipt:
        event_details["error_code"] = receipt["error_code"]
    return False, event_details


TASK_CREATE_FIELDS = frozenset({
    "title", "detail", "priority", "due", "scheduled", "estimate_minutes",
    "tags", "objective_ids",
})


def _validate_new_task_schedule(
    priority: str,
    due: str | None,
    scheduled: str | None,
    estimate_minutes: int | None,
) -> None:
    if priority not in PRIORITIES:
        raise ValueError("priority must be one of {}".format(", ".join(PRIORITIES)))
    if due:
        dt.date.fromisoformat(due)
    if scheduled:
        dt.date.fromisoformat(scheduled)
    if estimate_minutes is not None and (
        not isinstance(estimate_minutes, int)
        or isinstance(estimate_minutes, bool)
        or not 1 <= estimate_minutes <= 1440
    ):
        raise ValueError("estimate_minutes must be null or an integer from 1 to 1440")


def _normalize_new_task_relationships(
    tasks: list[dict[str, Any]],
    parent_id: str | None,
    dependencies: Iterable[str],
) -> tuple[str | None, list[str]]:
    known_tasks = {item["id"] for item in tasks}
    normalized_parent = parent_id.strip().upper() if parent_id else None
    normalized_dependencies = sorted(
        set(str(item).strip().upper() for item in dependencies if str(item).strip())
    )
    referenced = ({normalized_parent} if normalized_parent else set()) | set(
        normalized_dependencies
    )
    unknown_tasks = sorted(item for item in referenced if item not in known_tasks)
    if unknown_tasks:
        raise ValueError("unknown task ids: {}".format(", ".join(unknown_tasks)))
    return normalized_parent, normalized_dependencies


def _new_task_record(
    *,
    task_id: str,
    workspace_id: str,
    title: str,
    detail: str,
    priority: str,
    due: str | None,
    scheduled: str | None,
    estimate_minutes: int | None,
    tags: Iterable[str],
    objective_ids: Iterable[str],
    parent_id: str | None,
    dependencies: list[str],
) -> dict[str, Any]:
    return {
        "id": task_id,
        "uid": _task_uid(workspace_id, task_id),
        "title": _required_text(title, "title"),
        "detail": str(detail or "").strip(),
        "status": "open",
        "priority": priority,
        "due": due or None,
        "scheduled": scheduled or None,
        "estimate_minutes": estimate_minutes,
        "tags": sorted(set(str(tag).strip() for tag in tags if str(tag).strip())),
        "objective_ids": sorted(
            set(str(oid).strip().upper() for oid in objective_ids if str(oid).strip())
        ),
        "parent_id": parent_id,
        "dependencies": dependencies,
        "subtasks": [],
        "notes": [],
        "created": today(),
        "updated_at": today(),
        "revision": 0,
    }


def _validate_task_create_shape(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DomainError("request body must be a JSON object")
    unknown = sorted(set(body) - TASK_CREATE_FIELDS)
    if unknown:
        raise DomainError("task create has unknown fields", {"fields": unknown})
    return body


def _task_create_text_fields(body: dict[str, Any]) -> tuple[str, str, str]:
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DomainError("title must be a non-empty string", {"field": "title"})
    detail = body.get("detail", "")
    if not isinstance(detail, str):
        raise DomainError("detail must be a string", {"field": "detail"})
    priority = body.get("priority", "P2")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise DomainError(
            "priority must be one of {}".format(", ".join(PRIORITIES)),
            {"field": "priority"},
        )
    return title.strip(), detail.strip(), priority


def _task_create_date(body: dict[str, Any], field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    message = "{} must be null or YYYY-MM-DD".format(field)
    if not isinstance(value, str):
        raise DomainError(message, {"field": field})
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise DomainError(message, {"field": field}) from error
    if parsed.isoformat() != value:
        raise DomainError(message, {"field": field})
    return value


def _task_create_estimate(body: dict[str, Any]) -> int | None:
    value = body.get("estimate_minutes")
    if value is not None and (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 1440
    ):
        raise DomainError(
            "estimate_minutes must be null or an integer from 1 to 1440",
            {"field": "estimate_minutes"},
        )
    return value


def _task_create_string_list(
    body: dict[str, Any], field: str, *, uppercase: bool = False
) -> list[str]:
    values = body.get(field, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise DomainError("{} must be an array of strings".format(field), {"field": field})
    normalized = (value.strip() for value in values)
    if uppercase:
        normalized = (value.upper() for value in normalized)
    return sorted(set(value for value in normalized if value))


def _weekly_range(end: str | None, days: int) -> tuple[dt.date, dt.date]:
    if days < 1 or days > 366:
        raise ValueError("days must be between 1 and 366")
    end_day = dt.date.fromisoformat(end) if end else dt.date.today()
    return end_day - dt.timedelta(days=days - 1), end_day


def _weekly_project_slot(
    task_id: Any, entry: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task": entry.get("task") or task.get("title", task_id),
        "objective_ids": task.get("objective_ids", []),
        "done": [],
        "next": [],
        "blockers": [],
        "dates": [],
        "duration_seconds": 0,
    }


def _merge_weekly_entry(
    slot: dict[str, Any], entry: dict[str, Any], date: str
) -> None:
    duration_seconds = entry.get("duration_seconds", 0)
    if type(duration_seconds) is not int or duration_seconds < 0:
        raise StoreCorruptError("persisted worklog duration is invalid")
    slot["duration_seconds"] += duration_seconds
    for field in ("done", "next", "blockers"):
        for value in entry.get(field, []):
            if value not in slot[field]:
                slot[field].append(value)
    if date not in slot["dates"]:
        slot["dates"].append(date)


def _weekly_projects(
    worklog: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    start_day: dt.date,
    end_day: dt.date,
) -> dict[str, dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for date in sorted(worklog):
        parsed = dt.date.fromisoformat(date)
        if not start_day <= parsed <= end_day:
            continue
        for entry in worklog[date].get("entries", []):
            task_id = entry.get("task_id")
            task = tasks.get(task_id, {})
            slot = projects.setdefault(
                task_id, _weekly_project_slot(task_id, entry, task)
            )
            _merge_weekly_entry(slot, entry, date)
    return projects


def _weekly_objectives(
    projects: dict[str, dict[str, Any]],
    objectives: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    used = {
        objective_id
        for project in projects.values()
        for objective_id in project["objective_ids"]
    }
    return [
        {"id": objective_id, "objective": objectives[objective_id].get("objective", "")}
        for objective_id in sorted(used)
        if objective_id in objectives
    ]


def _snapshot_objective_node(objective: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": objective["id"],
        "kind": "objective",
        "title": objective["objective"],
        "status": objective.get("status", "active"),
        "meta": objective.get("quarter", ""),
        "quarter": objective.get("quarter", ""),
        "key_results": objective.get("key_results", []),
    }


def _snapshot_task_node(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "uid": task["uid"],
        "kind": "task",
        "title": task["title"],
        "status": task.get("status", "open"),
        "revision": task["revision"],
        "meta": "{} · {}".format(
            task.get("priority", "P2"), task.get("due") or "no due date"
        ),
        "detail": task.get("detail", ""),
        "tags": task.get("tags", []),
        "priority": task.get("priority", "P2"),
        "due": task.get("due"),
        "objective_ids": task.get("objective_ids", []),
        "parent_id": task.get("parent_id"),
        "dependencies": task.get("dependencies", []),
        "subtasks": task.get("subtasks", []),
    }


def _append_snapshot_task(
    task: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    known: set[str],
) -> None:
    node = _snapshot_task_node(task)
    nodes.append(node)
    known.add(node["id"])
    if task.get("parent_id"):
        edges.append(
            {"source": task["id"], "target": task["parent_id"], "kind": "parent"}
        )
    for dependency in task.get("dependencies", []):
        edges.append(
            {"source": task["id"], "target": dependency, "kind": "dependency"}
        )
    for objective_id in task.get("objective_ids", []):
        edges.append(
            {"source": task["id"], "target": objective_id, "kind": "objective"}
        )
    for subtask in task.get("subtasks", []):
        subtask_id = "{}-{}".format(task["id"], subtask["id"])
        nodes.append({
            "id": subtask_id,
            "kind": "subtask",
            "title": subtask["title"],
            "status": subtask.get("status", "open"),
            "meta": subtask.get("priority", "P2"),
        })
        known.add(subtask_id)
        edges.append(
            {"source": subtask_id, "target": task["id"], "kind": "parent"}
        )


def _append_snapshot_worklog(
    worklog: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    known: set[str],
) -> None:
    for date, day in sorted(worklog.items()):
        day_id = "D-" + date
        nodes.append({
            "id": day_id,
            "kind": "day",
            "title": date,
            "status": "recorded",
            "meta": "{} entries".format(len(day.get("entries", []))),
            "entry_count": len(day.get("entries", [])),
        })
        known.add(day_id)
        for entry in day.get("entries", []):
            if entry.get("task_id"):
                edges.append(
                    {"source": day_id, "target": entry["task_id"], "kind": "worklog"}
                )


def _append_snapshot_notes(
    notes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    known: set[str],
) -> None:
    for note in notes:
        nodes.append({
            "id": note["id"],
            "kind": "note",
            "title": note["text"],
            "status": "recorded",
            "meta": note.get("created", ""),
            "links": note.get("links", []),
        })
        known.add(note["id"])
        for link in note.get("links", []):
            edges.append({"source": note["id"], "target": link, "kind": "note"})


class WorkStack:
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

    def _append_task(
        self,
        data: dict[str, Any],
        title: str,
        detail: str = "",
        priority: str = "P2",
        due: str | None = None,
        tags: Iterable[str] = (),
        objective_ids: Iterable[str] = (),
        parent_id: str | None = None,
        dependencies: Iterable[str] = (),
        scheduled: str | None = None,
        estimate_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Validate the normal Task fields and append a new Task to ``data``."""

        _validate_new_task_schedule(priority, due, scheduled, estimate_minutes)
        normalized_parent, normalized_dependencies = _normalize_new_task_relationships(
            data["tasks"], parent_id, dependencies
        )
        task_id = _next_id(data["tasks"], "T", 4)
        workspace_id = self.documents.load(WorkspaceDocument.WORKSPACE)["id"]
        task = _new_task_record(
            task_id=task_id,
            workspace_id=workspace_id,
            title=title,
            detail=detail,
            priority=priority,
            due=due,
            scheduled=scheduled,
            estimate_minutes=estimate_minutes,
            tags=tags,
            objective_ids=objective_ids,
            parent_id=normalized_parent,
            dependencies=normalized_dependencies,
        )
        known = {item["id"] for item in self.list_objectives(status="all")}
        unknown = sorted(set(task["objective_ids"]) - known)
        if unknown:
            raise ValueError("unknown objective ids: {}".format(", ".join(unknown)))
        data["tasks"].append(task)
        return task

    @_transactional
    def add_task(
        self,
        title: str,
        detail: str = "",
        priority: str = "P2",
        due: str | None = None,
        tags: Iterable[str] = (),
        objective_ids: Iterable[str] = (),
        parent_id: str | None = None,
        dependencies: Iterable[str] = (),
    ) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = self._append_task(
            data,
            title,
            detail,
            priority,
            due,
            tags,
            objective_ids,
            parent_id,
            dependencies,
        )
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        append_bootstrap(
            activity,
            task,
            created_at=utc_now(),
            actor="local.user",
            provenance="cli",
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-create-cli-{}".format(task["id"]),
        )
        return task

    @staticmethod
    def _validate_task_create_v1(body: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical, strict v1 task-create payload."""

        body = _validate_task_create_shape(body)
        title, detail, priority = _task_create_text_fields(body)
        due = _task_create_date(body, "due")
        scheduled = _task_create_date(body, "scheduled")
        estimate_minutes = _task_create_estimate(body)

        return {
            "title": title,
            "detail": detail,
            "priority": priority,
            "due": due,
            "scheduled": scheduled,
            "estimate_minutes": estimate_minutes,
            "tags": _task_create_string_list(body, "tags"),
            "objective_ids": _task_create_string_list(
                body, "objective_ids", uppercase=True
            ),
        }

    @_optional_command_backend(
        "task_commands", "create_task_v1", "task"
    )
    @_transactional
    def create_task_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/tasks",
    ) -> dict[str, Any]:
        """Create one Task for one logical browser intent, or replay its frozen response."""

        self._validate_idempotency_key(idempotency_key)
        canonical_body = self._validate_task_create_v1(body)
        request_digest = self._request_digest(canonical_body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
        )
        if replay is not None:
            return replay

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = self._append_task(backlog, **canonical_body)
        append_bootstrap(
            activity,
            task,
            created_at=utc_now(),
            actor="local.user",
            provenance="api.v1",
        )
        response_body = {
            "data": self._project_task(task, planning_status="open"),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            201,
            response_body,
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: backlog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def add_task_note_v1(
        self,
        task_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        """Add one Task note for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog["tasks"], task_id, "task")
        next_revision = _guard_revision(task, body["revision"])
        note = {"date": today(), "text": _required_text(body["text"], "text")}
        task.setdefault("notes", []).append(note)
        task["updated_at"] = today()
        task["revision"] = next_revision
        planning_status = validate_and_project(backlog, activity)[task_id]
        response_body = {
            "data": self._project_task(task, planning_status=planning_status),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: backlog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-note-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_transactional
    def add_subtask_v1(
        self,
        task_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        """Add one subtask for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay
        if body["priority"] not in PRIORITIES:
            raise ValueError("invalid priority")

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog["tasks"], task_id, "task")
        next_revision = _guard_revision(task, body["revision"])
        subtask = {
            "id": _next_id(task.setdefault("subtasks", []), "S"),
            "title": _required_text(body["title"], "title"),
            "priority": body["priority"],
            "status": "open",
        }
        task["subtasks"].append(subtask)
        task["updated_at"] = today()
        task["revision"] = next_revision
        planning_status = validate_and_project(backlog, activity)[task_id]
        response_body = {
            "data": self._project_task(task, planning_status=planning_status),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: backlog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-subtask-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_transactional
    def add_subtask(
        self,
        task_id: str,
        title: str,
        priority: str = "P2",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if priority not in PRIORITIES:
            raise ValueError("invalid priority")
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        subtask = {
            "id": _next_id(task.setdefault("subtasks", []), "S"),
            "title": _required_text(title, "title"),
            "priority": priority,
            "status": "open",
        }
        task["subtasks"].append(subtask)
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return subtask

    @_transactional
    def set_subtask_status(
        self,
        task_id: str,
        subtask_id: str,
        status: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        subtask = _find(task.setdefault("subtasks", []), subtask_id, "subtask")
        subtask["status"] = status
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return subtask

    def list_tasks(self, status: str = "active") -> list[dict[str, Any]]:
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        projection = validate_and_project(backlog, self.documents.load(WorkspaceDocument.ACTIVITY))
        tasks = []
        for source in backlog.get("tasks", []):
            task = copy.deepcopy(source)
            task["status"] = projection[task["id"]]
            tasks.append(task)
        if status == "active":
            tasks = [task for task in tasks if task.get("status") in ("open", "started")]
        elif status != "all":
            if status not in TASK_STATUSES:
                raise ValueError("invalid task status")
            tasks = [task for task in tasks if task.get("status") == status]
        return sorted(
            tasks,
            key=lambda task: (
                TASK_STATUSES.index(task.get("status", "open")),
                PRIORITIES.index(task.get("priority", "P2")),
                task.get("due") or "9999-12-31",
                task.get("id", ""),
            ),
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = copy.deepcopy(_find(backlog.get("tasks", []), task_id, "task"))
        projection = validate_and_project(backlog, self.documents.load(WorkspaceDocument.ACTIVITY))
        task["status"] = projection[task["id"]]
        return task

    @_optional_command_backend(
        "planning_commands", "set_task_status", "task"
    )
    @_transactional
    def set_task_status(
        self,
        task_id: str,
        status: str,
        expected_revision: int | None = None,
        *,
        provenance: str = "cli",
    ) -> dict[str, Any]:
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        if provenance not in {"cli", "api.legacy"}:
            raise ValueError("invalid planning status provenance")
        data = self.documents.load(WorkspaceDocument.TASKS)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        task = _find(data["tasks"], task_id, "task")
        current_revision = _revision(task)
        if expected_revision is None:
            expected_revision = current_revision
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError("revision is required and must be a non-negative integer")
        if expected_revision != current_revision:
            raise RevisionConflictError(
                "task revision is stale",
                {"expected": current_revision, "received": expected_revision},
            )
        current_status = validate_and_project(data, activity)[task["id"]]
        if status == current_status:
            return self._project_task(task, planning_status=current_status)
        next_revision = _next_revision(task)
        append_transition(
            activity,
            task,
            prior_status=current_status,
            status=status,
            prior_revision=current_revision,
            new_revision=next_revision,
            created_at=utc_now(),
            actor="local.user",
            provenance=provenance,
        )
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.documents.save_many(
            {WorkspaceDocument.TASKS: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-status-{}-r{}".format(task["id"], next_revision),
        )
        return self._project_task(task, planning_status=status)

    @_transactional
    def set_task_status_v1(
        self,
        task_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
        request_digest: str | None = None,
    ) -> dict[str, Any]:
        """The keyed Task-status intent selected by a PRESENT Idempotency-Key.

        Ordering is the frozen one: the key and the body are completely
        validated, then the receipt is looked up, and only then is anything
        mutable consulted. So an exact replay returns its original Task even
        after unrelated revisions have moved on, and a key is never treated as
        authorization.

        A same-status action is not a no-op that skips checks: it still
        requires a successful revision check, and then persists ONLY its
        receipt, leaving the Task, its revision, its updated_at and the
        planning history untouched.
        """

        self._validate_keyed_intent_request(idempotency_key, body)
        if not _released_v3_attributed_composition_owner(self):
            # The keyed branch, no-op included, is released-v3 SAME-Store only,
            # bound by object identity. Refused before any read or write; the
            # ordinary unkeyed PATCH is untouched by this.
            raise DomainError(
                "keyed task status intents are not supported by this storage composition"
            )
        digest = self._raw_request_digest(body, request_digest)

        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "PATCH", path, digest)
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        current_revision = _revision(task)
        if body["revision"] != current_revision:
            raise RevisionConflictError(
                "task revision is stale",
                {"expected": current_revision, "received": body["revision"]},
            )
        status = body["status"]
        current_status = validate_and_project(data, activity)[task["id"]]

        if status == current_status:
            # A receipt, and nothing else. The Task is not rewritten, so its
            # revision and updated_at stay exactly where the caller saw them.
            projected = self._project_task(task, planning_status=current_status)
            response_body = {"data": projected, "meta": {"replayed": False}}
            self._record_idempotency(
                activity, idempotency_key, "PATCH", path, digest, 200, response_body
            )
            self.documents.save_many(
                {WorkspaceDocument.ACTIVITY: activity},
                operation_id="task-status-intent-{}".format(idempotency_key),
            )
            return {"status": 200, "body": response_body}

        next_revision = _next_revision(task)
        append_transition(
            activity,
            task,
            prior_status=current_status,
            status=status,
            prior_revision=current_revision,
            new_revision=next_revision,
            created_at=utc_now(),
            actor="local.user",
            provenance="cli",
        )
        task["updated_at"] = today()
        task["revision"] = next_revision
        projected = self._project_task(task, planning_status=status)
        response_body = {"data": projected, "meta": {"replayed": False}}
        self._record_idempotency(
            activity, idempotency_key, "PATCH", path, digest, 200, response_body
        )
        # ONE save commits the Task, its planning fact and the receipt together.
        # An ordinary PATCH followed by a separate receipt save would leave a
        # window where the change exists without its receipt.
        self.documents.save_many(
            {WorkspaceDocument.TASKS: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-status-intent-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    def _validate_keyed_intent_request(
        self, idempotency_key: Any, body: Any
    ) -> None:
        """Everything about the key and the body, before any lookup or read.

        The exact built-in str requirement matches the other keyed entrypoint:
        JSON and HTTP cannot carry a subclass, so nothing on the wire changes.
        """

        if type(idempotency_key) is not str:
            raise DomainError(
                "Idempotency-Key must be a string", {"field": "idempotency_key"}
            )
        self._validate_idempotency_key(idempotency_key)
        if type(body) is not dict or set(body) != {"status", "revision"}:
            raise DomainError(
                "a keyed task status intent requires exactly status and revision"
            )
        if type(body["status"]) is not str or body["status"] not in TASK_STATUSES:
            raise DomainError("invalid task status", {"field": "status"})
        revision = body["revision"]
        if type(revision) is not int or revision < 0:
            raise DomainError(
                "revision is required and must be a non-negative integer",
                {"field": "revision"},
            )

    @_transactional
    def add_task_note(
        self,
        task_id: str,
        text: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        note = {"date": today(), "text": _required_text(text, "text")}
        task.setdefault("notes", []).append(note)
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return note

    @_transactional
    def add_objective(self, text: str, quarter: str | None = None) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = {
            "id": _next_id(data["objectives"], "O"),
            "quarter": quarter or current_quarter(),
            "objective": _required_text(text, "objective"),
            "status": "active",
            "key_results": [],
            "created": today(),
            "updated_at": today(),
        }
        data["objectives"].append(objective)
        self.documents.save(WorkspaceDocument.OBJECTIVES, data)
        return objective

    @_optional_command_backend(
        "objective_commands", "create_objective", "intent"
    )
    @_transactional
    def create_objective_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/objectives",
    ) -> dict[str, Any]:
        """Create one Objective for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = {
            "id": _next_id(data["objectives"], "O"),
            "quarter": body["quarter"] or current_quarter(),
            "objective": _required_text(body["objective"], "objective"),
            "status": "active",
            "key_results": [],
            "created": today(),
            "updated_at": today(),
            "revision": 0,
        }
        data["objectives"].append(objective)
        self._event(
            activity,
            "objective.created",
            details={"objective_id": objective["id"], "revision": 0},
        )
        response_body = {"data": copy.deepcopy(objective), "meta": {"replayed": False}}
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="objective-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def add_key_result(self, objective_id: str, text: str, target: str = "") -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        current_revision = objective.get("revision", 0)
        next_revision = self._objective_next_revision(objective, current_revision)
        key_result = {
            "id": _next_id(objective.setdefault("key_results", []), "KR"),
            "text": _required_text(text, "text"),
            "target": str(target or "").strip(),
            "progress": 0,
            "status": "active",
        }
        objective["key_results"].append(key_result)
        objective["updated_at"] = today()
        objective["revision"] = next_revision
        self._event(
            activity,
            "key_result.created",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "revision": next_revision,
            },
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-create-{}-r{}".format(key_result["id"], next_revision),
        )
        return key_result

    def list_objectives(self, status: str = "active") -> list[dict[str, Any]]:
        objectives = list(self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", []))
        if status != "all":
            if status not in OBJECTIVE_STATUSES:
                raise ValueError("invalid objective status")
            objectives = [item for item in objectives if item.get("status") == status]
        return sorted(
            [self._project_objective(item) for item in objectives],
            key=lambda item: (item.get("quarter", ""), item.get("id", "")),
        )

    @staticmethod
    def _project_objective(objective: dict[str, Any]) -> dict[str, Any]:
        projected = copy.deepcopy(objective)
        revision = projected.get("revision", 0)
        if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
            raise StoreCorruptError("persisted objective revision is invalid")
        projected["revision"] = revision
        projected.setdefault("key_results", [])
        return projected

    @staticmethod
    def _objective_next_revision(objective: dict[str, Any], expected_revision: int) -> int:
        current = objective.get("revision", 0)
        if type(current) is not int or not 0 <= current <= MAX_REVISION:
            raise StoreCorruptError("persisted objective revision is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError("revision is required and must be a non-negative integer")
        if expected_revision != current:
            raise RevisionConflictError(
                "objective revision is stale",
                {"expected": current, "received": expected_revision},
            )
        if current == MAX_REVISION:
            raise RevisionExhaustedError(
                "objective revision cannot advance beyond the safe integer limit",
                {"maximum": MAX_REVISION},
            )
        return current + 1

    def objective_detail(self, objective_id: str) -> dict[str, Any]:
        with self.store.transaction():
            objective = _find(
                self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", []), objective_id, "objective"
            )
            normalized_id = objective["id"]
            tasks = [
                self._project_task(task)
                for task in self.list_tasks(status="all")
                if normalized_id in task.get("objective_ids", [])
            ]
            activity = [
                copy.deepcopy(event)
                for event in self.documents.load(WorkspaceDocument.ACTIVITY).get("activity", [])
                if event.get("details", {}).get("objective_id") == normalized_id
            ]
            return {
                "objective": self._project_objective(objective),
                "tasks": tasks,
                "activity": activity,
            }

    @_optional_command_backend(
        "objective_commands", "add_key_result", "intent"
    )
    @_transactional
    def add_key_result_v1(
        self,
        objective_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = _find(data["objectives"], objective_id, "objective")
        next_revision = self._objective_next_revision(objective, body["revision"])
        key_result = {
            "id": _next_id(objective.setdefault("key_results", []), "KR"),
            "text": _required_text(body["text"], "text"),
            "target": body["target"].strip(),
            "progress": 0,
            "status": "active",
        }
        objective["key_results"].append(key_result)
        objective["revision"] = next_revision
        objective["updated_at"] = today()
        self._event(
            activity,
            "key_result.created",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "revision": next_revision,
            },
        )
        response_body = {
            "data": self._project_objective(objective),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def patch_key_result_v1(
        self,
        objective_id: str,
        key_result_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        next_revision = self._objective_next_revision(objective, body["revision"])
        key_result = _find(objective.get("key_results", []), key_result_id, "key result")
        fields = sorted(set(body) - {"revision"})
        if "text" in body:
            key_result["text"] = _required_text(body["text"], "text")
        if "target" in body:
            if not isinstance(body["target"], str):
                raise DomainError("key result target must be a string")
            key_result["target"] = body["target"].strip()
        if "progress" in body:
            if type(body["progress"]) is not int or not 0 <= body["progress"] <= 100:
                raise DomainError("key result progress is invalid")
            key_result["progress"] = body["progress"]
        if "status" in body:
            if body["status"] not in OBJECTIVE_STATUSES:
                raise DomainError("key result status is invalid")
            key_result["status"] = body["status"]
        objective["revision"] = next_revision
        objective["updated_at"] = today()
        self._event(
            activity,
            "key_result.updated",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "fields": fields,
                "revision": next_revision,
            },
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-update-{}-r{}".format(key_result["id"], next_revision),
        )
        return self._project_objective(objective)

    @_transactional
    def patch_objective_v1(self, objective_id: str, body: dict[str, Any]) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        next_revision = self._objective_next_revision(objective, body["revision"])
        fields = sorted(set(body) - {"revision"})
        prior_status = objective.get("status", "active")
        if "objective" in body:
            objective["objective"] = _required_text(body["objective"], "objective")
        if "quarter" in body:
            objective["quarter"] = _required_text(body["quarter"], "quarter")
        if "status" in body:
            if body["status"] not in OBJECTIVE_STATUSES:
                raise DomainError("invalid objective status")
            objective["status"] = body["status"]
        objective["revision"] = next_revision
        objective["updated_at"] = today()
        details: dict[str, Any] = {
            "objective_id": objective["id"],
            "fields": fields,
            "revision": next_revision,
        }
        event_type = "objective.updated"
        if fields == ["status"]:
            event_type = "objective.status_changed"
            details.update({"prior_status": prior_status, "status": body["status"]})
        self._event(activity, event_type, details=details)
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="objective-update-{}-r{}".format(objective["id"], next_revision),
        )
        return self._project_objective(objective)

    @_transactional
    def link_task(self, objective_id: str, task_id: str) -> dict[str, Any]:
        _find(self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", []), objective_id, "objective")
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _next_revision(task)
        links = set(task.setdefault("objective_ids", []))
        links.add(objective_id.strip().upper())
        task["objective_ids"] = sorted(links)
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return task

    @_transactional
    def set_key_result_progress(
        self,
        objective_id: str,
        key_result_id: str,
        progress: int,
    ) -> dict[str, Any]:
        progress = max(0, min(100, int(progress)))
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        current_revision = objective.get("revision", 0)
        next_revision = self._objective_next_revision(objective, current_revision)
        key_result = _find(objective.get("key_results", []), key_result_id, "key result")
        prior = {
            "progress": key_result.get("progress", 0),
            "status": key_result.get("status", "active"),
        }
        key_result["progress"] = progress
        key_result["status"] = "done" if progress == 100 else "active"
        objective["updated_at"] = today()
        objective["revision"] = next_revision
        self._event(
            activity,
            "key_result.updated",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "prior": prior,
                "current": {"progress": progress, "status": key_result["status"]},
                "revision": next_revision,
            },
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-update-{}-r{}".format(key_result["id"], next_revision),
        )
        return key_result

    @_transactional
    def objective_rollup(self) -> list[dict[str, Any]]:
        tasks = self.list_tasks(status="all")
        output = []
        for objective in self.list_objectives(status="all"):
            linked = [task for task in tasks if objective["id"] in task.get("objective_ids", [])]
            output.append({
                "id": objective["id"],
                "objective": objective["objective"],
                "quarter": objective.get("quarter"),
                "status": objective.get("status"),
                "key_results": objective.get("key_results", []),
                "tasks": [
                    {
                        "id": task["id"],
                        "title": task["title"],
                        "status": task.get("status"),
                        "subtasks_done": sum(
                            1 for item in task.get("subtasks", []) if item.get("status") == "done"
                        ),
                        "subtasks_total": len(task.get("subtasks", [])),
                    }
                    for task in linked
                ],
            })
        return output

    def add_task_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Frame an ordinary create without changing its legacy domain rules."""
        fields = {"title", "detail", "priority", "due", "tags", "objective_ids", "parent_id", "dependencies"}
        if type(body) is not dict or set(body) != fields:
            raise DomainError("CLI backlog add requires exactly its eight creation fields")
        if any(type(body[field]) is not str for field in ("title", "detail", "priority")):
            raise DomainError("CLI backlog title, detail and priority must be strings")
        if any(body[field] is not None and type(body[field]) is not str for field in ("due", "parent_id")):
            raise DomainError("CLI backlog due and parent_id must be strings or null")
        for field in ("tags", "objective_ids", "dependencies"):
            if type(body[field]) is not list or any(type(item) is not str for item in body[field]):
                raise DomainError("CLI backlog collections must be arrays of strings")
        self._require_cli_owner_composition("backlog add")
        return self.add_task(
            title=body["title"], detail=body["detail"], priority=body["priority"], due=body["due"],
            tags=body["tags"], objective_ids=body["objective_ids"], parent_id=body["parent_id"],
            dependencies=body["dependencies"],
        )

    def link_task_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Preserve the legacy link lookup, revision and retained-reference rules."""
        if type(body) is not dict or set(body) != {"objective_id", "task_id"}:
            raise DomainError("CLI OKR link requires only objective_id and task_id")
        if any(type(body[field]) is not str for field in ("objective_id", "task_id")):
            raise DomainError("CLI OKR link identifiers must be strings")
        self._require_cli_owner_composition("OKR link")
        return self.link_task(objective_id=body["objective_id"], task_id=body["task_id"])

    def set_key_result_progress_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Frame legacy progress; its setter owns clamping, revisions and audit."""
        if type(body) is not dict or set(body) != {"objective_id", "key_result_id", "progress"}:
            raise DomainError("CLI OKR progress requires only objective_id, key_result_id and progress")
        if any(type(body[field]) is not str for field in ("objective_id", "key_result_id")):
            raise DomainError("CLI OKR progress identifiers must be strings")
        if type(body["progress"]) is not int:
            raise DomainError("CLI OKR progress must be an integer")
        self._require_cli_owner_composition("OKR progress")
        return self.set_key_result_progress(
            objective_id=body["objective_id"], key_result_id=body["key_result_id"], progress=body["progress"],
        )

    def checkin_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run the existing CLI operation only on its supported owner Store."""
        if type(body) is not dict or set(body) != {"date", "time"}:
            raise DomainError("CLI checkin requires only date and time")
        if any(type(body[field]) is not str for field in ("date", "time")):
            raise DomainError("CLI checkin date and time must be strings")
        self._require_cli_owner_composition("checkin")
        return self.checkin(time=body["time"], date=body["date"])

    def _require_cli_owner_composition(self, operation: str) -> None:
        """Require released same-Store documents and no alternate delegates."""
        delegates = (
            self.capture_reply_commands, self.intent_commands,
            self.objective_commands, self.task_commands,
            self.relationship_commands, self.planning_commands,
            self.work_session_commands, self.query_commands,
        )
        if not _attributed_released_composition(self) or any(
            delegate is not None for delegate in delegates
        ):
            raise DomainError("CLI {} is not supported by this storage composition".format(operation))

    def add_worklog_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Frame an ordinary entry, leaving its domain rules to add_worklog."""
        fields = {"task_id", "date", "done", "next_items", "blockers"}
        if type(body) is not dict or set(body) != fields:
            raise DomainError("CLI worklog add requires only task_id, date and categories")
        if any(type(body[field]) is not str for field in ("task_id", "date")):
            raise DomainError("CLI worklog task_id and date must be strings")
        for field in ("done", "next_items", "blockers"):
            if type(body[field]) is not list or any(type(item) is not str for item in body[field]):
                raise DomainError("CLI worklog categories must be arrays of strings")
        self._require_cli_owner_composition("worklog add")
        return self.add_worklog(
            task_id=body["task_id"], date=body["date"], done=body["done"],
            next_items=body["next_items"], blockers=body["blockers"],
        )

    @_transactional
    def checkin(self, time: str | None = None, date: str | None = None) -> dict[str, Any]:
        date = date or today()
        dt.date.fromisoformat(date)
        if time is None:
            time = dt.datetime.now().strftime("%H:%M")
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time):
            raise ValueError("time must use HH:MM")
        data = self.documents.load(WorkspaceDocument.WORKLOG)
        day = data.setdefault("days", {}).setdefault(date, {"entries": []})
        day["start_time"] = time
        self.documents.save(WorkspaceDocument.WORKLOG, data)
        return {"date": date, "start_time": time}

    @_transactional
    def add_worklog(
        self,
        task_id: str,
        done: Iterable[str] = (),
        next_items: Iterable[str] = (),
        blockers: Iterable[str] = (),
        date: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        date = date or today()
        dt.date.fromisoformat(date)
        data = self.documents.load(WorkspaceDocument.WORKLOG)
        day = data.setdefault("days", {}).setdefault(date, {"entries": []})
        entry = {
            "task_id": task["id"],
            "task": task["title"],
            "done": [str(item).strip() for item in done if str(item).strip()],
            "next": [str(item).strip() for item in next_items if str(item).strip()],
            "blockers": [str(item).strip() for item in blockers if str(item).strip()],
        }
        if not any(entry[key] for key in ("done", "next", "blockers")):
            raise ValueError("at least one worklog item is required")
        day["entries"].append(entry)
        self.documents.save(WorkspaceDocument.WORKLOG, data)
        return {"date": date, **entry}

    @staticmethod
    def _review_date(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise DomainError("date must use YYYY-MM-DD", {"field": "date"})
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError as error:
            raise DomainError("date is invalid", {"field": "date"}) from error
        if parsed.isoformat() != value:
            raise DomainError("date must use YYYY-MM-DD", {"field": "date"})
        return value

    @staticmethod
    def _review_items(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 20:
            raise DomainError(
                "{} must be an array with at most 20 items".format(field),
                {"field": field},
            )
        output: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise DomainError("{} items must be strings".format(field), {"field": field})
            normalized = item.strip()
            if len(normalized) > 1000:
                raise DomainError(
                    "{} items must be at most 1000 characters".format(field),
                    {"field": field},
                )
            if normalized:
                output.append(normalized)
        return output

    @_optional_command_backend("intent_commands", "checkin", "intent")
    @_transactional
    def checkin_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/checkin",
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        date = self._review_date(body.get("date"))
        time = body.get("time")
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            raise DomainError("time must use HH:MM", {"field": "time"})
        canonical = {"date": date, "time": time}
        request_digest = self._request_digest(canonical)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        day = worklog.setdefault("days", {}).setdefault(date, {"entries": []})
        day["start_time"] = time
        response_body = {
            "data": {"date": date, "start_time": time},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="review-checkin-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_attributed_released_v3_only("intent_commands", "add_worklog", "intent")
    @_transactional
    def add_worklog_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/entries",
        origin: str | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise DomainError("task_id is required", {"field": "task_id"})
        task = self.get_task(task_id.strip().upper())
        canonical = {
            "date": self._review_date(body.get("date")),
            "task_id": task["id"],
            "done": self._review_items(body.get("done"), "done"),
            "next": self._review_items(body.get("next"), "next"),
            "blockers": self._review_items(body.get("blockers"), "blockers"),
        }
        if not any(canonical[field] for field in ("done", "next", "blockers")):
            raise DomainError("at least one worklog item is required")
        request_digest = self._request_digest(canonical)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        days = worklog.setdefault("days", {})
        day = days.setdefault(canonical["date"], {"entries": []})
        entries = day.setdefault("entries", [])
        entry = {
            "task_id": task["id"],
            "task": task["title"],
            "done": canonical["done"],
            "next": canonical["next"],
            "blockers": canonical["blockers"],
        }
        # The date-local PHYSICAL ordinal, captured before the append, and EVERY
        # previously accepted physical entry, flattened across ALL dates.
        #
        # There is deliberately no date filter. A stored entry dated LATER than
        # this one was still accepted earlier, so a backdated append is not the
        # first for its Task. Filtering by date made exactly that case report
        # first_for_task true. Browser and legacy entries count too: "first for
        # this Task" is a fact about the stored Worklog, not about attributed
        # writes, and nothing here rewrites or reorders the Worklog.
        ordinal = len(entries)
        prior_entries: list[dict[str, Any]] = []
        for stored_date in sorted(days):
            stored = days[stored_date].get("entries")
            if not isinstance(stored, list):
                continue
            prior_entries.extend(stored[:ordinal] if stored_date == canonical["date"] else stored)
        facts = self._checkpoint_facts(
            idempotency_key=idempotency_key,
            date=canonical["date"],
            entry=entry,
            ordinal=ordinal,
            prior_entries=prior_entries,
            origin=origin,
        )
        # TP-F3 preflight, under the SAME outer transaction lock and before any
        # fresh document mutation: the committed notice must already be
        # buildable, and the shared event sequence must still have room for both
        # the manifest event this commit will emit and the typed event published
        # after it. Publication itself still happens after a successful save.
        # This validates capacity; it allocates nothing, adds no counter and
        # makes no crash-atomicity claim.
        if facts["recorded"]["origin"] is not None:
            self._preflight_committed_notice(facts)
        entries.append(entry)
        response_body = {
            "data": {"date": canonical["date"], **copy.deepcopy(entry)},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        # The recorded fact rides the EXISTING Worklog+Activity save_many beside
        # the existing idempotency receipt. It is carried as an ordinary
        # Activity record rather than a new document key: the Activity document
        # schema is validated by exact key set, so inventing a top-level list would be
        # a storage schema change and would fail every existing store. No
        # Worklog entry field, public response, Task status or revision changes,
        # and no history is rewritten.
        entries_feed = activity.setdefault("activity", [])
        entries_feed.append({
            "id": _next_id(entries_feed, "E", 6),
            "type": "worklog.recorded",
            "created_at": utc_now(),
            "task_id": facts["recorded"]["task_id"],
            "details": copy.deepcopy(facts["recorded"]),
        })
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="review-entry-{}".format(idempotency_key),
        )
        # Published after the save succeeded and while the SAME outer
        # transaction lock is still held, before any HTTP serialization. Only an
        # attributed write publishes; an ordinary or browser write records its
        # fact and stays silent.
        if facts["recorded"]["origin"] is not None:
            self.store.publish_change_notice(
                lambda event_id: build_committed_notice(facts=facts, event_id=event_id)
            )
        return {"status": 201, "body": response_body}

    def _raw_request_digest(
        self, body: dict[str, Any], supplied: str | None
    ) -> str:
        """The digest of the ORIGINAL parsed body, verifying any supplied one.

        Serialization can still fail on a value the domain accepted, so the
        failure is mapped to the same constant refusal rather than escaping as
        a Python encoding or recursion error carrying input detail.
        """

        try:
            computed = canonical_digest(body)
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
            raise DomainError("the checkpoint transition request is invalid") from error
        if supplied is not None and supplied != computed:
            raise DomainError("the request digest does not match the request body")
        return computed

    def _preflight_committed_notice(self, facts: dict[str, Any]) -> None:
        """Prove the notice is buildable at the id publication would use.

        The projected id accounts for the manifest event the committing save
        emits before publication, so the safe-integer ceiling is refused here
        rather than after Worklog, Activity, the recorded fact and the receipt
        have already been committed. The pure builder and its frozen twelve
        fields are reused unchanged; the result is discarded.
        """

        try:
            build_committed_notice(
                facts=facts, event_id=self.store.projected_change_event_id()
            )
        except CheckpointChangeError as error:
            raise DomainError("the review entry could not be recorded") from error

    def _checkpoint_facts(
        self,
        *,
        idempotency_key: str,
        date: str,
        entry: dict[str, Any],
        ordinal: int,
        prior_entries: list[dict[str, Any]],
        origin: str | None,
    ) -> dict[str, Any]:
        """Build the immutable commit facts BEFORE any document is mutated.

        The admitted pure builder decides identity, locator, counts and
        physical-first semantics; nothing is recomputed here. A refusal is
        content-free and carries no input value.
        """

        readiness = self.store.readiness
        if readiness is None:
            raise DomainError("the workspace is not ready to record a checkpoint")
        try:
            return build_checkpoint_facts(
                workspace_uid=readiness.workspace_uid,
                idempotency_key=idempotency_key,
                date=date,
                entry=entry,
                ordinal=ordinal,
                prior_entries=prior_entries,
                origin=origin,
            )
        except CheckpointChangeError as error:
            raise DomainError("the review entry could not be recorded") from error

    @staticmethod
    def _work_session_timestamp(value: Any, field: str) -> dt.datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise StoreCorruptError("persisted work session {} is invalid".format(field))
        try:
            parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise StoreCorruptError(
                "persisted work session {} is invalid".format(field)
            ) from error
        if parsed.tzinfo != dt.timezone.utc or parsed.microsecond:
            raise StoreCorruptError("persisted work session {} is invalid".format(field))
        return parsed

    @classmethod
    def _work_session_elapsed(
        cls, session: dict[str, Any], *, current_time: str | None = None
    ) -> int:
        now = cls._work_session_timestamp(current_time, "current_time") if current_time else None
        elapsed = 0
        for segment in session["segments"]:
            started = cls._work_session_timestamp(segment["started_at"], "segment.started_at")
            ended_value = segment.get("ended_at")
            ended = (
                cls._work_session_timestamp(ended_value, "segment.ended_at")
                if ended_value is not None
                else now
            )
            if ended is None:
                raise StoreCorruptError("persisted running work session has no current time")
            if ended < started:
                raise StoreCorruptError("persisted work session segment has negative duration")
            elapsed += int((ended - started).total_seconds())
        return elapsed

    @classmethod
    def _work_session_day(
        cls, date: Any, day: Any
    ) -> tuple[str, list[Any]]:
        try:
            valid_date = cls._review_date(date)
        except DomainError as error:
            raise StoreCorruptError("persisted worklog date is invalid") from error
        if not isinstance(day, dict):
            raise StoreCorruptError("persisted worklog day is invalid")
        sessions = day.get("sessions", [])
        if not isinstance(sessions, list):
            raise StoreCorruptError("persisted work sessions are invalid")
        return valid_date, sessions

    @classmethod
    def _work_session_header(
        cls, candidate: Any, valid_date: str, seen: set[str]
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(candidate, dict):
            raise StoreCorruptError("persisted work session is invalid")
        session_id = candidate.get("id")
        if (
            not isinstance(session_id, str)
            or not re.fullmatch(r"WS-\d{6,}", session_id)
            or session_id in seen
        ):
            raise StoreCorruptError("persisted work session id is invalid")
        seen.add(session_id)
        if candidate.get("date") != valid_date:
            raise StoreCorruptError("persisted work session date is invalid")
        task_id = candidate.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"T-\d+", task_id):
            raise StoreCorruptError("persisted work session task id is invalid")
        task_title = candidate.get("task")
        if not isinstance(task_title, str) or not task_title.strip():
            raise StoreCorruptError("persisted work session task title is invalid")
        state = candidate.get("state")
        if state not in {"running", "paused", "stopped"}:
            raise StoreCorruptError("persisted work session state is invalid")
        expected_worklog_states = (
            {"not_ready"} if state in {"running", "paused"} else {"pending", "recorded"}
        )
        if candidate.get("worklog_state") not in expected_worklog_states:
            raise StoreCorruptError("persisted work session worklog state is invalid")
        cls._work_session_timestamp(candidate.get("started_at"), "started_at")
        cls._work_session_timestamp(candidate.get("updated_at"), "updated_at")
        return candidate, state

    @classmethod
    def _work_session_segment(
        cls,
        candidate: Any,
        *,
        index: int,
        count: int,
        previous_end: dt.datetime | None,
    ) -> tuple[dt.datetime | None, bool]:
        if not isinstance(candidate, dict) or set(candidate) != {"started_at", "ended_at"}:
            raise StoreCorruptError("persisted work session segment is invalid")
        segment_start = cls._work_session_timestamp(
            candidate["started_at"], "segment.started_at"
        )
        if previous_end is not None and segment_start < previous_end:
            raise StoreCorruptError("persisted work session segments overlap")
        if candidate["ended_at"] is None:
            if index != count - 1:
                raise StoreCorruptError("persisted work session open segment is invalid")
            return None, True
        segment_end = cls._work_session_timestamp(
            candidate["ended_at"], "segment.ended_at"
        )
        if segment_end < segment_start:
            raise StoreCorruptError("persisted work session segment has negative duration")
        return segment_end, False

    @classmethod
    def _validate_work_session_segments(
        cls, session: dict[str, Any], state: str
    ) -> None:
        segments = session.get("segments")
        if not isinstance(segments, list) or not segments:
            raise StoreCorruptError("persisted work session segments are invalid")
        previous_end: dt.datetime | None = None
        open_segments = 0
        for index, segment in enumerate(segments):
            previous_end, is_open = cls._work_session_segment(
                segment,
                index=index,
                count=len(segments),
                previous_end=previous_end,
            )
            open_segments += int(is_open)
        if (state == "running") != (open_segments == 1):
            raise StoreCorruptError("persisted work session open segment is inconsistent")
        if state in {"paused", "stopped"} and open_segments:
            raise StoreCorruptError("persisted work session open segment is inconsistent")

    @classmethod
    def _work_session_records(cls, worklog: dict[str, Any]) -> list[dict[str, Any]]:
        days = worklog.get("days")
        if not isinstance(days, dict):
            raise StoreCorruptError("persisted worklog days are invalid")
        sessions: list[dict[str, Any]] = []
        seen: set[str] = set()
        active_count = 0
        for date, day in days.items():
            valid_date, candidates = cls._work_session_day(date, day)
            for candidate in candidates:
                session, state = cls._work_session_header(candidate, valid_date, seen)
                cls._validate_work_session_segments(session, state)
                active_count += int(state in {"running", "paused"})
                sessions.append(session)
        if active_count > 1:
            raise StoreCorruptError("multiple active work sessions are persisted")
        return sessions

    @classmethod
    def _project_work_session(
        cls, session: dict[str, Any], *, current_time: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": session["id"],
            "task_id": session["task_id"],
            "task": session["task"],
            "date": session["date"],
            "state": session["state"],
            "started_at": session["started_at"],
            "updated_at": session["updated_at"],
            "elapsed_seconds": cls._work_session_elapsed(
                session, current_time=current_time if session["state"] == "running" else None
            ),
            "worklog_state": session["worklog_state"],
        }

    @_optional_command_backend(
        "work_session_commands", "projection", "intent"
    )
    @_transactional
    def work_sessions_projection(self) -> dict[str, Any]:
        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        current_time = utc_now()
        current = next(
            (session for session in sessions if session["state"] in {"running", "paused"}),
            None,
        )
        pending = sorted(
            (
                session
                for session in sessions
                if session["state"] == "stopped" and session["worklog_state"] == "pending"
            ),
            key=lambda session: (session["updated_at"], session["id"]),
            reverse=True,
        )
        return {
            "current": (
                self._project_work_session(current, current_time=current_time)
                if current is not None
                else None
            ),
            "pending": [self._project_work_session(session) for session in pending],
        }

    @_optional_command_backend(
        "work_session_commands", "start", "intent"
    )
    @_transactional
    def start_work_session_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/work-sessions",
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise DomainError("task_id is required", {"field": "task_id"})
        task = self.get_task(task_id.strip().upper())
        canonical = {"task_id": task["id"]}
        request_digest = self._request_digest(canonical)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        if any(session["state"] in {"running", "paused"} for session in sessions):
            raise WorkSessionConflictError("another work session is already active")
        timestamp = utc_now()
        date = self._review_date(today())
        session = {
            "id": _next_id(sessions, "WS", 6),
            "task_id": task["id"],
            "task": task["title"],
            "date": date,
            "state": "running",
            "started_at": timestamp,
            "updated_at": timestamp,
            "segments": [{"started_at": timestamp, "ended_at": None}],
            "worklog_state": "not_ready",
        }
        worklog.setdefault("days", {}).setdefault(date, {"entries": []}).setdefault(
            "sessions", []
        ).append(session)
        response_body = {
            "data": self._project_work_session(session, current_time=timestamp),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="work-session-start-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_optional_command_backend(
        "work_session_commands", "transition", "intent"
    )
    @_transactional
    def transition_work_session_v1(
        self,
        session_id: str,
        action: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        wanted_id = str(session_id).strip().upper()
        if action not in {"pause", "resume", "stop"}:
            raise DomainError("work session action is invalid", {"action": action})
        canonical: dict[str, Any] = {}
        request_digest = self._request_digest(canonical)
        path = path or "/api/v1/work-sessions/{}/{}".format(wanted_id, action)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        session = _find(sessions, wanted_id, "work session")
        expected_state = {"pause": "running", "resume": "paused"}.get(action)
        if action == "stop":
            allowed = {"running", "paused"}
        else:
            allowed = {expected_state}
        if session["state"] not in allowed:
            raise WorkSessionConflictError(
                "cannot {} a {} work session".format(action, session["state"]),
                {"session_id": session["id"], "state": session["state"]},
            )
        timestamp = utc_now()
        if action in {"pause", "stop"} and session["state"] == "running":
            session["segments"][-1]["ended_at"] = timestamp
        if action == "resume":
            session["segments"].append({"started_at": timestamp, "ended_at": None})
        session["state"] = {"pause": "paused", "resume": "running", "stop": "stopped"}[
            action
        ]
        if action == "stop":
            session["worklog_state"] = "pending"
        session["updated_at"] = timestamp
        response_body = {
            "data": self._project_work_session(session, current_time=timestamp),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="work-session-{}-{}".format(action, idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_optional_command_backend(
        "work_session_commands", "record_worklog", "intent"
    )
    @_transactional
    def record_work_session_v1(
        self,
        session_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        wanted_id = str(session_id).strip().upper()
        canonical = {
            "done": self._review_items(body.get("done"), "done"),
            "next": self._review_items(body.get("next"), "next"),
            "blockers": self._review_items(body.get("blockers"), "blockers"),
        }
        if not any(canonical[field] for field in ("done", "next", "blockers")):
            raise DomainError("at least one worklog item is required")
        request_digest = self._request_digest(canonical)
        path = path or "/api/v1/work-sessions/{}/worklog".format(wanted_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        session = _find(sessions, wanted_id, "work session")
        if session["state"] != "stopped" or session["worklog_state"] != "pending":
            raise WorkSessionConflictError(
                "work session is not ready for a worklog",
                {"session_id": session["id"], "state": session["state"]},
            )
        entry = {
            "task_id": session["task_id"],
            "task": session["task"],
            "done": canonical["done"],
            "next": canonical["next"],
            "blockers": canonical["blockers"],
            "session_id": session["id"],
            "duration_seconds": self._work_session_elapsed(session),
        }
        worklog["days"][session["date"]].setdefault("entries", []).append(entry)
        session["worklog_state"] = "recorded"
        session["updated_at"] = utc_now()
        response_body = {
            "data": {"date": session["date"], **copy.deepcopy(entry)},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="work-session-worklog-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    # ---- D5 shared projection and transitions ---------------------------
    #
    # Every ACTIVE reader below goes through one whole-history-validated view.
    # Physical writers deliberately do NOT: append, check-in and work-session
    # writers keep loading the raw document, so future ordinals and
    # first-for-task still count superseded rows.

    # The five closed pure history codes. "duplicate_revision" was never one
    # of them; "history_invalid" is, and an invalid known history must reach
    # the 409 envelope rather than being reported as a malformed request.
    _TRANSITION_CONFLICT_CODES = frozenset(
        {"stale_revision", "same_state", "exhausted", "locator_mismatch", "history_invalid"}
    )

    def _workspace_uid(self) -> str:
        readiness = self.store.readiness
        if readiness is None:
            raise DomainError("the workspace is not ready")
        return readiness.workspace_uid

    def _refuse_transition(self, error: CheckpointTransitionError) -> DomainError:
        """Map one closed pure code to its frozen public envelope."""

        code = getattr(error, "code", "malformed")
        if code in self._TRANSITION_CONFLICT_CODES:
            return CheckpointTransitionConflictError(code)
        return DomainError("the checkpoint transition request is invalid")

    def _active_worklog(self) -> dict[str, Any]:
        """The Worklog every active reader shares, superseded rows removed."""

        try:
            return active_worklog_document(
                workspace_uid=self._workspace_uid(),
                worklog=self.documents.load(WorkspaceDocument.WORKLOG),
                activity=self.documents.load(WorkspaceDocument.ACTIVITY),
            )
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error

    def active_worklog_view(self) -> dict[str, Any]:
        """The shared active Worklog view, for readers outside this class.

        The local Agent backend and the running-owner Agent context read through
        this so every active reader sees one whole-history-validated projection.
        The caller must already hold the transaction, exactly as the readers in
        this class do.
        """

        return self._active_worklog()

    @_transactional
    def list_checkpoint_audit(self) -> dict[str, Any]:
        """The exact frozen complete audit view over the whole history."""

        try:
            return build_audit(
                workspace_uid=self._workspace_uid(),
                worklog=self.documents.load(WorkspaceDocument.WORKLOG),
                activity=self.documents.load(WorkspaceDocument.ACTIVITY),
            )
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error

    @_transactional
    def apply_checkpoint_transition_v1(
        self,
        checkpoint_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
        request_digest: str | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """Supersede or restore one checkpoint, durably and idempotently.

        Ordering is the frozen one. Ordinary validation runs first, then the
        receipt lookup, and only then anything mutable: no locator, history,
        revision or capacity decision may precede a matching replay, so an old
        exact replay after later cycles returns its original event and saves and
        publishes nothing.

        The digest is supplied by the transport, which computes it from the RAW
        parsed body before any normalization: a direct caller cannot forge one
        that would match a differently-worded request.
        """

        # This NEW entrypoint requires an exact built-in str key. JSON and HTTP
        # cannot carry a str subclass, so nothing on the wire is affected;
        # legacy writers keep their own long-standing semantics untouched.
        if type(idempotency_key) is not str:
            raise DomainError("Idempotency-Key must be a string", {"field": "idempotency_key"})
        self._validate_idempotency_key(idempotency_key)
        if type(checkpoint_id) is not str or _CHECKPOINT_ID.fullmatch(checkpoint_id) is None:
            # Malformed identifier SYNTAX is a bad request. An absent but
            # canonical identifier is a different thing and stays a
            # locator_mismatch conflict below.
            raise DomainError("the checkpoint identifier is invalid")
        if not _released_v3_attributed_composition_owner(self):
            raise DomainError(
                "checkpoint transitions are not supported by this storage composition"
            )
        # Ordinary shape validation runs FIRST, so a body the domain rejects is
        # refused content-free instead of reaching the serializer, where a lone
        # surrogate or a cycle would escape as UnicodeEncodeError or ValueError
        # and leak position and value detail out of the public boundary.
        try:
            request = normalize_transition_request(body)
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error
        # Only then the digest, and always of the ORIGINAL raw parsed body
        # rather than the normalized replacement, so replay identity keeps
        # distinguishing bodies the domain would normalize together. A supplied
        # digest is verified, never trusted.
        request_digest = self._raw_request_digest(body, request_digest)

        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        workspace_uid = self._workspace_uid()
        try:
            locator, row = physical_locator_for(
                workspace_uid=workspace_uid,
                checkpoint_id=checkpoint_id,
                worklog=worklog,
                activity=activity,
            )
            verify_locator({
                "workspace_uid": workspace_uid,
                "checkpoint_id": checkpoint_id,
                "recorded": row["recorded"],
                "actual_locator": locator,
            })
            transition = next_transition({
                "current": {"state": row["state"], "revision": row["revision"]},
                "request": request,
            })
            event = build_transition_event({
                "workspace_uid": workspace_uid,
                "checkpoint_id": checkpoint_id,
                "locator": locator,
                "transition": transition,
                "origin": origin,
            })
            if origin is not None:
                # Full notice shape and event capacity are proven BEFORE any
                # fresh mutation, at the ceiling publication could reach.
                build_transition_notice(
                    event, self.store.projected_change_event_id()
                )
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error

        response_body = {"data": copy.deepcopy(event), "meta": {"replayed": False}}
        feed = activity.setdefault("activity", [])
        feed.append({
            "id": _next_id(feed, "E", 6),
            "type": event["type"],
            "created_at": utc_now(),
            "task_id": event["task_id"],
            "details": copy.deepcopy(event),
        })
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        # ONE Activity-only save commits the transition and its receipt
        # atomically. Worklog and Task bytes are never touched by a transition.
        self.documents.save_many(
            {WorkspaceDocument.ACTIVITY: activity},
            operation_id="checkpoint-transition-{}".format(idempotency_key),
        )
        if origin is not None:
            # The existing Store publisher is reused unchanged: store.py is not
            # an owned path in this packet, and it needs no change. The record
            # carries the notice, and the encoder tells the two variants apart
            # by their disjoint frozen field sets.
            self.store.publish_change_notice(
                lambda event_id: build_transition_notice(event, event_id)
            )
        return {"status": 201, "body": response_body}

    @_transactional
    def review_projection(self, date: str, days: int = 7) -> dict[str, Any]:
        date = self._review_date(date)
        if type(days) is not int or days < 1 or days > 31:
            raise DomainError("days must be between 1 and 31", {"field": "days"})
        worklog = self._active_worklog()
        day = worklog.get("days", {}).get(date, {})
        return {
            "day": {
                "date": date,
                "start_time": day.get("start_time"),
                "entries": copy.deepcopy(day.get("entries", [])),
            },
            "weekly": self.weekly_report(end=date, days=days),
        }

    @_transactional
    def list_worklog(self, date: str | None = None) -> dict[str, Any]:
        # Transactional because the active view assembles TWO documents: without
        # one outer owner the Worklog and the Activity could come from different
        # committed states. The already transactional readers are unchanged.
        data = self._active_worklog()
        if date:
            dt.date.fromisoformat(date)
            return {"date": date, "entries": data.get("days", {}).get(date, {}).get("entries", [])}
        return data

    @_transactional
    def add_note(self, text: str, links: Iterable[str] = ()) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.NOTES)
        note = {
            "id": _next_id(data["notes"], "N", 4),
            "text": _required_text(text, "text"),
            "links": sorted(set(str(link).strip().upper() for link in links if str(link).strip())),
            "created": today(),
        }
        data["notes"].append(note)
        self.documents.save(WorkspaceDocument.NOTES, data)
        return note

    @_optional_command_backend("intent_commands", "create_note", "intent")
    @_transactional
    def create_note_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/notes",
    ) -> dict[str, Any]:
        """Create one graph note for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.NOTES)
        note = {
            "id": _next_id(data["notes"], "N", 4),
            "text": _required_text(body["text"], "text"),
            "links": sorted(
                set(str(link).strip().upper() for link in body["links"] if str(link).strip())
            ),
            "created": today(),
        }
        data["notes"].append(note)
        response_body = {"data": copy.deepcopy(note), "meta": {"replayed": False}}
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.NOTES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="graph-note-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    def _project_task(
        self,
        task: dict[str, Any],
        context_count: int = 0,
        *,
        planning_status: str | None = None,
    ) -> dict[str, Any]:
        projected = copy.deepcopy(task)
        task_uid = task.get("uid")
        try:
            parsed_uid = uuid.UUID(task_uid) if isinstance(task_uid, str) else None
        except ValueError as error:
            raise StoreCorruptError("persisted task UUID is invalid") from error
        if parsed_uid is None or parsed_uid.int == 0 or str(parsed_uid) != task_uid:
            raise StoreCorruptError("persisted task UUID is invalid")
        projected["uid"] = task_uid
        projected["revision"] = _revision(task)
        if planning_status is None:
            planning_status = self.get_task(task["id"])["status"]
        projected["status"] = planning_status
        projected.pop("status_fact_id", None)
        projected.setdefault("scheduled", None)
        projected.setdefault("estimate_minutes", None)
        projected["context_count"] = context_count
        return projected

    @staticmethod
    def _project_capture(capture: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id", "schema_version", "source_key", "source", "normalized", "task_hints",
            "provenance", "status", "linked_task_ids", "converted_task_ids", "revision",
            "created_at", "updated_at",
        )
        return {field: copy.deepcopy(capture[field]) for field in fields}

    @staticmethod
    def _project_reply(reply: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id",
            "task_id",
            "capture_id",
            "capture_revision",
            "provider",
            "capability",
            "target",
            "body",
            "body_digest",
            "target_digest",
            "state",
            "approved_at",
            "receipt",
            "created_at",
            "updated_at",
        )
        return {field: copy.deepcopy(reply[field]) for field in fields}

    @staticmethod
    def _event(
        activity: dict[str, Any],
        event_type: str,
        *,
        capture_id: str | None = None,
        task_id: str | None = None,
        reply_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": _next_id(activity.setdefault("activity", []), "E", 6),
            "type": event_type,
            "created_at": utc_now(),
            "details": details or {},
        }
        if capture_id:
            event["capture_id"] = capture_id
        if task_id:
            event["task_id"] = task_id
        if reply_id:
            event["reply_id"] = reply_id
        activity["activity"].append(event)
        return event

    @staticmethod
    def _request_digest(body: dict[str, Any]) -> str:
        return canonical_digest(body)

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
            raise DomainError(
                "Idempotency-Key must match [A-Za-z0-9._:-]{8,128}",
                {"header": "Idempotency-Key"},
            )
        return value

    def _idempotency_replay(
        self,
        activity: dict[str, Any],
        key: str,
        method: str,
        path: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        self._validate_idempotency_key(key)
        for record in activity.setdefault("idempotency", []):
            if record.get("key") != key:
                continue
            if (
                record.get("method") != method
                or record.get("path") != path
                or record.get("request_digest") != request_digest
            ):
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used for a different request",
                    {"key": key},
                )
            response_ref = record.get("response_ref")
            if response_ref is not None:
                if (
                    not isinstance(response_ref, dict)
                    or response_ref.get("kind") != "reply"
                    or not isinstance(response_ref.get("id"), str)
                ):
                    raise DomainError("stored idempotency response reference is invalid")
                reply = _find(
                    self.documents.load(WorkspaceDocument.REPLIES).get("replies", []),
                    response_ref["id"],
                    "reply",
                )
                body = {"data": self._project_reply(reply)}
                stored_meta = record.get("response_meta")
                if isinstance(stored_meta, dict) and stored_meta:
                    body["meta"] = copy.deepcopy(stored_meta)
            else:
                body = copy.deepcopy(record["response_body"])
            body.setdefault("meta", {})["replayed"] = True
            return {"status": 200, "body": body}
        return None

    @staticmethod
    def _record_idempotency(
        activity: dict[str, Any],
        key: str,
        method: str,
        path: str,
        request_digest: str,
        response_status: int,
        response_body: dict[str, Any] | None,
        *,
        response_ref: dict[str, str] | None = None,
        response_meta: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "key": key,
            "method": method,
            "path": path,
            "request_digest": request_digest,
            "response_status": response_status,
            "created_at": utc_now(),
        }
        if response_ref is None:
            if response_body is None:
                raise ValueError("response_body is required without response_ref")
            record["response_body"] = copy.deepcopy(response_body)
        else:
            record["response_ref"] = copy.deepcopy(response_ref)
            if response_meta:
                record["response_meta"] = copy.deepcopy(response_meta)
        activity.setdefault("idempotency", []).append(record)

    def storage_status(self) -> dict[str, Any]:
        """Return content-free readiness metadata for the local planning store."""

        with self.store.consistent_read() as readiness:
            total_bytes = self.documents.total_bytes()
            return {
                "workspace_id": readiness.workspace_uid,
                "store_schema_version": readiness.schema_version,
                "product_version": __version__,
                "remote_protocol_version": REMOTE_PROTOCOL_VERSION,
                "file_count": len(WorkspaceDocument),
                "total_bytes": total_bytes,
                "backup_format": "workstack-backup-v1",
                "restore_requires_shutdown": True,
            }

    def create_backup_download(self) -> BackupDownload:
        """Create a read-only archive while this server owns the store lease."""

        return create_backup_download(self.store)

    @_query_graph_backend
    def workspace_projection(self) -> dict[str, Any]:
        with self.store.transaction():
            workspace = self.documents.load(WorkspaceDocument.WORKSPACE)
            captures = self.documents.load(WorkspaceDocument.CAPTURES).get("captures", [])
            notes = self.documents.load(WorkspaceDocument.NOTES).get("notes", [])
            objectives = self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])
            source_tasks = self.list_tasks(status="all")
            contexts = group_context_by_task(
                notes, (self._project_capture(capture) for capture in captures),
                (task["id"] for task in source_tasks),
                (objective["id"] for objective in objectives),
            )
            tasks = [
                self._project_task(task, len(contexts[task["id"]]))
                for task in source_tasks
            ]
            snapshot = self.snapshot()
            return {
                "schema_version": "1.0",
                "workspace": {"id": workspace["id"], "name": workspace.get("name", "Work Stack")},
                "tasks": tasks,
                "objectives": [
                    self._project_objective(objective)
                    for objective in objectives
                ],
                "notes": copy.deepcopy(notes),
                "edges": snapshot["edges"],
                "inbox_count": sum(1 for capture in captures if capture.get("status") == "inbox"),
            }

    @_query_search_backend
    def search_projection(self, query: str, limit: int = 30) -> dict[str, Any]:
        """Search allowlisted local projections without exposing source or reply payloads."""

        query, limit = self._validate_search_request(query, limit)
        needle = query.casefold()
        entries = self._cached_search_entries()
        candidates = [
            candidate
            for entry in entries
            if (candidate := self._search_candidate(entry, needle)) is not None
        ]
        candidates.sort(key=lambda candidate: candidate[:4])
        return {"query": query, "items": [candidate[4] for candidate in candidates[:limit]]}

    @staticmethod
    def _validate_search_request(query: str, limit: int) -> tuple[str, int]:
        if not isinstance(query, str):
            raise DomainError("search query must be a string")
        query = _reject_controls(query.strip(), "query", multiline=False)
        if not 2 <= len(query) <= 100:
            raise DomainError("search query must be between 2 and 100 characters")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DomainError("search limit must be between 1 and 50")
        return query, limit

    @staticmethod
    def _search_entry(
        kind: str,
        item_id: str,
        title: str,
        subtitle: str,
        searchable: Iterable[str],
        target_kind: str,
        target_id: str | None,
    ) -> dict[str, Any]:
        values = [str(value) for value in searchable if value is not None]
        return {
            "kind": kind,
            "id": item_id,
            "title": title,
            "subtitle": subtitle,
            "target_kind": target_kind,
            "target_id": target_id,
            "folded_title": title.casefold(),
            "folded_id": item_id.casefold(),
            "folded_values": tuple(value.casefold() for value in values),
        }

    def _task_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for task in self.list_tasks(status="all"):
            due = " · due {}".format(task["due"]) if task.get("due") else ""
            searchable = [task.get("detail", ""), *task.get("tags", []), *task.get("objective_ids", [])]
            searchable.extend(note.get("text", "") for note in task.get("notes", []))
            searchable.extend(subtask.get("title", "") for subtask in task.get("subtasks", []))
            entries.append(self._search_entry(
                "task",
                task["id"],
                task["title"],
                "{} · {}{}".format(task.get("status", "open"), task.get("priority", "P2"), due),
                searchable,
                "task",
                task["id"],
            ))
        return entries

    def _objective_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for objective in self.list_objectives(status="all"):
            key_results = objective.get("key_results", [])
            searchable = [result.get("text", "") for result in key_results]
            searchable.extend(result.get("target", "") for result in key_results)
            entries.append(self._search_entry(
                "objective",
                objective["id"],
                objective["objective"],
                "{} · {}".format(
                    objective.get("quarter", "No quarter"),
                    objective.get("status", "active"),
                ),
                searchable,
                "objective",
                objective["id"],
            ))
        return entries

    def _note_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for note in self.documents.load(WorkspaceDocument.NOTES).get("notes", []):
            text = str(note.get("text", ""))
            item_id = str(note.get("id", ""))
            entries.append(self._search_entry(
                "note",
                item_id,
                text[:100] or str(note.get("id", "Note")),
                "Graph note · {} links".format(len(note.get("links", []))),
                [text, *note.get("links", [])],
                "workspace",
                None,
            ))
        return entries

    def _capture_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for capture in self.documents.load(WorkspaceDocument.CAPTURES).get("captures", []):
            projected = self._project_capture(capture)
            source = projected.get("source", {})
            normalized = projected.get("normalized", {})
            searchable = [normalized.get("summary", ""), normalized.get("context", "")]
            searchable.extend(
                action.get("title", "") for action in normalized.get("action_items", [])
            )
            entries.append(self._search_entry(
                "capture",
                projected["id"],
                source.get("display_title", projected["id"]),
                "{} · {}".format(
                    source.get("provider", "manual"), projected.get("status", "inbox")
                ),
                searchable,
                "capture",
                projected["id"],
            ))
        return entries

    @staticmethod
    def _activity_target(event: dict[str, Any]) -> tuple[str, str | None]:
        if event.get("task_id"):
            return "task", event["task_id"]
        if event.get("capture_id"):
            return "capture", event["capture_id"]
        return "workspace", None

    def _activity_search_entries(self) -> list[dict[str, Any]]:
        entries = []
        for event in self.documents.load(WorkspaceDocument.ACTIVITY).get("activity", []):
            event_type = str(event.get("type", ""))
            details = event.get("details", {})
            details = details if isinstance(details, dict) else {}
            target_kind, target_id = self._activity_target(event)
            entries.append(self._search_entry(
                "activity",
                str(event.get("id", "")),
                event_type.replace(".", " ").strip().title() or "Activity",
                str(event.get("created_at", "")),
                [event_type, details.get("provider", ""), details.get("state", "")],
                target_kind,
                target_id if isinstance(target_id, str) else None,
            ))
        return entries

    def _build_search_entries(self) -> list[dict[str, Any]]:
        entries = self._task_search_entries()
        entries.extend(self._objective_search_entries())
        entries.extend(self._note_search_entries())
        entries.extend(self._capture_search_entries())
        entries.extend(self._activity_search_entries())
        return entries

    def _cached_search_entries(self) -> list[dict[str, Any]]:
        with self.store.transaction():
            generation = self.store.generation
            if self._search_index_generation != generation:
                self._search_entries = self._build_search_entries()
                self._search_index_generation = generation
            return self._search_entries

    @staticmethod
    def _search_score(entry: dict[str, Any], needle: str) -> int | None:
        if needle == entry["folded_id"]:
            return 0
        if entry["folded_title"].startswith(needle):
            return 1
        if needle in entry["folded_title"] or needle in entry["folded_id"]:
            return 2
        if any(needle in value for value in entry["folded_values"]):
            return 3
        return None

    @classmethod
    def _search_candidate(
        cls, entry: dict[str, Any], needle: str
    ) -> tuple[int, int, str, str, dict[str, Any]] | None:
        score = cls._search_score(entry, needle)
        if score is None:
            return None
        kind_order = {"task": 0, "objective": 1, "note": 2, "capture": 3, "activity": 4}
        item = {
            field: entry[field]
            for field in ("kind", "id", "title", "subtitle", "target_kind", "target_id")
        }
        return kind_order[entry["kind"]], score, entry["folded_title"], entry["id"], item

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self.store.transaction():
            tasks = self.documents.load(WorkspaceDocument.TASKS).get("tasks", [])
            task = _find(tasks, task_id, "task")
            normalized_id = task["id"]
            contexts = group_context_by_task(
                self.documents.load(WorkspaceDocument.NOTES).get("notes", []),
                (self._project_capture(capture) for capture in
                 self.documents.load(WorkspaceDocument.CAPTURES).get("captures", [])),
                (item["id"] for item in tasks),
                (item["id"] for item in
                 self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])),
            )
            context = contexts[normalized_id]
            capture_ids = {item["id"] for item in context if item["ref"]["kind"] == "capture"}
            replies = [
                reply
                for reply in self.documents.load(WorkspaceDocument.REPLIES).get("replies", [])
                if reply.get("task_id") == normalized_id
            ]
            activity_data = self.documents.load(WorkspaceDocument.ACTIVITY)
            activity = [
                copy.deepcopy(event)
                for event in activity_data.get("activity", [])
                if event.get("task_id") == normalized_id
                or (
                    event.get("capture_id") in capture_ids
                    and not str(event.get("type", "")).startswith("reply.")
                )
            ]
            activity.extend(task_facts(activity_data, normalized_id))
            projected_status = validate_and_project(
                self.documents.load(WorkspaceDocument.TASKS), activity_data
            )[normalized_id]
            return {
                "task": self._project_task(
                    task, len(context), planning_status=projected_status
                ),
                "context": context,
                "replies": [
                    self._project_reply(reply)
                    for reply in sorted(replies, key=lambda item: item["id"])
                ],
                "activity": activity,
            }

    def planning_snapshot(self, task_id: str) -> SnapshotArtifact:
        """Freeze one committed task under one validated, non-recovering read."""

        try:
            with self.store.consistent_read():
                workspace = self.documents.load(WorkspaceDocument.WORKSPACE)
                backlog = self.documents.load(WorkspaceDocument.TASKS)
                activity = self.documents.load(WorkspaceDocument.ACTIVITY)
                task = copy.deepcopy(_find(backlog.get("tasks", []), task_id, "task"))
                planning_status = validate_and_project(backlog, activity)[task["id"]]
                return create_snapshot_artifact(
                    workspace["id"], task, planning_status
                )
        except SnapshotValidationError as error:
            raise SnapshotExportRefusedError(error) from error
        except (StoreCorruptError, StoreLockedError) as error:
            raise SnapshotStoreNotReadyError(
                "Snapshot export requires a fully ready store with no pending recovery."
            ) from error

    def confirmed_snapshot_export(
        self,
        task_id: str,
        expected_revision: int,
        expected_digest: str,
        disclosure_confirmed: bool,
    ) -> SnapshotArtifact:
        """Return reviewed canonical bytes without recording or mutating export state."""

        if disclosure_confirmed is not True:
            raise SnapshotDisclosureRequiredError(
                "Explicit snapshot disclosure confirmation is required."
            )
        if type(expected_revision) is not int or expected_revision < 0:
            raise SnapshotExportConflictError(
                "The reviewed snapshot revision is invalid or stale."
            )
        if not isinstance(expected_digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_digest
        ) is None:
            raise SnapshotExportConflictError(
                "The reviewed snapshot digest is invalid or stale."
            )
        artifact = self.planning_snapshot(task_id)
        if (
            artifact.snapshot["revision"] != expected_revision
            or not secrets.compare_digest(artifact.digest, expected_digest)
        ):
            raise SnapshotExportConflictError(
                "The task changed after review; reopen the disclosure before export."
            )
        return artifact

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
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        current_status = validate_and_project(backlog, activity)[task["id"]]
        changed_fields = _patch_changed_fields(
            changes, requested_status, current_status
        )
        if not changed_fields:
            return self._project_task(task, planning_status=current_status)

        _validate_key_result_refs_state(
            changes.get("key_result_refs", task.get("key_result_refs")),
            changes.get("objective_ids", task.get("objective_ids", [])),
            objectives_by_id,
        )
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
                created_at=utc_now(),
                actor="local.user",
                provenance="api.v1",
            )
            projected_status = requested_status
        task["updated_at"] = today()
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

    def list_captures(self, status: str = "inbox") -> list[dict[str, Any]]:
        if status != "all" and status not in CAPTURE_STATUSES:
            raise DomainError("invalid capture status")
        captures = self.documents.load(WorkspaceDocument.CAPTURES).get("captures", [])
        if status != "all":
            captures = [capture for capture in captures if capture.get("status") == status]
        return [self._project_capture(capture) for capture in sorted(captures, key=lambda item: item["id"])]

    @_capture_reply_backend
    @_transactional
    def ingest_capture(
        self,
        packet: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        method: str = "POST",
        path: str = "/api/v1/captures",
    ) -> dict[str, Any]:
        request_digest = request_digest or self._request_digest(packet)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, method, path, request_digest)
        if replay:
            return replay
        sanitized = validate_capture_packet(packet)
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        captures = captures_data.setdefault("captures", [])
        existing = next(
            (item for item in captures if item.get("source_key") == sanitized["source_key"]),
            None,
        )
        now = utc_now()
        duplicate = False
        if existing is None:
            capture = {
                **sanitized,
                "id": _next_id(captures, "C", 4),
                "status": "inbox",
                "linked_task_ids": [],
                "converted_task_ids": [],
                "revision": 0,
                "created_at": now,
                "updated_at": now,
                "recent_revisions": [],
            }
            captures.append(capture)
            response_status = 201
            self._event(activity, "capture.ingested", capture_id=capture["id"])
        elif existing["source"].get("fingerprint") == sanitized["source"]["fingerprint"]:
            _require_matching_capture_review(existing, sanitized)
            capture = existing
            duplicate = True
            response_status = 200
        else:
            old_time = parse_rfc3339(existing["source"]["retrieved_at"], "stored source.retrieved_at")
            new_time = parse_rfc3339(sanitized["source"]["retrieved_at"], "source.retrieved_at")
            if new_time < old_time:
                raise StaleCaptureError("capture packet is older than the current source revision")
            if new_time == old_time:
                raise SourceRevisionConflictError(
                    "different fingerprints have the same retrieval time"
                )
            previous_actions = {
                item["id"]: item.get("task_id")
                for item in existing.get("normalized", {}).get("action_items", [])
                if item.get("task_id")
            }
            for action in sanitized["normalized"]["action_items"]:
                if action["id"] in previous_actions:
                    action["task_id"] = previous_actions[action["id"]]
            recent = list(existing.get("recent_revisions", []))
            recent.append({
                "fingerprint": existing["source"].get("fingerprint"),
                "version_ref": existing["source"].get("version_ref"),
                "retrieved_at": existing["source"].get("retrieved_at"),
                "provenance_digest": canonical_digest(existing.get("provenance", {})),
                "redaction_policy_version": existing.get("provenance", {}).get("redaction_policy_version"),
            })
            for field in ("schema_version", "source_key", "source", "normalized", "task_hints", "provenance"):
                existing[field] = sanitized[field]
            existing["recent_revisions"] = recent[-10:]
            existing["revision"] = _next_revision(existing)
            existing["updated_at"] = now
            capture = existing
            response_status = 200
            self._event(
                activity,
                "capture.updated",
                capture_id=capture["id"],
                details={"revision": capture["revision"]},
            )
        body: dict[str, Any] = {"data": self._project_capture(capture)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(
            activity, idempotency_key, method, path, request_digest, response_status, body
        )
        self.documents.save_many(
            {WorkspaceDocument.CAPTURES: captures_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-ingest-{}".format(idempotency_key),
        )
        return {"status": response_status, "body": body}

    @_capture_reply_backend
    @_transactional
    def link_capture(
        self,
        capture_id: str,
        task_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        body_input = {"task_id": task_id}
        request_digest = request_digest or self._request_digest(body_input)
        path = path or "/api/v1/captures/{}/link".format(capture_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        task = _find(self.documents.load(WorkspaceDocument.TASKS).get("tasks", []), task_id, "task")
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        duplicate = task["id"] in capture.setdefault("linked_task_ids", [])
        if not duplicate:
            capture["linked_task_ids"].append(task["id"])
            capture["linked_task_ids"].sort()
            if capture.get("status") != "converted":
                capture["status"] = "linked"
            capture["revision"] = _next_revision(capture)
            capture["updated_at"] = utc_now()
            self._event(activity, "capture.linked", capture_id=capture["id"], task_id=task["id"])
        body: dict[str, Any] = {"data": self._project_capture(capture)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(activity, idempotency_key, "POST", path, request_digest, 200, body)
        self.documents.save_many(
            {WorkspaceDocument.CAPTURES: captures_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-link-{}".format(idempotency_key),
        )
        return {"status": 200, "body": body}

    @staticmethod
    def _capture_action_matches_task(
        action: dict[str, Any], task: dict[str, Any]
    ) -> bool:
        return all(
            action.get(field, default) == task.get(field, default)
            for field, default in (
                ("title", ""),
                ("detail", ""),
                ("priority", "P2"),
                ("due", None),
            )
        )

    @classmethod
    def _link_unique_matching_capture_action(
        cls, capture: dict[str, Any], task: dict[str, Any]
    ) -> None:
        matches = [
            action
            for action in capture.get("normalized", {}).get("action_items", [])
            if not action.get("task_id") and cls._capture_action_matches_task(action, task)
        ]
        if len(matches) == 1:
            matches[0]["task_id"] = task["id"]

    def _capture_task_intent_replay(
        self,
        activity: dict[str, Any],
        backlog: dict[str, Any],
        capture: dict[str, Any],
        intent_id: str | None,
        request_digest: str,
        idempotency_key: str,
        path: str,
    ) -> dict[str, Any] | None:
        if intent_id is None:
            return None
        event = next(
            (
                item
                for item in activity.get("activity", [])
                if item.get("type") == "capture.task_created"
                and item.get("capture_id") == capture["id"]
                and item.get("details", {}).get("intent_id") == intent_id
            ),
            None,
        )
        if event is None:
            return None
        if event.get("details", {}).get("request_digest") != request_digest:
            raise IdempotencyConflictError(
                "intent_id was already used for different Task fields",
                {"intent_id": intent_id},
            )
        task = _find(backlog.get("tasks", []), event.get("task_id"), "task")
        if task["id"] not in capture.get("converted_task_ids", []):
            raise StoreCorruptError("capture task intent link is incomplete")
        planning_status = validate_and_project(backlog, activity)[task["id"]]
        body = {
            "data": self._project_task(task, 1, planning_status=planning_status),
            "meta": {"intent_replayed": True},
        }
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            200,
            body,
        )
        self.documents.save_many(
            {WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-task-intent-replay-{}".format(idempotency_key),
        )
        return {"status": 200, "body": body}

    @_transactional
    def create_task_from_capture(
        self,
        capture_id: str,
        task_fields: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        intent_id, task_input = _validate_capture_task_fields(task_fields)

        request_digest = request_digest or self._request_digest(task_fields)
        path = path or "/api/v1/captures/{}/task".format(capture_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        intent_replay = self._capture_task_intent_replay(
            activity,
            backlog,
            capture,
            intent_id,
            request_digest,
            idempotency_key,
            path,
        )
        if intent_replay is not None:
            return intent_replay
        task = self._append_task(
            backlog,
            task_input["title"],
            task_input.get("detail", ""),
            task_input.get("priority", "P2"),
            task_input.get("due"),
            task_input.get("tags", ()),
            task_input.get("objective_ids", ()),
            task_input.get("parent_id"),
            task_input.get("dependencies", ()),
        )
        append_bootstrap(
            activity,
            task,
            created_at=utc_now(),
            actor="workstack.capture",
            provenance="api.v1.capture",
        )

        converted = set(capture.setdefault("converted_task_ids", []))
        converted.add(task["id"])
        capture["converted_task_ids"] = sorted(converted)
        self._link_unique_matching_capture_action(capture, task)
        capture["status"] = "converted"
        capture["revision"] = _next_revision(capture)
        capture["updated_at"] = utc_now()
        self._event(
            activity,
            "capture.task_created",
            capture_id=capture["id"],
            task_id=task["id"],
            details=(
                {"intent_id": intent_id, "request_digest": request_digest}
                if intent_id is not None
                else None
            ),
        )
        response_body = {
            "data": self._project_task(task, 1, planning_status="open")
        }
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            201,
            response_body,
        )
        self.documents.save_many(
            {
                WorkspaceDocument.TASKS: backlog,
                WorkspaceDocument.CAPTURES: captures_data,
                WorkspaceDocument.ACTIVITY: activity,
            },
            operation_id="capture-task-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def dismiss_capture(
        self,
        capture_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        request_digest = request_digest or self._request_digest({})
        path = path or "/api/v1/captures/{}/dismiss".format(capture_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        duplicate = capture.get("status") == "dismissed"
        if not duplicate:
            capture["status"] = "dismissed"
            capture["revision"] = _next_revision(capture)
            capture["updated_at"] = utc_now()
            self._event(activity, "capture.dismissed", capture_id=capture["id"])
        body: dict[str, Any] = {"data": self._project_capture(capture)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(activity, idempotency_key, "POST", path, request_digest, 200, body)
        self.documents.save_many(
            {WorkspaceDocument.CAPTURES: captures_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-dismiss-{}".format(idempotency_key),
        )
        return {"status": 200, "body": body}

    @_transactional
    def convert_capture_action(
        self,
        capture_id: str,
        action_id: str,
        objective_ids: Iterable[str],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        raw_objectives = list(objective_ids)
        if any(not isinstance(item, str) for item in raw_objectives):
            raise DomainError("objective_ids entries must be strings")
        normalized_objectives = sorted({item.strip().upper() for item in raw_objectives if item.strip()})
        body_input = {"objective_ids": normalized_objectives}
        request_digest = request_digest or self._request_digest(body_input)
        path = path or "/api/v1/captures/{}/actions/{}/task".format(capture_id, action_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        objectives = {item["id"] for item in self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])}
        missing = sorted(set(normalized_objectives) - objectives)
        if missing:
            raise DomainError("unknown objective ids", {"ids": missing})
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        action = _find(capture.get("normalized", {}).get("action_items", []), action_id, "capture action")
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        if action.get("task_id"):
            task = _find(backlog.get("tasks", []), action["task_id"], "task")
            response_status = 200
            duplicate = True
        else:
            task_id = _next_id(backlog.setdefault("tasks", []), "T", 4)
            workspace_id = self.documents.load(WorkspaceDocument.WORKSPACE)["id"]
            task = {
                "id": task_id,
                "uid": _task_uid(workspace_id, task_id),
                "title": action["title"],
                "detail": action.get("detail", ""),
                "status": "open",
                "priority": action.get("priority", "P2"),
                "due": action.get("due"),
                "tags": copy.deepcopy(capture.get("normalized", {}).get("tags", [])),
                "objective_ids": normalized_objectives,
                "parent_id": None,
                "dependencies": [],
                "subtasks": [],
                "notes": [],
                "created": today(),
                "updated_at": today(),
                "revision": 0,
            }
            backlog["tasks"].append(task)
            append_bootstrap(
                activity,
                task,
                created_at=utc_now(),
                actor="workstack.capture",
                provenance="api.v1.capture",
            )
            action["task_id"] = task_id
            converted = set(capture.setdefault("converted_task_ids", []))
            converted.add(task_id)
            capture["converted_task_ids"] = sorted(converted)
            capture["status"] = "converted"
            capture["revision"] = _next_revision(capture)
            capture["updated_at"] = utc_now()
            self._event(
                activity,
                "capture.action_converted",
                capture_id=capture["id"],
                task_id=task_id,
                details={"action_id": action["id"]},
            )
            response_status = 201
            duplicate = False
        projected_status = validate_and_project(backlog, activity)[task["id"]]
        body: dict[str, Any] = {
            "data": self._project_task(task, 1, planning_status=projected_status)
        }
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, response_status, body
        )
        self.documents.save_many(
            {
                WorkspaceDocument.TASKS: backlog,
                WorkspaceDocument.CAPTURES: captures_data,
                WorkspaceDocument.ACTIVITY: activity,
            },
            operation_id="capture-convert-{}".format(idempotency_key),
        )
        return {"status": response_status, "body": body}

    @_capture_reply_backend
    @_transactional
    def approve_reply(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str = "/api/v1/replies",
    ) -> dict[str, Any]:
        required = {"task_id", "capture_id", "body", "approved"}
        if set(request) != required:
            raise DomainError(
                "reply approval requires only task_id, capture_id, body, and approved",
                {
                    "missing": sorted(required - set(request)),
                    "unknown": sorted(set(request) - required),
                },
            )
        if not isinstance(request["task_id"], str) or not request["task_id"]:
            raise DomainError("task_id must be a non-empty string", {"field": "task_id"})
        if not isinstance(request["capture_id"], str) or not request["capture_id"]:
            raise DomainError("capture_id must be a non-empty string", {"field": "capture_id"})
        if request["approved"] is not True:
            raise DomainError("approved must be true", {"field": "approved"})
        body = _approved_plain_text(request["body"])

        request_digest = request_digest or self._request_digest(request)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog.get("tasks", []), request["task_id"], "task")
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), request["capture_id"], "capture")
        task_links = set(capture.get("linked_task_ids", [])) | set(
            capture.get("converted_task_ids", [])
        )
        if task["id"] not in task_links:
            raise DomainError(
                "capture is not linked to the task",
                {"task_id": task["id"], "capture_id": capture["id"]},
            )

        source = capture.get("source", {})
        provider = source.get("provider")
        capability = REPLY_CAPABILITIES.get(provider) if isinstance(provider, str) else None
        if capability is None:
            raise DomainError(
                "capture provider does not support replies",
                {"capture_id": capture["id"], "provider": provider},
            )
        target = {
            field: _opaque_reference(
                source.get(field), "source.{}".format(field), REPLY_TARGET_REF_MAX
            )
            for field in REPLY_TARGET_FIELDS
        }
        now = utc_now()
        replies_data = self.documents.load(WorkspaceDocument.REPLIES)
        replies = replies_data.setdefault("replies", [])
        reply = {
            "id": _next_id(replies, "R", 4),
            "task_id": task["id"],
            "capture_id": capture["id"],
            "capture_revision": _revision(capture),
            "provider": provider,
            "capability": capability,
            "target": target,
            "body": body,
            "body_digest": canonical_digest(body),
            "target_digest": canonical_digest(target),
            "state": "approved",
            "approved_at": now,
            "receipt": None,
            "created_at": now,
            "updated_at": now,
        }
        replies.append(reply)
        self._event(
            activity,
            "reply.approved",
            capture_id=capture["id"],
            task_id=task["id"],
            reply_id=reply["id"],
            details={"provider": provider, "state": "approved"},
        )
        response_body = {"data": self._project_reply(reply)}
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            201,
            None,
            response_ref={"kind": "reply", "id": reply["id"]},
        )
        self.documents.save_many(
            {WorkspaceDocument.REPLIES: replies_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="reply-approve-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @staticmethod
    def _validate_reply_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        _validate_reply_receipt_shape(receipt)
        if receipt["schema_version"] != "1.0":
            raise DomainError("schema_version must be 1.0", {"field": "schema_version"})
        reply_id = _opaque_reference(receipt["reply_id"], "reply_id", 64)
        provider = _reply_receipt_provider(receipt)
        outcome = _reply_receipt_outcome(receipt)
        occurred_at = _reply_receipt_occurred_at(receipt)
        digests = _reply_receipt_digests(receipt)
        projected: dict[str, Any] = {
            "schema_version": "1.0",
            "reply_id": reply_id,
            "provider": provider,
            "outcome": outcome,
            "occurred_at": occurred_at,
            **digests,
        }
        _project_reply_receipt_optional_fields(receipt, projected)
        return projected

    @_capture_reply_backend
    @_transactional
    def apply_reply_receipt(
        self,
        reply_id: str,
        receipt_input: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        request_digest = request_digest or self._request_digest(receipt_input)
        path = path or "/api/v1/replies/{}/receipt".format(reply_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        receipt = self._validate_reply_receipt(receipt_input)
        replies_data = self.documents.load(WorkspaceDocument.REPLIES)
        reply = _find(replies_data.get("replies", []), reply_id, "reply")
        mismatched = _reply_receipt_mismatches(receipt, reply)
        if mismatched:
            raise ReplyReceiptConflictError(
                "reply receipt does not match the approved command",
                {"fields": mismatched},
            )

        duplicate, event_details = _apply_terminal_reply_state(reply, receipt)
        if event_details is not None:
            self._event(
                activity,
                "reply.{}".format(reply["state"]),
                capture_id=reply["capture_id"],
                task_id=reply["task_id"],
                reply_id=reply["id"],
                details=event_details,
            )

        response_body: dict[str, Any] = {"data": self._project_reply(reply)}
        if duplicate:
            response_body["meta"] = {"duplicate": True}
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            200,
            None,
            response_ref={"kind": "reply", "id": reply["id"]},
            response_meta={"duplicate": True} if duplicate else None,
        )
        self.documents.save_many(
            {WorkspaceDocument.REPLIES: replies_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="reply-receipt-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_transactional
    def weekly_report(self, end: str | None = None, days: int = 7) -> dict[str, Any]:
        start_day, end_day = _weekly_range(end, days)
        tasks = {task["id"]: task for task in self.list_tasks(status="all")}
        objectives = {item["id"]: item for item in self.list_objectives(status="all")}
        worklog = self._active_worklog().get("days", {})
        projects = _weekly_projects(worklog, tasks, start_day, end_day)
        return {
            "range": {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": days},
            "objectives": _weekly_objectives(projects, objectives),
            "projects": list(projects.values()),
        }

    @_transactional
    def snapshot(self) -> dict[str, Any]:
        objectives = self.list_objectives(status="all")
        tasks = self.list_tasks(status="all")
        # The Graph is an ACTIVE reader: its day counts and Worklog edges use
        # the same validated membership as Review, weekly and the Agent
        # readers. The physical day itself is retained, so a day whose entries
        # are all superseded still appears with a zero count.
        worklog = self._active_worklog().get("days", {})
        notes = self.documents.load(WorkspaceDocument.NOTES).get("notes", [])
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        known: set[str] = set()

        for objective in objectives:
            node = _snapshot_objective_node(objective)
            nodes.append(node)
            known.add(node["id"])
        for task in tasks:
            _append_snapshot_task(task, nodes, edges, known)
        _append_snapshot_worklog(worklog, nodes, edges, known)
        _append_snapshot_notes(notes, nodes, edges, known)
        edges = [edge for edge in edges if edge["source"] in known and edge["target"] in known]
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "nodes": nodes,
            "edges": edges,
            "summary": {
                "objectives": len(objectives),
                "tasks": len(tasks),
                "days": len(worklog),
                "notes": len(notes),
            },
        }
