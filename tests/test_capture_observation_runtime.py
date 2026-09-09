"""Owner record-check and historical projection, over isolated fixtures.

These tests reuse the released verification HTTP case's store, imported
Capture and synthetic child. They do not prove B's Store mixin: the Store
under test is a recording overlay with a reentrant transaction, which is the
caller contract this slice owns. No live provider, credential or SSOT.
"""

from __future__ import annotations

import copy
import json
import threading
import unittest
from contextlib import contextmanager
from typing import Any

from workstack.capture_observation_runtime import (
    OBSERVATION_READ_UNAVAILABLE,
    OBSERVATION_SAVE_UNKNOWN,
    get_capture_source_observation,
    record_capture_source_check,
)
from workstack.capture_observations import OBSERVATION_SCHEMA, plan_observation_upsert
from workstack.knowledge_verification_guard import KnowledgeVerificationGuard
from workstack.knowledge_verification_runtime import (
    CAPTURE_REVISION_CHANGED,
    UNKNOWN_CAPTURE,
    VERIFICATION_AUTHORITY_CHANGED,
    VERIFICATION_BINDING_MISMATCH,
    VERIFICATION_RESULT_REFUSED,
    VerificationRuntimeError,
    verify_capture_source,
)
from workstack import knowledge_verification_runtime as kv_runtime

from tests.test_knowledge_verification_http import (
    ABSENT_CAPTURE,
    OTHER_UPSTREAM_UID,
    VerificationHttpCase,
)

LATER = "2026-12-01T00:00:00Z"
DRIFTED_DIGEST = "sha256:" + ("cd" * 32)


