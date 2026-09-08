from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import connection_registry as REGISTRY
import connection_registry_activation_recovery as RECOVERY
import connection_registry_mutations as MUTATIONS


PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"


def local_profile(data_dir: Path):
    return REGISTRY.LocalConnectionProfile(
        profile_id=PROFILE_A,
        label="Local",
        data_dir=str(data_dir.absolute()),
        expected_workspace_id=WORKSPACE_A,
        enabled=True,
        live_updates=True,
    )


def ssh_profile():
    return REGISTRY.SshConnectionProfile(
        profile_id=PROFILE_B,
        label="Remote",
        ssh_host_alias="work-linux",
        remote_app_dir="/srv/workstack/app",
        remote_data_dir="/srv/workstack/ssot",
        expected_workspace_id=WORKSPACE_B,
        preferred_forward_port=18765,
        remote_port=8765,
        enabled=True,
        live_updates=True,
    )


def registry(active: str, *profiles: object):
    return REGISTRY.ConnectionRegistry(1, active, tuple(profiles))


def plant_duplicate_pending_receipt(root: Path, receipt):
    """Write the extra pending receipt a build before this repair could leave.

    Its rollback is the already-activated registry, which is exactly the shape a
    same-candidate retry used to persist next to the first attempt.
    """

    activation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, receipt.activation_id))
    duplicate = MUTATIONS.ActivationReceipt(
        activation_id=activation_id,
        state="pending",
        previous_registry_digest=receipt.activated_registry_digest,
        activated_registry_digest=receipt.activated_registry_digest,
        profile_id=receipt.profile_id,
        profile_digest=receipt.profile_digest,
        proof_digest=receipt.proof_digest,
        rollback_file=f"{activation_id}.rollback.json",
    )
    records = root / MUTATIONS.ACTIVATION_DIRECTORY
    (records / duplicate.rollback_file).write_bytes(
        (root / REGISTRY.REGISTRY_FILE).read_bytes()
    )
    (records / f"{activation_id}.receipt.json").write_bytes(
        MUTATIONS._receipt_bytes(duplicate)
    )
    return duplicate


def plant_second_duplicate_pending_receipt(root: Path, receipt):
    """Write a second provably no-op receipt so one reconciliation writes twice."""

    activation_id = str(uuid.uuid5(uuid.NAMESPACE_OID, receipt.activation_id))
    duplicate = MUTATIONS.ActivationReceipt(
        activation_id=activation_id,
        state="pending",
        previous_registry_digest=receipt.activated_registry_digest,
        activated_registry_digest=receipt.activated_registry_digest,
        profile_id=receipt.profile_id,
        profile_digest=receipt.profile_digest,
        proof_digest=receipt.proof_digest,
        rollback_file=f"{activation_id}.rollback.json",
    )
    records = root / MUTATIONS.ACTIVATION_DIRECTORY
    (records / duplicate.rollback_file).write_bytes(
        (root / REGISTRY.REGISTRY_FILE).read_bytes()
    )
    (records / f"{activation_id}.receipt.json").write_bytes(
        MUTATIONS._receipt_bytes(duplicate)
    )
    return duplicate


def plant_foreign_pending_receipt(root: Path, receipt):
    """Write unconfirmed evidence that neither rolls back to nor activated now.

    Nothing proves this attempt failed, so no plan may close it and no page may
    offer an action while it is present.
    """

    activation_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, receipt.activation_id))
    foreign = MUTATIONS.ActivationReceipt(
        activation_id=activation_id,
        state="pending",
        previous_registry_digest="sha256:" + "a" * 64,
        activated_registry_digest="sha256:" + "b" * 64,
        profile_id=receipt.profile_id,
        profile_digest=receipt.profile_digest,
        proof_digest=receipt.proof_digest,
        rollback_file=f"{activation_id}.rollback.json",
    )
    records = root / MUTATIONS.ACTIVATION_DIRECTORY
    (records / foreign.rollback_file).write_bytes(b"{}")
    (records / f"{activation_id}.receipt.json").write_bytes(
        MUTATIONS._receipt_bytes(foreign)
    )
    return foreign


