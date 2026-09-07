"""Weekly Markdown preview core against real review projections."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workstack.capture import canonical_digest
from workstack.reporting import MAX_ITEM_CHARS, MAX_MARKDOWN_CHARS, inert_text
from workstack.service import WorkStack
from workstack.store import Store
from workstack.weekly_reporting import (
    MAX_DATES_PER_PROJECT,
    MAX_ITEMS_PER_FIELD,
    MAX_OBJECTIVES,
    MAX_OBJECTIVES_PER_PROJECT,
    MAX_PROJECTS,
    TEMPLATE_WEEKLY_V1,
    WeeklyReportPreviewError,
    preview_weekly_report,
)


GENERATED_AT = "2026-09-06T01:02:03Z"
ATTRIBUTED = "agent-cli-v1"
END = "2026-08-30"
START = "2026-08-24"
WEEK = [
    "2026-08-24",
    "2026-08-25",
    "2026-08-26",
    "2026-08-27",
    "2026-08-28",
    "2026-08-29",
    "2026-08-30",
]


def preview(projection, end_date=END, template=TEMPLATE_WEEKLY_V1, generated_at=GENERATED_AT):
    return preview_weekly_report(
        projection=projection,
        end_date=end_date,
        template=template,
        generated_at=generated_at,
    )


class WeeklyReportPreviewTest(unittest.TestCase):
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

    def real_projection(self) -> dict:
        return self.stack.review_projection(END, 7)

    def store_bytes(self) -> dict[str, bytes]:
        mapping: dict[str, bytes] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                mapping[path.relative_to(self.root).as_posix()] = path.read_bytes()
        return mapping

    def refuse(self, **kwargs):
        projection = kwargs.pop("projection", None)
        if projection is None:
            projection = self.real_projection()
        with self.assertRaises(WeeklyReportPreviewError) as error:
            preview(projection, **kwargs)
        return error.exception

    def test_same_task_text_on_two_dates_renders_once(self) -> None:
        fact = "Shared gate"
        self.add_entry("review.entry.weekly.0001", date=START, done=[fact], next=[], blockers=[])
        self.add_entry("review.entry.weekly.0002", date=END, done=[fact], next=[], blockers=[])
        projection = self.real_projection()
        self.assertEqual(projection["day"]["date"], END)
        self.assertEqual(projection["weekly"]["range"]["start"], START)
        self.assertEqual(projection["weekly"]["range"]["end"], END)
        self.assertEqual(projection["weekly"]["range"]["days"], 7)
        report = preview(projection)
        self.assertEqual(report["template"], TEMPLATE_WEEKLY_V1)
        self.assertEqual(report["period"], {"kind": "week", "start": START, "end": END, "days": 7})
        self.assertEqual(report["generated_at"], GENERATED_AT)
        self.assertIsNone(report["absence"])
        self.assertEqual(report["provenance"]["task_ids"], [self.task["id"]])
        self.assertEqual(
            report["provenance"]["sources"],
            [{
                "kind": "review.weekly.project",
                "task_id": self.task["id"],
                "dates": [START, END],
            }],
        )
        encoded = inert_text(fact)
        self.assertEqual(report["markdown"].count(encoded), 1)
        self.assertNotIn("no work", report["markdown"].casefold())
        json.loads(json.dumps(report, ensure_ascii=False))

    def test_project_objectives_are_sorted_deduped_and_looked_up(self) -> None:
        first = self.stack.add_objective("Ship the weekly gate", "2030-Q2")
        second = self.stack.add_objective("Keep the digest exact", "2030-Q2")
        linked = self.stack.add_task(
            "Roll up the week",
            objective_ids=[second["id"], first["id"], first["id"]],
        )
        self.add_entry(
            "review.entry.weekly.0015",
            task_id=linked["id"],
            done=["Rolled up"],
            next=[],
            blockers=[],
        )
        report = preview(self.real_projection())
        line = "Objectives: {} — {}; {} — {}".format(
            inert_text(first["id"]),
            inert_text("Ship the weekly gate"),
            inert_text(second["id"]),
            inert_text("Keep the digest exact"),
        )
        self.assertIn(line, report["markdown"])
        self.assertIn("Duration:", report["markdown"])

    def test_same_text_in_another_task_or_field_is_preserved(self) -> None:
        fact = "Shared fact"
        other = self.stack.add_task("Second boundary")
        self.add_entry("review.entry.weekly.0003", done=[fact], next=[fact], blockers=[])
        self.add_entry(
            "review.entry.weekly.0004",
            task_id=other["id"],
            done=[fact],
            next=[],
            blockers=[],
        )
        report = preview(self.real_projection())
        self.assertEqual(report["provenance"]["task_ids"], [self.task["id"], other["id"]])
        self.assertEqual(report["markdown"].count(inert_text(fact)), 3)
        self.assertLess(
            report["markdown"].index(inert_text(self.task["id"])),
            report["markdown"].index(inert_text(other["id"])),
        )

    def test_coverage_lists_exactly_seven_days(self) -> None:
        self.add_entry("review.entry.weekly.0005", date=START, done=["Early"], next=[], blockers=[])
        self.add_entry("review.entry.weekly.0006", date=END, done=["Late"], next=[], blockers=[])
        report = preview(self.real_projection())
        coverage = report["provenance"]["coverage"]
        self.assertEqual(coverage["record_dates"], [START, END])
        self.assertEqual(coverage["no_record_dates"], WEEK[1:-1])
        self.assertEqual(sorted(coverage["record_dates"] + coverage["no_record_dates"]), WEEK)
        for date in WEEK:
            expected = "Records." if date in {START, END} else "No records."
            self.assertIn("- {}: {}".format(date, expected), report["markdown"])

    def test_empty_week_says_no_records_never_no_work(self) -> None:
        projection = self.real_projection()
        self.assertEqual(projection["weekly"]["projects"], [])
        report = preview(projection)
        self.assertEqual(report["absence"], "no records")
        self.assertEqual(report["provenance"]["task_ids"], [])
        self.assertEqual(report["provenance"]["sources"], [])
        self.assertEqual(report["provenance"]["coverage"]["record_dates"], [])
        self.assertEqual(report["provenance"]["coverage"]["no_record_dates"], WEEK)
        self.assertIn("No records.", report["markdown"])
        self.assertNotIn("## Projects", report["markdown"])
        self.assertNotIn("no work", report["markdown"].casefold())

    def test_duration_only_empty_lists_count_as_records(self) -> None:
        worklog = self.stack.store.load("worklog.json")
        worklog["days"] = {
            END: {
                "entries": [{
                    "task_id": self.task["id"],
                    "task": self.task["title"],
                    "done": [],
                    "next": [],
                    "blockers": [],
                    "duration_seconds": 60,
                }]
            }
        }
        self.stack.store.save("worklog.json", worklog)
        report = preview(self.real_projection())
        self.assertIsNone(report["absence"])
        self.assertEqual(report["provenance"]["coverage"]["record_dates"], [END])
        self.assertIn("Duration: 60s", report["markdown"])
        self.assertIn("- {}: Records.".format(END), report["markdown"])
        self.assertNotIn("\nDone\n", report["markdown"])
        self.assertNotIn("no work", report["markdown"].casefold())

    def test_superseded_entry_is_absent(self) -> None:
        self.add_entry("review.entry.weekly.0007", origin=ATTRIBUTED)
        before = self.real_projection()
        self.assertEqual(len(before["weekly"]["projects"]), 1)
        checkpoint = self.stack.list_checkpoint_audit()["entries"][-1]["checkpoint_id"]
        body = {
            "state": "superseded",
            "revision": 0,
            "reason": {"code": "incorrect", "explanation": "because"},
        }
        self.stack.apply_checkpoint_transition_v1(
            checkpoint,
            body,
            "review.transition.weekly.0001",
            path="/api/v1/review/checkpoints/{}/transitions".format(checkpoint),
            request_digest=canonical_digest(body),
            origin=ATTRIBUTED,
        )
        after = self.real_projection()
        self.assertEqual(after["weekly"]["projects"], [])
        report = preview(after)
        self.assertEqual(report["absence"], "no records")
        self.assertEqual(report["provenance"]["sources"], [])
        self.assertNotIn("Closed one gate", report["markdown"])

    def test_malicious_markup_is_inert_literal_text(self) -> None:
        payload = "[pwn](javascript:alert(1)) <script>x</script> ![img](http://evil.example/x)"
        self.add_entry("review.entry.weekly.0008", done=[payload], next=[], blockers=[])
        report = preview(self.real_projection())
        escaped = inert_text(payload)
        self.assertIn(escaped, report["markdown"])
        self.assertNotIn("[pwn](javascript:alert(1))", report["markdown"])
        self.assertNotIn("<script>", report["markdown"])
        self.assertNotIn("![img](", report["markdown"])

    def test_same_inputs_are_byte_identical_and_projects_sort_by_task_id(self) -> None:
        other = self.stack.add_task("Earlier title")
        self.add_entry("review.entry.weekly.0009", done=["First"], next=[], blockers=[])
        self.add_entry(
            "review.entry.weekly.0010",
            task_id=other["id"],
            done=["Second"],
            next=[],
            blockers=[],
        )
        projection = self.real_projection()
        projection["weekly"]["projects"] = list(reversed(projection["weekly"]["projects"]))
        first = preview(projection)
        second = preview(projection)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(first["provenance"]["task_ids"], [self.task["id"], other["id"]])
        self.assertLess(
            first["markdown"].index(inert_text(self.task["id"])),
            first["markdown"].index(inert_text(other["id"])),
        )

    def test_source_store_documents_are_unchanged(self) -> None:
        self.add_entry("review.entry.weekly.0011")
        before = self.store_bytes()
        preview(self.real_projection())
        self.assertEqual(self.store_bytes(), before)

    def test_prior_generated_preview_keys_are_ignored(self) -> None:
        self.add_entry("review.entry.weekly.0012")
        projection = self.real_projection()
        projection["template"] = TEMPLATE_WEEKLY_V1
        projection["period"] = {"kind": "week"}
        projection["generated_at"] = "1999-01-01T00:00:00Z"
        projection["absence"] = "no records"
        projection["provenance"] = {"invented": True}
        projection["markdown"] = "# Weekly review invented\n\nNo work.\n"
        projection["extra"] = 1
        report = preview(projection)
        self.assertEqual(
            report["provenance"]["ignored_keys"],
            ["absence", "extra", "generated_at", "markdown", "period", "provenance", "template"],
        )
        self.assertNotEqual(report["markdown"], projection["markdown"])
        self.assertNotIn("invented", report["markdown"])
        self.assertNotIn("no work", report["markdown"].casefold())

    def test_unsupported_template_and_bad_date_and_timestamp_are_refused(self) -> None:
        projection = self.real_projection()
        self.assertEqual(self.refuse(projection=projection, template="daily-v1").details["field"], "template")
        self.assertEqual(self.refuse(projection=projection, end_date="2026-13-01").details["field"], "end_date")
        self.assertEqual(self.refuse(projection=projection, generated_at="tonight").details["field"], "generated_at")

    def test_mismatched_day_and_malformed_projection_are_refused(self) -> None:
        mismatch = self.refuse(end_date="2026-08-31")
        self.assertEqual(mismatch.details["field"], "date")
        with self.assertRaises(WeeklyReportPreviewError) as malformed:
            preview_weekly_report(
                projection=["not", "an", "object"],
                end_date=END,
                template=TEMPLATE_WEEKLY_V1,
                generated_at=GENERATED_AT,
            )
        self.assertEqual(malformed.exception.details["field"], "projection")

    def test_non_json_nan_surrogate_and_deep_projection_are_refused(self) -> None:
        projection = self.real_projection()
        projection["stamp"] = object()
        self.assertEqual(self.refuse(projection=projection).details["field"], "projection")

        projection = self.real_projection()
        projection["weekly"]["bad"] = "\ud800"
        self.assertEqual(self.refuse(projection=projection).details["field"], "projection")

        projection = self.real_projection()
        cursor: dict = projection
        for _ in range(4000):
            nested: dict = {}
            cursor["n"] = nested
            cursor = nested
        self.assertEqual(self.refuse(projection=projection).details["field"], "projection")

        projection = self.real_projection()
        projection["weekly"]["bad"] = float("nan")
        self.assertEqual(self.refuse(projection=projection).details["field"], "projection")
        projection = self.real_projection()
        projection["weekly"]["bad"] = float("inf")
        self.assertEqual(self.refuse(projection=projection).details["field"], "projection")

    def test_duplicate_task_and_out_of_range_or_duplicate_dates_are_refused(self) -> None:
        self.add_entry("review.entry.weekly.0013")
        projection = self.real_projection()
        project = dict(projection["weekly"]["projects"][0])
        projection["weekly"]["projects"].append(project)
        duplicate = self.refuse(projection=projection)
        self.assertEqual(duplicate.details["field"], "task_id")

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["dates"] = [END, "2026-01-01"]
        out_of_range = self.refuse(projection=projection)
        self.assertEqual(out_of_range.details["field"], "dates")

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["dates"] = [END, END]
        repeated = self.refuse(projection=projection)
        self.assertEqual(repeated.details["field"], "dates")

    def test_caps_and_output_limit_are_refused_without_truncation(self) -> None:
        self.add_entry("review.entry.weekly.0014")
        projection = self.real_projection()
        seed = dict(projection["weekly"]["projects"][0])
        projection["weekly"]["projects"] = [
            {**seed, "task_id": "T-{:04d}".format(index), "dates": [END]}
            for index in range(MAX_PROJECTS + 1)
        ]
        too_many = self.refuse(projection=projection)
        self.assertEqual(too_many.details["field"], "projects")
        self.assertEqual(too_many.details["limit"], MAX_PROJECTS)

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["done"] = ["x" * (MAX_ITEM_CHARS + 1)]
        item = self.refuse(projection=projection)
        self.assertEqual(item.details["field"], "item")

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["next"] = ["n{}".format(index) for index in range(MAX_ITEMS_PER_FIELD + 1)]
        items = self.refuse(projection=projection)
        self.assertEqual(items.details["field"], "next")

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["dates"] = WEEK + ["2026-08-23"]
        dates = self.refuse(projection=projection)
        self.assertEqual(dates.details["field"], "dates")
        self.assertEqual(dates.details["limit"], MAX_DATES_PER_PROJECT)

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["duration_seconds"] = True
        duration = self.refuse(projection=projection)
        self.assertEqual(duration.details["field"], "duration_seconds")

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["objective_ids"] = [
            "O-{:04d}".format(index) for index in range(MAX_OBJECTIVES_PER_PROJECT + 1)
        ]
        objectives = self.refuse(projection=projection)
        self.assertEqual(objectives.details["field"], "objective_ids")
        self.assertEqual(objectives.details["limit"], MAX_OBJECTIVES_PER_PROJECT)

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["done"] = [
            "{:03d}{}".format(index, "!" * 900) for index in range(80)
        ]
        oversized = self.refuse(projection=projection)
        self.assertEqual(oversized.details["field"], "markdown")
        self.assertEqual(oversized.details["limit"], MAX_MARKDOWN_CHARS)

    def test_broken_range_shape_is_refused(self) -> None:
        projection = self.real_projection()
        projection["weekly"]["range"]["days"] = 6
        self.assertEqual(self.refuse(projection=projection).details["field"], "range")
        projection = self.real_projection()
        projection["weekly"]["range"]["days"] = True
        self.assertEqual(self.refuse(projection=projection).details["field"], "range")
        projection = self.real_projection()
        projection["weekly"]["range"]["start"] = "2026-08-25"
        self.assertEqual(self.refuse(projection=projection).details["field"], "range")

    def test_empty_project_dates_are_refused_instead_of_inconsistent_absence(self) -> None:
        self.add_entry("review.entry.weekly.0016")
        projection = self.real_projection()
        projection["weekly"]["projects"][0]["dates"] = []
        error = self.refuse(projection=projection)
        self.assertEqual(error.details["field"], "dates")
        self.assertNotIn("absence", str(error.details))

    def test_unrepresentable_week_start_is_refused_and_final_civil_week_is_supported(self) -> None:
        earliest = {
            "day": {"date": "0001-01-01", "start_time": None, "entries": []},
            "weekly": {
                "range": {"start": "0001-01-01", "end": "0001-01-01", "days": 7},
                "objectives": [],
                "projects": [],
            },
        }
        error = self.refuse(projection=earliest, end_date="0001-01-01")
        self.assertEqual(error.details["field"], "range")
        self.assertIsInstance(error.__cause__, OverflowError)

        last_start = "9999-12-25"
        last_end = "9999-12-31"
        last_week = {
            "day": {"date": last_end, "start_time": None, "entries": []},
            "weekly": {
                "range": {"start": last_start, "end": last_end, "days": 7},
                "objectives": [],
                "projects": [{
                    "task_id": self.task["id"],
                    "task": self.task["title"],
                    "objective_ids": [],
                    "done": ["Closed the civil calendar"],
                    "next": [],
                    "blockers": [],
                    "dates": [last_end],
                    "duration_seconds": 1,
                }],
            },
        }
        report = preview(last_week, end_date=last_end)
        self.assertEqual(report["period"], {"kind": "week", "start": last_start, "end": last_end, "days": 7})
        self.assertEqual(report["provenance"]["coverage"]["record_dates"], [last_end])
        self.assertEqual(len(report["provenance"]["coverage"]["no_record_dates"]), 6)
        self.assertIn("- {}: Records.".format(last_end), report["markdown"])
        self.assertNotIn("10000", report["markdown"])

    def test_missing_aggregate_fields_are_refused_and_do_not_become_zero_duration(self) -> None:
        self.add_entry("review.entry.weekly.0017")
        projection = self.real_projection()
        del projection["weekly"]["objectives"]
        self.assertEqual(self.refuse(projection=projection).details["field"], "objectives")

        for field in ("objective_ids", "done", "next", "blockers", "dates", "duration_seconds"):
            projection = self.real_projection()
            del projection["weekly"]["projects"][0][field]
            error = self.refuse(projection=projection)
            self.assertEqual(error.details["field"], field, field)
            self.assertNotIn("Duration: 0s", str(error))

        projection = self.real_projection()
        projection["weekly"]["objectives"] = [
            {"id": "O-{:04d}".format(index), "objective": "cap"}
            for index in range(MAX_OBJECTIVES + 1)
        ]
        too_many = self.refuse(projection=projection)
        self.assertEqual(too_many.details["field"], "objectives")
        self.assertEqual(too_many.details["limit"], MAX_OBJECTIVES)

        projection = self.real_projection()
        projection["weekly"]["projects"][0]["duration_seconds"] = 0
        report = preview(projection)
        self.assertIn("Duration: 0s", report["markdown"])
        self.assertIsNone(report["absence"])
