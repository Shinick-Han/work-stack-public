"""Bounded, redacted remote-update diagnostic snapshot and copyable summary.

Recovering a broken remote update meant screenshotting several screens and
retyping what they said.  This module turns whatever the desktop already knows
into one small snapshot and one copyable block of text, so an operator can
paste a single summary instead of collecting fragments by hand.

Two rules shape everything here.

The first is honesty.  Every fact is either observed or ``unknown``; there is
no defaulting to the reassuring answer.  A stop command that was merely issued
is not a process that exited, so process exit, original listener release and
lease release stay three independent observations that each carry their own
verified/failed/unknown verdict.  A backup that never ran says ``not run``,
which is not the same as a backup that failed.  Absent facts normalize to
``unknown`` rather than disappearing.

The second is redaction, enforced twice so one mistake upstream is not enough
to leak.  ``normalize_view`` copies an allowlist of field names and nothing
else, so a session token, a full argv, a host name, a filesystem path or a
Task/Context body has no field to arrive in even when the caller hands over its
whole internal state.  Then every surviving string must match the narrow
grammar of the specific field it landed in: a version starts with a digit, a
code is SCREAMING_SNAKE, an action is snake_case, and every other field is a
closed vocabulary.  Per-field grammars rather than one shared "symbolic
string" shape, because a host name and a session token are both perfectly
respectable symbolic strings, and a shared pattern would print either of them
in the version row.  Between them the grammars also exclude whitespace, a
newline, a quote, an angle bracket, a shell metacharacter and a path
separator, which is what keeps an untrusted remote-reported string from
becoming markup or a command when the summary is pasted somewhere else.  A
value that does not fit is refused outright rather than escaped: a refused
fact reads as ``Unknown``, which is true, while an escaped one would still be
attacker-chosen text sitting in an operator's report.

This is a formatter, not a telemetry system.  Nothing here reads a file, opens
a socket, spawns a process, consults the clock or touches the SSOT.  The
snapshot is an internal projection between the flow, the screen and this
report; it is deliberately not a published API, and callers project their
existing typed evidence into it through the ``project_*`` helpers below.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "remote-update-view/1"

# The flow's ordered stages, per R1 clarification 1, which resolved R5's
# ``backup/stop`` slash notation into two literals in this order: the current
# owner is stopped normally first, and the supported backup runs against a
# quiescent data directory afterwards. They stay separate stages because a
# released owner and a verified backup are separately observable, and
# collapsing them would hide which of the two a stuck update is waiting on.
FLOW_STAGES = (
    "preview",
    "stop",
    "backup",
    "prepare",
    "probe",
    "activate",
    "verify",
    "ready",
)
STAGES = ("idle",) + FLOW_STAGES + ("failed", "cancelled", "unknown")

OWNER_STATES = ("live", "stopping", "dead", "foreign", "unfenced", "unknown")
OBSERVATIONS = ("verified", "failed", "unknown")
INSTALL_CAPABILITIES = ("available", "unavailable", "unknown")
INSTALL_METHODS = ("transactional", "verified_unpack", "unknown")
BACKUP_STATUSES = ("verified", "failed", "not_run", "unknown")

VERSION_FIELDS = ("desktop", "remote", "served_ui", "protocol", "schema")
OWNER_OBSERVATION_FIELDS = ("process_exit", "listener_release", "lease_release")

UNKNOWN = "unknown"

MAX_SYMBOL_LENGTH = 64
MAX_VERSION_LENGTH = 48
MAX_ACTIONS = 8
MAX_PROTOCOL_NUMBER = 1_000_000
# The whole point is one pasteable block. The field set is fixed and every
# value is a bounded symbol, so this ceiling is a guard against a future field
# rather than a truncation policy: a report may never grow into a log dump.
MAX_REPORT_CHARACTERS = 4096

# Each field gets the narrowest grammar that still admits every legitimate
# value, rather than one permissive "symbolic string" shape shared by all of
# them. That distinction is load-bearing: `build01.corp.example.com` and a
# session token are both perfectly good symbolic strings, so a single shared
# pattern would happily print a host name in the version row. A version has to
# start with a digit, a code is SCREAMING_SNAKE, an action is snake_case, and
# none of those three grammars can express a host name, a path or a token.
_VERSION = re.compile(r"\A[0-9]+(\.[0-9]+){0,3}(\+[A-Za-z0-9.]{1,16})?\Z")
_CODE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")
_ACTION = re.compile(r"\A[a-z][a-z0-9_]*\Z")
# A sha256 hex digest is the exact shape of a session-token hash, and lowercase
# hex also satisfies the action grammar. Refusing the shape everywhere costs an
# abbreviated-commit build id nothing, because a short id still passes.
_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")
_UI_ASSET_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class DiagnosticsError(ValueError):
    """Raised only for a programming error in a caller, never for bad facts."""


def _bounded(value: object, limit: int) -> str | None:
    """Admit a non-empty string within ``limit`` that is not a digest."""

    if isinstance(value, bool) or not isinstance(value, str):
        return None
    if not value or len(value) > limit:
        return None
    if _SHA256_HEX.fullmatch(value) is not None:
        return None
    return value


def _code(value: object) -> str | None:
    """Admit one bounded symbolic error code, or refuse it as unknown."""

    text = _bounded(value, MAX_SYMBOL_LENGTH)
    if text is None or _CODE.fullmatch(text) is None:
        return None
    return text


def _action(value: object) -> str | None:
    """Admit one bounded symbolic action name, or refuse it as unknown."""

    text = _bounded(value, MAX_SYMBOL_LENGTH)
    if text is None or _ACTION.fullmatch(text) is None:
        return None
    return text


def _enum(value: object, allowed: Sequence[str], fallback: str = UNKNOWN) -> str:
    """Normalize to a member of ``allowed``; anything else is the fallback.

    Membership in a closed vocabulary is the whole check, so no untrusted text
    can reach the report through an enum field however it is shaped.
    """

    return value if isinstance(value, str) and value in allowed else fallback


def _tristate(value: object) -> bool | None:
    """Only a real bool is an answer. Absent and malformed are both unknown."""

    return value if isinstance(value, bool) else None


def _version(value: object) -> str | None:
    """Accept a bounded version-shaped string, or an in-range protocol number."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value <= MAX_PROTOCOL_NUMBER else None
    text = _bounded(value, MAX_VERSION_LENGTH)
    if text is None or _VERSION.fullmatch(text) is None:
        return None
    return text


