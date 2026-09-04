"""Checkpoint commit facts and typed publication, at the service and Store seam.

Every assertion here runs against a real contained ``Store`` and ``WorkStack``:
the recorded fact really is persisted, the typed record really is appended to
the Store's own bounded event sequence, and the encoder really renders it.

Against the pre-implementation baseline these are RED: a valid attributed
checkpoint committed with no recorded fact and no typed record at all, and the
encoder rendered a typed record as a legacy sync frame.

Never exercised here: an installed runtime, a live SSOT, a browser, an
installer, a process launch or any durable outbox. Delivery stays bounded and
process-local, exactly as the existing stream already promises.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workstack import sse_events
from workstack.checkpoint_change import derive_checkpoint_id
from workstack.service import DomainError, WorkStack
from workstack.store import Store

# The frozen wire values are written literally rather than imported, so this
# module imports cleanly against a baseline that does not define them yet and
# every RED is a behavioural failure instead of collection noise.
CHANGE_NOTICE_TYPE = "workstack.change.v1"
ATTRIBUTED = "agent-cli-v1"
NOTICE_FIELDS = {
    "event_id", "kind", "workspace_uid", "task_id", "date", "checkpoint_id",
    "done_count", "next_count", "blocker_count", "first_for_task", "origin",
    "replayed",
}


class _CheckpointCase(unittest.TestCase):
    """A real contained Store, torn down before the temporary root is removed."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Checkpoint boundary")

    def tearDown(self) -> None:
        # Explicit ordering: cleanup registered with addCleanup would run after
        # tearDown and could still find the writer lease held on Windows.
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------
    def entry_body(self, *, date: str = "2026-09-03", done=("one",), next_=(), blockers=()):
        return {
            "date": date,
            "task_id": self.task["id"],
            "done": list(done),
            "next": list(next_),
            "blockers": list(blockers),
        }

    def commit(self, key: str, *, origin: str | None = ATTRIBUTED, **kwargs):
        return self.stack.add_worklog_v1(
            self.entry_body(**kwargs),
            key,
            path="/api/v1/review/entries",
            origin=origin,
        )

    def facts(self) -> list[dict]:
        """The recorded facts, read back from the persisted Activity feed."""

        activity = self.stack.store.load("activity.json")
        return [
            record["details"]
            for record in activity.get("activity", [])
            if record.get("type") == "worklog.recorded"
        ]

    def typed_records(self) -> list[dict]:
        batch = self.stack.store.sync_events(0)
        return [event for event in batch["events"] if event.get("type") == CHANGE_NOTICE_TYPE]

    def workspace_uid(self) -> str:
        readiness = self.stack.store.readiness
        assert readiness is not None
        return readiness.workspace_uid


