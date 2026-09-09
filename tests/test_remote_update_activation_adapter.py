"""Adapter tests against the real connection registry services on disposable state.

Nothing here mocks the registry: every test builds a throwaway desktop state
root, saves a real ``connection-registry.json`` through the product's own
``save_connection_registry``, issues a real short-lived Test proof through
``ConnectionRegistryMutationService`` and drives the real activation, confirm,
restore and recovery-inspection code paths.  Only two things are simulated, and
both are simulated as the real world produces them: a lost response (the service
commits and the caller never sees the answer) and pre-existing receipt evidence
an older or foreign writer could have left on disk.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import connection_registry as REGISTRY  # noqa: E402
from connection_activation_evidence import (  # noqa: E402
    ACTIVATION_DIRECTORY,
    _receipt_bytes,
    activation_receipts,
)
from connection_registry_mutations import (  # noqa: E402
    ConnectionRegistryMutationService,
    registry_digest,
)
from profile_inspection import ProfileTestResult  # noqa: E402
from remote_update_activation_adapter import (  # noqa: E402
    ActivationAdapterRefusal,
    ActivationCandidate,
    ActivationResponseLost,
    RegistryActivationAdapter,
)
from remote_update_activation_binding import (  # noqa: E402
    BINDING_FILE,
    ActivationBindingError,
    ActivationBindingStore,
)

PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"
OPERATION = "op-activation_issue-0001"
OTHER_OPERATION = "op-activation_issue-0002"


def local_profile(data_dir: Path, profile_id: str, workspace_id: str, label: str):
    return REGISTRY.LocalConnectionProfile(
        profile_id=profile_id,
        label=label,
        data_dir=str(data_dir.absolute()),
        expected_workspace_id=workspace_id,
        enabled=True,
        live_updates=True,
    )


def ready_result(profile) -> ProfileTestResult:
    return ProfileTestResult(
        profile.profile_id,
        profile.kind,
        "ready",
        profile.expected_workspace_id,
        "1.0.13",
        1,
    )


def registry_of(active: str, *profiles):
    return REGISTRY.ConnectionRegistry(1, active, tuple(profiles))


def registry_bytes(root: Path) -> bytes:
    return (root / REGISTRY.REGISTRY_FILE).read_bytes()


def evidence_tree(root: Path) -> dict[str, bytes]:
    """Every byte of registry and activation evidence, the no-write oracle."""

    tree = {REGISTRY.REGISTRY_FILE: registry_bytes(root)}
    records = root / ACTIVATION_DIRECTORY
    if records.exists():
        for path in sorted(records.iterdir()):
            if path.is_file():
                tree[f"{ACTIVATION_DIRECTORY}/{path.name}"] = path.read_bytes()
    return tree


class LostAfterCommit:
    """A service whose activation commits and whose answer never comes back."""

    def __init__(self, service: ConnectionRegistryMutationService) -> None:
        self._service = service
        self.calls = 0

    def activate(self, *args, **kwargs):
        self.calls += 1
        self._service.activate(*args, **kwargs)
        raise OSError("the activation response was lost")

    def confirm(self, *args, **kwargs):
        return self._service.confirm(*args, **kwargs)

    def restore(self, *args, **kwargs):
        return self._service.restore(*args, **kwargs)

    def reconcile_activations(self, *args, **kwargs):
        return self._service.reconcile_activations(*args, **kwargs)


class Lane:
    """One disposable desktop state root with a real registry and real services."""

    def __init__(self, directory: str) -> None:
        self.root = Path(directory)
        self.clock = [100.0]
        self.profile_a = local_profile(
            self.root / "ssot-a", PROFILE_A, WORKSPACE_A, "Current"
        )
        self.profile_b = local_profile(
            self.root / "ssot-b", PROFILE_B, WORKSPACE_B, "Updated"
        )
        self.before = registry_of(PROFILE_A, self.profile_a, self.profile_b)
        REGISTRY.save_connection_registry(self.root, self.before)
        self.after = registry_of(PROFILE_B, self.profile_a, self.profile_b)
        self.service = ConnectionRegistryMutationService(
            self.root,
            monotonic_clock=lambda: self.clock[0],
            proof_ttl_seconds=60,
        )
        self.adapter = RegistryActivationAdapter(
            self.root, mutation_service=self.service
        )

    # -- helpers -------------------------------------------------------

    def digest(self, registry=None) -> str:
        return registry_digest(self.before if registry is None else registry)

    def live_digest(self) -> str:
        return registry_digest(REGISTRY.load_connection_registry(self.root))

    def proof_for(self, profile, base_digest: str) -> str:
        return self.service.issue_successful_test_proof(
            profile, ready_result(profile), base_registry_digest=base_digest
        ).proof_id

    def candidate(
        self,
        operation_id: str = OPERATION,
        *,
        profile=None,
        target=None,
        expected: str | None = None,
    ) -> ActivationCandidate:
        profile = self.profile_b if profile is None else profile
        target = self.after if target is None else target
        expected = self.digest() if expected is None else expected
        return ActivationCandidate(
            operation_id=operation_id,
            registry=target,
            profile_id=profile.profile_id,
            proof_id=self.proof_for(profile, expected),
            expected_registry_digest=expected,
        )

    def issue(self, operation_id: str = OPERATION, **changes):
        candidate = self.candidate(operation_id, **changes)
        self.adapter.bind(candidate)
        return self.adapter.activate(operation_id)

    def plant_duplicate(self, receipt) -> str:
        """Leave a second receipt carrying this operation's exact fingerprint."""

        activation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "duplicate-receipt"))
        twin = replace(
            receipt,
            activation_id=activation_id,
            rollback_file=f"{activation_id}.rollback.json",
        )
        records = self.root / ACTIVATION_DIRECTORY
        (records / twin.rollback_file).write_bytes(
            (records / receipt.rollback_file).read_bytes()
        )
        (records / f"{activation_id}.receipt.json").write_bytes(_receipt_bytes(twin))
        return activation_id

    def sole_receipt(self):
        receipts = activation_receipts(self.root)
        assert len(receipts) == 1, receipts
        return receipts[0]


