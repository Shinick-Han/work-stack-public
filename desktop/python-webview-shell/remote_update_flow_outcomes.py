"""What one port outcome means, decided without touching any flow state.

Every function here answers the same shape of question: given one typed
outcome that a port returned, which stage and which bounded code does that
outcome establish?  Nothing here reads or writes the flow, calls a port or
decides what happens next; ``remote_update_flow`` owns all of that and calls
these to keep the two questions apart.

The sets at the bottom are the same kind of statement in table form: which
codes keep a mutation identity recorded and under which semantic kind, which
codes move the run order forward, and which codes no re-run can improve.
"""

from __future__ import annotations

from dataclasses import replace

from remote_update_flow_contract import (
    ACTIVATION_STATES,
    BACKUP_STATUSES,
    CAPABILITIES,
    KIND_ACTIVATION_CONFIRM,
    KIND_ACTIVATION_CONFIRMED,
    KIND_ACTIVATION_ISSUE,
    KIND_ACTIVATION_PENDING,
    KIND_ACTIVATION_ROLLBACK,
    KIND_BACKUP_CREATE,
    KIND_BACKUP_RESTORE,
    KIND_OWNER_STOP,
    KIND_PREPARE_APPLY,
    METHODS,
    OBSERVATIONS,
    OWNER_STATES,
    ActivationOutcome,
    BackupOutcome,
    OwnerStopFacts,
    PrepareOutcome,
    ProbeOutcome,
    RemoteUpdateStage,
    RestoreOutcome,
    RollbackOutcome,
    StartupEvidence,
    VerifyOutcome,
    flag,
    member,
)


Decision = tuple[RemoteUpdateStage, str]

_RELEASE_FIELDS = ("process_exit", "listener_release", "lease_release")


def classify_owner_stop(facts: OwnerStopFacts) -> Decision:
    """Decide the stop from independent evidence, never from a sent command."""

    # A missing session token is its own condition.  It is not a legacy
    # receipt, and it is not a licence to clear or override anything.
    token = flag(facts.token_available)
    if token is False:
        return (RemoteUpdateStage.FAILED, "stop_refused_missing_token")
    if token is None:
        return (RemoteUpdateStage.UNKNOWN, "stop_token_unknown")
    state = member(facts.state, OWNER_STATES)
    if state == "foreign":
        return (RemoteUpdateStage.FAILED, "stop_refused_foreign_host")
    if state == "unfenced":
        return (RemoteUpdateStage.FAILED, "stop_refused_unfenced_receipt")
    if state == "live":
        return (RemoteUpdateStage.FAILED, "stop_owner_still_live")
    if state != "dead":
        return (RemoteUpdateStage.UNKNOWN, "stop_incomplete_unknown")
    releases = tuple(member(getattr(facts, name), OBSERVATIONS) for name in _RELEASE_FIELDS)
    if "failed" in releases:
        return (RemoteUpdateStage.FAILED, "stop_failed")
    if "unknown" in releases:
        return (RemoteUpdateStage.UNKNOWN, "stop_incomplete_unknown")
    # All three releases are verified.  An absent pidfd narrowed how they could
    # be read, so it is named rather than hidden; it never licences a blind
    # kill, and no such fallback exists anywhere in this flow.
    if flag(facts.pidfd_available) is False:
        return (RemoteUpdateStage.STOP, "stop_verified_without_pidfd")
    return (RemoteUpdateStage.STOP, "stop_verified")


def classify_backup_create(
    outcome: BackupOutcome, planned_migration: bool | None
) -> tuple[BackupOutcome, RemoteUpdateStage, str]:
    """Normalize the backup fact and say whether the gate is now open."""

    status = member(outcome.status, BACKUP_STATUSES)
    reported = flag(outcome.migration_required)
    fact = BackupOutcome(
        status=status,
        migration_required=reported if reported is not None else planned_migration,
    )
    if status == "verified":
        return (fact, RemoteUpdateStage.BACKUP, "backup_verified")
    if status == "failed":
        return (fact, RemoteUpdateStage.FAILED, "backup_failed")
    if status == "not_run":
        return (fact, RemoteUpdateStage.FAILED, "backup_not_run")
    return (fact, RemoteUpdateStage.UNKNOWN, "backup_unknown")


