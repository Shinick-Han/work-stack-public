"""Frozen value and serialization contract for the P0 agent CLI."""

import dataclasses
import datetime
import json
import pathlib
import re
from typing import Protocol


__all__ = [
    "AgentBackend",
    "AgentOutcome",
    "AuthorityAdmission",
    "CHECKPOINT_COMMAND",
    "CONTEXT_COMMAND",
    "CheckpointRequest",
    "ContextRequest",
    "JsonRequester",
    "RuntimeDependencies",
    "STATUS_COMMAND",
    "ServerCoordinates",
    "StatusRequest",
    "StoreFactory",
    "contract_fixture_bytes",
    "parse_checkpoint_packet",
    "render_outcome",
]


STATUS_COMMAND = "status"
CONTEXT_COMMAND = "context"
CHECKPOINT_COMMAND = "checkpoint"

_CONTRACT = "workstack.cli.v1"
_CHECKPOINT_INPUT_MAX_BYTES = 32768
_ENVELOPE_MAX_BYTES = 32768
_LIST_MAX_ITEMS = 20
_ITEM_MAX_CHARACTERS = 1000
_TASK_ID_PATTERN = re.compile(r"T-[0-9]{4,}")
_INTENT_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WORKSPACE_UID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_COMMANDS = {"agent.status", "agent.context", "agent.checkpoint"}
_TRANSPORTS = {"running-server", "exclusive-local"}
_SAFE_ERROR_MESSAGES = {
    "capability_not_enabled": "the selected storage format is v4 or another unsupported capability",
    "commit_unknown": "the mutation outcome is unverifiable after the bounded identical replay",
    "context_too_large": "the Task core projection alone exceeds the envelope bound",
    "internal_error": "unexpected exception; envelope is content-free",
    "invalid_authority": "the resolved authority does not exist, is unrecognizable or cannot be inspected",
    "invalid_body": "the checkpoint packet violates exact-field, bound, format or non-empty rules",
    "owner_unavailable": "server ownership metadata exists but the running server is unavailable; never falls back locally",
    "workspace_mismatch": "the expected workspace UID does not match the actual or server identity",
}
_STATUS_DATA_FIELDS = {
    "actual_workspace_uid",
    "capability_reason",
    "capability_supported",
    "contract",
    "data_dir_available",
    "exclusive_local_available",
    "expected_workspace_uid",
    "ready",
    "running_server_available",
    "storage_format",
}
_CONTEXT_DATA_FIELDS = {"omitted", "recent_worklog", "task", "workspace_uid"}
_TASK_DATA_FIELDS = {
    "detail",
    "due",
    "id",
    "priority",
    "revision",
    "status",
    "title",
    "uid",
}
_WORKLOG_DATA_FIELDS = {"blockers", "date", "done", "next"}
_CHECKPOINT_DATA_FIELDS = {"blockers", "date", "done", "next", "task", "task_id"}
_OMITTED_CATEGORIES = {
    "attachments",
    "captures",
    "objectives",
    "relationships",
    "work_sessions",
}
_OVERFLOW_MARKER = "recent_worklog_overflow"


@dataclasses.dataclass(frozen=True, kw_only=True)
class AuthorityAdmission:
    data_dir: pathlib.Path
    workspace_uid: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class ServerCoordinates:
    host: str
    port: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class StatusRequest:
    data_dir: pathlib.Path
    expected_workspace_uid: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class ContextRequest:
    task_id: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class CheckpointRequest:
    task_id: str
    date: str
    done: list[str]
    next: list[str]
    blockers: list[str]
    intent_id: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class AgentOutcome:
    command: str
    commit_state: str | None
    data: dict[str, object] | None
    error_code: str | None
    error_details: dict[str, object]
    error_message: str | None
    intent_id: str | None
    replayed: bool | None
    retryable: bool | None
    task_id: str | None
    transport: str | None
    workspace_uid: str | None


class AgentBackend(Protocol):
    def checkpoint(self, *, request: CheckpointRequest) -> dict[str, object]: ...

    def context(
        self, *, request: ContextRequest, today: datetime.date
    ) -> dict[str, object]: ...

    def status(self, *, request: StatusRequest) -> dict[str, object]: ...


class JsonRequester(Protocol):
    def request(
        self,
        *,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
    ) -> tuple[int, dict[str, object]]: ...


