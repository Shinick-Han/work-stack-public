from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_update_flow_contract as CONTRACT
from remote_update_flow import RemoteUpdateFlow
from remote_update_flow_contract import (
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
    RestoreOutcome,
    RollbackOutcome,
    StartupEvidence,
    VerifyOutcome,
)


PREVIEW_OK = PreviewFacts(
    desktop_version="1.0.14",
    remote_version="1.0.13",
    served_ui_version="1.0.13",
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
# A successful activation is a *pending* receipt plus the restart it requires.
ACTIVATE_OK = ActivationOutcome(
    status="verified", committed=True, state="pending", restart_required=True
)
# Only the confirmation of that same receipt puts the activation in force, and
# it is the confirmation that carries the versions now being served.
CONFIRM_OK = ActivationOutcome(
    status="verified",
    committed=True,
    state="confirmed",
    remote_version="1.0.14",
    schema_version="v6",
)
RESTORE_OK = RestoreOutcome(status="verified", pre_migration_data=True)
ROLLBACK_OK = RollbackOutcome(status="verified", previous_activation_selected=True)
VERIFY_OK = VerifyOutcome(
    status="verified",
    desktop_version="1.0.14",
    remote_version="1.0.14",
    served_ui_version="1.0.14",
    protocol_version="7",
    schema_version="v6",
)

HAPPY_CODES = (
    "preview_ready",
    "stop_verified",
    "backup_verified",
    "prepare_ready",
    "probe_verified",
    "activate_pending_restart",
    "activate_committed",
    "update_ready",
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
    def __init__(
        self, calls: list[tuple[str, str]], stop: object = (), observe: object = ()
    ) -> None:
        self.stop_script = Script(calls, "owner.stop", stop)
        self.observe_script = Script(calls, "owner.observe", observe)

    def stop(self, operation_id: str) -> object:
        return self.stop_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)


class FakeBackup:
    def __init__(
        self,
        calls: list[tuple[str, str]],
        create: object = (),
        observe: object = (),
        restore: object = (),
        observe_restore: object = (),
    ) -> None:
        self.create_script = Script(calls, "backup.create_verified", create)
        self.observe_script = Script(calls, "backup.observe", observe)
        self.restore_script = Script(calls, "backup.restore", restore)
        self.observe_restore_script = Script(
            calls, "backup.observe_restore", observe_restore
        )

    def create_verified(self, operation_id: str) -> object:
        return self.create_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)

    def restore(self, operation_id: str) -> object:
        return self.restore_script.take(operation_id)

    def observe_restore(self, operation_id: str) -> object:
        return self.observe_restore_script.take(operation_id)


class FakePrepare:
    def __init__(
        self, calls: list[tuple[str, str]], prepare: object = (), observe: object = ()
    ) -> None:
        self.prepare_script = Script(calls, "prepare.prepare", prepare)
        self.observe_script = Script(calls, "prepare.observe", observe)

    def prepare(self, operation_id: str) -> object:
        return self.prepare_script.take(operation_id)

    def observe(self, operation_id: str) -> object:
        return self.observe_script.take(operation_id)


class FakeProbe:
    def __init__(self, calls: list[tuple[str, str]], probe: object = ()) -> None:
        self.probe_script = Script(calls, "probe.probe", probe)

    def probe(self, operation_id: str) -> object:
        return self.probe_script.take(operation_id)


class FakeActivation:
    def __init__(
        self,
        calls: list[tuple[str, str]],
        activate: object = (),
        observe: object = (),
        confirm: object = (),
        observe_confirm: object = (),
        rollback: object = (),
        observe_rollback: object = (),
    ) -> None:
        self.activate_script = Script(calls, "activation.activate", activate)
        self.observe_script = Script(calls, "activation.observe", observe)
        self.confirm_script = Script(calls, "activation.confirm", confirm)
        self.observe_confirm_script = Script(
            calls, "activation.observe_confirm", observe_confirm
        )
        self.rollback_script = Script(calls, "activation.rollback", rollback)
        self.observe_rollback_script = Script(
            calls, "activation.observe_rollback", observe_rollback
        )

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
    def __init__(self, calls: list[tuple[str, str]], verify: object = ()) -> None:
        self.verify_script = Script(calls, "verification.verify", verify)

    def verify(self, operation_id: str) -> object:
        return self.verify_script.take(operation_id)


class FakeJournal:
    """The durable record, holding identities the way the real port does."""

    def __init__(self, entries: object = ()) -> None:
        self.entries = self._normalize(entries)
        self.writes: list[tuple[object, ...]] = []

    @staticmethod
    def _normalize(entries: object) -> tuple[object, ...]:
        if entries is None:
            return ()
        if isinstance(entries, (tuple, list)):
            return tuple(entries)
        return (entries,)

    def load(self) -> tuple[object, ...]:
        return self.entries

    def record(self, entries: object) -> None:
        self.entries = self._normalize(entries)
        self.writes.append(self.entries)

    def _recorded(self, retained: bool) -> object:
        for entry in self.entries:
            if not isinstance(entry, PendingOperation):
                continue
            if (entry.kind in CONTRACT.ACTIVATION_ANCHOR_KINDS) is retained:
                return entry
        return None

    @property
    def pending(self) -> object:
        """The identity in flight, if the record holds one."""

        return self._recorded(False)

    @property
    def activation(self) -> object:
        """The retained activation receipt, pending or confirmed."""

        return self._recorded(True)

    @property
    def kinds(self) -> list[str]:
        return [write[-1].kind if write else None for write in self.writes]


class Harness:
    """A flow plus the fakes behind it, wired for one scenario."""

    def __init__(self, journal: FakeJournal | None = None, **scripts: object) -> None:
        self.calls: list[tuple[str, str]] = []
        self.preview = FakePreview(self.calls, scripts.get("preview", (PREVIEW_OK,)))
        self.owner = FakeOwner(
            self.calls,
            stop=scripts.get("stop", (STOP_OK,)),
            observe=scripts.get("stop_observe", ()),
        )
        self.backup = FakeBackup(
            self.calls,
            create=scripts.get("backup", (BACKUP_OK,)),
            observe=scripts.get("backup_observe", ()),
            restore=scripts.get("restore", ()),
            observe_restore=scripts.get("restore_observe", ()),
        )
        self.prepare = FakePrepare(
            self.calls,
            prepare=scripts.get("prepare", (PREPARE_OK,)),
            observe=scripts.get("prepare_observe", ()),
        )
        self.probe = FakeProbe(self.calls, probe=scripts.get("probe", (PROBE_OK,)))
        self.activation = FakeActivation(
            self.calls,
            activate=scripts.get("activate", (ACTIVATE_OK,)),
            observe=scripts.get("activate_observe", ()),
            confirm=scripts.get("confirm", (CONFIRM_OK,)),
            observe_confirm=scripts.get("confirm_observe", ()),
            rollback=scripts.get("rollback", ()),
            observe_rollback=scripts.get("rollback_observe", ()),
        )
        self.verification = FakeVerification(
            self.calls, verify=scripts.get("verify", (VERIFY_OK,))
        )
        self.journal = journal
        self.issued: list[str] = []
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=self.preview,
                owner=self.owner,
                backup=self.backup,
                prepare=self.prepare,
                probe=self.probe,
                activation=self.activation,
                verification=self.verification,
            ),
            operation_ids=self._operation_id,
            journal=journal,
        )

    def _operation_id(self, stage: str) -> str:
        issue = f"op-{stage}-{1 + sum(1 for name in self.issued if name == stage)}"
        self.issued.append(stage)
        return issue

    def names(self, prefix: str) -> list[str]:
        return [name for name, _ in self.calls if name.startswith(prefix)]

    def run(self, steps: int) -> list[str]:
        return [self.flow.advance().code for _ in range(steps)]

    def startup(self, **fields: object) -> StartupEvidence:
        """Startup evidence that resumes the activation this harness issued."""

        evidence = {
            "selected_activation_id": "op-activate-1",
            "authority_selected": "verified",
            "workspace_match": "verified",
        }
        evidence.update(fields)
        return StartupEvidence(**evidence)

    def resume(self, **fields: object) -> object:
        return self.flow.resume_after_restart(self.startup(**fields))

    def run_to_ready(self) -> list[str]:
        """Preview through verify, including the restart the activation needs."""

        codes = self.run(6)
        codes.append(self.resume().code)
        codes.append(self.flow.advance().code)
        return codes


def confirmed_activation(journal: FakeJournal, **scripts: object) -> Harness:
    """A flow whose activation is confirmed, with every identity journaled."""

    harness = Harness(journal=journal, **scripts)
    harness.run(6)
    harness.resume()
    return harness


def failed_after_activation(**scripts: object) -> Harness:
    """A flow whose activation is in force and whose verification failed."""

    harness = Harness(verify=(VerifyOutcome(status="failed"),), **scripts)
    harness.run(6)
    harness.resume()
    harness.flow.advance()
    return harness