def classify_prepare_apply(
    outcome: PrepareOutcome,
) -> tuple[PrepareOutcome, RemoteUpdateStage, str]:
    """Normalize what preparation measured and say whether it may activate."""

    prepared = replace(
        outcome,
        status=member(outcome.status, OBSERVATIONS),
        capability=member(outcome.capability, CAPABILITIES),
        method=member(outcome.method, METHODS),
        previous_app_retained=flag(outcome.previous_app_retained),
        previous_profile_retained=flag(outcome.previous_profile_retained),
    )
    if prepared.capability == "unavailable":
        return (prepared, RemoteUpdateStage.FAILED, "prepare_capability_unavailable")
    if prepared.status == "failed":
        return (prepared, RemoteUpdateStage.FAILED, "prepare_failed")
    if prepared.status == "unknown":
        return (prepared, RemoteUpdateStage.UNKNOWN, "prepare_unknown")
    # A prepared target that did not keep the previous app and profile has
    # taken the recovery path away, so the flow stops before activating.
    retained = (prepared.previous_app_retained, prepared.previous_profile_retained)
    if False in retained:
        return (prepared, RemoteUpdateStage.FAILED, "previous_app_not_preserved")
    if None in retained:
        return (prepared, RemoteUpdateStage.UNKNOWN, "previous_app_preservation_unknown")
    return (prepared, RemoteUpdateStage.PREPARE, "prepare_ready")


def classify_probe(outcome: ProbeOutcome) -> Decision:
    """The workspace answer outranks the probe's own verdict.

    A healthy server serving the wrong workspace is the worse finding, and it
    has to be caught before anything switches the connection to it.
    """

    match = member(outcome.workspace_match, OBSERVATIONS)
    if match == "failed":
        return (RemoteUpdateStage.FAILED, "probe_workspace_mismatch")
    if match == "unknown":
        return (RemoteUpdateStage.UNKNOWN, "probe_workspace_unknown")
    status = member(outcome.status, OBSERVATIONS)
    if status == "failed":
        return (RemoteUpdateStage.FAILED, "probe_failed")
    if status == "unknown":
        return (RemoteUpdateStage.UNKNOWN, "probe_unknown")
    return (RemoteUpdateStage.PROBE, "probe_verified")


def classify_activation_issue(outcome: ActivationOutcome) -> Decision:
    """An issued activation ends pending, never in force.

    The registry seam this wraps writes a *pending* receipt and requires a
    restart before anything may confirm it.  So a durably written pending
    receipt is the success here, and a port that claims the activation is
    already in force is reporting something this flow cannot act on.
    """

    status = member(outcome.status, OBSERVATIONS)
    committed = flag(outcome.committed)
    if status == "failed" or (status == "verified" and committed is False):
        return (RemoteUpdateStage.FAILED, "activate_failed")
    if status == "verified" and committed is True:
        state = member(outcome.state, ACTIVATION_STATES)
        if state == "pending" and flag(outcome.restart_required) is True:
            return (RemoteUpdateStage.ACTIVATE, "activate_pending_restart")
    # The port answered, but the answer does not establish a pending receipt
    # this flow may hold.  Only observing that same identity can settle it.
    return (RemoteUpdateStage.UNKNOWN, "activate_unknown")


def classify_activation_confirm(outcome: ActivationOutcome) -> Decision:
    """Only a confirmed receipt puts the activation in force."""

    status = member(outcome.status, OBSERVATIONS)
    committed = flag(outcome.committed)
    state = member(outcome.state, ACTIVATION_STATES)
    if status == "verified" and committed is True and state == "confirmed":
        return (RemoteUpdateStage.ACTIVATE, "activate_committed")
    if status == "failed" or (status == "verified" and committed is False):
        return (RemoteUpdateStage.FAILED, "activate_confirm_failed")
    return (RemoteUpdateStage.UNKNOWN, "activate_confirm_unknown")


def classify_activation_rollback(outcome: RollbackOutcome) -> Decision:
    """A rollback counts only when the previous selection is actually back."""

    status = member(outcome.status, OBSERVATIONS)
    if status == "verified" and flag(outcome.previous_activation_selected) is True:
        return (RemoteUpdateStage.FAILED, "rollback_verified")
    if status == "failed":
        return (RemoteUpdateStage.FAILED, "rollback_failed")
    return (RemoteUpdateStage.UNKNOWN, "rollback_unknown")


def classify_backup_restore(outcome: RestoreOutcome) -> Decision:
    """A restore counts only when the pre-migration data is demonstrably back.

    Anything less leaves the question the whole recovery turns on unanswered,
    so it stays an open identity to reconcile rather than a settled restore.
    """

    status = member(outcome.status, OBSERVATIONS)
    if status == "verified" and flag(outcome.pre_migration_data) is True:
        return (RemoteUpdateStage.FAILED, "restore_verified")
    if status == "failed":
        return (RemoteUpdateStage.FAILED, "restore_failed")
    return (RemoteUpdateStage.UNKNOWN, "restore_unknown")


