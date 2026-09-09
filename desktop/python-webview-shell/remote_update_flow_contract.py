"""Types, ports and published snapshot for the remote update flow.

This module is the vocabulary half of the flow: the stage and code enums, the
typed facts each injected port returns, the port protocols themselves, and the
``remote-update-view/1`` projection the desktop update screen and the
diagnostics copier read.  ``remote_update_flow`` holds the lifecycle that
decides between them.

Nothing here performs an effect.  It is split out so the lifecycle module and
anything that only needs to read a snapshot can share one definition of the
vocabulary without importing the other's concerns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

from remote_update_skill_port import SKILL_FLOW_CODES, SkillPort


SCHEMA_VERSION = "remote-update-view/1"


class RemoteUpdateStage(str, Enum):
    """Stage vocabulary published in the snapshot.

    ``STOP`` and ``BACKUP`` are the two halves of the contract's compound
    ``backup/stop`` step.  They are separate tokens because they are separate
    observations, and stop runs first: the backup has to be taken with no
    writer mutating behind it.
    """

    IDLE = "idle"
    PREVIEW = "preview"
    STOP = "stop"
    BACKUP = "backup"
    PREPARE = "prepare"
    PROBE = "probe"
    ACTIVATE = "activate"
    VERIFY = "verify"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


RUN_ORDER: tuple[RemoteUpdateStage, ...] = (
    RemoteUpdateStage.PREVIEW,
    RemoteUpdateStage.STOP,
    RemoteUpdateStage.BACKUP,
    RemoteUpdateStage.PREPARE,
    RemoteUpdateStage.PROBE,
    RemoteUpdateStage.ACTIVATE,
    RemoteUpdateStage.VERIFY,
)

# The explicitly selected order that stages the new application code first.
# Same stages, same journal identities; only the position of PREPARE differs.
PREPARE_FIRST_RUN_ORDER: tuple[RemoteUpdateStage, ...] = (
    RemoteUpdateStage.PREVIEW,
    RemoteUpdateStage.PREPARE,
    RemoteUpdateStage.STOP,
    RemoteUpdateStage.BACKUP,
    RemoteUpdateStage.PROBE,
    RemoteUpdateStage.ACTIVATE,
    RemoteUpdateStage.VERIFY,
)

TERMINAL_STAGES = frozenset({RemoteUpdateStage.READY, RemoteUpdateStage.CANCELLED})

# The prepare port call that stages code and nothing else, and the action the
# flow offers for it.  Both are separate names on purpose: a caller that may
# only stage code must not be able to reach the full preparation by asking for
# the same thing, and a screen must be able to tell the two apart.
PREPARE_CODE_ONLY = "prepare_code_only"
ACTION_PREPARE_CODE_ONLY = "run_prepare_code_only"

Observation = Literal["verified", "failed", "unknown"]
OwnerViewState = Literal["live", "stopping", "dead", "foreign", "unfenced", "unknown"]

OBSERVATIONS = frozenset({"verified", "failed", "unknown"})
OWNER_STATES = frozenset({"live", "stopping", "dead", "foreign", "unfenced", "unknown"})
CAPABILITIES = frozenset({"available", "unavailable", "unknown"})
METHODS = frozenset({"transactional", "verified_unpack", "unknown"})
BACKUP_STATUSES = frozenset({"verified", "failed", "not_run", "unknown"})
# The registry's activation receipt is restart-bound: activate() writes it
# pending, a restart selects the new authority, and only then does confirm()
# move that same receipt to confirmed.  Rolled back is the third settled end.
ACTIVATION_STATES = frozenset({"pending", "confirmed", "rolled_back", "unknown"})

VERSION_FIELDS: tuple[str, ...] = ("desktop", "remote", "served_ui", "protocol", "schema")
OWNER_FIELDS: tuple[str, ...] = (
    "state", "token_available", "pidfd_available",
    "process_exit", "listener_release", "lease_release",
)
INSTALL_FIELDS: tuple[str, ...] = ("capability", "method")
BACKUP_FIELDS: tuple[str, ...] = ("status", "migration_required")

MAX_VERSION_LENGTH = 64

# Every code the snapshot can carry, grouped by the stage that publishes it.
# Bounded and symbolic on purpose: ports read remote hosts and may hand the
# flow a detail string, and none of those strings becomes a published code.
CODES: frozenset[str] = frozenset({
    "idle",
    "preview_ready", "preview_failed", "preview_unknown",
    "stop_verified", "stop_verified_without_pidfd", "stop_refused_missing_token",
    "stop_token_unknown", "stop_refused_foreign_host", "stop_refused_unfenced_receipt",
    "stop_owner_still_live", "stop_failed", "stop_incomplete_unknown",
    "backup_verified", "backup_failed", "backup_not_run", "backup_unknown",
    "backup_commit_unknown", "backup_not_verified_before_start",
    "prepare_ready", "prepare_failed", "prepare_unknown", "prepare_commit_unknown",
    "prepare_capability_unavailable", "previous_app_not_preserved",
    "previous_app_preservation_unknown",
    "probe_verified", "probe_failed", "probe_unknown", "probe_workspace_mismatch",
    "probe_workspace_unknown",
    "activate_pending_restart", "activate_restart_not_selected",
    "activate_restart_identity_mismatch", "activate_workspace_mismatch",
    "activate_workspace_unknown",
    "activate_committed", "activate_failed", "activate_unknown",
    "activate_commit_unknown", "activate_confirm_failed", "activate_confirm_unknown",
    "update_ready", "verify_failed", "verify_unknown",
    "cancelled_before_preview", "cancelled_before_stop", "cancelled_before_backup",
    "cancelled_before_prepare", "cancelled_before_probe", "cancelled_before_activate",
    "cancel_deferred_commit_unknown",
    "restore_verified", "restore_verified_activation_selected",
    "restore_failed", "restore_unknown",
    "rollback_verified", "rollback_failed", "rollback_unknown",
}) | SKILL_FLOW_CODES

# Refusal codes are raised, never published: each describes a call that was
# not legal in the flow's current condition, and the condition is unchanged.
REFUSAL_CODES: frozenset[str] = frozenset({
    "advance_refused_terminal", "advance_refused_failed", "advance_refused_unsettled",
    "advance_refused_restart_required",
    "retry_refused_not_failed", "retry_refused_pending_commit",
    "retry_refused_unrecoverable", "reconcile_refused_no_pending",
    "resume_refused_no_pending_activation", "resume_refused_pending_commit",
    "cancel_refused_activation_committed", "restore_refused_backup_not_verified",
    "restore_refused_pending_commit",
    "rollback_refused_migrated_ssot", "rollback_refused_no_previous_app",
    "rollback_refused_pending_commit", "rollback_refused_no_activation",
    "skill_refused_unavailable", "skill_refused_not_ready",
    "skill_refused_in_flight", "skill_refused_inspect_required",
})

# Symbolic next actions.  The first entry of a published tuple is the one clear
# next action for that condition; the rest are the escapes that stay legal.
ACTIONS: frozenset[str] = frozenset({
    "run_preview", "run_stop", "run_backup", "run_prepare", "run_probe",
    "run_prepare_code_only",
    "run_activate", "run_verify", "restart_desktop",
    "retry_stage", "reconcile_pending", "resolve_owner_authority",
    "resolve_activation_pairing",
    "restore_backup", "rollback_activation",
    "inspect_diagnostics", "cancel", "dismiss", "finish",
    "run_skill_inspect", "run_skill_install", "run_skill_update",
})


@dataclass(frozen=True)
class RunOrder:
    """The stage sequence one flow runs, and everything derived from it.

    Two orders exist and the flow is built with one of them.  The default runs
    preparation after the verified backup.  The explicitly selected
    ``prepare_before_stop`` order stages the new application code first,
    because the maintenance and observation helpers the later stages need live
    *inside* that new tree: staging only after the backup would require those
    helpers to be present before they are installed, which is a cycle.

    Staging in that order is code-only -- an absent new directory, every
    payload hash, the sidecar and the imports, and nothing else.  It reads and
    writes no SSOT, starts no server and changes no registry selection, so it
    is safe with the owner still running, and it is offered under its own
    action name so a caller allowed to stage code cannot reach the full
    preparation through the same offer.  The safety gate does not move: the
    probe, which is the first step that could run the staged code against the
    SSOT, still refuses without the verified backup that the verified stop
    precedes in both orders.

    The four indices are held rather than searched because the flow compares
    the stage it has reached against them on every published snapshot, and the
    two orders answer those comparisons differently.
    """

    stages: tuple[RemoteUpdateStage, ...]
    actions: Mapping[RemoteUpdateStage, str]
    cancel_codes: Mapping[RemoteUpdateStage, str]
    prepare_before_stop: bool
    prepare: int
    backup: int
    probe: int
    activate: int

    def __len__(self) -> int:
        return len(self.stages)

    def __getitem__(self, index: int) -> RemoteUpdateStage:
        return self.stages[index]

    def index(self, stage: RemoteUpdateStage) -> int:
        return self.stages.index(stage)

    def action(self, index: int) -> str:
        """The one clear next action for the stage at ``index``."""

        return self.actions[self.stages[index]]


def run_order(prepare_before_stop: bool = False, prepare_port: object = None) -> RunOrder:
    """Build the order a flow runs, default or prepare-first.

    Cancelling names the stage that will now not run, so a cancellation landing
    between two stages is never ambiguous about which one happened.  Verify has
    no entry: reaching it means activation is in force, which refuses
    cancelling.

    A prepare-first flow is refused outright when its prepare port cannot stage
    code only.  Falling back to ``prepare`` there would run the full
    preparation before the owner has stopped, which is the one thing this order
    must never do, so the absence is a construction error rather than something
    discovered at the stage.
    """

    stages = PREPARE_FIRST_RUN_ORDER if prepare_before_stop else RUN_ORDER
    actions = {stage: f"run_{stage.value}" for stage in stages}
    if prepare_before_stop:
        actions[RemoteUpdateStage.PREPARE] = ACTION_PREPARE_CODE_ONLY
        if not callable(getattr(prepare_port, PREPARE_CODE_ONLY, None)):
            raise ValueError(f"prepare port cannot {PREPARE_CODE_ONLY}")
    return RunOrder(
        stages=stages,
        actions=actions,
        cancel_codes={stage: f"cancelled_before_{stage.value}" for stage in stages[:-1]},
        prepare_before_stop=prepare_before_stop,
        prepare=stages.index(RemoteUpdateStage.PREPARE),
        backup=stages.index(RemoteUpdateStage.BACKUP),
        probe=stages.index(RemoteUpdateStage.PROBE),
        activate=stages.index(RemoteUpdateStage.ACTIVATE),
    )


class RemoteUpdateRefused(Exception):
    """A control call that is not legal in the flow's current condition."""

    def __init__(self, code: str) -> None:
        if code not in REFUSAL_CODES:
            raise ValueError(f"unbounded refusal code {code!r}")
        self.code = code
        super().__init__(code)


