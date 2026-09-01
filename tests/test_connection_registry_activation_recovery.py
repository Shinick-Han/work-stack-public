from __future__ import annotations

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

    def test_multiple_pending_activations_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, mutations, first = self._activate(root)
            remote = ssh_profile()
            current_digest = MUTATIONS.registry_digest(candidate)
            proof = mutations.issue_successful_test_proof(
                remote,
                ready_result(remote),
                base_registry_digest=current_digest,
            )
            second = mutations.activate(
                candidate,
                PROFILE_B,
                proof.proof_id,
                expected_registry_digest=current_digest,
            )
            recovery = RECOVERY.ConnectionRegistryActivationRecoveryService(root)

            status = recovery.inspect()

            self.assertEqual("blocked", status.state)
            self.assertEqual("multiple_pending_activations", status.code)
            for receipt in (first, second):
                with self.assertRaises(RECOVERY.ActivationRecoveryRefusedError):
                    recovery.restore(
                        receipt.activation_id,
                        expected_registry_digest=current_digest,
                    )

    def test_new_pending_activation_between_inspect_and_restore_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _old, candidate, mutations, first = self._activate(root)
            current_digest = MUTATIONS.registry_digest(candidate)
            remote = ssh_profile()

            class RacingMutationService:
                def restore(self, activation_id: str, *, expected_registry_digest: str):
                    proof = mutations.issue_successful_test_proof(
                        remote,
                        ready_result(remote),
                        base_registry_digest=current_digest,
                    )
                    mutations.activate(
                        candidate,
                        PROFILE_B,
                        proof.proof_id,
                        expected_registry_digest=current_digest,
                    )
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


if __name__ == "__main__":
    unittest.main()