class ActivationAdapterTestCase(unittest.TestCase):
    def lane(self, stack) -> Lane:
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        return Lane(directory)


class SuccessfulActivationTests(ActivationAdapterTestCase):
    def test_a_successful_activation_is_pending_and_needs_the_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            observed = lane.issue()

            self.assertEqual(observed.status, "verified")
            self.assertIs(observed.committed, True)
            self.assertEqual(observed.state, "pending")
            self.assertIs(observed.restart_required, True)
            self.assertEqual(observed.selection, "selected")
            self.assertEqual(observed.code, "activation_pending_restart")
            self.assertEqual(observed.profile_id, PROFILE_B)
            self.assertEqual(observed.current_registry_digest, lane.digest(lane.after))
            self.assertEqual(lane.live_digest(), lane.digest(lane.after))
            # The activated profile's data directory is never created or read.
            self.assertFalse((lane.root / "ssot-a").exists())
            self.assertFalse((lane.root / "ssot-b").exists())

    def test_the_operation_id_is_bound_to_a_different_activation_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            observed = lane.issue()
            binding = lane.adapter.binding(OPERATION)

            self.assertIsNotNone(observed.activation_id)
            self.assertNotEqual(observed.activation_id, OPERATION)
            self.assertEqual(binding.operation_id, OPERATION)
            self.assertEqual(binding.activation_id, observed.activation_id)
            self.assertEqual(binding.activation_id, lane.sole_receipt().activation_id)
            self.assertTrue(binding.issued)

    def test_pending_then_confirm_settles_the_same_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()

            confirmed = lane.adapter.confirm(OPERATION)

            self.assertEqual(confirmed.activation_id, pending.activation_id)
            self.assertEqual(confirmed.status, "verified")
            self.assertIs(confirmed.committed, True)
            self.assertEqual(confirmed.state, "confirmed")
            self.assertIs(confirmed.restart_required, False)
            self.assertEqual(confirmed.selection, "selected")
            self.assertEqual(confirmed.code, "activation_confirmed")
            self.assertEqual(lane.sole_receipt().state, "confirmed")
            self.assertEqual(
                lane.adapter.observe_confirm(OPERATION).state, "confirmed"
            )

    def test_confirming_twice_writes_nothing_more(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            lane.adapter.confirm(OPERATION)
            settled = evidence_tree(lane.root)

            again = lane.adapter.confirm(OPERATION)

            self.assertEqual(again.state, "confirmed")
            self.assertEqual(evidence_tree(lane.root), settled)


class IdempotencyTests(ActivationAdapterTestCase):
    def test_the_same_operation_never_opens_a_second_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            first = lane.issue()
            settled = evidence_tree(lane.root)

            second = lane.adapter.activate(OPERATION)

            self.assertEqual(second.activation_id, first.activation_id)
            self.assertEqual(second.state, "pending")
            self.assertEqual(evidence_tree(lane.root), settled)
            self.assertEqual(len(activation_receipts(lane.root)), 1)

    def test_rebinding_an_operation_to_another_candidate_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.adapter.bind(lane.candidate())

            with self.assertRaises(ActivationBindingError) as caught:
                lane.adapter.bind(
                    lane.candidate(profile=lane.profile_a, target=lane.before)
                )

            self.assertEqual(caught.exception.code, "binding_conflict")

    def test_an_unbound_operation_is_refused_before_any_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            before = evidence_tree(lane.root)

            for call in (
                lane.adapter.activate,
                lane.adapter.observe,
                lane.adapter.confirm,
                lane.adapter.rollback,
            ):
                with self.subTest(call=call.__name__), self.assertRaises(
                    ActivationBindingError
                ) as caught:
                    call(OPERATION)
                self.assertEqual(caught.exception.code, "binding_absent")
            self.assertEqual(evidence_tree(lane.root), before)


class LostResponseTests(ActivationAdapterTestCase):
    def test_a_lost_activation_is_recovered_by_its_exact_candidate_fingerprint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lossy = LostAfterCommit(lane.service)
            adapter = RegistryActivationAdapter(lane.root, mutation_service=lossy)
            adapter.bind(lane.candidate())

            observed = adapter.activate(OPERATION)

            self.assertEqual(lossy.calls, 1)
            self.assertEqual(observed.state, "pending")
            self.assertIs(observed.committed, True)
            self.assertEqual(observed.selection, "selected")
            self.assertEqual(len(activation_receipts(lane.root)), 1)
            self.assertEqual(
                adapter.binding(OPERATION).activation_id,
                lane.sole_receipt().activation_id,
            )

    def test_a_recovered_activation_is_reconciled_and_never_reissued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lossy = LostAfterCommit(lane.service)
            adapter = RegistryActivationAdapter(lane.root, mutation_service=lossy)
            adapter.bind(lane.candidate())
            adapter.activate(OPERATION)
            settled = evidence_tree(lane.root)

            again = adapter.activate(OPERATION)

            self.assertEqual(lossy.calls, 1)
            self.assertEqual(again.state, "pending")
            self.assertEqual(evidence_tree(lane.root), settled)

    def test_an_unknown_activation_is_never_reissued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.adapter.bind(lane.candidate())
            ActivationBindingStore(lane.root).mark_issued(OPERATION)
            # The registry moved under the operation while its own receipt is
            # absent: nothing proves the activation did or did not happen.
            REGISTRY.save_connection_registry(
                lane.root,
                registry_of(
                    PROFILE_A,
                    replace(lane.profile_a, label="Renamed by another writer"),
                    lane.profile_b,
                ),
            )
            before = evidence_tree(lane.root)

            observed = lane.adapter.observe(OPERATION)
            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.activate(OPERATION)

            self.assertEqual(observed.status, "unknown")
            self.assertEqual(observed.state, "unknown")
            self.assertEqual(observed.code, "activation_unknown")
            self.assertEqual(caught.exception.code, "activation_unknown_not_reissued")
            self.assertEqual(evidence_tree(lane.root), before)

    def test_unreadable_activation_evidence_leaves_the_issue_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)

            class LostAndUnreadable(LostAfterCommit):
                def activate(self, *args, **kwargs):
                    records = Path(directory) / ACTIVATION_DIRECTORY
                    records.mkdir(parents=True, exist_ok=True)
                    (records / "not-a-uuid.receipt.json").write_bytes(b"{}")
                    raise OSError("the activation response was lost")

            adapter = RegistryActivationAdapter(
                lane.root,
                mutation_service=LostAndUnreadable(lane.service),
                binding_store=ActivationBindingStore(lane.root),
            )
            adapter.bind(lane.candidate())
            with self.assertRaises(ActivationResponseLost) as caught:
                adapter.activate(OPERATION)

            self.assertEqual(caught.exception.operation_id, OPERATION)

    def test_a_lost_confirmation_settles_from_the_receipt_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()

            class LostConfirm(LostAfterCommit):
                def confirm(self, *args, **kwargs):
                    self._service.confirm(*args, **kwargs)
                    raise OSError("the confirmation response was lost")

            adapter = RegistryActivationAdapter(
                lane.root, mutation_service=LostConfirm(lane.service)
            )
            observed = adapter.confirm(OPERATION)

            self.assertEqual(observed.state, "confirmed")
            self.assertIs(observed.committed, True)
            self.assertEqual(lane.sole_receipt().state, "confirmed")

    def test_an_unprovable_confirmation_is_reported_lost_not_settled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()

            class LostAndUnreadable(LostAfterCommit):
                def __init__(self, service, root: Path) -> None:
                    super().__init__(service)
                    self._root = root

                def confirm(self, *args, **kwargs):
                    for path in (self._root / ACTIVATION_DIRECTORY).iterdir():
                        if path.name.endswith(".receipt.json"):
                            path.write_bytes(b"{")
                    raise OSError("the confirmation response was lost")

            adapter = RegistryActivationAdapter(
                lane.root, mutation_service=LostAndUnreadable(lane.service, lane.root)
            )
            with self.assertRaises(ActivationResponseLost) as caught:
                adapter.confirm(OPERATION)

            self.assertEqual(caught.exception.operation_id, OPERATION)
            self.assertEqual(caught.exception.code, "activation_response_lost")