class StoreFactory(Protocol):
    def __call__(self, *, root: pathlib.Path) -> "workstack.store.Store": ...


@dataclasses.dataclass(frozen=True, kw_only=True)
class RuntimeDependencies:
    admit_authority: "callable(*, data_dir: pathlib.Path, expected_workspace_uid: str) -> AuthorityAdmission"
    create_local_backend: "callable(*, admission: AuthorityAdmission, store_factory: StoreFactory) -> AgentBackend"
    create_running_server_backend: "callable(*, server_info_path: pathlib.Path, expected_workspace_uid: str, request_json: JsonRequester) -> AgentBackend"
    request_json: JsonRequester
    store_factory: StoreFactory
    today: "callable() -> datetime.date"


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _checkpoint_items(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > _LIST_MAX_ITEMS:
        raise ValueError("invalid checkpoint list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("invalid checkpoint item")
        item = item.strip()
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("checkpoint item is not valid Unicode") from error
        if len(item) > _ITEM_MAX_CHARACTERS:
            raise ValueError("checkpoint item is too large")
        if item:
            normalized.append(item)
    return normalized


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate checkpoint field")
        value[key] = item
    return value


def parse_checkpoint_packet(*, raw: bytes, intent_id: str) -> CheckpointRequest:
    """Parse and normalize one exact-field checkpoint packet."""

    if not isinstance(raw, bytes) or len(raw) > _CHECKPOINT_INPUT_MAX_BYTES:
        raise ValueError("invalid checkpoint packet bytes")
    if not isinstance(intent_id, str) or _INTENT_ID_PATTERN.fullmatch(intent_id) is None:
        raise ValueError("invalid intent_id")
    try:
        decoded = raw.decode("utf-8")
        packet = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ValueError("invalid checkpoint JSON") from error
    fields = {"blockers", "date", "done", "next", "task_id"}
    if not isinstance(packet, dict) or set(packet) != fields:
        raise ValueError("invalid checkpoint fields")
    task_id = packet["task_id"]
    if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("invalid task_id")
    date = packet["date"]
    if not isinstance(date, str) or _DATE_PATTERN.fullmatch(date) is None:
        raise ValueError("invalid checkpoint date")
    try:
        if datetime.date.fromisoformat(date).isoformat() != date:
            raise ValueError("non-canonical checkpoint date")
    except ValueError as error:
        raise ValueError("invalid checkpoint date") from error
    done = _checkpoint_items(packet["done"])
    next_items = _checkpoint_items(packet["next"])
    blockers = _checkpoint_items(packet["blockers"])
    if not any((done, next_items, blockers)):
        raise ValueError("checkpoint requires at least one non-empty item")
    return CheckpointRequest(
        task_id=task_id,
        date=date,
        done=done,
        next=next_items,
        blockers=blockers,
        intent_id=intent_id,
    )


def _outcome_meta(outcome: AgentOutcome) -> dict[str, object]:
    meta: dict[str, object] = {"command": outcome.command}
    for name in (
        "commit_state",
        "intent_id",
        "replayed",
        "task_id",
        "transport",
        "workspace_uid",
    ):
        value = getattr(outcome, name)
        if value is not None:
            meta[name] = value
    return meta


def _is_workspace_uid(value: object) -> bool:
    return type(value) is str and _WORKSPACE_UID_PATTERN.fullmatch(value) is not None


def _is_task_id(value: object) -> bool:
    return type(value) is str and _TASK_ID_PATTERN.fullmatch(value) is not None


def _is_intent_id(value: object) -> bool:
    return type(value) is str and _INTENT_ID_PATTERN.fullmatch(value) is not None


def _is_date(value: object) -> bool:
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        return False
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _exact_dict(
    value: object, fields: set[str], label: str
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid {} shape".format(label))
    return value


def _validate_item_list(value: object, *, label: str) -> list[str]:
    if type(value) is not list or len(value) > _LIST_MAX_ITEMS:
        raise ValueError("invalid {} list".format(label))
    for item in value:
        if type(item) is not str or not item or item != item.strip():
            raise ValueError("invalid {} item".format(label))
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("invalid {} item".format(label)) from error
        if len(item) > _ITEM_MAX_CHARACTERS:
            raise ValueError("invalid {} item".format(label))
    return value


def _validate_status_data(data: dict[str, object], workspace_uid: str) -> None:
    data = _exact_dict(data, _STATUS_DATA_FIELDS, "status data")
    actual = data["actual_workspace_uid"]
    expected = data["expected_workspace_uid"]
    if not _is_workspace_uid(actual) or not _is_workspace_uid(expected):
        raise ValueError("invalid status workspace UID")
    if actual != expected or actual != workspace_uid:
        raise ValueError("inconsistent status workspace UID")
    if data["contract"] != _CONTRACT:
        raise ValueError("invalid status contract")
    reason = data["capability_reason"]
    if reason is not None and type(reason) is not str:
        raise ValueError("invalid status capability reason")
    for field in (
        "capability_supported",
        "data_dir_available",
        "exclusive_local_available",
        "ready",
        "running_server_available",
    ):
        if type(data[field]) is not bool:
            raise ValueError("invalid status boolean")
    if type(data["storage_format"]) is not str or data["storage_format"] not in {
        "unknown",
        "v3",
        "v4",
    }:
        raise ValueError("invalid status storage format")


def _validate_task_data(value: object, task_id: str) -> None:
    task = _exact_dict(value, _TASK_DATA_FIELDS, "context task")
    if task["id"] != task_id or not _is_task_id(task["id"]):
        raise ValueError("invalid context Task ID")
    if not _is_workspace_uid(task["uid"]):
        raise ValueError("invalid context Task UID")
    if type(task["revision"]) is not int or task["revision"] < 0:
        raise ValueError("invalid context Task revision")
    for field in ("detail", "priority", "status", "title"):
        if type(task[field]) is not str:
            raise ValueError("invalid context Task field")
    if task["due"] is not None and not _is_date(task["due"]):
        raise ValueError("invalid context Task due date")


def _validate_worklog_data(value: object) -> str:
    entry = _exact_dict(value, _WORKLOG_DATA_FIELDS, "recent worklog entry")
    if not _is_date(entry["date"]):
        raise ValueError("invalid recent worklog date")
    lists = [
        _validate_item_list(entry[field], label="recent worklog {}".format(field))
        for field in ("done", "next", "blockers")
    ]
    if not any(lists):
        raise ValueError("empty recent worklog entry")
    return entry["date"]  # type: ignore[return-value]


def _validate_context_data(
    data: dict[str, object], *, task_id: str, workspace_uid: str
) -> None:
    data = _exact_dict(data, _CONTEXT_DATA_FIELDS, "context data")
    if data["workspace_uid"] != workspace_uid or not _is_workspace_uid(
        data["workspace_uid"]
    ):
        raise ValueError("invalid context workspace UID")
    _validate_task_data(data["task"], task_id)
    recent = data["recent_worklog"]
    if type(recent) is not list or len(recent) > 5:
        raise ValueError("invalid recent worklog")
    dates = [_validate_worklog_data(entry) for entry in recent]
    if dates != sorted(dates, reverse=True):
        raise ValueError("recent worklog is not newest first")
    omitted = data["omitted"]
    allowed = _OMITTED_CATEGORIES | {_OVERFLOW_MARKER}
    if (
        type(omitted) is not list
        or any(type(item) is not str for item in omitted)
        or len(omitted) != len(set(omitted))
        or not _OMITTED_CATEGORIES.issubset(omitted)
        or not set(omitted).issubset(allowed)
    ):
        raise ValueError("invalid context omission markers")


def _validate_checkpoint_data(data: dict[str, object], task_id: str) -> None:
    data = _exact_dict(data, _CHECKPOINT_DATA_FIELDS, "checkpoint data")
    if data["task_id"] != task_id or not _is_task_id(data["task_id"]):
        raise ValueError("invalid checkpoint response Task ID")
    if type(data["task"]) is not str or not data["task"]:
        raise ValueError("invalid checkpoint response Task title")
    if not _is_date(data["date"]):
        raise ValueError("invalid checkpoint response date")
    lists = [
        _validate_item_list(data[field], label="checkpoint {}".format(field))
        for field in ("done", "next", "blockers")
    ]
    if not any(lists):
        raise ValueError("empty checkpoint response")


def _validate_outcome(outcome: AgentOutcome) -> bool:
    if (
        not isinstance(outcome, AgentOutcome)
        or type(outcome.command) is not str
        or outcome.command not in _COMMANDS
    ):
        raise ValueError("invalid agent outcome")
    if type(outcome.error_details) is not dict:
        raise ValueError("invalid error details")
    if outcome.retryable is not None and type(outcome.retryable) is not bool:
        raise ValueError("invalid retryable value")
    success = type(outcome.data) is dict
    failure = (
        type(outcome.error_code) is str
        and type(outcome.error_message) is str
        and outcome.data is None
    )
    if success == failure:
        raise ValueError("outcome must be exactly one of success or failure")
    if success:
        if any(
            value is not None
            for value in (outcome.error_code, outcome.error_message, outcome.retryable)
        ) or outcome.error_details:
            raise ValueError("success cannot contain error fields")
        if (
            type(outcome.transport) is not str
            or outcome.transport not in _TRANSPORTS
            or not _is_workspace_uid(outcome.workspace_uid)
        ):
            raise ValueError("success is missing transport metadata")
        if outcome.command == "agent.checkpoint":
            if (
                outcome.commit_state != "committed"
                or not _is_intent_id(outcome.intent_id)
                or type(outcome.replayed) is not bool
                or not _is_task_id(outcome.task_id)
            ):
                raise ValueError("invalid checkpoint success metadata")
            _validate_checkpoint_data(outcome.data, outcome.task_id)
        elif any(
            value is not None
            for value in (outcome.commit_state, outcome.intent_id, outcome.replayed)
        ):
            raise ValueError("non-checkpoint success has checkpoint metadata")
        elif outcome.command == "agent.context":
            if not _is_task_id(outcome.task_id):
                raise ValueError("context success is missing task_id")
            _validate_context_data(
                outcome.data,
                task_id=outcome.task_id,
                workspace_uid=outcome.workspace_uid,
            )
        elif outcome.task_id is not None:
            raise ValueError("status success has task_id")
        else:
            _validate_status_data(outcome.data, outcome.workspace_uid)
        return True
    if outcome.error_code not in _SAFE_ERROR_MESSAGES:
        raise ValueError("unknown error code")
    if outcome.error_code == "commit_unknown":
        if (
            outcome.command != "agent.checkpoint"
            or outcome.commit_state != "unknown"
            or not _is_intent_id(outcome.intent_id)
            or not _is_task_id(outcome.task_id)
            or outcome.transport != "running-server"
            or not _is_workspace_uid(outcome.workspace_uid)
            or outcome.replayed is not None
        ):
            raise ValueError("invalid commit_unknown metadata")
    elif any(
        value is not None
        for value in (
            outcome.commit_state,
            outcome.intent_id,
            outcome.replayed,
            outcome.task_id,
            outcome.transport,
            outcome.workspace_uid,
        )
    ):
        raise ValueError("ordinary failure has command-inapplicable metadata")
    if outcome.error_code == "internal_error" and outcome.retryable is not None:
        raise ValueError("internal_error cannot recommend retry")
    return False


def _success_envelope(outcome: AgentOutcome) -> dict[str, object]:
    return {
        "contract": _CONTRACT,
        "data": outcome.data,
        "meta": _outcome_meta(outcome),
    }


def _failure_envelope(outcome: AgentOutcome) -> dict[str, object]:
    error: dict[str, object] = {
        "code": outcome.error_code,
        "details": {},
        "message": _SAFE_ERROR_MESSAGES[outcome.error_code],
    }
    if outcome.retryable is not None:
        error["retryable"] = outcome.retryable
    return {
        "contract": _CONTRACT,
        "error": error,
        "meta": _outcome_meta(outcome),
    }


def render_outcome(*, outcome: AgentOutcome) -> bytes:
    """Render one validated success or failure envelope as canonical JSON."""

    success = _validate_outcome(outcome)
    envelope = _success_envelope(outcome) if success else _failure_envelope(outcome)
    try:
        rendered = _canonical_json_bytes(envelope)
    except (TypeError, ValueError) as error:
        raise ValueError("outcome is not JSON serializable") from error
    if len(rendered) > _ENVELOPE_MAX_BYTES:
        raise ValueError("rendered outcome is too large")
    return rendered


_ADMISSION = {
    "approved_store_free_seam": "B1 uses bounded direct document reads of authority documents (workspace.json identity and format marker documents); it imports neither Store nor any workstack.storage module and creates no files or directories",
    "order": [
        "require explicit --data-dir and --workspace-uid",
        "resolve the data path without creating it",
        "require an existing directory and recognizable Work Stack authority",
        "inspect format and workspace identity without importing or constructing Store",
        "refuse v4 with capability_not_enabled; refuse missing, unknown and unrecognizable authorities with invalid_authority",
        "require actual workspace UID equal to expected workspace UID before any Task content is returned or mutation sent",
        "only then construct the v3 Store or contact its declared loopback server owner",
    ],
    "uid_rule": "the workspace UID is the canonical non-nil lowercase RFC 4122 UUID read from workspace.json id; expected and actual values are compared as canonical strings",
}

_BACKEND_RESULTS = {
    "checkpoint": {
        "commit_state": "committed|unknown",
        "entry": "the committed or replayed review-entry data mapping, null when commit_state is unknown",
        "replayed": "bool",
    },
    "context": {
        "entries": "raw worklog entries for the bounded 31-day window",
        "entry_keys": ["blockers", "date", "done", "next", "task_id"],
        "task": "the raw Task detail mapping",
        "workspace_uid": "str",
    },
    "status": {
        "keys": [
            "actual_workspace_uid",
            "capability_reason",
            "capability_supported",
            "contract",
            "data_dir_available",
            "exclusive_local_available",
            "expected_workspace_uid",
            "ready",
            "running_server_available",
            "storage_format",
        ]
    },
}

_CLI_CONTRACT = {"contract_string": _CONTRACT}

_COMMAND_DECLARATION = {
    "checkpoint_input": {
        "exact_fields": ["blockers", "date", "done", "next", "task_id"],
        "rule": "the packet mirrors the existing review-entry body exactly; an external workspace_uid never enters the API body",
    },
    "legacy_apply_excluded": True,
    "meta_command_prefix": "agent.",
    "registry": "workstack.agent_commands.COMMANDS",
    "values": [STATUS_COMMAND, CONTEXT_COMMAND, CHECKPOINT_COMMAND],
}

_ENVELOPE = {
    "data_shapes": {
        "agent.checkpoint": {
            "keys": ["blockers", "date", "done", "next", "task", "task_id"],
            "source": "the existing review-entry response data returned by WorkStack.add_worklog_v1 and POST /api/v1/review/entries",
        },
        "agent.context": {
            "keys": ["omitted", "recent_worklog", "task", "workspace_uid"],
            "omitted_categories": [
                "attachments",
                "captures",
                "objectives",
                "relationships",
                "work_sessions",
            ],
            "overflow_marker": "recent_worklog_overflow",
            "overflow_marker_channel": "data.omitted",
            "recent_worklog_entry_keys": ["blockers", "date", "done", "next"],
            "source": "the CLI-v1 context data shape with golden tests",
            "task_allowlist": [
                "detail",
                "due",
                "id",
                "priority",
                "revision",
                "status",
                "title",
                "uid",
            ],
        },
        "agent.status": {
            "keys": [
                "actual_workspace_uid",
                "capability_reason",
                "capability_supported",
                "contract",
                "data_dir_available",
                "exclusive_local_available",
                "expected_workspace_uid",
                "ready",
                "running_server_available",
                "storage_format",
            ],
            "source": "the AgentBackend.status result mapping",
        },
    },
    "failure": {
        "error_optional": {"retryable": "bool"},
        "error_required": {"code": "str", "details": "object", "message": "str"},
        "forbidden": ["data"],
        "required": {"contract": "str", "error": "object", "meta": "object"},
        "variants": {
            "commit_unknown": {
                "error_code": "commit_unknown",
                "meta_required": {
                    "command": "agent.checkpoint",
                    "commit_state": "unknown",
                    "intent_id": "str",
                    "task_id": "str",
                    "transport": "running-server",
                    "workspace_uid": "str",
                },
            },
            "ordinary_command_failure": {
                "meta_forbidden": ["commit_state"],
                "meta_required": {"command": "agent.<command>"},
            },
        },
    },
    "renderer": "compact sorted-key UTF-8 JSON, exactly one object, one trailing LF",
    "rules": [
        "success and failure are mutually exclusive: success has data and no error; failure has error and no data",
        "a field that does not apply to the executed command is omitted, never filled with a placeholder",
        "commit_state=committed appears only on successful agent.checkpoint",
        "commit_state=unknown appears only on commit_unknown after a POST mutation attempt and failed identical replay",
        "final serialized envelope, not merely data, is bounded to 32 KiB",
        "paths, CSRF values, tokens and raw server bodies never enter error details",
        "successful status emits data_dir_available as a boolean and never emits the resolved absolute path",
        "retryable appears only when the CLI can give a sound retry recommendation",
    ],
    "success": {
        "forbidden": ["error"],
        "required": {"contract": "str", "data": "object", "meta": "object"},
        "variants": {
            "agent.checkpoint": {
                "meta_optional": {},
                "meta_required": {
                    "command": "agent.checkpoint",
                    "commit_state": "committed",
                    "intent_id": "str",
                    "replayed": "bool",
                    "task_id": "str",
                    "transport": "running-server|exclusive-local",
                    "workspace_uid": "str",
                },
            },
            "agent.context": {
                "meta_optional": {},
                "meta_required": {
                    "command": "agent.context",
                    "task_id": "str",
                    "transport": "running-server|exclusive-local",
                    "workspace_uid": "str",
                },
            },
            "agent.status": {
                "meta_optional": {},
                "meta_required": {
                    "command": "agent.status",
                    "transport": "running-server|exclusive-local",
                    "workspace_uid": "str",
                },
            },
        },
    },
}

_ERRORS = {
    "codes": dict(_SAFE_ERROR_MESSAGES),
    "exit_mapping": {
        "0": "success or idempotent replay",
        "1": "parsed command failure; inspect error.code",
        "2": "command-line usage or parser failure; no agent envelope",
    },
}

_LIMITS = {
    "checkpoint_input_max_bytes": _CHECKPOINT_INPUT_MAX_BYTES,
    "checkpoint_nonempty_rule": {
        "fields": ["done", "next", "blockers"],
        "individual_lists_may_be_empty": True,
        "minimum_nonempty_items_across_fields": 1,
    },
    "context_lookback_days": 31,
    "date_format": "YYYY-MM-DD strict ISO calendar date",
    "envelope_max_bytes": _ENVELOPE_MAX_BYTES,
    "http_timeout_seconds": 10,
    "intent_id_pattern": "[A-Za-z0-9._:-]{8,128}",
    "item_max_characters": _ITEM_MAX_CHARACTERS,
    "list_max_items": _LIST_MAX_ITEMS,
    "recent_worklog_max_entries": 5,
    "server_info": {
        "host_allowlist": ["127.0.0.1", "::1", "localhost"],
        "port_max": 65535,
        "port_min": 1,
        "version": 1,
    },
    "storage_format_values": ["unknown", "v3", "v4"],
    "task_id_pattern": "T-[0-9]{4,}",
    "workspace_uid": "canonical non-nil lowercase RFC 4122 UUID",
}

_TRANSPORT_RULES = {
    "automatic_retry_policy": "only one identical replay is automatic after possible POST response loss; session, storage, GET and pre-POST failures are never retried",
    "commit_unknown_precondition": "valid only after a POST may have reached the server and the identical bounded replay also cannot establish the result",
    "context_daily_review_gets": {
        "count": 31,
        "date_order": "today through today minus 30 days, newest first",
        "weekly_projection_forbidden": True,
    },
    "identical_replay": "both POST attempts reuse pre-serialized bytes and the same Idempotency-Key",
    "no_fresh_key_on_response_loss": True,
    "post_attempt_maximum": 2,
    "session_failure_omits_commit_state": True,
}


def contract_fixture_bytes() -> bytes:
    """Return the frozen M0 projection without consulting the filesystem."""

    return _canonical_json_bytes(
        {
            "admission": _ADMISSION,
            "backend_results": _BACKEND_RESULTS,
            "cli_contract": _CLI_CONTRACT,
            "commands": _COMMAND_DECLARATION,
            "envelope": _ENVELOPE,
            "errors": _ERRORS,
            "limits": _LIMITS,
            "transport_rules": _TRANSPORT_RULES,
        }
    )