class LostResponse(Exception):
    """A mutating request was sent and its response was lost.

    A port raises this instead of reporting failure when it cannot say whether
    the far side committed.  The flow answers by reconciling the same
    ``operation_id`` through the same port, never by issuing again.
    """

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(operation_id)


class PortRefusal(Exception):
    """A port settled its request as refused, having committed nothing."""

    def __init__(self, code: str = "") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreviewFacts:
    """What the preview stage reads before anything is touched."""

    desktop_version: str | None = None
    remote_version: str | None = None
    served_ui_version: str | None = None
    protocol_version: str | None = None
    schema_version_before: str | None = None
    schema_version_target: str | None = None
    migration_required: bool | None = None
    install_capability: str = "unknown"
    install_method: str = "unknown"


@dataclass(frozen=True)
class OwnerStopFacts:
    """Owner stop evidence.

    The three release observations are independent on purpose.  A stop command
    that was merely spawned proves none of them, so each carries its own
    verified/failed/unknown answer and the flow needs all three before it
    calls the old writer stopped.
    """

    state: str = "unknown"
    token_available: bool | None = None
    pidfd_available: bool | None = None
    process_exit: str = "unknown"
    listener_release: str = "unknown"
    lease_release: str = "unknown"


@dataclass(frozen=True)
class BackupOutcome:
    status: str = "unknown"
    migration_required: bool | None = None