class RestartTests(ActivationAdapterTestCase):
    def test_the_binding_survives_a_restart_and_confirms_the_same_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()
            self.assertTrue((lane.root / BINDING_FILE).is_file())

            # A restart: new services, new binding store, no in-process proof.
            restarted = RegistryActivationAdapter(
                lane.root,
                mutation_service=ConnectionRegistryMutationService(lane.root),
                binding_store=ActivationBindingStore(lane.root),
            )
            resumed = restarted.observe(OPERATION)
            confirmed = restarted.confirm(OPERATION)

            self.assertEqual(resumed.activation_id, pending.activation_id)
            self.assertEqual(resumed.state, "pending")
            self.assertIs(resumed.restart_required, True)
            self.assertEqual(confirmed.activation_id, pending.activation_id)
            self.assertEqual(confirmed.state, "confirmed")

    def test_a_restart_cannot_reissue_an_activation_it_cannot_prove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.adapter.bind(lane.candidate())
            restarted = RegistryActivationAdapter(
                lane.root,
                mutation_service=ConnectionRegistryMutationService(lane.root),
            )
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                restarted.activate(OPERATION)

            self.assertEqual(
                caught.exception.code, "activation_candidate_unavailable"
            )
            self.assertEqual(evidence_tree(lane.root), before)