class SnapshotContractTests(unittest.TestCase):
    def test_full_flow_reaches_ready_in_stage_order(self) -> None:
        harness = Harness()
        self.assertEqual(harness.flow.snapshot().stage, "idle")
        self.assertEqual(harness.flow.snapshot().actions[0], "run_preview")

        codes = harness.run_to_ready()

        self.assertEqual(tuple(codes), HAPPY_CODES)
        final = harness.flow.snapshot()
        self.assertEqual(final.stage, "ready")
        self.assertEqual(final.code, "update_ready")
        self.assertEqual(final.actions, ("finish",))
        self.assertEqual(
            harness.names(""),
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

    def test_published_stage_names_match_each_step(self) -> None:
        harness = Harness()
        stages = [harness.flow.advance().stage for _ in range(6)]
        stages.append(harness.resume().stage)
        stages.append(harness.flow.advance().stage)
        self.assertEqual(
            stages,
            [
                "preview", "stop", "backup", "prepare", "probe",
                # The activation is issued, then held at its restart, and only
                # the confirmation of that same receipt moves the flow on.
                "activate", "activate", "ready",
            ],
        )

    def test_ready_document_carries_exactly_the_contract_keys(self) -> None:
        harness = Harness()
        harness.run_to_ready()

        document = harness.flow.snapshot().to_document()

        self.assertEqual(
            list(document),
            [
                "schema_version",
                "stage",
                "code",
                "versions",
                "owner",
                "install",
                "backup",
                "actions",
            ],
        )
        self.assertEqual(document["schema_version"], "remote-update-view/1")
        self.assertEqual(list(document["versions"]), list(CONTRACT.VERSION_FIELDS))
        self.assertEqual(
            document["versions"],
            {
                "desktop": "1.0.14",
                "remote": "1.0.14",
                "served_ui": "1.0.14",
                "protocol": "7",
                "schema": "v6",
            },
        )
        self.assertEqual(
            list(document["owner"]),
            [
                "state",
                "token_available",
                "pidfd_available",
                "process_exit",
                "listener_release",
                "lease_release",
            ],
        )
        self.assertEqual(
            document["owner"],
            {
                "state": "dead",
                "token_available": True,
                "pidfd_available": True,
                "process_exit": "verified",
                "listener_release": "verified",
                "lease_release": "verified",
            },
        )
        self.assertEqual(list(document["install"]), ["capability", "method"])
        self.assertEqual(
            document["install"], {"capability": "available", "method": "verified_unpack"}
        )
        self.assertEqual(list(document["backup"]), ["status", "migration_required"])
        self.assertEqual(
            document["backup"], {"status": "verified", "migration_required": True}
        )
        self.assertEqual(document["actions"], ["finish"])

    def test_snapshot_is_deterministic_for_one_condition(self) -> None:
        harness = Harness()
        harness.run(4)

        first = harness.flow.snapshot().to_document()
        second = harness.flow.snapshot().to_document()

        self.assertEqual(first, second)
        self.assertEqual(harness.flow.snapshot(), harness.flow.snapshot())

    def test_absent_facts_normalize_to_unknown(self) -> None:
        harness = Harness(preview=(PreviewFacts(),))

        snapshot = harness.flow.advance()

        document = snapshot.to_document()
        self.assertEqual(document["code"], "preview_ready")
        self.assertEqual(
            document["versions"],
            {
                "desktop": None,
                "remote": None,
                "served_ui": None,
                "protocol": None,
                "schema": None,
            },
        )
        self.assertEqual(
            document["owner"],
            {
                "state": "unknown",
                "token_available": None,
                "pidfd_available": None,
                "process_exit": "unknown",
                "listener_release": "unknown",
                "lease_release": "unknown",
            },
        )
        self.assertEqual(
            document["install"], {"capability": "unknown", "method": "unknown"}
        )
        self.assertEqual(
            document["backup"], {"status": "not_run", "migration_required": None}
        )

    def test_out_of_vocabulary_port_values_normalize_without_leaking(self) -> None:
        rogue_preview = PreviewFacts(
            desktop_version="1.0.14\nsecret-token",
            remote_version="v" * 200,
            served_ui_version="   ",
            protocol_version=7,
            schema_version_before="v5",
            install_capability="TOTALLY_AVAILABLE",
            install_method="rsync",
        )
        rogue_stop = OwnerStopFacts(
            state="probably-fine",
            token_available="yes",
            pidfd_available=1,
            process_exit="ok",
            listener_release="",
            lease_release="VERIFIED",
        )
        harness = Harness(preview=(rogue_preview,), stop=(rogue_stop,))

        harness.flow.advance()
        snapshot = harness.flow.advance()

        document = snapshot.to_document()
        self.assertEqual(
            document["versions"],
            {
                "desktop": None,
                "remote": None,
                "served_ui": None,
                "protocol": None,
                "schema": "v5",
            },
        )
        self.assertEqual(document["owner"]["state"], "unknown")
        self.assertIsNone(document["owner"]["token_available"])
        self.assertIsNone(document["owner"]["pidfd_available"])
        self.assertEqual(document["owner"]["process_exit"], "unknown")
        self.assertEqual(document["owner"]["lease_release"], "unknown")
        self.assertEqual(
            document["install"], {"capability": "unknown", "method": "unknown"}
        )
        # A token-availability answer that is not a boolean is unknown, and
        # unknown authority is never treated as permission to stop.
        self.assertEqual(document["code"], "stop_token_unknown")
        self.assertEqual(document["stage"], "unknown")

    def test_every_published_code_and_action_stays_inside_the_vocabulary(self) -> None:
        seen_codes: set[str] = set()
        seen_actions: set[str] = set()
        for scenario in _all_scenarios():
            for document in scenario:
                seen_codes.add(document["code"])
                seen_actions.update(document["actions"])
                self.assertEqual(document["schema_version"], "remote-update-view/1")
                self.assertIn(
                    document["stage"],
                    {stage.value for stage in CONTRACT.RemoteUpdateStage},
                )
        self.assertTrue(seen_codes <= CONTRACT.CODES, seen_codes - CONTRACT.CODES)
        self.assertTrue(seen_actions <= CONTRACT.ACTIONS, seen_actions - CONTRACT.ACTIONS)
        self.assertGreater(len(seen_codes), 20)

    def test_schema_transition_reports_before_target_and_migration(self) -> None:
        harness = Harness()
        harness.flow.advance()

        transition = harness.flow.schema_transition()

        self.assertEqual(transition.before, "v5")
        self.assertEqual(transition.target, "v6")
        self.assertIs(transition.migration_required, True)
        # The published document carries the schema in use, not the target.
        self.assertEqual(harness.flow.snapshot().to_document()["versions"]["schema"], "v5")

    def test_schema_transition_survives_activation_replacing_the_schema(self) -> None:
        harness = Harness()
        harness.run_to_ready()

        transition = harness.flow.schema_transition()

        self.assertEqual((transition.before, transition.target), ("v5", "v6"))
        self.assertEqual(harness.flow.snapshot().to_document()["versions"]["schema"], "v6")


class OwnerStopTests(unittest.TestCase):
    def _stopped(self, facts: OwnerStopFacts) -> dict[str, object]:
        harness = Harness(stop=(facts,))
        harness.flow.advance()
        return harness.flow.advance().to_document()

    def test_missing_session_token_is_refused_as_its_own_condition(self) -> None:
        document = self._stopped(replace(STOP_OK, token_available=False))

        self.assertEqual(document["stage"], "failed")
        self.assertEqual(document["code"], "stop_refused_missing_token")
        self.assertEqual(document["actions"][0], "resolve_owner_authority")
        self.assertNotIn("retry_stage", document["actions"])

    def test_unknown_token_availability_is_unknown_not_permission(self) -> None:
        document = self._stopped(replace(STOP_OK, token_available=None))

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "stop_token_unknown")

    def test_foreign_host_receipt_is_refused(self) -> None:
        document = self._stopped(replace(STOP_OK, state="foreign"))

        self.assertEqual(document["code"], "stop_refused_foreign_host")
        self.assertEqual(document["owner"]["state"], "foreign")

    def test_legacy_unfenced_receipt_is_its_own_refusal(self) -> None:
        document = self._stopped(replace(STOP_OK, state="unfenced"))

        self.assertEqual(document["code"], "stop_refused_unfenced_receipt")

    def test_owner_still_live_is_a_failure_not_a_stop(self) -> None:
        document = self._stopped(replace(STOP_OK, state="live"))

        self.assertEqual(document["code"], "stop_owner_still_live")
        self.assertEqual(document["stage"], "failed")

    def test_stopping_state_is_unknown_not_stopped(self) -> None:
        document = self._stopped(replace(STOP_OK, state="stopping"))

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "stop_incomplete_unknown")

    def test_an_absent_or_replaced_owner_is_unknown_and_never_dead(self) -> None:
        # An owner nobody can find is not an owner anybody watched exit.  Only
        # independent evidence may say dead, so every observation outside the
        # published vocabulary normalizes to unknown.
        for state in ("absent", "replaced", "ambiguous"):
            with self.subTest(state=state):
                document = self._stopped(replace(STOP_OK, state=state))

                self.assertEqual(document["owner"]["state"], "unknown")
                self.assertEqual(document["stage"], "unknown")
                self.assertEqual(document["code"], "stop_incomplete_unknown")

    def test_each_release_observation_is_required_independently(self) -> None:
        for field in ("process_exit", "listener_release", "lease_release"):
            with self.subTest(field=field, answer="failed"):
                document = self._stopped(replace(STOP_OK, **{field: "failed"}))
                self.assertEqual(document["code"], "stop_failed")
                self.assertEqual(document["stage"], "failed")
            with self.subTest(field=field, answer="unknown"):
                document = self._stopped(replace(STOP_OK, **{field: "unknown"}))
                self.assertEqual(document["code"], "stop_incomplete_unknown")
                self.assertEqual(document["stage"], "unknown")

    def test_absent_pidfd_is_named_and_does_not_block_a_verified_stop(self) -> None:
        document = self._stopped(replace(STOP_OK, pidfd_available=False))

        self.assertEqual(document["stage"], "stop")
        self.assertEqual(document["code"], "stop_verified_without_pidfd")
        self.assertIs(document["owner"]["pidfd_available"], False)
        self.assertEqual(document["actions"][0], "run_backup")

    def test_unknown_pidfd_with_three_verified_releases_is_a_verified_stop(self) -> None:
        document = self._stopped(replace(STOP_OK, pidfd_available=None))

        self.assertEqual(document["code"], "stop_verified")

    def test_no_condition_ever_offers_to_force_an_owner(self) -> None:
        forced = {"kill_owner", "force_stop", "delete_receipt", "override_owner"}
        self.assertEqual(CONTRACT.ACTIONS & forced, set())
        for state in ("live", "stopping", "dead", "foreign", "unfenced", "unknown"):
            for token in (True, False, None):
                document = self._stopped(
                    replace(STOP_OK, state=state, token_available=token)
                )
                self.assertEqual(set(document["actions"]) & forced, set())

    def test_stop_runs_before_backup_so_the_backup_is_quiescent(self) -> None:
        harness = Harness()
        harness.run(3)

        ordered = harness.names("")
        self.assertLess(
            ordered.index("owner.stop"), ordered.index("backup.create_verified")
        )


