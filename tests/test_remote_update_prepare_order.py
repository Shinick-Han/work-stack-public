"""The explicitly selected prepare-before-stop run order, and the old default.

These are lifecycle tests.  The ports below are synthetic fakes that record
every call and replay scripted answers; they are NOT the production ports and
nothing here demonstrates that a real host stages, backs up or activates
anything.  What they do demonstrate is which port calls this flow makes, in
which order, with which operation identity, and -- for the failure families --
which calls it never makes at all.

Two orders are covered.  The default is unchanged: preview, stop, backup,
prepare, probe, activate, verify.  ``prepare_before_stop=True`` stages the new
application code first, because the maintenance and observation helpers the
later stages need live inside that new tree.  The tests that matter most are
the ones proving that moving preparation earlier moved nothing else: no backup
is taken and nothing runs the staged code when staging fails or is lost, the
probe still refuses without a verified backup, and the journalled identity is
still one PREPARE reconciled through one observation.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_update_flow_contract as CONTRACT
from remote_update_flow import RemoteUpdateFlow
from remote_update_flow_contract import (
    ACTION_PREPARE_CODE_ONLY,
    KIND_ACTIVATION_PENDING,
    KIND_PREPARE_APPLY,
    ActivationOutcome,
    BackupOutcome,
    LostResponse,
    OwnerStopFacts,
    PendingOperation,
    PortRefusal,
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    RemoteUpdatePorts,
    RemoteUpdateRefused,
    StartupEvidence,
    VerifyOutcome,
)


PREVIEW_OK = PreviewFacts(
    desktop_version="1.0.14",
    remote_version="1.0.8",
    served_ui_version="1.0.8",
    protocol_version="7",
    schema_version_before="v5",
    schema_version_target="v6",
    migration_required=True,
    install_capability="available",
    install_method="verified_unpack",
)
STOP_OK = OwnerStopFacts(
    state="dead",
    token_available=True,
    pidfd_available=True,
    process_exit="verified",
    listener_release="verified",
    lease_release="verified",
)
BACKUP_OK = BackupOutcome(status="verified", migration_required=True)
# Staging keeps the current application and profile exactly where they are;
# that is what makes the answers below the flow's proof a recovery still exists.
PREPARE_OK = PrepareOutcome(
    status="verified",
    capability="available",
    method="verified_unpack",
    previous_app_retained=True,
    previous_profile_retained=True,
)
PROBE_OK = ProbeOutcome(
    status="verified",
    workspace_match="verified",
    served_ui_version="1.0.14",
    protocol_version="7",
)
ACTIVATE_OK = ActivationOutcome(
    status="verified", committed=True, state="pending", restart_required=True
)
CONFIRM_OK = ActivationOutcome(
    status="verified",
    committed=True,
    state="confirmed",
    remote_version="1.0.14",
    schema_version="v6",
)
VERIFY_OK = VerifyOutcome(
    status="verified",
    desktop_version="1.0.14",
    remote_version="1.0.14",
    served_ui_version="1.0.14",
    protocol_version="7",
    schema_version="v6",
)

# The port calls that reach the SSOT, take the archive, or run the staged code.
# No prepare-first test may see one of these before its stage legitimately ran.
GUARDED_CALLS = (
    "backup.create_verified",
    "backup.restore",
    "probe.probe",
    "activation.activate",
    "activation.confirm",
    "verification.verify",
)


class Script:
    """One port method: records every call and replays scripted results."""

    def __init__(self, calls: list[tuple[str, str]], name: str, results: object) -> None:
        self._calls = calls
        self._name = name
        self._results = list(results)

    def take(self, operation_id: str) -> object:
        self._calls.append((self._name, operation_id))
        if not self._results:
            raise AssertionError(f"unscripted call to {self._name}")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakePreview:
    def __init__(self, calls: list[tuple[str, str]], results: object) -> None:
        self.describe_script = Script(calls, "preview.describe", results)

    def describe(self) -> object:
        return self.describe_script.take("")


class FakeOwner:
    def __init__(self, calls: list[tuple[str, str]], stop: object, observe: object) -> None:
        self.stop_script = Script(calls, "owner.stop", stop)
        self.observe_script = Script(calls, "owner.observe", observe)

    def stop(self, operation_id: str) -> object:
        return self.stop_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)


class FakeBackup:
    def __init__(self, calls: list[tuple[str, str]], create: object, observe: object) -> None:
        self.create_script = Script(calls, "backup.create_verified", create)
        self.observe_script = Script(calls, "backup.observe", observe)
        self.restore_script = Script(calls, "backup.restore", ())
        self.observe_restore_script = Script(calls, "backup.observe_restore", ())

    def create_verified(self, operation_id: str) -> object:
        return self.create_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)

    def restore(self, operation_id: str) -> object:
        return self.restore_script.take(operation_id)

    def observe_restore(self, operation_id: str) -> object:
        return self.observe_restore_script.take(operation_id)


class FullPrepare:
    """The prepare port as it exists today: no code-only staging call."""

    def __init__(self, calls: list[tuple[str, str]], prepare: object, observe: object) -> None:
        self.prepare_script = Script(calls, "prepare.prepare", prepare)
        self.observe_script = Script(calls, "prepare.observe", observe)

    def prepare(self, operation_id: str) -> object:
        return self.prepare_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)


class CodeOnlyPrepare(FullPrepare):
    """A prepare port that can also stage the new application code alone."""

    def __init__(
        self,
        calls: list[tuple[str, str]],
        prepare: object,
        observe: object,
        code_only: object,
    ) -> None:
        super().__init__(calls, prepare, observe)
        self.code_only_script = Script(calls, "prepare.prepare_code_only", code_only)

    def prepare_code_only(self, operation_id: str) -> object:
        return self.code_only_script.take(operation_id)


class FakeProbe:
    def __init__(self, calls: list[tuple[str, str]], probe: object) -> None:
        self.probe_script = Script(calls, "probe.probe", probe)

    def probe(self, operation_id: str) -> object:
        return self.probe_script.take(operation_id)


class FakeActivation:
    def __init__(self, calls: list[tuple[str, str]], activate: object, confirm: object) -> None:
        self.activate_script = Script(calls, "activation.activate", activate)
        self.observe_script = Script(calls, "activation.observe", ())
        self.confirm_script = Script(calls, "activation.confirm", confirm)
        self.observe_confirm_script = Script(calls, "activation.observe_confirm", ())
        self.rollback_script = Script(calls, "activation.rollback", ())
        self.observe_rollback_script = Script(calls, "activation.observe_rollback", ())

    def activate(self, operation_id: str) -> object:
        return self.activate_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)

    def confirm(self, operation_id: str) -> object:
        return self.confirm_script.take(operation_id)

    def observe_confirm(self, operation_id: str) -> object:
        return self.observe_confirm_script.take(operation_id)

    def rollback(self, operation_id: str) -> object:
        return self.rollback_script.take(operation_id)

    def observe_rollback(self, operation_id: str) -> object:
        return self.observe_rollback_script.take(operation_id)


class FakeVerification:
    def __init__(self, calls: list[tuple[str, str]], verify: object) -> None:
        self.verify_script = Script(calls, "verification.verify", verify)

    def verify(self, operation_id: str) -> object:
        return self.verify_script.take(operation_id)


class FakeJournal:
    """The durable record, holding identities the way the real port does."""

    def __init__(self, entries: object = ()) -> None:
        self.entries = tuple(entries)
        self.writes: list[tuple[object, ...]] = []

    def load(self) -> tuple[object, ...]:
        return self.entries

    def record(self, entries: object) -> None:
        self.entries = tuple(entries)
        self.writes.append(self.entries)

    @property
    def kinds(self) -> list[str | None]:
        return [write[-1].kind if write else None for write in self.writes]


class Harness:
    """A flow plus the fakes behind it, wired for one scenario and one order."""

    def __init__(
        self,
        *,
        prepare_before_stop: bool = False,
        journal: FakeJournal | None = None,
        code_only_port: bool = True,
        **scripts: object,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.prepare = self._prepare_port(code_only_port, scripts)
        self.journal = journal
        self.issued: list[str] = []
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=FakePreview(self.calls, scripts.get("preview", (PREVIEW_OK,))),
                owner=FakeOwner(
                    self.calls,
                    scripts.get("stop", (STOP_OK,)),
                    scripts.get("stop_observe", ()),
                ),
                backup=FakeBackup(
                    self.calls,
                    scripts.get("backup", (BACKUP_OK,)),
                    scripts.get("backup_observe", ()),
                ),
                prepare=self.prepare,
                probe=FakeProbe(self.calls, scripts.get("probe", (PROBE_OK,))),
                activation=FakeActivation(
                    self.calls,
                    scripts.get("activate", (ACTIVATE_OK,)),
                    scripts.get("confirm", (CONFIRM_OK,)),
                ),
                verification=FakeVerification(
                    self.calls, scripts.get("verify", (VERIFY_OK,))
                ),
            ),
            operation_ids=self._operation_id,
            journal=journal,
            prepare_before_stop=prepare_before_stop,
        )

    def _prepare_port(self, code_only: bool, scripts: dict) -> FullPrepare:
        full = scripts.get("prepare", (PREPARE_OK,))
        observe = scripts.get("prepare_observe", ())
        if not code_only:
            return FullPrepare(self.calls, full, observe)
        return CodeOnlyPrepare(
            self.calls, full, observe, scripts.get("code_only", (PREPARE_OK,))
        )

    def _operation_id(self, stage: str) -> str:
        issue = f"op-{stage}-{1 + sum(1 for name in self.issued if name == stage)}"
        self.issued.append(stage)
        return issue

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def run(self, steps: int) -> list[str]:
        return [self.flow.advance().code for _ in range(steps)]

    def resume(self, **fields: object) -> object:
        evidence = {
            "selected_activation_id": "op-activate-1",
            "authority_selected": "verified",
            "workspace_match": "verified",
        }
        evidence.update(fields)
        return self.flow.resume_after_restart(StartupEvidence(**evidence))

    def run_to_ready(self) -> list[str]:
        """Preview through verify, including the restart the activation needs."""

        codes = self.run(6)
        codes.append(self.resume().code)
        codes.append(self.flow.advance().code)
        return codes


def prepare_first(**scripts: object) -> Harness:
    return Harness(prepare_before_stop=True, **scripts)


HAPPY_CODES = [
    "preview_ready",
    "stop_verified",
    "backup_verified",
    "prepare_ready",
    "probe_verified",
    "activate_pending_restart",
    "activate_committed",
    "update_ready",
]

PREPARE_FIRST_CODES = [
    "preview_ready",
    "prepare_ready",
    "stop_verified",
    "backup_verified",
    "probe_verified",
    "activate_pending_restart",
    "activate_committed",
    "update_ready",
]


class DefaultOrderUnchangedTests(unittest.TestCase):
    """The old order is the default and nothing about it moved."""

    def test_default_flow_still_runs_the_original_stage_order(self) -> None:
        harness = Harness()
        self.assertEqual(harness.run_to_ready(), HAPPY_CODES)
        self.assertEqual(
            harness.names,
            [
                "preview.describe",
                "owner.stop",
                "backup.create_verified",
                "prepare.prepare",
                "probe.probe",
                "activation.activate",
                "activation.confirm",
                "verification.verify",
            ],
        )

    def test_default_flow_offers_the_original_actions(self) -> None:
        harness = Harness()
        offered = [harness.flow.snapshot().actions[0]]
        for _ in range(4):
            harness.flow.advance()
            offered.append(harness.flow.snapshot().actions[0])
        self.assertEqual(
            offered,
            ["run_preview", "run_stop", "run_backup", "run_prepare", "run_probe"],
        )
        self.assertNotIn(ACTION_PREPARE_CODE_ONLY, offered)

    def test_default_flow_never_calls_the_code_only_staging(self) -> None:
        harness = Harness()
        harness.run_to_ready()
        self.assertNotIn("prepare.prepare_code_only", harness.names)

    def test_default_flow_accepts_a_port_without_code_only_staging(self) -> None:
        harness = Harness(code_only_port=False)
        self.assertEqual(harness.run_to_ready(), HAPPY_CODES)

    def test_default_prepare_still_refuses_without_a_verified_backup(self) -> None:
        harness = Harness(backup=(BackupOutcome(status="failed"),))
        self.assertEqual(harness.run(3), ["preview_ready", "stop_verified", "backup_failed"])
        with self.assertRaises(RemoteUpdateRefused):
            harness.flow.advance()
        self.assertNotIn("prepare.prepare", harness.names)

    def test_default_cancellation_names_the_stage_of_its_own_order(self) -> None:
        harness = Harness()
        harness.run(1)
        self.assertEqual(harness.flow.cancel().code, "cancelled_before_stop")


class PrepareFirstOrderTests(unittest.TestCase):
    """The explicitly selected order stages code first and nothing else moves."""

    def test_prepare_first_flow_reaches_ready_through_the_new_order(self) -> None:
        harness = prepare_first()
        self.assertEqual(harness.run_to_ready(), PREPARE_FIRST_CODES)
        self.assertEqual(
            harness.names,
            [
                "preview.describe",
                "prepare.prepare_code_only",
                "owner.stop",
                "backup.create_verified",
                "probe.probe",
                "activation.activate",
                "activation.confirm",
                "verification.verify",
            ],
        )

    def test_prepare_first_staging_never_calls_the_full_preparation(self) -> None:
        harness = prepare_first()
        harness.run_to_ready()
        self.assertNotIn("prepare.prepare", harness.names)

    def test_prepare_first_offers_its_own_code_only_action(self) -> None:
        harness = prepare_first()
        self.assertEqual(harness.flow.snapshot().actions[0], "run_preview")
        harness.run(1)
        self.assertEqual(harness.flow.snapshot().actions[0], ACTION_PREPARE_CODE_ONLY)
        harness.run(1)
        self.assertEqual(harness.flow.snapshot().actions[0], "run_stop")

    def test_staging_happens_before_any_stop_backup_or_probe(self) -> None:
        harness = prepare_first()
        harness.run(2)
        self.assertEqual(
            harness.names, ["preview.describe", "prepare.prepare_code_only"]
        )
        self.assertEqual(harness.flow.snapshot().backup["status"], "not_run")

    def test_prepare_first_cancellation_names_the_stage_of_its_own_order(self) -> None:
        harness = prepare_first()
        harness.run(1)
        self.assertEqual(harness.flow.cancel().code, "cancelled_before_prepare")

    def test_prepare_first_publishes_only_the_bounded_vocabulary(self) -> None:
        harness = prepare_first()
        for code in harness.run_to_ready():
            self.assertIn(code, CONTRACT.CODES)
        snapshot = harness.flow.snapshot()
        self.assertEqual(snapshot.schema_version, "remote-update-view/1")
        self.assertLessEqual(set(snapshot.actions), CONTRACT.ACTIONS)

    def test_every_offered_action_of_the_new_order_is_in_the_contract(self) -> None:
        harness = prepare_first()
        seen: set[str] = set(harness.flow.snapshot().actions)
        for _ in range(6):
            harness.flow.advance()
            seen.update(harness.flow.snapshot().actions)
        self.assertIn(ACTION_PREPARE_CODE_ONLY, seen)
        self.assertLessEqual(seen, CONTRACT.ACTIONS)


class StagingCannotOpenTheGateTests(unittest.TestCase):
    """A staging that did not verify takes no backup and runs no new code."""

    def assert_nothing_guarded_ran(self, harness: Harness) -> None:
        for name in GUARDED_CALLS:
            self.assertNotIn(name, harness.names)

    def test_failed_staging_stops_before_the_stop_and_the_backup(self) -> None:
        harness = prepare_first(code_only=(PrepareOutcome(status="failed"),))
        self.assertEqual(harness.run(2), ["preview_ready", "prepare_failed"])
        with self.assertRaises(RemoteUpdateRefused):
            harness.flow.advance()
        self.assertNotIn("owner.stop", harness.names)
        self.assert_nothing_guarded_ran(harness)

    def test_refused_staging_stops_before_the_stop_and_the_backup(self) -> None:
        harness = prepare_first(code_only=(PortRefusal("denied"),))
        self.assertEqual(harness.run(2), ["preview_ready", "prepare_failed"])
        self.assert_nothing_guarded_ran(harness)

    def test_lost_staging_keeps_the_identity_and_takes_no_backup(self) -> None:
        harness = prepare_first(code_only=(LostResponse("op-prepare-1"),))
        self.assertEqual(harness.run(2), ["preview_ready", "prepare_commit_unknown"])
        self.assertEqual(harness.flow.snapshot().stage, "unknown")
        pending = harness.flow.pending_operation()
        self.assertEqual((pending.kind, pending.stage), (KIND_PREPARE_APPLY, "prepare"))
        self.assert_nothing_guarded_ran(harness)

    def test_a_lost_staging_is_reconciled_never_issued_again(self) -> None:
        harness = prepare_first(
            code_only=(LostResponse("op-prepare-1"),), prepare_observe=(PREPARE_OK,)
        )
        harness.run(2)
        self.assertEqual(harness.flow.advance().code, "prepare_ready")
        self.assertEqual(
            harness.calls,
            [
                ("preview.describe", ""),
                ("prepare.prepare_code_only", "op-prepare-1"),
                ("prepare.observe", "op-prepare-1"),
            ],
        )
        self.assertIsNone(harness.flow.pending_operation())

    def test_capability_unavailable_staging_takes_no_backup(self) -> None:
        harness = prepare_first(
            code_only=(PrepareOutcome(status="verified", capability="unavailable"),)
        )
        self.assertEqual(harness.run(2), ["preview_ready", "prepare_capability_unavailable"])
        self.assert_nothing_guarded_ran(harness)

    def test_staging_that_lost_the_current_app_takes_no_backup(self) -> None:
        harness = prepare_first(
            code_only=(
                PrepareOutcome(
                    status="verified",
                    capability="available",
                    previous_app_retained=False,
                    previous_profile_retained=True,
                ),
            )
        )
        self.assertEqual(harness.run(2), ["preview_ready", "previous_app_not_preserved"])
        self.assert_nothing_guarded_ran(harness)
        with self.assertRaises(RemoteUpdateRefused):
            harness.flow.retry()

    def test_staging_with_an_unknown_retention_answer_takes_no_backup(self) -> None:
        harness = prepare_first(
            code_only=(PrepareOutcome(status="verified", capability="available"),)
        )
        self.assertEqual(
            harness.run(2), ["preview_ready", "previous_app_preservation_unknown"]
        )
        self.assert_nothing_guarded_ran(harness)

    def test_a_failed_stop_after_staging_never_reaches_the_backup(self) -> None:
        harness = prepare_first(stop=(OwnerStopFacts(state="live", token_available=True),))
        self.assertEqual(
            harness.run(3), ["preview_ready", "prepare_ready", "stop_owner_still_live"]
        )
        self.assertNotIn("backup.create_verified", harness.names)
        self.assertNotIn("probe.probe", harness.names)

    def test_a_failed_backup_after_staging_never_reaches_the_probe(self) -> None:
        harness = prepare_first(backup=(BackupOutcome(status="failed"),))
        self.assertEqual(harness.run(4)[-1], "backup_failed")
        with self.assertRaises(RemoteUpdateRefused):
            harness.flow.advance()
        self.assertNotIn("probe.probe", harness.names)
        self.assertNotIn("activation.activate", harness.names)


class ConstructionGateTests(unittest.TestCase):
    """A prepare-first flow is refused when its port cannot stage code only."""

    def test_prepare_first_requires_a_code_only_prepare_port(self) -> None:
        with self.assertRaises(ValueError) as raised:
            Harness(prepare_before_stop=True, code_only_port=False)
        self.assertIn("prepare_code_only", str(raised.exception))

    def test_a_refused_construction_touched_no_port(self) -> None:
        calls: list[tuple[str, str]] = []
        ports = RemoteUpdatePorts(
            preview=FakePreview(calls, (PREVIEW_OK,)),
            owner=FakeOwner(calls, (STOP_OK,), ()),
            backup=FakeBackup(calls, (BACKUP_OK,), ()),
            prepare=FullPrepare(calls, (PREPARE_OK,), ()),
            probe=FakeProbe(calls, (PROBE_OK,)),
            activation=FakeActivation(calls, (ACTIVATE_OK,), (CONFIRM_OK,)),
            verification=FakeVerification(calls, (VERIFY_OK,)),
        )
        with self.assertRaises(ValueError):
            RemoteUpdateFlow(
                ports, operation_ids=lambda stage: stage, prepare_before_stop=True
            )
        self.assertEqual(calls, [])


class JournalledIdentityTests(unittest.TestCase):
    """One PREPARE identity, journalled and resumed correctly in each order."""

    def test_staging_journals_the_existing_prepare_kind(self) -> None:
        journal = FakeJournal()
        harness = prepare_first(journal=journal, code_only=(LostResponse("op-prepare-1"),))
        harness.run(2)
        self.assertEqual(journal.kinds, [KIND_PREPARE_APPLY, KIND_PREPARE_APPLY])
        self.assertEqual(journal.entries[-1].operation_id, "op-prepare-1")

    def test_a_resumed_staging_does_not_pretend_a_backup_verified(self) -> None:
        journal = FakeJournal((PendingOperation("prepare", "op-prepare-1", KIND_PREPARE_APPLY),))
        harness = prepare_first(journal=journal, prepare_observe=(PREPARE_OK,))
        snapshot = harness.flow.snapshot()
        self.assertEqual(snapshot.stage, "unknown")
        self.assertEqual(snapshot.code, "prepare_commit_unknown")
        self.assertEqual(snapshot.backup["status"], "not_run")
        self.assertEqual(harness.flow.reconcile().code, "prepare_ready")
        self.assertEqual(harness.flow.snapshot().actions[0], "run_stop")

    def test_the_same_record_in_the_default_order_resumes_after_the_backup(self) -> None:
        journal = FakeJournal((PendingOperation("prepare", "op-prepare-1", KIND_PREPARE_APPLY),))
        harness = Harness(journal=journal, prepare_observe=(PREPARE_OK,))
        self.assertEqual(harness.flow.snapshot().backup["status"], "verified")
        self.assertEqual(harness.flow.reconcile().code, "prepare_ready")
        self.assertEqual(harness.flow.snapshot().actions[0], "run_probe")

    def test_a_resumed_activation_reconstructs_the_backup_in_the_new_order(self) -> None:
        journal = FakeJournal(
            (PendingOperation("activate", "op-activate-1", KIND_ACTIVATION_PENDING, "verified"),)
        )
        harness = prepare_first(journal=journal)
        snapshot = harness.flow.snapshot()
        self.assertEqual(snapshot.stage, "activate")
        self.assertEqual(snapshot.code, "activate_pending_restart")
        self.assertEqual(snapshot.backup["status"], "verified")
        self.assertEqual(snapshot.actions[0], "restart_desktop")

    def test_a_resumed_staging_offers_no_rollback_before_any_activation(self) -> None:
        journal = FakeJournal((PendingOperation("prepare", "op-prepare-1", KIND_PREPARE_APPLY),))
        harness = prepare_first(journal=journal, prepare_observe=(PREPARE_OK,))
        harness.flow.reconcile()
        self.assertNotIn("rollback_activation", harness.flow.snapshot().actions)
        with self.assertRaises(RemoteUpdateRefused):
            harness.flow.rollback()

    def test_a_verified_prepare_first_update_clears_the_record(self) -> None:
        journal = FakeJournal()
        harness = prepare_first(journal=journal)
        self.assertEqual(harness.run_to_ready(), PREPARE_FIRST_CODES)
        self.assertEqual(journal.entries, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