class RefusalTests(ActivationAdapterTestCase):
    def test_a_registry_that_moved_after_the_proof_refuses_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.adapter.bind(lane.candidate())
            REGISTRY.save_connection_registry(
                lane.root,
                registry_of(
                    PROFILE_A,
                    replace(lane.profile_a, label="Changed elsewhere"),
                    lane.profile_b,
                ),
            )
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.activate(OPERATION)

            self.assertEqual(caught.exception.code, "registry_conflict")
            self.assertEqual(evidence_tree(lane.root), before)
            self.assertEqual(activation_receipts(lane.root), ())

    def test_an_expired_test_proof_refuses_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.adapter.bind(lane.candidate())
            lane.clock[0] += 3600.0
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.activate(OPERATION)

            self.assertEqual(caught.exception.code, "activation_proof_refused")
            self.assertEqual(evidence_tree(lane.root), before)
            self.assertEqual(activation_receipts(lane.root), ())

    def test_a_candidate_that_does_not_select_its_profile_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.bind(lane.candidate(target=lane.before))

            self.assertEqual(caught.exception.code, "activation_candidate_refused")
            self.assertIsNone(ActivationBindingStore(lane.root).find(OPERATION))

    def test_a_disabled_candidate_profile_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            disabled = replace(lane.profile_b, enabled=False)
            target = registry_of(PROFILE_B, lane.profile_a, disabled)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.bind(lane.candidate(target=target))

            self.assertEqual(caught.exception.code, "activation_candidate_refused")