class BackupGateTests(unittest.TestCase):
    def test_failed_backup_stops_the_flow_before_preparation(self) -> None:
        harness = Harness(backup=(BackupOutcome(status="failed"),))
        harness.flow.advance()
        harness.flow.advance()

        document = harness.flow.advance().to_document()

        self.assertEqual(document["stage"], "failed")
        self.assertEqual(document["code"], "backup_failed")
        self.assertEqual(document["backup"]["status"], "failed")
        self.assertEqual(harness.names("prepare"), [])
        self.assertNotIn("restore_backup", document["actions"])

    def test_preparation_refuses_to_start_without_a_verified_backup(self) -> None:
        harness = Harness(
            backup=(BackupOutcome(status="unknown"),),
            backup_observe=(BackupOutcome(status="not_run"),),
        )
        harness.run(3)
        # Reconcile settles the unknown archive as never taken, then the gate
        # refuses everything that could reach the new server.
        harness.flow.advance()
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.advance()

        self.assertEqual(refused.exception.code, "advance_refused_failed")
        self.assertEqual(harness.flow.snapshot().code, "backup_not_run")
        self.assertEqual(harness.names("prepare"), [])
        self.assertEqual(harness.names("probe"), [])

    def test_gate_code_appears_when_a_retry_reaches_prepare_unbacked(self) -> None:
        # A prepare retry after the backup answer regressed to unknown must
        # still refuse: the gate is re-checked, not remembered as satisfied.
        harness = Harness(
            backup=(BACKUP_OK,),
            prepare=(PrepareOutcome(status="failed"),),
        )
        harness.run(4)
        self.assertEqual(harness.flow.snapshot().code, "prepare_failed")
        harness.flow._backup = BackupOutcome(status="unknown")

        document = harness.flow.retry().to_document()

        self.assertEqual(document["code"], "backup_not_verified_before_start")
        self.assertEqual(document["stage"], "failed")
        self.assertEqual(harness.names("prepare"), ["prepare.prepare"])

    def test_verified_backup_is_visible_before_the_first_start(self) -> None:
        harness = Harness()
        harness.run(3)

        document = harness.flow.snapshot().to_document()

        self.assertEqual(document["backup"]["status"], "verified")
        self.assertIs(document["backup"]["migration_required"], True)
        self.assertEqual(document["actions"][0], "run_prepare")
        self.assertEqual(harness.names("probe"), [])


class PreparationTests(unittest.TestCase):
    def _prepared(self, outcome: PrepareOutcome) -> dict[str, object]:
        harness = Harness(prepare=(outcome,))
        harness.run(3)
        self.harness = harness
        return harness.flow.advance().to_document()

    def test_a_target_that_dropped_the_previous_app_never_activates(self) -> None:
        document = self._prepared(replace(PREPARE_OK, previous_app_retained=False))

        self.assertEqual(document["stage"], "failed")
        self.assertEqual(document["code"], "previous_app_not_preserved")
        self.assertEqual(self.harness.names("activation"), [])
        self.assertNotIn("retry_stage", document["actions"])
        self.assertEqual(document["actions"][0], "restore_backup")

    def test_a_dropped_previous_profile_is_the_same_refusal(self) -> None:
        document = self._prepared(replace(PREPARE_OK, previous_profile_retained=False))

        self.assertEqual(document["code"], "previous_app_not_preserved")

    def test_unknown_retention_is_unknown_and_not_retried(self) -> None:
        document = self._prepared(replace(PREPARE_OK, previous_profile_retained=None))

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "previous_app_preservation_unknown")
        self.assertEqual(self.harness.names("activation"), [])
        with self.assertRaises(RemoteUpdateRefused) as refused:
            self.harness.flow.retry()
        self.assertEqual(refused.exception.code, "retry_refused_unrecoverable")

    def test_unavailable_install_capability_is_reported_as_measured(self) -> None:
        document = self._prepared(
            replace(PREPARE_OK, capability="unavailable", method="unknown")
        )

        self.assertEqual(document["code"], "prepare_capability_unavailable")
        self.assertEqual(document["install"]["capability"], "unavailable")

    def test_measured_method_replaces_the_previewed_one(self) -> None:
        document = self._prepared(replace(PREPARE_OK, method="transactional"))

        self.assertEqual(document["install"]["method"], "transactional")
        self.assertEqual(document["code"], "prepare_ready")


class ProbeTests(unittest.TestCase):
    def _probed(self, outcome: ProbeOutcome) -> dict[str, object]:
        harness = Harness(probe=(outcome,))
        harness.run(4)
        self.harness = harness
        return harness.flow.advance().to_document()

    def test_a_foreign_workspace_refuses_before_any_switch(self) -> None:
        document = self._probed(replace(PROBE_OK, workspace_match="failed"))

        self.assertEqual(document["stage"], "failed")
        self.assertEqual(document["code"], "probe_workspace_mismatch")
        self.assertEqual(self.harness.names("activation"), [])

    def test_an_unproven_workspace_is_unknown_not_a_match(self) -> None:
        document = self._probed(replace(PROBE_OK, workspace_match="unknown"))

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "probe_workspace_unknown")
        self.assertEqual(self.harness.names("activation"), [])

    def test_the_workspace_answer_outranks_the_probe_verdict(self) -> None:
        document = self._probed(
            ProbeOutcome(status="failed", workspace_match="failed")
        )

        self.assertEqual(document["code"], "probe_workspace_mismatch")

    def test_probe_versions_reach_the_snapshot(self) -> None:
        document = self._probed(PROBE_OK)

        self.assertEqual(document["code"], "probe_verified")
        self.assertEqual(document["versions"]["served_ui"], "1.0.14")

    def test_a_read_only_probe_is_retryable(self) -> None:
        harness = Harness(
            probe=(ProbeOutcome(status="failed", workspace_match="verified"), PROBE_OK)
        )
        harness.run(4)
        self.assertEqual(harness.flow.advance().code, "probe_failed")

        self.assertEqual(harness.flow.retry().code, "probe_verified")
        self.assertEqual(len(harness.names("probe")), 2)