class RecordedFacts(_CheckpointCase):
    """D1: one recorded fact per NEW idempotent released-v3 checkpoint."""

    def test_an_attributed_checkpoint_records_exactly_one_fact(self) -> None:
        self.commit("checkpoint.facts.0001")
        facts = self.facts()
        self.assertEqual(len(facts), 1)
        recorded = facts[0]
        self.assertEqual(recorded["type"], "worklog.recorded")
        self.assertEqual(recorded["task_id"], self.task["id"])
        self.assertEqual(recorded["origin"], ATTRIBUTED)
        self.assertEqual(recorded["date"], "2026-09-03")
        self.assertEqual(recorded["ordinal"], 0)
        self.assertEqual(
            recorded["checkpoint_id"],
            derive_checkpoint_id(
                workspace_uid=self.workspace_uid(),
                idempotency_key="checkpoint.facts.0001",
            ),
            "the CP identity must come from the admitted builder, not a new rule",
        )

    def test_an_ordinary_write_records_a_fact_but_publishes_no_notice(self) -> None:
        self.commit("checkpoint.browser.0001", origin=None)
        facts = self.facts()
        self.assertEqual(len(facts), 1)
        self.assertIsNone(facts[0]["origin"])
        self.assertEqual(self.typed_records(), [], "an ordinary write is not an Agent notice")

    def test_the_fact_rides_the_same_save_as_the_idempotency_receipt(self) -> None:
        """One save_many: the fact and the receipt are never half-persisted."""

        self.commit("checkpoint.same.save.0001")
        activity = self.stack.store.load("activity.json")
        keys = [record["key"] for record in activity.get("idempotency", [])]
        self.assertIn("checkpoint.same.save.0001", keys)
        recorded = [r for r in activity["activity"] if r.get("type") == "worklog.recorded"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(
            set(recorded[0]), {"id", "type", "created_at", "task_id", "details"},
            "the recorded fact must keep the existing Activity record shape",
        )

    def test_a_replay_records_no_second_fact_and_publishes_nothing_new(self) -> None:
        first = self.commit("checkpoint.replay.0001")
        self.assertEqual(first["status"], 201)
        before = len(self.typed_records())
        replay = self.commit("checkpoint.replay.0001")
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(len(self.facts()), 1, "a replay is not a new checkpoint")
        self.assertEqual(len(self.typed_records()), before, "a replay publishes nothing")

    def test_the_public_response_body_is_unchanged_by_the_recorded_fact(self) -> None:
        result = self.commit("checkpoint.response.0001")
        self.assertEqual(set(result["body"]), {"data", "meta"})
        self.assertEqual(
            set(result["body"]["data"]),
            {"date", "task_id", "task", "done", "next", "blockers"},
        )
        self.assertNotIn("checkpoint_id", json.dumps(result["body"]))

    def test_physical_first_flattens_every_earlier_entry_across_dates(self) -> None:
        self.commit("checkpoint.first.0001", date="2026-09-01")
        self.commit("checkpoint.first.0002", date="2026-09-03")
        notices = [record["notice"] for record in self.typed_records()]
        self.assertEqual(len(notices), 2)
        self.assertTrue(notices[0]["first_for_task"], "the earliest entry is the first")
        self.assertFalse(
            notices[1]["first_for_task"],
            "a later date must see the earlier physical entry, not restart per day",
        )

    def test_the_date_local_ordinal_is_captured_before_the_append(self) -> None:
        self.commit("checkpoint.ordinal.0001", date="2026-09-03")
        self.commit("checkpoint.ordinal.0002", date="2026-09-03")
        ordinals = [fact["ordinal"] for fact in self.facts()]
        self.assertEqual(ordinals, [0, 1])

    def test_an_empty_stored_title_is_accepted_by_the_corrected_freeze(self) -> None:
        """The corrected contract keeps an empty stored title in the digest.

        The uncorrected upper-level contract says the title is non-empty. The
        released Task API refuses an empty Task title, so the empty case is
        reachable only at the builder the service delegates to; asserting it
        here is what pins the corrected rule rather than the uncorrected one.
        """

        from workstack.checkpoint_change import build_checkpoint_facts

        entry = {
            "task_id": "T-0001",
            "task": "",
            "done": ["one"],
            "next": [],
            "blockers": [],
        }
        facts = build_checkpoint_facts(
            workspace_uid=self.workspace_uid(),
            idempotency_key="checkpoint.empty.0001",
            date="2026-09-03",
            entry=entry,
            ordinal=0,
            prior_entries=[],
            origin=ATTRIBUTED,
        )
        self.assertTrue(facts["recorded"]["entry_digest"].startswith("sha256:"))
        self.assertEqual(facts["done_count"], 1)


class TypedPublication(_CheckpointCase):
    """D2/D3: one typed record, in the existing sequence, with frozen fields."""

    def test_one_typed_record_carries_exactly_the_twelve_frozen_fields(self) -> None:
        self.commit("checkpoint.typed.0001")
        records = self.typed_records()
        self.assertEqual(len(records), 1)
        notice = records[0]["notice"]
        self.assertEqual(set(notice), NOTICE_FIELDS)
        self.assertEqual(notice["kind"], "agent.checkpoint.committed")
        self.assertEqual(notice["origin"], ATTRIBUTED)
        self.assertIs(notice["replayed"], False)
        self.assertEqual(notice["done_count"], 1)
        self.assertEqual(notice["next_count"], 0)
        self.assertEqual(notice["blocker_count"], 0)
        self.assertIs(notice["first_for_task"], True)

    def test_no_title_prose_raw_key_digest_or_ordinal_enters_the_frame(self) -> None:
        self.commit("checkpoint.leak.0001", done=("a secret item",))
        serialized = json.dumps(self.typed_records()[0]["notice"])
        for forbidden in (
            "checkpoint.leak.0001", "a secret item", "Checkpoint boundary",
            "ordinal", "entry_digest", "generation", "reason", "sha256:",
        ):
            self.assertNotIn(forbidden, serialized, forbidden)

    def test_the_record_id_equals_its_payload_event_id(self) -> None:
        self.commit("checkpoint.ident.0001")
        record = self.typed_records()[0]
        self.assertEqual(record["id"], record["notice"]["event_id"])

    def test_typed_and_legacy_records_share_one_ascending_sequence(self) -> None:
        self.commit("checkpoint.mixed.0001")
        self.stack.add_task("Ordinary change")
        self.commit("checkpoint.mixed.0002")
        batch = self.stack.store.sync_events(0)
        identifiers = [event["id"] for event in batch["events"]]
        self.assertEqual(identifiers, sorted(set(identifiers)))
        self.assertLessEqual(identifiers[-1], batch["latest_event_id"])
        kinds = {event.get("type") for event in batch["events"]}
        self.assertIn(CHANGE_NOTICE_TYPE, kinds)
        self.assertTrue(kinds - {CHANGE_NOTICE_TYPE}, "legacy records must survive")

    def test_the_legacy_manifest_record_is_not_retrofitted(self) -> None:
        self.commit("checkpoint.legacy.0001")
        batch = self.stack.store.sync_events(0)
        legacy = [e for e in batch["events"] if e.get("type") != CHANGE_NOTICE_TYPE]
        self.assertTrue(legacy)
        for event in legacy:
            self.assertNotIn("notice", event)
            self.assertEqual(
                set(event), {"id", "type", "workspace_id", "generation", "changed_files"}
            )

    def test_a_rejected_notice_consumes_no_id_and_appends_no_record(self) -> None:
        store = self.stack.store
        before = store.sync_events(0)["latest_event_id"]
        with self.assertRaises(ValueError):
            store.publish_change_notice(lambda event_id: None)
        after = store.sync_events(0)
        self.assertEqual(after["latest_event_id"], before, "a rejected notice burns no id")
        self.assertEqual(self.typed_records(), [])


class AttributedBackendGuard(_CheckpointCase):
    """The composition seam: an attributed call must never reach a backend."""

    class _Commands:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def add_worklog(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"status": 201, "body": {"data": {}, "meta": {"replayed": False}}}

    def test_an_attributed_call_refuses_before_the_backend_is_called(self) -> None:
        commands = self._Commands()
        self.stack.intent_commands = commands
        with self.assertRaises(DomainError):
            self.commit("checkpoint.backend.0001")
        self.assertEqual(commands.calls, [], "the backend must not be dispatched")
        self.assertEqual(self.facts(), [], "no document may be written")
        self.assertEqual(self.typed_records(), [])

    def test_an_unattributed_call_keeps_ordinary_backend_behaviour(self) -> None:
        commands = self._Commands()
        self.stack.intent_commands = commands
        result = self.commit("checkpoint.backend.0002", origin=None)
        self.assertEqual(result["status"], 201)
        self.assertEqual(len(commands.calls), 1)
        self.assertNotIn(
            "origin", commands.calls[0][1], "the backend signature stays unchanged"
        )


class EncoderTypedFrames(unittest.TestCase):
    """The pure encoder: the whole batch validates before any byte is produced."""

    def notice(self, **overrides):
        notice = {
            "event_id": 5,
            "kind": "agent.checkpoint.committed",
            "workspace_uid": "11111111-2222-4333-8444-555555555555",
            "task_id": "T-0001",
            "date": "2026-09-03",
            "checkpoint_id": "CP-" + "a" * 64,
            "done_count": 1,
            "next_count": 0,
            "blocker_count": 0,
            "first_for_task": True,
            "origin": "agent-cli-v1",
            "replayed": False,
        }
        notice.update(overrides)
        return notice

    def batch(self, *events, latest=None, generation=7, state="in-sync"):
        return {
            "delivery": "bounded-process-local",
            "latest_event_id": latest if latest is not None else max(e["id"] for e in events),
            "generation": generation,
            "state": state,
            "events": list(events),
        }

    def legacy(self, identifier: int):
        return {
            "id": identifier,
            "type": "store.committed",
            "workspace_id": "11111111-2222-4333-8444-555555555555",
            "generation": 7,
            "changed_files": ["backlog.json"],
        }

    def typed(self, identifier: int, **overrides):
        return {
            "id": identifier,
            "type": CHANGE_NOTICE_TYPE,
            "notice": self.notice(event_id=identifier, **overrides),
        }

    def test_a_typed_record_renders_under_its_own_event_name(self) -> None:
        body = sse_events.encode_sync_stream(self.batch(self.typed(5)), 0).decode("utf-8")
        self.assertIn("event: workstack.change.v1\n", body)
        self.assertIn("id: 5\n", body)
        self.assertNotIn("event: sync", body)
        data = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(set(data), NOTICE_FIELDS)

    def test_mixed_records_keep_order_and_their_own_names(self) -> None:
        body = sse_events.encode_sync_stream(
            self.batch(self.legacy(4), self.typed(5), self.legacy(6)), 0
        ).decode("utf-8")
        names = [line[len("event: "):] for line in body.splitlines() if line.startswith("event: ")]
        self.assertEqual(names, ["sync", "workstack.change.v1", "sync"])

    def test_a_malformed_typed_record_raises_before_any_output(self) -> None:
        for broken in (
            self.typed(5, kind="agent.checkpoint.other"),
            self.typed(5, origin="browser"),
            self.typed(5, replayed=True),
            self.typed(5, done_count=0, next_count=0, blocker_count=0),
            self.typed(5, done_count=21),
            self.typed(5, task_id="T-1"),
            self.typed(5, checkpoint_id="CP-nothex"),
            self.typed(5, date="2026-9-3"),
            self.typed(5, workspace_uid="00000000-0000-0000-0000-000000000000"),
            self.typed(5, first_for_task=1),
        ):
            with self.subTest(broken=broken["notice"]):
                with self.assertRaises(sse_events.SseEncodingError):
                    sse_events.encode_sync_stream(self.batch(broken), 0)

    def test_an_extra_or_missing_field_is_refused(self) -> None:
        extra = self.typed(5)
        extra["notice"]["title"] = "leaked"
        with self.assertRaises(sse_events.SseEncodingError):
            sse_events.encode_sync_stream(self.batch(extra), 0)
        missing = self.typed(5)
        del missing["notice"]["origin"]
        with self.assertRaises(sse_events.SseEncodingError):
            sse_events.encode_sync_stream(self.batch(missing), 0)

    def test_a_payload_event_id_must_match_its_record_id(self) -> None:
        mismatched = self.typed(5)
        mismatched["notice"]["event_id"] = 6
        with self.assertRaises(sse_events.SseEncodingError):
            sse_events.encode_sync_stream(self.batch(mismatched, latest=9), 0)

    def test_the_existing_heartbeat_and_retention_bound_are_unchanged(self) -> None:
        empty = {
            "delivery": "bounded-process-local", "latest_event_id": 0,
            "generation": 7, "state": "in-sync", "events": [],
        }
        self.assertEqual(
            sse_events.encode_sync_stream(empty, 0).decode("utf-8"),
            "retry: 3000\n: heartbeat\n\n",
        )
        oversized = self.batch(*[self.legacy(i) for i in range(1, 130)])
        with self.assertRaises(sse_events.SseEncodingError):
            sse_events.encode_sync_stream(oversized, 0)

    def test_retention_gaps_and_a_late_cursor_stay_legitimate(self) -> None:
        body = sse_events.encode_sync_stream(
            self.batch(self.typed(40), self.legacy(90), latest=90), 10
        ).decode("utf-8")
        self.assertIn("id: 40\n", body)
        self.assertIn("id: 90\n", body)


class BackdatedFirstForTask(_CheckpointCase):
    """TP-F1: every previously accepted entry counts, whatever its date."""

    def test_a_backdated_append_after_a_later_entry_is_not_first(self) -> None:
        """The regression: a date filter made this report first_for_task true."""

        self.commit("checkpoint.backdate.0001", origin=None, date="2026-09-20")
        self.commit("checkpoint.backdate.0002", date="2026-09-01")
        notices = [record["notice"] for record in self.typed_records()]
        self.assertEqual(len(notices), 1, "only the attributed write publishes")
        self.assertFalse(
            notices[0]["first_for_task"],
            "an entry stored earlier, even for a later date, was still accepted first",
        )

    def test_a_genuinely_first_entry_is_still_first(self) -> None:
        """Healthy control: the fix must not make everything not-first."""

        self.commit("checkpoint.backdate.0003", date="2026-09-01")
        notices = [record["notice"] for record in self.typed_records()]
        self.assertTrue(notices[0]["first_for_task"])

    def test_the_date_local_preappend_ordinal_survives_the_fix(self) -> None:
        self.commit("checkpoint.backdate.0004", origin=None, date="2026-09-20")
        self.commit("checkpoint.backdate.0005", date="2026-09-01")
        self.commit("checkpoint.backdate.0006", date="2026-09-01")
        ordinals = [fact["ordinal"] for fact in self.facts()]
        self.assertEqual(
            ordinals, [0, 0, 1],
            "the ordinal stays date-local and pre-append, it is not global",
        )

    def test_the_stored_worklog_is_not_reordered_or_rewritten(self) -> None:
        self.commit("checkpoint.backdate.0007", origin=None, date="2026-09-20")
        self.commit("checkpoint.backdate.0008", date="2026-09-01")
        worklog = self.stack.store.load("worklog.json")
        self.assertEqual(sorted(worklog["days"]), ["2026-09-01", "2026-09-20"])
        for day in worklog["days"].values():
            self.assertEqual(len(day["entries"]), 1)
            self.assertEqual(
                set(day["entries"][0]), {"task_id", "task", "done", "next", "blockers"}
            )


class InjectedDocumentComposition(_CheckpointCase):
    """TP-F2: only the admitted released composition may carry attribution."""

    class _Delegating:
        """Satisfies the DocumentRepository protocol by delegating, nothing more."""

        def __init__(self, inner) -> None:
            self._inner = inner
            self.saves = 0

        def load(self, document):
            return self._inner.load(document)

        def save(self, document, value):
            self.saves += 1
            return self._inner.save(document, value)

        def save_many(self, writes, operation_id=None):
            self.saves += 1
            if operation_id is None:
                return self._inner.save_many(writes)
            return self._inner.save_many(writes, operation_id=operation_id)

        def total_bytes(self) -> int:
            return self._inner.total_bytes()

    def document_bytes(self) -> dict:
        return {
            name: (self.root / name).read_bytes()
            for name in ("worklog.json", "activity.json")
        }

    def test_an_injected_repository_refuses_an_attributed_write(self) -> None:
        injected = self._Delegating(self.stack.documents)
        self.stack.documents = injected
        before = self.document_bytes()
        with self.assertRaises(DomainError):
            self.commit("checkpoint.injected.0001")
        self.assertEqual(injected.saves, 0, "no write may be attempted")
        self.assertEqual(self.document_bytes(), before, "documents must be byte-identical")
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.typed_records(), [])

    def test_the_injected_repository_still_serves_an_ordinary_write(self) -> None:
        """Healthy control: only ATTRIBUTION is refused, not the composition."""

        injected = self._Delegating(self.stack.documents)
        self.stack.documents = injected
        result = self.commit("checkpoint.injected.0002", origin=None)
        self.assertEqual(result["status"], 201)
        self.assertEqual(injected.saves, 1)
        self.assertEqual(len(self.facts()), 1)
        self.assertEqual(self.typed_records(), [])

    def test_the_admitted_composition_still_accepts_attribution(self) -> None:
        """Healthy control: the real released composition is unaffected."""

        result = self.commit("checkpoint.injected.0003")
        self.assertEqual(result["status"], 201)
        self.assertEqual(len(self.typed_records()), 1)


