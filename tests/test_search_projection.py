from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workstack.service import DomainError, WorkStack
from workstack.store import Store


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


class SearchProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.stack = WorkStack(Store(Path(self.temporary.name)))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projects_every_allowlisted_kind_in_stable_kind_order(self) -> None:
        task = self.stack.add_task("Release signal task")
        objective = self.stack.add_objective("Release signal objective", "2026-Q3")
        note = self.stack.add_note("Release signal note")
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.manual.fixture.json").read_text(encoding="utf-8")
        )
        packet["source"]["display_title"] = "Release signal capture"
        capture = self.stack.ingest_capture(packet, "search.capture.0001")["body"]["data"]
        with self.stack.store.transaction():
            activity = self.stack.store.load("activity.json")
            self.stack._event(activity, "release.signal")
            event = activity["activity"][-1]
            self.stack.store.save_many(
                {"activity.json": activity}, operation_id="search-release-activity"
            )

        result = self.stack.search_projection("release signal", 50)
        expected_ids = [task["id"], objective["id"], note["id"], capture["id"], event["id"]]
        positions = {item["id"]: index for index, item in enumerate(result["items"])}

        self.assertEqual(result["query"], "release signal")
        self.assertEqual(sorted(expected_ids, key=positions.get), expected_ids)
        self.assertEqual(
            {item["kind"] for item in result["items"] if item["id"] in expected_ids},
            {"task", "objective", "note", "capture", "activity"},
        )

    def test_activity_search_ignores_non_allowlisted_detail_values(self) -> None:
        with self.stack.store.transaction():
            activity = self.stack.store.load("activity.json")
            self.stack._event(
                activity,
                "task.observed",
                details={"private_payload": "DO_NOT_PROJECT_REPLY_BODY"},
            )
            self.stack.store.save_many(
                {"activity.json": activity}, operation_id="search-private-activity"
            )

        result = self.stack.search_projection("DO_NOT_PROJECT_REPLY_BODY")

        self.assertEqual(result["items"], [])

    def test_rejects_invalid_query_and_boolean_limit(self) -> None:
        for query in (None, "x", "a" * 101, "bad\nquery"):
            with self.subTest(query=query), self.assertRaises(DomainError):
                self.stack.search_projection(query)  # type: ignore[arg-type]
        with self.assertRaises(DomainError):
            self.stack.search_projection("valid query", True)


if __name__ == "__main__":
    unittest.main()