@dataclass(frozen=True)
class PrepareOutcome:
    """Preparation result.

    Preparation stages a new application beside the running one; it never
    activates and never replaces the current app, so the retention answers
    below are the flow's proof that a recovery path still exists.
    """

    status: str = "unknown"
    capability: str = "unknown"
    method: str = "unknown"
    previous_app_retained: bool | None = None
    previous_profile_retained: bool | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    status: str = "unknown"
    workspace_match: str = "unknown"
    served_ui_version: str | None = None
    protocol_version: str | None = None


@dataclass(frozen=True)
class ActivationOutcome:
    """One answer about the activation receipt this flow owns.

    ``committed`` says the registry durably wrote what was asked; ``state``
    says what the receipt now is.  The two are separate because a successful
    ``activate`` commits a receipt that is only *pending*: the desktop still
    has to restart, select the new authority and confirm that same receipt
    before the activation is in force.
    """

    status: str = "unknown"
    committed: bool | None = None
    state: str = "unknown"
    restart_required: bool | None = None
    remote_version: str | None = None
    schema_version: str | None = None


@dataclass(frozen=True)
class RestoreOutcome:
    """A backup restore, and what the restored data now is.

    ``pre_migration_data`` is the answer recovery turns on: only a verified
    restore that put the pre-migration archive back makes running the previous
    application against this data correct again.
    """

    status: str = "unknown"
    pre_migration_data: bool | None = None


