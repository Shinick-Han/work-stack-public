"""Oracles for the pure report-document model.

Every fixture here is synthetic: the UUIDs are counted, the paths are the API
routes the contract names, and the markdown is a heading. Nothing reads a home
directory, a live store or a credential, and no test creates `reports.json`.

The suite is written against the accepted contract rather than against the
implementation: expected key sets, the transition table and the serialization
used for every byte cap are all restated here by hand, so a change to the
module that quietly changes its meaning fails instead of agreeing with itself.
"""

from __future__ import annotations

import ast
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from workstack.report_documents import (
    MAX_LEDGER_RECORD_BYTES,
    MAX_MARKDOWN_CHARS,
    MAX_REPORTS_BYTES,
    MAX_REPORT_CONTENT_REVISIONS,
    MAX_REPORT_DOCUMENTS,
    MAX_REPORT_DOCUMENT_REVISION,
    MAX_REPORT_JSON_DEPTH,
    MAX_REPORT_NOTE_BYTES,
    MAX_REPORT_NOTE_CHARS,
    REPORTS_DOCUMENT_VERSION,
    REPORT_LEDGER_MAX_RECORDS,
    REPORT_LEDGER_RETENTION_DAYS,
    TEMPLATE_DAILY_V1,
    ReportDocumentError,
    append_report_receipt,
    normalize_report_request,
    plan_report_mutation,
    prepare_report_replay,
    validate_reports_document,
)


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
OTHER_WORKSPACE = "1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d"
DIGEST = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64
NOW = "2026-09-06T12:00:00Z"
LATER = "2026-09-06T13:00:00Z"
GENERATED = "2026-09-06T11:59:00Z"
ROUTE = "/api/v1/reports"
KEY = "report-key-0001"

# Restated by hand from the contract, not imported from the module.
TOP_KEYS = {"version", "reports", "idempotency"}
REPORT_KEYS = {
    "uid", "workspace_uid", "template", "period", "source_digest",
    "source_generated_at", "state", "revision", "archived_from_state",
    "archived_at", "archive_note", "revisions", "created_at", "updated_at",
}
CONTENT_KEYS = {
    "content_revision", "document_revision", "markdown", "authored_at", "note",
}
LEDGER_KEYS = {
    "key", "method", "path", "request_digest", "response_status",
    "response_body", "created_at",
}
PLAN_KEYS = {"document", "report", "content_entry", "reopened"}
REPLAY_KEYS = {"document", "replay"}