def retention_of(prepared: PrepareOutcome) -> str:
    """Whether preparation proved the previous app *and* profile were kept.

    One ``OBSERVATIONS`` member, because this is the fact recovery needs after
    a restart and a restart can only carry a preserved answer, never re-derive
    one.  Either answer missing is ``unknown``; it never becomes ``failed`` and
    it never becomes permission to roll back.
    """

    answers = (prepared.previous_app_retained, prepared.previous_profile_retained)
    if all(answer is True for answer in answers):
        return "verified"
    return "failed" if False in answers else "unknown"


def classify_verify(outcome: VerifyOutcome) -> Decision:
    status = member(outcome.status, OBSERVATIONS)
    if status == "failed":
        return (RemoteUpdateStage.FAILED, "verify_failed")
    if status == "unknown":
        return (RemoteUpdateStage.UNKNOWN, "verify_unknown")
    return (RemoteUpdateStage.READY, "update_ready")


def classify_startup(evidence: object, activation_id: str) -> Decision | None:
    """Say whether the restart may resume this exact activation identity.

    ``None`` means it may: the host selected this same activation, the new
    authority is actually the selected one, and the runtime it brought up
    serves the same workspace.  Every other answer keeps the receipt pending.
    """

    if not isinstance(evidence, StartupEvidence):
        return (RemoteUpdateStage.UNKNOWN, "activate_restart_not_selected")
    selected = evidence.selected_activation_id
    if not isinstance(selected, str) or selected != activation_id:
        return (RemoteUpdateStage.UNKNOWN, "activate_restart_identity_mismatch")
    if member(evidence.authority_selected, OBSERVATIONS) != "verified":
        return (RemoteUpdateStage.UNKNOWN, "activate_restart_not_selected")
    match = member(evidence.workspace_match, OBSERVATIONS)
    if match == "failed":
        return (RemoteUpdateStage.FAILED, "activate_workspace_mismatch")
    if match != "verified":
        return (RemoteUpdateStage.UNKNOWN, "activate_workspace_unknown")
    return None


# Codes that keep a mutation identity recorded, and the semantic kind the
# record carries afterwards.  An outcome that did not settle whether the far
# side committed keeps its own kind; an activation whose receipt is durably
# pending becomes the retained receipt that the restart resumes; and a
# confirmed receipt becomes the passive anchor that says which activation is
# selected, kept until the update verifies or a rollback puts the previous
# selection back.
RETAINED_KINDS: dict[str, str] = {
    "stop_incomplete_unknown": KIND_OWNER_STOP,
    "stop_token_unknown": KIND_OWNER_STOP,
    "backup_unknown": KIND_BACKUP_CREATE,
    "backup_commit_unknown": KIND_BACKUP_CREATE,
    "prepare_unknown": KIND_PREPARE_APPLY,
    "prepare_commit_unknown": KIND_PREPARE_APPLY,
    "activate_unknown": KIND_ACTIVATION_ISSUE,
    "activate_commit_unknown": KIND_ACTIVATION_ISSUE,
    "activate_pending_restart": KIND_ACTIVATION_PENDING,
    "activate_committed": KIND_ACTIVATION_CONFIRMED,
    "activate_confirm_unknown": KIND_ACTIVATION_CONFIRM,
    "restore_unknown": KIND_BACKUP_RESTORE,
    "rollback_unknown": KIND_ACTIVATION_ROLLBACK,
}

# Codes that mean the run order moved on by one stage.
PROGRESS_CODES: frozenset[str] = frozenset({
    "preview_ready", "stop_verified", "stop_verified_without_pidfd",
    "backup_verified", "prepare_ready", "probe_verified", "activate_committed",
})

# Codes whose stage must not be re-run.  Either an operator has to settle an
# authority or identity question outside this flow, or the stage already
# mutated the target once and a second issue would be a duplicate mutation.
UNRECOVERABLE_CODES: frozenset[str] = frozenset({
    "stop_refused_missing_token", "stop_refused_foreign_host",
    "stop_refused_unfenced_receipt", "previous_app_not_preserved",
    "previous_app_preservation_unknown", "probe_workspace_mismatch",
    "activate_workspace_mismatch", "activate_workspace_unknown",
})

# The activation receipt is written and the desktop has to restart into it.
RESTART_CODES: frozenset[str] = frozenset({
    "activate_pending_restart", "activate_restart_not_selected",
    "activate_restart_identity_mismatch", "activate_confirm_failed",
})

RECOVERY_SETTLED_CODES: frozenset[str] = frozenset({
    "restore_verified", "rollback_verified",
})

RECOVERY_CODES: frozenset[str] = RECOVERY_SETTLED_CODES | frozenset({
    "restore_failed", "restore_unknown", "restore_verified_activation_selected",
    "rollback_failed", "rollback_unknown",
})