class LostResponseTests(unittest.TestCase):
    def test_a_lost_prepare_reconciles_the_same_identity_once(self) -> None:
        journal = FakeJournal()
        harness = Harness(
            journal=journal,
            prepare=(LostResponse("op-prepare-1"),),
            prepare_observe=(PREPARE_OK,),
        )
        harness.run(3)

        lost = harness.flow.advance().to_document()

        self.assertEqual(lost["stage"], "unknown")
        self.assertEqual(lost["code"], "prepare_commit_unknown")
        self.assertEqual(lost["actions"][0], "reconcile_pending")
        self.assertEqual(
            harness.flow.pending_operation(),
            PendingOperation("prepare", "op-prepare-1", CONTRACT.KIND_PREPARE_APPLY),
        )
        self.assertEqual(
            journal.pending,
            PendingOperation("prepare", "op-prepare-1", CONTRACT.KIND_PREPARE_APPLY),
        )

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["code"], "prepare_ready")
        self.assertIsNone(journal.pending)
        # One issue, one reconcile of that exact identity, never a second issue.
        self.assertEqual(harness.names("prepare"), ["prepare.prepare", "prepare.observe"])
        self.assertEqual(
            [operation for name, operation in harness.calls if name.startswith("prepare")],
            ["op-prepare-1", "op-prepare-1"],
        )

    def test_a_lost_activation_that_committed_continues_without_reissuing(self) -> None:
        harness = Harness(
            activate=(LostResponse("op-activate-1"),), activate_observe=(ACTIVATE_OK,)
        )
        harness.run(5)

        lost = harness.flow.advance().to_document()
        self.assertEqual(lost["code"], "activate_commit_unknown")

        settled = harness.flow.advance().to_document()
        # The observed receipt is pending, so the flow holds at its restart
        # rather than treating a reconciled activation as a finished one.
        self.assertEqual(settled["code"], "activate_pending_restart")

        confirmed = harness.resume().to_document()
        self.assertEqual(confirmed["code"], "activate_committed")
        self.assertEqual(confirmed["versions"]["remote"], "1.0.14")
        self.assertEqual(harness.flow.advance().code, "update_ready")
        self.assertEqual(
            harness.names("activation"),
            ["activation.activate", "activation.observe", "activation.confirm"],
        )

    def test_a_reconcile_that_is_also_lost_stays_unknown_and_issues_nothing(self) -> None:
        harness = Harness(
            activate=(LostResponse("op-activate-1"),),
            activate_observe=(
                LostResponse("op-activate-1"),
                LostResponse("op-activate-1"),
            ),
        )
        harness.run(5)
        harness.flow.advance()

        first = harness.flow.advance().to_document()
        second = harness.flow.reconcile().to_document()

        self.assertEqual(first["code"], "activate_commit_unknown")
        self.assertEqual(second["code"], "activate_commit_unknown")
        self.assertEqual(second["stage"], "unknown")
        self.assertEqual(harness.names("activation.activate"), ["activation.activate"])

    def test_an_ambiguous_activation_answer_is_reconciled_not_reissued(self) -> None:
        harness = Harness(
            activate=(ActivationOutcome(status="verified", committed=None),),
            activate_observe=(ActivationOutcome(status="failed"),),
        )
        harness.run(5)

        ambiguous = harness.flow.advance().to_document()
        self.assertEqual(ambiguous["code"], "activate_unknown")
        self.assertEqual(ambiguous["actions"][0], "reconcile_pending")
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.retry()
        self.assertEqual(refused.exception.code, "retry_refused_pending_commit")

        settled = harness.flow.reconcile().to_document()

        self.assertEqual(settled["code"], "activate_failed")
        self.assertEqual(harness.names("activation.activate"), ["activation.activate"])

    def test_a_settled_refusal_releases_the_identity_and_allows_a_retry(self) -> None:
        journal = FakeJournal()
        harness = Harness(
            journal=journal, backup=(PortRefusal("BACKUP_WRITE_FAILED"), BACKUP_OK)
        )
        harness.run(2)

        refused = harness.flow.advance().to_document()

        self.assertEqual(refused["code"], "backup_failed")
        self.assertIsNone(journal.pending)
        self.assertEqual(refused["actions"][0], "retry_stage")
        self.assertEqual(harness.flow.retry().code, "backup_verified")
        self.assertEqual(
            harness.names("backup"),
            ["backup.create_verified", "backup.create_verified"],
        )

    def test_a_non_conforming_outcome_is_treated_as_an_open_commit(self) -> None:
        harness = Harness(
            activate=({"status": "verified", "committed": True},),
            activate_observe=(ACTIVATE_OK,),
        )
        harness.run(5)

        opened = harness.flow.advance().to_document()

        self.assertEqual(opened["code"], "activate_commit_unknown")
        self.assertEqual(harness.flow.advance().code, "activate_pending_restart")
        self.assertEqual(harness.names("activation.activate"), ["activation.activate"])

    def test_reconcile_without_a_pending_identity_is_refused(self) -> None:
        harness = Harness()
        harness.run(3)

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.reconcile()

        self.assertEqual(refused.exception.code, "reconcile_refused_no_pending")


class RestartTests(unittest.TestCase):
    def test_a_restart_reconciles_the_recorded_identity_instead_of_reissuing(self) -> None:
        journal = FakeJournal(
            PendingOperation("activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_ISSUE)
        )
        harness = Harness(journal=journal, activate_observe=(ACTIVATE_OK,))

        resumed = harness.flow.snapshot().to_document()

        self.assertEqual(resumed["stage"], "unknown")
        self.assertEqual(resumed["code"], "activate_commit_unknown")
        self.assertEqual(resumed["actions"][0], "reconcile_pending")

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["code"], "activate_pending_restart")
        self.assertEqual(harness.names("activation"), ["activation.observe"])
        self.assertEqual(
            [operation for name, operation in harness.calls], ["op-activate-1"]
        )
        self.assertEqual(harness.resume().code, "activate_committed")
        self.assertEqual(harness.flow.advance().code, "update_ready")

    def test_a_restart_after_the_backup_keeps_the_gate_satisfied(self) -> None:
        journal = FakeJournal(
            PendingOperation("prepare", "op-prepare-1", CONTRACT.KIND_PREPARE_APPLY)
        )
        harness = Harness(journal=journal, prepare_observe=(PREPARE_OK,))

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["code"], "prepare_ready")
        self.assertEqual(settled["backup"]["status"], "verified")
        self.assertIsNone(settled["backup"]["migration_required"])
        # The owner observations are not re-asserted from a record that never
        # carried them.
        self.assertEqual(settled["owner"]["process_exit"], "unknown")
        self.assertEqual(harness.flow.advance().code, "probe_verified")

    def test_a_restart_before_the_backup_does_not_invent_one(self) -> None:
        journal = FakeJournal(
            PendingOperation("stop", "op-stop-1", CONTRACT.KIND_OWNER_STOP)
        )
        harness = Harness(journal=journal, stop_observe=(STOP_OK,))

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["code"], "stop_verified")
        self.assertEqual(settled["backup"]["status"], "not_run")

    def test_an_unusable_record_does_not_resume_anything(self) -> None:
        for recorded in (
            # Stages that never mutate, an unnamed kind, a kind that does not
            # match the stage it claims, and something that is not a record.
            PendingOperation("preview", "op-preview-1", ""),
            PendingOperation("probe", "op-probe-1", "probe"),
            PendingOperation("activate", "op-1", ""),
            PendingOperation("stop", "op-1", CONTRACT.KIND_BACKUP_CREATE),
            "not-a-record",
        ):
            with self.subTest(recorded=recorded):
                harness = Harness(journal=FakeJournal(recorded))

                snapshot = harness.flow.snapshot()

                self.assertEqual(snapshot.stage, "idle")
                self.assertIsNone(harness.flow.pending_operation())
                self.assertEqual(harness.flow.advance().code, "preview_ready")


class CancellationTests(unittest.TestCase):
    def test_cancellation_names_the_stage_that_will_not_run(self) -> None:
        expected = {
            0: "cancelled_before_preview",
            1: "cancelled_before_stop",
            2: "cancelled_before_backup",
            3: "cancelled_before_prepare",
            4: "cancelled_before_probe",
            5: "cancelled_before_activate",
        }
        for steps, code in expected.items():
            with self.subTest(steps=steps):
                harness = Harness()
                harness.run(steps)

                document = harness.flow.cancel().to_document()

                self.assertEqual(document["stage"], "cancelled")
                self.assertEqual(document["code"], code)
                self.assertEqual(document["actions"], ["dismiss", "inspect_diagnostics"])
                self.assertEqual(harness.names("verification"), [])

    def test_cancelling_before_activation_never_touches_the_registry(self) -> None:
        harness = Harness()
        harness.run(5)

        harness.flow.cancel()

        self.assertEqual(harness.names("activation"), [])
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.advance()
        self.assertEqual(refused.exception.code, "advance_refused_terminal")

    def test_cancellation_is_refused_once_activation_committed(self) -> None:
        harness = Harness()
        harness.run(6)

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.cancel()

        self.assertEqual(refused.exception.code, "cancel_refused_activation_committed")
        # The receipt is written and pending; that is already past the point a
        # cancellation could undo without a rollback.
        self.assertEqual(harness.flow.snapshot().code, "activate_pending_restart")
        self.assertNotIn("cancel", harness.flow.snapshot().actions)
        self.assertEqual(harness.resume().code, "activate_committed")
        self.assertEqual(harness.flow.advance().code, "update_ready")

    def test_cancellation_waits_for_an_open_commit_to_settle(self) -> None:
        harness = Harness(
            prepare=(LostResponse("op-prepare-1"),), prepare_observe=(PREPARE_OK,)
        )
        harness.run(3)
        harness.flow.advance()

        deferred = harness.flow.cancel().to_document()

        self.assertEqual(deferred["stage"], "unknown")
        self.assertEqual(deferred["code"], "cancel_deferred_commit_unknown")
        self.assertEqual(deferred["actions"][0], "reconcile_pending")

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["stage"], "cancelled")
        self.assertEqual(settled["code"], "cancelled_before_probe")
        self.assertEqual(harness.names("prepare"), ["prepare.prepare", "prepare.observe"])

    def test_a_deferred_cancellation_yields_to_a_commit_that_happened(self) -> None:
        harness = Harness(
            activate=(LostResponse("op-activate-1"),), activate_observe=(ACTIVATE_OK,)
        )
        harness.run(5)
        harness.flow.advance()
        harness.flow.cancel()

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["stage"], "activate")
        self.assertEqual(settled["code"], "activate_pending_restart")
        self.assertEqual(harness.resume().code, "activate_committed")
        self.assertEqual(harness.flow.advance().code, "update_ready")