def canonical(value: Any) -> bytes:
    """The serialization the contract measures every byte cap against."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def uid(index: int) -> str:
    """Synthetic canonical UUIDv4s. The `abcd` group keeps them case-sensitive."""

    return "{:08x}-abcd-4000-8000-000000000000".format(index)


def content(**overrides: Any) -> dict[str, Any]:
    entry = {
        "content_revision": 1,
        "document_revision": 1,
        "markdown": "# body",
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


def response_body(**data: Any) -> dict[str, Any]:
    return {"data": dict(data) or {"uid": uid(1)}, "meta": {"replayed": False}}


def receipt(**overrides: Any) -> dict[str, Any]:
    value = {
        "key": KEY,
        "method": "POST",
        "path": ROUTE,
        "request_digest": DIGEST,
        "response_status": 201,
        "response_body": response_body(),
        "created_at": NOW,
    }
    value.update(overrides)
    return value


def document(
    reports: list[dict[str, Any]] | None = None,
    ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "reports": [] if reports is None else reports,
        "idempotency": [] if ledger is None else ledger,
    }


def create_request(**overrides: Any) -> dict[str, Any]:
    value = {
        "workspace_uid": WORKSPACE,
        "template": "daily-v1",
        "period": {"kind": "day", "date": "2026-09-06"},
        "source_digest": DIGEST,
        "source_generated_at": GENERATED,
        "markdown": "# first",
    }
    value.update(overrides)
    return value


def nest(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"n": value}
    return value


def stamp(day: int, hour: int = 12) -> str:
    return "2026-09-{:02d}T{:02d}:00:00Z".format(day, hour)


class ReportCase(unittest.TestCase):
    """Shared refusal assertions: a code, an allowlisted field, no content."""

    maxDiff = None

    @contextmanager
    def refused(self, code: str, field: str | None = None) -> Iterator[Any]:
        with self.assertRaises(ReportDocumentError) as caught:
            yield caught
        self.assertEqual(caught.exception.code, code)
        if field is not None:
            self.assertEqual(caught.exception.field, field)

    def assertContentFree(self, error: ReportDocumentError, *secrets: str) -> None:
        message = str(error)
        self.assertTrue(message and "\n" not in message)
        for secret in secrets:
            self.assertNotIn(secret, message)
            if error.field is not None:
                self.assertNotIn(secret, error.field)

    def plan(self, doc: dict[str, Any], name: str, body: dict[str, Any], **kwargs: Any):
        kwargs.setdefault("now", LATER)
        kwargs.setdefault("report_uid", uid(1))
        return plan_report_mutation(doc, name, body, **kwargs)

    def created(self, **overrides: Any) -> dict[str, Any]:
        plan = plan_report_mutation(
            document(),
            "create",
            create_request(**overrides),
            report_uid=uid(1),
            current_source_digest=DIGEST,
            now=NOW,
        )
        return plan["document"]


# 1. The module is pure: nothing but stdlib and one other pure product module.


class ImportClosureTest(ReportCase):
    STDLIB = {"__future__", "datetime", "json", "re", "typing", "collections"}
    # Names that would mean this module reached for a clock, an identity source
    # or the filesystem. Searched as source text so an indirect call is caught.
    FORBIDDEN_CALLS = (
        "datetime.now", "utcnow", "date.today", "time.time", "time.monotonic",
        "uuid4", "uuid1", "getrandbits", "open(", "os.environ", "Path(",
        "read_text", "write_text", "read_bytes", "write_bytes",
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

    def test_the_closure_is_two_new_modules_plus_the_pure_preview_module(self) -> None:
        product, stdlib = self.closure("workstack.report_documents")
        self.assertEqual(
            product,
            {
                "workstack.report_documents",
                "workstack.report_json",
                "workstack.reporting",
            },
        )
        self.assertLessEqual(stdlib, self.STDLIB)

    def test_the_admission_leaf_reaches_nothing_in_the_product(self) -> None:
        product, stdlib = self.closure("workstack.report_json")
        self.assertEqual(product, {"workstack.report_json"})
        self.assertLessEqual(stdlib, self.STDLIB)

    def test_no_store_service_server_repository_or_path_is_reachable(self) -> None:
        product, _ = self.closure("workstack.report_documents")
        for banned in ("store", "store_rosters", "service", "server", "storage"):
            self.assertNotIn("workstack." + banned, product)

    def test_neither_module_calls_a_clock_an_identity_source_or_the_disk(self) -> None:
        for name in ("workstack.report_documents", "workstack.report_json"):
            body = self.source(name)
            for call in self.FORBIDDEN_CALLS:
                with self.subTest(module=name, call=call):
                    self.assertNotIn(call, body)

    def test_planning_is_deterministic_for_the_same_arguments(self) -> None:
        first = self.created()
        second = self.created()
        self.assertEqual(first, second)


# 2. The persisted shape, admitted exactly and repaired never.


class DocumentValidationTest(ReportCase):
    def test_the_default_empty_document_is_valid_and_detached(self) -> None:
        source = document()
        admitted = validate_reports_document(source, workspace_uid=WORKSPACE)

        self.assertEqual(admitted, {"version": 1, "reports": [], "idempotency": []})
        self.assertEqual(set(admitted), TOP_KEYS)
        self.assertIsNot(admitted, source)
        self.assertIsNot(admitted["reports"], source["reports"])
        self.assertIsNot(admitted["idempotency"], source["idempotency"])

    def test_a_populated_document_round_trips_by_value_without_sharing(self) -> None:
        source = document([report()], [receipt()])
        admitted = validate_reports_document(source, workspace_uid=WORKSPACE)

        self.assertEqual(admitted, source)
        self.assertIsNot(admitted["reports"][0], source["reports"][0])
        self.assertIsNot(
            admitted["reports"][0]["revisions"][0], source["reports"][0]["revisions"][0]
        )
        self.assertIsNot(
            admitted["idempotency"][0]["response_body"],
            source["idempotency"][0]["response_body"],
        )

    def test_the_version_is_exactly_one(self) -> None:
        self.assertEqual(REPORTS_DOCUMENT_VERSION, 1)
        for wrong in (0, 2, "1", 1.0, True, None):
            with self.subTest(version=repr(wrong)):
                broken = document()
                broken["version"] = wrong
                with self.refused("report_body_invalid", "document"):
                    validate_reports_document(broken, workspace_uid=WORKSPACE)

    def test_the_top_level_key_set_is_exact(self) -> None:
        extra = document()
        extra["extra"] = 1
        missing = {"version": 1, "reports": []}
        for broken in (extra, missing, [], "document", None, 3):
            with self.subTest(document=repr(broken)[:24]):
                with self.refused("report_body_invalid", "document"):
                    validate_reports_document(broken, workspace_uid=WORKSPACE)

    def test_every_report_key_is_required_and_no_other_is_allowed(self) -> None:
        for name in sorted(REPORT_KEYS):
            with self.subTest(missing=name):
                broken = report()
                del broken[name]
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([broken]), workspace_uid=WORKSPACE
                    )
        surplus = report()
        surplus["source_stale"] = False
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(document([surplus]), workspace_uid=WORKSPACE)

    def test_every_content_key_is_required_and_no_other_is_allowed(self) -> None:
        for name in sorted(CONTENT_KEYS):
            with self.subTest(missing=name):
                entry = content()
                del entry[name]
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([report(revisions=[entry])]), workspace_uid=WORKSPACE
                    )
        surplus = content(reopened=False)
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(
                document([report(revisions=[surplus])]), workspace_uid=WORKSPACE
            )

    def test_every_ledger_key_is_required_and_no_other_is_allowed(self) -> None:
        for name in sorted(LEDGER_KEYS):
            with self.subTest(missing=name):
                record = receipt()
                del record[name]
                with self.refused("report_body_invalid", "idempotency"):
                    validate_reports_document(
                        document(ledger=[record]), workspace_uid=WORKSPACE
                    )
        with self.refused("report_body_invalid", "idempotency"):
            validate_reports_document(
                document(ledger=[receipt(response_meta={})]), workspace_uid=WORKSPACE
            )

    def test_identities_are_canonical_lowercase_uuids(self) -> None:
        wrong = (
            uid(1).upper(),
            "00000001-abcd-3000-8000-000000000000",
            "00000000-0000-0000-0000-000000000000",
            "00000001-abcd-4000-c000-000000000000",
            "00000001abcd40008000000000000000",
            uid(1) + " ",
        )
        for value in wrong:
            with self.subTest(uid=value):
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([report(uid=value)]), workspace_uid=WORKSPACE
                    )

    def test_the_workspace_argument_binds_every_report(self) -> None:
        with self.refused("report_body_invalid", "workspace_uid") as caught:
            validate_reports_document(
                document([report(workspace_uid=OTHER_WORKSPACE)]),
                workspace_uid=WORKSPACE,
            )
        self.assertContentFree(caught.exception, WORKSPACE, OTHER_WORKSPACE)
        with self.refused("report_body_invalid", "workspace_uid"):
            validate_reports_document(document(), workspace_uid="not-a-uuid")

    def test_dates_digests_and_instants_are_admitted_exactly(self) -> None:
        cases = (
            ({"period": {"kind": "day", "date": "2026-02-30"}}, "period"),
            ({"period": {"kind": "day", "date": "2026-9-6"}}, "period"),
            ({"period": {"kind": "week", "date": "2026-09-06"}}, "period"),
            ({"period": {"kind": "day", "start": "2026-09-06"}}, "period"),
            ({"source_digest": "sha256:" + "0" * 63}, "source_digest"),
            ({"source_digest": "SHA256:" + "0" * 64}, "source_digest"),
            ({"source_generated_at": "2026-09-06T11:59:00"}, "reports"),
            ({"source_generated_at": "2026-09-06"}, "reports"),
        )
        for overrides, field in cases:
            with self.subTest(overrides=str(overrides)[:40]):
                with self.refused("report_body_invalid", field):
                    validate_reports_document(
                        document([report(**overrides)]), workspace_uid=WORKSPACE
                    )

    def test_fractional_and_offset_source_instants_are_accepted(self) -> None:
        for value in (
            "2026-09-06T11:59:00Z",
            "2026-09-06T11:59:00.123456Z",
            "2026-09-06T04:59:00-07:00",
        ):
            with self.subTest(instant=value):
                validate_reports_document(
                    document([report(source_generated_at=value)]),
                    workspace_uid=WORKSPACE,
                )

    def test_persisted_stamps_are_canonical_second_precision_utc(self) -> None:
        for value in (
            "2026-09-06T12:00:00.500Z",
            "2026-09-06T12:00:00+00:00",
            "2026-09-06 12:00:00Z",
            "2026-02-30T12:00:00Z",
        ):
            with self.subTest(stamp=value):
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([report(created_at=value, updated_at=value)]),
                        workspace_uid=WORKSPACE,
                    )

    def test_updated_at_never_precedes_created_at(self) -> None:
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(
                document([report(created_at=LATER, updated_at=NOW)]),
                workspace_uid=WORKSPACE,
            )
        validate_reports_document(
            document([report(created_at=NOW, updated_at=LATER)]),
            workspace_uid=WORKSPACE,
        )

    def test_a_template_this_build_does_not_render_is_unsupported(self) -> None:
        self.assertEqual(TEMPLATE_DAILY_V1, "daily-v1")
        with self.refused("report_template_unsupported", "template"):
            validate_reports_document(
                document([report(template="weekly-v1")]), workspace_uid=WORKSPACE
            )
        with self.refused("report_body_invalid", "template"):
            validate_reports_document(
                document([report(template=1)]), workspace_uid=WORKSPACE
            )

    def test_archive_fields_are_null_unless_the_report_is_archived(self) -> None:
        for name in ("archived_from_state", "archived_at", "archive_note"):
            with self.subTest(field=name):
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([report(**{name: "draft" if "state" in name else NOW})]),
                        workspace_uid=WORKSPACE,
                    )

    def test_an_archived_report_records_where_it_came_from(self) -> None:
        archived = report(
            state="archived", archived_from_state="finalized", archived_at=LATER
        )
        validate_reports_document(document([archived]), workspace_uid=WORKSPACE)
        for broken in ({"archived_from_state": None}, {"archived_at": None},
                       {"archived_from_state": "archived"}):
            with self.subTest(broken=str(broken)):
                record = dict(archived)
                record.update(broken)
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([record]), workspace_uid=WORKSPACE
                    )

    def test_history_is_contiguous_ascending_and_bounded(self) -> None:
        self.assertEqual(MAX_REPORT_CONTENT_REVISIONS, 20)
        broken = (
            [],
            [content(content_revision=2)],
            [content(), content(content_revision=3, document_revision=2)],
            [content(), content(content_revision=2, document_revision=1)],
            [content(document_revision=0)],
            [content(document_revision=2)],
            [content(content_revision=True)],
        )
        for entries in broken:
            with self.subTest(entries=len(entries)):
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([report(revisions=entries)]), workspace_uid=WORKSPACE
                    )

    def test_a_full_history_is_valid_and_a_longer_one_is_not(self) -> None:
        full = [
            content(content_revision=index + 1, document_revision=index + 1)
            for index in range(MAX_REPORT_CONTENT_REVISIONS)
        ]
        validate_reports_document(
            document([report(revisions=full, revision=MAX_REPORT_CONTENT_REVISIONS)]),
            workspace_uid=WORKSPACE,
        )
        over = full + [content(content_revision=21, document_revision=21)]
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(
                document([report(revisions=over, revision=21)]),
                workspace_uid=WORKSPACE,
            )

    def test_report_revision_is_a_positive_integer_within_the_ceiling(self) -> None:
        self.assertEqual(MAX_REPORT_DOCUMENT_REVISION, 9_007_199_254_740_991)
        for value in (0, -1, True, 1.0, "1", MAX_REPORT_DOCUMENT_REVISION + 1):
            with self.subTest(revision=repr(value)):
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(
                        document([report(revision=value)]), workspace_uid=WORKSPACE
                    )

    def test_report_identities_and_active_periods_are_unique(self) -> None:
        twin = document([report(), report()])
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(twin, workspace_uid=WORKSPACE)
        same_period = document([report(), report(uid=uid(2))])
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(same_period, workspace_uid=WORKSPACE)

    def test_an_archived_report_frees_its_period(self) -> None:
        archived = report(
            uid=uid(2),
            state="archived",
            archived_from_state="draft",
            archived_at=NOW,
        )
        validate_reports_document(
            document([report(), archived]), workspace_uid=WORKSPACE
        )

    def test_ledger_keys_are_unique_and_match_the_shipped_pattern(self) -> None:
        with self.refused("report_body_invalid", "idempotency"):
            validate_reports_document(
                document(ledger=[receipt(), receipt()]), workspace_uid=WORKSPACE
            )
        for value in ("short", "x" * 129, "bad key", "key/with/slash", 12345678):
            with self.subTest(key=repr(value)):
                with self.refused("report_body_invalid", "key"):
                    validate_reports_document(
                        document(ledger=[receipt(key=value)]), workspace_uid=WORKSPACE
                    )

    def test_only_post_and_only_the_two_recorded_statuses_are_stored(self) -> None:
        with self.refused("report_body_invalid", "idempotency"):
            validate_reports_document(
                document(ledger=[receipt(method="GET")]), workspace_uid=WORKSPACE
            )
        for value in (204, 409, 500, "200", True, 200.0):
            with self.subTest(status=repr(value)):
                with self.refused("report_body_invalid", "idempotency"):
                    validate_reports_document(
                        document(ledger=[receipt(response_status=value)]),
                        workspace_uid=WORKSPACE,
                    )

    def test_a_stored_receipt_body_is_a_writer_envelope_that_is_not_a_replay(self) -> None:
        broken = (
            {"data": {}},
            {"data": {}, "meta": {"replayed": False}, "extra": 1},
            {"data": [], "meta": {"replayed": False}},
            {"data": {}, "meta": {"replayed": True}},
            {"data": {}, "meta": {"replayed": False, "trace": "x"}},
            {"data": {}, "meta": {}},
        )
        for body in broken:
            with self.subTest(body=str(body)[:40]):
                with self.refused("report_body_invalid", "response_body"):
                    validate_reports_document(
                        document(ledger=[receipt(response_body=body)]),
                        workspace_uid=WORKSPACE,
                    )

    def test_a_stored_receipt_never_carries_the_authored_history(self) -> None:
        for body in (
            response_body(revisions=[content()]),
            {"data": {"report": {"revisions": []}}, "meta": {"replayed": False}},
            {"data": {"items": [{"revisions": [1]}]}, "meta": {"replayed": False}},
        ):
            with self.subTest(body=str(body)[:40]):
                with self.refused("report_body_invalid", "response_body"):
                    validate_reports_document(
                        document(ledger=[receipt(response_body=body)]),
                        workspace_uid=WORKSPACE,
                    )
        validate_reports_document(
            document(ledger=[receipt(response_body=response_body(revisions=3))]),
            workspace_uid=WORKSPACE,
        )

    def test_nothing_is_repaired_defaulted_pruned_or_reordered(self) -> None:
        stale = receipt(key="expired-000001", created_at=stamp(1))
        fresh = receipt(key="fresh-00000001", created_at=stamp(6))
        source = document([report(uid=uid(2), period={"kind": "day", "date": "2026-09-05"}),
                           report()], [stale, fresh])
        admitted = validate_reports_document(source, workspace_uid=WORKSPACE)
        self.assertEqual([item["uid"] for item in admitted["reports"]], [uid(2), uid(1)])
        self.assertEqual(
            [item["key"] for item in admitted["idempotency"]],
            ["expired-000001", "fresh-00000001"],
        )


# 3. Python values JSON cannot carry.


class JsonSafetyTest(ReportCase):
    def test_a_bool_never_passes_as_an_integer(self) -> None:
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(
                {"version": True, "reports": [], "idempotency": []},
                workspace_uid=WORKSPACE,
            )
        with self.refused("report_body_invalid", "reports"):
            validate_reports_document(
                document([report(revision=True)]), workspace_uid=WORKSPACE
            )

    def test_container_and_scalar_subclasses_are_refused(self) -> None:
        class Mapping(dict):
            pass

        class Sequence(list):
            pass

        class Text(str):
            pass

        class Number(int):
            pass

        for value in (Mapping(document()), Sequence(), Text("x"), Number(1)):
            with self.subTest(kind=type(value).__name__):
                with self.refused("report_body_invalid", "document"):
                    validate_reports_document(value, workspace_uid=WORKSPACE)
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(
                document([report(markdown=Text("x"))]), workspace_uid=WORKSPACE
            )

    def test_a_lone_surrogate_is_refused_wherever_it_appears(self) -> None:
        lone = "bad \ud800 text"
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(
                document(ledger=[receipt(response_body=response_body(note=lone))]),
                workspace_uid=WORKSPACE,
            )
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(
                document(ledger=[receipt(response_body={"data": {lone: 1},
                                                        "meta": {"replayed": False}})]),
                workspace_uid=WORKSPACE,
            )

    def test_non_string_dict_keys_are_refused(self) -> None:
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(
                document(ledger=[receipt(response_body={"data": {1: "x"},
                                                        "meta": {"replayed": False}})]),
                workspace_uid=WORKSPACE,
            )

    def test_nan_and_infinity_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                with self.refused("report_body_invalid", "document"):
                    validate_reports_document(
                        document(ledger=[receipt(response_body=response_body(n=value))]),
                        workspace_uid=WORKSPACE,
                    )

    def test_a_cycle_is_refused_rather_than_recursing(self) -> None:
        data: dict[str, Any] = {"uid": uid(1)}
        data["self"] = data
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(
                document(ledger=[receipt(response_body={"data": data,
                                                        "meta": {"replayed": False}})]),
                workspace_uid=WORKSPACE,
            )

    def test_depth_sixteen_passes_and_seventeen_is_refused(self) -> None:
        self.assertEqual(MAX_REPORT_JSON_DEPTH, 16)
        # The document, the ledger, the receipt and its body are four
        # containers, so a chain standing in for data may be twelve deep and
        # no more: the thirteenth link would sit at level seventeen.
        deep = document(ledger=[receipt(response_body={"data": nest(12),
                                                       "meta": {"replayed": False}})])
        validate_reports_document(deep, workspace_uid=WORKSPACE)
        deeper = document(ledger=[receipt(response_body={"data": nest(13),
                                                         "meta": {"replayed": False}})])
        with self.refused("report_body_invalid", "document"):
            validate_reports_document(deeper, workspace_uid=WORKSPACE)


# 4. The byte caps, measured as canonical UTF-8.


def padded_receipt(index: int, target: int) -> dict[str, Any]:
    value = receipt(
        key="pad-{:011d}".format(index),
        response_body={"data": {"pad": ""}, "meta": {"replayed": False}},
    )
    if target:
        value["response_body"]["data"]["pad"] = "x" * (target - len(canonical(value)))
    return value


def document_of_size(target: int) -> dict[str, Any]:
    """A valid document whose canonical serialization is exactly `target` bytes."""

    records: list[dict[str, Any]] = []
    value = document(ledger=records)
    index = 0
    while len(canonical(value)) < target:
        records.append(padded_receipt(index, MAX_LEDGER_RECORD_BYTES))
        if len(canonical(value)) >= target:
            records.pop()
            break
        index += 1
    records.append(padded_receipt(index, 0))
    records[-1]["response_body"]["data"]["pad"] = "x" * (target - len(canonical(value)))
    return value


class ByteCapTest(ReportCase):
    def test_a_receipt_at_the_cap_passes_and_one_byte_more_does_not(self) -> None:
        self.assertEqual(MAX_LEDGER_RECORD_BYTES, 192 * 1024)
        exact = padded_receipt(0, MAX_LEDGER_RECORD_BYTES)
        self.assertEqual(len(canonical(exact)), MAX_LEDGER_RECORD_BYTES)
        validate_reports_document(document(ledger=[exact]), workspace_uid=WORKSPACE)

        over = padded_receipt(0, MAX_LEDGER_RECORD_BYTES + 1)
        with self.refused("report_body_invalid", "response_body"):
            validate_reports_document(document(ledger=[over]), workspace_uid=WORKSPACE)

    def test_a_document_at_the_cap_passes_and_one_byte_more_does_not(self) -> None:
        self.assertEqual(MAX_REPORTS_BYTES, 8 * 1024 * 1024)
        exact = document_of_size(MAX_REPORTS_BYTES)
        self.assertEqual(len(canonical(exact)), MAX_REPORTS_BYTES)
        validate_reports_document(exact, workspace_uid=WORKSPACE)

        over = document_of_size(MAX_REPORTS_BYTES + 1)
        with self.refused("report_storage_full", "document"):
            validate_reports_document(over, workspace_uid=WORKSPACE)

    def test_the_markdown_character_cap_is_not_a_byte_cap(self) -> None:
        self.assertEqual(MAX_MARKDOWN_CHARS, 100000)
        wide = "é" * MAX_MARKDOWN_CHARS
        self.assertGreater(len(wide.encode("utf-8")), MAX_MARKDOWN_CHARS)
        validate_reports_document(
            document([report(revisions=[content(markdown=wide)])]),
            workspace_uid=WORKSPACE,
        )
        with self.refused("report_body_invalid", "markdown"):
            validate_reports_document(
                document([report(revisions=[content(markdown=wide + "x")])]),
                workspace_uid=WORKSPACE,
            )

    def test_a_note_is_bounded_by_characters_and_by_bytes(self) -> None:
        self.assertEqual((MAX_REPORT_NOTE_CHARS, MAX_REPORT_NOTE_BYTES), (240, 1024))
        plain = "n" * MAX_REPORT_NOTE_CHARS
        validate_reports_document(
            document([report(revisions=[content(note=plain)])]), workspace_uid=WORKSPACE
        )
        with self.refused("report_body_invalid", "note"):
            validate_reports_document(
                document([report(revisions=[content(note=plain + "n")])]),
                workspace_uid=WORKSPACE,
            )
        # The widest UTF-8 code point is four bytes, so 240 of them are 960:
        # the character cap always binds first, and the byte cap is a second
        # guard on stored size that today cannot be reached on its own.
        widest = "\U0001f600" * MAX_REPORT_NOTE_CHARS
        self.assertEqual(len(widest), MAX_REPORT_NOTE_CHARS)
        self.assertLessEqual(len(widest.encode("utf-8")), MAX_REPORT_NOTE_BYTES)
        validate_reports_document(
            document([report(revisions=[content(note=widest)])]),
            workspace_uid=WORKSPACE,
        )
        with self.refused("report_body_invalid", "note"):
            validate_reports_document(
                document([report(revisions=[content(note=widest + "\U0001f600")])]),
                workspace_uid=WORKSPACE,
            )


# 5. Create.


class CreateTest(ReportCase):
    def test_the_created_record_and_the_return_shape_are_exact(self) -> None:
        source = document()
        request = create_request()
        plan = plan_report_mutation(
            source,
            "create",
            request,
            report_uid=uid(7),
            current_source_digest=DIGEST,
            now=NOW,
        )

        self.assertEqual(set(plan), PLAN_KEYS)
        self.assertIs(plan["reopened"], False)
        self.assertEqual(
            plan["document"]["reports"][0],
            {
                "uid": uid(7),
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
                "revisions": [
                    {
                        "content_revision": 1,
                        "document_revision": 1,
                        "markdown": "# first",
                        "authored_at": NOW,
                        "note": None,
                    }
                ],
                "created_at": NOW,
                "updated_at": NOW,
            },
        )
        self.assertEqual(set(plan["report"]), REPORT_KEYS - {"revisions"})
        self.assertEqual(set(plan["content_entry"]), CONTENT_KEYS)
        self.assertEqual(plan["document"]["idempotency"], [])

    def test_the_inputs_are_untouched_and_unshared(self) -> None:
        source = document([report(uid=uid(2), period={"kind": "day", "date": "2026-09-05"})])
        before = canonical(source)
        request = create_request()
        request_before = canonical(request)

        plan = plan_report_mutation(
            source, "create", request, report_uid=uid(7),
            current_source_digest=DIGEST, now=NOW,
        )

        self.assertEqual(canonical(source), before)
        self.assertEqual(canonical(request), request_before)
        self.assertEqual(len(source["reports"]), 1)
        self.assertIsNot(plan["document"]["reports"][0], source["reports"][0])
        self.assertIsNot(plan["document"]["reports"][1]["period"], request["period"])

    def test_a_changed_source_refuses_the_creation(self) -> None:
        with self.refused("report_source_changed", "source_digest") as caught:
            plan_report_mutation(
                document(), "create", create_request(), report_uid=uid(7),
                current_source_digest=OTHER_DIGEST, now=NOW,
            )
        self.assertContentFree(caught.exception, DIGEST, OTHER_DIGEST)

    def test_an_active_report_already_covering_the_period_refuses(self) -> None:
        with self.refused("report_duplicate_period", "period"):
            plan_report_mutation(
                document([report()]), "create", create_request(), report_uid=uid(7),
                current_source_digest=DIGEST, now=NOW,
            )

    def test_an_archived_report_does_not_block_a_new_one(self) -> None:
        archived = report(
            state="archived", archived_from_state="finalized", archived_at=NOW
        )
        plan = plan_report_mutation(
            document([archived]), "create", create_request(), report_uid=uid(7),
            current_source_digest=DIGEST, now=NOW,
        )
        self.assertEqual(len(plan["document"]["reports"]), 2)

    def test_the_document_cap_refuses_without_evicting_anything(self) -> None:
        self.assertEqual(MAX_REPORT_DOCUMENTS, 500)
        reports = [
            report(uid=uid(index + 1), period={"kind": "day", "date": "2026-01-01"},
                   state="archived", archived_from_state="draft", archived_at=NOW)
            for index in range(MAX_REPORT_DOCUMENTS)
        ]
        full = document(reports)
        with self.refused("report_document_limit", "reports"):
            plan_report_mutation(
                full, "create", create_request(), report_uid=uid(900),
                current_source_digest=DIGEST, now=NOW,
            )
        self.assertEqual(len(full["reports"]), MAX_REPORT_DOCUMENTS)

    def test_the_allocated_identity_must_not_already_exist(self) -> None:
        with self.refused("report_body_invalid", "report_uid"):
            plan_report_mutation(
                document([report(period={"kind": "day", "date": "2026-09-05"})]),
                "create", create_request(), report_uid=uid(1),
                current_source_digest=DIGEST, now=NOW,
            )

    def test_a_create_needs_a_computed_source_digest(self) -> None:
        with self.refused("report_body_invalid", "source_digest"):
            plan_report_mutation(
                document(), "create", create_request(), report_uid=uid(7),
                current_source_digest=None, now=NOW,
            )

    def test_a_caller_supplied_instant_must_be_canonical(self) -> None:
        for value in ("2026-09-06T12:00:00", "2026-09-06T12:00:00.5Z", None, 0):
            with self.subTest(now=repr(value)):
                with self.refused("report_body_invalid", "now"):
                    plan_report_mutation(
                        document(), "create", create_request(), report_uid=uid(7),
                        current_source_digest=DIGEST, now=value,
                    )


# 6 and 7. The transition table.


class TransitionTest(ReportCase):
    TABLE = {
        ("draft", "revise"): "draft",
        ("draft", "finalize"): "finalized",
        ("draft", "archive"): "archived",
        ("finalized", "revise"): "draft",
        ("finalized", "archive"): "archived",
        ("archived", "restore"): "draft",
    }
    REFUSED = (
        ("finalized", "finalize"),
        ("draft", "restore"),
        ("finalized", "restore"),
        ("archived", "revise"),
        ("archived", "finalize"),
        ("archived", "archive"),
    )

    def start(self, state: str, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {"state": state, "revision": 1}
        if state == "archived":
            base.update(archived_from_state="draft", archived_at=NOW)
        base.update(overrides)
        return document([report(**base)])

    def request(self, name: str, revision: int = 1, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"workspace_uid": WORKSPACE, "expected_revision": revision}
        if name == "revise":
            body.update(markdown="# next", note=None)
        if name == "archive":
            body.update(note=None)
        body.update(extra)
        return body

    def test_every_allowed_transition_lands_in_the_contracted_state(self) -> None:
        for (state, name), expected in self.TABLE.items():
            with self.subTest(state=state, operation=name):
                plan = self.plan(self.start(state), name, self.request(name))
                self.assertEqual(plan["report"]["state"], expected)
                self.assertEqual(plan["report"]["revision"], 2)
                self.assertEqual(plan["report"]["updated_at"], LATER)
                self.assertEqual(plan["report"]["created_at"], NOW)

    def test_every_disallowed_transition_is_refused_without_writing(self) -> None:
        for state, name in self.REFUSED:
            with self.subTest(state=state, operation=name):
                source = self.start(state)
                before = canonical(source)
                with self.refused("report_state_invalid", "document"):
                    self.plan(source, name, self.request(name))
                self.assertEqual(canonical(source), before)

    def test_a_stale_expected_revision_refuses_all_four_operations(self) -> None:
        for name in ("revise", "finalize", "archive", "restore"):
            state = "archived" if name == "restore" else "draft"
            with self.subTest(operation=name):
                source = self.start(state, revision=4)
                before = canonical(source)
                with self.refused("report_revision_conflict", "expected_revision"):
                    self.plan(source, name, self.request(name, revision=3))
                self.assertEqual(canonical(source), before)

    def test_the_revision_check_runs_before_the_state_check(self) -> None:
        with self.refused("report_revision_conflict", "expected_revision"):
            self.plan(
                self.start("archived", revision=2), "finalize", self.request("finalize")
            )

    def test_an_unknown_identity_is_not_found(self) -> None:
        with self.refused("report_not_found", "report_uid") as caught:
            self.plan(
                self.start("draft"), "finalize", self.request("finalize"),
                report_uid=uid(99),
            )
        self.assertContentFree(caught.exception, uid(99))

    def test_reviving_a_finalized_report_reopens_it_and_says_so(self) -> None:
        plan = self.plan(self.start("finalized"), "revise", self.request("revise"))
        self.assertIs(plan["reopened"], True)
        self.assertEqual(plan["report"]["state"], "draft")
        again = self.plan(self.start("draft"), "revise", self.request("revise"))
        self.assertIs(again["reopened"], False)

    def test_a_revise_appends_exactly_one_entry_and_leaves_the_older_ones(self) -> None:
        source = self.start("draft")
        original = source["reports"][0]["revisions"][0]
        plan = self.plan(source, "revise", self.request("revise"))
        history = plan["document"]["reports"][0]["revisions"]

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], original)
        self.assertEqual(
            history[1],
            {
                "content_revision": 2,
                "document_revision": 2,
                "markdown": "# next",
                "authored_at": LATER,
                "note": None,
            },
        )
        self.assertEqual(plan["content_entry"], history[1])

    def test_finalize_archive_and_restore_write_no_content(self) -> None:
        for name, state in (("finalize", "draft"), ("archive", "draft"),
                            ("restore", "archived")):
            with self.subTest(operation=name):
                plan = self.plan(self.start(state), name, self.request(name))
                self.assertIsNone(plan["content_entry"])
                self.assertEqual(len(plan["document"]["reports"][0]["revisions"]), 1)

    def test_archive_records_its_origin_time_and_note(self) -> None:
        plan = self.plan(
            self.start("finalized"), "archive", self.request("archive", note="stale")
        )
        self.assertEqual(plan["report"]["archived_from_state"], "finalized")
        self.assertEqual(plan["report"]["archived_at"], LATER)
        self.assertEqual(plan["report"]["archive_note"], "stale")

    def test_restore_returns_to_the_recorded_state_and_clears_the_metadata(self) -> None:
        source = self.start("archived", archived_from_state="finalized",
                            archive_note="stale")
        plan = self.plan(source, "restore", self.request("restore"))
        self.assertEqual(plan["report"]["state"], "finalized")
        self.assertIsNone(plan["report"]["archived_from_state"])
        self.assertIsNone(plan["report"]["archived_at"])
        self.assertIsNone(plan["report"]["archive_note"])

    def test_restore_refuses_when_the_period_was_taken_meanwhile(self) -> None:
        archived = report(
            state="archived", archived_from_state="draft", archived_at=NOW
        )
        taken = report(uid=uid(2))
        source = document([archived, taken])
        before = canonical(source)
        with self.refused("report_duplicate_period", "period"):
            self.plan(source, "restore", self.request("restore"))
        self.assertEqual(canonical(source), before)

    def test_a_full_history_refuses_only_the_write(self) -> None:
        full = [
            content(content_revision=index + 1, document_revision=index + 1)
            for index in range(MAX_REPORT_CONTENT_REVISIONS)
        ]
        source = self.start("draft", revisions=full, revision=20)
        with self.refused("report_revision_limit", "reports"):
            self.plan(source, "revise", self.request("revise", revision=20))
        for name in ("finalize", "archive"):
            with self.subTest(operation=name):
                plan = self.plan(source, name, self.request(name, revision=20))
                self.assertEqual(len(plan["document"]["reports"][0]["revisions"]), 20)

    def test_the_revision_ceiling_refuses_every_transition(self) -> None:
        ceiling = MAX_REPORT_DOCUMENT_REVISION
        for name in ("revise", "finalize", "archive", "restore"):
            state = "archived" if name == "restore" else "draft"
            with self.subTest(operation=name):
                source = self.start(state, revision=ceiling)
                with self.refused("report_revision_limit", "reports"):
                    self.plan(source, name, self.request(name, revision=ceiling))

    def test_the_creation_source_and_older_entries_survive_every_transition(self) -> None:
        current = self.created()
        original = json.loads(canonical(current["reports"][0]["revisions"][0]))
        steps = (
            ("revise", {"markdown": "# two", "note": "why"}),
            ("finalize", {}),
            ("revise", {"markdown": "# three", "note": None}),
            ("archive", {"note": "done"}),
            ("restore", {}),
        )
        for index, (name, extra) in enumerate(steps):
            body = self.request(name, revision=index + 1, **extra)
            plan = plan_report_mutation(
                current, name, body, report_uid=uid(1), now=stamp(7, 12 + index)
            )
            current = plan["document"]
            record = current["reports"][0]
            self.assertEqual(record["source_digest"], DIGEST)
            self.assertEqual(record["source_generated_at"], GENERATED)
            self.assertEqual(record["created_at"], NOW)
            self.assertEqual(canonical(record["revisions"][0]), canonical(original))
        self.assertEqual(current["reports"][0]["revision"], 6)
        self.assertEqual(current["reports"][0]["state"], "draft")

    def test_a_non_create_never_takes_a_computed_source_digest(self) -> None:
        with self.refused("report_body_invalid", "source_digest"):
            self.plan(
                self.start("draft"), "finalize", self.request("finalize"),
                current_source_digest=DIGEST,
            )

    def test_an_instant_before_the_last_write_is_refused(self) -> None:
        with self.refused("report_body_invalid", "now"):
            plan_report_mutation(
                self.start("draft", created_at=LATER, updated_at=LATER),
                "finalize", self.request("finalize"), report_uid=uid(1), now=NOW,
            )


class RequestAdmissionTest(ReportCase):
    def test_each_operation_accepts_exactly_its_own_keys(self) -> None:
        bodies = {
            "create": create_request(),
            "revise": {"workspace_uid": WORKSPACE, "expected_revision": 1,
                       "markdown": "# x", "note": None},
            "finalize": {"workspace_uid": WORKSPACE, "expected_revision": 1},
            "archive": {"workspace_uid": WORKSPACE, "expected_revision": 1, "note": None},
            "restore": {"workspace_uid": WORKSPACE, "expected_revision": 1},
        }
        for name, body in bodies.items():
            with self.subTest(operation=name):
                admitted = normalize_report_request(name, body)
                self.assertEqual(admitted, body)
                self.assertIsNot(admitted, body)
                for extra in ("markdown", "note", "source_digest", "uid"):
                    if extra in body:
                        continue
                    widened = dict(body)
                    widened[extra] = "x"
                    with self.refused("report_body_invalid", "request"):
                        normalize_report_request(name, widened)

    def test_an_unknown_operation_is_refused(self) -> None:
        for name in ("delete", "Create", "", None, 1):
            with self.subTest(operation=repr(name)):
                with self.refused("report_body_invalid", "operation"):
                    normalize_report_request(name, create_request())

    def test_expected_revision_is_a_positive_integer_within_the_ceiling(self) -> None:
        for value in (0, -1, True, "1", 1.0, MAX_REPORT_DOCUMENT_REVISION + 1):
            with self.subTest(revision=repr(value)):
                with self.refused("report_body_invalid", "expected_revision"):
                    normalize_report_request(
                        "finalize",
                        {"workspace_uid": WORKSPACE, "expected_revision": value},
                    )

    def test_a_well_formed_unknown_template_is_unsupported(self) -> None:
        with self.refused("report_template_unsupported", "template"):
            normalize_report_request("create", create_request(template="weekly-v1"))
        with self.refused("report_body_invalid", "template"):
            normalize_report_request("create", create_request(template=None))

    def test_planning_re_admits_the_request_without_a_prior_normalize(self) -> None:
        with self.refused("report_body_invalid", "request"):
            plan_report_mutation(
                document(), "create", {"workspace_uid": WORKSPACE},
                report_uid=uid(7), current_source_digest=DIGEST, now=NOW,
            )


# 8, 9 and 10. The ledger.


class ReplayTest(ReportCase):
    def replay(self, doc: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        arguments = {
            "workspace_uid": WORKSPACE,
            "key": KEY,
            "method": "POST",
            "path": ROUTE,
            "request_digest": DIGEST,
            "now": LATER,
        }
        arguments.update(kwargs)
        return prepare_report_replay(doc, **arguments)

    def test_a_miss_returns_a_pruned_candidate_and_no_replay(self) -> None:
        result = self.replay(document())
        self.assertEqual(set(result), REPLAY_KEYS)
        self.assertIsNone(result["replay"])
        self.assertEqual(result["document"], document())

    def test_an_exact_replay_returns_the_recorded_status_and_data(self) -> None:
        stored = receipt(response_status=201, response_body=response_body(uid=uid(1),
                                                                         revision=1))
        source = document(ledger=[stored])
        before = canonical(source)

        result = self.replay(source)

        self.assertEqual(result["replay"]["response_status"], 201)
        self.assertEqual(
            result["replay"]["response_body"],
            {"data": {"uid": uid(1), "revision": 1}, "meta": {"replayed": True}},
        )
        self.assertEqual(canonical(source), before)
        self.assertIsNot(result["replay"]["response_body"], stored["response_body"])
        self.assertIs(stored["response_body"]["meta"]["replayed"], False)

    def test_a_replay_survives_later_mutations_of_the_same_report(self) -> None:
        created = self.created()
        appended = append_report_receipt(
            created, workspace_uid=WORKSPACE, key=KEY, method="POST", path=ROUTE,
            request_digest=DIGEST, response_status=201,
            response_body=response_body(uid=uid(1), state="draft", revision=1),
            now=NOW,
        )
        moved = appended
        for index, name in enumerate(("finalize", "archive")):
            body: dict[str, Any] = {"workspace_uid": WORKSPACE,
                                    "expected_revision": index + 1}
            if name == "archive":
                body["note"] = None
            moved = plan_report_mutation(
                moved, name, body, report_uid=uid(1), now=stamp(7, 12 + index)
            )["document"]

        result = self.replay(moved, now=stamp(8))
        self.assertEqual(result["replay"]["response_status"], 201)
        self.assertEqual(
            result["replay"]["response_body"]["data"],
            {"uid": uid(1), "state": "draft", "revision": 1},
        )
        self.assertEqual(moved["reports"][0]["state"], "archived")

    def test_a_reused_key_with_a_different_request_is_a_conflict(self) -> None:
        source = document(ledger=[receipt()])
        for changed in ({"method": "PUT"}, {"path": "/api/v1/reports/x"},
                        {"request_digest": OTHER_DIGEST}):
            with self.subTest(changed=str(changed)):
                if "method" in changed:
                    # A method this feature never records is refused as a body
                    # defect before it can be compared.
                    with self.refused("report_body_invalid", "idempotency"):
                        self.replay(source, **changed)
                    continue
                with self.refused("idempotency_conflict", "key") as caught:
                    self.replay(source, **changed)
                self.assertContentFree(caught.exception, KEY, ROUTE, DIGEST)

    def test_the_retention_boundary_keeps_and_then_drops(self) -> None:
        self.assertEqual(REPORT_LEDGER_RETENTION_DAYS, 30)
        stored = receipt(created_at="2026-08-07T12:00:00Z")
        just_inside = self.replay(document(ledger=[stored]),
                                  now="2026-09-06T11:59:59Z")
        self.assertEqual(len(just_inside["document"]["idempotency"]), 1)
        self.assertIsNotNone(just_inside["replay"])

        exactly = self.replay(document(ledger=[stored]), now="2026-09-06T12:00:00Z")
        self.assertEqual(exactly["document"]["idempotency"], [])
        self.assertIsNone(exactly["replay"])

    def test_an_expired_key_is_a_miss_rather_than_a_conflict(self) -> None:
        stored = receipt(created_at=stamp(1), request_digest=OTHER_DIGEST)
        result = self.replay(document(ledger=[stored]), now="2026-10-06T12:00:00Z")
        self.assertIsNone(result["replay"])
        self.assertEqual(result["document"]["idempotency"], [])

    def test_a_full_ledger_still_replays_exactly(self) -> None:
        records = [receipt(key="fill-{:010d}".format(index))
                   for index in range(REPORT_LEDGER_MAX_RECORDS - 1)]
        records.append(receipt())
        result = self.replay(document(ledger=records))
        self.assertIsNotNone(result["replay"])
        self.assertEqual(len(result["document"]["idempotency"]),
                         REPORT_LEDGER_MAX_RECORDS)


class AppendTest(ReportCase):
    def append(self, doc: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        arguments = {
            "workspace_uid": WORKSPACE,
            "key": KEY,
            "method": "POST",
            "path": ROUTE,
            "request_digest": DIGEST,
            "response_status": 201,
            "response_body": response_body(),
            "now": LATER,
        }
        arguments.update(kwargs)
        return append_report_receipt(doc, **arguments)

    def test_one_record_is_appended_at_the_end_and_nothing_else_moves(self) -> None:
        first = receipt(key="older-00000001", created_at=NOW)
        source = document(ledger=[first])
        before = canonical(source)

        result = self.append(source)

        self.assertEqual(canonical(source), before)
        self.assertEqual(len(result["idempotency"]), 2)
        self.assertEqual(result["idempotency"][0], first)
        self.assertEqual(
            result["idempotency"][1],
            {
                "key": KEY,
                "method": "POST",
                "path": ROUTE,
                "request_digest": DIGEST,
                "response_status": 201,
                "response_body": response_body(),
                "created_at": LATER,
            },
        )

    def test_a_surviving_duplicate_key_is_a_conflict(self) -> None:
        with self.refused("idempotency_conflict", "key"):
            self.append(document(ledger=[receipt()]))

    def test_an_expired_duplicate_key_is_pruned_and_then_accepted(self) -> None:
        stale = receipt(created_at=stamp(1))
        result = self.append(document(ledger=[stale]), now="2026-10-06T12:00:00Z")
        self.assertEqual(len(result["idempotency"]), 1)
        self.assertEqual(result["idempotency"][0]["created_at"], "2026-10-06T12:00:00Z")

    def test_a_full_ledger_refuses_and_deletes_nothing(self) -> None:
        self.assertEqual(REPORT_LEDGER_MAX_RECORDS, 1_000)
        records = [
            receipt(key="fill-{:010d}".format(index), created_at=stamp(6, 12))
            for index in range(REPORT_LEDGER_MAX_RECORDS)
        ]
        records[0]["created_at"] = stamp(6, 10)
        source = document(ledger=records)
        before = canonical(source)

        with self.refused("report_idempotency_capacity", "idempotency") as caught:
            self.append(source, now="2026-10-05T12:00:00Z")

        self.assertEqual(canonical(source), before)
        # The oldest receipt was written 2026-09-06T10:00:00Z, so it expires at
        # 2026-10-06T10:00:00Z, which is 79,200 seconds after this attempt.
        self.assertEqual(caught.exception.retry_after_seconds, 79_200)
        self.assertContentFree(caught.exception, KEY)

    def test_capacity_returns_once_the_oldest_receipts_expire(self) -> None:
        records = [
            receipt(key="fill-{:010d}".format(index), created_at=stamp(6))
            for index in range(REPORT_LEDGER_MAX_RECORDS)
        ]
        result = self.append(document(ledger=records), now="2026-10-06T12:00:00Z")
        self.assertEqual(len(result["idempotency"]), 1)

    def test_only_a_capacity_refusal_carries_a_retry_hint(self) -> None:
        with self.refused("idempotency_conflict", "key") as caught:
            self.append(document(ledger=[receipt()]))
        self.assertIsNone(caught.exception.retry_after_seconds)

    def test_an_oversized_receipt_refuses_and_returns_no_candidate(self) -> None:
        oversized = padded_receipt(0, MAX_LEDGER_RECORD_BYTES + 1)
        source = document()
        with self.refused("report_body_invalid", "response_body"):
            self.append(
                source, key=oversized["key"],
                response_body=oversized["response_body"],
            )
        self.assertEqual(source["idempotency"], [])

    def test_a_receipt_that_would_overflow_the_document_is_storage_full(self) -> None:
        head = document_of_size(MAX_REPORTS_BYTES - 4096)
        with self.refused("report_storage_full", "document"):
            self.append(head, response_body=padded_receipt(999, MAX_LEDGER_RECORD_BYTES)
                        ["response_body"])

    def test_a_receipt_body_must_be_a_fresh_writer_envelope(self) -> None:
        for body in (
            {"data": {}, "meta": {"replayed": True}},
            {"data": {}, "meta": {}},
            {"data": {"revisions": []}, "meta": {"replayed": False}},
            {"data": [], "meta": {"replayed": False}},
        ):
            with self.subTest(body=str(body)[:40]):
                with self.refused("report_body_invalid", "response_body"):
                    self.append(document(), response_body=body)

    def test_only_the_two_recorded_statuses_may_be_appended(self) -> None:
        for value in (204, 409, True, "201"):
            with self.subTest(status=repr(value)):
                with self.refused("report_body_invalid", "idempotency"):
                    self.append(document(), response_status=value)


# Directed probes: the identities the contract admits, and the calendar edges.


def version_uid(nibble: str) -> str:
    return "00000001-abcd-{}000-8000-000000000000".format(nibble)


class WorkspaceIdentityTest(ReportCase):
    """Any non-nil canonical lowercase RFC 4122 authority, not only v1 to v5.

    The version nibble does not decide whether an identity is admissible; the
    variant nibble does, and it is also what excludes the nil UUID. A workspace
    created by a build that allocates version 7 is a real authority, and every
    entry point has to take it.
    """

    V7 = "00000001-abcd-7000-8000-000000000000"

    @staticmethod
    def requests(owner: str) -> dict[str, dict[str, Any]]:
        return {
            "create": create_request(workspace_uid=owner),
            "revise": {"workspace_uid": owner, "expected_revision": 1,
                       "markdown": "# x", "note": None},
            "finalize": {"workspace_uid": owner, "expected_revision": 1},
            "archive": {"workspace_uid": owner, "expected_revision": 1, "note": None},
            "restore": {"workspace_uid": owner, "expected_revision": 1},
        }

    def admit_everywhere(self, owner: str) -> None:
        """Every door an identity can arrive at: document, request, ledger."""

        empty = document()
        self.assertEqual(validate_reports_document(empty, workspace_uid=owner), empty)

        bound = document([report(workspace_uid=owner)], [receipt()])
        self.assertEqual(validate_reports_document(bound, workspace_uid=owner), bound)

        for name, body in self.requests(owner).items():
            with self.subTest(operation=name):
                self.assertEqual(normalize_report_request(name, body), body)

        planned = plan_report_mutation(
            document(), "create", create_request(workspace_uid=owner),
            report_uid=uid(7), current_source_digest=DIGEST, now=NOW,
        )
        self.assertEqual(planned["report"]["workspace_uid"], owner)

        replayed = prepare_report_replay(
            document(ledger=[receipt()]), workspace_uid=owner, key=KEY,
            method="POST", path=ROUTE, request_digest=DIGEST, now=LATER,
        )
        self.assertEqual(replayed["replay"]["response_status"], 201)

        appended = append_report_receipt(
            document(), workspace_uid=owner, key=KEY, method="POST", path=ROUTE,
            request_digest=DIGEST, response_status=201,
            response_body=response_body(), now=LATER,
        )
        self.assertEqual(len(appended["idempotency"]), 1)

    def test_the_version_seven_identity_the_probe_named_is_admitted(self) -> None:
        self.admit_everywhere(self.V7)

    def test_every_version_nibble_is_admitted_at_every_entry_point(self) -> None:
        for nibble in "0123456789abcdef":
            with self.subTest(version=nibble):
                self.admit_everywhere(version_uid(nibble))

    def test_the_variant_still_carries_the_rfc_and_the_non_nil_rule(self) -> None:
        for value in (
            "00000000-0000-0000-0000-000000000000",
            "00000001-abcd-7000-0000-000000000000",
            "00000001-abcd-7000-c000-000000000000",
            "00000001-abcd-7000-f000-000000000000",
            self.V7.upper(),
            "00000001abcd70008000000000000000",
            "00000001-abcd-7000-8000-0000000000000",
            " 00000001-abcd-7000-8000-000000000000",
        ):
            with self.subTest(uid=value):
                with self.refused("report_body_invalid", "workspace_uid") as caught:
                    validate_reports_document(document(), workspace_uid=value)
                self.assertContentFree(caught.exception, value)

    def test_a_bound_report_must_still_carry_the_admitted_identity(self) -> None:
        with self.refused("report_body_invalid", "workspace_uid"):
            validate_reports_document(
                document([report(workspace_uid=self.V7)]), workspace_uid=WORKSPACE
            )

    def test_a_report_identity_is_still_version_four_only(self) -> None:
        for nibble in "0123456789abcdef":
            candidate = version_uid(nibble)
            with self.subTest(version=nibble):
                bound = document([report(uid=candidate)])
                if nibble == "4":
                    validate_reports_document(bound, workspace_uid=WORKSPACE)
                    continue
                with self.refused("report_body_invalid", "reports"):
                    validate_reports_document(bound, workspace_uid=WORKSPACE)
        with self.refused("report_body_invalid", "report_uid"):
            plan_report_mutation(
                document(), "create", create_request(),
                report_uid=self.V7, current_source_digest=DIGEST, now=NOW,
            )


class CalendarBoundaryTest(ReportCase):
    """Retention is an age, so the representable calendar has no cliff in it.

    Adding thirty days to a stamp near 9999-12-31 overflows, and an overflow is
    not a refusal: it escapes as something the caller never contracted for. The
    window is therefore measured by subtracting the two stamps, which agrees
    with the previous arithmetic everywhere that one did not raise.
    """

    LAST = "9999-12-31T23:59:59Z"
    FIRST = "0001-01-01T00:00:00Z"
    WINDOW = REPORT_LEDGER_RETENTION_DAYS * 86_400

    def replay(self, doc: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "workspace_uid": WORKSPACE, "key": KEY, "method": "POST",
            "path": ROUTE, "request_digest": DIGEST, "now": LATER,
        }
        arguments.update(kwargs)
        return prepare_report_replay(doc, **arguments)

    def append(self, doc: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "workspace_uid": WORKSPACE, "key": "fresh-00000001", "method": "POST",
            "path": ROUTE, "request_digest": DIGEST, "response_status": 201,
            "response_body": response_body(), "now": LATER,
        }
        arguments.update(kwargs)
        return append_report_receipt(doc, **arguments)

    def full_ledger(self, created_at: str) -> dict[str, Any]:
        return document(ledger=[
            receipt(key="fill-{:010d}".format(index), created_at=created_at)
            for index in range(REPORT_LEDGER_MAX_RECORDS)
        ])

    def test_a_receipt_at_the_end_of_the_calendar_survives_and_replays(self) -> None:
        source = document(ledger=[receipt(created_at=self.LAST)])
        result = self.replay(source, now=self.LAST)
        self.assertEqual(len(result["document"]["idempotency"]), 1)
        self.assertEqual(result["replay"]["response_status"], 201)

    def test_a_new_receipt_lands_beside_one_at_the_end_of_the_calendar(self) -> None:
        appended = self.append(
            document(ledger=[receipt(created_at=self.LAST)]), now=self.LAST
        )
        self.assertEqual(len(appended["idempotency"]), 2)
        self.assertEqual(appended["idempotency"][1]["created_at"], self.LAST)

    def test_a_full_ledger_at_the_end_of_the_calendar_reports_a_retry(self) -> None:
        with self.refused("report_idempotency_capacity", "idempotency") as caught:
            self.append(self.full_ledger(self.LAST), now=self.LAST)
        self.assertEqual(caught.exception.retry_after_seconds, self.WINDOW)

    def test_a_receipt_stamped_in_the_future_is_younger_than_nothing(self) -> None:
        result = self.replay(document(ledger=[receipt(created_at=self.LAST)]), now=NOW)
        self.assertEqual(len(result["document"]["idempotency"]), 1)
        self.assertEqual(result["replay"]["response_status"], 201)

    def test_a_future_stamped_full_ledger_still_reports_a_positive_retry(self) -> None:
        with self.refused("report_idempotency_capacity", "idempotency") as caught:
            self.append(self.full_ledger(self.LAST), now=NOW)
        self.assertGreater(caught.exception.retry_after_seconds, self.WINDOW)

    def test_year_one_is_the_control_and_expires_on_the_exact_second(self) -> None:
        source = document(ledger=[receipt(created_at=self.FIRST)])
        inside = self.replay(source, now="0001-01-30T23:59:59Z")
        self.assertEqual(len(inside["document"]["idempotency"]), 1)
        self.assertEqual(inside["replay"]["response_status"], 201)

        exactly = self.replay(source, now="0001-01-31T00:00:00Z")
        self.assertEqual(exactly["document"]["idempotency"], [])
        self.assertIsNone(exactly["replay"])

    def test_the_window_is_the_same_length_at_both_ends_of_the_calendar(self) -> None:
        for created, expiry in (
            (self.FIRST, "0001-01-31T00:00:00Z"),
            (NOW, "2026-10-06T12:00:00Z"),
            ("9999-11-01T12:00:00Z", "9999-12-01T12:00:00Z"),
        ):
            with self.subTest(created=created):
                source = document(ledger=[receipt(created_at=created)])
                self.assertIsNone(self.replay(source, now=expiry)["replay"])
                with self.refused("report_idempotency_capacity", "idempotency") as held:
                    self.append(self.full_ledger(created), now=created)
                self.assertEqual(held.exception.retry_after_seconds, self.WINDOW)

    def test_no_instant_inside_the_calendar_leaks_a_non_refusal(self) -> None:
        edges = (
            self.FIRST, "0001-01-01T00:00:01Z", "0001-03-01T00:00:00Z",
            NOW, "9999-12-01T00:00:00Z", self.LAST,
        )
        for created in edges:
            source = document(ledger=[receipt(created_at=created)])
            validate_reports_document(source, workspace_uid=WORKSPACE)
            for moment in edges:
                with self.subTest(created=created, now=moment):
                    try:
                        self.replay(source, now=moment)
                    except ReportDocumentError:
                        pass
                    try:
                        self.append(source, now=moment)
                    except ReportDocumentError:
                        pass

    def test_a_plan_accepts_any_instant_at_or_after_the_last_write(self) -> None:
        created = self.created()
        body = {"workspace_uid": WORKSPACE, "expected_revision": 1}
        plan = plan_report_mutation(
            created, "finalize", body, report_uid=uid(1), now=self.LAST
        )
        self.assertEqual(plan["report"]["updated_at"], self.LAST)
        with self.refused("report_body_invalid", "now"):
            plan_report_mutation(
                created, "finalize", body, report_uid=uid(1), now=self.FIRST
            )


# 11 and 12. Boundaries with the rest of the product.


class ProductBoundaryTest(ReportCase):
    def test_the_revision_ceiling_matches_the_released_store(self) -> None:
        from workstack.store import MAX_REVISION

        self.assertEqual(MAX_REPORT_DOCUMENT_REVISION, MAX_REVISION)

    def test_the_template_and_markdown_cap_come_from_the_preview_module(self) -> None:
        from workstack import reporting

        self.assertEqual(TEMPLATE_DAILY_V1, reporting.TEMPLATE_DAILY_V1)
        self.assertEqual(MAX_MARKDOWN_CHARS, reporting.MAX_MARKDOWN_CHARS)

    def test_current_store_has_reports_and_preserves_the_historical_roster(self) -> None:
        from workstack.store import DEFAULTS, STORE_SCHEMA_VERSION
        from workstack.store_rosters import V3_DOCUMENT_NAMES, V5_DOCUMENT_NAMES

        self.assertEqual(STORE_SCHEMA_VERSION, 5)
        self.assertEqual(frozenset(DEFAULTS), V5_DOCUMENT_NAMES)
        self.assertEqual(V5_DOCUMENT_NAMES - V3_DOCUMENT_NAMES, {"reports.json"})
        self.assertEqual(DEFAULTS["reports.json"], {"version": 1, "reports": [], "idempotency": []})

    def test_reports_has_an_explicit_semantic_document_name(self) -> None:
        from workstack.storage.document_repository import WorkspaceDocument

        self.assertEqual(WorkspaceDocument.REPORTS.value, "reports")
        self.assertEqual(len(WorkspaceDocument), 9)

    def test_the_error_carries_only_machine_readable_facts(self) -> None:
        with self.refused("report_body_invalid") as caught:
            validate_reports_document(
                document([report(markdown="secret")]), workspace_uid=WORKSPACE
            )
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(
            {"code", "field", "retry_after_seconds"} & set(vars(error)),
            {"code", "field", "retry_after_seconds"},
        )
        self.assertContentFree(error, "secret", WORKSPACE, uid(1), DIGEST)


if __name__ == "__main__":
    unittest.main()
