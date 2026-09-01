from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import connection_registry as REGISTRY
import connection_registry_mutations as MUTATIONS
import profile_inspection as INSPECTION
import v4_activation_binding as BINDING


PROFILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
REGISTRY_DIGEST = "sha256:" + "1" * 64
PREVIOUS_DIGEST = "sha256:" + "2" * 64
ROLLBACK_DIGEST = "sha256:" + "3" * 64
MANIFEST_DIGEST = "sha256:" + "4" * 64


def profile(**changes: object) -> REGISTRY.LocalConnectionProfile:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "label": "Normalized",
        "data_dir": "C:\\work-stack\\normalized",
        "expected_workspace_id": WORKSPACE_ID,
        "enabled": True,
        "live_updates": True,
    }
    values.update(changes)
    return REGISTRY.LocalConnectionProfile(**values)  # type: ignore[arg-type]


def ready_result(**changes: object) -> INSPECTION.ProfileTestResult:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "kind": "local",
        "status": "ready",
        "actual_workspace_id": WORKSPACE_ID,
        "product_version": "1.0.6",
        "protocol_version": 1,
        "authority": INSPECTION.AuthorityInspection(
            storage_format="v4",
            schema_version=4,
            authority_manifest_digest=MANIFEST_DIGEST,
            capabilities=INSPECTION.AuthorityCapabilities(
                read=True, write=False, migrate=False, projection=True
            ),
        ),
    }
    values.update(changes)
    return INSPECTION.ProfileTestResult(**values)  # type: ignore[arg-type]


def proof() -> BINDING.V4ActivationProof:
    return BINDING.issue_v4_activation_proof(
        profile(),
        ready_result(),
        registry_digest=REGISTRY_DIGEST,
        enable_v4_activation=True,
    )


def receipt() -> BINDING.V4ActivationReceipt:
    return BINDING.prepare_v4_activation_receipt(
        proof(),
        previous_registry_digest=PREVIOUS_DIGEST,
        rollback_artifact_digest=ROLLBACK_DIGEST,
        enable_v4_activation=True,
    )