def byte_tree(root: Path) -> dict[str, bytes]:
    """Snapshot every file under the state root for an exact no-write oracle."""

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def ready_result(profile: object):
    return MUTATIONS.ProfileTestResult(
        profile.profile_id,
        profile.kind,
        "ready",
        profile.expected_workspace_id,
        "1.0.6",
        1,
    )


class ActivationRecoveryTest(unittest.TestCase):
    def _activate(self, root: Path):
        local = local_profile(root / "ssot-must-not-be-created")
        remote = ssh_profile()
        original = registry(PROFILE_A, local, remote)
        candidate = registry(PROFILE_B, local, remote)
        REGISTRY.save_connection_registry(root, original)
        mutations = MUTATIONS.ConnectionRegistryMutationService(root)
        original_digest = MUTATIONS.registry_digest(original)
        proof = mutations.issue_successful_test_proof(
            remote,
            ready_result(remote),
            base_registry_digest=original_digest,
        )
        receipt = mutations.activate(
            candidate,
            PROFILE_B,
            proof.proof_id,
            expected_registry_digest=original_digest,
        )
        return original, candidate, mutations, receipt

    def test_absent_records_report_none_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            current = registry(PROFILE_A, local)
            REGISTRY.save_connection_registry(root, current)

            status = RECOVERY.ConnectionRegistryActivationRecoveryService(root).inspect()

            self.assertEqual("none", status.state)
            self.assertEqual("no_recovery", status.code)
            self.assertFalse(status.can_restore)
            self.assertFalse((root / MUTATIONS.ACTIVATION_DIRECTORY).exists())
            self.assertFalse((root / "ssot").exists())
            self.assertEqual(
                {
                    "state": "none",
                    "code": "no_recovery",
                    "message": "No connection activation requires recovery.",
                    "can_restore": False,
                    "can_reconcile": False,
                    "activation_id": None,
                    "profile_id": None,
                    "current_registry_digest": None,
                },
                RECOVERY.activation_recovery_status_to_document(status),
            )

    def test_matching_pending_activation_is_recoverable_and_inspection_is_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, _mutations, receipt = self._activate(root)
            receipt_path = (
                root
                / MUTATIONS.ACTIVATION_DIRECTORY
                / f"{receipt.activation_id}.receipt.json"
            )
            rollback_path = (
                root / MUTATIONS.ACTIVATION_DIRECTORY / receipt.rollback_file
            )
            registry_path = root / REGISTRY.REGISTRY_FILE
            before = tuple(path.read_bytes() for path in (registry_path, receipt_path, rollback_path))

            status = RECOVERY.ConnectionRegistryActivationRecoveryService(root).inspect()

            self.assertEqual("recovery_required", status.state)
            self.assertTrue(status.can_restore)
            self.assertEqual(receipt.activation_id, status.activation_id)
            self.assertEqual(PROFILE_B, status.profile_id)
            self.assertEqual(MUTATIONS.registry_digest(candidate), status.current_registry_digest)
            self.assertEqual(
                before,
                tuple(path.read_bytes() for path in (registry_path, receipt_path, rollback_path)),
            )
            self.assertFalse((root / "ssot-must-not-be-created").exists())

    def test_explicit_restore_uses_exact_binding_and_closes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, _candidate, mutations, receipt = self._activate(root)
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            status = recovery.inspect()

            result = recovery.restore(
                receipt.activation_id,
                expected_registry_digest=status.current_registry_digest,
            )

            self.assertEqual("restored", result.state)
            self.assertEqual(MUTATIONS.registry_digest(original), result.restored_registry_digest)
            self.assertEqual(original, REGISTRY.load_connection_registry(root))
            self.assertEqual(
                "restored",
                MUTATIONS.load_activation_receipt(root, receipt.activation_id).state,
            )
            self.assertEqual(
                {
                    "state": "restored",
                    "activation_id": receipt.activation_id,
                    "profile_id": PROFILE_B,
                    "restored_registry_digest": MUTATIONS.registry_digest(original),
                },
                RECOVERY.activation_recovery_result_to_document(result),
            )

    def test_wrong_activation_or_digest_is_refused_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, candidate, _mutations, receipt = self._activate(root)
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)
            wrong_id = str(uuid.uuid4())

            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                recovery.restore(
                    wrong_id,
                    expected_registry_digest=MUTATIONS.registry_digest(candidate),
                )
            self.assertEqual("recovery_conflict", caught.exception.code)
            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError):
                recovery.restore(
                    receipt.activation_id,
                    expected_registry_digest=MUTATIONS.registry_digest(original),
                )
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertEqual("pending", MUTATIONS.load_activation_receipt(root, receipt.activation_id).state)

    def test_confirmed_and_restored_receipts_cannot_be_restored_again(self) -> None:
        for terminal_state in ("confirmed", "restored"):
            with self.subTest(terminal_state=terminal_state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _original, candidate, mutations, receipt = self._activate(root)
                digest = MUTATIONS.registry_digest(candidate)
                if terminal_state == "confirmed":
                    mutations.confirm(receipt.activation_id, expected_registry_digest=digest)
                else:
                    mutations.restore(receipt.activation_id, expected_registry_digest=digest)
                recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)

                self.assertEqual("none", recovery.inspect().state)
                with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                    recovery.restore(
                        receipt.activation_id,
                        expected_registry_digest=digest,
                    )
                self.assertEqual("recovery_not_allowed", caught.exception.code)

    def test_stale_activation_is_blocked_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, _candidate, _mutations, receipt = self._activate(root)
            REGISTRY.save_connection_registry(root, original)
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)

            status = recovery.inspect()

            self.assertEqual("blocked", status.state)
            self.assertEqual("stale_activation", status.code)
            self.assertIsNone(status.activation_id)
            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                recovery.restore(
                    receipt.activation_id,
                    expected_registry_digest=MUTATIONS.registry_digest(original),
                )
            self.assertEqual("stale_activation", caught.exception.code)
            self.assertEqual("pending", MUTATIONS.load_activation_receipt(root, receipt.activation_id).state)

    def test_tampered_rollback_or_receipt_is_sanitized_and_refused(self) -> None:
        for target in ("rollback", "receipt"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _old, candidate, _mutations, receipt = self._activate(root)
                activation_root = root / MUTATIONS.ACTIVATION_DIRECTORY
                path = (
                    activation_root / receipt.rollback_file
                    if target == "rollback"
                    else activation_root / f"{receipt.activation_id}.receipt.json"
                )
                path.write_bytes(b"{}\n")
                recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)

                status = recovery.inspect()

                self.assertEqual("blocked", status.state)
                self.assertEqual("invalid_recovery_evidence", status.code)
                self.assertNotIn(str(root), status.message)
                with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError):
                    recovery.restore(
                        receipt.activation_id,
                        expected_registry_digest=MUTATIONS.registry_digest(candidate),
                    )

    def test_historical_duplicates_reconcile_then_restore_original_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, candidate, _mutations, first = self._activate(root)
            current_digest = MUTATIONS.registry_digest(candidate)
            second = plant_duplicate_pending_receipt(root, first)
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)

            status = recovery.inspect()

            self.assertEqual("can_reconcile", status.state)
            self.assertEqual("reconcile_required", status.code)
            self.assertFalse(status.can_restore)
            self.assertTrue(status.can_reconcile)
            self.assertEqual(first.activation_id, status.activation_id)
            self.assertEqual(current_digest, status.current_registry_digest)
            for receipt in (first, second):
                with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError):
                    recovery.restore(
                        receipt.activation_id,
                        expected_registry_digest=current_digest,
                    )

            report = recovery.reconcile(
                first.activation_id, expected_registry_digest=current_digest
            )
            refreshed = report.status

            self.assertEqual("resumed", report.state)
            self.assertEqual("recovery_required", refreshed.state)
            self.assertTrue(refreshed.can_restore)
            self.assertFalse(refreshed.can_reconcile)
            self.assertEqual(first.activation_id, refreshed.activation_id)
            self.assertEqual(
                "superseded",
                MUTATIONS.load_activation_receipt(root, second.activation_id).state,
            )
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))

            result = recovery.restore(
                first.activation_id, expected_registry_digest=current_digest
            )

            self.assertEqual("restored", result.state)
            self.assertEqual(original, REGISTRY.load_connection_registry(root))
            self.assertEqual(
                first.previous_registry_digest, result.restored_registry_digest
            )

    def test_unprovable_duplicate_evidence_exposes_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, _mutations, first = self._activate(root)
            current_digest = MUTATIONS.registry_digest(candidate)
            plant_duplicate_pending_receipt(root, first)
            plant_foreign_pending_receipt(root, first)
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)
            before = byte_tree(root)

            status = recovery.inspect()

            self.assertEqual("blocked", status.state)
            self.assertEqual("multiple_pending_activations", status.code)
            self.assertFalse(status.can_restore)
            self.assertFalse(status.can_reconcile)
            self.assertEqual(
                {
                    "state": "blocked",
                    "code": "multiple_pending_activations",
                    "message": "Multiple connection activations require manual review.",
                    "can_restore": False,
                    "can_reconcile": False,
                    "activation_id": None,
                    "profile_id": None,
                    "current_registry_digest": None,
                },
                RECOVERY.activation_recovery_status_to_document(status),
            )

            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                recovery.reconcile(
                    first.activation_id, expected_registry_digest=current_digest
                )

            self.assertEqual("multiple_pending_activations", caught.exception.code)
            self.assertEqual(before, byte_tree(root))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))

    def test_evidence_arriving_between_render_and_click_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, mutations, first = self._activate(root)
            plant_duplicate_pending_receipt(root, first)
            rendered = RECOVERY.ConnectionRegistryActivationRecoveryService(root).inspect()
            self.assertTrue(rendered.can_reconcile)

            class RacingMutationService:
                def reconcile_activations(self, **request: object):
                    plant_foreign_pending_receipt(root, first)
                    return mutations.reconcile_activations(**request)

            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=RacingMutationService()
            )
            before = byte_tree(root)

            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                recovery.reconcile(
                    rendered.activation_id,
                    expected_registry_digest=rendered.current_registry_digest,
                )

            after = byte_tree(root)

            self.assertEqual("recovery_conflict", caught.exception.code)
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertEqual(before, {path: after[path] for path in before})
            self.assertEqual("pending", MUTATIONS.load_activation_receipt(root, first.activation_id).state)

    def test_stale_registry_digest_from_a_stale_page_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, _mutations, first = self._activate(root)
            plant_duplicate_pending_receipt(root, first)
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)
            self.assertTrue(recovery.inspect().can_reconcile)
            before = byte_tree(root)

            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                recovery.reconcile(
                    first.activation_id,
                    expected_registry_digest="sha256:" + "9" * 64,
                )

            self.assertEqual("recovery_conflict", caught.exception.code)
            self.assertEqual(before, byte_tree(root))
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))

    def test_new_pending_activation_between_inspect_and_restore_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, mutations, first = self._activate(root)
            current_digest = MUTATIONS.registry_digest(candidate)

            class RacingMutationService:
                def restore(self, activation_id: str, *, expected_registry_digest: str):
                    plant_duplicate_pending_receipt(root, first)
                    return mutations.restore(
                        activation_id,
                        expected_registry_digest=expected_registry_digest,
                    )

            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=RacingMutationService()
            )

            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as caught:
                recovery.restore(
                    first.activation_id,
                    expected_registry_digest=current_digest,
                )

            self.assertEqual("recovery_conflict", caught.exception.code)
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))
            self.assertEqual("pending", MUTATIONS.load_activation_receipt(root, first.activation_id).state)

    def test_prepared_after_registry_replace_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            remote = ssh_profile()
            original = registry(PROFILE_A, local, remote)
            candidate = registry(PROFILE_B, local, remote)
            REGISTRY.save_connection_registry(root, original)
            mutations = MUTATIONS.ConnectionRegistryMutationService(root)
            original_digest = MUTATIONS.registry_digest(original)
            proof = mutations.issue_successful_test_proof(
                remote, ready_result(remote), base_registry_digest=original_digest
            )
            with mock.patch.object(
                MUTATIONS,
                "_replace_receipt_if_digest",
                side_effect=RuntimeError("simulated crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    mutations.activate(
                        candidate,
                        PROFILE_B,
                        proof.proof_id,
                        expected_registry_digest=original_digest,
                    )

            status = RECOVERY.ConnectionRegistryActivationRecoveryService(root).inspect()

            self.assertEqual("recovery_required", status.state)
            prepared = MUTATIONS.load_activation_receipt(root, status.activation_id)
            self.assertEqual("prepared", prepared.state)

    def test_prepared_before_registry_replace_is_stale_not_automatically_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            remote = ssh_profile()
            original = registry(PROFILE_A, local, remote)
            candidate = registry(PROFILE_B, local, remote)
            REGISTRY.save_connection_registry(root, original)
            mutations = MUTATIONS.ConnectionRegistryMutationService(root)
            original_digest = MUTATIONS.registry_digest(original)
            proof = mutations.issue_successful_test_proof(
                remote, ready_result(remote), base_registry_digest=original_digest
            )
            with mock.patch.object(
                MUTATIONS,
                "_replace_registry_if_digest",
                side_effect=RuntimeError("simulated crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    mutations.activate(
                        candidate,
                        PROFILE_B,
                        proof.proof_id,
                        expected_registry_digest=original_digest,
                    )

            status = RECOVERY.ConnectionRegistryActivationRecoveryService(root).inspect()

            self.assertEqual("blocked", status.state)
            self.assertEqual("stale_activation", status.code)
            self.assertEqual(original, REGISTRY.load_connection_registry(root))

    def test_activation_record_scan_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            REGISTRY.save_connection_registry(root, registry(PROFILE_A, local))
            activation_root = root / MUTATIONS.ACTIVATION_DIRECTORY
            activation_root.mkdir()
            for index in range(MUTATIONS.MAX_ACTIVATION_RECORDS + 1):
                (activation_root / f"noise-{index}").touch()

            status = RECOVERY.ConnectionRegistryActivationRecoveryService(root).inspect()

            self.assertEqual("blocked", status.state)
            self.assertEqual("invalid_recovery_evidence", status.code)


class ReconciliationOutcomeBoundaryTest(unittest.TestCase):
    """Every completed reconciliation is described from re-read evidence."""

    def _duplicates(self, root: Path):
        """Activate once, then leave two provably no-op duplicate receipts."""

        _original, candidate, mutations, kept = ActivationRecoveryTest()._activate(root)
        first = plant_duplicate_pending_receipt(root, kept)
        second = plant_second_duplicate_pending_receipt(root, kept)
        return candidate, mutations, kept, (first, second)

    def _counted(self, service):
        """Count how many times one reconciliation inspects the records."""

        calls: list[str] = []
        inspect = service.inspect

        def counting_inspect():
            calls.append("inspect")
            return inspect()

        service.inspect = counting_inspect
        return calls

    def _fault_on_second_receipt(self):
        write = MUTATIONS._replace_receipt_if_digest
        calls: list[str] = []

        def faulted(path, receipt, expected_digest):
            calls.append(receipt.activation_id)
            if len(calls) == 2:
                raise MUTATIONS.RegistryConflictError("Activation receipt changed")
            return write(path, receipt, expected_digest)

        return mock.patch.object(
            MUTATIONS, "_replace_receipt_if_digest", side_effect=faulted
        )

    def test_an_incomplete_outcome_is_inspected_again_without_offering_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, planted = self._duplicates(root)
            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            inspections = self._counted(service)

            with self._fault_on_second_receipt():
                report = service.reconcile(
                    advertised.activation_id,
                    expected_registry_digest=advertised.current_registry_digest,
                )

            # The pre-action inspection, then the required fresh one after the
            # partial write.  Skipping the second one was the reported defect.
            self.assertEqual(2, len(inspections))
            self.assertEqual("incomplete", report.state)
            # The exact current status is retained, but an incomplete report is
            # never the one state a click may follow.
            self.assertIsNotNone(report.status)
            self.assertNotEqual("resumed", report.state)
            states = [
                MUTATIONS.load_activation_receipt(root, item.activation_id).state
                for item in planted
            ]
            self.assertEqual(1, states.count("superseded"))
            self.assertEqual(1, states.count("pending"))
            self.assertEqual(
                "pending",
                MUTATIONS.load_activation_receipt(root, kept.activation_id).state,
            )
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))

    def test_an_uncertain_outcome_is_inspected_again_and_stays_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _candidate, mutations, _kept, _planted = self._duplicates(root)
            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            inspections = self._counted(service)
            write = MUTATIONS._replace_receipt_if_digest
            read = MUTATIONS.load_activation_receipt
            committed: list[str] = []

            def record(path, receipt, expected_digest):
                write(path, receipt, expected_digest)
                committed.append(receipt.activation_id)

            def unreadable_after_the_write(state_root, activation_id):
                if committed:
                    raise RuntimeError("Could not inspect activation records")
                return read(state_root, activation_id)

            with (
                mock.patch.object(
                    MUTATIONS, "_replace_receipt_if_digest", side_effect=record
                ),
                mock.patch.object(
                    MUTATIONS,
                    "load_activation_receipt",
                    side_effect=unreadable_after_the_write,
                ),
            ):
                report = service.reconcile(
                    advertised.activation_id,
                    expected_registry_digest=advertised.current_registry_digest,
                )

            # The core could not read its own targets back, and the fresh
            # inspection that follows still has to be attempted.
            self.assertEqual(2, len(inspections))
            self.assertEqual("uncertain", report.state)

    def test_an_unexpected_post_write_failure_is_uncertain_not_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _candidate, mutations, _kept, planted = self._duplicates(root)

            class UnexpectedFailureAfterTheWrites:
                def reconcile_activations(self, **fields: object):
                    mutations.reconcile_activations(**fields)
                    raise KeyError("activation_id")

            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=UnexpectedFailureAfterTheWrites()
            )
            advertised = service.inspect()

            report = service.reconcile(
                advertised.activation_id,
                expected_registry_digest=advertised.current_registry_digest,
            )

            # Records really moved, so this may never be reported as the
            # refusal class whose invariant is that nothing was written.
            self.assertEqual("uncertain", report.state)
            self.assertIsNone(report.status)
            for item in planted:
                self.assertEqual(
                    "superseded",
                    MUTATIONS.load_activation_receipt(root, item.activation_id).state,
                )

    def test_an_unexpected_committed_path_inspection_failure_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _candidate, mutations, _kept, planted = self._duplicates(root)
            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            inspect = service.inspect
            calls: list[str] = []

            def unavailable_after_the_write():
                calls.append("inspect")
                if len(calls) > 1:
                    raise KeyError("activation_id")
                return inspect()

            service.inspect = unavailable_after_the_write

            report = service.reconcile(
                advertised.activation_id,
                expected_registry_digest=advertised.current_registry_digest,
            )

            self.assertEqual(2, len(calls))
            self.assertEqual("uncertain", report.state)
            self.assertIsNone(report.status)
            for item in planted:
                self.assertEqual(
                    "superseded",
                    MUTATIONS.load_activation_receipt(root, item.activation_id).state,
                )


    def _lock_that_fails_to_release(self, error: BaseException):
        """Fail while the core's mutation lock exits, after it has written."""

        real = MUTATIONS.connection_registry_mutation_lock

        @contextlib.contextmanager
        def failing_lock(state_root):
            with real(state_root):
                try:
                    yield
                finally:
                    raise error

        return mock.patch.object(
            MUTATIONS, "connection_registry_mutation_lock", failing_lock
        )

    def test_a_lock_exit_failure_after_a_commit_is_never_a_refusal(self) -> None:
        """An OSError from the lock's exit is not proof that nothing moved."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, planted = self._duplicates(root)
            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            )
            advertised = service.inspect()
            inspections = self._counted(service)

            with self._lock_that_fails_to_release(
                OSError("Could not release the connection registry mutation lock")
            ):
                report = service.reconcile(
                    advertised.activation_id,
                    expected_registry_digest=advertised.current_registry_digest,
                )

            # The duplicates really did move, so the expected-family error may
            # not be told to the caller as the zero-write refusal class.
            self.assertEqual("uncertain", report.state)
            self.assertNotEqual("resumed", report.state)
            self.assertEqual(2, len(inspections))
            for item in planted:
                self.assertEqual(
                    "superseded",
                    MUTATIONS.load_activation_receipt(root, item.activation_id).state,
                )
                self.assertTrue(
                    (root / MUTATIONS.ACTIVATION_DIRECTORY / item.rollback_file).is_file()
                )
            self.assertEqual(
                "pending",
                MUTATIONS.load_activation_receipt(root, kept.activation_id).state,
            )
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))

    def test_an_expected_family_without_the_core_proof_is_uncertain(self) -> None:
        """Membership of an expected family is not itself a zero-write proof."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _candidate, mutations, _kept, planted = self._duplicates(root)

            class ExpectedFamilyAfterTheWrites:
                def reconcile_activations(self, **fields: object):
                    mutations.reconcile_activations(**fields)
                    raise OSError("Could not release the lock")

            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=ExpectedFamilyAfterTheWrites()
            )
            advertised = service.inspect()

            report = service.reconcile(
                advertised.activation_id,
                expected_registry_digest=advertised.current_registry_digest,
            )

            self.assertEqual("uncertain", report.state)
            for item in planted:
                self.assertEqual(
                    "superseded",
                    MUTATIONS.load_activation_receipt(root, item.activation_id).state,
                )

    def test_a_proven_pre_write_refusal_is_still_raised_precisely(self) -> None:
        """Narrowing the refusal class may not blunt a real zero-write refusal."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, mutations, kept, planted = self._duplicates(root)
            advertised = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=mutations
            ).inspect()
            before = byte_tree(root)

            class EvidenceArrivesBeforeTheCoreWrites:
                def reconcile_activations(self, **fields: object):
                    plant_foreign_pending_receipt(root, kept)
                    return mutations.reconcile_activations(**fields)

            service = RECOVERY.ConnectionRegistryActivationRecoveryService(
                root, mutation_service=EvidenceArrivesBeforeTheCoreWrites()
            )

            with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError) as refused:
                service.reconcile(
                    advertised.activation_id,
                    expected_registry_digest=advertised.current_registry_digest,
                )

            after = byte_tree(root)
            self.assertEqual("recovery_conflict", refused.exception.code)
            self.assertEqual(before, {path: after[path] for path in before})
            for item in planted:
                self.assertEqual(
                    "pending",
                    MUTATIONS.load_activation_receipt(root, item.activation_id).state,
                )
            self.assertEqual(candidate, REGISTRY.load_connection_registry(root))


if __name__ == "__main__":
    unittest.main()