class RecoveryTests(unittest.TestCase):
    def _failed_after_activation(self, **scripts: object) -> Harness:
        harness = failed_after_activation(**scripts)
        self.assertEqual(harness.flow.snapshot().code, "verify_failed")
        return harness

    def test_rollback_is_refused_against_a_migrated_ssot(self) -> None:
        harness = self._failed_after_activation()
        document = harness.flow.snapshot().to_document()

        self.assertIs(document["backup"]["migration_required"], True)
        self.assertNotIn("rollback_activation", document["actions"])
        self.assertIn("restore_backup", document["actions"])

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.rollback()

        self.assertEqual(refused.exception.code, "rollback_refused_migrated_ssot")
        self.assertEqual(harness.names("activation.rollback"), [])

    def test_rollback_is_refused_while_migration_is_merely_unknown(self) -> None:
        harness = self._failed_after_activation(
            preview=(replace(PREVIEW_OK, migration_required=None),),
            backup=(BackupOutcome(status="verified"),),
        )

        self.assertIsNone(harness.flow.snapshot().to_document()["backup"]["migration_required"])
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.rollback()

        self.assertEqual(refused.exception.code, "rollback_refused_migrated_ssot")

    def test_rollback_runs_where_no_migration_happened(self) -> None:
        harness = self._failed_after_activation(
            preview=(replace(PREVIEW_OK, migration_required=False),),
            backup=(BackupOutcome(status="verified", migration_required=False),),
            rollback=(ROLLBACK_OK,),
        )
        document = harness.flow.snapshot().to_document()
        self.assertIn("rollback_activation", document["actions"])

        recovered = harness.flow.rollback().to_document()

        self.assertEqual(recovered["stage"], "failed")
        self.assertEqual(recovered["code"], "rollback_verified")
        self.assertEqual(recovered["actions"], ["dismiss", "inspect_diagnostics"])
        self.assertEqual(harness.names("activation.rollback"), ["activation.rollback"])

    def test_rollback_is_refused_when_no_previous_app_was_kept(self) -> None:
        harness = Harness(
            preview=(replace(PREVIEW_OK, migration_required=False),),
            backup=(BackupOutcome(status="verified", migration_required=False),),
            prepare=(replace(PREPARE_OK, previous_app_retained=None),),
        )
        harness.run(4)

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.rollback()

        self.assertEqual(refused.exception.code, "rollback_refused_no_previous_app")

    def test_restore_needs_a_verified_backup(self) -> None:
        harness = Harness(backup=(BackupOutcome(status="failed"),))
        harness.run(3)

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.restore()

        self.assertEqual(refused.exception.code, "restore_refused_backup_not_verified")
        self.assertEqual(harness.names("backup.restore"), [])

    def test_restore_from_the_verified_backup_is_the_migrated_recovery_path(self) -> None:
        harness = self._failed_after_activation(
            restore=(RESTORE_OK,), rollback=(ROLLBACK_OK,)
        )

        recovered = harness.flow.restore().to_document()

        # The data is back, but the new activation is still the selected one,
        # so this is not yet a finished recovery.
        self.assertEqual(recovered["code"], "restore_verified_activation_selected")
        self.assertEqual(recovered["stage"], "failed")
        self.assertEqual(recovered["actions"], ["rollback_activation", "inspect_diagnostics"])

        settled = harness.flow.rollback().to_document()

        self.assertEqual(settled["code"], "rollback_verified")
        self.assertEqual(settled["actions"], ["dismiss", "inspect_diagnostics"])

    def test_a_lost_restore_leaves_an_identity_to_reconcile(self) -> None:
        harness = self._failed_after_activation(
            restore=(LostResponse("op-backup_restore-1"),),
            restore_observe=(RESTORE_OK,),
        )

        lost = harness.flow.restore().to_document()

        self.assertEqual(lost["stage"], "unknown")
        self.assertEqual(lost["code"], "restore_unknown")
        self.assertEqual(lost["actions"][0], "reconcile_pending")
        self.assertEqual(harness.names("backup.restore"), ["backup.restore"])

        settled = harness.flow.reconcile().to_document()

        # The identity reconciles as the restore it was, through the restore's
        # own observation, not as a backup creation.
        self.assertEqual(settled["code"], "restore_verified_activation_selected")
        self.assertEqual(harness.names("backup.observe"), ["backup.observe_restore"])

    def test_a_failed_restore_offers_the_restore_again_and_not_a_stage_retry(self) -> None:
        harness = self._failed_after_activation(
            restore=(RestoreOutcome(status="failed"), RESTORE_OK)
        )

        failed = harness.flow.restore().to_document()

        self.assertEqual(failed["code"], "restore_failed")
        self.assertEqual(failed["actions"], ["restore_backup", "inspect_diagnostics"])
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.retry()
        self.assertEqual(refused.exception.code, "retry_refused_unrecoverable")
        self.assertEqual(
            harness.flow.restore().code, "restore_verified_activation_selected"
        )


class ControlSurfaceTests(unittest.TestCase):
    def test_advance_is_refused_on_a_terminal_condition(self) -> None:
        harness = Harness()
        harness.run_to_ready()

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.advance()

        self.assertEqual(refused.exception.code, "advance_refused_terminal")
        self.assertEqual(harness.flow.snapshot().code, "update_ready")

    def test_advance_is_refused_on_a_failure_that_needs_an_explicit_choice(self) -> None:
        harness = Harness(verify=(VerifyOutcome(status="failed"), VERIFY_OK))
        harness.run(6)
        harness.resume()
        harness.flow.advance()

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.advance()

        self.assertEqual(refused.exception.code, "advance_refused_failed")
        self.assertEqual(harness.flow.retry().code, "update_ready")

    def test_retry_is_refused_when_nothing_failed(self) -> None:
        harness = Harness()
        harness.run(3)

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.retry()

        self.assertEqual(refused.exception.code, "retry_refused_not_failed")

    def test_a_refused_call_leaves_the_published_condition_untouched(self) -> None:
        harness = Harness()
        harness.run(3)
        before = harness.flow.snapshot().to_document()

        for call in (harness.flow.retry, harness.flow.reconcile, harness.flow.rollback):
            with self.assertRaises(RemoteUpdateRefused):
                call()

        self.assertEqual(harness.flow.snapshot().to_document(), before)

    def test_refusal_codes_never_appear_as_published_codes(self) -> None:
        self.assertEqual(CONTRACT.CODES & CONTRACT.REFUSAL_CODES, frozenset())

    def test_one_clear_next_action_leads_each_condition(self) -> None:
        for scenario in _all_scenarios():
            for document in scenario:
                with self.subTest(code=document["code"]):
                    self.assertTrue(document["actions"], document)
                    self.assertEqual(
                        len(document["actions"]), len(set(document["actions"]))
                    )
                    self.assertNotEqual(document["actions"][0], "inspect_diagnostics")

    def test_a_committed_activation_withdraws_the_cancel_action(self) -> None:
        harness = Harness()
        codes_with_cancel = []
        for _ in range(6):
            snapshot = harness.flow.advance()
            if "cancel" in snapshot.actions:
                codes_with_cancel.append(snapshot.code)
        self.assertEqual(
            codes_with_cancel,
            ["preview_ready", "stop_verified", "backup_verified", "prepare_ready", "probe_verified"],
        )
        self.assertNotIn("cancel", harness.flow.snapshot().actions)