@dataclass(frozen=True)
class RollbackOutcome:
    """An activation rollback, and whether the previous selection is in force."""

    status: str = "unknown"
    previous_activation_selected: bool | None = None


@dataclass(frozen=True)
class StartupEvidence:
    """What the host observed at startup, after the activation restart.

    The flow resumes its retained receipt only on this evidence:
    ``selected_activation_id`` has to be the identity the flow recorded, the
    new authority has to have actually been selected, and the workspace the new
    runtime serves has to be the same one.  Absent evidence is unknown, and
    unknown never confirms.
    """

    selected_activation_id: str | None = None
    authority_selected: str = "unknown"
    workspace_match: str = "unknown"


@dataclass(frozen=True)
class VerifyOutcome:
    status: str = "unknown"
    desktop_version: str | None = None
    remote_version: str | None = None
    served_ui_version: str | None = None
    protocol_version: str | None = None
    schema_version: str | None = None


@dataclass(frozen=True)
class MeasuredVersions:
    """Versions measured so far, each one nullable until something reads it."""

    desktop: str | None = None
    remote: str | None = None
    served_ui: str | None = None
    protocol: str | None = None
    schema: str | None = None


@dataclass(frozen=True)
class SchemaTransition:
    """Schema before, schema after, and whether getting there migrates data.

    Deliberately not part of ``remote-update-view/1``: that contract's
    ``versions`` map carries one ``schema`` slot, which is the schema in use
    now.  The target is handed to the screen through this typed value rather
    than smuggled into the published document as an extra key.
    """

    before: str | None = None
    target: str | None = None
    migration_required: bool | None = None


KIND_OWNER_STOP = "owner_stop"
KIND_BACKUP_CREATE = "backup_create"
KIND_BACKUP_RESTORE = "backup_restore"
KIND_PREPARE_APPLY = "prepare_apply"
KIND_ACTIVATION_ISSUE = "activation_issue"
KIND_ACTIVATION_PENDING = "activation_pending"
KIND_ACTIVATION_CONFIRMED = "activation_confirmed"
KIND_ACTIVATION_CONFIRM = "activation_confirm"
KIND_ACTIVATION_ROLLBACK = "activation_rollback"

# The two anchor kinds.  Neither is a request in flight: each is a receipt the
# registry has already written, kept on record because a restart has to find
# the activation this flow put in the registry and still owns the recovery of.
ACTIVATION_ANCHOR_KINDS = frozenset({KIND_ACTIVATION_PENDING, KIND_ACTIVATION_CONFIRMED})


@dataclass(frozen=True)
class PendingOperation:
    """One recorded mutation identity, and what that identity is.

    ``kind`` is the semantic operation, not merely the stage that ran it: a
    backup-stage identity is either a create or a restore, and an activate-stage
    identity is an issue, a receipt awaiting its restart, a confirmed receipt
    still owed a verification, a confirmation or a rollback.  They reconcile
    through different port calls and mean different things, so the durable
    record has to say which one it is.

    ``previous_app_retained`` is the one measured fact a restart cannot go back
    and re-measure: preparation observed it, preparation's identity is settled
    and gone, and recovery after a restart needs it to know whether the
    previous application is still there to return to.  It is an
    ``OBSERVATIONS`` member, and a record that carries no answer says
    ``unknown`` rather than letting a fresh instance's default stand in.
    """

    stage: str
    operation_id: str
    kind: str = ""
    previous_app_retained: str = "unknown"