def _version_field(field: str, value: object) -> str | None:
    """Preserve an observed UI asset identity only in its dedicated field.

    Callers must supply measured asset evidence, never a token hash or a
    product/source version substituted for a served build. A bare digest and
    arbitrary strings remain refused; other version fields keep their grammar.
    This formatter validates representation, not how the caller measured it.
    """

    if field == "served_ui" and isinstance(value, str):
        if len(value) == 71 and _UI_ASSET_DIGEST.fullmatch(value):
            return value
    return _version(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalize_actions(raw_actions: object) -> tuple[list[str], list[str]]:
    """Admit up to ``MAX_ACTIONS`` distinct action names, and name the refusals.

    Scanning is bounded as well as admission: a caller handing over a huge list
    must not be able to grow the refusal list without limit either.
    """

    if raw_actions is None:
        return [], []
    if not isinstance(raw_actions, (list, tuple)):
        return [], ["actions"]

    actions: list[str] = []
    notes: list[str] = []
    scanned = 0
    for index, entry in enumerate(raw_actions[: MAX_ACTIONS + 1]):
        if len(actions) >= MAX_ACTIONS:
            break
        scanned = index + 1
        action = _action(entry)
        if action is None:
            notes.append(f"actions.{index}")
        elif action not in actions:
            actions.append(action)
    if scanned < len(raw_actions):
        notes.append("actions.overflow")
    return actions, notes


def normalize_view_with_notes(payload: object) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Project any caller state into the snapshot, and name refused fields.

    The returned snapshot always has the full R6 shape whatever arrives, so a
    truncated or malformed payload yields an honest all-unknown report instead
    of an exception or a missing section.  Refused fields are reported by field
    name only; the refused value never leaves this function, because naming the
    field is what makes a partial report legible while printing the value is
    exactly the leak this module exists to prevent.
    """

    source = _mapping(payload)
    notes: list[str] = []

    def refuse(field: str, supplied: object, resolved: object) -> None:
        """Note a field the caller supplied that could not be admitted."""

        if supplied is None or supplied == resolved:
            return
        notes.append(field)

    raw_stage = source.get("stage")
    stage = _enum(raw_stage, STAGES)
    refuse("stage", raw_stage, stage)

    raw_code = source.get("code")
    code = _code(raw_code)
    refuse("code", raw_code, code)

    raw_versions = _mapping(source.get("versions"))
    versions: dict[str, str | None] = {}
    for field in VERSION_FIELDS:
        raw = raw_versions.get(field)
        value = _version_field(field, raw)
        versions[field] = value
        if raw is not None and value is None:
            notes.append(f"versions.{field}")

    raw_owner = _mapping(source.get("owner"))
    owner: dict[str, Any] = {
        "state": _enum(raw_owner.get("state"), OWNER_STATES),
        "token_available": _tristate(raw_owner.get("token_available")),
        "pidfd_available": _tristate(raw_owner.get("pidfd_available")),
    }
    refuse("owner.state", raw_owner.get("state"), owner["state"])
    for field in OWNER_OBSERVATION_FIELDS:
        raw = raw_owner.get(field)
        owner[field] = _enum(raw, OBSERVATIONS)
        refuse(f"owner.{field}", raw, owner[field])

    raw_install = _mapping(source.get("install"))
    install = {
        "capability": _enum(raw_install.get("capability"), INSTALL_CAPABILITIES),
        "method": _enum(raw_install.get("method"), INSTALL_METHODS),
    }
    refuse("install.capability", raw_install.get("capability"), install["capability"])
    refuse("install.method", raw_install.get("method"), install["method"])

    raw_backup = _mapping(source.get("backup"))
    backup = {
        "status": _enum(raw_backup.get("status"), BACKUP_STATUSES),
        "migration_required": _tristate(raw_backup.get("migration_required")),
    }
    refuse("backup.status", raw_backup.get("status"), backup["status"])

    actions, action_notes = _normalize_actions(source.get("actions"))
    notes.extend(action_notes)

    view = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "code": code,
        "versions": versions,
        "owner": owner,
        "install": install,
        "backup": backup,
        "actions": actions,
    }
    return view, tuple(notes)


def normalize_view(payload: object) -> dict[str, Any]:
    """The snapshot alone, for callers that do not surface refusals."""

    view, _ = normalize_view_with_notes(payload)
    return view


# --- projections from existing production evidence -------------------------
#
# These exist so coordinator glue can feed the typed values the shell already
# computes into this snapshot without changing those APIs. Each mapping is
# lossy in exactly one documented direction and never upgrades a weaker
# observation into a stronger one.

# ``remote_owner.OwnerState`` is the richer vocabulary, and R1 clarification 5
# settles the lossy direction: absent, replaced and ambiguous all map to
# "unknown" unless independent evidence supports another R6 state, which this
# projection by itself never has. "ambiguous" is the state that forbids acting,
# so it may never read as dead. "replaced" is tempting to call dead -- the
# recorded process is gone either way -- but a pid now naming something
# unrelated is exactly the case where a confident verdict is worth least, so it
# stays unknown too. "absent" loses the distinction between "no receipt exists"
# and "liveness could not be determined"; the R6 enum has no slot for it, and
# inventing one would be a second UI contract.
_OWNER_STATE_PROJECTION = {
    "live": "live",
    "stopping": "stopping",
    "dead": "dead",
    "foreign": "foreign",
    "unfenced": "unfenced",
    "replaced": UNKNOWN,
    "ambiguous": UNKNOWN,
    "absent": UNKNOWN,
}

# ``remote_startup_state.RemoteStartupState`` values, which describe one
# connection attempt rather than the whole update flow.
_STARTUP_STAGE_PROJECTION = {
    "IDLE": "idle",
    "PROBING": "probe",
    "STARTING_TUNNEL": "activate",
    "WAITING_REMOTE_READY": "activate",
    "VERIFYING_AUTHORITY": "verify",
    "READY": "ready",
    "MONITORING": "ready",
    "FAILED": "failed",
    "STOPPED": "idle",
}


def project_owner_state(classification: object) -> str:
    """Map a production owner classification into the snapshot enum."""

    if not isinstance(classification, str):
        return UNKNOWN
    return _OWNER_STATE_PROJECTION.get(classification, UNKNOWN)


def project_stage(startup_state: object) -> str:
    """Map a remote startup attempt state into the snapshot stage enum."""

    if not isinstance(startup_state, str):
        return UNKNOWN
    return _STARTUP_STAGE_PROJECTION.get(startup_state, UNKNOWN)


def project_observation(*, attempted: bool, confirmed: object) -> str:
    """Turn one release observation into verified/failed/unknown.

    ``confirmed`` is a tristate on purpose. Issuing a stop and then failing to
    observe the result is ``unknown``, never ``failed`` and never ``verified``;
    only an actual negative observation is a failure.
    """

    if not attempted:
        return UNKNOWN
    truth = _tristate(confirmed)
    if truth is None:
        return UNKNOWN
    return "verified" if truth else "failed"


# --- rendering -------------------------------------------------------------

_UNKNOWN_TEXT = "Unknown"

_STAGE_TEXT = {
    "preview": "Preview",
    "backup": "Backup",
    "stop": "Stopping the current server",
    "prepare": "Prepare",
    "probe": "Probe",
    "activate": "Activate",
    "verify": "Verify",
    "ready": "Ready",
    "idle": "Idle",
    "failed": "Failed",
    "cancelled": "Cancelled",
    UNKNOWN: _UNKNOWN_TEXT,
}

_OWNER_STATE_TEXT = {
    "live": "Live",
    "stopping": "Stopping",
    "dead": "Dead",
    "foreign": "Owned by another host",
    "unfenced": "Legacy receipt without host or boot identity",
    UNKNOWN: _UNKNOWN_TEXT,
}

_OBSERVATION_TEXT = {
    "verified": "Verified",
    "failed": "Failed",
    UNKNOWN: _UNKNOWN_TEXT,
}

_CAPABILITY_TEXT = {
    "available": "Available",
    "unavailable": "Unavailable",
    UNKNOWN: _UNKNOWN_TEXT,
}

_METHOD_TEXT = {
    "transactional": "Transactional replace",
    "verified_unpack": "Verified unpack",
    UNKNOWN: _UNKNOWN_TEXT,
}

_BACKUP_TEXT = {
    "verified": "Verified",
    "failed": "Failed",
    "not_run": "Not run",
    UNKNOWN: _UNKNOWN_TEXT,
}

_VERSION_LABELS = (
    ("desktop", "Desktop"),
    ("remote", "Remote server"),
    ("served_ui", "Served interface"),
    ("protocol", "Protocol"),
    ("schema", "Schema"),
)

REDACTION_NOTICE = (
    "Redacted by design: no session token or token hash, no command line, "
    "no host name or filesystem path, and no Task or Context content."
)


def _tristate_text(value: bool | None, yes: str, no: str) -> str:
    if value is None:
        return _UNKNOWN_TEXT
    return yes if value else no


def format_report(view: Mapping[str, Any], *, refused: Iterable[str] = ()) -> str:
    """Render one bounded, copyable summary of an already-normalized snapshot.

    ``view`` must come from ``normalize_view``; passing raw caller state would
    bypass the allowlist, so an unrecognized schema is refused outright rather
    than rendered on a best-effort basis.
    """

    if not isinstance(view, Mapping) or view.get("schema_version") != SCHEMA_VERSION:
        raise DiagnosticsError("format_report requires a normalize_view snapshot")

    versions = _mapping(view.get("versions"))
    owner = _mapping(view.get("owner"))
    install = _mapping(view.get("install"))
    backup = _mapping(view.get("backup"))
    actions = [name for name in (view.get("actions") or ()) if _action(name)]

    lines = [
        f"Work Stack remote update diagnostics ({SCHEMA_VERSION})",
        f"Stage: {_STAGE_TEXT.get(view.get('stage'), _UNKNOWN_TEXT)}",
        f"Code: {_code(view.get('code')) or 'None'}",
        "",
        "Versions",
    ]
    for field, label in _VERSION_LABELS:
        lines.append(f"  {label}: {_version_field(field, versions.get(field)) or _UNKNOWN_TEXT}")

    lines.extend([
        "",
        "Owner",
        f"  State: {_OWNER_STATE_TEXT.get(owner.get('state'), _UNKNOWN_TEXT)}",
        "  Session token: "
        + _tristate_text(owner.get("token_available"), "Available", "Missing"),
        "  pidfd support: "
        + _tristate_text(owner.get("pidfd_available"), "Available", "Unavailable"),
        f"  Process exit: {_OBSERVATION_TEXT.get(owner.get('process_exit'), _UNKNOWN_TEXT)}",
        "  Listener release: "
        + _OBSERVATION_TEXT.get(owner.get("listener_release"), _UNKNOWN_TEXT),
        f"  Lease release: {_OBSERVATION_TEXT.get(owner.get('lease_release'), _UNKNOWN_TEXT)}",
        "",
        "Filesystem",
        f"  Install capability: {_CAPABILITY_TEXT.get(install.get('capability'), _UNKNOWN_TEXT)}",
        f"  Install method: {_METHOD_TEXT.get(install.get('method'), _UNKNOWN_TEXT)}",
        "",
        "Backup",
        f"  Status: {_BACKUP_TEXT.get(backup.get('status'), _UNKNOWN_TEXT)}",
        "  Migration required: "
        + _tristate_text(backup.get("migration_required"), "Yes", "No"),
        "",
        f"Next action: {actions[0] if actions else _UNKNOWN_TEXT}",
    ])
    if len(actions) > 1:
        lines.append(f"Other actions: {', '.join(actions[1:MAX_ACTIONS])}")

    # Field names only, and only ones this module itself produced. A caller
    # cannot smuggle text into the report through the refusal list.
    known_fields = _refusable_field_names()
    named = [field for field in refused if field in known_fields]
    if named:
        lines.append(f"Refused as malformed or unsafe: {', '.join(named[:MAX_ACTIONS])}")

    lines.extend(["", REDACTION_NOTICE])
    report = "\n".join(lines)
    if len(report) > MAX_REPORT_CHARACTERS:
        raise DiagnosticsError("diagnostic report exceeded its bounded size")
    return report


def _refusable_field_names() -> frozenset[str]:
    """Every field name ``normalize_view_with_notes`` can report as refused."""

    names = {"stage", "code", "owner.state", "actions", "actions.overflow"}
    names.update(f"versions.{field}" for field in VERSION_FIELDS)
    names.update(f"owner.{field}" for field in OWNER_OBSERVATION_FIELDS)
    names.update({"install.capability", "install.method", "backup.status"})
    names.update(f"actions.{index}" for index in range(MAX_ACTIONS + 1))
    return frozenset(names)


def render_report(payload: object, *, include_refused: bool = True) -> str:
    """Normalize caller state and render it in one call."""

    view, notes = normalize_view_with_notes(payload)
    return format_report(view, refused=notes if include_refused else ())