class RestartBoundActivationTests(unittest.TestCase):
    """The activation seam is restart-bound, and this flow holds it there.

    ``activate`` writes a receipt that is only pending.  Nothing may treat
    that as a finished activation, nothing may verify it in the same process,
    and the only way onward is a restart that selects this exact activation
    and brings up the same workspace.
    """

    def test_activation_holds_at_the_restart_and_never_verifies_in_process(self) -> None:
        harness = Harness()

        harness.run(6)

        document = harness.flow.snapshot().to_document()
        self.assertEqual(document["stage"], "activate")
        self.assertEqual(document["code"], "activate_pending_restart")
        self.assertEqual(document["actions"][0], "restart_desktop")
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.advance()
        self.assertEqual(refused.exception.code, "advance_refused_restart_required")
        # Confirmation and verification both wait for the restart.
        self.assertEqual(harness.names("activation"), ["activation.activate"])
        self.assertEqual(harness.names("verification"), [])

    def test_a_crash_after_the_pending_receipt_finds_the_retained_identity(self) -> None:
        journal = FakeJournal()
        harness = Harness(journal=journal)
        harness.run(6)

        self.assertEqual(
            journal.activation,
            PendingOperation(
                "activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_PENDING,
                "verified",
            ),
        )
        self.assertIsNone(journal.pending)

        # The desktop dies here.  The next start reads the same record.
        restarted = Harness(journal=FakeJournal(journal.entries))
        document = restarted.flow.snapshot().to_document()

        self.assertEqual(document["stage"], "activate")
        self.assertEqual(document["code"], "activate_pending_restart")
        self.assertEqual(document["actions"][0], "restart_desktop")
        self.assertEqual(restarted.resume().code, "activate_committed")
        # The same receipt is confirmed; no second activation is ever issued.
        self.assertEqual(restarted.names("activation"), ["activation.confirm"])
        self.assertEqual([operation for _, operation in restarted.calls], ["op-activate-1"])

    def test_resume_requires_the_startup_to_have_selected_this_activation(self) -> None:
        harness = Harness()
        harness.run(6)

        document = harness.flow.resume_after_restart(
            harness.startup(selected_activation_id="op-activate-9")
        ).to_document()

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "activate_restart_identity_mismatch")
        self.assertEqual(document["actions"][0], "restart_desktop")
        self.assertEqual(harness.names("activation.confirm"), [])
        self.assertEqual(
            harness.flow.retained_activation().operation_id, "op-activate-1"
        )

    def test_resume_requires_the_new_authority_to_have_been_selected(self) -> None:
        harness = Harness()
        harness.run(6)

        document = harness.resume(authority_selected="unknown").to_document()

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "activate_restart_not_selected")
        self.assertEqual(harness.names("activation.confirm"), [])
        self.assertIsNotNone(harness.flow.retained_activation())

    def test_resume_requires_the_same_workspace_before_confirming(self) -> None:
        for answer, stage, code in (
            ("failed", "failed", "activate_workspace_mismatch"),
            ("unknown", "unknown", "activate_workspace_unknown"),
        ):
            with self.subTest(answer=answer):
                harness = Harness()
                harness.run(6)

                document = harness.resume(workspace_match=answer).to_document()

                self.assertEqual(document["stage"], stage)
                self.assertEqual(document["code"], code)
                self.assertEqual(harness.names("activation.confirm"), [])
                # Nothing this flow can re-run answers a workspace question.
                self.assertNotIn("retry_stage", document["actions"])
                with self.assertRaises(RemoteUpdateRefused) as refused:
                    harness.flow.retry()
                self.assertEqual(refused.exception.code, "retry_refused_pending_commit")

    def test_resume_without_a_retained_activation_is_refused(self) -> None:
        harness = Harness()
        harness.run(3)

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.resume()

        self.assertEqual(refused.exception.code, "resume_refused_no_pending_activation")
        self.assertEqual(harness.names("activation"), [])

    def test_an_activation_reported_as_already_in_force_is_not_accepted(self) -> None:
        # The existing seam cannot confirm without a restart, so a port that
        # says it did is not something this flow may act on.
        harness = Harness(
            activate=(
                ActivationOutcome(status="verified", committed=True, state="confirmed"),
            ),
            activate_observe=(ACTIVATE_OK,),
        )
        harness.run(5)

        document = harness.flow.advance().to_document()

        self.assertEqual(document["stage"], "unknown")
        self.assertEqual(document["code"], "activate_unknown")
        self.assertEqual(document["actions"][0], "reconcile_pending")
        self.assertEqual(harness.names("verification"), [])


class MutationIdentityTests(unittest.TestCase):
    """Every mutation is recorded as the operation it actually is.

    Backup creation and backup restore share a stage; activation issue,
    confirmation and rollback share another.  They reconcile through different
    port calls and mean different things, so a lost one is settled as itself,
    and no new mutation is issued while any of them is open.
    """

    def test_a_lost_confirmation_is_reconciled_through_its_own_observation(self) -> None:
        harness = Harness(
            confirm=(LostResponse("op-activate-1"),), confirm_observe=(CONFIRM_OK,)
        )
        harness.run(6)

        lost = harness.resume().to_document()

        self.assertEqual(lost["stage"], "unknown")
        self.assertEqual(lost["code"], "activate_confirm_unknown")
        self.assertEqual(lost["actions"][0], "reconcile_pending")

        settled = harness.flow.advance().to_document()

        self.assertEqual(settled["code"], "activate_committed")
        # One confirmation, reconciled once as a confirmation.
        self.assertEqual(
            harness.names("activation"),
            ["activation.activate", "activation.confirm", "activation.observe_confirm"],
        )

    def test_a_refused_confirmation_keeps_the_receipt_pending(self) -> None:
        journal = FakeJournal()
        harness = Harness(
            journal=journal, confirm=(PortRefusal("REGISTRY_DIGEST_MOVED"), CONFIRM_OK)
        )
        harness.run(6)

        failed = harness.resume().to_document()

        self.assertEqual(failed["stage"], "failed")
        self.assertEqual(failed["code"], "activate_confirm_failed")
        self.assertEqual(failed["actions"][0], "restart_desktop")
        # The receipt is still there and still pending, so it is still held.
        self.assertEqual(
            journal.activation,
            PendingOperation(
                "activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_PENDING,
                "verified",
            ),
        )
        self.assertEqual(harness.resume().code, "activate_committed")
        self.assertEqual(
            harness.names("activation.confirm"),
            ["activation.confirm", "activation.confirm"],
        )

    def test_a_restore_is_refused_while_another_identity_is_unsettled(self) -> None:
        journal = FakeJournal()
        harness = Harness(
            journal=journal,
            prepare=(LostResponse("op-prepare-1"),),
            prepare_observe=(PREPARE_OK,),
        )
        harness.run(3)
        open_commit = harness.flow.advance().to_document()
        self.assertEqual(open_commit["code"], "prepare_commit_unknown")

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.restore()

        self.assertEqual(refused.exception.code, "restore_refused_pending_commit")
        self.assertEqual(harness.names("backup.restore"), [])
        # The ambiguous preparation identity is still the one on record.
        self.assertEqual(journal.pending.kind, CONTRACT.KIND_PREPARE_APPLY)
        self.assertEqual(
            open_commit["actions"], ["reconcile_pending", "inspect_diagnostics"]
        )

    def test_a_lost_restore_is_never_reconciled_as_a_backup_creation(self) -> None:
        harness = failed_after_activation(
            restore=(LostResponse("op-backup_restore-1"),),
            restore_observe=(RESTORE_OK,),
            rollback=(ROLLBACK_OK,),
        )
        harness.flow.restore()

        settled = harness.flow.reconcile().to_document()

        self.assertEqual(settled["code"], "restore_verified_activation_selected")
        self.assertEqual(harness.names("backup.observe"), ["backup.observe_restore"])
        # A restore reconciled as a backup creation would rewind the run order
        # and offer preparation again.  It is a restore, so it does not.
        self.assertNotIn("run_prepare", settled["actions"])
        self.assertEqual(settled["stage"], "failed")

    def test_a_lost_rollback_is_reconciled_as_a_rollback(self) -> None:
        harness = failed_after_activation(
            preview=(replace(PREVIEW_OK, migration_required=False),),
            backup=(BackupOutcome(status="verified", migration_required=False),),
            rollback=(LostResponse("op-activate-1"),),
            rollback_observe=(ROLLBACK_OK,),
        )

        lost = harness.flow.rollback().to_document()

        self.assertEqual(lost["stage"], "unknown")
        self.assertEqual(lost["code"], "rollback_unknown")
        self.assertEqual(lost["actions"][0], "reconcile_pending")

        settled = harness.flow.reconcile().to_document()

        self.assertEqual(settled["code"], "rollback_verified")
        self.assertEqual(settled["actions"], ["dismiss", "inspect_diagnostics"])
        self.assertEqual(harness.names("activation.rollback"), ["activation.rollback"])
        self.assertEqual(
            harness.names("activation.observe_rollback"), ["activation.observe_rollback"]
        )

    def test_an_unknown_recovery_answer_keeps_its_identity(self) -> None:
        harness = failed_after_activation(
            restore=(RestoreOutcome(status="unknown"), RESTORE_OK),
            restore_observe=(RestoreOutcome(status="unknown"),),
        )

        opened = harness.flow.restore().to_document()

        self.assertEqual(opened["stage"], "unknown")
        self.assertEqual(opened["code"], "restore_unknown")
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.restore()
        self.assertEqual(refused.exception.code, "restore_refused_pending_commit")
        # The second restore was refused, not issued.
        self.assertEqual(harness.names("backup.restore"), ["backup.restore"])
        self.assertEqual(harness.flow.reconcile().code, "restore_unknown")

    def test_each_identity_is_journaled_under_the_operation_it_is(self) -> None:
        journal = FakeJournal()
        harness = failed_after_activation(
            journal=journal, restore=(RESTORE_OK,), rollback=(ROLLBACK_OK,)
        )

        harness.flow.restore()
        harness.flow.rollback()

        recorded = {entry.kind for write in journal.writes for entry in write}
        self.assertEqual(
            recorded,
            {
                CONTRACT.KIND_OWNER_STOP,
                CONTRACT.KIND_BACKUP_CREATE,
                CONTRACT.KIND_PREPARE_APPLY,
                CONTRACT.KIND_ACTIVATION_ISSUE,
                CONTRACT.KIND_ACTIVATION_PENDING,
                CONTRACT.KIND_ACTIVATION_CONFIRM,
                CONTRACT.KIND_ACTIVATION_CONFIRMED,
                CONTRACT.KIND_BACKUP_RESTORE,
                CONTRACT.KIND_ACTIVATION_ROLLBACK,
            },
        )
        # Everything settled, so nothing is left recorded.
        self.assertEqual(journal.entries, ())


