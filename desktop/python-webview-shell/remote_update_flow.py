"""Bounded desktop-managed remote update flow.

One remote update attempt at a time, in one order: preview, owner stop,
verified backup, application preparation, same-workspace probe, activation,
verification.  Manual recovery used to mean running each of those by hand and
remembering which had already happened; this module is what remembers.

Pure lifecycle only.  Every effect that reaches a remote host, an owner
receipt, a backup archive, the connection registry or the SSOT arrives through
an explicitly injected port from ``remote_update_flow_contract``.  Nothing
here imports pywebview, spawns a process, opens a socket, reads the registry,
touches live SSOT data or consults the clock.  What an individual outcome
*means* lives in ``remote_update_flow_outcomes``; this module decides what the
flow does next about it.

Four rules shape almost every decision below:

*   A verified backup exists before anything starts the new server, and the
    old writer is stopped before that backup is taken, because a backup taken
    while a writer is still mutating is not a consistent backup.
*   Activation is restart-bound.  Issuing it writes a *pending* receipt; the
    desktop then has to restart, select the new authority and confirm that
    same retained receipt.  Nothing here treats a pending receipt as an
    activation in force, and nothing advances past it in the same process.
    The confirmed receipt stays on record afterwards as a passive anchor, not
    a request to confirm again, so a restart before the update is whole
    rebuilds which activation is selected instead of waking up idle.
*   A mutating request is issued at most once per operation identity, and each
    identity is recorded under the semantic operation it is -- backup create
    or restore, activation issue, confirmation or rollback.  While any
    identity is unsettled no new mutation is issued at all, including a
    restore.  A lost or unknown answer keeps the identity; only an answer that
    says what happened releases it.
*   Authority that could not be verified is never recovered by force, and old
    code never runs against migrated data.  Rollback to the preserved previous
    application stays refused until a verified restore has put the
    pre-migration data back, and a restore that leaves the new activation
    selected is not a finished recovery.

The order above is the default.  ``prepare_before_stop=True`` selects the other
order, staging the new application code first -- preview, prepare, stop, backup,
probe, activate, verify -- because the helpers the later stages run live inside
that new tree.  ``RunOrder`` holds why that is safe and what it does not move.

:meth:`RemoteUpdateFlow.snapshot` publishes the internal
``remote-update-view/1`` projection shared with the desktop update screen and
the diagnostics copier.  It is not a public API, it carries only the bounded
symbolic facts that contract names, and no port string reaches it unbounded.
"""

from __future__ import annotations

import threading
from typing import Callable

import remote_update_flow_outcomes as outcomes
from remote_update_flow_contract import (
    ACTIONS,
    ACTIVATION_ANCHOR_KINDS,
    BACKUP_STATUSES,
    CAPABILITIES,
    CODES,
    KIND_ACTIVATION_CONFIRM,
    KIND_ACTIVATION_ISSUE,
    KIND_ACTIVATION_PENDING,
    KIND_ACTIVATION_ROLLBACK,
    KIND_BACKUP_CREATE,
    KIND_BACKUP_RESTORE,
    KIND_OWNER_STOP,
    KIND_PREPARE_APPLY,
    METHODS,
    MUTATIONS,
    OBSERVATIONS,
    OWNER_STATES,
    PREPARE_CODE_ONLY,
    SCHEMA_VERSION,
    TERMINAL_STAGES,
    VERSION_FIELDS,
    BackupOutcome,
    JournalPort,
    LostResponse,
    MeasuredVersions,
    MutationSpec,
    OwnerStopFacts,
    PendingOperation,
    PortRefusal,
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    RemoteUpdatePorts,
    RemoteUpdateRefused,
    RemoteUpdateSnapshot,
    RemoteUpdateStage,
    RunOrder,
    SchemaTransition,
    StartupEvidence,
    VerifyOutcome,
    flag,
    member,
    measured_version,
    run_order,
    version,
)
from remote_update_skill_offer import SkillOfferMixin


