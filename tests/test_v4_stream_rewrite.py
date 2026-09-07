"""Wave 4 D3A: pure v4 stream rewrite for permanent-delete staging.

Tests use converted fixture copies and synthetic chained streams only.
They must not open the live SSOT or bind port 8765.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import unittest
from pathlib import Path
from unittest import mock

from workstack.storage.canonical import canonical_json_bytes, canonical_sha256
from workstack.storage.layout import STREAM_KINDS
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.v4_stream_rewrite import (
    V4StreamDeletion,
    V4StreamRewriteError,
    rewrite_v4_deletion_streams,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
LIVE_SSOT = Path.home() / "WorkStack" / "SSOT" / "main"
CANDIDATE_CREATED_AT = "2026-09-01T12:00:00Z"


def _assert_not_live_ssot() -> None:
    resolved = FIXTURE.resolve()
    live = LIVE_SSOT.resolve() if LIVE_SSOT.exists() else LIVE_SSOT
    if resolved == live or live in resolved.parents or resolved in live.parents:
        raise AssertionError("refusing to use live SSOT path")


def _load_streams() -> dict[str, list[dict]]:
    _assert_not_live_ssot()
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE.glob("*.json"))
    }
    conversion = convert_v3_documents(documents, candidate_created_at=CANDIDATE_CREATED_AT)
    return {kind: [dict(item) for item in conversion.streams[kind]] for kind in STREAM_KINDS}


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _segment_digests(streams: dict[str, list[dict]]) -> dict[str, str]:
    grouped: dict[str, list[dict]] = {}
    for kind, events in streams.items():
        for event in events:
            artifact = f"streams/{kind}/{str(event['created_at'])[:7]}.ndjson"
            grouped.setdefault(artifact, []).append(event)
    digests: dict[str, str] = {}
    for artifact, events in grouped.items():
        ordered = sorted(events, key=lambda item: int(item["sequence"]))
        body = b"\n".join(canonical_json_bytes(dict(event)) for event in ordered) + b"\n"
        digests[artifact] = _digest(body)
    return digests


def _chain(entries: list[tuple[str, dict]]) -> dict[str, list[dict]]:
    previous = None
    grouped = {kind: [] for kind in STREAM_KINDS}
    for sequence, (kind, source) in enumerate(entries, start=1):
        event = copy.deepcopy(source)
        event.pop("event_digest", None)
        event.pop("previous_event_digest", None)
        event["sequence"] = sequence
        if previous is not None:
            event["previous_event_digest"] = previous
        event["event_digest"] = canonical_sha256(event)
        previous = event["event_digest"]
        grouped[kind].append(event)
    return grouped


def _ordered(streams: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    entries = [(kind, event) for kind in STREAM_KINDS for event in streams[kind]]
    entries.sort(key=lambda item: int(item[1]["sequence"]))
    return entries


def _freeze(targets) -> tuple:
    return tuple(
        (
            item.action,
            item.scope,
            item.artifact,
            item.expected_digest,
            item.proposed_digest,
            item.proposed_bytes,
        )
        for item in targets
    )


def _lines(body: bytes) -> list[dict]:
    return [json.loads(line) for line in body.decode("utf-8").splitlines() if line]


def _empty_deletion() -> V4StreamDeletion:
    return V4StreamDeletion((), (), (), ())


def _populated_t0001_deletion(streams: dict[str, list[dict]]) -> V4StreamDeletion:
    activity = tuple(
        str(event.get("legacy_event_id") or event["event_uid"])
        for event in streams["activity"]
        if event.get("legacy_event_id") in {"E-000002", "E-000003"}
    )
    planning = tuple(
        str(event.get("legacy_fact_id") or event["event_uid"])
        for event in streams["planning-status"]
        if event.get("legacy_fact_id") in {"PS-000001", "PS-000003"}
    )
    entries = tuple(
        (str(event["event_uid"]), str(event["work_date"]))
        for event in streams["worklog"]
        if event.get("kind") == "entry"
    )
    sessions = tuple(
        (str(event["event_uid"]), str(event["work_date"]), str(event["session_id"]))
        for event in streams["worklog"]
        if event.get("kind") == "session"
    )
    return V4StreamDeletion(activity, planning, entries, sessions)


class StreamRewriteDeterminismTest(unittest.TestCase):
    def test_shuffled_inputs_are_byte_identical_and_inputs_stay_frozen(self) -> None:
        streams = _load_streams()
        before = copy.deepcopy(streams)
        deletion = _populated_t0001_deletion(streams)
        digests = _segment_digests(streams)
        first = rewrite_v4_deletion_streams(streams, deletion, segment_digests=digests)
        second = rewrite_v4_deletion_streams(streams, deletion, segment_digests=digests)
        self.assertEqual(_freeze(first), _freeze(second))
        self.assertEqual(streams, before)
        rng = random.Random(20260904)
        items = list(streams.items())
        rng.shuffle(items)
        shuffled = {}
        for kind, events in items:
            copied = [copy.deepcopy(dict(event)) for event in events]
            rng.shuffle(copied)
            shuffled[kind] = copied
        third = rewrite_v4_deletion_streams(
            shuffled, deletion, segment_digests=_segment_digests(shuffled)
        )
        self.assertEqual(_freeze(first), _freeze(third))
        self.assertEqual(streams, before)

    def test_rewrite_does_not_open_files_or_read_clocks(self) -> None:
        streams = _load_streams()
        deletion = _populated_t0001_deletion(streams)
        digests = _segment_digests(streams)

        def refuse_open(*_args, **_kwargs):
            raise AssertionError("stream rewrite opened a file")

        def refuse_clock(*_args, **_kwargs):
            raise AssertionError("stream rewrite read a clock")

        with mock.patch("builtins.open", refuse_open), mock.patch(
            "time.time", refuse_clock
        ), mock.patch("time.monotonic", refuse_clock):
            rewrite_v4_deletion_streams(streams, deletion, segment_digests=digests)


class StreamRewriteOracleTest(unittest.TestCase):
    def test_global_rechain_and_planning_predecessor_repair(self) -> None:
        streams = _load_streams()
        deletion = _populated_t0001_deletion(streams)
        targets = rewrite_v4_deletion_streams(
            streams, deletion, segment_digests=_segment_digests(streams)
        )
        by_artifact = {item.artifact: item for item in targets}
        planning = _lines(by_artifact["streams/planning-status/2026-09.ndjson"].proposed_bytes)
        self.assertEqual(len(planning), 1)
        self.assertEqual(planning[0]["legacy_fact_id"], "PS-000001")
        self.assertIsNone(planning[0]["previous_legacy_fact_id"])
        self.assertIsNone(planning[0]["previous_event_uid"])
        self.assertEqual(planning[0]["sequence"], 1)
        activity = _lines(by_artifact["streams/activity/2026-09.ndjson"].proposed_bytes)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["legacy_event_id"], "E-000001")
        self.assertEqual(activity[0]["sequence"], 2)
        self.assertEqual(activity[0]["previous_event_digest"], planning[0]["event_digest"])
        worklog = _lines(by_artifact["streams/worklog/2026-09.ndjson"].proposed_bytes)
        self.assertEqual([item["kind"] for item in worklog], ["check-in"])
        self.assertEqual(worklog[0]["sequence"], 3)
        for event in (*planning, *activity, *worklog):
            payload = dict(event)
            claimed = payload.pop("event_digest")
            self.assertEqual(claimed, canonical_sha256(payload))
        combined = planning + activity + worklog
        self.assertEqual([item["sequence"] for item in combined], [1, 2, 3])

    def test_planning_identity_rewrite_after_interleaved_removal(self) -> None:
        streams = _load_streams()
        deletion = V4StreamDeletion(
            activity_ids=(),
            planning_ids=("PS-000002",),
            worklog_entries=(),
            worklog_sessions=(),
        )
        targets = rewrite_v4_deletion_streams(
            streams, deletion, segment_digests=_segment_digests(streams)
        )
        planning = _lines(
            next(
                item.proposed_bytes
                for item in targets
                if item.artifact == "streams/planning-status/2026-09.ndjson"
            )
        )
        self.assertEqual([item["legacy_fact_id"] for item in planning], ["PS-000001", "PS-000002"])
        self.assertIsNone(planning[0]["previous_legacy_fact_id"])
        self.assertEqual(planning[1]["previous_legacy_fact_id"], "PS-000001")
        self.assertEqual(planning[1]["previous_event_uid"], planning[0]["event_uid"])
        original = [
            event
            for event in streams["planning-status"]
            if event["legacy_fact_id"] != "PS-000002"
        ]
        self.assertEqual(planning[0]["event_uid"], original[0]["event_uid"])
        self.assertEqual(planning[1]["event_uid"], original[1]["event_uid"])
        self.assertEqual(original[1]["legacy_fact_id"], "PS-000003")

    def test_removal_before_within_and_across_segments_and_empty_delete(self) -> None:
        streams = _load_streams()
        ordered = _ordered(streams)
        rebuilt: list[tuple[str, dict]] = []
        august_uid = None
        for kind, event in ordered:
            copied = copy.deepcopy(event)
            if kind == "activity" and event.get("legacy_event_id") == "E-000001":
                copied["created_at"] = "2026-08-01T02:00:00Z"
                august_uid = copied["legacy_event_id"]
            rebuilt.append((kind, copied))
        chained = _chain(rebuilt)
        deletion = V4StreamDeletion(
            activity_ids=(august_uid, "E-000002"),
            planning_ids=(),
            worklog_entries=(),
            worklog_sessions=(),
        )
        targets = rewrite_v4_deletion_streams(
            chained, deletion, segment_digests=_segment_digests(chained)
        )
        by_artifact = {item.artifact: item for item in targets}
        self.assertEqual(by_artifact["streams/activity/2026-08.ndjson"].action, "delete")
        self.assertIsNone(by_artifact["streams/activity/2026-08.ndjson"].proposed_bytes)
        september = _lines(by_artifact["streams/activity/2026-09.ndjson"].proposed_bytes)
        self.assertEqual([item["legacy_event_id"] for item in september], ["E-000003"])
        self.assertNotIn("streams/planning-status/2026-08.ndjson", by_artifact)

    def test_unchanged_segment_is_omitted(self) -> None:
        streams = _load_streams()
        last_worklog = streams["worklog"][-1]
        if last_worklog.get("kind") == "entry":
            deletion = V4StreamDeletion(
                (),
                (),
                ((str(last_worklog["event_uid"]), str(last_worklog["work_date"])),),
                (),
            )
        else:
            deletion = V4StreamDeletion(
                (),
                (),
                (),
                (
                    (
                        str(last_worklog["event_uid"]),
                        str(last_worklog["work_date"]),
                        str(last_worklog["session_id"]),
                    ),
                ),
            )
        targets = rewrite_v4_deletion_streams(
            streams, deletion, segment_digests=_segment_digests(streams)
        )
        artifacts = {item.artifact for item in targets}
        self.assertNotIn("streams/planning-status/2026-09.ndjson", artifacts)
        self.assertNotIn("streams/activity/2026-09.ndjson", artifacts)
        self.assertIn("streams/worklog/2026-09.ndjson", artifacts)


class StreamRewriteFailClosedTest(unittest.TestCase):
    def test_gap_duplicate_broken_chain_and_missing_digest_refuse(self) -> None:
        streams = _load_streams()
        deletion = _empty_deletion()
        digests = _segment_digests(streams)
        gapped = copy.deepcopy(streams)
        gapped["activity"][0]["sequence"] = 99
        with self.assertRaises(V4StreamRewriteError) as ctx:
            rewrite_v4_deletion_streams(gapped, deletion, segment_digests=digests)
        self.assertEqual(ctx.exception.code, "invalid_stream")
        self.assertEqual(str(ctx.exception), "invalid_stream")
        duplicated = copy.deepcopy(streams)
        duplicated["activity"][1]["event_uid"] = duplicated["activity"][0]["event_uid"]
        with self.assertRaises(V4StreamRewriteError):
            rewrite_v4_deletion_streams(duplicated, deletion, segment_digests=digests)
        broken = copy.deepcopy(streams)
        broken["activity"][0]["event_digest"] = "sha256:" + ("ab" * 32)
        with self.assertRaises(V4StreamRewriteError):
            rewrite_v4_deletion_streams(broken, deletion, segment_digests=digests)
        missing = dict(digests)
        missing.pop("streams/activity/2026-09.ndjson")
        with self.assertRaises(V4StreamRewriteError):
            rewrite_v4_deletion_streams(
                streams, _populated_t0001_deletion(streams), segment_digests=missing
            )

    def test_unknown_or_duplicate_identity_refuses_without_partial_targets(self) -> None:
        streams = _load_streams()
        digests = _segment_digests(streams)
        with self.assertRaises(V4StreamRewriteError) as ctx:
            rewrite_v4_deletion_streams(
                streams,
                V4StreamDeletion(("E-999999",), (), (), ()),
                segment_digests=digests,
            )
        self.assertEqual(str(ctx.exception), "invalid_stream")
        with self.assertRaises(V4StreamRewriteError):
            rewrite_v4_deletion_streams(
                streams,
                V4StreamDeletion(("E-000002", "E-000002"), (), (), ()),
                segment_digests=digests,
            )

    def test_inconsistent_planning_predecessor_refuses(self) -> None:
        streams = _load_streams()
        streams["planning-status"][2]["previous_event_uid"] = streams["planning-status"][1][
            "event_uid"
        ]
        with self.assertRaises(V4StreamRewriteError):
            rewrite_v4_deletion_streams(
                streams, _empty_deletion(), segment_digests=_segment_digests(streams)
            )

    def test_legacy_and_event_uid_for_one_activity_event_is_ambiguous(self) -> None:
        streams = _load_streams()
        event = next(
            item for item in streams["activity"] if item.get("legacy_event_id") == "E-000002"
        )
        with self.assertRaises(V4StreamRewriteError) as ctx:
            rewrite_v4_deletion_streams(
                streams,
                V4StreamDeletion(
                    (str(event["legacy_event_id"]), str(event["event_uid"])),
                    (),
                    (),
                    (),
                ),
                segment_digests=_segment_digests(streams),
            )
        self.assertEqual(ctx.exception.code, "invalid_stream")
        self.assertEqual(str(ctx.exception), "invalid_stream")
        self.assertNotIn(str(event["event_uid"]), str(ctx.exception))

    def test_legacy_and_event_uid_for_one_planning_event_is_ambiguous(self) -> None:
        streams = _load_streams()
        event = next(
            item
            for item in streams["planning-status"]
            if item.get("legacy_fact_id") == "PS-000001"
        )
        with self.assertRaises(V4StreamRewriteError) as ctx:
            rewrite_v4_deletion_streams(
                streams,
                V4StreamDeletion(
                    (),
                    (str(event["legacy_fact_id"]), str(event["event_uid"])),
                    (),
                    (),
                ),
                segment_digests=_segment_digests(streams),
            )
        self.assertEqual(ctx.exception.code, "invalid_stream")
        self.assertEqual(str(ctx.exception), "invalid_stream")