@dataclass(frozen=True)
class MutationSpec:
    """How one semantic mutation is issued, observed and read back.

    ``issue`` is empty for both anchor kinds: those records are not requests in
    flight but receipts the registry has already written, kept so a restart
    finds them.  They stay observable through the same identity.
    """

    kind: str
    stage: RemoteUpdateStage
    port: str
    issue: str
    observe: str
    classify: str
    outcome: type
    open_code: str
    refused_code: str


MUTATIONS: dict[str, MutationSpec] = {
    spec.kind: spec
    for spec in (
        MutationSpec(
            KIND_OWNER_STOP, RemoteUpdateStage.STOP, "owner", "stop", "observe",
            "owner_stop", OwnerStopFacts,
            "stop_incomplete_unknown", "stop_failed",
        ),
        MutationSpec(
            KIND_BACKUP_CREATE, RemoteUpdateStage.BACKUP, "backup",
            "create_verified", "observe", "backup_create", BackupOutcome,
            "backup_commit_unknown", "backup_failed",
        ),
        MutationSpec(
            KIND_BACKUP_RESTORE, RemoteUpdateStage.BACKUP, "backup",
            "restore", "observe_restore", "backup_restore", RestoreOutcome,
            "restore_unknown", "restore_failed",
        ),
        MutationSpec(
            KIND_PREPARE_APPLY, RemoteUpdateStage.PREPARE, "prepare",
            "prepare", "observe", "prepare_apply", PrepareOutcome,
            "prepare_commit_unknown", "prepare_failed",
        ),
        MutationSpec(
            KIND_ACTIVATION_ISSUE, RemoteUpdateStage.ACTIVATE, "activation",
            "activate", "observe", "activation_issue", ActivationOutcome,
            "activate_commit_unknown", "activate_failed",
        ),
        MutationSpec(
            KIND_ACTIVATION_PENDING, RemoteUpdateStage.ACTIVATE, "activation",
            "", "observe", "activation_issue", ActivationOutcome,
            "activate_commit_unknown", "activate_failed",
        ),
        MutationSpec(
            KIND_ACTIVATION_CONFIRMED, RemoteUpdateStage.ACTIVATE, "activation",
            "", "observe_confirm", "activation_confirm", ActivationOutcome,
            "activate_confirm_unknown", "activate_confirm_failed",
        ),
        MutationSpec(
            KIND_ACTIVATION_CONFIRM, RemoteUpdateStage.ACTIVATE, "activation",
            "confirm", "observe_confirm", "activation_confirm", ActivationOutcome,
            "activate_confirm_unknown", "activate_confirm_failed",
        ),
        MutationSpec(
            KIND_ACTIVATION_ROLLBACK, RemoteUpdateStage.ACTIVATE, "activation",
            "rollback", "observe_rollback", "activation_rollback", RollbackOutcome,
            "rollback_unknown", "rollback_failed",
        ),
    )
}


class PreviewPort(Protocol):
    def describe(self) -> PreviewFacts: ...


class OwnerStopPort(Protocol):
    def stop(self, operation_id: str) -> OwnerStopFacts: ...
    def observe(self, operation_id: str) -> OwnerStopFacts: ...


class BackupPort(Protocol):
    def create_verified(self, operation_id: str) -> BackupOutcome: ...
    def observe(self, operation_id: str) -> BackupOutcome: ...
    def restore(self, operation_id: str) -> RestoreOutcome: ...
    def observe_restore(self, operation_id: str) -> RestoreOutcome: ...


class PreparePort(Protocol):
    def prepare(self, operation_id: str) -> PrepareOutcome: ...
    def observe(self, operation_id: str) -> PrepareOutcome: ...


class CodeOnlyPreparePort(PreparePort, Protocol):
    """A prepare port that can also stage the new application code alone.

    ``prepare_code_only`` writes an absent new application directory, verifies
    the archive, the sidecar, every payload hash, the imports and the
    completion receipt, and stops there.  It reads and writes no SSOT, runs no
    migration, starts no server, and changes no registry selection or
    activation; the current application and profile stay exactly where they
    are.  Its answer is the same :class:`PrepareOutcome`, including the
    retention answers, because it is the same PREPARE identity: it is
    reconciled through the same ``observe`` under the same operation id, and a
    flow that issued it never issues ``prepare`` for that identity as well.

    Only a flow built with ``prepare_before_stop=True`` calls it, and such a
    flow is refused at construction when the port does not provide it.
    """

    def prepare_code_only(self, operation_id: str) -> PrepareOutcome: ...


