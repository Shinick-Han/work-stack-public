"""Adapter behaviour at the real services' crash windows and evidence seams.

This is the second half of ``test_remote_update_activation_adapter``; it reuses
that module's disposable ``Lane`` (a throwaway desktop state root, a real
``connection-registry.json``, a real short-lived Test proof and the real
mutation/recovery services) and lives in its own file only to stay inside the
800-line file budget.

Nothing here mocks a service.  Three real seams are exercised:

* the exact I/O steps the real services perform are failed one at a time, in
  place, so the adapter sees precisely the half-written state a crash or a full
  disk leaves behind — ``activate`` writes its receipt, then the registry, then
  transitions the receipt; ``restore`` writes the registry, then transitions the
  receipt;
* the adapter's own bounded binding file fails its post-effect write or read,
  which is the case the exception's class alone gets wrong;
* receipt evidence appears on disk after the operation's identity is already
  resolved, which is what a confirm or a restore would otherwise settle on top
  of.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import connection_registry as REGISTRY  # noqa: E402
import connection_registry_mutations as MUTATIONS  # noqa: E402
from remote_update_activation_adapter import (  # noqa: E402
    ActivationAdapterRefusal,
    ActivationResponseLost,
    RegistryActivationAdapter,
)
from remote_update_activation_binding import (  # noqa: E402
    ActivationBindingError,
    ActivationBindingStore,
)
from tests.test_remote_update_activation_adapter import (  # noqa: E402
    OPERATION,
    OTHER_OPERATION,
    PROFILE_A,
    Lane,
    evidence_tree,
)


@contextmanager
def failing_at(name: str, predicate):
    """Fail exactly one real mutation-service write, where it is really called.

    The service keeps its own ordering, its own lock and its own compare-and-swap
    guards; only the single I/O step named here refuses to land, which is what a
    process death or an out-of-space disk produces at that instant.
    """

    original = getattr(MUTATIONS, name)

    def failing(*args, **kwargs):
        if predicate(*args, **kwargs):
            raise OSError(f"{name} never landed")
        return original(*args, **kwargs)

    setattr(MUTATIONS, name, failing)
    try:
        yield
    finally:
        setattr(MUTATIONS, name, original)


def receipt_transition_to(state: str):
    return lambda _path, receipt, _digest: receipt.state == state


class CountingMutations:
    """The real mutation service, with the calls the adapter makes counted."""

    def __init__(self, service) -> None:
        self._service = service
        self.activates = 0
        self.confirms = 0
        self.restores = 0

    def activate(self, *args, **kwargs):
        self.activates += 1
        return self._service.activate(*args, **kwargs)

    def confirm(self, *args, **kwargs):
        self.confirms += 1
        return self._service.confirm(*args, **kwargs)

    def restore(self, *args, **kwargs):
        self.restores += 1
        return self._service.restore(*args, **kwargs)

    def reconcile_activations(self, *args, **kwargs):
        return self._service.reconcile_activations(*args, **kwargs)


class UnwritableResolve(ActivationBindingStore):
    """A binding store whose post-effect write is the thing that fails."""

    def resolve(self, operation_id: str, activation_id: str):
        raise ActivationBindingError("binding_unwritable")


class BreakableBindingStore(ActivationBindingStore):
    """A binding store whose file can stop being readable mid-operation."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root)
        self.readable = True

    def load(self):
        if not self.readable:
            raise ActivationBindingError("binding_store_invalid")
        return super().load()


class CrashBoundaryTestCase(unittest.TestCase):
    def counted(self, lane: Lane) -> tuple[RegistryActivationAdapter, CountingMutations]:
        counting = CountingMutations(lane.service)
        adapter = RegistryActivationAdapter(lane.root, mutation_service=counting)
        return adapter, counting