class RemoteUpdateFlow(SkillOfferMixin):
    """One bounded remote update attempt, driven one call at a time.

    ``advance`` runs the next stage, or settles a recorded identity when one
    is in flight.  ``resume_after_restart`` is how the restart-bound
    activation continues: it confirms the retained receipt, and only on
    evidence that the restart selected that same activation and the runtime it
    brought up serves the same workspace.  ``retry`` re-runs a stage that
    settled as failed, ``reconcile`` settles an unsettled identity explicitly,
    and ``cancel``, ``restore`` and ``rollback`` are the recovery calls.  A
    call that is not legal in the current condition raises
    ``RemoteUpdateRefused`` and changes nothing, so the snapshot always
    describes the flow itself and never a rejected request.

    ``prepare_before_stop`` picks the run order once, at construction: a
    restart rebuilds the same flow the same way, from the same selection.

    ``on_completed`` is told once, when the update is whole *and* the journal
    anchor has been durably released -- never before, and never if that release
    raises.  A caller keeping its own side record may retire it there.
    """

    def __init__(
        self,
        ports: RemoteUpdatePorts,
        *,
        operation_ids: Callable[[str], str],
        journal: JournalPort | None = None,
        prepare_before_stop: bool = False,
        on_completed: Callable[[], object] | None = None,
    ) -> None:
        self._ports = ports
        self._order: RunOrder = run_order(prepare_before_stop, ports.prepare)
        self._operation_ids = operation_ids
        self._journal = journal
        self._on_completed = on_completed
        self._lock = threading.Lock()
        self._stage = RemoteUpdateStage.IDLE
        self._code = "idle"
        self._index = 0
        self._cancel_requested = False
        self._activation_id: str | None = None
        self._retention = "unknown"
        self._restored_pre_migration = False
        self._preview = PreviewFacts()
        self._versions = MeasuredVersions()
        self._owner = OwnerStopFacts()
        self._backup = BackupOutcome(status="not_run")
        self._prepare = PrepareOutcome()
        self._pending: PendingOperation | None = None
        self._activation: PendingOperation | None = None
        if journal is not None:
            self._adopt_recorded(journal.load())

    def _adopt_recorded(self, loaded: object) -> None:
        """Resume a restart into the recorded identities, never past them."""

        entries = loaded if isinstance(loaded, (tuple, list)) else (loaded,)
        for entry in entries:
            self._adopt_entry_locked(entry)
        if self._pending is None and self._activation is None:
            return
        self._index = self._resumed_index_locked()
        if self._pending is not None:
            self._stage = RemoteUpdateStage.UNKNOWN
            self._code = MUTATIONS[self._pending.kind].open_code
        else:
            self._stage = RemoteUpdateStage.ACTIVATE
            waiting = self._restart_pending_locked()
            self._code = "activate_pending_restart" if waiting else "activate_committed"
        if self._index > self._order.backup:
            # This flow issues nothing past the backup without a verified
            # one, so a recorded identity for a later stage of *this* order is
            # itself proof the backup verified before the restart.  Only that
            # gate is reconstructed; nothing else in the record is re-asserted.
            self._backup = BackupOutcome(status="verified", migration_required=None)

    def _adopt_entry_locked(self, entry: object) -> None:
        if not isinstance(entry, PendingOperation) or not isinstance(entry.kind, str):
            return
        spec = MUTATIONS.get(entry.kind)
        if spec is None or spec.stage.value != entry.stage:
            return
        if entry.kind in ACTIVATION_ANCHOR_KINDS:
            self._activation = entry
        else:
            self._pending = entry
        if spec.port == "activation":
            self._activation_id = entry.operation_id
        retained = member(entry.previous_app_retained, OBSERVATIONS)
        if retained != "unknown":
            self._retention = retained

    def _resumed_index_locked(self) -> int:
        """The furthest point the recorded identities themselves establish.

        A confirmed anchor proves activation is behind this flow, so what is
        left is the verification; every other record resumes at the stage that
        issued it.  The furthest wins, so a restore recorded after an
        activation never rewinds past it.
        """

        index = 0
        if self._activation is not None:
            index = self._order.activate + (0 if self._restart_pending_locked() else 1)
        if self._pending is not None:
            index = max(index, self._order.index(MUTATIONS[self._pending.kind].stage))
        return index

    # -- published state -------------------------------------------------

    def snapshot(self) -> RemoteUpdateSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def schema_transition(self) -> SchemaTransition:
        """Report schema before, schema target and whether data migrates."""

        with self._lock:
            return SchemaTransition(
                before=version(self._preview.schema_version_before),
                target=version(self._preview.schema_version_target),
                migration_required=self._migration_required_locked(),
            )

    def pending_operation(self) -> PendingOperation | None:
        """The identity currently in flight, if any."""

        with self._lock:
            return self._pending

    def retained_activation(self) -> PendingOperation | None:
        """The activation receipt this flow is holding, pending or confirmed."""

        with self._lock:
            return self._activation

    def _migration_required_locked(self) -> bool | None:
        """Whether reaching the target migrates data.  A plan, not a state."""

        recorded = flag(self._backup.migration_required)
        return recorded if recorded is not None else flag(self._preview.migration_required)

    def _data_migrated_locked(self) -> bool | None:
        """Whether the data now in place has already been migrated.

        Distinct from the plan above, and it is this answer that decides
        whether running the previous application is still correct.  Nothing
        has started the new server before the probe, so nothing is migrated
        yet; a verified restore of the pre-migration archive puts it back.
        """

        if self._restored_pre_migration:
            return False
        if self._index <= self._order.probe:
            return False
        return self._migration_required_locked()

    def _activation_held_locked(self) -> bool:
        """Whether this flow's activation is in the registry, pending or in force."""

        return self._activation is not None

    def _restart_pending_locked(self) -> bool:
        """Whether the retained receipt is still waiting for its restart."""

        anchor = self._activation
        return anchor is not None and anchor.kind == KIND_ACTIVATION_PENDING

    def _snapshot_locked(self) -> RemoteUpdateSnapshot:
        assert self._code in CODES, self._code
        actions = self._actions_locked()
        assert all(action in ACTIONS for action in actions), actions
        owner = self._owner
        return RemoteUpdateSnapshot(
            schema_version=SCHEMA_VERSION,
            stage=self._stage.value,
            code=self._code,
            versions={name: getattr(self._versions, name) for name in VERSION_FIELDS},
            owner={
                "state": member(owner.state, OWNER_STATES),
                "token_available": flag(owner.token_available),
                "pidfd_available": flag(owner.pidfd_available),
                "process_exit": member(owner.process_exit, OBSERVATIONS),
                "listener_release": member(owner.listener_release, OBSERVATIONS),
                "lease_release": member(owner.lease_release, OBSERVATIONS),
            },
            install={
                "capability": member(self._measured_locked("capability"), CAPABILITIES),
                "method": member(self._measured_locked("method"), METHODS),
            },
            backup={
                "status": member(self._backup.status, BACKUP_STATUSES),
                "migration_required": self._migration_required_locked(),
            },
            actions=actions,
        )

    def _measured_locked(self, field: str) -> str:
        """Prefer what preparation measured over what the preview predicted."""

        measured = getattr(self._prepare, field)
        return measured if measured != "unknown" else getattr(self._preview, f"install_{field}")

    def _actions_locked(self) -> tuple[str, ...]:
        if self._stage is RemoteUpdateStage.READY:
            skill = self._skill_locked()
            return skill.actions() + (() if skill.state.in_flight else ("finish",))
        if self._stage is RemoteUpdateStage.CANCELLED:
            return ("dismiss", "inspect_diagnostics")
        if self._pending is not None:
            # Unsettled identity: only settling this one may be offered.
            return ("reconcile_pending", "inspect_diagnostics")
        if self._code in outcomes.RESTART_CODES:
            return ("restart_desktop",) + self._recovery_locked() + ("inspect_diagnostics",)
        if self._code == "restore_verified_activation_selected":
            return self._pairing_actions_locked()
        if self._code in outcomes.RECOVERY_SETTLED_CODES:
            # The recovery is done and the pairing it restored is consistent;
            # nothing here resumes the update, so only closing is left.
            return ("dismiss", "inspect_diagnostics")
        if self._code in outcomes.RECOVERY_CODES:
            return self._recovery_locked() + ("inspect_diagnostics",)
        if self._stage in (RemoteUpdateStage.FAILED, RemoteUpdateStage.UNKNOWN):
            return self._failed_actions_locked()
        next_action = self._order.action(self._index)
        return (next_action, "inspect_diagnostics") + self._escape_locked()

    def _pairing_actions_locked(self) -> tuple[str, ...]:
        """The data is back but the new activation is still selected.

        That pairing is exactly what must not be left behind, so this is not a
        settled recovery and ``dismiss`` is not offered.  Where the previous
        application was preserved the flow can put it back itself; where it
        was not, say so instead of pretending the recovery finished.
        """

        if self._rollback_available_locked():
            return ("rollback_activation", "inspect_diagnostics")
        return ("resolve_activation_pairing", "inspect_diagnostics")

    def _failed_actions_locked(self) -> tuple[str, ...]:
        first: tuple[str, ...] = ("retry_stage",)
        if self._code in outcomes.UNRECOVERABLE_CODES:
            # Nothing re-run changes the answer; where the question is owner
            # authority, say so instead of offering force.
            first = ("resolve_owner_authority",) if self._code.startswith("stop_") else ()
        return first + self._recovery_locked() + ("inspect_diagnostics",) + self._escape_locked()

    def _escape_locked(self) -> tuple[str, ...]:
        return () if self._activation_held_locked() else ("cancel",)

    def _recovery_locked(self) -> tuple[str, ...]:
        if self._pending is not None:
            return ()
        offered: list[str] = []
        if self._rollback_available_locked():
            offered.append("rollback_activation")
        if self._backup.status == "verified":
            offered.append("restore_backup")
        return tuple(offered)

    def _rollback_available_locked(self) -> bool:
        """Offer the previous app only where running it is still correct.

        Once the data in place has been migrated the previous app is old code
        against new data, so rollback stops being a recovery path at all and
        the only honest offer left is an explicit restore from the verified
        backup.  After that restore verifies, this becomes true again.
        """

        if self._activation_id is None or self._index < self._order.prepare:
            return False
        if self._data_migrated_locked() is not False:
            return False
        return self._retention == "verified"

    # -- drive -----------------------------------------------------------

    def advance(self) -> RemoteUpdateSnapshot:
        with self._lock:
            if self._pending is not None:
                return self._reconcile_locked()
            if self._stage in TERMINAL_STAGES:
                raise RemoteUpdateRefused("advance_refused_terminal")
            # Recovery from a settled failure, or an outcome nothing can
            # settle, is an explicit choice: advancing never re-issues a stage.
            if self._stage is RemoteUpdateStage.FAILED:
                raise RemoteUpdateRefused("advance_refused_failed")
            if self._stage is RemoteUpdateStage.UNKNOWN:
                raise RemoteUpdateRefused("advance_refused_unsettled")
            if self._restart_pending_locked():
                # Written and pending: only a restart that selects it moves on.
                raise RemoteUpdateRefused("advance_refused_restart_required")
            if self._cancel_requested and not self._activation_held_locked():
                return self._settle_cancelled_locked()
            return self._run_locked()

    def retry(self) -> RemoteUpdateSnapshot:
        with self._lock:
            if self._pending is not None or self._restart_pending_locked():
                raise RemoteUpdateRefused("retry_refused_pending_commit")
            if self._stage not in (RemoteUpdateStage.FAILED, RemoteUpdateStage.UNKNOWN):
                raise RemoteUpdateRefused("retry_refused_not_failed")
            if self._code in outcomes.UNRECOVERABLE_CODES:
                raise RemoteUpdateRefused("retry_refused_unrecoverable")
            if self._code in outcomes.RECOVERY_CODES:
                raise RemoteUpdateRefused("retry_refused_unrecoverable")
            if self._cancel_requested and not self._activation_held_locked():
                return self._settle_cancelled_locked()
            return self._run_locked()

    def reconcile(self) -> RemoteUpdateSnapshot:
        with self._lock:
            if self._pending is None:
                raise RemoteUpdateRefused("reconcile_refused_no_pending")
            return self._reconcile_locked()

    def resume_after_restart(self, evidence: StartupEvidence) -> RemoteUpdateSnapshot:
        """Continue the retained activation after the restart it required.

        The confirmation runs against the same recorded activation identity,
        never a fresh one, and only once the evidence says this restart
        actually selected that activation and the runtime it brought up serves
        the same workspace.  Anything short of that keeps the receipt pending.
        """

        with self._lock:
            if self._pending is not None:
                raise RemoteUpdateRefused("resume_refused_pending_commit")
            if self._activation is None or not self._restart_pending_locked():
                # Nothing retained, or already confirmed: a second mutation.
                raise RemoteUpdateRefused("resume_refused_no_pending_activation")
            activation_id = self._activation.operation_id
            decision = outcomes.classify_startup(evidence, activation_id)
            if decision is not None:
                return self._publish_locked(*decision)
            return self._issue_locked(KIND_ACTIVATION_CONFIRM, activation_id)

    def cancel(self) -> RemoteUpdateSnapshot:
        """Ask the flow to stop at the next boundary it may stop at."""

        with self._lock:
            if self._activation_held_locked():
                raise RemoteUpdateRefused("cancel_refused_activation_committed")
            self._cancel_requested = True
            if self._pending is not None:
                # Already out: abandoning it buries a possible commit.
                self._code = "cancel_deferred_commit_unknown"
                return self._snapshot_locked()
            if self._stage in TERMINAL_STAGES:
                return self._snapshot_locked()
            return self._settle_cancelled_locked()

    def restore(self) -> RemoteUpdateSnapshot:
        """Restore the verified backup through the supported restore path."""

        with self._lock:
            if self._pending is not None:
                # A mutation like any other: it would bury the unsettled one.
                raise RemoteUpdateRefused("restore_refused_pending_commit")
            if self._backup.status != "verified":
                raise RemoteUpdateRefused("restore_refused_backup_not_verified")
            return self._issue_locked(
                KIND_BACKUP_RESTORE, self._operation_ids(KIND_BACKUP_RESTORE)
            )

    def rollback(self) -> RemoteUpdateSnapshot:
        """Return to the preserved previous app, where that is still valid."""

        with self._lock:
            if self._pending is not None:
                raise RemoteUpdateRefused("rollback_refused_pending_commit")
            if self._data_migrated_locked() is not False:
                raise RemoteUpdateRefused("rollback_refused_migrated_ssot")
            if self._retention != "verified":
                raise RemoteUpdateRefused("rollback_refused_no_previous_app")
            if self._activation_id is None:
                raise RemoteUpdateRefused("rollback_refused_no_activation")
            return self._issue_locked(KIND_ACTIVATION_ROLLBACK, self._activation_id)

    # -- stage bodies ----------------------------------------------------

    def _run_locked(self) -> RemoteUpdateSnapshot:
        stage = self._order[self._index]
        return getattr(self, f"_run_{stage.value}_locked")()

    def _publish_locked(self, stage: RemoteUpdateStage, code: str) -> RemoteUpdateSnapshot:
        self._stage = stage
        self._code = code
        return self._snapshot_locked()

    def _progress_locked(self, stage: RemoteUpdateStage, code: str) -> RemoteUpdateSnapshot:
        self._index = self._order.index(stage) + 1
        return self._publish_locked(stage, code)

    def _run_preview_locked(self) -> RemoteUpdateSnapshot:
        try:
            facts = self._ports.preview.describe()
        except (LostResponse, PortRefusal):
            # The preview only reads, so a lost preview commits nothing.
            return self._publish_locked(RemoteUpdateStage.FAILED, "preview_failed")
        if not isinstance(facts, PreviewFacts):
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, "preview_unknown")
        self._preview = PreviewFacts(
            desktop_version=facts.desktop_version,
            remote_version=facts.remote_version,
            served_ui_version=facts.served_ui_version,
            protocol_version=facts.protocol_version,
            schema_version_before=facts.schema_version_before,
            schema_version_target=facts.schema_version_target,
            migration_required=flag(facts.migration_required),
            install_capability=member(facts.install_capability, CAPABILITIES),
            install_method=member(facts.install_method, METHODS),
        )
        self._absorb_versions_locked(
            desktop=facts.desktop_version,
            remote=facts.remote_version,
            served_ui=facts.served_ui_version,
            protocol=facts.protocol_version,
            schema=facts.schema_version_before,
        )
        return self._progress_locked(RemoteUpdateStage.PREVIEW, "preview_ready")

    def _run_stop_locked(self) -> RemoteUpdateSnapshot:
        return self._issue_locked(
            KIND_OWNER_STOP, self._operation_ids(RemoteUpdateStage.STOP.value)
        )

    def _run_backup_locked(self) -> RemoteUpdateSnapshot:
        return self._issue_locked(
            KIND_BACKUP_CREATE, self._operation_ids(RemoteUpdateStage.BACKUP.value)
        )

    def _run_prepare_locked(self) -> RemoteUpdateSnapshot:
        operation_id = self._operation_ids(RemoteUpdateStage.PREPARE.value)
        if self._order.prepare_before_stop:
            # Staging runs ahead of the stop, so there is no backup to gate
            # on: this writes an absent directory.  ``RunOrder`` holds why.
            return self._issue_locked(KIND_PREPARE_APPLY, operation_id, call=PREPARE_CODE_ONLY)
        if self._backup.status != "verified":
            # Nothing that could reach the new server runs without one.
            return self._publish_locked(
                RemoteUpdateStage.FAILED, "backup_not_verified_before_start"
            )
        return self._issue_locked(KIND_PREPARE_APPLY, operation_id)

    def _run_probe_locked(self) -> RemoteUpdateSnapshot:
        if self._backup.status != "verified":
            return self._publish_locked(
                RemoteUpdateStage.FAILED, "backup_not_verified_before_start"
            )
        operation_id = self._operation_ids(RemoteUpdateStage.PROBE.value)
        try:
            outcome = self._ports.probe.probe(operation_id)
        except LostResponse:
            # The probe reads the prepared app; a lost answer commits nothing.
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, "probe_unknown")
        except PortRefusal:
            return self._publish_locked(RemoteUpdateStage.FAILED, "probe_failed")
        if not isinstance(outcome, ProbeOutcome):
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, "probe_unknown")
        self._absorb_versions_locked(
            served_ui=outcome.served_ui_version, protocol=outcome.protocol_version
        )
        stage, code = outcomes.classify_probe(outcome)
        if code in outcomes.PROGRESS_CODES:
            return self._progress_locked(stage, code)
        return self._publish_locked(stage, code)

    def _run_activate_locked(self) -> RemoteUpdateSnapshot:
        operation_id = self._operation_ids(RemoteUpdateStage.ACTIVATE.value)
        self._activation_id = operation_id
        return self._issue_locked(KIND_ACTIVATION_ISSUE, operation_id)

    def _run_verify_locked(self) -> RemoteUpdateSnapshot:
        operation_id = self._operation_ids(RemoteUpdateStage.VERIFY.value)
        try:
            outcome = self._ports.verification.verify(operation_id)
        except LostResponse:
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, "verify_unknown")
        except PortRefusal:
            return self._publish_locked(RemoteUpdateStage.FAILED, "verify_failed")
        if not isinstance(outcome, VerifyOutcome):
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, "verify_unknown")
        self._absorb_versions_locked(
            desktop=outcome.desktop_version,
            remote=outcome.remote_version,
            served_ui=outcome.served_ui_version,
            protocol=outcome.protocol_version,
            schema=outcome.schema_version,
        )
        stage, code = outcomes.classify_verify(outcome)
        if stage is RemoteUpdateStage.READY:
            self._index = len(self._order)
            # The anchor has nothing left to recover, and a clear that raises
            # never reaches the hook: retiring a side record while the
            # activation is still recorded strands the next process.
            self._record_locked(None, None)
            self._completed_locked()
        return self._publish_locked(stage, code)

    def _completed_locked(self) -> None:
        """Tell the caller the attempt is finished, once its anchor is gone."""

        if self._on_completed is None:
            return
        try:
            self._on_completed()
        except Exception:  # noqa: BLE001 - the caller reports its own write
            pass

    def _absorb_versions_locked(self, **measured: object) -> None:
        """Keep omitted fields, but never present a new unknown as old evidence."""

        self._versions = MeasuredVersions(**{
            name: measured_version(name, measured[name]) if name in measured
            else getattr(self._versions, name)
            for name in VERSION_FIELDS
        })

    # -- mutation identity ----------------------------------------------

    def _record_locked(
        self, pending: PendingOperation | None, activation: PendingOperation | None
    ) -> None:
        self._pending = pending
        self._activation = activation
        if self._journal is not None:
            self._journal.record(
                tuple(entry for entry in (activation, pending) if entry is not None)
            )

    def _new_record_locked(self, kind: str, operation_id: str) -> PendingOperation:
        """One durable record, stamped with the fact a restart cannot re-read.

        Preparation measures the previous-app answer once and its identity is
        gone by the time recovery needs it, so the record carries it.
        """

        stage = MUTATIONS[kind].stage.value
        return PendingOperation(stage, operation_id, kind, self._retention)

    def _issue_locked(
        self, kind: str, operation_id: str, *, call: str = ""
    ) -> RemoteUpdateSnapshot:
        """Issue one mutating request, once, and publish how it settled.

        The identity is recorded under its semantic kind before the call, so a
        crash between the two still leaves a restart something to reconcile as
        the operation it actually is.  It is released only by an answer that
        says what happened; anything else keeps it recorded, which is what
        makes the next call a reconcile instead of a second issue.  ``call``
        only narrows which port call issues that kind, never the identity: a
        code-only staging and a full preparation are both PREPARE.
        """

        spec = MUTATIONS[kind]
        record = self._new_record_locked(kind, operation_id)
        self._record_locked(record, self._activation)
        port = getattr(self._ports, spec.port)
        try:
            outcome = getattr(port, call or spec.issue)(operation_id)
        except LostResponse:
            decision = (RemoteUpdateStage.UNKNOWN, spec.open_code)
        except PortRefusal:
            # Refused: nothing committed, so the identity is released.
            decision = (RemoteUpdateStage.FAILED, spec.refused_code)
        else:
            decision = (
                self._decide_locked(spec, outcome)
                if isinstance(outcome, spec.outcome)
                else (RemoteUpdateStage.UNKNOWN, spec.open_code)
            )
        return self._settle_locked(decision, operation_id)

    def _decide_locked(self, spec: MutationSpec, outcome: object) -> tuple:
        return getattr(self, f"_decide_{spec.classify}_locked")(outcome)

    def _decide_owner_stop_locked(self, facts: OwnerStopFacts) -> tuple:
        self._owner = facts
        return outcomes.classify_owner_stop(facts)

    def _decide_backup_create_locked(self, outcome: BackupOutcome) -> tuple:
        self._backup, stage, code = outcomes.classify_backup_create(
            outcome, flag(self._preview.migration_required)
        )
        return (stage, code)

    def _decide_prepare_apply_locked(self, outcome: PrepareOutcome) -> tuple:
        self._prepare, stage, code = outcomes.classify_prepare_apply(outcome)
        self._retention = outcomes.retention_of(self._prepare)
        return (stage, code)

    def _decide_activation_issue_locked(self, outcome: object) -> tuple:
        decision = outcomes.classify_activation_issue(outcome)
        if decision[1] == "activate_failed":
            # Nothing was written, so there is no receipt to hold or roll back.
            self._activation_id = None
        return decision

    def _decide_activation_confirm_locked(self, outcome: object) -> tuple:
        decision = outcomes.classify_activation_confirm(outcome)
        if decision[1] == "activate_committed":
            self._absorb_versions_locked(
                remote=outcome.remote_version, schema=outcome.schema_version
            )
        return decision

    def _decide_backup_restore_locked(self, outcome: object) -> tuple:
        decision = outcomes.classify_backup_restore(outcome)
        if decision[1] == "restore_verified":
            # The archive is back: the previous app is not old code now.
            self._restored_pre_migration = True
        return decision

    def _decide_activation_rollback_locked(self, outcome: object) -> tuple:
        decision = outcomes.classify_activation_rollback(outcome)
        if decision[1] == "rollback_verified":
            # The previous selection serves again: the activation is not.
            self._activation_id = None
        return decision

    def _settle_locked(
        self, decision: tuple[RemoteUpdateStage, str], operation_id: str
    ) -> RemoteUpdateSnapshot:
        stage, code = decision
        if code == "restore_verified" and self._activation_held_locked():
            # The data is back but the new activation is still selected, and
            # that pairing is the thing recovery exists to prevent.
            code = "restore_verified_activation_selected"
        keep = outcomes.RETAINED_KINDS.get(code)
        record = self._new_record_locked(keep, operation_id) if keep is not None else None
        if keep in ACTIVATION_ANCHOR_KINDS:
            # A written receipt is a registry fact, not a request in flight.
            self._record_locked(None, record)
        elif record is not None:
            self._record_locked(record, self._activation)
        else:
            self._record_locked(None, self._retained_after_locked(code))
        if code in outcomes.PROGRESS_CODES:
            return self._progress_locked(stage, code)
        return self._publish_locked(stage, code)

    def _retained_after_locked(self, code: str) -> PendingOperation | None:
        """Keep the retained receipt unless this code ended the receipt itself.

        A verified rollback closes it: the previous selection is back, so this
        flow no longer has an activation to recover.  The only other closing
        answer is a verified update, settled by the verification stage.  A
        refused confirmation closes nothing, and a committed one replaces the
        pending anchor with the confirmed one through ``RETAINED_KINDS``.
        """

        return None if code == "rollback_verified" else self._activation

    def _reconcile_locked(self) -> RemoteUpdateSnapshot:
        """Settle the recorded identity by asking, never by issuing again."""

        pending = self._pending
        assert pending is not None
        spec = MUTATIONS[pending.kind]
        try:
            observed = getattr(getattr(self._ports, spec.port), spec.observe)(
                pending.operation_id
            )
        except (LostResponse, PortRefusal):
            # Still unsettled: the identity stays recorded, nothing re-issued.
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, spec.open_code)
        if not isinstance(observed, spec.outcome):
            return self._publish_locked(RemoteUpdateStage.UNKNOWN, spec.open_code)
        published = self._settle_locked(
            self._decide_locked(spec, observed), pending.operation_id
        )
        if (
            self._cancel_requested
            and self._pending is None
            and not self._activation_held_locked()
        ):
            return self._settle_cancelled_locked()
        return published

    def _settle_cancelled_locked(self) -> RemoteUpdateSnapshot:
        """Cancel at the boundary before the stage that has not run yet."""

        stage = (
            self._order[self._index]
            if self._index < len(self._order)
            else RemoteUpdateStage.VERIFY
        )
        code = self._order.cancel_codes.get(stage)
        if code is None:
            raise RemoteUpdateRefused("cancel_refused_activation_committed")
        return self._publish_locked(RemoteUpdateStage.CANCELLED, code)