class NoticeCapacityPreflight(_CheckpointCase):
    """TP-F3: capacity and notice validity are proven before mutation."""

    MAX_SAFE_INTEGER = 9007199254740991

    def document_bytes(self) -> dict:
        return {
            name: (self.root / name).read_bytes()
            for name in ("worklog.json", "activity.json")
        }

    def test_one_committed_save_emits_exactly_one_manifest_event(self) -> None:
        """The projected id depends on this, so it is measured, not assumed."""

        before = self.stack.store.sync_events(0)["latest_event_id"]
        self.commit("checkpoint.capacity.0001", origin=None)
        after = self.stack.store.sync_events(0)["latest_event_id"]
        self.assertEqual(after - before, 1, "an ordinary commit emits one event")

    def test_the_projection_reserves_the_worst_successful_branch(self) -> None:
        """Corrected: the default is the proved ceiling, not the best case.

        An earlier version of this test asserted current + 2, encoding the false
        premise that a successful commit always emits exactly one event. The
        late-external branches emit two or three, so the default now reserves
        MAX_COMMIT_EVENTS; the explicit-zero form is unchanged.
        """

        from workstack.store import MAX_COMMIT_EVENTS

        store = self.stack.store
        current = store.sync_events(0)["latest_event_id"]
        self.assertEqual(MAX_COMMIT_EVENTS, 3)
        self.assertEqual(
            store.projected_change_event_id(), current + MAX_COMMIT_EVENTS + 1
        )
        self.assertEqual(store.projected_change_event_id(pending_commit_events=0), current + 1)

    def test_the_safe_integer_boundary_refuses_before_any_mutation(self) -> None:
        """MAX-1 leaves room for the manifest event but not the typed one."""

        store = self.stack.store
        with store._process_lock:
            store._event_sequence = self.MAX_SAFE_INTEGER - 1
        before = self.document_bytes()
        facts_before = len(self.facts())
        # The refusal is caught broadly on purpose: WHICH exception surfaces is
        # secondary, and pinning the type would turn the real defect -- that the
        # documents were already committed -- into an error instead of the
        # failing assertion below.
        raised: Exception | None = None
        try:
            self.commit("checkpoint.capacity.0002")
        except Exception as error:  # noqa: BLE001 - the type is asserted after
            raised = error
        self.assertIsInstance(raised, DomainError, repr(raised))
        self.assertEqual(
            self.document_bytes(), before,
            "Worklog and Activity must be byte-identical after the refusal",
        )
        self.assertEqual(len(self.facts()), facts_before, "no fact may be recorded")
        self.assertEqual(self.typed_records(), [], "no typed notice is emitted")

    def test_one_below_the_boundary_still_commits_coherently(self) -> None:
        """Healthy control: the refusal is a boundary, not a blanket failure."""

        store = self.stack.store
        # One below the ceiling the corrected projection actually reserves.
        with store._process_lock:
            store._event_sequence = self.MAX_SAFE_INTEGER - 5
        result = self.commit("checkpoint.capacity.0003")
        self.assertEqual(result["status"], 201)
        records = self.typed_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], records[0]["notice"]["event_id"])
        self.assertEqual(len(self.facts()), 1)

    def test_a_save_failure_emits_nothing_at_all(self) -> None:
        class _Failing:
            def __init__(self, inner):
                self._inner = inner

            def load(self, document):
                return self._inner.load(document)

            def save(self, document, value):
                raise OSError("injected save failure")

            def save_many(self, writes, operation_id=None):
                raise OSError("injected save failure")

            def total_bytes(self):
                return self._inner.total_bytes()

        before = self.document_bytes()
        self.stack.documents = _Failing(self.stack.documents)
        with self.assertRaises(OSError):
            self.stack.add_worklog_v1(
                self.entry_body(), "checkpoint.capacity.0004",
                path="/api/v1/review/entries", origin=None,
            )
        self.assertEqual(self.document_bytes(), before)
        self.assertEqual(self.typed_records(), [])

    def test_a_replay_emits_nothing_and_preflights_nothing_new(self) -> None:
        self.commit("checkpoint.capacity.0005")
        before = len(self.typed_records())
        replay = self.commit("checkpoint.capacity.0005")
        self.assertEqual(replay["status"], 200)
        self.assertEqual(len(self.typed_records()), before)