class ForeignEvidenceTests(ActivationAdapterTestCase):
    def test_a_foreign_unconfirmed_receipt_blocks_a_new_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            live = lane.live_digest()
            second = lane.candidate(
                OTHER_OPERATION,
                profile=lane.profile_a,
                target=lane.before,
                expected=live,
            )
            lane.adapter.bind(second)
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.activate(OTHER_OPERATION)

            self.assertEqual(
                caught.exception.code, "activation_foreign_unconfirmed"
            )
            self.assertEqual(evidence_tree(lane.root), before)

    def test_two_receipts_with_this_candidate_refuse_every_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            store = ActivationBindingStore(lane.root)
            recorded = store.find(OPERATION)
            assert recorded is not None
            # A fresh binding: this is the lost-response path, where the
            # operation has to find its receipt by fingerprint alone.
            store.release(OPERATION)
            store.bind(replace(recorded, activation_id=None))
            lane.plant_duplicate(lane.sole_receipt())
            before = evidence_tree(lane.root)

            for call in (
                lane.adapter.observe,
                lane.adapter.confirm,
                lane.adapter.rollback,
            ):
                with self.subTest(call=call.__name__), self.assertRaises(
                    ActivationAdapterRefusal
                ) as caught:
                    call(OPERATION)
                self.assertEqual(caught.exception.code, "receipt_ambiguous")
            self.assertEqual(evidence_tree(lane.root), before)

    def test_a_binding_pointed_at_another_receipt_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            foreign = lane.plant_duplicate(
                replace(lane.sole_receipt(), profile_id=PROFILE_A)
            )
            store = ActivationBindingStore(lane.root)
            recorded = store.find(OPERATION)
            assert recorded is not None
            store.release(OPERATION)
            store.bind(replace(recorded, activation_id=foreign))
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.observe(OPERATION)

            self.assertEqual(caught.exception.code, "receipt_identity_mismatch")
            self.assertEqual(evidence_tree(lane.root), before)

    def test_a_resolved_identity_cannot_be_repointed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            store = ActivationBindingStore(lane.root)

            with self.assertRaises(ActivationBindingError) as caught:
                store.resolve(OPERATION, str(uuid.uuid4()))

            self.assertEqual(caught.exception.code, "binding_conflict")


class SelectionProofTests(ActivationAdapterTestCase):
    def test_a_receipt_alone_does_not_prove_the_selected_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()
            # Another writer puts the previous selection back without touching
            # the receipt.  The receipt still exists; the selection is gone.
            REGISTRY.save_connection_registry(lane.root, lane.before)

            observed = lane.adapter.observe(OPERATION)

            self.assertEqual(observed.activation_id, pending.activation_id)
            self.assertEqual(observed.state, "pending")
            self.assertEqual(observed.selection, "not_selected")
            self.assertEqual(observed.status, "unknown")
            self.assertIsNone(observed.committed)
            self.assertEqual(observed.code, "activation_pending_not_selected")

    def test_an_unselected_activation_cannot_be_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            REGISTRY.save_connection_registry(lane.root, lane.before)
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.confirm(OPERATION)

            self.assertEqual(caught.exception.code, "confirm_not_selected")
            self.assertEqual(evidence_tree(lane.root), before)


