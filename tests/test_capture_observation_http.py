"""The saved source-observation routes, through a real server and a real child.

The harness is the released verification route's own: one loopback server bound
to ``127.0.0.1:0`` over a store this test created in a temporary directory, one
synthetic local Python verifier this test wrote, and a real Capture built by
the released policy, issue and manual import routes. Nothing here opens a home
directory, a live SSOT, a provider, a credential or any network beyond that
socket, and no dependency is installed.

Reusing :class:`~tests.test_knowledge_verification_http.VerificationHttpCase`
is deliberate: these two routes must be judged against the *same* store, the
same operator configuration and the same single verification gate the released
route runs under, and a second copy of that fixture could drift from it.

The classes are the boundary's obligations in order: who may reach the two
routes and what a refusal costs, one whole recorded observation over a real
store, what a read may and may not reconcile, and what a failed save may claim.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode

from workstack.capture_observation_http import (
    OBSERVATION_PATH,
    OBSERVATION_READ_UNAVAILABLE,
    OBSERVATION_SAVE_UNKNOWN,
    RECORD_CHECK_PATH,
)
from workstack.file_lease import StoreLockedError
from workstack.http_route_types import IDEMPOTENT_POST_ROUTES
from workstack.knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME
from workstack.store_errors import (
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
)
from workstack.store_layout import MIGRATION_BACKUP_DIR
from workstack.store_report_migration import verify_archive_file

from tests.test_knowledge_verification_http import (
    ABSENT_CAPTURE,
    DOCUMENT_ONE,
    NOW,
    OTHER_UPSTREAM_UID,
    VERSION_ONE,
    VerificationHttpCase,
)

RECORD = RECORD_CHECK_PATH
OBSERVATION = OBSERVATION_PATH
VERIFY = "/api/v1/knowledge/captures/verify"
CAPTURES = "/api/v1/captures"

LATER = "2026-09-09T10:30:00Z"


class ObservationHttpCase(VerificationHttpCase):
    """The released verification fixture, plus the two new request shapes."""

    # -- the wire --------------------------------------------------------

    def record(
        self,
        capture: dict[str, Any],
        headers: dict[str, str] | None = None,
        **overrides: Any,
    ):
        return self.post(RECORD, self.verify_body(capture, **overrides), headers)

    def observation_query(
        self, capture: dict[str, Any], **overrides: Any
    ) -> dict[str, str]:
        values = {
            "workspace_uid": self.workspace_uid,
            "capture_id": capture["id"],
            "capture_revision": str(capture["revision"]),
        }
        values.update(overrides)
        return values

    def read_observation(self, capture: dict[str, Any], **overrides: Any):
        return self.call(
            "GET", OBSERVATION + "?" + urlencode(self.observation_query(capture, **overrides))
        )

    # -- assertions ------------------------------------------------------

    def assertClosedProjection(self, data: Any) -> None:
        """The wire is exactly the frozen projection, and carries no record."""

        self.assertEqual(set(data), {"binding", "observation"})
        self.assertEqual(
            set(data["binding"]),
            {"workspace_uid", "capture_id", "capture_revision"},
        )
        observation = data["observation"]
        if observation is not None:
            self.assertEqual(
                set(observation),
                {"accepted_at", "checked_at", "binding_state", "result"},
            )
            self.assertIn(observation["binding_state"], ("unchanged", "changed"))
        rendered = json.dumps(data)
        # No raw stored record, request envelope, digest, command or alias.
        # The released protocol result is reused whole, so its own `schema` and
        # `verification_id` are released content and are not probed here; the
        # stored record's schema string, which is not, is.
        for absent in (
            "capture_digest",
            "workstack.capture-observation",
            "command",
            "environment",
            "connection",
        ):
            self.assertNotIn(absent, rendered, absent)
        self.assertNoCanary(data)

    def assertRecorded(self, payload: Any) -> dict[str, Any]:
        self.assertEqual(payload["meta"], {"outcome": "observation_recorded"})
        data = payload["data"]
        self.assertClosedProjection(data)
        observation = data["observation"]
        self.assertIsNotNone(observation)
        self.assertEqual(observation["binding_state"], "unchanged")
        self.assertIsNotNone(observation["result"])
        self.assertEqual(observation["checked_at"], observation["result"]["checked_at"])
        return observation

    def assertReady(self, payload: Any) -> Any:
        self.assertEqual(payload["meta"], {"outcome": "observation_ready"})
        self.assertClosedProjection(payload["data"])
        return payload["data"]["observation"]

    # -- store facts -----------------------------------------------------

    def captures_document(self) -> dict[str, Any]:
        return self.document("captures.json")

    def backup_archives(self) -> list[Any]:
        directory = self.root / MIGRATION_BACKUP_DIR
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.zip"))

    def document_bodies(self) -> dict[str, bytes]:
        """Every authoritative document, as the exact bytes on disk."""

        return {
            path.name: path.read_bytes()
            for path in sorted(self.root.glob("*.json"))
        }

    def capture_wire(self) -> Any:
        status, payload = self.call("GET", CAPTURES)
        self.assertEqual(status, 200, payload)
        return payload


class ObservationAdmissionTest(ObservationHttpCase):
    """Who reaches the two routes, and what a refusal costs."""

    def test_the_record_route_is_not_registered_as_idempotent(self) -> None:
        """A cached replay would turn an explicit new check into an old answer."""

        self.assertNotIn("knowledge_capture_record_check", IDEMPOTENT_POST_ROUTES)

    def test_a_record_without_csrf_reaches_no_child_and_saves_nothing(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        headers = self.owner_headers()
        for name in ("X-WorkStack-CSRF", "Origin"):
            with self.subTest(missing=name):
                without = {k: v for k, v in headers.items() if k != name}
                status, payload = self.record(capture, without)
                self.assertEqual(status, 403, payload)
                self.assertEqual(self.child_calls(), 0)
                self.assertNothingSaved(before)

    def test_a_capture_bearer_alone_reaches_no_child(self) -> None:
        """Capture ingestion authority is not owner record-check authority."""

        capture = self.ready()
        before = self.snapshot()
        for token in ("not-a-real-token", self.server.capture_token):
            with self.subTest(real=token != "not-a-real-token"):
                status, payload = self.record(
                    capture,
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {}".format(token),
                    },
                )
                self.assertIn(status, (401, 403), payload)
                self.assertEqual(self.child_calls(), 0)
                self.assertNothingSaved(before)

    def test_a_non_canonical_record_target_is_the_unknown_route(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        body = json.dumps(self.verify_body(capture)).encode("utf-8")
        for target in (
            RECORD + "?unexpected=1",
            RECORD + "?",
            RECORD + "#fragment",
            RECORD + ";x=1",
            RECORD + "/",
            "/api/v1/knowledge/captures/record-check%2f",
        ):
            with self.subTest(target=target):
                status, payload = self.call("POST", target, body, self.owner_headers())
                self.assertEqual(status, 404, payload)
                self.assertEqual(payload["error"]["code"], "not_found")
                self.assertEqual(self.child_calls(), 0)
                self.assertNothingSaved(before)

    def test_an_idempotency_key_is_refused_before_any_child(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        headers = self.owner_headers()
        headers["Idempotency-Key"] = "11111111-1111-4111-8111-111111111111"
        status, payload = self.record(capture, headers)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "unsupported_idempotency_key")
        self.assertEqual(self.child_calls(), 0)
        self.assertNothingSaved(before)

    def test_a_malformed_record_body_reaches_no_child(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        bodies: tuple[Any, ...] = (
            {},
            {"workspace_uid": self.workspace_uid},
            self.verify_body(capture, extra="x"),
            self.verify_body(capture, capture_revision="1"),
            self.verify_body(capture, capture_revision=True),
            self.verify_body(capture, capture_revision=-1),
            self.verify_body(capture, workspace_uid="not-a-uuid"),
            self.verify_body(capture, capture_id="not a capture id"),
        )
        for body in bodies:
            with self.subTest(body=sorted(body)):
                status, payload = self.post(RECORD, body)
                self.assertEqual(status, 400, payload)
                self.assertIn(
                    payload["error"]["code"], ("invalid_body", "invalid_request")
                )
                self.assertNoCanary(payload)
                self.assertEqual(self.child_calls(), 0)
                self.assertNothingSaved(before)

    def test_a_non_canonical_observation_target_is_the_unknown_route(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        query = urlencode(self.observation_query(capture))
        for target in (
            OBSERVATION + ";x=1?" + query,
            OBSERVATION + "/?" + query,
            OBSERVATION + "?" + query + "#fragment",
            "/api/v1/knowledge/captures/observation%2e?" + query,
            "/api/v1/knowledge/captures//observation?" + query,
        ):
            with self.subTest(target=target):
                status, payload = self.call("GET", target)
                self.assertEqual(status, 404, payload)
                self.assertEqual(payload["error"]["code"], "not_found")
                self.assertEqual(self.child_calls(), 0)
                self.assertNothingSaved(before)

    def test_a_malformed_observation_query_is_refused_before_the_store(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        base = self.observation_query(capture)
        targets = [
            OBSERVATION,
            OBSERVATION + "?",
            OBSERVATION + "?" + urlencode({k: v for k, v in base.items() if k != "capture_id"}),
            OBSERVATION + "?" + urlencode(dict(base, unexpected="1")),
            OBSERVATION + "?" + urlencode(base) + "&capture_id=" + base["capture_id"],
            OBSERVATION + "?" + urlencode(dict(base, workspace_uid="")),
            OBSERVATION + "?" + urlencode(dict(base, capture_revision="")),
        ]
        for revision in ("007", "+1", " 1", "1.0", "0x1", "1e2", "-1", "٢", "9" * 40):
            targets.append(
                OBSERVATION + "?" + urlencode(dict(base, capture_revision=revision))
            )
        for target in targets:
            with self.subTest(target=target):
                status, payload = self.call("GET", target)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["code"], "invalid_query")
                self.assertNoCanary(payload)
                self.assertEqual(self.child_calls(), 0)
                self.assertNothingSaved(before)

    def test_an_unknown_capture_read_is_not_the_unknown_route(self) -> None:
        """The legacy-server diagnosis is `not_found`, and this is not it."""

        capture = self.ready()
        status, payload = self.read_observation(capture, capture_id=ABSENT_CAPTURE)
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["code"], "unknown_capture")
        self.assertNoCanary(payload)
        self.assertEqual(self.child_calls(), 0)

    def test_a_read_of_a_capture_with_no_history_is_a_null_observation(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        status, payload = self.read_observation(capture)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(self.assertReady(payload))
        self.assertEqual(payload["data"]["binding"]["capture_id"], capture["id"])
        # No child, no save, and no storage format was activated by a read.
        self.assertEqual(self.child_calls(), 0)
        self.assertNothingSaved(before)
        self.assertEqual(self.captures_document()["version"], 1)
        self.assertEqual(self.backup_archives(), [])

    def test_query_key_order_and_ordinary_encoding_are_accepted(self) -> None:
        capture = self.ready()
        base = self.observation_query(capture)
        reordered = "&".join(
            "{}={}".format(name, base[name])
            for name in ("capture_revision", "capture_id", "workspace_uid")
        )
        encoded = urlencode(base, safe="")
        for query in (reordered, encoded):
            with self.subTest(query=query):
                status, payload = self.call("GET", OBSERVATION + "?" + query)
                self.assertEqual(status, 200, payload)
                self.assertIsNone(self.assertReady(payload))


class ObservationRecordTest(ObservationHttpCase):
    """One whole recorded observation, over a real store."""

    def test_a_recorded_check_backs_up_the_original_and_survives_a_reopen(self) -> None:
        capture = self.ready()
        original_bodies = self.document_bodies()
        original_wire = self.capture_wire()
        knowledge_before = self.document(KNOWLEDGE_DOCUMENT_NAME)
        self.assertEqual(self.captures_document()["version"], 1)
        self.assertEqual(self.backup_archives(), [])

        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        observation = self.assertRecorded(payload)
        self.assertEqual(observation["accepted_at"], NOW)
        self.assertEqual(self.child_calls(), 1)

        # The captures document is now the observation container, and the
        # Capture rows in it are the rows that were already there.
        stored = self.captures_document()
        self.assertEqual(stored["version"], 2)
        self.assertEqual(len(stored["observations"]), 1)
        self.assertEqual(
            stored["captures"], json.loads(original_bodies["captures.json"])["captures"]
        )

        # Exactly one rollback archive, holding the exact original documents.
        archives = self.backup_archives()
        self.assertEqual(len(archives), 1, archives)
        verified = verify_archive_file(archives[0])
        self.assertEqual(verified.bodies, original_bodies)
        self.assertEqual(len(verified.bodies), 11, sorted(verified.bodies))

        # No idempotent response cache, and no unrelated document moved.
        # `activity.json` is one of the eleven roster documents, so it exists
        # either way: what this route must never do is *change* it, because
        # that is where the idempotent-POST mechanism stores a whole response
        # body. Its bytes are compared, not its existence.
        after = self.document_bodies()
        self.assertEqual(after["activity.json"], original_bodies["activity.json"])
        self.assertEqual(
            {name: body for name, body in after.items() if name != "captures.json"},
            {
                name: body
                for name, body in original_bodies.items()
                if name != "captures.json"
            },
        )
        self.assertEqual(self.document(KNOWLEDGE_DOCUMENT_NAME), knowledge_before)
        # The existing Capture wire is byte-identical: this slice adds no field.
        self.assertEqual(self.capture_wire(), original_wire)

        # A second owner incarnation reads the same saved observation back, and
        # reading it starts no child.
        self.restart()
        status, payload = self.read_observation(capture)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.assertReady(payload), observation)
        self.assertEqual(self.child_calls(), 1)

    def test_a_repeated_read_never_invokes_a_child(self) -> None:
        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        recorded = payload["data"]["observation"]
        for _ in range(3):
            status, payload = self.read_observation(capture)
            self.assertEqual(status, 200, payload)
            self.assertEqual(self.assertReady(payload), recorded)
        self.assertEqual(self.child_calls(), 1)

    def test_each_explicit_record_runs_exactly_one_child(self) -> None:
        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.child_calls(), 1)
        archives = self.backup_archives()
        self.assertEqual(len(archives), 1)

        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        self.assertRecorded(payload)
        # One more child, and no second format-migration backup: the container
        # is already version 2 and an ordinary update does not re-migrate.
        self.assertEqual(self.child_calls(), 2)
        self.assertEqual(self.backup_archives(), archives)
        self.assertEqual(self.captures_document()["version"], 2)

    def test_historical_times_are_not_clamped_to_the_reading_clock(self) -> None:
        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        recorded = payload["data"]["observation"]
        self.assertEqual(recorded["accepted_at"], NOW)

        self.now = LATER
        status, payload = self.read_observation(capture)
        self.assertEqual(status, 200, payload)
        observation = self.assertReady(payload)
        self.assertEqual(observation["accepted_at"], NOW)
        self.assertEqual(observation["checked_at"], recorded["checked_at"])
        self.assertEqual(observation["binding_state"], "unchanged")
        self.assertEqual(self.child_calls(), 1)

    def test_the_released_verify_route_is_unchanged_and_still_saves_nothing(
        self,
    ) -> None:
        capture = self.ready()
        before = self.snapshot()
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"], {"outcome": "verification_ready"})
        self.assertEqual(set(payload["data"]), {"binding", "result"})
        self.assertNothingSaved(before)
        self.assertEqual(self.captures_document()["version"], 1)
        self.assertEqual(self.backup_archives(), [])


class ObservationReconciliationTest(ObservationHttpCase):
    """What a read may reconcile, and what it must refuse to do."""

    def test_changed_authority_shows_times_only_and_runs_no_child(self) -> None:
        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        recorded = payload["data"]["observation"]

        # The operator's roster moves under the saved observation. Nothing
        # about the Capture the browser holds changed, so this is a reconciled
        # read rather than a refusal.
        status, payload = self.post(
            "/api/v1/knowledge/connections", self.policy_body(upstream=OTHER_UPSTREAM_UID)
        )
        self.assertEqual(status, 200, payload)

        status, payload = self.read_observation(capture)
        self.assertEqual(status, 200, payload)
        observation = self.assertReady(payload)
        self.assertEqual(observation["binding_state"], "changed")
        self.assertIsNone(observation["result"])
        self.assertEqual(observation["accepted_at"], recorded["accepted_at"])
        self.assertEqual(observation["checked_at"], recorded["checked_at"])
        # Changed carries no old evidence, and reconciling started no child.
        rendered = json.dumps(payload)
        self.assertNotIn(DOCUMENT_ONE, rendered)
        self.assertNotIn(VERSION_ONE, rendered)
        self.assertEqual(self.child_calls(), 1)

    def test_a_read_of_a_different_binding_is_refused(self) -> None:
        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        status, payload = self.read_observation(
            capture, capture_revision=str(capture["revision"] + 1)
        )
        self.assertIn(status, (404, 409), payload)
        self.assertNoCanary(payload)
        self.assertEqual(self.child_calls(), 1)

    def test_a_record_for_a_drifted_revision_saves_nothing(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        status, payload = self.record(
            capture, capture_revision=capture["revision"] + 1
        )
        self.assertIn(status, (404, 409), payload)
        self.assertNoCanary(payload)
        self.assertEqual(self.child_calls(), 0)
        self.assertNothingSaved(before)

    def test_a_second_concurrent_record_is_refused_by_the_one_gate(self) -> None:
        capture = self.ready()
        self.arm_barrier()
        outcome: dict[str, Any] = {}

        def run() -> None:
            outcome["first"] = self.record(capture)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.await_child()
            status, payload = self.record(capture)
            self.assertEqual(status, 409, payload)
            self.assertEqual(payload["error"]["code"], "verification_busy")
            self.assertNoCanary(payload)
        finally:
            self.release_child()
            thread.join(timeout=60)
        status, payload = outcome["first"]
        self.assertEqual(status, 200, payload)
        self.assertRecorded(payload)
        self.assertEqual(self.child_calls(), 1)

    def test_an_unusable_child_is_an_upstream_refusal_that_saves_nothing(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        for mode, expected in (
            ("garbage", 502),
            ("silent", 502),
            ("exit", 502),
            ("forged_ref", 502),
        ):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                status, payload = self.record(capture)
                self.assertEqual(status, expected, payload)
                self.assertNoCanary(payload)
                self.assertNothingSaved(before)
                self.assertEqual(self.captures_document()["version"], 1)


class UnconfiguredVerifierRecordTest(ObservationHttpCase):
    """An operator who never opted this alias into verification."""

    verifier_configured = False

    def test_a_record_without_a_configured_verifier_starts_no_child(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        status, payload = self.record(capture)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], "knowledge_verifier_unavailable")
        self.assertNoCanary(payload)
        self.assertEqual(self.child_calls(), 0)
        self.assertNothingSaved(before)

    def test_a_read_needs_no_verifier_configuration_at_all(self) -> None:
        capture = self.ready()
        status, payload = self.read_observation(capture)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(self.assertReady(payload))
        self.assertEqual(self.child_calls(), 0)


class ObservationSaveUnknownTest(ObservationHttpCase):
    """A save whose outcome the owner cannot confirm."""

    def fail_the_sink(self, error: BaseException) -> None:
        """Make the trusted storage sink refuse, for this server only."""

        store = self.server.stack.store
        original = store.record_capture_observation

        def refuse(record: Any) -> Any:
            raise error

        store.record_capture_observation = refuse  # type: ignore[method-assign]
        self.addCleanup(
            setattr, store, "record_capture_observation", original
        )

    def test_a_storage_refusal_becomes_an_unconfirmed_save(self) -> None:
        capture = self.ready()
        self.fail_the_sink(OSError("disk canary 7f2b90 detail"))
        status, payload = self.record(capture)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], OBSERVATION_SAVE_UNKNOWN)
        message = payload["error"]["message"]
        self.assertIn("could not be confirmed", message)
        # It must not claim the opposite of what it knows.
        self.assertNotIn("disk canary 7f2b90 detail", json.dumps(payload))
        self.assertNoCanary(payload)
        # One child ran, and no second one was started to recover.
        self.assertEqual(self.child_calls(), 1)

        # The client's only recourse is an explicit read; it is served, and it
        # starts nothing.
        status, payload = self.read_observation(capture)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.child_calls(), 1)

    def test_an_unconfirmable_projection_is_never_announced_as_recorded(self) -> None:
        """A 200 `observation_recorded` is a confirmed save, or it is not sent."""

        import unittest.mock as mock

        capture = self.ready()
        for projection in (
            {"binding": {}, "observation": None},
            {"binding": {}, "observation": {"binding_state": "changed"}},
        ):
            with self.subTest(projection=projection):
                with mock.patch(
                    "workstack.capture_observation_http.record_capture_source_check",
                    return_value=projection,
                ):
                    status, payload = self.record(capture)
                self.assertEqual(status, 503, payload)
                self.assertEqual(payload["error"]["code"], OBSERVATION_SAVE_UNKNOWN)

    def test_an_unreadable_history_is_refused_rather_than_shown_as_empty(self) -> None:
        import unittest.mock as mock

        capture = self.ready()
        with mock.patch(
            "workstack.capture_observation_http.get_capture_source_observation",
            side_effect=OSError("read canary 7f2b90 detail"),
        ):
            status, payload = self.read_observation(capture)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], OBSERVATION_READ_UNAVAILABLE)
        self.assertNotIn("read canary 7f2b90 detail", json.dumps(payload))
        self.assertNoCanary(payload)


class ObservationReadStorageFailureTest(ObservationHttpCase):
    """Storage failures raised inside the read, at each phase it has.

    None of these is a mocked *route*: the real runtime call runs, and the
    store underneath it refuses the way the released store really refuses --
    at the outer transaction's entry, while a later admission loads a document,
    and at that transaction's exit. All three must arrive as the one closed
    read refusal, and none of them may be reported as a changed binding or as
    an empty history.
    """

    def patch_store(self, name: str, replacement: Any) -> None:
        store = self.server.stack.store
        original = getattr(store, name)
        setattr(store, name, replacement)
        self.addCleanup(setattr, store, name, original)

    def assertClosedReadFailure(self, status: int, payload: Any) -> None:
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], OBSERVATION_READ_UNAVAILABLE)
        rendered = json.dumps(payload)
        self.assertNotIn("storage canary 7f2b90", rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("captures.json", rendered)
        self.assertNoCanary(payload)

    def test_a_transaction_that_cannot_be_entered_is_a_closed_read_refusal(
        self,
    ) -> None:
        capture = self.ready()

        def refuse_entry() -> Any:
            raise StoreLockedError("storage canary 7f2b90 lease detail")

        self.patch_store("transaction", refuse_entry)
        status, payload = self.read_observation(capture)
        self.assertClosedReadFailure(status, payload)
        self.assertEqual(self.child_calls(), 0)

    def test_a_transaction_that_fails_on_exit_is_a_closed_read_refusal(self) -> None:
        capture = self.ready()
        store = self.server.stack.store
        original = store.transaction

        @contextmanager
        def failing_exit() -> Any:
            with original():
                yield
            raise StoreCorruptError("storage canary 7f2b90 commit detail")

        self.patch_store("transaction", failing_exit)
        status, payload = self.read_observation(capture)
        self.assertClosedReadFailure(status, payload)
        self.assertEqual(self.child_calls(), 0)

    def refuse_captures_load(
        self, error: BaseException, *, occurrence: int
    ) -> tuple[list[str], Any]:
        """Fail the Nth `captures.json` load of the read, and count them.

        Occurrence 1 is the current-Capture requirement at the top of the read;
        occurrence 2 is the current-authority re-admission that decides the
        binding state. They are different phases of the same transaction, and
        both have to arrive as the same closed refusal.

        The restore callable is returned as well as registered, so a subtest
        can undo one patch without running the case's other cleanups.
        """

        store = self.server.stack.store
        original = store.load
        seen: list[str] = []

        def load(name: str) -> Any:
            if name == "captures.json":
                seen.append(name)
                if len(seen) == occurrence:
                    raise error
            return original(name)

        def restore() -> None:
            store.load = original

        store.load = load  # type: ignore[method-assign]
        self.addCleanup(restore)
        return seen, restore

    def test_the_first_capture_load_failing_is_a_closed_read_refusal(self) -> None:
        """Corrupt persisted state is refused, not downgraded to `changed`."""

        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        seen, _ = self.refuse_captures_load(
            StoreCorruptError("storage canary 7f2b90 document detail"), occurrence=1
        )
        status, payload = self.read_observation(capture)
        self.assertClosedReadFailure(status, payload)
        self.assertNotIn("changed", json.dumps(payload))
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.child_calls(), 1)

    def test_the_re_admission_capture_load_failing_is_a_closed_read_refusal(
        self,
    ) -> None:
        """The second load decides binding state; a fault there is not `changed`."""

        capture = self.ready()
        status, payload = self.record(capture)
        self.assertEqual(status, 200, payload)
        seen, _ = self.refuse_captures_load(
            StoreCorruptError("storage canary 7f2b90 re-admission detail"),
            occurrence=2,
        )
        status, payload = self.read_observation(capture)
        self.assertClosedReadFailure(status, payload)
        # The read really did get past the first load and fail in the phase
        # that judges the binding, and it still refused rather than reporting
        # a changed binding or an empty history.
        self.assertEqual(len(seen), 2)
        self.assertNotIn("changed", json.dumps(payload))
        self.assertEqual(self.child_calls(), 1)

    def test_a_synchronisation_refusal_is_closed_on_this_route_only(self) -> None:
        """Folded here by contract; every released route keeps its own envelope."""

        capture = self.ready()
        for error in (
            StoreExternalChangeError(
                {
                    "status": "external-change-invalid",
                    "generation": 3,
                    "changed_files": ["captures.json"],
                }
            ),
            StoreAdoptionConflictError("storage canary 7f2b90 adoption detail"),
        ):
            with self.subTest(error=type(error).__name__):
                _, restore = self.refuse_captures_load(error, occurrence=1)
                status, payload = self.read_observation(capture)
                self.assertClosedReadFailure(status, payload)
                restore()

        # The same refusal on a released route is still the released answer:
        # nothing about shared dispatch moved.
        self.refuse_captures_load(
            StoreExternalChangeError(
                {
                    "status": "external-change-invalid",
                    "generation": 3,
                    "changed_files": ["captures.json"],
                }
            ),
            occurrence=1,
        )
        status, payload = self.verify(capture)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(self.child_calls(), 0)