class ObservationRuntimeCase(VerificationHttpCase):
    """One real Capture plus a recording Store overlay for B's missing sink."""

    def setUp(self) -> None:
        super().setUp()
        self.guard = KnowledgeVerificationGuard()
        self.events: list[tuple[str, int]] = []
        self.saved: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.container_version = 1
        self.observations_mode = "list"
        self.revision_override: int | None = None
        self._install_recording_store()
        self._watch_child()

    def _install_recording_store(self) -> None:
        store = self.store
        original_transaction = store.transaction
        original_load = store.load

        @contextmanager
        def transaction() -> Any:
            self.events.append(("tx-enter", int(getattr(store._local, "depth", 0))))
            with original_transaction():
                yield
            self.events.append(("tx-exit", int(getattr(store._local, "depth", 0))))

        def load(name: str) -> Any:
            value = copy.deepcopy(original_load(name))
            if name != "captures.json":
                return value
            if self.revision_override is not None:
                for entry in value.get("captures") or []:
                    if isinstance(entry, dict) and entry.get("id") == self.capture_id:
                        entry["revision"] = self.revision_override
            if self.container_version == 2:
                value["version"] = 2
                if self.observations_mode == "missing":
                    value.pop("observations", None)
                elif self.observations_mode == "null":
                    value["observations"] = None
                else:
                    value["observations"] = copy.deepcopy(self.observations)
            return value

        def record_capture_observation(record: dict[str, Any]) -> dict[str, Any]:
            depth = int(getattr(store._local, "depth", 0))
            self.events.append(("save", depth))
            retained = copy.deepcopy(record)
            self.saved.append(retained)
            self.observations = plan_observation_upsert(
                self.observations, retained, workspace_uid=self.workspace_uid
            )
            self.container_version = 2
            return copy.deepcopy(record)

        store.transaction = transaction
        store.load = load
        store.record_capture_observation = record_capture_observation

    def _watch_child(self) -> None:
        original = kv_runtime.run_knowledge_driver

        def exchange(*arguments: Any, **keywords: Any) -> Any:
            depth = int(getattr(self.store._local, "depth", 0))
            self.events.append(("child", depth))
            return original(*arguments, **keywords)

        kv_runtime.run_knowledge_driver = exchange
        self.addCleanup(setattr, kv_runtime, "run_knowledge_driver", original)

    @property
    def capture_id(self) -> str | None:
        captures = json.loads((self.root / "captures.json").read_text(encoding="utf-8"))
        rows = captures.get("captures") or []
        if not rows:
            return None
        return rows[0]["id"]

    def record(self, capture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        keywords = dict(
            workspace_uid=self.workspace_uid,
            capture_id=capture["id"],
            capture_revision=capture["revision"],
            drivers=self.drivers(),
            guard=self.guard,
            clock=lambda: self.now,
        )
        keywords.update(overrides)
        return record_capture_source_check(self.store, **keywords)

    def read(self, capture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        keywords = dict(
            workspace_uid=self.workspace_uid,
            capture_id=capture["id"],
            capture_revision=capture["revision"],
            clock=lambda: self.now,
        )
        keywords.update(overrides)
        return get_capture_source_observation(self.store, **keywords)

    def refuse_record(self, capture: dict[str, Any], **overrides: Any) -> Any:
        with self.assertRaises(VerificationRuntimeError) as caught:
            self.record(capture, **overrides)
        return caught.exception

    def refuse_read(self, capture: dict[str, Any], **overrides: Any) -> Any:
        with self.assertRaises(VerificationRuntimeError) as caught:
            self.read(capture, **overrides)
        return caught.exception

    def assertProjectionClosed(self, payload: dict[str, Any]) -> None:
        self.assertEqual(set(payload), {"binding", "observation"})
        self.assertEqual(
            set(payload["binding"]),
            {"workspace_uid", "capture_id", "capture_revision"},
        )
        observation = payload["observation"]
        if observation is not None:
            self.assertEqual(
                set(observation),
                {"accepted_at", "checked_at", "binding_state", "result"},
            )
        rendered = json.dumps(payload)
        self.assertNotIn("capture_digest", rendered)
        self.assertNotIn("verifier_alias", rendered)
        self.assertNotIn(OBSERVATION_SCHEMA, rendered)
        self.assertNotIn("workstack.knowledge-verify.v1", rendered)
        self.assertNoCanary(payload)


class ReleasedVerifyShapeTest(ObservationRuntimeCase):
    """The public verify entry stays read-only and does not select the sink."""

    def test_released_verify_returns_binding_and_result_and_writes_nothing(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        payload = verify_capture_source(
            self.store,
            workspace_uid=self.workspace_uid,
            capture_id=capture["id"],
            capture_revision=capture["revision"],
            drivers=self.drivers(),
            guard=self.guard,
            clock=lambda: self.now,
        )
        self.assertEqual(set(payload), {"binding", "result"})
        self.assertNotIn("observation", payload)
        self.assertEqual(
            payload["binding"],
            {
                "workspace_uid": self.workspace_uid,
                "capture_id": capture["id"],
                "capture_revision": capture["revision"],
            },
        )
        self.assertEqual(payload["result"]["schema"], "workstack.knowledge-verification.v1")
        self.assertEqual(self.saved, [])
        self.assertEqual(self.container_version, 1)
        self.assertNothingSaved(before)
        self.assertEqual(self.child_calls(), 1)
        self.assertFalse(self.guard.active)


class RecordCheckSequenceTest(ObservationRuntimeCase):
    """Child outside the lock; re-admit, compare and save inside one outer lock."""

    def test_one_child_outside_the_lock_then_save_inside_the_same_outer_lock(
        self,
    ) -> None:
        capture = self.ready()
        payload = self.record(capture)
        self.assertProjectionClosed(payload)
        observation = payload["observation"]
        self.assertIsNotNone(observation)
        self.assertEqual(observation["binding_state"], "unchanged")
        self.assertIsNotNone(observation["result"])
        self.assertEqual(observation["accepted_at"], self.now)
        self.assertEqual(observation["checked_at"], observation["result"]["checked_at"])
        child_depths = [depth for name, depth in self.events if name == "child"]
        save_depths = [depth for name, depth in self.events if name == "save"]
        self.assertEqual(child_depths, [0])
        self.assertEqual(len(save_depths), 1)
        self.assertGreaterEqual(save_depths[0], 1)
        self.assertEqual(self.child_calls(), 1)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["schema"], OBSERVATION_SCHEMA)
        self.assertEqual(
            set(self.saved[0]),
            {
                "schema",
                "capture_digest",
                "verifier_alias",
                "accepted_at",
                "request",
                "result",
            },
        )

    def test_original_accepted_at_is_retained_and_the_window_is_not_renewed(
        self,
    ) -> None:
        capture = self.ready()
        stamps = [
            "2026-09-09T09:00:00Z",
            "2026-09-09T09:00:10Z",
            "2026-09-09T09:00:20Z",
        ]
        cursor = [0]

        def clock() -> str:
            value = stamps[min(cursor[0], len(stamps) - 1)]
            cursor[0] += 1
            return value

        payload = self.record(capture, clock=clock)
        record = self.saved[0]
        self.assertEqual(record["accepted_at"], "2026-09-09T09:00:10Z")
        self.assertEqual(payload["observation"]["accepted_at"], "2026-09-09T09:00:10Z")
        self.assertEqual(record["request"]["requested_at"], "2026-09-09T09:00:00Z")
        self.assertEqual(record["request"]["expires_at"], "2026-09-09T09:01:00Z")
        self.assertNotEqual(record["request"]["expires_at"], "2026-09-09T09:01:20Z")

    def test_capture_authority_drift_refuses_without_save(self) -> None:
        capture = self.ready()
        self.arm_barrier()
        outcome: list[Any] = []

        def run() -> None:
            try:
                outcome.append(self.record(capture))
            except VerificationRuntimeError as error:
                outcome.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 30)
        self.addCleanup(self.release_child)
        self.await_child()
        status, payload = self.post(
            "/api/v1/knowledge/connections", self.policy_body()
        )
        self.assertEqual(status, 200, payload)
        self.release_child()
        worker.join(30)
        error = outcome[0]
        self.assertIsInstance(error, VerificationRuntimeError)
        self.assertEqual(error.code, VERIFICATION_AUTHORITY_CHANGED)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.container_version, 1)
        self.assertEqual(self.child_calls(), 1)
        self.assertFalse(self.guard.active)

    def test_malformed_stdout_refuses_without_save(self) -> None:
        capture = self.ready()
        self.set_mode("garbage")
        error = self.refuse_record(capture)
        self.assertEqual(error.code, VERIFICATION_RESULT_REFUSED)
        self.assertEqual(error.details, {})
        self.assertEqual(self.saved, [])
        self.assertEqual(self.child_calls(), 1)
        self.assertFalse(self.guard.active)

    def test_save_exception_is_unknown_and_does_not_rerun_or_latch(self) -> None:
        capture = self.ready()

        def explode(_record: dict[str, Any]) -> dict[str, Any]:
            raise OSError("canary-save-path Z:\\\\PilotShare\\\\secret")

        self.store.record_capture_observation = explode
        with self.assertRaises(VerificationRuntimeError) as caught:
            self.record(capture)
        error = caught.exception
        self.assertEqual(error.code, OBSERVATION_SAVE_UNKNOWN)
        self.assertEqual(error.details, {})
        rendered = "{} {}".format(error, error.details)
        self.assertNotIn("canary-save-path", rendered)
        self.assertNotIn("PilotShare", rendered)
        self.assertEqual(self.child_calls(), 1)
        self.assertFalse(self.guard.active)
        self.assertFalse(self.guard.unsettled)
        self.assertEqual(self.saved, [])