class LateExternalCommitBranches(_CheckpointCase):
    """TPC-F1: how many events one SUCCESSFUL commit can really emit.

    The only thing substituted is the MOMENT an ordinary, legal whitespace byte
    is appended to an owned unrelated document. Every commit, manifest,
    race-resolution, event and publication method is the real one, and no event
    record is synthesized.
    """

    def unrelated_document(self):
        return self.root / "notes.json"

    def touch_unrelated(self) -> None:
        """An external writer's ordinary legal JSON whitespace change."""

        path = self.unrelated_document()
        path.write_bytes(path.read_bytes() + b" ")

    def events_emitted_by(self, action) -> int:
        store = self.stack.store
        before = store.sync_events(0)["latest_event_id"]
        action()
        return store.sync_events(0)["latest_event_id"] - before

    def install_seam(self, seam: str) -> None:
        """Append the external byte at one genuine point inside the real commit."""

        store = self.stack.store
        if seam == "after-local-writes":
            original = store._atomic_write_locked
            state = {"done": False}

            def patched(path, value):
                original(path, value)
                # After the prepared local writes land, before the commit reads
                # the authoritative hashes back for its race groups.
                if not state["done"] and path.name == "activity.json":
                    state["done"] = True
                    self.touch_unrelated()

            store._atomic_write_locked = patched
        elif seam == "before-manifest":
            original = store._write_committed_manifest_locked
            state = {"done": False}

            def patched(*args, **kwargs):
                if not state["done"]:
                    state["done"] = True
                    self.touch_unrelated()
                return original(*args, **kwargs)

            store._write_committed_manifest_locked = patched
        else:  # pragma: no cover - guarded by the caller
            raise AssertionError("unknown seam " + seam)

    # -- the three real successful branches --------------------------------
    def test_an_ordinary_commit_emits_one_event(self) -> None:
        emitted = self.events_emitted_by(
            lambda: self.commit("capacity.branch.0001", origin=None)
        )
        self.assertEqual(emitted, 1, "the undisturbed branch")

    def test_a_late_external_write_after_local_writes_emits_more_than_one(self) -> None:
        self.install_seam("after-local-writes")
        emitted = self.events_emitted_by(
            lambda: self.commit("capacity.branch.0002", origin=None)
        )
        self.assertGreater(emitted, 1, "this successful branch emits more than one event")
        self.assertLessEqual(emitted, 3)

    def test_a_late_external_write_before_the_manifest_emits_more_than_one(self) -> None:
        self.install_seam("before-manifest")
        emitted = self.events_emitted_by(
            lambda: self.commit("capacity.branch.0003", origin=None)
        )
        self.assertGreater(emitted, 1)
        self.assertLessEqual(emitted, 3, "three is the proved ceiling for this flow")

    def test_late_external_detection_still_produces_its_legacy_records(self) -> None:
        """Ordinary late-external behaviour must be preserved, not suppressed."""

        self.install_seam("before-manifest")
        self.commit("capacity.branch.0004", origin=None)
        batch = self.stack.store.sync_events(0)
        kinds = [event.get("type") for event in batch["events"]]
        self.assertTrue(
            any(kind not in (CHANGE_NOTICE_TYPE,) for kind in kinds),
            "the legacy records must survive",
        )
        self.assertTrue(any("external" in str(kind) for kind in kinds), kinds)

    # -- the projection must cover the worst successful branch -------------
    def test_the_projection_covers_every_successful_branch(self) -> None:
        store = self.stack.store
        current = store.sync_events(0)["latest_event_id"]
        self.assertGreaterEqual(
            store.projected_change_event_id(),
            current + 3 + 1,
            "the default projection must reserve room for the worst branch",
        )

    def test_a_late_external_branch_at_the_ceiling_refuses_before_mutation(self) -> None:
        """The reviewed failure: preflight passed, then the typed id overflowed."""

        store = self.stack.store
        with store._process_lock:
            store._event_sequence = 9007199254740991 - 2
        self.install_seam("after-local-writes")
        before = {
            name: (self.root / name).read_bytes()
            for name in ("worklog.json", "activity.json")
        }
        raised = None
        try:
            self.commit("capacity.branch.0005")
        except Exception as error:  # noqa: BLE001 - type asserted below
            raised = error
        self.assertIsInstance(raised, DomainError, repr(raised))
        after = {
            name: (self.root / name).read_bytes()
            for name in ("worklog.json", "activity.json")
        }
        self.assertEqual(after, before, "no fresh local durable mutation may happen")
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.typed_records(), [])

    def test_a_low_counter_late_external_branch_still_publishes(self) -> None:
        """Healthy control: the ceiling refuses, ordinary counters do not."""

        self.install_seam("after-local-writes")
        result = self.commit("capacity.branch.0006")
        self.assertEqual(result["status"], 201)
        records = self.typed_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], records[0]["notice"]["event_id"])
        self.assertEqual(len(self.facts()), 1)