class ProbePort(Protocol):
    def probe(self, operation_id: str) -> ProbeOutcome: ...


class ActivationPort(Protocol):
    """The restart-bound activation receipt, and nothing else.

    ``activate`` writes a pending receipt, ``confirm`` moves that same receipt
    to confirmed once a restart has selected the new authority, and ``rollback``
    puts the previous selection back.  Each is its own mutation with its own
    same-identity observation.
    """

    def activate(self, operation_id: str) -> ActivationOutcome: ...
    def observe(self, operation_id: str) -> ActivationOutcome: ...
    def confirm(self, operation_id: str) -> ActivationOutcome: ...
    def observe_confirm(self, operation_id: str) -> ActivationOutcome: ...
    def rollback(self, operation_id: str) -> RollbackOutcome: ...
    def observe_rollback(self, operation_id: str) -> RollbackOutcome: ...


class VerificationPort(Protocol):
    """Read the finished update back.  Nothing here mutates anything.

    The activation receipt is confirmed through :class:`ActivationPort`, so
    this port only measures what the now-active connection actually serves.
    """

    def verify(self, operation_id: str) -> VerifyOutcome: ...


class JournalPort(Protocol):
    """Durable record of the mutation identities that have not reached an end.

    The flow writes an identity before issuing it and clears it only once the
    request settles, so a desktop restart in the middle of a mutation finds
    that identity and reconciles it instead of issuing a second one.

    There are at most two at a time, and they are different in kind.  One is a
    request whose disposition is not known.  The other is the activation
    receipt the registry durably wrote: its disposition *is* known, and it
    stays recorded because a restart has to find it.  While that receipt is
    pending the record is what the restart resumes; once it is confirmed the
    record is a passive anchor, saying which activation is selected and still
    owed either a verified update or a verified rollback.  It is never a
    request to activate or confirm anything a second time.
    """

    def load(self) -> tuple[PendingOperation, ...]: ...
    def record(self, entries: tuple[PendingOperation, ...]) -> None: ...


@dataclass(frozen=True)
class RemoteUpdatePorts:
    preview: PreviewPort
    owner: OwnerStopPort
    backup: BackupPort
    prepare: PreparePort
    probe: ProbePort
    activation: ActivationPort
    verification: VerificationPort
    #: The optional agent-Skill side action offered on a ready update.  It
    #: defaults to nothing so every existing bundle, fixture and construction
    #: keeps working unchanged, and so a build whose skill transport is absent
    #: simply never makes the offer rather than failing to compose a flow.
    skill: SkillPort | None = None


@dataclass(frozen=True)
class RemoteUpdateSnapshot:
    """The ``remote-update-view/1`` projection."""

    stage: str
    code: str
    versions: Mapping[str, str | None]
    owner: Mapping[str, object]
    install: Mapping[str, str]
    backup: Mapping[str, object]
    actions: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def to_document(self) -> dict[str, object]:
        """Return the snapshot as plain data in one fixed key order."""

        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "code": self.code,
            "versions": {name: self.versions.get(name) for name in VERSION_FIELDS},
            "owner": {name: self.owner.get(name) for name in OWNER_FIELDS},
            "install": {name: self.install.get(name) for name in INSTALL_FIELDS},
            "backup": {name: self.backup.get(name) for name in BACKUP_FIELDS},
            "actions": list(self.actions),
        }


def member(value: object, allowed: frozenset[str]) -> str:
    """Admit one enum member, normalizing anything absent to unknown."""

    if isinstance(value, str) and value in allowed:
        return value
    return "unknown"


def flag(value: object) -> bool | None:
    """Admit a boolean answer, or nothing; anything else is unknown."""

    return value if isinstance(value, bool) else None


def version(value: object) -> str | None:
    """Admit a bounded single-line version string, or nothing.

    Ports read these off remote hosts, so the length bound and the control
    character rejection here are what keep a hostile string from reaching the
    screen through the projection.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_VERSION_LENGTH:
        return None
    if any(character < " " or character == "\x7f" for character in text):
        return None
    return text


def measured_version(field: str, value: object) -> str | None:
    """Admit a measured entrypoint digest only in its dedicated UI slot."""
    if field == "served_ui" and isinstance(value, str) and value.startswith("sha256:"):
        digest = value[7:]
        return value if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else None
    return version(value)