class RecoveryPairingTests(unittest.TestCase):
    """A restore that leaves the new activation selected is not a recovery.

    Restoring the pre-migration data under a registry that still selects the
    new application is exactly the incompatible pairing recovery exists to
    prevent, so the flow keeps going until the pairing is whole again.
    """

    def test_a_verified_restore_does_not_settle_while_the_activation_stands(self) -> None:
        harness = failed_after_activation(restore=(RESTORE_OK,), rollback=(ROLLBACK_OK,))

        restored = harness.flow.restore().to_document()

        self.assertEqual(restored["code"], "restore_verified_activation_selected")
        self.assertEqual(restored["actions"][0], "rollback_activation")
        self.assertNotIn("dismiss", restored["actions"])

        settled = harness.flow.rollback().to_document()

        self.assertEqual(settled["code"], "rollback_verified")
        self.assertEqual(settled["actions"], ["dismiss", "inspect_diagnostics"])

    def test_old_code_stays_refused_until_the_restore_is_verified(self) -> None:
        harness = failed_after_activation(restore=(RESTORE_OK,), rollback=(ROLLBACK_OK,))

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.rollback()
        self.assertEqual(refused.exception.code, "rollback_refused_migrated_ssot")
        self.assertEqual(harness.names("activation.rollback"), [])

        harness.flow.restore()

        # The data in place is the pre-migration archive again, so the
        # preserved previous application is no longer old code against it.
        self.assertEqual(harness.flow.rollback().code, "rollback_verified")

    def test_a_restore_that_cannot_prove_pre_migration_data_unlocks_nothing(self) -> None:
        harness = failed_after_activation(
            restore=(RestoreOutcome(status="verified", pre_migration_data=None),),
            restore_observe=(RestoreOutcome(status="failed"),),
        )

        opened = harness.flow.restore().to_document()
        self.assertEqual(opened["code"], "restore_unknown")

        settled = harness.flow.reconcile().to_document()
        self.assertEqual(settled["code"], "restore_failed")

        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.rollback()
        self.assertEqual(refused.exception.code, "rollback_refused_migrated_ssot")

    def test_a_pairing_that_cannot_be_restored_is_named_not_dismissed(self) -> None:
        # A restart resumed the pending receipt, so nothing here established
        # that a previous application was preserved to go back to.
        harness = Harness(
            journal=FakeJournal(
                PendingOperation(
                    "activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_PENDING
                )
            ),
            restore=(RESTORE_OK,),
        )

        restored = harness.flow.restore().to_document()

        self.assertEqual(restored["code"], "restore_verified_activation_selected")
        self.assertEqual(
            restored["actions"], ["resolve_activation_pairing", "inspect_diagnostics"]
        )
        self.assertNotIn("dismiss", restored["actions"])
        self.assertNotIn("rollback_activation", restored["actions"])


class SettledRestartRecoveryTests(unittest.TestCase):
    """A confirmed activation survives a restart, as an anchor and nothing more.

    Confirming the receipt is the moment the registry starts selecting the new
    application.  Until the update verifies whole, or a rollback puts the
    previous selection back, that fact is what recovery is about -- so it is
    on record, it is rebuilt exactly as it was, and it is never re-issued.
    The six cases below are one family and are checked together.
    """

    def test_a_crash_between_confirmation_and_verification_is_not_idle(self) -> None:
        # Case 1: confirm, crash before verify, reload.
        journal = FakeJournal()
        harness = confirmed_activation(journal)
        self.assertEqual(harness.flow.snapshot().code, "activate_committed")
        self.assertEqual(
            journal.activation,
            PendingOperation(
                "activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_CONFIRMED,
                "verified",
            ),
        )
        self.assertIsNone(journal.pending)

        restarted = Harness(journal=FakeJournal(journal.entries))
        document = restarted.flow.snapshot().to_document()

        # Not idle, and not the pending receipt either: the same activation,
        # confirmed, with the verification it is still owed as the next step.
        self.assertEqual(document["stage"], "activate")
        self.assertEqual(document["code"], "activate_committed")
        self.assertEqual(document["actions"], ["run_verify", "inspect_diagnostics"])
        self.assertEqual(
            restarted.flow.retained_activation(),
            PendingOperation(
                "activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_CONFIRMED,
                "verified",
            ),
        )

        # The anchor is passive: it is not a request to confirm anything.
        with self.assertRaises(RemoteUpdateRefused) as refused:
            restarted.resume()

        self.assertEqual(refused.exception.code, "resume_refused_no_pending_activation")
        self.assertEqual(restarted.names("activation"), [])
        self.assertEqual(restarted.flow.advance().code, "update_ready")

    def test_a_verified_update_retires_the_anchor_it_no_longer_needs(self) -> None:
        # Case 5, first half: a whole update has nothing left to recover.
        journal = FakeJournal()
        harness = confirmed_activation(journal)

        ready = harness.flow.advance().to_document()

        self.assertEqual(ready["code"], "update_ready")
        self.assertEqual(ready["actions"], ["finish"])
        self.assertEqual(journal.entries, ())
        self.assertIsNone(harness.flow.retained_activation())

    def test_a_crash_during_a_lost_restore_still_shows_the_unresolved_pairing(self) -> None:
        # Cases 2 and 3: the restore commits, its answer is lost, the desktop
        # dies, and the next start observes that same restore identity.
        journal = FakeJournal()
        harness = confirmed_activation(
            journal,
            verify=(VerifyOutcome(status="failed"),),
            restore=(LostResponse("op-backup_restore-1"),),
        )
        self.assertEqual(harness.flow.advance().code, "verify_failed")
        self.assertEqual(harness.flow.restore().code, "restore_unknown")

        restarted_journal = FakeJournal(journal.entries)
        restarted = Harness(
            journal=restarted_journal,
            restore_observe=(RESTORE_OK,),
            rollback=(ROLLBACK_OK,),
        )
        opened = restarted.flow.snapshot().to_document()
        self.assertEqual(opened["code"], "restore_unknown")
        self.assertEqual(opened["actions"][0], "reconcile_pending")

        settled = restarted.flow.reconcile().to_document()

        # The same restore, observed rather than re-issued.
        self.assertEqual(restarted.names("backup"), ["backup.observe_restore"])
        self.assertEqual(
            [operation for _, operation in restarted.calls], ["op-backup_restore-1"]
        )
        # The pre-migration data is back under a registry that still selects
        # the new application, and that pairing is not a settled recovery.
        self.assertEqual(settled["code"], "restore_verified_activation_selected")
        self.assertEqual(settled["stage"], "failed")
        self.assertNotIn("dismiss", settled["actions"])
        self.assertNotIn("finish", settled["actions"])
        self.assertEqual(settled["actions"][0], "rollback_activation")

        # Case 5, second half: only the verified rollback of that exact
        # receipt retires the anchor.
        rolled = restarted.flow.rollback().to_document()

        self.assertEqual(
            [
                operation
                for name, operation in restarted.calls
                if name == "activation.rollback"
            ],
            ["op-activate-1"],
        )
        self.assertEqual(rolled["code"], "rollback_verified")
        self.assertEqual(rolled["actions"], ["dismiss", "inspect_diagnostics"])
        self.assertEqual(restarted_journal.entries, ())

    def test_a_restart_without_retention_evidence_names_the_pairing(self) -> None:
        # Case 3 again, where nothing ever recorded that a previous
        # application was kept.  The answer is the open question, not success.
        harness = Harness(
            journal=FakeJournal(
                (
                    PendingOperation(
                        "activate", "op-activate-1",
                        CONTRACT.KIND_ACTIVATION_CONFIRMED,
                    ),
                    PendingOperation(
                        "backup", "op-backup_restore-1",
                        CONTRACT.KIND_BACKUP_RESTORE,
                    ),
                )
            ),
            restore_observe=(RESTORE_OK,),
        )

        settled = harness.flow.reconcile().to_document()

        self.assertEqual(settled["code"], "restore_verified_activation_selected")
        self.assertEqual(
            settled["actions"], ["resolve_activation_pairing", "inspect_diagnostics"]
        )
        with self.assertRaises(RemoteUpdateRefused) as refused:
            harness.flow.rollback()

        # Unavailable evidence is never guessed into a previous application.
        self.assertEqual(refused.exception.code, "rollback_refused_no_previous_app")
        self.assertEqual(harness.names("activation"), [])

    def test_a_crash_during_a_lost_rollback_reconciles_that_same_rollback(self) -> None:
        # Case 4: the rollback's answer is lost, the desktop dies, and the
        # next start observes the same receipt rather than mutating again.
        journal = FakeJournal()
        harness = confirmed_activation(
            journal,
            preview=(replace(PREVIEW_OK, migration_required=False),),
            backup=(BackupOutcome(status="verified", migration_required=False),),
            verify=(VerifyOutcome(status="failed"),),
            rollback=(LostResponse("op-activate-1"),),
        )
        harness.flow.advance()
        self.assertEqual(harness.flow.rollback().code, "rollback_unknown")

        restarted_journal = FakeJournal(journal.entries)
        restarted = Harness(
            journal=restarted_journal, rollback_observe=(ROLLBACK_OK,)
        )
        opened = restarted.flow.snapshot().to_document()
        self.assertEqual(opened["code"], "rollback_unknown")
        self.assertEqual(opened["actions"][0], "reconcile_pending")
        with self.assertRaises(RemoteUpdateRefused) as refused:
            restarted.flow.rollback()
        self.assertEqual(refused.exception.code, "rollback_refused_pending_commit")

        settled = restarted.flow.reconcile().to_document()

        self.assertEqual(restarted.names("activation"), ["activation.observe_rollback"])
        self.assertEqual([operation for _, operation in restarted.calls], ["op-activate-1"])
        self.assertEqual(settled["code"], "rollback_verified")
        self.assertEqual(settled["actions"], ["dismiss", "inspect_diagnostics"])
        self.assertEqual(restarted_journal.entries, ())

    def test_a_pending_receipt_across_a_restart_is_unchanged(self) -> None:
        # Case 6: the pending receipt still requires its restart, the same
        # selected activation and the same workspace before any confirmation.
        journal = FakeJournal()
        harness = Harness(journal=journal)
        harness.run(6)
        restarted = Harness(journal=FakeJournal(journal.entries))

        with self.assertRaises(RemoteUpdateRefused) as refused:
            restarted.flow.advance()

        self.assertEqual(refused.exception.code, "advance_refused_restart_required")
        held = restarted.resume(workspace_match="unknown").to_document()
        self.assertEqual(held["code"], "activate_workspace_unknown")
        self.assertEqual(restarted.names("activation"), [])
        self.assertEqual(
            restarted.flow.retained_activation().kind,
            CONTRACT.KIND_ACTIVATION_PENDING,
        )

        self.assertEqual(restarted.resume().code, "activate_committed")
        self.assertEqual(restarted.names("activation"), ["activation.confirm"])