class ActivationCrashBoundaryTests(CrashBoundaryTestCase):
    def test_an_activation_that_dies_before_its_registry_write_committed_nothing(
        self,
    ) -> None:
        """The receipt is published first, so a prepared receipt can be uncommitted.

        The real ``activate`` publishes the rollback file and the ``prepared``
        receipt before it replaces the registry.  A crash in between leaves this
        operation's own receipt on disk while nothing was selected: the adapter
        must bind that receipt and report the commit as unproven, not as a
        commit and not as a zero-write refusal.
        """

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter, counting = self.counted(lane)
            adapter.bind(lane.candidate())

            with failing_at("_replace_registry_if_digest", lambda *a, **k: True):
                observed = adapter.activate(OPERATION)

            self.assertEqual(counting.activates, 1)
            self.assertEqual(observed.state, "prepared")
            self.assertIsNone(observed.committed)
            self.assertEqual(observed.selection, "not_selected")
            self.assertEqual(observed.code, "activation_prepared_unselected")
            self.assertEqual(
                observed.activation_id, lane.sole_receipt().activation_id
            )
            self.assertEqual(lane.live_digest(), lane.digest())

    def test_an_activation_that_dies_before_its_pending_transition_is_committed(
        self,
    ) -> None:
        """The registry moved; only the receipt's own transition is missing."""

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter, counting = self.counted(lane)
            adapter.bind(lane.candidate())

            with failing_at(
                "_replace_receipt_if_digest", receipt_transition_to("pending")
            ):
                observed = adapter.activate(OPERATION)

            self.assertEqual(counting.activates, 1)
            self.assertEqual(observed.state, "prepared")
            self.assertIs(observed.committed, True)
            self.assertEqual(observed.selection, "selected")
            self.assertEqual(observed.code, "activation_prepared_uncommitted")
            self.assertEqual(lane.live_digest(), lane.digest(lane.after))

    def test_a_crashed_activation_is_reconciled_and_never_reissued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter, counting = self.counted(lane)
            adapter.bind(lane.candidate())
            with failing_at(
                "_replace_receipt_if_digest", receipt_transition_to("pending")
            ):
                first = adapter.activate(OPERATION)
            settled = evidence_tree(lane.root)

            again = adapter.activate(OPERATION)

            self.assertEqual(counting.activates, 1)
            self.assertEqual(again.activation_id, first.activation_id)
            self.assertEqual(evidence_tree(lane.root), settled)


class RollbackCrashBoundaryTests(CrashBoundaryTestCase):
    def test_a_restore_that_dies_before_its_receipt_transition_is_unknown(
        self,
    ) -> None:
        """The real ``restore`` writes the registry before it moves the receipt.

        Failing only that final transition leaves the previous registry selected
        while the receipt is still ``pending``.  That is a committed registry
        write with an unproven outcome, so it must surface as a lost response for
        this same operation — never as a refusal promising ``committed=False``.
        """

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter, counting = self.counted(lane)
            adapter.bind(lane.candidate())
            pending = adapter.activate(OPERATION)

            with failing_at(
                "_replace_receipt_if_digest", receipt_transition_to("restored")
            ), self.assertRaises(ActivationResponseLost) as caught:
                adapter.rollback(OPERATION)

            self.assertEqual(caught.exception.operation_id, OPERATION)
            self.assertEqual(caught.exception.code, "activation_response_lost")
            self.assertEqual(counting.restores, 1)
            self.assertEqual(lane.live_digest(), lane.digest())
            self.assertEqual(lane.sole_receipt().state, "pending")
            self.assertEqual(
                lane.sole_receipt().activation_id, pending.activation_id
            )

    def test_the_interrupted_restore_is_observed_on_the_same_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter, counting = self.counted(lane)
            adapter.bind(lane.candidate())
            pending = adapter.activate(OPERATION)
            with failing_at(
                "_replace_receipt_if_digest", receipt_transition_to("restored")
            ), self.assertRaises(ActivationResponseLost):
                adapter.rollback(OPERATION)
            interrupted = evidence_tree(lane.root)

            rolled = adapter.observe_rollback(OPERATION)
            activation = adapter.observe(OPERATION)

            self.assertEqual(rolled.status, "unknown")
            self.assertIsNone(rolled.committed)
            self.assertEqual(rolled.code, "rollback_interrupted")
            self.assertEqual(rolled.activation_id, pending.activation_id)
            self.assertIs(rolled.previous_activation_selected, True)
            self.assertEqual(activation.activation_id, pending.activation_id)
            self.assertEqual(activation.state, "pending")
            self.assertIsNone(activation.committed)
            self.assertEqual(activation.selection, "not_selected")
            self.assertEqual(evidence_tree(lane.root), interrupted)

    def test_an_interrupted_restore_is_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter, counting = self.counted(lane)
            adapter.bind(lane.candidate())
            adapter.activate(OPERATION)
            with failing_at(
                "_replace_receipt_if_digest", receipt_transition_to("restored")
            ), self.assertRaises(ActivationResponseLost):
                adapter.rollback(OPERATION)
            interrupted = evidence_tree(lane.root)

            again = adapter.rollback(OPERATION)

            self.assertEqual(counting.restores, 1)
            self.assertEqual(again.code, "rollback_interrupted")
            self.assertIsNone(again.committed)
            self.assertEqual(evidence_tree(lane.root), interrupted)


