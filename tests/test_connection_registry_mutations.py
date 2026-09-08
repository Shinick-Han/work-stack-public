from __future__ import annotations

import contextlib
import importlib.util
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
SPEC = importlib.util.spec_from_file_location(
    "connection_registry_mutations_test",
    SHELL / "connection_registry_mutations.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
import connection_registry as REGISTRY


PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"


def local_profile(data_dir: Path, **changes: object):
    values = {
        "profile_id": PROFILE_A,
        "label": "Local",
        "data_dir": str(data_dir.absolute()),
        "expected_workspace_id": WORKSPACE_A,
        "enabled": True,
        "live_updates": True,
    }
    values.update(changes)
    return REGISTRY.LocalConnectionProfile(**values)


def ssh_profile(**changes: object):
    values = {
        "profile_id": PROFILE_B,
        "label": "Remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "expected_workspace_id": WORKSPACE_B,
        "preferred_forward_port": 18765,
        "remote_port": 8765,
        "enabled": True,
        "live_updates": True,
    }
    values.update(changes)
    return REGISTRY.SshConnectionProfile(**values)


def registry(active: str, *profiles: object):
    return REGISTRY.ConnectionRegistry(1, active, tuple(profiles))


def ready_result(profile: object):
    return MODULE.ProfileTestResult(
        profile.profile_id,
        profile.kind,
        "ready",
        profile.expected_workspace_id,
        "1.0.6",
        1,
    )


def plant_pending_receipt(root: Path, template, seed: str, rollback: bytes, **overrides):
    """Persist the extra pending evidence a build before this repair could leave."""

    activation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
    planted = replace(
        template,
        activation_id=activation_id,
        state="pending",
        rollback_file=f"{activation_id}.rollback.json",
        **overrides,
    )
    records = root / MODULE.ACTIVATION_DIRECTORY
    (records / planted.rollback_file).write_bytes(rollback)
    (records / f"{activation_id}.receipt.json").write_bytes(
        MODULE._receipt_bytes(planted)
    )
    return planted


def byte_tree(root: Path) -> dict[str, bytes]:
    """Snapshot every file under the state root for an exact no-write oracle."""

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def receipt_names(root: Path) -> set[str]:
    return {path.name for path in (root / MODULE.ACTIVATION_DIRECTORY).iterdir()}


class ConnectionRegistryMutationTest(unittest.TestCase):
    def test_digest_is_canonical_and_absent_state_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = registry(PROFILE_A, local_profile(Path(directory) / "ssot"))
            document = REGISTRY.registry_to_document(current)
            self.assertEqual(MODULE.registry_digest(current), MODULE.registry_digest(document))
            self.assertRegex(MODULE.registry_digest(None), r"^sha256:[0-9a-f]{64}$")
            self.assertNotEqual(MODULE.registry_digest(None), MODULE.registry_digest(current))

    def test_metadata_save_uses_cas_and_preserves_active_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "never-created")
            current = registry(PROFILE_A, local)
            REGISTRY.save_connection_registry(root, current)
            expected = MODULE.registry_digest(current)
            service = MODULE.ConnectionRegistryMutationService(root)
            candidate = registry(
                PROFILE_A,
                replace(local, label="Renamed", live_updates=False),
                ssh_profile(enabled=False),
            )

            saved, saved_digest = service.save_metadata(
                candidate, expected_registry_digest=expected
            )

            self.assertEqual(saved, REGISTRY.load_connection_registry(root))
            self.assertEqual(saved_digest, MODULE.registry_digest(saved))
            self.assertFalse((root / "never-created").exists())

            changed_authority = registry(
                PROFILE_A,
                replace(saved.profiles[0], data_dir=str(root / "other")),
                saved.profiles[1],
            )
            with self.assertRaisesRegex(RuntimeError, "active profile authority"):
                service.save_metadata(
                    changed_authority, expected_registry_digest=saved_digest
                )

            switched = registry(
                PROFILE_B, saved.profiles[0], replace(saved.profiles[1], enabled=True)
            )
            with self.assertRaisesRegex(RuntimeError, "cannot change the active"):
                service.save_metadata(switched, expected_registry_digest=saved_digest)

    def test_metadata_save_rejects_stale_digest_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            original = registry(PROFILE_A, local)
            REGISTRY.save_connection_registry(root, original)
            stale = MODULE.registry_digest(original)
            external = registry(PROFILE_A, replace(local, label="External"))
            REGISTRY.save_connection_registry(root, external)
            service = MODULE.ConnectionRegistryMutationService(root)

            with self.assertRaises(MODULE.RegistryConflictError):
                service.save_metadata(
                    registry(PROFILE_A, replace(local, label="Mine")),
                    expected_registry_digest=stale,
                )

            self.assertEqual(REGISTRY.load_connection_registry(root), external)

    def test_test_proof_is_exact_bounded_and_expires(self) -> None:
        clock = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            current = registry(PROFILE_A, local)
            REGISTRY.save_connection_registry(root, current)
            service = MODULE.ConnectionRegistryMutationService(
                root, monotonic_clock=lambda: clock[0], proof_ttl_seconds=5
            )
            proof = service.issue_successful_test_proof(
                local,
                ready_result(local),
                base_registry_digest=MODULE.registry_digest(current),
            )
            self.assertEqual(proof.profile_digest, MODULE.profile_digest(local))

            changed = replace(local, label="Changed after Test")
            candidate = registry(PROFILE_A, changed)
            with self.assertRaisesRegex(MODULE.ActivationProofError, "exact"):
                service.activate(
                    candidate,
                    PROFILE_A,
                    proof.proof_id,
                    expected_registry_digest=MODULE.registry_digest(current),
                )

            clock[0] = proof.expires_at
            with self.assertRaisesRegex(MODULE.ActivationProofError, "expired"):
                service.activate(
                    current,
                    PROFILE_A,
                    proof.proof_id,
                    expected_registry_digest=MODULE.registry_digest(current),
                )

    def test_proof_rejects_identity_mismatch_or_unready_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "ssot")
            current = registry(PROFILE_A, local)
            REGISTRY.save_connection_registry(root, current)
            service = MODULE.ConnectionRegistryMutationService(root)
            mismatch = replace(ready_result(local), actual_workspace_id=WORKSPACE_B)
            candidate = MODULE.ProfileTestResult(
                local.profile_id, "local", "candidate", None, None, None
            )
            for result in (mismatch, candidate):
                with self.subTest(result=result), self.assertRaises(
                    MODULE.ActivationProofError
                ):
                    service.issue_successful_test_proof(
                        local,
                        result,
                        base_registry_digest=MODULE.registry_digest(current),
                    )

            with self.assertRaises(MODULE.RegistryConflictError):
                service.issue_successful_test_proof(
                    local,
                    ready_result(local),
                    base_registry_digest=MODULE.registry_digest(None),
                )

    def _activate_remote(self, root: Path):
        local = local_profile(root / "local-ssot")
        remote = ssh_profile()
        current = registry(PROFILE_A, local, remote)
        REGISTRY.save_connection_registry(root, current)
        service = MODULE.ConnectionRegistryMutationService(root)
        base_digest = MODULE.registry_digest(current)
        proof = service.issue_successful_test_proof(
            remote, ready_result(remote), base_registry_digest=base_digest
        )
        candidate = registry(PROFILE_B, local, remote)
        receipt = service.activate(
            candidate,
            PROFILE_B,
            proof.proof_id,
            expected_registry_digest=base_digest,
        )
        return service, current, candidate, proof, receipt

    def test_activation_persists_exact_rollback_and_pending_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, current, candidate, proof, receipt = self._activate_remote(root)

            self.assertEqual(receipt.state, "pending")
            self.assertEqual(receipt.profile_id, PROFILE_B)
            self.assertEqual(
                receipt.activated_registry_digest, MODULE.registry_digest(candidate)
            )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)
            self.assertEqual(
                MODULE.load_activation_receipt(root, receipt.activation_id), receipt
            )
            rollback = root / MODULE.ACTIVATION_DIRECTORY / receipt.rollback_file
            self.assertEqual(
                MODULE._registry_from_bytes(rollback.read_bytes(), "rollback"), current
            )

            with self.assertRaises(MODULE.ActivationProofError):
                service.activate(
                    candidate,
                    PROFILE_B,
                    proof.proof_id,
                    expected_registry_digest=MODULE.registry_digest(candidate),
                )

    def test_pending_activation_lookup_is_digest_bound_and_closes_after_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _current, candidate, _proof, receipt = self._activate_remote(root)
            digest = MODULE.registry_digest(candidate)

            self.assertEqual(
                MODULE.pending_activation_for_registry(root, digest), receipt
            )
            self.assertIsNone(
                MODULE.pending_activation_for_registry(root, MODULE.registry_digest(None))
            )
            service.confirm(receipt.activation_id, expected_registry_digest=digest)
            self.assertIsNone(MODULE.pending_activation_for_registry(root, digest))

    def test_restore_is_explicit_cas_and_closes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, current, candidate, _proof, receipt = self._activate_remote(root)

            restored = service.restore(
                receipt.activation_id,
                expected_registry_digest=MODULE.registry_digest(candidate),
            )

            self.assertEqual(restored.state, "restored")
            self.assertEqual(REGISTRY.load_connection_registry(root), current)
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                service.confirm(
                    receipt.activation_id,
                    expected_registry_digest=MODULE.registry_digest(current),
                )

    def test_confirm_requires_exact_activated_digest_and_disables_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _current, candidate, _proof, receipt = self._activate_remote(root)
            with self.assertRaises(MODULE.RegistryConflictError):
                service.confirm(
                    receipt.activation_id,
                    expected_registry_digest=MODULE.registry_digest(None),
                )

            confirmed = service.confirm(
                receipt.activation_id,
                expected_registry_digest=MODULE.registry_digest(candidate),
            )

            self.assertEqual(confirmed.state, "confirmed")
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                service.restore(
                    receipt.activation_id,
                    expected_registry_digest=MODULE.registry_digest(candidate),
                )

    def test_tampered_rollback_blocks_restore_without_changing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _current, candidate, _proof, receipt = self._activate_remote(root)
            rollback = root / MODULE.ACTIVATION_DIRECTORY / receipt.rollback_file
            rollback.write_text("{}", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                service.restore(
                    receipt.activation_id,
                    expected_registry_digest=MODULE.registry_digest(candidate),
                )

            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_prepared_receipt_remains_explicitly_restorable_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "local-ssot")
            remote = ssh_profile()
            current = registry(PROFILE_A, local, remote)
            candidate = registry(PROFILE_B, local, remote)
            REGISTRY.save_connection_registry(root, current)
            service = MODULE.ConnectionRegistryMutationService(root)
            proof = service.issue_successful_test_proof(
                remote,
                ready_result(remote),
                base_registry_digest=MODULE.registry_digest(current),
            )
            with mock.patch.object(
                MODULE,
                "_replace_receipt_if_digest",
                side_effect=RuntimeError("simulated interruption"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    service.activate(
                        candidate,
                        PROFILE_B,
                        proof.proof_id,
                        expected_registry_digest=MODULE.registry_digest(current),
                    )

            receipt_files = list(
                (root / MODULE.ACTIVATION_DIRECTORY).glob("*.receipt.json")
            )
            self.assertEqual(len(receipt_files), 1)
            activation_id = receipt_files[0].name.removesuffix(".receipt.json")
            self.assertEqual(
                MODULE.load_activation_receipt(root, activation_id).state, "prepared"
            )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

            restored = service.restore(
                activation_id,
                expected_registry_digest=MODULE.registry_digest(candidate),
            )
            self.assertEqual(restored.state, "restored")
            self.assertEqual(REGISTRY.load_connection_registry(root), current)

    def test_failed_same_candidate_retry_continues_the_first_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, current, candidate, _proof, receipt = self._activate_remote(root)
            candidate_digest = MODULE.registry_digest(candidate)
            rollback = root / MODULE.ACTIVATION_DIRECTORY / receipt.rollback_file
            rollback_bytes = rollback.read_bytes()
            names = receipt_names(root)
            remote = ssh_profile()
            # The desktop retries after a failed startup: the registry already
            # carries the candidate, so the new Test proof is bound to it.
            retry_proof = service.issue_successful_test_proof(
                remote, ready_result(remote), base_registry_digest=candidate_digest
            )

            retried = service.activate(
                candidate,
                PROFILE_B,
                retry_proof.proof_id,
                expected_registry_digest=candidate_digest,
            )

            self.assertEqual(receipt, retried)
            self.assertEqual(names, receipt_names(root))
            self.assertEqual(rollback_bytes, rollback.read_bytes())
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)
            self.assertEqual(
                MODULE.pending_activation_for_registry(root, candidate_digest), receipt
            )

            # The original rollback ancestry, not the retry's starting point,
            # remains the state an explicit restore reaches.
            service.restore(
                receipt.activation_id, expected_registry_digest=candidate_digest
            )
            self.assertEqual(REGISTRY.load_connection_registry(root), current)

    def test_retry_completes_an_activation_interrupted_before_its_registry_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = local_profile(root / "local-ssot")
            remote = ssh_profile()
            current = registry(PROFILE_A, local, remote)
            candidate = registry(PROFILE_B, local, remote)
            REGISTRY.save_connection_registry(root, current)
            service = MODULE.ConnectionRegistryMutationService(root)
            base_digest = MODULE.registry_digest(current)
            proof = service.issue_successful_test_proof(
                remote, ready_result(remote), base_registry_digest=base_digest
            )
            with mock.patch.object(
                MODULE,
                "_replace_registry_if_digest",
                side_effect=RuntimeError("simulated crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    service.activate(
                        candidate,
                        PROFILE_B,
                        proof.proof_id,
                        expected_registry_digest=base_digest,
                    )
            self.assertEqual(REGISTRY.load_connection_registry(root), current)
            names = receipt_names(root)
            activation_id = next(
                name.removesuffix(".receipt.json")
                for name in names
                if name.endswith(".receipt.json")
            )
            self.assertEqual(
                MODULE.load_activation_receipt(root, activation_id).state, "prepared"
            )
            retry_proof = service.issue_successful_test_proof(
                remote, ready_result(remote), base_registry_digest=base_digest
            )

            pending = service.activate(
                candidate,
                PROFILE_B,
                retry_proof.proof_id,
                expected_registry_digest=base_digest,
            )

            self.assertEqual(pending.activation_id, activation_id)
            self.assertEqual(pending.state, "pending")
            self.assertEqual(pending.previous_registry_digest, base_digest)
            self.assertEqual(names, receipt_names(root))
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_competing_unconfirmed_activation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, current, candidate, _proof, _receipt = self._activate_remote(root)
            candidate_digest = MODULE.registry_digest(candidate)
            names = receipt_names(root)
            local = local_profile(root / "local-ssot")
            proof = service.issue_successful_test_proof(
                local, ready_result(local), base_registry_digest=candidate_digest
            )

            with self.assertRaises(MODULE.RegistryConflictError) as caught:
                service.activate(
                    current,
                    PROFILE_A,
                    proof.proof_id,
                    expected_registry_digest=candidate_digest,
                )

            self.assertEqual(caught.exception.code, "activation_unconfirmed")
            self.assertEqual(names, receipt_names(root))
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_foreign_attempt_on_the_activated_state_blocks_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _current, candidate, _proof, receipt = self._activate_remote(root)
            candidate_digest = MODULE.registry_digest(candidate)
            plant_pending_receipt(
                root,
                receipt,
                "foreign-attempt",
                (root / REGISTRY.REGISTRY_FILE).read_bytes(),
                profile_id=PROFILE_A,
                previous_registry_digest=candidate_digest,
            )
            names = receipt_names(root)
            remote = ssh_profile()
            retry_proof = service.issue_successful_test_proof(
                remote, ready_result(remote), base_registry_digest=candidate_digest
            )

            with self.assertRaises(MODULE.RegistryConflictError) as caught:
                service.activate(
                    candidate,
                    PROFILE_B,
                    retry_proof.proof_id,
                    expected_registry_digest=candidate_digest,
                )

            self.assertEqual(caught.exception.code, "activation_conflict")
            self.assertEqual(names, receipt_names(root))
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_reconciliation_supersedes_only_provable_no_op_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, current, candidate, _proof, receipt = self._activate_remote(root)
            candidate_digest = MODULE.registry_digest(candidate)
            duplicate = plant_pending_receipt(
                root,
                receipt,
                "duplicate-retry",
                (root / REGISTRY.REGISTRY_FILE).read_bytes(),
                previous_registry_digest=candidate_digest,
            )
            with self.assertRaises(RuntimeError):
                MODULE.pending_activation_for_registry(root, candidate_digest)

            reconciliation = service.reconcile_activations(
                expected_registry_digest=candidate_digest
            )

            self.assertEqual(reconciliation.kept_activation_id, receipt.activation_id)
            self.assertEqual(
                reconciliation.superseded_activation_ids, (duplicate.activation_id,)
            )
            self.assertEqual(
                MODULE.pending_activation_for_registry(root, candidate_digest), receipt
            )
            superseded = MODULE.load_activation_receipt(root, duplicate.activation_id)
            self.assertEqual(superseded, replace(duplicate, state="superseded"))
            self.assertTrue(
                (root / MODULE.ACTIVATION_DIRECTORY / duplicate.rollback_file).is_file()
            )
            for method in (service.restore, service.confirm):
                with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                    method(
                        duplicate.activation_id,
                        expected_registry_digest=candidate_digest,
                    )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_reconciliation_refuses_unprovable_evidence_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _current, candidate, _proof, receipt = self._activate_remote(root)
            candidate_digest = MODULE.registry_digest(candidate)
            unprovable = plant_pending_receipt(
                root,
                receipt,
                "unprovable",
                (root / REGISTRY.REGISTRY_FILE).read_bytes(),
                previous_registry_digest=MODULE.registry_digest(None),
                activated_registry_digest=MODULE.registry_digest(None),
            )

            with self.assertRaises(MODULE.RegistryConflictError) as caught:
                service.reconcile_activations(
                    expected_registry_digest=candidate_digest
                )

            self.assertEqual(caught.exception.code, "activation_manual_review")
            for persisted in (receipt, unprovable):
                self.assertEqual(
                    MODULE.load_activation_receipt(root, persisted.activation_id),
                    persisted,
                )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)


    def _two_no_op_duplicates(self, root: Path):
        service, _current, candidate, _proof, receipt = self._activate_remote(root)
        candidate_digest = MODULE.registry_digest(candidate)
        live = (root / REGISTRY.REGISTRY_FILE).read_bytes()
        planted = tuple(
            plant_pending_receipt(
                root,
                receipt,
                seed,
                live,
                previous_registry_digest=candidate_digest,
            )
            for seed in ("duplicate-one", "duplicate-two")
        )
        return service, candidate, candidate_digest, receipt, planted

    def test_a_committed_reconciliation_reports_every_record_it_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, receipt, planted = self._two_no_op_duplicates(root)

            outcome = service.reconcile_activations(
                expected_registry_digest=digest,
                expected_kept_activation_id=receipt.activation_id,
            )

            self.assertEqual("committed", outcome.state)
            self.assertEqual(receipt.activation_id, outcome.kept_activation_id)
            self.assertEqual(
                {item.activation_id for item in planted},
                set(outcome.superseded_activation_ids),
            )
            self.assertEqual((), outcome.unresolved_activation_ids)
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_a_reconciliation_that_would_keep_another_receipt_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, _receipt, planted = self._two_no_op_duplicates(root)
            before = byte_tree(root)

            with self.assertRaises(MODULE.RegistryConflictError):
                service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=planted[0].activation_id,
                )

            self.assertEqual(before, byte_tree(root))
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_a_kept_receipt_with_broken_rollback_bytes_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, receipt, planted = self._two_no_op_duplicates(root)
            (root / MODULE.ACTIVATION_DIRECTORY / receipt.rollback_file).write_bytes(b"{}")
            before = byte_tree(root)

            with self.assertRaises(RuntimeError):
                service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertEqual(before, byte_tree(root))
            for planted_receipt in planted:
                self.assertEqual(
                    "pending",
                    MODULE.load_activation_receipt(
                        root, planted_receipt.activation_id
                    ).state,
                )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_a_kept_receipt_bound_to_another_profile_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, receipt, _planted = self._two_no_op_duplicates(root)
            path = MODULE._receipt_path(root, receipt.activation_id)
            path.write_bytes(
                MODULE._receipt_bytes(
                    replace(receipt, profile_digest="sha256:" + "c" * 64)
                )
            )
            before = byte_tree(root)

            with self.assertRaises(MODULE.RegistryConflictError):
                service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertEqual(before, byte_tree(root))
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_a_failed_second_transition_reports_incomplete_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, receipt, planted = self._two_no_op_duplicates(root)
            write = MODULE._replace_receipt_if_digest
            calls: list[str] = []

            def fault_on_second(path, target, expected_digest):
                calls.append(target.activation_id)
                if len(calls) == 2:
                    raise MODULE.RegistryConflictError("Activation receipt changed")
                return write(path, target, expected_digest)

            with mock.patch.object(
                MODULE, "_replace_receipt_if_digest", side_effect=fault_on_second
            ):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            # A partial write is reported as one, never raised as a refusal.
            self.assertEqual("incomplete", outcome.state)
            self.assertEqual(receipt.activation_id, outcome.kept_activation_id)
            self.assertEqual(1, len(outcome.superseded_activation_ids))
            self.assertEqual(1, len(outcome.unresolved_activation_ids))
            self.assertEqual(
                {item.activation_id for item in planted},
                set(outcome.superseded_activation_ids + outcome.unresolved_activation_ids),
            )
            for activation_id in outcome.superseded_activation_ids:
                self.assertEqual(
                    "superseded",
                    MODULE.load_activation_receipt(root, activation_id).state,
                )
            for activation_id in outcome.unresolved_activation_ids:
                self.assertEqual(
                    "pending", MODULE.load_activation_receipt(root, activation_id).state
                )
            for item in (receipt, *planted):
                self.assertTrue(
                    (root / MODULE.ACTIVATION_DIRECTORY / item.rollback_file).is_file()
                )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_records_that_cannot_be_read_back_report_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _candidate, digest, receipt, _planted = self._two_no_op_duplicates(root)

            with mock.patch.object(
                MODULE,
                "load_activation_receipt",
                side_effect=RuntimeError("Could not inspect activation records"),
            ):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertEqual("uncertain", outcome.state)
            self.assertEqual((), outcome.superseded_activation_ids)
            self.assertEqual((), outcome.unresolved_activation_ids)
            # Nothing was read back, so every target is explicitly unknown
            # rather than silently dropped or reported as still pending.
            self.assertEqual(
                {item.activation_id for item in _planted},
                set(outcome.unknown_activation_ids),
            )

    def test_an_unexpected_second_transition_fault_still_reports_from_disk(self) -> None:
        """An ordinary unexpected error after a write may not become a refusal."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, receipt, planted = self._two_no_op_duplicates(root)
            write = MODULE._replace_receipt_if_digest
            calls: list[str] = []

            def unexpected_on_second(path, target, expected_digest):
                calls.append(target.activation_id)
                if len(calls) == 2:
                    raise KeyError("activation_id")
                return write(path, target, expected_digest)

            with mock.patch.object(
                MODULE, "_replace_receipt_if_digest", side_effect=unexpected_on_second
            ):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertEqual("incomplete", outcome.state)
            self.assertEqual(receipt.activation_id, outcome.kept_activation_id)
            self.assertEqual(1, len(outcome.superseded_activation_ids))
            self.assertEqual(1, len(outcome.unresolved_activation_ids))
            self.assertEqual((), outcome.unknown_activation_ids)
            self.assertEqual(
                {item.activation_id for item in planted},
                set(outcome.superseded_activation_ids + outcome.unresolved_activation_ids),
            )
            for activation_id in outcome.superseded_activation_ids:
                self.assertEqual(
                    "superseded",
                    MODULE.load_activation_receipt(root, activation_id).state,
                )
            for activation_id in outcome.unresolved_activation_ids:
                self.assertEqual(
                    "pending", MODULE.load_activation_receipt(root, activation_id).state
                )
            for item in (receipt, *planted):
                self.assertTrue(
                    (root / MODULE.ACTIVATION_DIRECTORY / item.rollback_file).is_file()
                )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_a_control_exception_after_a_write_is_never_swallowed(self) -> None:
        """Process termination must still propagate out of the transition loop."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _candidate, digest, receipt, _planted = self._two_no_op_duplicates(root)
            write = MODULE._replace_receipt_if_digest
            calls: list[str] = []

            def terminate_on_second(path, target, expected_digest):
                calls.append(target.activation_id)
                if len(calls) == 2:
                    raise KeyboardInterrupt
                return write(path, target, expected_digest)

            with mock.patch.object(
                MODULE, "_replace_receipt_if_digest", side_effect=terminate_on_second
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.reconcile_activations(
                        expected_registry_digest=digest,
                        expected_kept_activation_id=receipt.activation_id,
                    )

    def test_a_partly_readable_result_keeps_every_state_it_proved(self) -> None:
        """An uncertain read-back reports the progress it actually observed."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _candidate, digest, receipt, planted = self._two_no_op_duplicates(root)
            read = MODULE.load_activation_receipt
            reads: list[str] = []

            def unreadable_after_the_first(state_root, activation_id):
                if activation_id in {item.activation_id for item in planted}:
                    reads.append(activation_id)
                    if len(reads) == 2:
                        raise RuntimeError("Could not inspect activation records")
                return read(state_root, activation_id)

            with mock.patch.object(
                MODULE,
                "load_activation_receipt",
                side_effect=unreadable_after_the_first,
            ):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            # Both receipts really did commit; the first was read back and the
            # second was not, so exactly one is proven and one is unknown.
            for item in planted:
                self.assertEqual(
                    "superseded",
                    MODULE.load_activation_receipt(root, item.activation_id).state,
                )
            self.assertEqual("uncertain", outcome.state)
            self.assertEqual((reads[0],), outcome.superseded_activation_ids)
            self.assertEqual((), outcome.unresolved_activation_ids)
            self.assertEqual((reads[1],), outcome.unknown_activation_ids)
            self.assertNotIn(reads[1], outcome.unresolved_activation_ids)


    def _lock_that_fails_to_release(self, error: BaseException):
        """Fail while the mutation lock exits, exactly as its own finally can.

        The real lock is still taken and still released; the failure escapes
        from the ``with`` statement after the core has already computed its
        result, which is the one post-write path a return statement cannot
        cover.
        """

        real = MODULE.connection_registry_mutation_lock

        @contextlib.contextmanager
        def failing_lock(state_root):
            with real(state_root):
                try:
                    yield
                finally:
                    raise error

        return mock.patch.object(
            MODULE, "connection_registry_mutation_lock", failing_lock
        )

    def test_a_lock_release_fault_after_a_write_is_reported_not_raised(self) -> None:
        """Releasing the lock happens after the result; it may not refuse."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, candidate, digest, receipt, planted = self._two_no_op_duplicates(root)

            with self._lock_that_fails_to_release(
                OSError("Could not release the connection registry mutation lock")
            ):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            # Every duplicate really did move, so the progress is reported and
            # the run is only denied the claim that it completed cleanly.
            self.assertEqual("uncertain", outcome.state)
            self.assertEqual(receipt.activation_id, outcome.kept_activation_id)
            self.assertEqual(
                {item.activation_id for item in planted},
                set(outcome.superseded_activation_ids),
            )
            self.assertEqual((), outcome.unresolved_activation_ids)
            self.assertEqual((), outcome.unknown_activation_ids)
            for item in planted:
                self.assertEqual(
                    "superseded",
                    MODULE.load_activation_receipt(root, item.activation_id).state,
                )
                self.assertTrue(
                    (root / MODULE.ACTIVATION_DIRECTORY / item.rollback_file).is_file()
                )
            self.assertEqual(
                "pending",
                MODULE.load_activation_receipt(root, receipt.activation_id).state,
            )
            self.assertEqual(REGISTRY.load_connection_registry(root), candidate)

    def test_a_lock_release_fault_after_a_partial_write_keeps_its_states(self) -> None:
        """An already partial run keeps the exact states it proved."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _candidate, digest, receipt, planted = self._two_no_op_duplicates(root)
            write = MODULE._replace_receipt_if_digest
            calls: list[str] = []

            def fault_on_second(path, target, expected_digest):
                calls.append(target.activation_id)
                if len(calls) == 2:
                    raise MODULE.RegistryConflictError("Activation receipt changed")
                return write(path, target, expected_digest)

            with (
                mock.patch.object(
                    MODULE, "_replace_receipt_if_digest", side_effect=fault_on_second
                ),
                self._lock_that_fails_to_release(OSError("Could not release the lock")),
            ):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertEqual("incomplete", outcome.state)
            self.assertEqual(1, len(outcome.superseded_activation_ids))
            self.assertEqual(1, len(outcome.unresolved_activation_ids))
            self.assertEqual(
                {item.activation_id for item in planted},
                set(outcome.superseded_activation_ids + outcome.unresolved_activation_ids),
            )

    def test_a_control_exception_while_the_lock_exits_still_propagates(self) -> None:
        """Process termination is never converted into a reported outcome."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _candidate, digest, receipt, planted = self._two_no_op_duplicates(root)

            with self._lock_that_fails_to_release(KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    service.reconcile_activations(
                        expected_registry_digest=digest,
                        expected_kept_activation_id=receipt.activation_id,
                    )

            for item in planted:
                self.assertEqual(
                    "superseded",
                    MODULE.load_activation_receipt(root, item.activation_id).state,
                )

    def test_only_the_proving_phase_marks_its_errors_as_zero_write(self) -> None:
        """The zero-write proof is the core's own, never the error's family."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _candidate, digest, receipt, _planted = self._two_no_op_duplicates(root)

            with self.assertRaises(MODULE.RegistryConflictError) as refused:
                service.reconcile_activations(
                    expected_registry_digest="sha256:" + "9" * 64,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertTrue(
                MODULE.reconciliation_proved_zero_write(refused.exception)
            )
            # The same ordinary family raised while the lock exits carries no
            # such proof, which is why it is never reported as a refusal.
            self.assertFalse(MODULE.reconciliation_proved_zero_write(OSError("release")))

            with self._lock_that_fails_to_release(OSError("release")):
                outcome = service.reconcile_activations(
                    expected_registry_digest=digest,
                    expected_kept_activation_id=receipt.activation_id,
                )

            self.assertEqual("uncertain", outcome.state)


if __name__ == "__main__":
    unittest.main()