class RepositoryStoreOwnership(_CheckpointCase):
    """TPC-F2: the repository's Store must BE the transaction and publisher."""

    def foreign_stack(self):
        """A second real Store, wrapped by the released adapter, wired to A."""

        import tempfile as _tempfile

        from workstack.storage.document_repository import StoreDocumentRepository

        self.foreign_temporary = _tempfile.TemporaryDirectory()
        self.addCleanup(self.foreign_temporary.cleanup)
        foreign_root = Path(self.foreign_temporary.name)
        foreign = WorkStack(Store(foreign_root))
        foreign.add_task("Foreign boundary")
        crossed = WorkStack(
            self.stack.store,
            initialize=False,
            document_repository=StoreDocumentRepository(foreign.store),
        )
        return crossed, foreign_root

    def document_bytes(self, root: Path) -> dict:
        return {
            name: (root / name).read_bytes()
            for name in ("worklog.json", "activity.json")
        }

    def test_an_attributed_call_refuses_when_the_repository_is_a_different_store(self) -> None:
        crossed, foreign_root = self.foreign_stack()
        owner_before = self.document_bytes(self.root)
        foreign_before = self.document_bytes(foreign_root)
        with self.assertRaises(DomainError):
            crossed.add_worklog_v1(
                {
                    "date": "2026-09-03", "task_id": self.task["id"],
                    "done": ["one"], "next": [], "blockers": [],
                },
                "capacity.owner.0001",
                path="/api/v1/review/entries",
                origin=ATTRIBUTED,
            )
        self.assertEqual(self.document_bytes(self.root), owner_before)
        self.assertEqual(self.document_bytes(foreign_root), foreign_before)
        self.assertEqual(self.typed_records(), [], "the owner must publish nothing")

    def test_the_same_object_released_composition_still_accepts_attribution(self) -> None:
        """Healthy control: identity binding must not break the real case."""

        result = self.commit("capacity.owner.0002")
        self.assertEqual(result["status"], 201)
        self.assertEqual(len(self.typed_records()), 1)

    def test_an_ordinary_different_store_call_is_unaffected(self) -> None:
        """Healthy control: only ATTRIBUTED calls gain the identity requirement."""

        crossed, foreign_root = self.foreign_stack()
        before = self.document_bytes(foreign_root)
        result = crossed.add_worklog_v1(
            {
                "date": "2026-09-03", "task_id": self.task["id"],
                "done": ["one"], "next": [], "blockers": [],
            },
            "capacity.owner.0003",
            path="/api/v1/review/entries",
            origin=None,
        )
        self.assertEqual(result["status"], 201)
        self.assertNotEqual(
            self.document_bytes(foreign_root), before,
            "the ordinary write still goes to the repository's own Store",
        )
        self.assertEqual(self.typed_records(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