def _all_scenarios() -> list[list[dict[str, object]]]:
    """Walk many conditions and collect every document they publish."""

    scenarios: list[list[dict[str, object]]] = []

    def walk(steps: int, **scripts: object) -> None:
        # A walk that runs into an open commit must have something to
        # reconcile with, so each mutating port observes what it returned.
        for observed, issued in (
            ("stop_observe", "stop"),
            ("backup_observe", "backup"),
            ("prepare_observe", "prepare"),
            ("activate_observe", "activate"),
            ("confirm_observe", "confirm"),
            ("restore_observe", "restore"),
            ("rollback_observe", "rollback"),
        ):
            if observed not in scripts:
                settled = tuple(
                    result
                    for result in scripts.get(issued, ())
                    if not isinstance(result, BaseException)
                )
                scripts[observed] = settled * 4
        harness = Harness(**scripts)
        documents = [harness.flow.snapshot().to_document()]
        for _ in range(steps):
            try:
                documents.append(harness.flow.advance().to_document())
            except RemoteUpdateRefused:
                break
        scenarios.append(documents)

    walk(7)
    walk(8, preview=(PreviewFacts(),))
    walk(8, preview=(PortRefusal(),))
    for state in ("live", "stopping", "dead", "foreign", "unfenced", "unknown"):
        for token in (True, False, None):
            walk(3, stop=(replace(STOP_OK, state=state, token_available=token),))
    for field in ("process_exit", "listener_release", "lease_release"):
        for answer in ("failed", "unknown"):
            walk(3, stop=(replace(STOP_OK, **{field: answer}),))
    walk(3, stop=(replace(STOP_OK, pidfd_available=False),))
    for status in ("verified", "failed", "not_run", "unknown"):
        walk(4, backup=(BackupOutcome(status=status),))
    walk(5, prepare=(replace(PREPARE_OK, previous_app_retained=False),))
    walk(5, prepare=(replace(PREPARE_OK, previous_profile_retained=None),))
    walk(5, prepare=(replace(PREPARE_OK, capability="unavailable"),))
    walk(5, prepare=(PortRefusal(),))
    for match in ("verified", "failed", "unknown"):
        for status in ("verified", "failed", "unknown"):
            walk(6, probe=(ProbeOutcome(status=status, workspace_match=match),))
    for status in ("verified", "failed", "unknown"):
        for committed in (True, False, None):
            walk(
                8,
                activate=(ActivationOutcome(status=status, committed=committed),),
                activate_observe=(ActivationOutcome(status="failed"),),
            )
    for status in ("verified", "failed", "unknown"):
        walk(8, verify=(VerifyOutcome(status=status),))
    walk(
        9,
        prepare=(LostResponse("op-prepare-1"),),
        prepare_observe=(PREPARE_OK,),
    )
    walk(
        9,
        activate=(LostResponse("op-activate-1"),),
        activate_observe=(ACTIVATE_OK,),
    )
    walk(
        9,
        stop=(LostResponse("op-stop-1"),),
        stop_observe=(STOP_OK,),
    )
    walk(
        9,
        backup=(LostResponse("op-backup-1"),),
        backup_observe=(BACKUP_OK,),
    )

    # Cancellation and recovery conditions, collected the same way.
    for steps in range(6):
        harness = Harness()
        harness.run(steps)
        scenarios.append([harness.flow.cancel().to_document()])

    # The restart the activation requires, resumed on every shape of evidence.
    for evidence in (
        None,
        StartupEvidence("op-activate-1", "verified", "verified"),
        StartupEvidence("op-activate-1", "verified", "failed"),
        StartupEvidence("op-activate-1", "verified", "unknown"),
        StartupEvidence("op-activate-1", "unknown", "verified"),
        StartupEvidence("op-somewhere-else", "verified", "verified"),
    ):
        harness = Harness()
        harness.run(6)
        documents = [harness.flow.snapshot().to_document()]
        try:
            documents.append(harness.flow.resume_after_restart(evidence).to_document())
        except RemoteUpdateRefused:
            pass
        for _ in range(2):
            try:
                documents.append(harness.flow.advance().to_document())
            except RemoteUpdateRefused:
                break
        scenarios.append(documents)

    # Every shape of confirmation answer for that retained receipt.
    for confirmation in (
        CONFIRM_OK,
        ActivationOutcome(status="verified", committed=False, state="pending"),
        ActivationOutcome(status="failed"),
        ActivationOutcome(status="unknown"),
        PortRefusal("REGISTRY_DIGEST_MOVED"),
        LostResponse("op-activate-1"),
        {"status": "verified"},
    ):
        harness = Harness(
            confirm=(confirmation,), confirm_observe=(ActivationOutcome(status="failed"),)
        )
        harness.run(6)
        documents = [harness.resume().to_document()]
        for _ in range(2):
            try:
                documents.append(harness.flow.advance().to_document())
            except RemoteUpdateRefused:
                break
        scenarios.append(documents)

    for restored in (
        RESTORE_OK,
        RestoreOutcome(status="verified", pre_migration_data=None),
        RestoreOutcome(status="failed"),
        RestoreOutcome(status="unknown"),
        LostResponse("op-backup_restore-1"),
        PortRefusal("V4_RESTORE_REFUSED"),
    ):
        harness = failed_after_activation(
            restore=(restored,),
            restore_observe=(RESTORE_OK,),
            rollback=(ROLLBACK_OK,),
        )
        documents = [harness.flow.restore().to_document()]
        for call in (harness.flow.rollback, harness.flow.reconcile):
            try:
                documents.append(call().to_document())
            except RemoteUpdateRefused:
                continue
        scenarios.append(documents)

    for rolled in (
        ROLLBACK_OK,
        RollbackOutcome(status="verified", previous_activation_selected=None),
        RollbackOutcome(status="failed"),
        LostResponse("op-activate-1"),
        PortRefusal("ACTIVATION_RECOVERY_REFUSED"),
    ):
        rolling = failed_after_activation(
            preview=(replace(PREVIEW_OK, migration_required=False),),
            backup=(BackupOutcome(status="verified", migration_required=False),),
            rollback=(rolled,),
            rollback_observe=(ROLLBACK_OK,),
        )
        documents = [rolling.flow.rollback().to_document()]
        try:
            documents.append(rolling.flow.reconcile().to_document())
        except RemoteUpdateRefused:
            pass
        scenarios.append(documents)

    for recorded in (
        PendingOperation("stop", "op-stop-1", CONTRACT.KIND_OWNER_STOP),
        PendingOperation("backup", "op-backup-1", CONTRACT.KIND_BACKUP_CREATE),
        PendingOperation("backup", "op-backup_restore-1", CONTRACT.KIND_BACKUP_RESTORE),
        PendingOperation("prepare", "op-prepare-1", CONTRACT.KIND_PREPARE_APPLY),
        PendingOperation("activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_ISSUE),
        PendingOperation("activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_PENDING),
        PendingOperation("activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_CONFIRM),
        PendingOperation(
            "activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_CONFIRMED
        ),
        PendingOperation("activate", "op-activate-1", CONTRACT.KIND_ACTIVATION_ROLLBACK),
    ):
        harness = Harness(
            journal=FakeJournal(recorded),
            stop_observe=(STOP_OK,),
            backup_observe=(BACKUP_OK,),
            restore_observe=(RESTORE_OK,),
            prepare_observe=(PREPARE_OK,),
            activate_observe=(ACTIVATE_OK,),
            confirm_observe=(CONFIRM_OK,),
            rollback_observe=(ROLLBACK_OK,),
        )
        documents = [harness.flow.snapshot().to_document()]
        for _ in range(4):
            try:
                documents.append(harness.flow.advance().to_document())
            except RemoteUpdateRefused:
                break
        scenarios.append(documents)

    return scenarios


if __name__ == "__main__":
    unittest.main()
