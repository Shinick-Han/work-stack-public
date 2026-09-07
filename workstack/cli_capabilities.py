"""Frozen CLI capability registry: parser leaves plus subtask operations.

Import is deterministic product data. It does not construct a Store, open a
network connection, read the environment or consult a clock. Traversal of an
already-built argparse tree is a pure walk; it is not a second parser.
"""

from __future__ import annotations

import argparse
import dataclasses


class CapabilityRegistryError(ValueError):
    """Fail-closed registry or parser-walk refusal."""


AGENT_APPLY_KEY = "agent.apply"
AGENT_ENVELOPE_KEYS = frozenset({"agent.status", "agent.context", "agent.checkpoint"})
CHECKPOINT_STATE_KEY = "worklog.checkpoint-state"

SKILL_ADVERTISED_KEYS = frozenset({
    "backlog.add",
    "backlog.list",
    "backlog.show",
    "backlog.start",
    "backlog.done",
    "backlog.note",
    "backlog.subtask.add",
    "okr.add-objective",
    "okr.add-key-result",
    "okr.list",
    "okr.progress",
    "okr.rollup",
    "worklog.checkin",
    "worklog.add",
    "worklog.list",
    "weekly",
    "snapshot.preview",
    "note",
    "agent.status",
    "agent.context",
    "agent.checkpoint",
    "agent.apply",
    "graph.export",
    "graph.serve",
})

ENTITY_KINDS = frozenset({
    "task",
    "subtask",
    "objective",
    "key_result",
    "worklog",
    "checkpoint",
    "weekly",
    "note",
    "capture",
    "workspace",
    "snapshot",
    "storage",
    "maintenance",
    "graph",
})

ONLINE_ROUTES = frozenset({
    "owner_post:/api/v1/cli/backlog/add",
    "owner_patch:/api/v1/tasks/{id}",
    "owner_post:/api/v1/tasks/{id}/notes",
    "owner_post:/api/v1/tasks/{id}/subtasks",
    "owner_patch:/api/v1/tasks/{id}/subtasks/{id}",
    "owner_post:/api/v1/objectives",
    "owner_post:/api/v1/objectives/{id}/key-results",
    "owner_post:/api/v1/cli/okr/link",
    "owner_post:/api/v1/cli/okr/progress",
    "owner_post:/api/v1/cli/worklog/checkin",
    "owner_post:/api/v1/cli/worklog/add",
    "owner_required_post:/api/v1/review/checkpoints/{id}/transitions",
    "owner_post:/api/v1/notes",
    "owner_required_post:/api/v1/captures",
    "agent_apply:/api/v1/tasks/{id}",
    "agent_runtime:status",
    "agent_runtime:context",
    "agent_runtime:POST /api/v1/review/entries",
    "local_only",
    "local_file:snapshot.export",
    "local_file:graph.export",
    "path_tool:storage",
    "path_tool:migration",
    "path_tool:v4_backup",
    "path_tool:maintenance",
    "process_owner:graph.serve",
})

OFFLINE_ROUTES = frozenset({
    "stack:backlog",
    "stack:okr",
    "stack:worklog",
    "stack:weekly",
    "stack:note",
    "stack:snapshot",
    "stack:graph",
    "none",
    "agent_apply:local",
    "agent_runtime:exclusive_local",
    "path_tool:storage",
    "path_tool:migration",
    "path_tool:v4_backup",
    "path_tool:maintenance",
    "process_owner:serve",
})

EVENT_KINDS = frozenset({
    "none",
    "sync_hint",
    "agent.checkpoint.committed",
    "checkpoint.transition",
})

EXCLUSION_KINDS = frozenset({
    "http_post",
    "http_get",
    "http_patch",
    "http_delete",
    "skill",
})

_CAPABILITY_SPEC = (
    ("command_key", str),
    ("entity_kind", str),
    ("operation", str),
    ("requires_revision", bool),
    ("requires_idempotency", bool),
    ("online_route", str),
    ("offline_route", str),
    ("event_kind", str),
    ("undoable", bool),
    ("approval_required", bool),
)

_EXCLUSION_SPEC = (
    ("surface", str),
    ("kind", str),
    ("justification", str),
)