class PostEffectBindingFailureTests(CrashBoundaryTestCase):
    def test_a_binding_write_that_fails_after_the_commit_is_lost_not_refused(
        self,
    ) -> None:
        """The registry really activated; only this adapter's note did not land.

        An ``ActivationBindingError`` means "committed nothing" only before the
        service is called.  Here the service has already replaced the registry,
        so the same operation is reported lost and stays bound for reconciliation.
        """

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            store = UnwritableResolve(lane.root)
            adapter = RegistryActivationAdapter(
                lane.root, mutation_service=lane.service, binding_store=store
            )
            adapter.bind(lane.candidate())

            with self.assertRaises(ActivationResponseLost) as caught:
                adapter.activate(OPERATION)

            self.assertEqual(caught.exception.operation_id, OPERATION)
            self.assertEqual(lane.live_digest(), lane.digest(lane.after))
            self.assertEqual(lane.sole_receipt().state, "pending")
            recorded = ActivationBindingStore(lane.root).require(OPERATION)
            self.assertIs(recorded.issued, True)
            self.assertIsNone(recorded.activation_id)

    def test_the_lost_binding_write_reconciles_the_same_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            adapter = RegistryActivationAdapter(
                lane.root,
                mutation_service=lane.service,
                binding_store=UnwritableResolve(lane.root),
            )
            adapter.bind(lane.candidate())
            with self.assertRaises(ActivationResponseLost):
                adapter.activate(OPERATION)
            committed = evidence_tree(lane.root)

            recovered = RegistryActivationAdapter(lane.root).observe(OPERATION)

            self.assertEqual(
                recovered.activation_id, lane.sole_receipt().activation_id
            )
            self.assertEqual(recovered.state, "pending")
            self.assertIs(recovered.committed, True)
            self.assertEqual(evidence_tree(lane.root), committed)

    def test_a_binding_read_that_fails_after_a_confirm_is_lost_not_refused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            store = BreakableBindingStore(lane.root)

            class ConfirmThenLoseTheBinding(CountingMutations):
                def confirm(self, *args, **kwargs):
                    self.confirms += 1
                    receipt = self._service.confirm(*args, **kwargs)
                    store.readable = False
                    return receipt

            service = ConfirmThenLoseTheBinding(lane.service)
            adapter = RegistryActivationAdapter(
                lane.root, mutation_service=service, binding_store=store
            )

            with self.assertRaises(ActivationResponseLost) as caught:
                adapter.confirm(OPERATION)

            self.assertEqual(caught.exception.operation_id, OPERATION)
            self.assertEqual(service.confirms, 1)
            self.assertEqual(lane.sole_receipt().state, "confirmed")

    def test_a_confirm_that_provably_wrote_nothing_stays_a_refusal(self) -> None:
        """The narrow refusal survives: the registry still selects the activation."""

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()

            class RefusingConfirm(CountingMutations):
                def confirm(self, *args, **kwargs):
                    self.confirms += 1
                    raise OSError("the confirmation never reached the receipt")

            adapter = RegistryActivationAdapter(
                lane.root, mutation_service=RefusingConfirm(lane.service)
            )
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationAdapterRefusal) as caught:
                adapter.confirm(OPERATION)

            self.assertEqual(caught.exception.code, "activation_refused")
            self.assertEqual(lane.sole_receipt().state, "pending")
            self.assertEqual(evidence_tree(lane.root), before)


