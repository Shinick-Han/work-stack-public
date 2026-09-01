from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
