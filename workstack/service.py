"""Domain logic shared by the CLI and web API."""

from __future__ import annotations

import datetime as dt
import copy
import re
import secrets
import unicodedata
import uuid
from functools import wraps
from typing import Any, Iterable
from urllib.parse import urlsplit

from . import __version__
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
from .planning_status import (
    append_bootstrap,
    append_transition,
    task_facts,
    validate_and_project,
)
from .maintenance import BackupDownload, create_backup_download
from .snapshot import SnapshotValidationError
from .snapshot_export import SnapshotArtifact, create_snapshot_artifact
from .store import DEFAULTS, MAX_REVISION, Store, StoreCorruptError, StoreLockedError


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


class WorkStack:
    def __init__(self, store: Store | None = None, *, initialize: bool = True) -> None:
        self.store = store or Store()
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
        known_tasks = {item["id"] for item in data["tasks"]}
        normalized_parent = parent_id.strip().upper() if parent_id else None
        normalized_dependencies = sorted(
            set(str(item).strip().upper() for item in dependencies if str(item).strip())
        )
        unknown_tasks = sorted(
            ({normalized_parent} if normalized_parent else set())
            | (set(normalized_dependencies) - known_tasks)
        )
        unknown_tasks = [item for item in unknown_tasks if item not in known_tasks]
        if unknown_tasks:
            raise ValueError("unknown task ids: {}".format(", ".join(unknown_tasks)))
        task_id = _next_id(data["tasks"], "T", 4)
        workspace_id = self.store.load("workspace.json")["id"]
        task = {
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
            "parent_id": normalized_parent,
            "dependencies": normalized_dependencies,
            "subtasks": [],
            "notes": [],
            "created": today(),
            "updated_at": today(),
            "revision": 0,
        }
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
        data = self.store.load("backlog.json")
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
        activity = self.store.load("activity.json")
        append_bootstrap(
            activity,
            task,
            created_at=utc_now(),
            actor="local.user",
            provenance="cli",
        )
        self.store.save_many(
            {"backlog.json": data, "activity.json": activity},
            operation_id="task-create-cli-{}".format(task["id"]),
        )
        return task

    @staticmethod
    def _validate_task_create_v1(body: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical, strict v1 task-create payload."""

        if not isinstance(body, dict):
            raise DomainError("request body must be a JSON object")
        allowed = {
            "title", "detail", "priority", "due", "scheduled", "estimate_minutes",
            "tags", "objective_ids",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise DomainError("task create has unknown fields", {"fields": unknown})

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

        due = body.get("due")
        if due is not None:
            if not isinstance(due, str):
                raise DomainError("due must be null or YYYY-MM-DD", {"field": "due"})
            try:
                parsed_due = dt.date.fromisoformat(due)
            except ValueError as error:
                raise DomainError("due must be null or YYYY-MM-DD", {"field": "due"}) from error
            if parsed_due.isoformat() != due:
                raise DomainError("due must be null or YYYY-MM-DD", {"field": "due"})

        scheduled = body.get("scheduled")
        if scheduled is not None:
            if not isinstance(scheduled, str):
                raise DomainError(
                    "scheduled must be null or YYYY-MM-DD", {"field": "scheduled"}
                )
            try:
                parsed_scheduled = dt.date.fromisoformat(scheduled)
            except ValueError as error:
                raise DomainError(
                    "scheduled must be null or YYYY-MM-DD", {"field": "scheduled"}
                ) from error
            if parsed_scheduled.isoformat() != scheduled:
                raise DomainError(
                    "scheduled must be null or YYYY-MM-DD", {"field": "scheduled"}
                )

        estimate_minutes = body.get("estimate_minutes")
        if estimate_minutes is not None and (
            not isinstance(estimate_minutes, int)
            or isinstance(estimate_minutes, bool)
            or not 1 <= estimate_minutes <= 1440
        ):
            raise DomainError(
                "estimate_minutes must be null or an integer from 1 to 1440",
                {"field": "estimate_minutes"},
            )

        def string_list(field: str, *, uppercase: bool = False) -> list[str]:
            values = body.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise DomainError("{} must be an array of strings".format(field), {"field": field})
            normalized = (value.strip() for value in values)
            if uppercase:
                normalized = (value.upper() for value in normalized)
            return sorted(set(value for value in normalized if value))

        return {
            "title": title.strip(),
            "detail": detail.strip(),
            "priority": priority,
            "due": due,
            "scheduled": scheduled,
            "estimate_minutes": estimate_minutes,
            "tags": string_list("tags"),
            "objective_ids": string_list("objective_ids", uppercase=True),
        }

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
        )
        if replay is not None:
            return replay

        backlog = self.store.load("backlog.json")
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
        self.store.save_many(
            {"backlog.json": backlog, "activity.json": activity},
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        backlog = self.store.load("backlog.json")
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
        self.store.save_many(
            {"backlog.json": backlog, "activity.json": activity},
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay
        if body["priority"] not in PRIORITIES:
            raise ValueError("invalid priority")

        backlog = self.store.load("backlog.json")
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
        self.store.save_many(
            {"backlog.json": backlog, "activity.json": activity},
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
        data = self.store.load("backlog.json")
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
        self.store.save("backlog.json", data)
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
        data = self.store.load("backlog.json")
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        subtask = _find(task.setdefault("subtasks", []), subtask_id, "subtask")
        subtask["status"] = status
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.store.save("backlog.json", data)
        return subtask

    def list_tasks(self, status: str = "active") -> list[dict[str, Any]]:
        backlog = self.store.load("backlog.json")
        projection = validate_and_project(backlog, self.store.load("activity.json"))
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
        backlog = self.store.load("backlog.json")
        task = copy.deepcopy(_find(backlog.get("tasks", []), task_id, "task"))
        projection = validate_and_project(backlog, self.store.load("activity.json"))
        task["status"] = projection[task["id"]]
        return task

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
        data = self.store.load("backlog.json")
        activity = self.store.load("activity.json")
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
        self.store.save_many(
            {"backlog.json": data, "activity.json": activity},
            operation_id="task-status-{}-r{}".format(task["id"], next_revision),
        )
        return self._project_task(task, planning_status=status)

    @_transactional
    def add_task_note(
        self,
        task_id: str,
        text: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        data = self.store.load("backlog.json")
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        note = {"date": today(), "text": _required_text(text, "text")}
        task.setdefault("notes", []).append(note)
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.store.save("backlog.json", data)
        return note

    @_transactional
    def add_objective(self, text: str, quarter: str | None = None) -> dict[str, Any]:
        data = self.store.load("okr.json")
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
        self.store.save("okr.json", data)
        return objective

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.store.load("okr.json")
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
        self.store.save_many(
            {"okr.json": data, "activity.json": activity},
            operation_id="objective-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def add_key_result(self, objective_id: str, text: str, target: str = "") -> dict[str, Any]:
        data = self.store.load("okr.json")
        activity = self.store.load("activity.json")
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
        self.store.save_many(
            {"okr.json": data, "activity.json": activity},
            operation_id="key-result-create-{}-r{}".format(key_result["id"], next_revision),
        )
        return key_result

    def list_objectives(self, status: str = "active") -> list[dict[str, Any]]:
        objectives = list(self.store.load("okr.json").get("objectives", []))
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
                self.store.load("okr.json").get("objectives", []), objective_id, "objective"
            )
            normalized_id = objective["id"]
            tasks = [
                self._project_task(task)
                for task in self.list_tasks(status="all")
                if normalized_id in task.get("objective_ids", [])
            ]
            activity = [
                copy.deepcopy(event)
                for event in self.store.load("activity.json").get("activity", [])
                if event.get("details", {}).get("objective_id") == normalized_id
            ]
            return {
                "objective": self._project_objective(objective),
                "tasks": tasks,
                "activity": activity,
            }

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.store.load("okr.json")
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
        self.store.save_many(
            {"okr.json": data, "activity.json": activity},
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
        data = self.store.load("okr.json")
        activity = self.store.load("activity.json")
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
        self.store.save_many(
            {"okr.json": data, "activity.json": activity},
            operation_id="key-result-update-{}-r{}".format(key_result["id"], next_revision),
        )
        return self._project_objective(objective)

    @_transactional
    def patch_objective_v1(self, objective_id: str, body: dict[str, Any]) -> dict[str, Any]:
        data = self.store.load("okr.json")
        activity = self.store.load("activity.json")
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
        self.store.save_many(
            {"okr.json": data, "activity.json": activity},
            operation_id="objective-update-{}-r{}".format(objective["id"], next_revision),
        )
        return self._project_objective(objective)

    @_transactional
    def link_task(self, objective_id: str, task_id: str) -> dict[str, Any]:
        _find(self.store.load("okr.json").get("objectives", []), objective_id, "objective")
        data = self.store.load("backlog.json")
        task = _find(data["tasks"], task_id, "task")
        next_revision = _next_revision(task)
        links = set(task.setdefault("objective_ids", []))
        links.add(objective_id.strip().upper())
        task["objective_ids"] = sorted(links)
        task["updated_at"] = today()
        task["revision"] = next_revision
        self.store.save("backlog.json", data)
        return task

    @_transactional
    def set_key_result_progress(
        self,
        objective_id: str,
        key_result_id: str,
        progress: int,
    ) -> dict[str, Any]:
        progress = max(0, min(100, int(progress)))
        data = self.store.load("okr.json")
        activity = self.store.load("activity.json")
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
        self.store.save_many(
            {"okr.json": data, "activity.json": activity},
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

    @_transactional
    def checkin(self, time: str | None = None, date: str | None = None) -> dict[str, Any]:
        date = date or today()
        dt.date.fromisoformat(date)
        if time is None:
            time = dt.datetime.now().strftime("%H:%M")
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time):
            raise ValueError("time must use HH:MM")
        data = self.store.load("worklog.json")
        day = data.setdefault("days", {}).setdefault(date, {"entries": []})
        day["start_time"] = time
        self.store.save("worklog.json", data)
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
        data = self.store.load("worklog.json")
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
        self.store.save("worklog.json", data)
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.store.load("worklog.json")
        day = worklog.setdefault("days", {}).setdefault(date, {"entries": []})
        day["start_time"] = time
        response_body = {
            "data": {"date": date, "start_time": time},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.store.save_many(
            {"worklog.json": worklog, "activity.json": activity},
            operation_id="review-checkin-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def add_worklog_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/entries",
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.store.load("worklog.json")
        day = worklog.setdefault("days", {}).setdefault(canonical["date"], {"entries": []})
        entry = {
            "task_id": task["id"],
            "task": task["title"],
            "done": canonical["done"],
            "next": canonical["next"],
            "blockers": canonical["blockers"],
        }
        day.setdefault("entries", []).append(entry)
        response_body = {
            "data": {"date": canonical["date"], **copy.deepcopy(entry)},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.store.save_many(
            {"worklog.json": worklog, "activity.json": activity},
            operation_id="review-entry-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

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
    def _work_session_records(cls, worklog: dict[str, Any]) -> list[dict[str, Any]]:
        days = worklog.get("days")
        if not isinstance(days, dict):
            raise StoreCorruptError("persisted worklog days are invalid")
        sessions: list[dict[str, Any]] = []
        seen: set[str] = set()
        active_count = 0
        for date, day in days.items():
            try:
                valid_date = cls._review_date(date)
            except DomainError as error:
                raise StoreCorruptError("persisted worklog date is invalid") from error
            if not isinstance(day, dict):
                raise StoreCorruptError("persisted worklog day is invalid")
            day_sessions = day.get("sessions", [])
            if not isinstance(day_sessions, list):
                raise StoreCorruptError("persisted work sessions are invalid")
            for session in day_sessions:
                if not isinstance(session, dict):
                    raise StoreCorruptError("persisted work session is invalid")
                session_id = session.get("id")
                if (
                    not isinstance(session_id, str)
                    or not re.fullmatch(r"WS-\d{6,}", session_id)
                    or session_id in seen
                ):
                    raise StoreCorruptError("persisted work session id is invalid")
                seen.add(session_id)
                if session.get("date") != valid_date:
                    raise StoreCorruptError("persisted work session date is invalid")
                if not isinstance(session.get("task_id"), str) or not re.fullmatch(
                    r"T-\d+", session["task_id"]
                ):
                    raise StoreCorruptError("persisted work session task id is invalid")
                if not isinstance(session.get("task"), str) or not session["task"].strip():
                    raise StoreCorruptError("persisted work session task title is invalid")
                state = session.get("state")
                worklog_state = session.get("worklog_state")
                if state not in {"running", "paused", "stopped"}:
                    raise StoreCorruptError("persisted work session state is invalid")
                expected_worklog_states = (
                    {"not_ready"} if state in {"running", "paused"} else {"pending", "recorded"}
                )
                if worklog_state not in expected_worklog_states:
                    raise StoreCorruptError("persisted work session worklog state is invalid")
                cls._work_session_timestamp(session.get("started_at"), "started_at")
                cls._work_session_timestamp(session.get("updated_at"), "updated_at")
                segments = session.get("segments")
                if not isinstance(segments, list) or not segments:
                    raise StoreCorruptError("persisted work session segments are invalid")
                open_segments = 0
                previous_end: dt.datetime | None = None
                for index, segment in enumerate(segments):
                    if not isinstance(segment, dict) or set(segment) != {"started_at", "ended_at"}:
                        raise StoreCorruptError("persisted work session segment is invalid")
                    segment_start = cls._work_session_timestamp(
                        segment["started_at"], "segment.started_at"
                    )
                    if previous_end is not None and segment_start < previous_end:
                        raise StoreCorruptError("persisted work session segments overlap")
                    if segment["ended_at"] is None:
                        open_segments += 1
                        if index != len(segments) - 1:
                            raise StoreCorruptError("persisted work session open segment is invalid")
                        previous_end = None
                    else:
                        segment_end = cls._work_session_timestamp(
                            segment["ended_at"], "segment.ended_at"
                        )
                        if segment_end < segment_start:
                            raise StoreCorruptError(
                                "persisted work session segment has negative duration"
                            )
                        previous_end = segment_end
                if (state == "running") != (open_segments == 1):
                    raise StoreCorruptError("persisted work session open segment is inconsistent")
                if state in {"paused", "stopped"} and open_segments:
                    raise StoreCorruptError("persisted work session open segment is inconsistent")
                if state in {"running", "paused"}:
                    active_count += 1
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

    @_transactional
    def work_sessions_projection(self) -> dict[str, Any]:
        worklog = self.store.load("worklog.json")
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.store.load("worklog.json")
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
        self.store.save_many(
            {"worklog.json": worklog, "activity.json": activity},
            operation_id="work-session-start-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.store.load("worklog.json")
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
        self.store.save_many(
            {"worklog.json": worklog, "activity.json": activity},
            operation_id="work-session-{}-{}".format(action, idempotency_key),
        )
        return {"status": 200, "body": response_body}

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.store.load("worklog.json")
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
        self.store.save_many(
            {"worklog.json": worklog, "activity.json": activity},
            operation_id="work-session-worklog-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def review_projection(self, date: str, days: int = 7) -> dict[str, Any]:
        date = self._review_date(date)
        if type(days) is not int or days < 1 or days > 31:
            raise DomainError("days must be between 1 and 31", {"field": "days"})
        worklog = self.store.load("worklog.json")
        day = worklog.get("days", {}).get(date, {})
        return {
            "day": {
                "date": date,
                "start_time": day.get("start_time"),
                "entries": copy.deepcopy(day.get("entries", [])),
            },
            "weekly": self.weekly_report(end=date, days=days),
        }

    def list_worklog(self, date: str | None = None) -> dict[str, Any]:
        data = self.store.load("worklog.json")
        if date:
            dt.date.fromisoformat(date)
            return {"date": date, "entries": data.get("days", {}).get(date, {}).get("entries", [])}
        return data

    @_transactional
    def add_note(self, text: str, links: Iterable[str] = ()) -> dict[str, Any]:
        data = self.store.load("notes.json")
        note = {
            "id": _next_id(data["notes"], "N", 4),
            "text": _required_text(text, "text"),
            "links": sorted(set(str(link).strip().upper() for link in links if str(link).strip())),
            "created": today(),
        }
        data["notes"].append(note)
        self.store.save("notes.json", data)
        return note

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.store.load("notes.json")
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
        self.store.save_many(
            {"notes.json": data, "activity.json": activity},
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
                    self.store.load("replies.json").get("replies", []),
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
            total_bytes = sum(self.store.path(name).stat().st_size for name in DEFAULTS)
            return {
                "workspace_id": readiness.workspace_uid,
                "store_schema_version": readiness.schema_version,
                "product_version": __version__,
                "file_count": len(DEFAULTS),
                "total_bytes": total_bytes,
                "backup_format": "workstack-backup-v1",
                "restore_requires_shutdown": True,
            }

    def create_backup_download(self) -> BackupDownload:
        """Create a read-only archive while this server owns the store lease."""

        return create_backup_download(self.store)

    def workspace_projection(self) -> dict[str, Any]:
        with self.store.transaction():
            workspace = self.store.load("workspace.json")
            captures = self.store.load("captures.json").get("captures", [])
            counts: dict[str, int] = {}
            for capture in captures:
                for task_id in set(capture.get("linked_task_ids", [])) | set(capture.get("converted_task_ids", [])):
                    counts[task_id] = counts.get(task_id, 0) + 1
            tasks = [
                self._project_task(task, counts.get(task.get("id"), 0))
                for task in self.list_tasks(status="all")
            ]
            snapshot = self.snapshot()
            return {
                "schema_version": "1.0",
                "workspace": {"id": workspace["id"], "name": workspace.get("name", "Work Stack")},
                "tasks": tasks,
                "objectives": [
                    self._project_objective(objective)
                    for objective in self.store.load("okr.json").get("objectives", [])
                ],
                "notes": copy.deepcopy(self.store.load("notes.json").get("notes", [])),
                "edges": snapshot["edges"],
                "inbox_count": sum(1 for capture in captures if capture.get("status") == "inbox"),
            }

    def search_projection(self, query: str, limit: int = 30) -> dict[str, Any]:
        """Search allowlisted local projections without exposing source or reply payloads."""

        if not isinstance(query, str):
            raise DomainError("search query must be a string")
        query = _reject_controls(query.strip(), "query", multiline=False)
        if not 2 <= len(query) <= 100:
            raise DomainError("search query must be between 2 and 100 characters")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DomainError("search limit must be between 1 and 50")
        needle = query.casefold()
        candidates: list[tuple[int, int, str, str, dict[str, Any]]] = []
        kind_order = {"task": 0, "objective": 1, "note": 2, "capture": 3, "activity": 4}

        def index_item(
            kind: str,
            item_id: str,
            title: str,
            subtitle: str,
            searchable: Iterable[str],
            target_kind: str,
            target_id: str | None,
        ) -> None:
            values = [str(value) for value in searchable if value is not None]
            self._search_entries.append({
                "kind": kind,
                "id": item_id,
                "title": title,
                "subtitle": subtitle,
                "target_kind": target_kind,
                "target_id": target_id,
                "folded_title": title.casefold(),
                "folded_id": item_id.casefold(),
                "folded_values": tuple(value.casefold() for value in values),
            })

        with self.store.transaction():
            generation = self.store.generation
            if self._search_index_generation != generation:
                self._search_entries = []
                for task in self.list_tasks(status="all"):
                    index_item(
                        "task",
                        task["id"],
                        task["title"],
                        "{} · {}{}".format(
                            task.get("status", "open"),
                            task.get("priority", "P2"),
                            " · due {}".format(task["due"]) if task.get("due") else "",
                        ),
                        [
                            task.get("detail", ""),
                            *task.get("tags", []),
                            *task.get("objective_ids", []),
                            *(note.get("text", "") for note in task.get("notes", [])),
                            *(subtask.get("title", "") for subtask in task.get("subtasks", [])),
                        ],
                        "task",
                        task["id"],
                    )

                for objective in self.list_objectives(status="all"):
                    index_item(
                        "objective",
                        objective["id"],
                        objective["objective"],
                        "{} · {}".format(
                            objective.get("quarter", "No quarter"),
                            objective.get("status", "active"),
                        ),
                        [
                            *(result.get("text", "") for result in objective.get("key_results", [])),
                            *(result.get("target", "") for result in objective.get("key_results", [])),
                        ],
                        "objective",
                        objective["id"],
                    )

                for note in self.store.load("notes.json").get("notes", []):
                    text = str(note.get("text", ""))
                    index_item(
                        "note",
                        str(note.get("id", "")),
                        text[:100] or str(note.get("id", "Note")),
                        "Graph note · {} links".format(len(note.get("links", []))),
                        [text, *note.get("links", [])],
                        "workspace",
                        None,
                    )

                captures = self.store.load("captures.json").get("captures", [])
                for capture in captures:
                    projected = self._project_capture(capture)
                    source = projected.get("source", {})
                    normalized = projected.get("normalized", {})
                    index_item(
                        "capture",
                        projected["id"],
                        source.get("display_title", projected["id"]),
                        "{} · {}".format(
                            source.get("provider", "manual"), projected.get("status", "inbox")
                        ),
                        [
                            normalized.get("summary", ""),
                            normalized.get("context", ""),
                            *(action.get("title", "") for action in normalized.get("action_items", [])),
                        ],
                        "capture",
                        projected["id"],
                    )

                for event in self.store.load("activity.json").get("activity", []):
                    event_type = str(event.get("type", ""))
                    details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}
                    safe_terms = [event_type, details.get("provider", ""), details.get("state", "")]
                    target_kind = "task" if event.get("task_id") else "capture" if event.get("capture_id") else "workspace"
                    target_id = event.get("task_id") or event.get("capture_id")
                    index_item(
                        "activity",
                        str(event.get("id", "")),
                        event_type.replace(".", " ").strip().title() or "Activity",
                        str(event.get("created_at", "")),
                        safe_terms,
                        target_kind,
                        target_id if isinstance(target_id, str) else None,
                    )
                self._search_index_generation = generation

            entries = self._search_entries

        for entry in entries:
            if needle == entry["folded_id"]:
                score = 0
            elif entry["folded_title"].startswith(needle):
                score = 1
            elif needle in entry["folded_title"] or needle in entry["folded_id"]:
                score = 2
            elif any(needle in value for value in entry["folded_values"]):
                score = 3
            else:
                continue
            item = {
                field: entry[field]
                for field in ("kind", "id", "title", "subtitle", "target_kind", "target_id")
            }
            candidates.append((
                kind_order[entry["kind"]],
                score,
                entry["folded_title"],
                entry["id"],
                item,
            ))

        candidates.sort(key=lambda candidate: candidate[:4])
        return {"query": query, "items": [candidate[4] for candidate in candidates[:limit]]}

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self.store.transaction():
            task = _find(self.store.load("backlog.json").get("tasks", []), task_id, "task")
            normalized_id = task["id"]
            captures = [
                capture
                for capture in self.store.load("captures.json").get("captures", [])
                if normalized_id in capture.get("linked_task_ids", [])
                or normalized_id in capture.get("converted_task_ids", [])
            ]
            capture_ids = {capture["id"] for capture in captures}
            replies = [
                reply
                for reply in self.store.load("replies.json").get("replies", [])
                if reply.get("task_id") == normalized_id
            ]
            activity_data = self.store.load("activity.json")
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
                self.store.load("backlog.json"), activity_data
            )[normalized_id]
            return {
                "task": self._project_task(
                    task, len(captures), planning_status=projected_status
                ),
                "context": [self._project_capture(capture) for capture in captures],
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
                workspace = self.store.load("workspace.json")
                backlog = self.store.load("backlog.json")
                activity = self.store.load("activity.json")
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

    @_transactional
    def patch_task(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise DomainError("task patch must be an object")
        allowed = {
            "title", "detail", "status", "priority", "due", "scheduled",
            "estimate_minutes", "tags", "objective_ids",
            "parent_id", "dependencies", "revision",
        }
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise DomainError("unknown task fields", {"fields": unknown})
        expected_revision = patch.get("revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise DomainError("revision is required and must be a non-negative integer")
        backlog = self.store.load("backlog.json")
        task = _find(backlog.get("tasks", []), task_id, "task")
        current_revision = _revision(task)
        if expected_revision != current_revision:
            raise RevisionConflictError(
                "task revision is stale",
                {"expected": current_revision, "received": expected_revision},
            )
        tasks_by_id = {item["id"]: item for item in backlog.get("tasks", [])}
        objectives = {item["id"] for item in self.store.load("okr.json").get("objectives", [])}
        changes = {key: value for key, value in patch.items() if key != "revision"}
        if "title" in changes:
            if not isinstance(changes["title"], str):
                raise DomainError("title must be a string")
            changes["title"] = _required_text(changes["title"], "title")
        if "detail" in changes:
            if not isinstance(changes["detail"], str):
                raise DomainError("detail must be a string")
            changes["detail"] = changes["detail"].strip()
        if "status" in changes and changes["status"] not in TASK_STATUSES:
            raise DomainError("invalid task status")
        if "priority" in changes and changes["priority"] not in PRIORITIES:
            raise DomainError("invalid task priority")
        if "due" in changes and changes["due"] is not None:
            if not isinstance(changes["due"], str):
                raise DomainError("due must be an ISO date or null")
            try:
                parsed_due = dt.date.fromisoformat(changes["due"])
            except ValueError as error:
                raise DomainError("due must be an ISO date or null") from error
            if parsed_due.isoformat() != changes["due"]:
                raise DomainError("due must be an ISO date or null")
        if "scheduled" in changes and changes["scheduled"] is not None:
            if not isinstance(changes["scheduled"], str):
                raise DomainError("scheduled must be an ISO date or null")
            try:
                parsed_scheduled = dt.date.fromisoformat(changes["scheduled"])
            except ValueError as error:
                raise DomainError("scheduled must be an ISO date or null") from error
            if parsed_scheduled.isoformat() != changes["scheduled"]:
                raise DomainError("scheduled must be an ISO date or null")
        if "estimate_minutes" in changes and changes["estimate_minutes"] is not None:
            estimate = changes["estimate_minutes"]
            if (
                not isinstance(estimate, int)
                or isinstance(estimate, bool)
                or not 1 <= estimate <= 1440
            ):
                raise DomainError(
                    "estimate_minutes must be null or an integer from 1 to 1440"
                )
        for field in ("tags", "objective_ids", "dependencies"):
            if field in changes and not isinstance(changes[field], list):
                raise DomainError("{} must be an array".format(field))
        if "tags" in changes:
            if any(not isinstance(item, str) for item in changes["tags"]):
                raise DomainError("tags entries must be strings")
            changes["tags"] = sorted({item.strip() for item in changes["tags"] if item.strip()})
        if "objective_ids" in changes:
            if any(not isinstance(item, str) for item in changes["objective_ids"]):
                raise DomainError("objective_ids entries must be strings")
            changes["objective_ids"] = sorted({item.strip().upper() for item in changes["objective_ids"] if item.strip()})
            missing = sorted(set(changes["objective_ids"]) - objectives)
            if missing:
                raise DomainError("unknown objective ids", {"ids": missing})
        if "dependencies" in changes:
            if any(not isinstance(item, str) for item in changes["dependencies"]):
                raise DomainError("dependencies entries must be strings")
            changes["dependencies"] = sorted({item.strip().upper() for item in changes["dependencies"] if item.strip()})
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
        if "parent_id" in changes:
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
        activity = self.store.load("activity.json")
        current_status = validate_and_project(backlog, activity)[task["id"]]
        requested_status = changes.pop("status", None)
        changed_fields = sorted(changes)
        if requested_status is not None:
            changed_fields.append("status")
        if requested_status == current_status:
            changed_fields.remove("status")
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
        self.store.save_many(
            {"backlog.json": backlog, "activity.json": activity},
            operation_id="task-patch-{}-r{}".format(task["id"], task["revision"]),
        )
        return self._project_task(task, planning_status=projected_status)

    def list_captures(self, status: str = "inbox") -> list[dict[str, Any]]:
        if status != "all" and status not in CAPTURE_STATUSES:
            raise DomainError("invalid capture status")
        captures = self.store.load("captures.json").get("captures", [])
        if status != "all":
            captures = [capture for capture in captures if capture.get("status") == status]
        return [self._project_capture(capture) for capture in sorted(captures, key=lambda item: item["id"])]

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(activity, idempotency_key, method, path, request_digest)
        if replay:
            return replay
        sanitized = validate_capture_packet(packet)
        captures_data = self.store.load("captures.json")
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
        self.store.save_many(
            {"captures.json": captures_data, "activity.json": activity},
            operation_id="capture-ingest-{}".format(idempotency_key),
        )
        return {"status": response_status, "body": body}

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        task = _find(self.store.load("backlog.json").get("tasks", []), task_id, "task")
        captures_data = self.store.load("captures.json")
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
        self.store.save_many(
            {"captures.json": captures_data, "activity.json": activity},
            operation_id="capture-link-{}".format(idempotency_key),
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
        allowed = {
            "title",
            "detail",
            "priority",
            "due",
            "tags",
            "objective_ids",
            "parent_id",
            "dependencies",
        }
        unknown = sorted(set(task_fields) - allowed)
        if unknown:
            raise DomainError("unknown task fields", {"fields": unknown})
        if "title" not in task_fields or not isinstance(task_fields["title"], str):
            raise DomainError("title is required and must be a string", {"field": "title"})
        for field in ("detail", "priority"):
            if field in task_fields and not isinstance(task_fields[field], str):
                raise DomainError("{} must be a string".format(field), {"field": field})
        if "due" in task_fields and task_fields["due"] is not None and not isinstance(task_fields["due"], str):
            raise DomainError("due must be an ISO date or null", {"field": "due"})
        if "parent_id" in task_fields and task_fields["parent_id"] is not None and not isinstance(task_fields["parent_id"], str):
            raise DomainError("parent_id must be a task ID or null", {"field": "parent_id"})
        for field in ("tags", "objective_ids", "dependencies"):
            if field in task_fields:
                value = task_fields[field]
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise DomainError("{} must be an array of strings".format(field), {"field": field})

        request_digest = request_digest or self._request_digest(task_fields)
        path = path or "/api/v1/captures/{}/task".format(capture_id)
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        captures_data = self.store.load("captures.json")
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        backlog = self.store.load("backlog.json")
        task = self._append_task(
            backlog,
            task_fields["title"],
            task_fields.get("detail", ""),
            task_fields.get("priority", "P2"),
            task_fields.get("due"),
            task_fields.get("tags", ()),
            task_fields.get("objective_ids", ()),
            task_fields.get("parent_id"),
            task_fields.get("dependencies", ()),
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
        capture["status"] = "converted"
        capture["revision"] = _next_revision(capture)
        capture["updated_at"] = utc_now()
        self._event(
            activity,
            "capture.task_created",
            capture_id=capture["id"],
            task_id=task["id"],
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
        self.store.save_many(
            {
                "backlog.json": backlog,
                "captures.json": captures_data,
                "activity.json": activity,
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        captures_data = self.store.load("captures.json")
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
        self.store.save_many(
            {"captures.json": captures_data, "activity.json": activity},
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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        objectives = {item["id"] for item in self.store.load("okr.json").get("objectives", [])}
        missing = sorted(set(normalized_objectives) - objectives)
        if missing:
            raise DomainError("unknown objective ids", {"ids": missing})
        captures_data = self.store.load("captures.json")
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        action = _find(capture.get("normalized", {}).get("action_items", []), action_id, "capture action")
        backlog = self.store.load("backlog.json")
        if action.get("task_id"):
            task = _find(backlog.get("tasks", []), action["task_id"], "task")
            response_status = 200
            duplicate = True
        else:
            task_id = _next_id(backlog.setdefault("tasks", []), "T", 4)
            workspace_id = self.store.load("workspace.json")["id"]
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
        self.store.save_many(
            {
                "backlog.json": backlog,
                "captures.json": captures_data,
                "activity.json": activity,
            },
            operation_id="capture-convert-{}".format(idempotency_key),
        )
        return {"status": response_status, "body": body}

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        backlog = self.store.load("backlog.json")
        task = _find(backlog.get("tasks", []), request["task_id"], "task")
        captures_data = self.store.load("captures.json")
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
        replies_data = self.store.load("replies.json")
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
        self.store.save_many(
            {"replies.json": replies_data, "activity.json": activity},
            operation_id="reply-approve-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @staticmethod
    def _validate_reply_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        required = {
            "schema_version",
            "reply_id",
            "provider",
            "outcome",
            "occurred_at",
            "body_digest",
            "target_digest",
        }
        optional = {"remote_message_ref", "web_url", "error_code"}
        unknown = sorted(set(receipt) - required - optional)
        missing = sorted(required - set(receipt))
        if unknown or missing:
            raise DomainError(
                "reply receipt has unknown or missing fields",
                {"missing": missing, "unknown": unknown},
            )
        if receipt["schema_version"] != "1.0":
            raise DomainError("schema_version must be 1.0", {"field": "schema_version"})
        reply_id = _opaque_reference(receipt["reply_id"], "reply_id", 64)
        provider = receipt["provider"]
        if not isinstance(provider, str) or provider not in REPLY_CAPABILITIES:
            raise DomainError("provider is not supported", {"field": "provider"})
        outcome = receipt["outcome"]
        if outcome not in REPLY_OUTCOMES:
            raise DomainError(
                "outcome must be sent, failed, or unknown", {"field": "outcome"}
            )
        occurred_at = receipt["occurred_at"]
        if not isinstance(occurred_at, str):
            raise DomainError("occurred_at must be a string", {"field": "occurred_at"})
        try:
            parse_rfc3339(occurred_at, "occurred_at")
        except ValueError as error:
            raise DomainError(
                "occurred_at must be strict RFC3339", {"field": "occurred_at"}
            ) from error

        digests: dict[str, str] = {}
        for field in ("body_digest", "target_digest"):
            value = receipt[field]
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise DomainError(
                    "{} must be canonical SHA-256".format(field), {"field": field}
                )
            digests[field] = value

        projected: dict[str, Any] = {
            "schema_version": "1.0",
            "reply_id": reply_id,
            "provider": provider,
            "outcome": outcome,
            "occurred_at": occurred_at,
            **digests,
        }
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
        return projected

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
        activity = self.store.load("activity.json")
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        receipt = self._validate_reply_receipt(receipt_input)
        replies_data = self.store.load("replies.json")
        reply = _find(replies_data.get("replies", []), reply_id, "reply")
        mismatched: list[str] = []
        if receipt["reply_id"] != reply["id"]:
            mismatched.append("reply_id")
        if receipt["provider"] != reply["provider"]:
            mismatched.append("provider")
        for field in ("body_digest", "target_digest"):
            if not secrets.compare_digest(receipt[field], reply[field]):
                mismatched.append(field)
        if mismatched:
            raise ReplyReceiptConflictError(
                "reply receipt does not match the approved command",
                {"fields": mismatched},
            )

        stored_receipt = reply.get("receipt")
        duplicate = stored_receipt is not None
        if duplicate and stored_receipt != receipt:
            raise ReplyReceiptConflictError(
                "reply already has a different terminal receipt",
                {"reply_id": reply["id"], "state": reply.get("state")},
            )
        if not duplicate:
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
        self.store.save_many(
            {"replies.json": replies_data, "activity.json": activity},
            operation_id="reply-receipt-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_transactional
    def weekly_report(self, end: str | None = None, days: int = 7) -> dict[str, Any]:
        if days < 1 or days > 366:
            raise ValueError("days must be between 1 and 366")
        end_day = dt.date.fromisoformat(end) if end else dt.date.today()
        start_day = end_day - dt.timedelta(days=days - 1)
        tasks = {task["id"]: task for task in self.list_tasks(status="all")}
        objectives = {item["id"]: item for item in self.list_objectives(status="all")}
        worklog = self.store.load("worklog.json").get("days", {})
        projects: dict[str, dict[str, Any]] = {}
        for date in sorted(worklog):
            parsed = dt.date.fromisoformat(date)
            if not start_day <= parsed <= end_day:
                continue
            for entry in worklog[date].get("entries", []):
                task_id = entry.get("task_id")
                task = tasks.get(task_id, {})
                slot = projects.setdefault(
                    task_id,
                    {
                        "task_id": task_id,
                        "task": entry.get("task") or task.get("title", task_id),
                        "objective_ids": task.get("objective_ids", []),
                        "done": [],
                        "next": [],
                        "blockers": [],
                        "dates": [],
                        "duration_seconds": 0,
                    },
                )
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
        used = {oid for project in projects.values() for oid in project["objective_ids"]}
        return {
            "range": {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": days},
            "objectives": [
                {"id": oid, "objective": objectives[oid].get("objective", "")}
                for oid in sorted(used)
                if oid in objectives
            ],
            "projects": list(projects.values()),
        }

    @_transactional
    def snapshot(self) -> dict[str, Any]:
        objectives = self.list_objectives(status="all")
        tasks = self.list_tasks(status="all")
        worklog = self.store.load("worklog.json").get("days", {})
        notes = self.store.load("notes.json").get("notes", [])
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        known: set[str] = set()

        for objective in objectives:
            node = {
                "id": objective["id"],
                "kind": "objective",
                "title": objective["objective"],
                "status": objective.get("status", "active"),
                "meta": objective.get("quarter", ""),
                "quarter": objective.get("quarter", ""),
                "key_results": objective.get("key_results", []),
            }
            nodes.append(node)
            known.add(node["id"])
        for task in tasks:
            node = {
                "id": task["id"],
                "uid": task["uid"],
                "kind": "task",
                "title": task["title"],
                "status": task.get("status", "open"),
                "revision": task["revision"],
                "meta": "{} · {}".format(task.get("priority", "P2"), task.get("due") or "no due date"),
                "detail": task.get("detail", ""),
                "tags": task.get("tags", []),
                "priority": task.get("priority", "P2"),
                "due": task.get("due"),
                "objective_ids": task.get("objective_ids", []),
                "parent_id": task.get("parent_id"),
                "dependencies": task.get("dependencies", []),
                "subtasks": task.get("subtasks", []),
            }
            nodes.append(node)
            known.add(node["id"])
            if task.get("parent_id"):
                edges.append({"source": task["id"], "target": task["parent_id"], "kind": "parent"})
            for dependency in task.get("dependencies", []):
                edges.append({"source": task["id"], "target": dependency, "kind": "dependency"})
            for objective_id in task.get("objective_ids", []):
                edges.append({"source": task["id"], "target": objective_id, "kind": "objective"})
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
                edges.append({"source": subtask_id, "target": task["id"], "kind": "parent"})
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
                    edges.append({"source": day_id, "target": entry["task_id"], "kind": "worklog"})
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