class OperationOwnershipTests(CrashBoundaryTestCase):
    def test_one_fingerprint_cannot_be_claimed_by_a_second_operation(self) -> None:
        """Recovery names a receipt by its fingerprint, so that name is exclusive."""

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            candidate = lane.candidate()
            lane.adapter.bind(candidate)
            before = evidence_tree(lane.root)

            with self.assertRaises(ActivationBindingError) as caught:
                lane.adapter.bind(replace(candidate, operation_id=OTHER_OPERATION))

            self.assertEqual(caught.exception.code, "binding_owned")
            self.assertIsNone(ActivationBindingStore(lane.root).find(OTHER_OPERATION))
            self.assertEqual(evidence_tree(lane.root), before)

    def test_an_operation_that_never_issued_adopts_no_receipt(self) -> None:
        """An unissued operation must not inherit the receipt another one wrote."""

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            issued = lane.issue()
            store = ActivationBindingStore(lane.root)
            recorded = store.require(OPERATION)
            # The store refuses this pair; reach the state an older file could
            # still hold, so the observation path is proven on its own.
            store.release(OPERATION)
            store.bind(
                replace(recorded, operation_id=OTHER_OPERATION, activation_id=None,
                        issued=False)
            )
            before = evidence_tree(lane.root)

            observed = lane.adapter.observe(OTHER_OPERATION)

            self.assertIsNone(observed.activation_id)
            self.assertEqual(observed.code, "activation_not_issued")
            self.assertIs(observed.committed, False)
            self.assertIsNone(store.require(OTHER_OPERATION).activation_id)
            self.assertIs(store.require(OTHER_OPERATION).issued, False)
            self.assertEqual(lane.sole_receipt().activation_id, issued.activation_id)
            self.assertEqual(evidence_tree(lane.root), before)

    def test_an_unissued_operation_cannot_confirm_or_roll_back_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            store = ActivationBindingStore(lane.root)
            recorded = store.require(OPERATION)
            store.release(OPERATION)
            store.bind(
                replace(recorded, operation_id=OTHER_OPERATION, activation_id=None,
                        issued=False)
            )
            before = evidence_tree(lane.root)

            for call in (lane.adapter.confirm, lane.adapter.rollback):
                with self.subTest(call=call.__name__), self.assertRaises(
                    ActivationAdapterRefusal
                ) as caught:
                    call(OTHER_OPERATION)
                self.assertEqual(caught.exception.code, "activation_unresolved")
            self.assertEqual(evidence_tree(lane.root), before)


class ResolvedEvidenceGuardTests(CrashBoundaryTestCase):
    def test_a_duplicate_receipt_after_resolution_blocks_every_mutation(self) -> None:
        """A second receipt with this fingerprint is a duplicate, not a synonym.

        The identity was already resolved, so the earlier fingerprint-time
        ambiguity check no longer sees it.  Confirm and rollback re-prove the
        evidence they are about to settle, and refuse without writing.
        """

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()
            twin = lane.plant_duplicate(lane.sole_receipt())
            before = evidence_tree(lane.root)

            for call in (lane.adapter.confirm, lane.adapter.rollback):
                with self.subTest(call=call.__name__), self.assertRaises(
                    ActivationAdapterRefusal
                ) as caught:
                    call(OPERATION)
                self.assertEqual(caught.exception.code, "receipt_ambiguous")

            observed = lane.adapter.observe(OPERATION)
            self.assertEqual(observed.activation_id, pending.activation_id)
            self.assertNotEqual(observed.activation_id, twin)
            self.assertEqual(observed.state, "pending")
            self.assertEqual(evidence_tree(lane.root), before)

    def test_a_foreign_unconfirmed_receipt_after_resolution_blocks_every_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            lane.issue()
            lane.plant_duplicate(replace(lane.sole_receipt(), profile_id=PROFILE_A))
            before = evidence_tree(lane.root)

            for call in (lane.adapter.confirm, lane.adapter.rollback):
                with self.subTest(call=call.__name__), self.assertRaises(
                    ActivationAdapterRefusal
                ) as caught:
                    call(OPERATION)
                self.assertEqual(
                    caught.exception.code, "activation_foreign_unconfirmed"
                )
            self.assertEqual(evidence_tree(lane.root), before)

    def test_a_settled_neighbour_never_blocks_this_operation(self) -> None:
        """Only *unconfirmed* neighbours are evidence a mutation could land on."""

        with tempfile.TemporaryDirectory() as directory:
            lane = Lane(directory)
            pending = lane.issue()
            lane.plant_duplicate(
                replace(lane.sole_receipt(), state="confirmed", profile_id=PROFILE_A)
            )

            confirmed = lane.adapter.confirm(OPERATION)

            self.assertEqual(confirmed.activation_id, pending.activation_id)
            self.assertEqual(confirmed.state, "confirmed")
            self.assertIs(confirmed.committed, True)


if __name__ == "__main__":
    unittest.main()