class PassiveObservationTests(ActivationAdapterTestCase):
    def test_every_observation_leaves_the_registry_and_receipts_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            before = evidence_tree(lane.root)

            observed = lane.adapter.observe(OPERATION)
            confirmed = lane.adapter.observe_confirm(OPERATION)
            rolled = lane.adapter.observe_rollback(OPERATION)

            self.assertEqual(evidence_tree(lane.root), before)
            self.assertEqual(observed.state, "pending")
            self.assertEqual(confirmed.state, "pending")
            self.assertEqual(rolled.status, "failed")
            self.assertIs(rolled.committed, False)
            self.assertEqual(rolled.code, "rollback_not_committed")

    def test_observation_recovers_the_binding_without_a_registry_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            store = ActivationBindingStore(lane.root)
            recorded = store.find(OPERATION)
            assert recorded is not None
            store.release(OPERATION)
            store.bind(replace(recorded, activation_id=None))
            before = evidence_tree(lane.root)

            observed = RegistryActivationAdapter(lane.root).observe(OPERATION)

            self.assertEqual(evidence_tree(lane.root), before)
            self.assertEqual(
                observed.activation_id, lane.sole_receipt().activation_id
            )
            self.assertEqual(
                store.require(OPERATION).activation_id, observed.activation_id
            )

    def test_an_operation_that_never_issued_reports_no_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.adapter.bind(lane.candidate())

            observed = lane.adapter.observe(OPERATION)

            self.assertEqual(observed.status, "unknown")
            self.assertIs(observed.committed, False)
            self.assertEqual(observed.state, "unknown")
            self.assertEqual(observed.code, "activation_not_issued")
            self.assertIsNone(observed.activation_id)


class RollbackTests(ActivationAdapterTestCase):
    def test_a_pending_activation_rolls_back_to_the_previous_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()

            rolled = lane.adapter.rollback(OPERATION)

            self.assertEqual(rolled.status, "verified")
            self.assertIs(rolled.committed, True)
            self.assertIs(rolled.previous_activation_selected, True)
            self.assertEqual(rolled.code, "activation_rolled_back")
            self.assertEqual(rolled.activation_id, pending.activation_id)
            self.assertEqual(lane.live_digest(), lane.digest())
            self.assertEqual(lane.sole_receipt().state, "restored")
            self.assertEqual(lane.adapter.observe(OPERATION).state, "rolled_back")

    def test_rolling_back_twice_writes_nothing_more(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            lane.adapter.rollback(OPERATION)
            settled = evidence_tree(lane.root)

            again = lane.adapter.rollback(OPERATION)

            self.assertEqual(again.status, "verified")
            self.assertIs(again.previous_activation_selected, True)
            self.assertEqual(evidence_tree(lane.root), settled)

    def test_a_confirmed_activation_cannot_be_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            lane.adapter.confirm(OPERATION)
            settled = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                lane.adapter.rollback(OPERATION)

            self.assertEqual(caught.exception.code, "rollback_not_unconfirmed")
            self.assertEqual(evidence_tree(lane.root), settled)
            self.assertEqual(
                lane.adapter.observe_rollback(OPERATION).code,
                "rollback_unavailable_confirmed",
            )

    def test_a_rollback_is_unknown_while_the_registry_is_not_the_activated_state(
        self,
    ) -> None:
        """The previous selection is already back while the receipt is not.

        This is exactly the shape the real ``restore`` leaves behind when it is
        interrupted between its registry write and its receipt transition, and
        nothing on disk distinguishes it from another writer having put the
        previous selection back.  Either way the rollback is unknown: it must
        not be reported as a refusal that committed nothing, and it must not be
        reissued.
        """

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()
            REGISTRY.save_connection_registry(lane.root, lane.before)
            before = evidence_tree(lane.root)

            rolled = lane.adapter.rollback(OPERATION)

            self.assertEqual(rolled.status, "unknown")
            self.assertIsNone(rolled.committed)
            self.assertEqual(rolled.code, "rollback_interrupted")
            self.assertEqual(rolled.activation_id, pending.activation_id)
            self.assertIs(rolled.previous_activation_selected, True)
            self.assertEqual(evidence_tree(lane.root), before)


if __name__ == "__main__":
    unittest.main()