class HistoricalReadTest(ObservationRuntimeCase):
    """Saved history never calls a source, renews a window or fabricates null."""

    def test_legacy_history_is_null_without_mutation_child_or_clock(self) -> None:
        capture = self.ready()
        before = self.snapshot()

        def clock() -> str:
            raise AssertionError("a null history read must not re-admit")

        payload = self.read(capture, clock=clock)
        self.assertProjectionClosed(payload)
        self.assertIsNone(payload["observation"])
        self.assertEqual(self.child_calls(), 0)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.container_version, 1)
        self.assertNothingSaved(before)

    def test_historical_expiry_does_not_invalidate_an_originally_accepted_result(
        self,
    ) -> None:
        capture = self.ready()
        recorded = self.record(capture)
        payload = self.read(capture, clock=lambda: LATER)
        self.assertProjectionClosed(payload)
        observation = payload["observation"]
        self.assertEqual(observation["binding_state"], "unchanged")
        self.assertEqual(observation["result"], recorded["observation"]["result"])
        self.assertEqual(
            observation["accepted_at"], recorded["observation"]["accepted_at"]
        )
        self.assertEqual(self.child_calls(), 1)

    def test_unchanged_history_returns_the_original_result(self) -> None:
        capture = self.ready()
        recorded = self.record(capture)
        payload = self.read(capture)
        self.assertEqual(payload["observation"]["binding_state"], "unchanged")
        self.assertEqual(
            payload["observation"]["result"], recorded["observation"]["result"]
        )
        self.assertEqual(payload["binding"], recorded["binding"])
        self.assertEqual(self.child_calls(), 1)

    def test_digest_revision_and_policy_changes_hide_the_old_result(self) -> None:
        capture = self.ready()
        recorded = self.record(capture)
        accepted = recorded["observation"]["accepted_at"]
        checked = recorded["observation"]["checked_at"]

        self.observations[0]["capture_digest"] = DRIFTED_DIGEST
        digest_payload = self.read(capture)
        self.assertEqual(digest_payload["observation"]["binding_state"], "changed")
        self.assertIsNone(digest_payload["observation"]["result"])
        self.assertEqual(digest_payload["observation"]["accepted_at"], accepted)
        self.assertEqual(digest_payload["observation"]["checked_at"], checked)
        self.assertEqual(digest_payload["binding"]["capture_revision"], capture["revision"])

        self.observations[0]["capture_digest"] = self.saved[0]["capture_digest"]
        self.revision_override = capture["revision"] + 1
        revision_payload = self.read(capture, capture_revision=self.revision_override)
        self.assertEqual(revision_payload["binding"]["capture_revision"], self.revision_override)
        self.assertEqual(revision_payload["observation"]["binding_state"], "changed")
        self.assertIsNone(revision_payload["observation"]["result"])
        self.revision_override = None

        status, body = self.post("/api/v1/knowledge/connections", self.policy_body())
        self.assertEqual(status, 200, body)
        policy_payload = self.read(capture)
        self.assertEqual(policy_payload["observation"]["binding_state"], "changed")
        self.assertIsNone(policy_payload["observation"]["result"])
        self.assertEqual(policy_payload["observation"]["accepted_at"], accepted)
        self.assertEqual(self.child_calls(), 1)

    def test_corrupt_saved_record_refuses_rather_than_becoming_null(self) -> None:
        capture = self.ready()
        self.container_version = 2
        self.observations = [{"schema": "not-an-observation"}]
        error = self.refuse_read(capture)
        self.assertEqual(error.code, OBSERVATION_READ_UNAVAILABLE)
        self.assertEqual(error.details, {})
        self.assertEqual(self.child_calls(), 0)
        self.assertEqual(self.saved, [])

    def test_container2_missing_or_null_observations_refuse(self) -> None:
        capture = self.ready()
        for mode in ("missing", "null"):
            with self.subTest(mode=mode):
                self.container_version = 2
                self.observations_mode = mode
                error = self.refuse_read(capture)
                self.assertEqual(error.code, OBSERVATION_READ_UNAVAILABLE)
                self.assertEqual(error.details, {})
        self.assertEqual(self.child_calls(), 0)
        self.assertEqual(self.saved, [])

    def test_unknown_capture_and_binding_mismatch_remain_refusals(self) -> None:
        capture = self.ready()
        self.record(capture)
        unknown = self.refuse_read(capture, capture_id=ABSENT_CAPTURE)
        self.assertEqual(unknown.code, UNKNOWN_CAPTURE)
        mismatch = self.refuse_read(capture, workspace_uid=OTHER_UPSTREAM_UID)
        self.assertEqual(mismatch.code, VERIFICATION_BINDING_MISMATCH)
        revision = self.refuse_read(capture, capture_revision=capture["revision"] + 1)
        self.assertEqual(revision.code, CAPTURE_REVISION_CHANGED)
        self.assertEqual(self.child_calls(), 1)

    def test_identity_is_admitted_before_any_store_read(self) -> None:
        class Boom:
            def transaction(self) -> Any:
                raise AssertionError("parser must refuse before a Store read")

        with self.assertRaises(VerificationRuntimeError) as caught:
            get_capture_source_observation(
                Boom(),
                workspace_uid="1f2e3d4c-5b6a-4798-8899-aabbccddeeff",
                capture_id="nope",
                capture_revision=0,
                clock=lambda: self.now,
            )
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(caught.exception.details, {"field": "capture_id"})


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
