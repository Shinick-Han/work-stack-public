"""Daily Markdown preview core against real review projections."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workstack.capture import canonical_digest
from workstack.reporting import (
    MAX_PROJECTION_BYTES,
    TEMPLATE_DAILY_V1,
    DailyReportPreviewError,
    inert_text,
    preview_daily_report,
)
from workstack.service import WorkStack
from workstack.store import Store


GENERATED_AT = "2026-09-06T01:02:03Z"
ATTRIBUTED = "agent-cli-v1"
DAY = "2026-08-30"
REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "preview_daily_report.py"
CLI_ERROR = "preview input is invalid"
_ASCII_PUNCT = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'


def visible_text(inert: str) -> str:
    """Undo inert encoding the way CommonMark visible text would."""

    parts: list[str] = []
    index = 0
    while index < len(inert):
        if inert.startswith("&amp;", index):
            parts.append("&")
            index += 5
            continue
        if inert.startswith("&lt;", index):
            parts.append("<")
            index += 4
            continue
        if inert.startswith("&gt;", index):
            parts.append(">")
            index += 4
            continue
        if (
            index + 1 < len(inert)
            and inert[index] == "\\"
            and inert[index + 1] in _ASCII_PUNCT
        ):
            parts.append(inert[index + 1])
            index += 2
            continue
        parts.append(inert[index])
        index += 1
    return "".join(parts)


def preview(projection, date=DAY, template=TEMPLATE_DAILY_V1, generated_at=GENERATED_AT):
    return preview_daily_report(
        projection=projection,
        date=date,
        template=template,
        generated_at=generated_at,
    )


class DailyReportPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Intent boundary")

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def real_projection(self, date: str = DAY) -> dict:
        return self.stack.review_projection(date)

    def run_cli(self, raw: bytes, date: str = DAY, template: str = TEMPLATE_DAILY_V1, generated_at: str = GENERATED_AT):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--date",
                date,
                "--template",
                template,
                "--generated-at",
                generated_at,
            ],
            input=raw,
            capture_output=True,
            cwd=str(REPO),
        )

    def assert_fact_visible(self, raw: str, markdown: str) -> str:
        encoded = inert_text(raw)
        self.assertIn(encoded, markdown)
        self.assertEqual(visible_text(encoded), raw)
        return encoded

    def assert_cli_rejected(self, completed) -> None:
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(stderr.strip(), CLI_ERROR)
        self.assertNotIn("Traceback", stderr)
        self.assertLessEqual(len(stderr), 64)

    def test_real_review_projection_is_the_source_not_a_hand_built_copy(self) -> None:
        self.stack.checkin_v1({"date": DAY, "time": "09:20"}, "review.checkin.preview.0001")
        self.add_entry("review.entry.preview.0001")
        projection = self.real_projection()
        self.assertEqual(projection["day"]["date"], DAY)
        self.assertEqual(len(projection["day"]["entries"]), 1)
        report = preview(projection)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["template"], TEMPLATE_DAILY_V1)
        self.assertEqual(report["period"], {"kind": "day", "date": DAY})
        self.assertEqual(report["generated_at"], GENERATED_AT)
        self.assertNotEqual(report["period"]["date"], report["generated_at"][:10])
        self.assertIsNone(report["absence"])
        self.assertEqual(report["provenance"]["date"], DAY)
        self.assertEqual(report["provenance"]["task_ids"], [self.task["id"]])
        self.assertEqual(report["provenance"]["sources"][0]["kind"], "review.day.entry")
        self.assertEqual(report["provenance"]["sources"][0]["index"], 0)
        self.assertEqual(report["provenance"]["sources"][0]["task_id"], self.task["id"])
        self.assertEqual(
            report["provenance"]["weekly_range"],
            projection["weekly"]["range"],
        )
        self.assert_fact_visible("Closed one gate", report["markdown"])
        self.assert_fact_visible(self.task["id"], report["markdown"])
        self.assert_fact_visible("09:20", report["markdown"])
        self.assertNotIn("no work", report["markdown"].casefold())
        json.loads(encoded)

    def test_empty_day_says_no_records_never_no_work(self) -> None:
        projection = self.real_projection()
        self.assertEqual(projection["day"]["entries"], [])
        report = preview(projection)
        self.assertEqual(report["absence"], "no records")
        self.assertEqual(report["provenance"]["task_ids"], [])
        self.assertEqual(report["provenance"]["sources"], [])
        self.assertIn("No records.", report["markdown"])
        self.assertNotIn("no work", report["markdown"].casefold())

    def test_superseded_active_membership_matches_review_projection(self) -> None:
        self.add_entry("review.entry.preview.0002", origin=ATTRIBUTED)
        before = self.real_projection()
        self.assertEqual(len(before["day"]["entries"]), 1)
        checkpoint = self.stack.list_checkpoint_audit()["entries"][-1]["checkpoint_id"]
        body = {
            "state": "superseded",
            "revision": 0,
            "reason": {"code": "incorrect", "explanation": "because"},
        }
        self.stack.apply_checkpoint_transition_v1(
            checkpoint,
            body,
            "review.transition.preview.0001",
            path="/api/v1/review/checkpoints/{}/transitions".format(checkpoint),
            request_digest=canonical_digest(body),
            origin=ATTRIBUTED,
        )
        after = self.real_projection()
        self.assertEqual(after["day"]["entries"], [])
        report = preview(after)
        self.assertEqual(report["absence"], "no records")
        self.assertEqual(report["provenance"]["sources"], [])
        self.assertIn("No records.", report["markdown"])
        self.assertNotIn("Closed one gate", report["markdown"])

    def test_repeated_source_entries_stay_distinct(self) -> None:
        self.add_entry("review.entry.preview.0003", done=["First fact"], next=[], blockers=[])
        self.add_entry("review.entry.preview.0004", done=["Second fact"], next=[], blockers=[])
        report = preview(self.real_projection())
        self.assertEqual(len(report["provenance"]["sources"]), 2)
        self.assertEqual(report["provenance"]["task_ids"], [self.task["id"]])
        self.assertEqual(
            [source["index"] for source in report["provenance"]["sources"]],
            [0, 1],
        )
        self.assertIn("First fact", report["markdown"])
        self.assertIn("Second fact", report["markdown"])
        self.assertEqual(
            report["markdown"].count("### {}".format(inert_text(self.task["id"]))),
            2,
        )

    def test_sparse_and_partial_facts_are_named_not_invented(self) -> None:
        self.add_entry(
            "review.entry.preview.0005",
            done=[],
            next=[],
            blockers=["Need a review"],
        )
        projection = self.real_projection()
        projection["day"]["entries"][0].pop("task")
        report = preview(projection)
        self.assertIn("title omitted", report["markdown"])
        self.assertIn("Need a review", report["markdown"])
        self.assertNotIn("Closed one gate", report["markdown"])
        self.assertNotIn("progress", report["markdown"].casefold())
        self.assertNotIn("completed", report["markdown"].casefold())
        self.assertIn("Not recorded.", report["markdown"])

    def test_malicious_markup_is_inert_literal_text(self) -> None:
        payload = "[pwn](javascript:alert(1)) <script>x</script> ![img](http://evil.example/x)"
        self.add_entry("review.entry.preview.0006", done=[payload], next=[], blockers=[])
        report = preview(self.real_projection())
        escaped = inert_text(payload)
        self.assertIn(escaped, report["markdown"])
        self.assertEqual(visible_text(escaped), payload)
        self.assertNotIn("[pwn](javascript:alert(1))", report["markdown"])
        self.assertNotIn("<script>", report["markdown"])
        self.assertNotIn("![img](", report["markdown"])
        self.assertIn("&lt;script&gt;", report["markdown"])
        self.assertIn("\\[pwn\\]", report["markdown"])

    def test_every_ascii_punctuation_character_is_neutralized(self) -> None:
        for char in _ASCII_PUNCT:
            encoded = inert_text(char)
            self.assertEqual(visible_text(encoded), char)
            if char in "&<>":
                self.assertTrue(encoded.startswith("&") and encoded.endswith(";"))
            else:
                self.assertEqual(encoded, "\\" + char)

    def test_gfm_constructs_stay_literal_in_fact_contexts(self) -> None:
        mixed = "*em* _em_ ~~del~~ a|b https://evil.example/x"
        # The ONE synthetic address literal in this file. GFM autolinks a bare
        # address, so the real behaviour under test needs the real shape; it is
        # bound here once and reused below rather than repeated.
        bare_address = "person@example.com"
        self.add_entry(
            "review.entry.preview.0011",
            done=["# nested heading", "- list", "1. item", mixed],
            next=[bare_address],
            blockers=[],
        )
        projection = self.real_projection()
        projection["day"]["start_time"] = "# heading"
        report = preview(projection)
        for fact in (
            "# heading",
            "# nested heading",
            "- list",
            "1. item",
            mixed,
            bare_address,
        ):
            self.assert_fact_visible(fact, report["markdown"])
        self.assertNotIn("\n# heading\n", report["markdown"])
        self.assertNotIn("- # nested heading", report["markdown"])
        self.assertNotIn("- - list", report["markdown"])
        self.assertNotIn("1. item", report["markdown"])
        self.assertNotIn("*em*", report["markdown"])
        self.assertNotIn("_em_", report["markdown"])
        self.assertNotIn("~~del~~", report["markdown"])
        self.assertNotIn("a|b", report["markdown"])
        self.assertNotIn("https://evil.example/x", report["markdown"])
        self.assertNotIn("https://", report["markdown"])
        self.assertNotIn(bare_address, report["markdown"])

    def test_prior_generated_preview_keys_are_not_ingested(self) -> None:
        self.add_entry("review.entry.preview.0007")
        projection = self.real_projection()
        projection["template"] = TEMPLATE_DAILY_V1
        projection["markdown"] = "# Daily review invented\n\nNo work.\n"
        projection["generated_at"] = "1999-01-01T00:00:00Z"
        report = preview(projection)
        self.assertEqual(
            report["provenance"]["ignored_keys"],
            ["generated_at", "markdown", "template"],
        )
        self.assertNotEqual(report["markdown"], projection["markdown"])
        self.assertNotIn("invented", report["markdown"])
        self.assertNotIn("no work", report["markdown"].casefold())

    def test_same_inputs_are_byte_identical(self) -> None:
        self.add_entry("review.entry.preview.0008")
        projection = self.real_projection()
        first = json.dumps(preview(projection), ensure_ascii=False, sort_keys=True)
        second = json.dumps(preview(projection), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_unsupported_template_and_bad_date_and_timestamp_are_refused(self) -> None:
        projection = self.real_projection()
        with self.assertRaises(DailyReportPreviewError) as template_error:
            preview(projection, template="weekly-v1")
        self.assertEqual(template_error.exception.details["field"], "template")
        with self.assertRaises(DailyReportPreviewError) as date_error:
            preview(projection, date="2026-13-01")
        self.assertEqual(date_error.exception.details["field"], "date")
        with self.assertRaises(DailyReportPreviewError) as stamp_error:
            preview(projection, generated_at="tonight")
        self.assertEqual(stamp_error.exception.details["field"], "generated_at")

    def test_mismatched_or_malformed_projection_is_refused(self) -> None:
        projection = self.real_projection()
        with self.assertRaises(DailyReportPreviewError) as mismatch:
            preview(projection, date="2026-08-31")
        self.assertEqual(mismatch.exception.details["field"], "date")
        with self.assertRaises(DailyReportPreviewError) as malformed:
            preview_daily_report(
                projection=["not", "an", "object"],
                date=DAY,
                template=TEMPLATE_DAILY_V1,
                generated_at=GENERATED_AT,
            )
        self.assertEqual(malformed.exception.details["field"], "projection")

    def test_oversized_item_is_refused(self) -> None:
        self.add_entry("review.entry.preview.0009")
        projection = self.real_projection()
        projection["day"]["entries"][0]["done"] = ["x" * 1001]
        with self.assertRaises(DailyReportPreviewError) as error:
            preview(projection)
        self.assertEqual(error.exception.details["field"], "item")
        self.assertIn("oversized", str(error.exception))

    def test_weekly_projects_are_not_copied_as_completion(self) -> None:
        self.add_entry("review.entry.preview.0010")
        projection = self.real_projection()
        projection["weekly"]["projects"] = [{
            "task_id": self.task["id"],
            "task": self.task["title"],
            "done": ["Invented completion"],
            "next": [],
            "blockers": [],
            "objective_ids": [],
            "dates": [DAY],
            "duration_seconds": 99,
        }]
        report = preview(projection)
        self.assertNotIn("Invented completion", report["markdown"])
        self.assertEqual(report["provenance"]["weekly_range"], projection["weekly"]["range"])

    def test_non_json_lone_surrogate_and_deep_projection_are_refused(self) -> None:
        projection = self.real_projection()
        projection["stamp"] = object()
        with self.assertRaises(DailyReportPreviewError) as non_json:
            preview(projection)
        self.assertEqual(non_json.exception.details["field"], "projection")

        projection = self.real_projection()
        projection["day"]["start_time"] = "\ud800"
        with self.assertRaises(DailyReportPreviewError) as surrogate:
            preview(projection)
        self.assertEqual(surrogate.exception.details["field"], "projection")

        projection = self.real_projection()
        cursor: dict = projection
        for _ in range(4000):
            nested: dict = {}
            cursor["n"] = nested
            cursor = nested
        with self.assertRaises(DailyReportPreviewError) as deep:
            preview(projection)
        self.assertEqual(deep.exception.details["field"], "projection")

    def test_nan_and_infinity_are_refused(self) -> None:
        projection = self.real_projection()
        projection["weekly"]["bad"] = float("nan")
        with self.assertRaises(DailyReportPreviewError) as nan_error:
            preview(projection)
        self.assertEqual(nan_error.exception.details["field"], "projection")
        projection = self.real_projection()
        projection["weekly"]["bad"] = float("inf")
        with self.assertRaises(DailyReportPreviewError) as inf_error:
            preview(projection)
        self.assertEqual(inf_error.exception.details["field"], "projection")

    def test_stdin_cli_roundtrip_uses_real_review_projection(self) -> None:
        self.stack.checkin_v1({"date": DAY, "time": "09:20"}, "review.checkin.preview.cli")
        self.add_entry("review.entry.preview.cli")
        projection = self.real_projection()
        expected = preview(projection)
        completed = self.run_cli(json.dumps(projection, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(completed.stdout.decode("utf-8")), expected)
        self.assertEqual(completed.stderr, b"")

    def test_stdin_cli_rejects_invalid_utf8_json_deep_surrogate_and_oversize(self) -> None:
        self.assert_cli_rejected(self.run_cli(b"\xff"))
        self.assert_cli_rejected(self.run_cli(b"["))
        self.assert_cli_rejected(self.run_cli(b"[]"))
        deep = ("{" + '"n":' * 2000 + "{}" + "}" * 2000).encode("utf-8")
        self.assertLessEqual(len(deep), MAX_PROJECTION_BYTES)
        self.assert_cli_rejected(self.run_cli(deep))
        surrogate = json.dumps({"x": "ok", "s": "\ud800"}, ensure_ascii=True).encode("ascii")
        self.assert_cli_rejected(self.run_cli(surrogate))
        self.assert_cli_rejected(self.run_cli(b'{"x": NaN}'))
        self.assert_cli_rejected(self.run_cli(b'{"x": Infinity}'))
        self.assert_cli_rejected(self.run_cli(b'{"x": -Infinity}'))
        self.assert_cli_rejected(self.run_cli(b"x" * (MAX_PROJECTION_BYTES + 1)))

    def test_stdin_cli_roundtrip_keeps_nonascii_facts_as_utf8_bytes(self) -> None:
        fact = "café — 한글"
        self.add_entry(
            "review.entry.preview.nonascii",
            done=[fact],
            next=[],
            blockers=[],
        )
        projection = self.real_projection()
        expected = preview(projection)
        raw = json.dumps(projection, ensure_ascii=False).encode("utf-8")
        self.assertIn("한글".encode("utf-8"), raw)
        completed = self.run_cli(raw)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(completed.stdout.decode("utf-8")), expected)
        self.assertIn("한글".encode("utf-8"), completed.stdout)
        self.assert_fact_visible(fact, expected["markdown"])


if __name__ == "__main__":
    unittest.main()