class V4ActivationBindingTest(unittest.TestCase):
    def test_contract_is_default_off_and_does_not_change_v3_service(self) -> None:
        with self.assertRaises(BINDING.V4ActivationDisabledError):
            BINDING.issue_v4_activation_proof(
                profile(), ready_result(), registry_digest=REGISTRY_DIGEST
            )
        self.assertEqual(1, MUTATIONS.ACTIVATION_RECEIPT_VERSION)
        self.assertNotIn("v4", MUTATIONS.ActivationReceipt.__annotations__)

    def test_proof_binds_every_v4_authority_coordinate(self) -> None:
        issued = proof()
        coordinates = issued.coordinates
        self.assertEqual(PROFILE_ID, coordinates.profile_id)
        self.assertEqual(MUTATIONS.profile_digest(profile()), coordinates.profile_digest)
        self.assertEqual(REGISTRY_DIGEST, coordinates.registry_digest)
        self.assertEqual(WORKSPACE_ID, coordinates.workspace_uid)
        self.assertEqual(("v4", 4), (coordinates.storage_format, coordinates.schema_version))
        self.assertEqual(MANIFEST_DIGEST, coordinates.authority_manifest_digest)
        document = BINDING.v4_activation_proof_to_document(issued)
        self.assertEqual("workstack.v4-activation-proof", document["contract"])
        self.assertEqual(1, document["contract_version"])
        self.assertEqual(issued, BINDING.v4_activation_proof_from_document(document))

    def test_non_v4_or_unready_inspection_cannot_issue_proof(self) -> None:
        v3 = dataclasses.replace(
            ready_result().authority, storage_format="v3", schema_version=3
        )
        cases = (
            ready_result(authority=v3),
            ready_result(status="identity_mismatch"),
            ready_result(actual_workspace_id="22222222-2222-4222-8222-222222222222"),
            ready_result(authority=None),
        )
        for result in cases:
            with self.subTest(result=result), self.assertRaises(
                BINDING.V4ActivationBindingError
            ):
                BINDING.issue_v4_activation_proof(
                    profile(),
                    result,
                    registry_digest=REGISTRY_DIGEST,
                    enable_v4_activation=True,
                )

    def test_receipt_is_versioned_and_preserves_exact_rollback_binding(self) -> None:
        pending = receipt()
        document = BINDING.v4_activation_receipt_to_document(pending)
        self.assertEqual("workstack.v4-activation-receipt", document["contract"])
        self.assertEqual(1, document["contract_version"])
        self.assertEqual(PREVIOUS_DIGEST, document["previous_registry_digest"])
        self.assertEqual(ROLLBACK_DIGEST, document["rollback_artifact_digest"])
        self.assertRegex(document["proof_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(pending.rollback_available)
        self.assertEqual(
            pending, BINDING.v4_activation_receipt_from_document(document)
        )

    def test_restart_confirmation_reinspects_and_matches_all_coordinates(self) -> None:
        calls = []

        def reinspect() -> INSPECTION.ProfileTestResult:
            calls.append(True)
            return ready_result()

        pending = receipt()
        confirmed = BINDING.confirm_v4_activation_after_restart(
            pending,
            profile(),
            current_registry_digest=REGISTRY_DIGEST,
            reinspect=reinspect,
            enable_v4_activation=True,
        )
        self.assertEqual([True], calls)
        self.assertEqual("confirmed", confirmed.state)
        self.assertFalse(confirmed.rollback_available)
        self.assertEqual(pending.rollback_artifact_digest, confirmed.rollback_artifact_digest)

    def test_any_restart_mismatch_leaves_original_rollback_available(self) -> None:
        pending = receipt()
        cases = (
            (profile(label="changed"), REGISTRY_DIGEST, ready_result()),
            (profile(), "sha256:" + "9" * 64, ready_result()),
            (
                profile(),
                REGISTRY_DIGEST,
                ready_result(authority=dataclasses.replace(
                    ready_result().authority,
                    authority_manifest_digest="sha256:" + "8" * 64,
                )),
            ),
            (
                profile(),
                REGISTRY_DIGEST,
                ready_result(actual_workspace_id="22222222-2222-4222-8222-222222222222"),
            ),
        )
        for candidate, digest, inspected in cases:
            with self.subTest(digest=digest, inspected=inspected), self.assertRaises(
                BINDING.V4ActivationBindingError
            ):
                BINDING.confirm_v4_activation_after_restart(
                    pending,
                    candidate,
                    current_registry_digest=digest,
                    reinspect=lambda value=inspected: value,
                    enable_v4_activation=True,
                )
            self.assertEqual("pending", pending.state)
            self.assertTrue(pending.rollback_available)
            self.assertEqual(ROLLBACK_DIGEST, pending.rollback_artifact_digest)

    def test_invalid_receipt_state_or_future_version_is_fail_closed(self) -> None:
        pending = receipt()
        cases = (
            dataclasses.replace(pending, state="confirmed"),
            dataclasses.replace(pending, contract_version=2),
            dataclasses.replace(pending, rollback_artifact_digest="invalid"),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(
                BINDING.V4ActivationBindingError
            ):
                BINDING.confirm_v4_activation_after_restart(
                    candidate,
                    profile(),
                    current_registry_digest=REGISTRY_DIGEST,
                    reinspect=ready_result,
                    enable_v4_activation=True,
                )

        document = BINDING.v4_activation_receipt_to_document(pending)
        for changed in (
            {**document, "contract_version": 2},
            {**document, "unexpected": True},
            {key: value for key, value in document.items() if key != "coordinates"},
        ):
            with self.subTest(changed=changed), self.assertRaises(
                BINDING.V4ActivationBindingError
            ):
                BINDING.v4_activation_receipt_from_document(changed)


if __name__ == "__main__":
    unittest.main()
