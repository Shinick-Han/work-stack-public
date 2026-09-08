"""Oracles for the pure report read and list projections.

Fixtures are synthetic. Nothing here opens a store, a service, HTTP, a clock
or a live workspace, and nothing asserts that a public service is ready.
Expected key sets, the page size, the cursor encoding and the filter aliases
are restated by hand from the storage contract so a quiet change of meaning
fails the suite instead of agreeing with itself.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import hashlib
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from workstack.report_documents import ReportDocumentError, validate_reports_document
from workstack.report_queries import (
    ReportQueryError,
    list_report_documents,
    read_report_document,
)


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
OTHER_WORKSPACE = "1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d"
DIGEST = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64
NOW = "2026-09-06T12:00:00Z"
GENERATED = "2026-09-06T11:59:00Z"
MARKDOWN = "# body <script>alert(1)</script> | a | b |"
LEDGER_KEY = "report-key-0001"

LIST_ITEM_KEYS = {
    "uid", "template", "period", "state", "revision", "content_revision",
    "source_digest", "archived_from_state", "created_at", "updated_at",
}
LIST_ENVELOPE_KEYS = {"workspace_uid", "reports", "omitted_count", "cursor"}
READ_KEYS = LIST_ITEM_KEYS | {"revisions", "source_stale"}
PAGE_SIZE = 50
CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 256


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def uid(index: int) -> str:
    return "{:08x}-abcd-4000-8000-000000000000".format(index)


def content(**overrides: Any) -> dict[str, Any]:
    entry = {
        "content_revision": 1,
        "document_revision": 1,
        "markdown": MARKDOWN,
        "authored_at": NOW,
        "note": None,
    }
    entry.update(overrides)
    return entry


def report(**overrides: Any) -> dict[str, Any]:
    value = {
        "uid": uid(1),
        "workspace_uid": WORKSPACE,
        "template": "daily-v1",
        "period": {"kind": "day", "date": "2026-09-06"},
        "source_digest": DIGEST,
        "source_generated_at": GENERATED,
        "state": "draft",
        "revision": 1,
        "archived_from_state": None,
        "archived_at": None,
        "archive_note": None,
        "revisions": [content()],
        "created_at": NOW,
        "updated_at": NOW,
    }
    value.update(overrides)
    return value


def receipt() -> dict[str, Any]:
    return {
        "key": LEDGER_KEY,
        "method": "POST",
        "path": "/api/v1/reports",
        "request_digest": DIGEST,
        "response_status": 201,
        "response_body": {"data": {"uid": uid(1)}, "meta": {"replayed": False}},
        "created_at": NOW,
    }


def document(
    reports: list[dict[str, Any]] | None = None,
    ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "reports": [] if reports is None else reports,
        "idempotency": [] if ledger is None else ledger,
    }


def day_at(index: int) -> str:
    return (dt.date(2026, 1, 1) + dt.timedelta(days=index)).isoformat()


def stamp_at(index: int) -> str:
    moment = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    moment = moment + dt.timedelta(seconds=index)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def numbered_report(index: int, **overrides: Any) -> dict[str, Any]:
    stamp = stamp_at(index)
    value = report(
        uid=uid(index),
        period={"kind": "day", "date": day_at(index)},
        created_at=stamp,
        updated_at=stamp,
    )
    value.update(overrides)
    return value


def many_reports(count: int) -> list[dict[str, Any]]:
    return [numbered_report(index) for index in range(1, count + 1)]


def binding(owner: str, state: str) -> str:
    material = json.dumps(
        {"state": state, "workspace_uid": owner},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def encode_cursor(
    owner: str,
    state: str,
    updated_at: str,
    report_uid: str,
    **overrides: Any,
) -> str:
    payload = {
        "binding": binding(owner, state),
        "uid": report_uid,
        "updated_at": updated_at,
        "v": CURSOR_VERSION,
    }
    payload.update(overrides)
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def listed(doc: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("workspace_uid", WORKSPACE)
    return list_report_documents(doc, **kwargs)


def read(doc: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("workspace_uid", WORKSPACE)
    kwargs.setdefault("report_uid", uid(1))
    kwargs.setdefault("current_source_digest", DIGEST)
    return read_report_document(doc, **kwargs)


def complexity(function: ast.AST) -> int:
    score = 1
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(0, len(node.values) - 1)
        elif isinstance(node, ast.Try):
            score += len(node.handlers) + int(bool(node.orelse))
        elif isinstance(node, ast.Match):
            score += max(0, len(node.cases) - 1)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(generator.ifs) for generator in node.generators)
    return score


class ReportQueryCase(unittest.TestCase):
    maxDiff = None

    @contextmanager
    def query_refused(self, code: str, field: str | None = None) -> Iterator[Any]:
        with self.assertRaises(ReportQueryError) as caught:
            yield caught
        self.assertEqual(caught.exception.code, code)
        if field is not None:
            self.assertEqual(caught.exception.field, field)

    @contextmanager
    def model_refused(self, code: str, field: str | None = None) -> Iterator[Any]:
        with self.assertRaises(ReportDocumentError) as caught:
            yield caught
        self.assertEqual(caught.exception.code, code)
        if field is not None:
            self.assertEqual(caught.exception.field, field)

    def assertContentFree(self, error: BaseException, *secrets: str) -> None:
        message = str(error)
        self.assertTrue(message and "\n" not in message)
        for secret in secrets:
            self.assertNotIn(secret, message)
            field = getattr(error, "field", None)
            if field is not None:
                self.assertNotIn(secret, field)

    def assertListItem(self, item: dict[str, Any]) -> None:
        self.assertEqual(set(item), LIST_ITEM_KEYS)
        self.assertIsInstance(item["period"], dict)
        self.assertEqual(set(item["period"]), {"kind", "date"})


class ImportClosureTest(ReportQueryCase):
    STDLIB = {
        "__future__", "base64", "binascii", "collections", "datetime",
        "hashlib", "json", "re", "typing",
    }
    FORBIDDEN_CALLS = (
        "datetime.now", "utcnow", "date.today", "time.time", "time.monotonic",
        "uuid4", "uuid1", "getrandbits", "open(", "os.environ", "Path(",
        "read_text", "write_text", "read_bytes", "write_bytes", "inert_text",
        "canonical_digest",
    )

    def closure(self, entry: str) -> tuple[set[str], set[str]]:
        product: set[str] = set()
        stdlib: set[str] = set()
        pending = [entry]
        while pending:
            name = pending.pop()
            if name in product:
                continue
            product.add(name)
            tree = ast.parse(self.source(name))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    stdlib.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        pending.append("workstack." + module if module else "workstack")
                    elif module.split(".")[0] == "workstack":
                        pending.append(module)
                    elif module:
                        stdlib.add(module.split(".")[0])
        return product, stdlib

    @staticmethod
    def source(name: str) -> str:
        return ROOT.joinpath(*name.split(".")).with_suffix(".py").read_text(
            encoding="utf-8"
        )

    def test_the_leaf_reaches_only_the_pure_report_modules(self) -> None:
        product, stdlib = self.closure("workstack.report_queries")
        self.assertEqual(
            product,
            {
                "workstack.report_queries",
                "workstack.report_documents",
                "workstack.report_json",
                "workstack.reporting",
            },
        )
        self.assertLessEqual(stdlib, self.STDLIB)

    def test_no_store_service_server_or_source_digest_helper_is_reachable(self) -> None:
        product, _ = self.closure("workstack.report_queries")
        for banned in ("store", "service", "server", "storage", "capture", "maintenance"):
            self.assertNotIn("workstack." + banned, product)

    def test_the_module_does_not_call_a_clock_identity_source_or_the_disk(self) -> None:
        body = self.source("workstack.report_queries")
        for call in self.FORBIDDEN_CALLS:
            with self.subTest(call=call):
                self.assertNotIn(call, body)

    def test_public_names_are_exactly_the_frozen_query_surface(self) -> None:
        from workstack import report_queries

        self.assertEqual(
            set(report_queries.__all__),
            {"ReportQueryError", "list_report_documents", "read_report_document"},
        )


class QualityBoundsTest(ReportQueryCase):
    def test_new_production_file_stays_inside_the_structural_caps(self) -> None:
        path = ROOT / "workstack" / "report_queries.py"
        source = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 800)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            self.assertLessEqual(length, 100, node.name)
            self.assertLessEqual(complexity(node), 15, node.name)


class ListProjectionTest(ReportQueryCase):
    def test_list_projects_exactly_the_contract_item_and_envelope(self) -> None:
        page = listed(document([report()]))
        self.assertEqual(set(page), LIST_ENVELOPE_KEYS)
        self.assertEqual(page["workspace_uid"], WORKSPACE)
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])
        self.assertEqual(len(page["reports"]), 1)
        item = page["reports"][0]
        self.assertListItem(item)
        self.assertEqual(item["uid"], uid(1))
        self.assertEqual(item["template"], "daily-v1")
        self.assertEqual(item["period"], {"kind": "day", "date": "2026-09-06"})
        self.assertEqual(item["state"], "draft")
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["content_revision"], 1)
        self.assertEqual(item["source_digest"], DIGEST)
        self.assertIsNone(item["archived_from_state"])
        self.assertEqual(item["created_at"], NOW)
        self.assertEqual(item["updated_at"], NOW)

    def test_content_revision_is_taken_from_the_last_history_entry(self) -> None:
        history = [
            content(),
            content(content_revision=2, document_revision=3, markdown="# two"),
            content(content_revision=3, document_revision=5, markdown="# three"),
        ]
        page = listed(document([report(revision=5, revisions=history)]))
        self.assertEqual(page["reports"][0]["content_revision"], 3)
        self.assertEqual(page["reports"][0]["revision"], 5)

    def test_archived_from_state_is_projected_when_archived(self) -> None:
        item = listed(
            document([
                report(
                    state="archived",
                    archived_from_state="finalized",
                    archived_at=NOW,
                    archive_note=None,
                )
            ]),
            state="archived",
        )["reports"][0]
        self.assertEqual(item["state"], "archived")
        self.assertEqual(item["archived_from_state"], "finalized")

    def test_an_empty_document_is_an_empty_last_page(self) -> None:
        page = listed(document())
        self.assertEqual(page["reports"], [])
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])


class ListOrderAndTiesTest(ReportQueryCase):
    def test_order_is_updated_at_desc_then_uid_asc_including_stable_ties(self) -> None:
        reports = [
            numbered_report(3, updated_at=NOW, created_at=NOW),
            numbered_report(1, updated_at=NOW, created_at=NOW),
            numbered_report(2, updated_at=NOW, created_at=NOW),
            numbered_report(4, updated_at="2026-09-06T13:00:00Z", created_at=NOW),
        ]
        page = listed(document(reports), state="all")
        self.assertEqual(
            [item["uid"] for item in page["reports"]],
            [uid(4), uid(1), uid(2), uid(3)],
        )

    def test_insertion_order_does_not_change_tied_uid_order(self) -> None:
        first = listed(document([
            numbered_report(5, updated_at=NOW, created_at=NOW),
            numbered_report(1, updated_at=NOW, created_at=NOW),
        ]))
        second = listed(document([
            numbered_report(1, updated_at=NOW, created_at=NOW),
            numbered_report(5, updated_at=NOW, created_at=NOW),
        ]))
        self.assertEqual(
            [item["uid"] for item in first["reports"]],
            [item["uid"] for item in second["reports"]],
        )
        self.assertEqual([item["uid"] for item in first["reports"]], [uid(1), uid(5)])


class ListPagingTest(ReportQueryCase):
    def test_more_than_fifty_documents_page_without_duplicates(self) -> None:
        doc = document(many_reports(55))
        first = listed(doc)
        self.assertEqual(len(first["reports"]), PAGE_SIZE)
        self.assertEqual(first["omitted_count"], 5)
        self.assertIsInstance(first["cursor"], str)
        self.assertEqual(type(first["omitted_count"]), int)
        second = listed(doc, cursor=first["cursor"])
        first_ids = [item["uid"] for item in first["reports"]]
        second_ids = [item["uid"] for item in second["reports"]]
        self.assertEqual(len(second_ids), 5)
        self.assertEqual(second["omitted_count"], 0)
        self.assertIsNone(second["cursor"])
        self.assertEqual(len(set(first_ids) & set(second_ids)), 0)
        self.assertEqual(set(first_ids + second_ids), {uid(index) for index in range(1, 56)})
        expected = [uid(index) for index in range(55, 0, -1)]
        self.assertEqual(first_ids + second_ids, expected)

    def test_issued_cursor_matches_the_canonical_oracle_encoding(self) -> None:
        doc = document(many_reports(51))
        page = listed(doc)
        anchor = page["reports"][-1]
        self.assertEqual(
            page["cursor"],
            encode_cursor(WORKSPACE, "active", anchor["updated_at"], anchor["uid"]),
        )
        again = listed(doc)
        self.assertEqual(page["cursor"], again["cursor"])

    def test_exactly_fifty_documents_is_a_last_page(self) -> None:
        page = listed(document(many_reports(50)))
        self.assertEqual(len(page["reports"]), 50)
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])


class ListAliasesTest(ReportQueryCase):
    def mixed(self) -> dict[str, Any]:
        return document([
            numbered_report(1, state="draft"),
            numbered_report(2, state="finalized"),
            numbered_report(
                3,
                state="archived",
                archived_from_state="draft",
                archived_at=stamp_at(3),
            ),
        ])

    def test_active_selects_draft_and_finalized_and_omits_archived(self) -> None:
        page = listed(self.mixed(), state="active")
        self.assertEqual([item["state"] for item in page["reports"]], ["finalized", "draft"])

    def test_archived_selects_only_archived(self) -> None:
        page = listed(self.mixed(), state="archived")
        self.assertEqual([item["uid"] for item in page["reports"]], [uid(3)])
        self.assertEqual(page["reports"][0]["state"], "archived")

    def test_all_selects_every_stored_state(self) -> None:
        page = listed(self.mixed(), state="all")
        self.assertEqual(
            [item["state"] for item in page["reports"]],
            ["archived", "finalized", "draft"],
        )

    def test_unknown_state_alias_is_invalid_query(self) -> None:
        with self.query_refused("invalid_query", "state") as caught:
            listed(document([report()]), state="draft")
        self.assertContentFree(caught.exception, "draft", WORKSPACE)


class CursorBindingTest(ReportQueryCase):
    def test_a_cursor_from_another_workspace_is_refused(self) -> None:
        left = document(many_reports(51))
        token = listed(left)["cursor"]
        right_reports = []
        for index in range(1, 52):
            item = numbered_report(index)
            item["workspace_uid"] = OTHER_WORKSPACE
            right_reports.append(item)
        with self.query_refused("report_cursor_invalid", "cursor") as caught:
            listed(document(right_reports), workspace_uid=OTHER_WORKSPACE, cursor=token)
        self.assertContentFree(caught.exception, WORKSPACE, OTHER_WORKSPACE, token)

    def test_a_cursor_from_another_filter_is_refused(self) -> None:
        doc = document(many_reports(51) + [
            numbered_report(
                60,
                state="archived",
                archived_from_state="draft",
                archived_at=stamp_at(60),
            )
        ])
        token = listed(doc, state="active")["cursor"]
        with self.query_refused("report_cursor_invalid", "cursor") as caught:
            listed(doc, state="all", cursor=token)
        self.assertContentFree(caught.exception, token)
        with self.query_refused("report_cursor_invalid", "cursor"):
            listed(doc, state="archived", cursor=token)

    def test_the_cursor_does_not_carry_the_workspace_uid_in_plaintext(self) -> None:
        token = listed(document(many_reports(51)))["cursor"]
        self.assertIsNotNone(token)
        self.assertNotIn(WORKSPACE, token)
        self.assertNotIn("active", token)


class CursorAnchorTest(ReportQueryCase):
    def test_a_deleted_anchor_is_refused_rather_than_restarted(self) -> None:
        reports = many_reports(51)
        first = listed(document(reports))
        token = first["cursor"]
        anchor_uid = first["reports"][-1]["uid"]
        remaining = [item for item in reports if item["uid"] != anchor_uid]
        with self.query_refused("report_cursor_invalid", "cursor") as caught:
            listed(document(remaining), cursor=token)
        self.assertContentFree(caught.exception, anchor_uid, token)
        restarted = listed(document(remaining))
        self.assertEqual(restarted["reports"][0]["uid"], uid(51))
        self.assertEqual(len(restarted["reports"]), 50)

    def test_an_updated_anchor_is_refused_rather_than_restarted(self) -> None:
        reports = many_reports(51)
        first = listed(document(reports))
        token = first["cursor"]
        anchor_uid = first["reports"][-1]["uid"]
        for item in reports:
            if item["uid"] == anchor_uid:
                item["updated_at"] = "2026-09-06T18:00:00Z"
        with self.query_refused("report_cursor_invalid", "cursor") as caught:
            listed(document(reports), cursor=token)
        self.assertContentFree(caught.exception, "2026-09-06T18:00:00Z", token)

    def test_a_forged_anchor_uid_that_was_never_on_the_page_is_refused(self) -> None:
        doc = document(many_reports(51))
        token = encode_cursor(WORKSPACE, "active", stamp_at(99), uid(99))
        with self.query_refused("report_cursor_invalid", "cursor"):
            listed(doc, cursor=token)


class CursorMalformedTest(ReportQueryCase):
    def issued(self) -> str:
        token = listed(document(many_reports(51)))["cursor"]
        self.assertIsInstance(token, str)
        return token

    def test_malformed_and_noncanonical_cursors_are_refused(self) -> None:
        issued = self.issued()
        payload = {
            "binding": binding(WORKSPACE, "active"),
            "uid": uid(1),
            "updated_at": stamp_at(1),
            "v": CURSOR_VERSION,
        }
        spaced = base64.urlsafe_b64encode(
            json.dumps(payload, indent=2).encode("utf-8")
        ).decode("ascii").rstrip("=")
        unsorted = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "v": CURSOR_VERSION,
                    "uid": uid(1),
                    "updated_at": stamp_at(1),
                    "binding": binding(WORKSPACE, "active"),
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        wrong_version = encode_cursor(
            WORKSPACE, "active", stamp_at(1), uid(1), v=2
        )
        cases = (
            issued + "=",
            issued + "==",
            spaced,
            unsorted,
            wrong_version,
            "E-000001",
            "%%%",
            "abc",
            "A" * 80,
        )
        for token in cases:
            with self.subTest(token=token[:40]):
                with self.query_refused("report_cursor_invalid", "cursor") as caught:
                    listed(document(many_reports(51)), cursor=token)
                self.assertContentFree(caught.exception, token, WORKSPACE, uid(1))

    def test_an_oversized_cursor_is_refused(self) -> None:
        token = "A" * (MAX_CURSOR_CHARS + 1)
        with self.query_refused("report_cursor_invalid", "cursor") as caught:
            listed(document(many_reports(51)), cursor=token)
        self.assertContentFree(caught.exception, token[:32], WORKSPACE)

    def test_a_short_cursor_is_refused(self) -> None:
        with self.query_refused("report_cursor_invalid", "cursor"):
            listed(document([report()]), cursor="A" * 32)


class QueryTypesTest(ReportQueryCase):
    def test_bool_and_other_limit_types_are_invalid_query(self) -> None:
        doc = document([report()])
        for limit in (True, False, "50", 50.0, 1, 0, 51, 49, None):
            with self.subTest(limit=limit):
                with self.query_refused("invalid_query", "limit") as caught:
                    listed(doc, limit=limit)
                self.assertContentFree(caught.exception, WORKSPACE)

    def test_wrong_types_for_state_cursor_and_identities_are_invalid_query(self) -> None:
        doc = document([report()])
        with self.query_refused("invalid_query", "state"):
            listed(doc, state=True)
        with self.query_refused("invalid_query", "state"):
            listed(doc, state=None)
        with self.query_refused("invalid_query", "cursor"):
            listed(doc, cursor=1)
        with self.query_refused("invalid_query", "cursor"):
            listed(doc, cursor=b"AAAA")
        with self.query_refused("invalid_query", "workspace_uid"):
            listed(doc, workspace_uid=1)
        with self.query_refused("invalid_query", "workspace_uid"):
            listed(doc, workspace_uid=WORKSPACE.upper())
        with self.query_refused("invalid_query", "report_uid"):
            read(doc, report_uid=1)
        with self.query_refused("invalid_query", "report_uid"):
            read(doc, report_uid=uid(1).upper())
        with self.query_refused("invalid_query", "report_uid"):
            read(doc, report_uid="00000001-abcd-7000-8000-000000000000")


class ReadProjectionTest(ReportQueryCase):
    def test_read_returns_the_list_item_plus_ordered_history_and_staleness(self) -> None:
        history = [
            content(markdown="# one"),
            content(content_revision=2, document_revision=2, markdown=MARKDOWN),
        ]
        item = read(document([report(revision=2, revisions=history)]))
        self.assertEqual(set(item), READ_KEYS)
        self.assertListItem({key: item[key] for key in LIST_ITEM_KEYS})
        self.assertEqual(item["revisions"], history)
        self.assertIs(item["source_stale"], False)
        self.assertEqual(item["revisions"][1]["markdown"], MARKDOWN)

    def test_source_stale_is_the_inequality_against_the_supplied_digest(self) -> None:
        doc = document([report()])
        self.assertIs(read(doc, current_source_digest=DIGEST)["source_stale"], False)
        self.assertIs(read(doc, current_source_digest=OTHER_DIGEST)["source_stale"], True)

    def test_authored_markdown_is_not_rewritten_or_turned_into_html(self) -> None:
        item = read(document([report()]))
        self.assertEqual(item["revisions"][0]["markdown"], MARKDOWN)
        self.assertNotIn("<p>", item["revisions"][0]["markdown"])
        self.assertNotIn("&lt;", item["revisions"][0]["markdown"])


class ReadRefusalTest(ReportQueryCase):
    def test_unknown_uid_is_report_not_found(self) -> None:
        with self.model_refused("report_not_found", "report_uid") as caught:
            read(document([report()]), report_uid=uid(2), current_source_digest=DIGEST)
        self.assertContentFree(caught.exception, uid(2), DIGEST, MARKDOWN)

    def test_digest_shape_is_validated_even_when_the_target_is_absent(self) -> None:
        doc = document([report()])
        for digest in (
            True,
            1,
            None,
            "sha256:" + "0" * 63,
            "SHA256:" + "0" * 64,
            "sha256:" + "G" * 64,
            DIGEST.upper(),
            "0" * 64,
        ):
            with self.subTest(digest=digest):
                with self.model_refused("report_body_invalid", "source_digest") as caught:
                    read(doc, report_uid=uid(9), current_source_digest=digest)
                self.assertContentFree(
                    caught.exception, uid(9), MARKDOWN, LEDGER_KEY, str(digest)
                )

    def test_matching_digest_is_still_shape_checked(self) -> None:
        item = read(document([report()]), current_source_digest=DIGEST)
        self.assertIs(item["source_stale"], False)
        with self.model_refused("report_body_invalid", "source_digest"):
            read(document([report()]), current_source_digest=DIGEST + "0")

    def test_a_bad_document_is_refused_by_the_real_validator(self) -> None:
        with self.model_refused("report_body_invalid", "document"):
            listed({"version": 2, "reports": [], "idempotency": []})
        with self.model_refused("report_body_invalid", "document"):
            read({"version": 2, "reports": [], "idempotency": []})
        with self.model_refused("report_body_invalid", "workspace_uid"):
            listed(document([report()]), workspace_uid=OTHER_WORKSPACE)

    def test_validate_reports_document_is_the_admission_path(self) -> None:
        admitted = validate_reports_document(document([report()]), workspace_uid=WORKSPACE)
        page = listed(admitted)
        self.assertEqual(page["reports"][0]["uid"], uid(1))


class DetachedIOTest(ReportQueryCase):
    def test_list_and_read_do_not_share_or_mutate_caller_trees(self) -> None:
        history = [content(markdown=MARKDOWN)]
        original = document([report(revisions=history)], ledger=[receipt()])
        before = canonical(original)
        page = listed(original)
        item = read(original)
        page["reports"][0]["period"]["date"] = "1999-01-01"
        page["reports"][0]["uid"] = "mutated"
        item["revisions"][0]["markdown"] = "rewritten"
        item["period"]["date"] = "1999-01-01"
        original["reports"][0]["revisions"][0]["markdown"] = "caller-changed"
        original["reports"][0]["period"]["date"] = "1999-12-31"
        self.assertEqual(canonical(original) != before, True)
        reread = read(
            document([report(revisions=[content(markdown=MARKDOWN)])], ledger=[receipt()])
        )
        self.assertEqual(reread["revisions"][0]["markdown"], MARKDOWN)
        self.assertEqual(reread["period"]["date"], "2026-09-06")
        fresh = listed(document([report()]))
        self.assertEqual(fresh["reports"][0]["period"]["date"], "2026-09-06")
        self.assertEqual(page["reports"][0]["period"]["date"], "1999-01-01")
        self.assertEqual(item["revisions"][0]["markdown"], "rewritten")

    def test_inputs_are_not_mutated_by_a_successful_query(self) -> None:
        original = document(many_reports(51), ledger=[receipt()])
        before = canonical(original)
        listed(original)
        read(original, report_uid=uid(1))
        self.assertEqual(canonical(original), before)


class LeakageTest(ReportQueryCase):
    def test_list_does_not_leak_markdown_history_ledger_or_source_stale(self) -> None:
        page = listed(document(many_reports(51), ledger=[receipt()]))
        dumped = canonical(page)
        self.assertNotIn("markdown", dumped)
        self.assertNotIn("revisions", dumped)
        self.assertNotIn("idempotency", dumped)
        self.assertNotIn(LEDGER_KEY, dumped)
        self.assertNotIn("source_stale", dumped)
        self.assertNotIn("source_generated_at", dumped)
        self.assertNotIn("archived_at", dumped)
        self.assertNotIn("archive_note", dumped)
        self.assertNotIn(MARKDOWN, dumped)
        self.assertNotIn("<html", dumped)
        self.assertIn("source_digest", dumped)

    def test_read_does_not_leak_the_ledger_or_compute_a_source(self) -> None:
        item = read(document([report()], ledger=[receipt()]), current_source_digest=OTHER_DIGEST)
        dumped = canonical(item)
        self.assertNotIn(LEDGER_KEY, dumped)
        self.assertNotIn("idempotency", dumped)
        self.assertNotIn("request_digest", dumped)
        self.assertIn(MARKDOWN, dumped)
        self.assertIs(item["source_stale"], True)

    def test_refusals_do_not_quote_raw_inputs(self) -> None:
        secrets = (WORKSPACE, uid(1), DIGEST, MARKDOWN, LEDGER_KEY, OTHER_DIGEST)
        with self.query_refused("invalid_query", "limit") as caught:
            listed(document([report()], ledger=[receipt()]), limit=True)
        self.assertContentFree(caught.exception, *secrets)
        with self.model_refused("report_not_found", "report_uid") as caught:
            read(document([report()], ledger=[receipt()]), report_uid=uid(8))
        self.assertContentFree(caught.exception, *secrets, uid(8))


if __name__ == "__main__":
    unittest.main()
