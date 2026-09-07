"""Oracle HTTP tests for GET /api/v1/reports/daily-preview against a real loopback handler."""

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

from workstack.capture import canonical_digest
from workstack.reporting import TEMPLATE_DAILY_V1, inert_text, preview_daily_report
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store


DAY = "2026-08-30"
ATTRIBUTED = "agent-cli-v1"
OTHER_UID = "22222222-2222-4222-8222-222222222222"
NIL_UID = "00000000-0000-0000-0000-000000000000"
KOREAN_FACT = "café — 한글"


class DailyReportPreviewHttpTest(unittest.TestCase):
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
        date: str = DAY,
        template: str = TEMPLATE_DAILY_V1,
        workspace_uid: str | None = None,
        extra: str = "",
    ) -> str:
        query = urlencode(
            {
                "date": date,
                "template": template,
                "workspace_uid": self.workspace_uid if workspace_uid is None else workspace_uid,
            }
        )
        if extra:
            query = query + "&" + extra
        return "/api/v1/reports/daily-preview?" + query

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
            "date": DAY,
            "task_id": self.task["id"],
            "done": ["Closed one gate"],
            "next": ["Open the next gate"],
            "blockers": [],
        }
        body.update(fields)
        if origin is None:
            return self.stack.add_worklog_v1(body, key)
        return self.stack.add_worklog_v1(body, key, origin=str(origin))

    def expected_preview(self, generated_at: str, date: str = DAY) -> dict:
        return preview_daily_report(
            projection=self.stack.review_projection(date, 1),
            date=date,
            template=TEMPLATE_DAILY_V1,
            generated_at=generated_at,
        )

    def expected_source_digest(self, date: str = DAY) -> str:
        projection = self.stack.review_projection(date, 1)
        return canonical_digest({"date": date, "day": projection["day"]})

    def assert_success_envelope(self, payload: dict, date: str = DAY) -> dict:
        self.assertEqual(set(payload), {"data"})
        self.assertEqual(
            list(payload["data"]),
            ["workspace_uid", "source_digest", "preview"],
        )
        self.assertEqual(payload["data"]["workspace_uid"], self.workspace_uid)
        digest = payload["data"]["source_digest"]
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(digest, self.expected_source_digest(date))
        preview = payload["data"]["preview"]
        self.assertEqual(preview, self.expected_preview(preview["generated_at"], date))
        return preview

    def assert_stable_error(self, raw: bytes, payload: dict, forbidden: tuple[str, ...] = ()) -> None:
        text = raw.decode("utf-8")
        self.assertNotIn("Traceback", text)
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message", "details"})
        for item in forbidden:
            self.assertNotIn(item, text)
            self.assertNotIn(item, payload["error"]["message"])

    def _source_digest(self, date: str = DAY) -> str:
        status, payload, _, _ = self.json_request(self.preview_path(date=date))
        self.assertEqual(status, 200)
        self.assert_success_envelope(payload, date)
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
        day = worklog.setdefault("days", {}).setdefault(DAY, {"entries": []})
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

    def test_valid_date_and_uid_returns_exact_core_preview(self) -> None:
        self.add_entry("review.entry.http.0001")
        before = self.store_bytes()
        status, payload, headers, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertIn("x-workstack-request-id", headers)
        preview = self.assert_success_envelope(payload)
        self.assertEqual(preview["provenance"]["weekly_range"]["days"], 1)
        self.assertEqual(self.store_bytes(), before)

    def test_empty_records_say_no_records_never_no_work(self) -> None:
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        preview = self.assert_success_envelope(payload)
        self.assertEqual(preview["absence"], "no records")
        self.assertIn("No records.", preview["markdown"])
        self.assertNotIn("no work", preview["markdown"].casefold())
        self.assertNotIn(b"no work", raw.lower())
        self.assertEqual(self.store_bytes(), before)

    def test_superseded_entry_is_absent_from_active_preview(self) -> None:
        self.add_entry("review.entry.http.0002", origin=ATTRIBUTED)
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["preview"]["provenance"]["sources"]), 1)
        checkpoint = self.stack.list_checkpoint_audit()["entries"][-1]["checkpoint_id"]
        body = {
            "state": "superseded",
            "revision": 0,
            "reason": {"code": "incorrect", "explanation": "because"},
        }
        self.stack.apply_checkpoint_transition_v1(
            checkpoint,
            body,
            "review.transition.http.0001",
            path="/api/v1/review/checkpoints/{}/transitions".format(checkpoint),
            request_digest=canonical_digest(body),
            origin=ATTRIBUTED,
        )
        before = self.store_bytes()
        status, payload, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        preview = self.assert_success_envelope(payload)
        self.assertEqual(preview["absence"], "no records")
        self.assertEqual(preview["provenance"]["sources"], [])
        self.assertNotIn("Closed one gate", preview["markdown"])
        self.assertEqual(self.store_bytes(), before)

    def test_korean_facts_are_preserved_as_utf8(self) -> None:
        self.add_entry(
            "review.entry.http.nonascii",
            done=[KOREAN_FACT],
            next=[],
            blockers=[],
        )
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertIn("한글".encode("utf-8"), raw)
        preview = self.assert_success_envelope(payload)
        self.assertIn(inert_text(KOREAN_FACT), preview["markdown"])
        self.assertEqual(self.store_bytes(), before)

    def test_query_refusals_are_stable_400_without_raw_text(self) -> None:
        before = self.store_bytes()
        cases = (
            ("/api/v1/reports/daily-preview", ()),
            (self.preview_path(extra="extra=1"), ("extra=1",)),
            (self.preview_path(extra="date=" + DAY), ()),
            (self.preview_path(date=""), ()),
            (self.preview_path(date="2026-13-40"), ("2026-13-40",)),
            (self.preview_path(date="2026-8-30"), ("2026-8-30",)),
            (self.preview_path(template="weekly-v1"), ("weekly-v1",)),
            (self.preview_path(workspace_uid=self.workspace_uid.upper()), ()),
            (self.preview_path(workspace_uid=NIL_UID), (NIL_UID,)),
            (
                "/api/v1/reports/daily-preview?"
                + urlencode({"template": TEMPLATE_DAILY_V1, "workspace_uid": self.workspace_uid}),
                (),
            ),
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
        self.add_entry("review.entry.http.mismatch")
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path(workspace_uid=OTHER_UID))
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "workspace_mismatch")
        self.assertEqual(payload["error"]["details"], {})
        self.assert_stable_error(raw, payload, (OTHER_UID, "Closed one gate"))
        self.assertEqual(self.store_bytes(), before)

    def test_core_bounded_refusal_is_422_without_details(self) -> None:
        bulky = ["a" * 1000] * 20
        for index in range(6):
            self.add_entry(
                "review.entry.http.oversize.{:04d}".format(index),
                done=bulky,
                next=[],
                blockers=[],
            )
        before = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "report_preview_unavailable")
        self.assertEqual(payload["error"]["details"], {})
        self.assert_stable_error(raw, payload, ("a" * 32, "projection"))
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

    def test_source_digest_is_stable_across_generation_clocks(self) -> None:
        self.add_entry("review.entry.http.digest.clock")
        before = self.store_bytes()
        with patch("workstack.reporting_http._utc_now", return_value="2026-09-06T01:02:03Z"):
            status, first, _, _ = self.json_request(self.preview_path())
        with patch("workstack.reporting_http._utc_now", return_value="2026-09-07T23:59:59Z"):
            later_status, second, _, _ = self.json_request(self.preview_path())
        self.assertEqual(status, 200)
        self.assertEqual(later_status, 200)
        self.assert_success_envelope(first)
        self.assert_success_envelope(second)
        self.assertEqual(first["data"]["source_digest"], second["data"]["source_digest"])
        self.assertEqual(first["data"]["source_digest"], self.expected_source_digest())
        self.assertNotEqual(
            first["data"]["preview"]["generated_at"],
            second["data"]["preview"]["generated_at"],
        )
        self.assertEqual(self.store_bytes(), before)

    def test_source_digest_changes_with_checkin_facts_title_and_date(self) -> None:
        empty = self._source_digest()
        self.stack.checkin_v1({"date": DAY, "time": "09:15"}, "review.checkin.http.digest")
        after_checkin = self._source_digest()
        self.assertNotEqual(empty, after_checkin)
        self.add_entry("review.entry.http.digest.fact", done=["Changed fact"], next=[], blockers=[])
        after_fact = self._source_digest()
        self.assertNotEqual(after_checkin, after_fact)
        other = self.stack.add_task("Other title")
        self.stack.add_worklog_v1(
            {
                "date": DAY,
                "task_id": other["id"],
                "done": ["Second title"],
                "next": [],
                "blockers": [],
            },
            "review.entry.http.digest.title",
        )
        after_title = self._source_digest()
        self.assertNotEqual(after_fact, after_title)
        other_day = self._source_digest(date="2026-08-31")
        self.assertNotEqual(after_title, other_day)
        self.assertEqual(after_title, self.expected_source_digest())
        self.assertEqual(other_day, self.expected_source_digest("2026-08-31"))

    def test_wrong_uid_with_poisoned_store_is_content_free_mismatch(self) -> None:
        self.add_entry("review.entry.http.poison")
        token = self._poison_worklog()
        mutated = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path(workspace_uid=OTHER_UID))
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "workspace_mismatch")
        self.assertEqual(payload["error"]["details"], {})
        self.assert_stable_error(raw, payload, (OTHER_UID, token, "Closed one gate", "source_digest"))
        self.assertEqual(self.store_bytes(), mutated)

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

    def test_external_workspace_replacement_keeps_original_owner_on_sync_required(self) -> None:
        workspace_path = self.root / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        replacement = str(uuid.uuid4())
        workspace["id"] = replacement
        workspace_path.write_text(json.dumps(workspace), encoding="utf-8")
        mutated = self.store_bytes()
        status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "invalid")
        self.assertEqual(payload["error"]["details"]["changed_files"], ["workspace.json"])
        self.assertNotEqual(payload["error"]["code"], "workspace_mismatch")
        self.assert_stable_error(raw, payload, (replacement, "source_digest"))
        self.assertEqual(self.store_bytes(), mutated)

    def test_external_change_refuses_with_store_sync_required(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-external", "text": "Changed", "links": [], "created": DAY})
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
        self.add_entry("review.entry.http.race.malformed")
        token = "SECRET-CORRUPT-BODY"

        def poison() -> None:
            self._poison_worklog(token)

        with patch("workstack.reporting_http._after_precheck_race_hook", side_effect=poison):
            status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "invalid")
        self.assertIn("worklog.json", payload["error"]["details"]["changed_files"])
        self.assert_stable_error(raw, payload, (token, "Closed one gate", "source_digest"))
        self.assertEqual((self.root / "worklog.json").read_text(encoding="utf-8"), "{" + token)

    def test_race_workspace_uid_swap_after_precheck_is_store_sync_required(self) -> None:
        captured: dict[str, str] = {}

        def swap() -> None:
            captured["replacement"] = self._replace_workspace_uid()

        with patch("workstack.reporting_http._after_precheck_race_hook", side_effect=swap):
            status, payload, _, raw = self.json_request(self.preview_path())
        replacement = captured["replacement"]
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "invalid")
        self.assertEqual(payload["error"]["details"]["changed_files"], ["workspace.json"])
        self.assertNotEqual(payload["error"]["code"], "workspace_mismatch")
        self.assert_stable_error(raw, payload, (replacement, "source_digest"))
        self.assertEqual(
            json.loads((self.root / "workspace.json").read_text(encoding="utf-8"))["id"],
            replacement,
        )

    def test_race_source_assembly_change_after_projection_is_store_sync_required(self) -> None:
        self.add_entry("review.entry.http.race.source")
        token = "EXTERNAL-SOURCE-FACT"

        def mutate() -> None:
            self._mutate_worklog_source(token)

        with patch("workstack.reporting_http._after_projection_race_hook", side_effect=mutate):
            status, payload, _, raw = self.json_request(self.preview_path())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "store_sync_required")
        self.assertEqual(payload["error"]["details"]["state"], "external-change-detected")
        self.assertIn("worklog.json", payload["error"]["details"]["changed_files"])
        self.assert_stable_error(raw, payload, (token, "source_digest"))
        self.assertIn(token, (self.root / "worklog.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
