"""Oracle HTTP tests for GET /api/v1/reports/weekly-preview against a real loopback handler."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from workstack.capture import canonical_digest, fingerprint_for, source_key_for
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.weekly_reporting import TEMPLATE_WEEKLY_V1, preview_weekly_report
from workstack.weekly_reporting_http import (
    WeeklyPreviewHttpError,
    parse_weekly_preview_query,
    weekly_source_digest,
)


END = "2026-08-30"
START = "2026-08-24"
OTHER_DAY = "2026-08-31"
ATTRIBUTED = "agent-cli-v1"
OTHER_UID = "22222222-2222-4222-8222-222222222222"
NIL_UID = "00000000-0000-0000-0000-000000000000"
CANONICAL_UID = "11111111-1111-4111-8111-111111111111"
LETTERED_UID = "aa11bb22-cc33-4dd4-8ee5-ff6677889900"
PACKET = Path(__file__).resolve().parents[1] / "contracts" / "capture-packet-v1.fixture.json"
CATALOG_CANARY = "R45-WEEKLY-CATALOG-CANARY-TITLE"


class WeeklyPreviewQueryTest(unittest.TestCase):
    def test_parse_accepts_exactly_the_frozen_keys(self) -> None:
        query = urlencode({
            "end_date": END,
            "template": TEMPLATE_WEEKLY_V1,
            "workspace_uid": CANONICAL_UID,
        })
        self.assertEqual(
            parse_weekly_preview_query(query),
            {
                "end_date": END,
                "template": TEMPLATE_WEEKLY_V1,
                "workspace_uid": CANONICAL_UID,
            },
        )

    def test_parse_refuses_missing_blank_duplicate_extra_and_noncanonical(self) -> None:
        valid = {
            "end_date": END,
            "template": TEMPLATE_WEEKLY_V1,
            "workspace_uid": CANONICAL_UID,
        }
        cases = (
            "",
            urlencode(valid) + "&extra=1",
            urlencode(valid) + "&end_date=" + END,
            urlencode({**valid, "end_date": ""}),
            urlencode({**valid, "end_date": "2026-13-40"}),
            urlencode({**valid, "end_date": "2026-8-30"}),
            urlencode({**valid, "template": "daily-v1"}),
            urlencode({**valid, "workspace_uid": LETTERED_UID.upper()}),
            urlencode({**valid, "workspace_uid": NIL_UID}),
            urlencode({"template": TEMPLATE_WEEKLY_V1, "workspace_uid": CANONICAL_UID}),
        )
        for query in cases:
            with self.subTest(query=query):
                with self.assertRaises(WeeklyPreviewHttpError) as error:
                    parse_weekly_preview_query(query)
                self.assertEqual(error.exception.code, "invalid_query")
                self.assertEqual(error.exception.status, 400)
                self.assertEqual(error.exception.details, {})


class WeeklyReportPreviewHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Preview HTTP")
        with self.stack.store.consistent_read() as readiness:
            self.workspace_uid = readiness.workspace_uid
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def store_bytes(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in DEFAULTS}

    def preview_path(
        self,
        *,
        end_date: str = END,
        template: str = TEMPLATE_WEEKLY_V1,
        workspace_uid: str | None = None,
        extra: str = "",
    ) -> str:
        query = urlencode(
            {
                "end_date": end_date,
                "template": template,
                "workspace_uid": self.workspace_uid if workspace_uid is None else workspace_uid,
            }
        )
        if extra:
            query = query + "&" + extra
        return "/api/v1/reports/weekly-preview?" + query

    def request(
        self,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, raw, response_headers

    def json_request(
        self,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, dict[str, str], bytes]:
        status, raw, response_headers = self.request(path, headers)
        self.assertEqual(response_headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(response_headers["cache-control"], "no-store")
        return status, json.loads(raw.decode("utf-8")), response_headers, raw

    def add_entry(self, key: str, **fields: object) -> dict:
        origin = fields.pop("origin", None)
        body = {
            "date": END,
            "task_id": self.task["id"],
            "done": ["Closed one gate"],
            "next": ["Open the next gate"],
            "blockers": [],
        }
        body.update(fields)
        if origin is None:
            return self.stack.add_worklog_v1(body, key)
        return self.stack.add_worklog_v1(body, key, origin=str(origin))

    def expected_preview(self, generated_at: str, end_date: str = END) -> dict:
        return preview_weekly_report(
            projection=self.stack.review_projection(end_date, 7),
            end_date=end_date,
            template=TEMPLATE_WEEKLY_V1,
            generated_at=generated_at,
        )

    def expected_source_digest(self, end_date: str = END) -> str:
        projection = self.stack.review_projection(end_date, 7)
        return canonical_digest({"end_date": end_date, "weekly": projection["weekly"]})

    def assert_success_envelope(
        self,
        payload: dict,
        end_date: str = END,
        *,
        catalog_items: list[dict] | None = None,
        omitted_count: int = 0,
    ) -> dict:
        self.assertEqual(set(payload), {"data"})
        self.assertEqual(
            list(payload["data"]),
            ["workspace_uid", "source_digest", "preview", "context_catalog"],
        )
        self.assertEqual(payload["data"]["workspace_uid"], self.workspace_uid)
        digest = payload["data"]["source_digest"]
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(digest, self.expected_source_digest(end_date))
        preview = payload["data"]["preview"]
        self.assertEqual(preview, self.expected_preview(preview["generated_at"], end_date))
        self.assertEqual(preview["period"]["days"], 7)
        self.assertEqual(preview["period"]["end"], end_date)
        catalog = payload["data"]["context_catalog"]
        self.assertEqual(list(catalog), ["captured_at", "items", "omitted_count"])
        self.assertEqual(catalog["captured_at"], preview["generated_at"])
        self.assertEqual(catalog["omitted_count"], omitted_count)
        if catalog_items is None:
            self.assertEqual(catalog["items"], [])
        else:
            self.assertEqual(catalog["items"], catalog_items)
        return preview

    def ingest_capture(self, *, title: str, suffix: str) -> dict:
        packet = json.loads(PACKET.read_text(encoding="utf-8"))
        packet["source"]["display_title"] = title
        packet["source"]["object_ref"] = "message:r45-" + suffix
        packet["source"]["version_ref"] = "change-key:r45-" + suffix
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
        return self.stack.ingest_capture(
            packet, "r45.ingest." + suffix
        )["body"]["data"]

    def link_capture(self, capture_id: str, task_id: str, suffix: str) -> dict:
        return self.stack.link_capture(
            capture_id, task_id, "r45.link." + suffix
        )["body"]["data"]

    def catalog_item(self, capture: dict, linked_task_ids: list[str]) -> dict:
        return {
            "capture_id": capture["id"],
            "capture_revision": capture["revision"],
            "title": capture["source"]["display_title"],
            "linked_task_ids": linked_task_ids,
            "status": capture["status"],
        }

    def assert_stable_error(self, raw: bytes, payload: dict, forbidden: tuple[str, ...] = ()) -> None:
        text = raw.decode("utf-8")
        self.assertNotIn("Traceback", text)
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message", "details"})
        for item in forbidden:
            self.assertNotIn(item, text)
            self.assertNotIn(item, payload["error"]["message"])

    def _source_digest(self, end_date: str = END) -> str:
        status, payload, _, _ = self.json_request(self.preview_path(end_date=end_date))
        self.assertEqual(status, 200)
        self.assert_success_envelope(payload, end_date)
        return payload["data"]["source_digest"]

    def _poison_worklog(self, token: str = "SECRET-CORRUPT-BODY") -> str:
        (self.root / "worklog.json").write_text("{" + token, encoding="utf-8")
        return token

    def _replace_workspace_uid(self) -> str:
        path = self.root / "workspace.json"
        workspace = json.loads(path.read_text(encoding="utf-8"))
        replacement = str(uuid.uuid4())
        workspace["id"] = replacement
        path.write_text(json.dumps(workspace), encoding="utf-8")
        return replacement

    def _mutate_worklog_source(self, token: str = "EXTERNAL-SOURCE-FACT") -> str:
        path = self.root / "worklog.json"
        worklog = json.loads(path.read_text(encoding="utf-8"))
        day = worklog.setdefault("days", {}).setdefault(END, {"entries": []})
        day.setdefault("entries", []).append(
            {
                "task_id": self.task["id"],
                "task": "External title",
                "done": [token],
                "next": [],
                "blockers": [],
            }
        )
        path.write_text(json.dumps(worklog), encoding="utf-8")
        return token

    def test_valid_end_date_returns_seven_day_core_preview_without_mutation(self) -> None:
        self.add_entry("review.entry.weekly.http.0001")
        before = self.store_bytes()
        status, payload, headers, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertIn("x-workstack-request-id", headers)
        preview = self.assert_success_envelope(payload)
        self.assertEqual(preview["provenance"]["range"], {"start": START, "end": END, "days": 7})
        self.assertEqual(self.store_bytes(), before)

    def test_review_projection_is_read_once_for_preview_and_digest(self) -> None:
        self.add_entry("review.entry.weekly.http.readonce")
        calls: list[tuple[str, int]] = []
        original = self.stack.review_projection

        def counting(date: str, days: int = 7):
            calls.append((date, days))
            return original(date, days)

        before = self.store_bytes()
        with patch.object(self.stack, "review_projection", side_effect=counting):
            status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertEqual(calls, [(END, 7)])
        preview = payload["data"]["preview"]
        digest = weekly_source_digest(
            end_date=END,
            weekly=original(END, 7)["weekly"],
        )
        self.assertEqual(payload["data"]["source_digest"], digest)
        self.assertEqual(preview["period"]["days"], 7)
        self.assertEqual(self.store_bytes(), before)

    def test_source_digest_is_stable_across_clock_and_end_day_checkin(self) -> None:
        self.add_entry("review.entry.weekly.http.digest.clock")
        before = self.store_bytes()
        with patch("workstack.weekly_reporting_http._utc_now", return_value="2026-09-06T01:02:03Z"):
            status, first, _, _ = self.json_request(self.preview_path())
        with patch("workstack.weekly_reporting_http._utc_now", return_value="2026-09-07T23:59:59Z"):
            later_status, second, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertEqual(later_status, 200)
        self.assertEqual(first["data"]["source_digest"], second["data"]["source_digest"])
        self.assertNotEqual(
            first["data"]["preview"]["generated_at"],
            second["data"]["preview"]["generated_at"],
        )
        after_clock = first["data"]["source_digest"]
        self.stack.checkin_v1({"date": END, "time": "09:15"}, "review.checkin.weekly.http.digest")
        after_checkin = self._source_digest()
        self.assertEqual(after_clock, after_checkin)
        self.assertNotEqual(self.store_bytes()["worklog.json"], before["worklog.json"])

    def test_source_digest_changes_with_weekly_project_objective_fact_and_range(self) -> None:
        baseline = self._source_digest()
        self.add_entry("review.entry.weekly.http.digest.fact", done=["Changed fact"], next=[], blockers=[])
        after_fact = self._source_digest()
        self.assertNotEqual(baseline, after_fact)
        self.add_entry(
            "review.entry.weekly.http.digest.range",
            date=START,
            done=["Earlier fact"],
            next=[],
            blockers=[],
        )
        after_week_day = self._source_digest()
        self.assertNotEqual(after_fact, after_week_day)
        objective = self.stack.add_objective("Ship the weekly digest", "2030-Q2")
        linked = self.stack.add_task("Objective roll-up", objective_ids=[objective["id"]])
        self.add_entry(
            "review.entry.weekly.http.digest.objective",
            task_id=linked["id"],
            done=["Linked objective"],
            next=[],
            blockers=[],
        )
        after_objective = self._source_digest()
        self.assertNotEqual(after_week_day, after_objective)
        other_range = self._source_digest(end_date=OTHER_DAY)
        self.assertNotEqual(after_objective, other_range)
        self.assertEqual(after_objective, self.expected_source_digest())
        self.assertEqual(other_range, self.expected_source_digest(OTHER_DAY))

    def test_query_refusals_are_stable_400_without_raw_text(self) -> None:
        before = self.store_bytes()
        cases = (
            ("/api/v1/reports/weekly-preview", ()),
            (self.preview_path(extra="extra=1"), ("extra=1",)),
            (self.preview_path(extra="end_date=" + END), ()),
            (self.preview_path(end_date=""), ()),
            (self.preview_path(end_date="2026-13-40"), ("2026-13-40",)),
            (self.preview_path(template="daily-v1"), ("daily-v1",)),
            (self.preview_path(workspace_uid=self.workspace_uid.upper()), ()),
            (self.preview_path(workspace_uid=NIL_UID), (NIL_UID,)),
        )
        for path, forbidden in cases:
            with self.subTest(path=path):
                status, payload, _, raw = self.json_request(path)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_query")
                self.assertEqual(payload["error"]["details"], {})
                self.assert_stable_error(raw, payload, forbidden)
        self.assertEqual(self.store_bytes(), before)

    def test_workspace_mismatch_is_409_before_report_read(self) -> None:
        self.add_entry("review.entry.weekly.http.mismatch")
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path(workspace_uid=OTHER_UID))
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "workspace_mismatch")
        self.assertEqual(payload["error"]["details"], {})
        self.assert_stable_error(raw, payload, (OTHER_UID, "Closed one gate", "source_digest"))
        self.assertEqual(self.store_bytes(), before)

    def test_core_bounded_refusal_is_422_without_details(self) -> None:
        for index in range(8):
            task = self.stack.add_task("Bulk {}".format(index))
            self.add_entry(
                "review.entry.weekly.http.oversize.{:04d}".format(index),
                task_id=task["id"],
                done=["{:02d}{:02d}{}".format(index, item, "!" * 900) for item in range(20)],
                next=[],
                blockers=[],
            )
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "report_preview_unavailable")
        self.assertEqual(payload["error"]["details"], {})
        self.assert_stable_error(raw, payload, ("!" * 32, "projection", "source_digest"))
        self.assertEqual(self.store_bytes(), before)

    def test_extreme_civil_years_match_core_handling(self) -> None:
        before = self.store_bytes()
        status, payload, _, _ = self.json_request(self.preview_path(end_date="9999-12-31"))
        self.assertEqual(status, 200)
        preview = self.assert_success_envelope(payload, "9999-12-31")
        self.assertEqual(preview["period"]["start"], "9999-12-25")
        self.assertEqual(preview["absence"], "no records")
        status, payload, _, raw = self.json_request(self.preview_path(end_date="0001-01-01"))
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "report_preview_unavailable")
        self.assertEqual(payload["error"]["details"], {})
        self.assert_stable_error(raw, payload, ("OverflowError", "source_digest"))
        self.assertEqual(self.store_bytes(), before)

    def test_host_boundary_matches_existing_get_rules(self) -> None:
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(
            self.preview_path(),
            headers={"Host": "example.com:80"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_host")
        self.assert_stable_error(raw, payload)
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assert_success_envelope(payload)
        self.assertEqual(self.store_bytes(), before)

    def test_matching_owner_with_malformed_candidate_requires_sync(self) -> None:
        token = self._poison_worklog()
        mutated = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "invalid")
        self.assertIn("worklog.json", payload["error"]["details"]["changed_files"])
        self.assert_stable_error(raw, payload, (token, "source_digest"))
        self.assertEqual(self.store_bytes(), mutated)

    def test_external_change_refuses_with_store_sync_required(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-external", "text": "Changed", "links": [], "created": END})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        mutated = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "external-change-detected")
        self.assertEqual(payload["error"]["details"]["changed_files"], ["notes.json"])
        self.assert_stable_error(raw, payload, ("N-external",))
        self.assertEqual(self.store_bytes(), mutated)

    def test_race_malformed_candidate_after_precheck_is_store_sync_required(self) -> None:
        self.add_entry("review.entry.weekly.http.race.malformed")
        token = "SECRET-CORRUPT-BODY"

        def poison() -> None:
            self._poison_worklog(token)

        with patch("workstack.weekly_reporting_http._after_precheck_race_hook", side_effect=poison):
            status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "invalid")
        self.assert_stable_error(raw, payload, (token, "Closed one gate", "source_digest"))

    def test_race_workspace_uid_swap_after_precheck_is_store_sync_required(self) -> None:
        captured: dict[str, str] = {}

        def swap() -> None:
            captured["replacement"] = self._replace_workspace_uid()

        with patch("workstack.weekly_reporting_http._after_precheck_race_hook", side_effect=swap):
            status, payload, _, raw = self.json_request(self.preview_path())
        replacement = captured["replacement"]
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertNotEqual(payload["error"]["code"], "workspace_mismatch")
        self.assert_stable_error(raw, payload, (replacement, "source_digest"))

    def test_race_source_assembly_change_after_projection_is_store_sync_required(self) -> None:
        self.add_entry("review.entry.weekly.http.race.source")
        token = "EXTERNAL-SOURCE-FACT"

        def mutate() -> None:
            self._mutate_worklog_source(token)

        with patch("workstack.weekly_reporting_http._after_projection_race_hook", side_effect=mutate):
            status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "external-change-detected")
        self.assert_stable_error(raw, payload, (token, "source_digest"))

    def test_multi_day_related_captures_appear_beside_unchanged_preview(self) -> None:
        self.add_entry("review.entry.weekly.http.catalog.end")
        earlier = self.stack.add_task("Earlier week task")
        self.add_entry(
            "review.entry.weekly.http.catalog.start",
            date=START,
            task_id=earlier["id"],
            done=["Closed an earlier gate"],
            next=[],
            blockers=[],
        )
        digest_before = self.expected_source_digest()
        end_capture = self.ingest_capture(title=CATALOG_CANARY, suffix="end-day")
        end_linked = self.link_capture(end_capture["id"], self.task["id"], "end-day")
        start_capture = self.ingest_capture(title="Earlier-week context", suffix="start-day")
        start_linked = self.link_capture(start_capture["id"], earlier["id"], "start-day")
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        preview = self.assert_success_envelope(
            payload,
            catalog_items=[
                self.catalog_item(end_linked, [self.task["id"]]),
                self.catalog_item(start_linked, [earlier["id"]]),
            ],
        )
        self.assertEqual(
            set(preview["provenance"]["task_ids"]),
            {self.task["id"], earlier["id"]},
        )
        self.assertEqual(preview["provenance"]["range"], {"start": START, "end": END, "days": 7})
        self.assertEqual(payload["data"]["source_digest"], digest_before)
        self.assertNotIn(CATALOG_CANARY, preview["markdown"])
        self.assertNotIn("Earlier-week context", preview["markdown"])
        self.assertNotIn("context_catalog", preview)
        self.assertNotIn(b"web_url", raw)
        self.assertEqual(self.store_bytes(), before)

    def test_empty_week_omits_linked_captures(self) -> None:
        capture = self.ingest_capture(title=CATALOG_CANARY, suffix="empty-week")
        self.link_capture(capture["id"], self.task["id"], "empty-week")
        before = self.store_bytes()
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        preview = self.assert_success_envelope(payload)
        self.assertEqual(preview["absence"], "no records")
        self.assertEqual(preview["provenance"]["task_ids"], [])
        self.assertEqual(self.store_bytes(), before)

    def test_converted_provenance_alone_is_not_a_catalog_link(self) -> None:
        self.add_entry("review.entry.weekly.http.catalog.converted.end")
        capture = self.ingest_capture(title=CATALOG_CANARY, suffix="converted")
        converted = self.stack.create_task_from_capture(
            capture["id"],
            {"title": "Converted from the capture"},
            "r45.convert.0001",
        )["body"]["data"]
        self.add_entry(
            "review.entry.weekly.http.catalog.converted.start",
            date=START,
            task_id=converted["id"],
            done=["Converted task earlier in the week"],
            next=[],
            blockers=[],
        )
        before = self.store_bytes()
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        preview = self.assert_success_envelope(payload)
        self.assertIn(converted["id"], preview["provenance"]["task_ids"])
        self.assertIn(self.task["id"], preview["provenance"]["task_ids"])
        self.assertEqual(self.store_bytes(), before)

    def test_unrelated_capture_is_omitted(self) -> None:
        self.add_entry("review.entry.weekly.http.catalog.unrelated")
        other = self.stack.add_task("Unrelated task")
        capture = self.ingest_capture(title=CATALOG_CANARY, suffix="unrelated")
        self.link_capture(capture["id"], other["id"], "unrelated")
        before = self.store_bytes()
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assert_success_envelope(payload)
        self.assertEqual(self.store_bytes(), before)

    def test_catalog_truncates_after_thirty_two_qualifying_captures(self) -> None:
        earlier = self.stack.add_task("Overflow week task")
        self.add_entry(
            "review.entry.weekly.http.catalog.truncate",
            date=START,
            task_id=earlier["id"],
            done=["Earlier overflow fact"],
            next=[],
            blockers=[],
        )
        rows = []
        for index in range(1, 34):
            suffix = "trunc-{0:04d}".format(index)
            capture = self.ingest_capture(
                title="Context {0:04d}".format(index), suffix=suffix
            )
            rows.append(self.link_capture(capture["id"], earlier["id"], suffix))
        before = self.store_bytes()
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        expected = [self.catalog_item(row, [earlier["id"]]) for row in rows[:32]]
        preview = self.assert_success_envelope(
            payload, catalog_items=expected, omitted_count=1
        )
        self.assertEqual(
            payload["data"]["context_catalog"]["items"][-1]["capture_id"],
            rows[31]["id"],
        )
        self.assertIn(earlier["id"], preview["provenance"]["task_ids"])
        self.assertEqual(self.store_bytes(), before)

    def test_catalogue_loads_captures_before_the_projection_race_hook(self) -> None:
        order: list[str] = []
        original = WorkStack.list_captures

        def tracking_list(stack: WorkStack, status: str = "inbox") -> list:
            order.append("captures:" + status)
            return original(stack, status)

        def hook() -> None:
            order.append("hook")

        with patch.object(WorkStack, "list_captures", tracking_list):
            with patch(
                "workstack.weekly_reporting_http._after_projection_race_hook",
                side_effect=hook,
            ):
                status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assert_success_envelope(payload)
        self.assertEqual(order, ["captures:all", "hook"])

    def test_race_capture_change_after_snapshot_is_store_sync_required(self) -> None:
        self.add_entry("review.entry.weekly.http.race.capture")
        token = "EXTERNAL-CAPTURE-FACT"

        def mutate() -> None:
            path = self.root / "captures.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("captures", []).append({"id": "C-external", "title": token})
            path.write_text(json.dumps(data), encoding="utf-8")

        with patch("workstack.weekly_reporting_http._after_projection_race_hook", side_effect=mutate):
            status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "external-change-detected")
        self.assertIn("captures.json", payload["error"]["details"]["changed_files"])
        self.assert_stable_error(raw, payload, (token, "source_digest"))
        self.assertIn(token, (self.root / "captures.json").read_text(encoding="utf-8"))
