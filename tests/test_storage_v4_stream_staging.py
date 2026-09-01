from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.streams import V4StreamStagingError, stage_stream_appends


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T00:00:00Z"


def _conversion():
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE.glob("*.json")
    }
    return convert_v3_documents(documents, candidate_created_at=CREATED_AT)


def _body(events) -> bytes:
    return b"\n".join(canonical_json_bytes(dict(event)) for event in events) + b"\n"


def _artifact_digests(streams):
    grouped = {}
    for kind, events in streams.items():
        for event in events:
            artifact = f"streams/{kind}/{event['created_at'][:7]}.ndjson"
            grouped.setdefault(artifact, []).append(event)
    return {
        artifact: "sha256:" + hashlib.sha256(_body(events)).hexdigest()
        for artifact, events in grouped.items()
    }


def _activity_draft(conversion, *, created_at="2026-09-01T02:12:00Z"):
    event = copy.deepcopy(dict(conversion.streams["activity"][-1]))
    for field in ("sequence", "previous_event_digest", "event_digest"):
        event.pop(field, None)
    event["event_uid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    event["legacy_event_id"] = "E-999999"
    event["created_at"] = created_at
    event["event_type"] = "task.reviewed"
    return event


class V4StreamStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        conversion = _conversion()
        self.streams = {
            kind: tuple(copy.deepcopy(list(events)))
            for kind, events in conversion.streams.items()
        }
        self.digests = _artifact_digests(self.streams)
        self.draft = _activity_draft(conversion)

    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(V4StreamStagingError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_append_assigns_global_sequence_chain_and_full_segment_bytes(self) -> None:
        before = copy.deepcopy((self.streams, self.draft, self.digests))

        first = stage_stream_appends(
            self.streams,
            (("activity", self.draft),),
            current_artifact_digests=self.digests,
        )
        second = stage_stream_appends(
            self.streams,
            (("activity", self.draft),),
            current_artifact_digests=self.digests,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        write = first[0]
        appended = write.appended_events[0]
        self.assertEqual(write.artifact, "streams/activity/2026-09.ndjson")
        self.assertEqual(appended["sequence"], 10)
        global_head = next(
            event
            for events in self.streams.values()
            for event in events
            if event["sequence"] == 9
        )
        self.assertEqual(appended["previous_event_digest"], global_head["event_digest"])
        self.assertRegex(appended["event_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(write.body.endswith(b"\n"))
        self.assertEqual((self.streams, self.draft, self.digests), before)

    def test_new_month_creates_a_segment_without_an_expected_digest(self) -> None:
        draft = copy.deepcopy(self.draft)
        draft["created_at"] = "2026-10-01T00:00:00Z"

        writes = stage_stream_appends(
            self.streams,
            (("activity", draft),),
            current_artifact_digests=self.digests,
        )

        self.assertEqual(writes[0].artifact, "streams/activity/2026-10.ndjson")
        self.assertIsNone(writes[0].expected_digest)
        self.assertEqual(writes[0].first_sequence, 10)

    def test_stale_segment_late_event_owned_chain_and_duplicate_uid_are_rejected(self) -> None:
        stale = dict(self.digests)
        stale["streams/activity/2026-09.ndjson"] = "sha256:" + "0" * 64
        self.assert_code(
            "STALE_STREAM_DIGEST",
            lambda: stage_stream_appends(
                self.streams,
                (("activity", self.draft),),
                current_artifact_digests=stale,
            ),
        )

        late = copy.deepcopy(self.draft)
        late["created_at"] = "2026-09-01T01:00:00Z"
        self.assert_code(
            "LATE_EVENT_REJECTED",
            lambda: stage_stream_appends(
                self.streams,
                (("activity", late),),
                current_artifact_digests=self.digests,
            ),
        )

        owned = copy.deepcopy(self.draft)
        owned["sequence"] = 10
        self.assert_code(
            "CHAIN_FIELD_OWNED_BY_WRITER",
            lambda: stage_stream_appends(
                self.streams,
                (("activity", owned),),
                current_artifact_digests=self.digests,
            ),
        )

        duplicate = copy.deepcopy(self.draft)
        duplicate["event_uid"] = self.streams["activity"][0]["event_uid"]
        self.assert_code(
            "DUPLICATE_EVENT_UID",
            lambda: stage_stream_appends(
                self.streams,
                (("activity", duplicate),),
                current_artifact_digests=self.digests,
            ),
        )

    def test_multiple_additions_share_one_deterministic_global_chain(self) -> None:
        second = copy.deepcopy(self.draft)
        second["event_uid"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        second["legacy_event_id"] = "E-1000000"
        second["created_at"] = "2026-09-01T02:13:00Z"

        writes = stage_stream_appends(
            self.streams,
            (("activity", self.draft), ("activity", second)),
            current_artifact_digests=self.digests,
        )

        events = writes[0].appended_events
        self.assertEqual([event["sequence"] for event in events], [10, 11])
        self.assertEqual(events[1]["previous_event_digest"], events[0]["event_digest"])
        self.assertEqual(writes[0].last_sequence, 11)


if __name__ == "__main__":
    unittest.main()