@dataclasses.dataclass(frozen=True)
class CliCapability:
    command_key: str
    entity_kind: str
    operation: str
    requires_revision: bool
    requires_idempotency: bool
    online_route: str
    offline_route: str
    event_kind: str
    undoable: bool
    approval_required: bool


@dataclasses.dataclass(frozen=True)
class CliExclusion:
    surface: str
    kind: str
    justification: str


def _typed_fields(row: object, spec: tuple[tuple[str, type], ...]) -> dict[str, object]:
    if type(row) is not tuple or len(row) != len(spec):
        raise CapabilityRegistryError("row is incomplete")
    values: dict[str, object] = {}
    for (name, expected), value in zip(spec, row):
        if type(value) is not expected:
            raise CapabilityRegistryError("field has the wrong type")
        if expected is str and value == "":
            raise CapabilityRegistryError("field is empty")
        values[name] = value
    return values


def _require_unique(values: tuple[str, ...], message: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise CapabilityRegistryError(message)
        seen.add(value)


def _capability_from_row(row: object) -> CliCapability:
    values = _typed_fields(row, _CAPABILITY_SPEC)
    if values["entity_kind"] not in ENTITY_KINDS:
        raise CapabilityRegistryError("unknown entity kind")
    if values["online_route"] not in ONLINE_ROUTES:
        raise CapabilityRegistryError("unknown online route")
    if values["offline_route"] not in OFFLINE_ROUTES:
        raise CapabilityRegistryError("unknown offline route")
    if values["event_kind"] not in EVENT_KINDS:
        raise CapabilityRegistryError("unknown event kind")
    return CliCapability(**values)


def _exclusion_from_row(row: object) -> CliExclusion:
    values = _typed_fields(row, _EXCLUSION_SPEC)
    if values["kind"] not in EXCLUSION_KINDS:
        raise CapabilityRegistryError("unknown exclusion kind")
    return CliExclusion(**values)


def compile_cli_registry(
    rows: tuple[object, ...],
    exclusions: tuple[object, ...],
) -> tuple[tuple[CliCapability, ...], tuple[CliExclusion, ...]]:
    capabilities = tuple(_capability_from_row(row) for row in rows)
    if not capabilities:
        raise CapabilityRegistryError("capability registry is empty")
    _require_unique(
        tuple(item.command_key for item in capabilities),
        "duplicate command key",
    )
    compiled = tuple(_exclusion_from_row(row) for row in exclusions)
    if not compiled:
        raise CapabilityRegistryError("exclusion list is empty")
    _require_unique(
        tuple(item.kind + "\0" + item.surface for item in compiled),
        "duplicate exclusion",
    )
    _require_exclusion_partition(capabilities, compiled)
    return capabilities, compiled


def _require_exclusion_partition(
    capabilities: tuple[CliCapability, ...],
    exclusions: tuple[CliExclusion, ...],
) -> None:
    keys = frozenset(item.command_key for item in capabilities)
    skill = frozenset(item.surface for item in exclusions if item.kind == "skill")
    http = frozenset(item.surface for item in exclusions if item.kind.startswith("http"))
    if skill - keys:
        raise CapabilityRegistryError("skill exclusion is not a command key")
    if keys - skill != SKILL_ADVERTISED_KEYS:
        raise CapabilityRegistryError("skill advertised set does not partition the registry")
    if http & keys:
        raise CapabilityRegistryError("http exclusion collides with a command key")


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    found = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    if len(found) > 1:
        raise CapabilityRegistryError("parser has multiple subparser groups")
    if not found:
        return None
    return found[0]


def _operation_choices(parser: argparse.ArgumentParser) -> tuple[str, ...] | None:
    found = [action for action in parser._actions if action.dest == "operation"]
    if len(found) > 1:
        raise CapabilityRegistryError("parser has multiple operation fields")
    if not found:
        return None
    choices = found[0].choices
    if not choices:
        raise CapabilityRegistryError("operation field has no choices")
    return tuple(str(choice) for choice in choices)


def _walk_parser(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> list[str]:
    sub = _subparsers_action(parser)
    operations = _operation_choices(parser)
    if sub is not None and operations is not None:
        raise CapabilityRegistryError("parser mixes subcommands with operation choices")
    if sub is not None:
        keys: list[str] = []
        for name, child in sub.choices.items():
            keys.extend(_walk_parser(child, prefix + (str(name),)))
        if not keys:
            raise CapabilityRegistryError("subparser group is empty")
        return keys
    if not prefix:
        raise CapabilityRegistryError("parser leaf is missing a command path")
    if operations is not None:
        return [".".join(prefix + (choice,)) for choice in operations]
    return [".".join(prefix)]


def parser_command_keys(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    """Independent walk of an existing argparse tree, including subtask operations."""

    return tuple(_walk_parser(parser, ()))


def registry_command_keys() -> tuple[str, ...]:
    return tuple(item.command_key for item in CAPABILITIES)


FAMILY_ADMITTED = "admitted"
FAMILY_AGENT_APPLY = "agent_apply"
FAMILY_AGENT_ENVELOPE = "agent_envelope"
FAMILY_OWNER_REQUIRED = "owner_required"
FAMILY_PATH_TOOL = "path_tool"
FAMILY_PROCESS_OWNER = "process_owner"


# (domain, action) pairs whose registry key carries a third parser word, mapped
# to the namespace field that word is parsed into.
_NESTED_COMMAND_FIELDS = {
    ("backlog", "subtask"): "operation",
    ("storage", "migration"): "migration_action",
    ("storage", "v4-backup"): "v4_backup_action",
}


def _command_word(namespace: argparse.Namespace, field: str) -> str:
    """Read one non-empty parser word from a namespace, failing closed."""

    value = getattr(namespace, field, None)
    if type(value) is not str or not value:
        raise CapabilityRegistryError("uncovered command")
    return value


def command_key_from_parsed(namespace: argparse.Namespace) -> str:
    """Map a parsed argparse namespace onto one registry command key."""

    domain = _command_word(namespace, "domain")
    if getattr(namespace, "action", None) is None:
        return domain
    action = _command_word(namespace, "action")
    nested_field = _NESTED_COMMAND_FIELDS.get((domain, action))
    if nested_field is None:
        return domain + "." + action
    return domain + "." + action + "." + _command_word(namespace, nested_field)


def require_capability(command_key: str) -> CliCapability:
    if type(command_key) is not str or not command_key:
        raise CapabilityRegistryError("uncovered command")
    try:
        return CAPABILITY_BY_KEY[command_key]
    except KeyError:
        raise CapabilityRegistryError("uncovered command") from None


def command_family(capability: CliCapability) -> str:
    """Partition parser keys into admission families without aliasing them."""

    key = capability.command_key
    if key in AGENT_ENVELOPE_KEYS:
        return FAMILY_AGENT_ENVELOPE
    if key == AGENT_APPLY_KEY:
        return FAMILY_AGENT_APPLY
    online = capability.online_route
    if online.startswith("path_tool:"):
        return FAMILY_PATH_TOOL
    if online.startswith("process_owner:"):
        return FAMILY_PROCESS_OWNER
    if online.startswith("owner_required_post:"):
        return FAMILY_OWNER_REQUIRED
    return FAMILY_ADMITTED


# command_key, entity_kind, operation, requires_revision, requires_idempotency,
# online_route, offline_route, event_kind, undoable, approval_required
CAPABILITY_ROWS: tuple[tuple[object, ...], ...] = (
    ("backlog.add", "task", "add", False, False, "owner_post:/api/v1/cli/backlog/add", "stack:backlog", "sync_hint", False, False),
    ("backlog.list", "task", "list", False, False, "local_only", "stack:backlog", "none", False, False),
    ("backlog.show", "task", "show", False, False, "local_only", "stack:backlog", "none", False, False),
    ("backlog.start", "task", "start", True, False, "owner_patch:/api/v1/tasks/{id}", "stack:backlog", "sync_hint", True, False),
    ("backlog.done", "task", "done", True, False, "owner_patch:/api/v1/tasks/{id}", "stack:backlog", "sync_hint", True, False),
    ("backlog.drop", "task", "drop", True, False, "owner_patch:/api/v1/tasks/{id}", "stack:backlog", "sync_hint", True, False),
    ("backlog.reopen", "task", "reopen", True, False, "owner_patch:/api/v1/tasks/{id}", "stack:backlog", "sync_hint", True, False),
    ("backlog.note", "task", "note", True, True, "owner_post:/api/v1/tasks/{id}/notes", "stack:backlog", "sync_hint", False, False),
    ("backlog.subtask.add", "subtask", "add", True, True, "owner_post:/api/v1/tasks/{id}/subtasks", "stack:backlog", "sync_hint", False, False),
    ("backlog.subtask.start", "subtask", "start", True, False, "owner_patch:/api/v1/tasks/{id}/subtasks/{id}", "stack:backlog", "sync_hint", False, False),
    ("backlog.subtask.done", "subtask", "done", True, False, "owner_patch:/api/v1/tasks/{id}/subtasks/{id}", "stack:backlog", "sync_hint", False, False),
    ("backlog.subtask.drop", "subtask", "drop", True, False, "owner_patch:/api/v1/tasks/{id}/subtasks/{id}", "stack:backlog", "sync_hint", False, False),
    ("backlog.subtask.reopen", "subtask", "reopen", True, False, "owner_patch:/api/v1/tasks/{id}/subtasks/{id}", "stack:backlog", "sync_hint", False, False),
    ("okr.add-objective", "objective", "add-objective", False, True, "owner_post:/api/v1/objectives", "stack:okr", "sync_hint", False, False),
    ("okr.add-key-result", "key_result", "add-key-result", True, True, "owner_post:/api/v1/objectives/{id}/key-results", "stack:okr", "sync_hint", False, False),
    ("okr.list", "objective", "list", False, False, "local_only", "stack:okr", "none", False, False),
    ("okr.link", "objective", "link", False, False, "owner_post:/api/v1/cli/okr/link", "stack:okr", "sync_hint", False, False),
    ("okr.progress", "objective", "progress", False, False, "owner_post:/api/v1/cli/okr/progress", "stack:okr", "sync_hint", False, False),
    ("okr.rollup", "objective", "rollup", False, False, "local_only", "stack:okr", "none", False, False),
    ("worklog.checkin", "worklog", "checkin", False, False, "owner_post:/api/v1/cli/worklog/checkin", "stack:worklog", "sync_hint", False, False),
    ("worklog.add", "worklog", "add", False, False, "owner_post:/api/v1/cli/worklog/add", "stack:worklog", "sync_hint", False, False),
    ("worklog.checkpoint-state", "checkpoint", "checkpoint-state", True, True, "owner_required_post:/api/v1/review/checkpoints/{id}/transitions", "none", "checkpoint.transition", False, False),
    ("worklog.list", "worklog", "list", False, False, "local_only", "stack:worklog", "none", False, False),
    ("weekly", "weekly", "weekly", False, False, "local_only", "stack:weekly", "none", False, False),
    ("note", "note", "note", False, True, "owner_post:/api/v1/notes", "stack:note", "sync_hint", False, False),
    ("capture.ingest", "capture", "ingest", False, True, "owner_required_post:/api/v1/captures", "none", "sync_hint", False, False),
    ("agent.apply", "task", "apply", True, False, "agent_apply:/api/v1/tasks/{id}", "agent_apply:local", "sync_hint", False, False),
    ("agent.status", "workspace", "status", False, False, "agent_runtime:status", "agent_runtime:exclusive_local", "none", False, False),
    ("agent.context", "task", "context", False, False, "agent_runtime:context", "agent_runtime:exclusive_local", "none", False, False),
    ("agent.checkpoint", "checkpoint", "checkpoint", False, True, "agent_runtime:POST /api/v1/review/entries", "agent_runtime:exclusive_local", "agent.checkpoint.committed", False, False),
    ("snapshot.preview", "snapshot", "preview", False, False, "local_only", "stack:snapshot", "none", False, False),
    ("snapshot.export", "snapshot", "export", True, False, "local_file:snapshot.export", "stack:snapshot", "none", False, True),
    ("storage.validate", "storage", "validate", False, False, "path_tool:storage", "path_tool:storage", "none", False, False),
    ("storage.migration.plan", "storage", "plan", False, False, "path_tool:migration", "path_tool:migration", "none", False, False),
    ("storage.migration.preview", "storage", "preview", False, False, "path_tool:migration", "path_tool:migration", "none", False, False),
    ("storage.migration.execute", "storage", "execute", False, False, "path_tool:migration", "path_tool:migration", "none", False, True),
    ("storage.migration.verify", "storage", "verify", False, False, "path_tool:migration", "path_tool:migration", "none", False, False),
    ("storage.migration.receipt", "storage", "receipt", False, False, "path_tool:migration", "path_tool:migration", "none", False, False),
    ("storage.migration.resume", "storage", "resume", False, False, "path_tool:migration", "path_tool:migration", "none", False, True),
    ("storage.v4-backup.create", "storage", "create", False, False, "path_tool:v4_backup", "path_tool:v4_backup", "none", False, False),
    ("storage.v4-backup.verify", "storage", "verify", False, False, "path_tool:v4_backup", "path_tool:v4_backup", "none", False, False),
    ("storage.v4-backup.restore", "storage", "restore", False, False, "path_tool:v4_backup", "path_tool:v4_backup", "none", False, True),
    ("maintenance.backup", "maintenance", "backup", False, False, "path_tool:maintenance", "path_tool:maintenance", "none", False, False),
    ("maintenance.verify", "maintenance", "verify", False, False, "path_tool:maintenance", "path_tool:maintenance", "none", False, False),
    ("maintenance.restore", "maintenance", "restore", False, False, "path_tool:maintenance", "path_tool:maintenance", "none", False, True),
    ("maintenance.relocate", "maintenance", "relocate", False, False, "path_tool:maintenance", "path_tool:maintenance", "none", False, False),
    ("maintenance.initialize", "maintenance", "initialize", False, False, "path_tool:maintenance", "path_tool:maintenance", "none", False, False),
    ("graph.export", "graph", "export", False, False, "local_file:graph.export", "stack:graph", "none", False, False),
    ("graph.serve", "graph", "serve", False, False, "process_owner:graph.serve", "process_owner:serve", "none", False, False),
)

EXCLUSION_ROWS: tuple[tuple[object, ...], ...] = (
    ("POST /api/v1/sync/adopt", "http_post", "GUI/API workspace adopt; no argparse leaf"),
    ("POST /api/v1/sync/rebind-workspace", "http_post", "GUI/API workspace rebind; no argparse leaf"),
    ("POST /api/v1/tasks", "http_post", "GUI task create; CLI uses POST /api/v1/cli/backlog/add"),
    ("POST /api/v1/work-sessions", "http_post", "GUI work-session create; no argparse leaf"),
    ("POST /api/v1/work-sessions/{id}/{action}", "http_post", "GUI work-session lifecycle; no argparse leaf"),
    ("POST /api/v1/maintenance/backup", "http_post", "owner HTTP backup; CLI maintenance.backup is a path tool"),
    ("POST /api/v1/tasks/{id}/snapshot/export", "http_post", "owner HTTP snapshot export; CLI writes a local file"),
    ("POST /api/v1/tasks/{id}/deletion-preview", "http_post", "GUI permanent-deletion preview; no argparse leaf"),
    ("POST /api/v1/review/checkin", "http_post", "GUI review checkin; CLI uses POST /api/v1/cli/worklog/checkin"),
    ("POST /api/v1/captures/{id}/link", "http_post", "GUI capture link; no argparse leaf"),
    ("POST /api/v1/captures/{id}/actions/{id}/task", "http_post", "GUI capture action conversion; no argparse leaf"),
    ("POST /api/v1/captures/{id}/task", "http_post", "GUI capture-to-task conversion; no argparse leaf"),
    ("POST /api/v1/captures/{id}/dismiss", "http_post", "GUI capture dismiss; no argparse leaf"),
    ("POST /api/v1/replies", "http_post", "GUI reply create; no argparse leaf"),
    ("POST /api/v1/replies/{id}/receipt", "http_post", "GUI reply receipt; no argparse leaf"),
    ("GET /api/v1/session", "http_get", "CLI reads do not forward owner GET; session is agent-runtime internal"),
    ("GET /api/v1/health", "http_get", "CLI has no health command"),
    ("GET /api/v1/sync/status", "http_get", "CLI has no sync-status command"),
    ("GET /api/v1/sync/rebind-preview", "http_get", "CLI has no rebind-preview command"),
    ("GET /api/v1/sync/events", "http_get", "CLI has no sync-events command"),
    ("GET /api/v1/events", "http_get", "process-local SSE hint bus; no CLI tail command"),
    ("GET /api/v1/storage", "http_get", "live-owner identity; CLI storage.validate inspects a path"),
    ("GET /api/v1/workspace", "http_get", "CLI backlog/okr list remains exclusive-local"),
    ("GET /api/v1/search", "http_get", "CLI has no search command"),
    ("GET /api/v1/review/checkpoints", "http_get", "checkpoint audit GET; CLI has no audit list command"),
    ("GET /api/v1/review", "http_get", "CLI weekly/worklog list remains exclusive-local"),
    ("GET /api/v1/reports/daily-preview", "http_get", "GUI report preview; no registered CLI equivalent"),
    ("GET /api/v1/reports/weekly-preview", "http_get", "GUI weekly report preview; no registered CLI equivalent"),
    ("GET /api/v1/work-sessions", "http_get", "CLI has no work-session list command"),
    ("GET /api/v1/objectives/{id}", "http_get", "CLI okr list/rollup remains exclusive-local"),
    ("GET /api/v1/tasks/{id}/snapshot", "http_get", "CLI snapshot.preview remains exclusive-local"),
    ("GET /api/v1/tasks/{id}", "http_get", "CLI backlog.show remains exclusive-local"),
    ("GET /api/v1/captures", "http_get", "CLI has no capture list command"),
    ("PATCH /api/v1/objectives/{id}", "http_patch", "GUI objective PATCH; no argparse leaf"),
    ("PATCH /api/v1/objectives/{id}/key-results/{id}", "http_patch", "GUI key-result PATCH; CLI uses okr.progress POST"),
    ("DELETE /api/v1/tasks/{id}", "http_delete", "GUI permanent deletion; drop is a status change, not this route"),
    ("backlog.drop", "skill", "parser status mutation; omitted from root Skill examples"),
    ("backlog.reopen", "skill", "parser status mutation; omitted from root Skill examples"),
    ("backlog.subtask.start", "skill", "parser subtask status; root Skill advertises only subtask add"),
    ("backlog.subtask.done", "skill", "parser subtask status; root Skill advertises only subtask add"),
    ("backlog.subtask.drop", "skill", "parser subtask status; root Skill advertises only subtask add"),
    ("backlog.subtask.reopen", "skill", "parser subtask status; root Skill advertises only subtask add"),
    ("okr.link", "skill", "parser mutation; omitted from root Skill examples"),
    ("worklog.checkpoint-state", "skill", "owner-only transition CLI; distinct from agent checkpoint and not in Skills"),
    ("capture.ingest", "skill", "capture ingest is parser-complete but not Skill-advertised"),
    ("snapshot.export", "skill", "local disclosure export; omitted from Skills"),
    ("storage.validate", "skill", "path tool; agents must not treat storage inspect as a Skill command"),
    ("storage.migration.plan", "skill", "agent Skill forbids migrate; parser path tool remains registered"),
    ("storage.migration.preview", "skill", "agent Skill forbids migrate; parser path tool remains registered"),
    ("storage.migration.execute", "skill", "agent Skill forbids migrate; destructive path tool remains registered"),
    ("storage.migration.verify", "skill", "agent Skill forbids migrate; parser path tool remains registered"),
    ("storage.migration.receipt", "skill", "agent Skill forbids migrate; parser path tool remains registered"),
    ("storage.migration.resume", "skill", "agent Skill forbids migrate; destructive path tool remains registered"),
    ("storage.v4-backup.create", "skill", "inactive v4 authority tool; omitted from Skills"),
    ("storage.v4-backup.verify", "skill", "inactive v4 authority tool; omitted from Skills"),
    ("storage.v4-backup.restore", "skill", "agent Skill forbids restore; destructive path tool remains registered"),
    ("maintenance.backup", "skill", "local maintenance tool; omitted from Skills"),
    ("maintenance.verify", "skill", "local maintenance tool; omitted from Skills"),
    ("maintenance.restore", "skill", "agent Skill forbids restore; destructive path tool remains registered"),
    ("maintenance.relocate", "skill", "local maintenance tool; omitted from Skills"),
    ("maintenance.initialize", "skill", "local maintenance tool; omitted from Skills"),
)

CAPABILITIES, EXCLUSIONS = compile_cli_registry(CAPABILITY_ROWS, EXCLUSION_ROWS)
CAPABILITY_BY_KEY = {item.command_key: item for item in CAPABILITIES}
