"""Oracles for deterministic report command composition.

Every fixture is synthetic: the UUIDs are counted, the digests are repeated
hex, the route is the one the contract names and the markdown is a heading.
Nothing here initializes an authority, reads a home directory, touches a live
store or creates `reports.json`, and the two trusted callbacks are recorders
written in this file rather than anything that could reach a real day.

The expectations are restated by hand from the contract — the exact response
key set per operation, the status per operation, the composition order and the
transition table — instead of being read back out of the module, so an
implementation that quietly changes its meaning fails here rather than
agreeing with itself.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from workstack.report_command_service import ReportDocumentError, execute_report_command


ROOT = Path(__file__).resolve().parents[1]
MODULE = "workstack.report_command_service"
WORKSPACE = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
DIGEST = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64
NOW = "2026-09-06T12:00:00Z"
LATER = "2026-09-06T13:00:00Z"
GENERATED = "2026-09-06T11:59:00Z"
DATE = "2026-09-06"
ROUTE = "/api/v1/reports"
KEY = "report-key-0001"

# Restated from the contract, not imported. The response payload is the
# planned report summary — every stored report field except the authored
# history — plus exactly what the operation adds.
SUMMARY_KEYS = {
    "uid", "workspace_uid", "template", "period", "source_digest",
    "source_generated_at", "state", "revision", "archived_from_state",
    "archived_at", "archive_note", "created_at", "updated_at",
}
CONTENT_KEYS = {
    "content_revision", "document_revision", "markdown", "authored_at", "note",
}
DATA_KEYS = {
    "create": SUMMARY_KEYS | {"content_entry", "source_stale"},
    "revise": SUMMARY_KEYS | {"content_entry", "reopened", "source_stale"},
    "finalize": SUMMARY_KEYS | {"source_stale"},
    "archive": SUMMARY_KEYS,
    "restore": SUMMARY_KEYS,
}
STATUSES = {"create": 201, "revise": 200, "finalize": 200, "archive": 200, "restore": 200}
RESULT_KEYS = {"document_to_save", "response_status", "response_body"}
LEDGER_KEYS = {
    "key", "method", "path", "request_digest", "response_status",
    "response_body", "created_at",
}


def canonical(value: Any) -> bytes:
    """The serialization every byte cap and every no-mutation check measures."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def uid(index: int) -> str:
    """Synthetic canonical UUIDv4s, distinguishable by their first group."""

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
        "period": {"kind": "day", "date": DATE},
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


def archived(**overrides: Any) -> dict[str, Any]:
    return report(
        state="archived",
        archived_from_state="draft",
        archived_at=NOW,
        archive_note=None,
        **overrides,
    )


def receipt(**overrides: Any) -> dict[str, Any]:
    value = {
        "key": KEY,
        "method": "POST",
        "path": ROUTE,
        "request_digest": DIGEST,
        "response_status": 201,
        "response_body": {"data": {"uid": uid(1)}, "meta": {"replayed": False}},
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
        "period": {"kind": "day", "date": DATE},
        "source_digest": DIGEST,
        "source_generated_at": GENERATED,
        "markdown": "# first",
    }
    value.update(overrides)
    return value


def transition_request(operation: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"workspace_uid": WORKSPACE, "expected_revision": 1}
    if operation == "revise":
        value["markdown"] = "# second"
        value["note"] = None
    if operation == "archive":
        value["note"] = None
    value.update(overrides)
    return value


def stamp(day: int, hour: int = 12) -> str:
    return "2026-09-{:02d}T{:02d}:00:00Z".format(day, hour)


def holds(value: Any, name: str) -> bool:
    """Whether `name` appears as a key anywhere inside a JSON tree."""

    if isinstance(value, dict):
        return name in value or any(holds(item, name) for item in value.values())
    if isinstance(value, list):
        return any(holds(item, name) for item in value)
    return False


def forbidden_uid() -> str:
    raise AssertionError("allocate_report_uid must not be called")


def forbidden_digest(date: str) -> str:
    raise AssertionError("current_day_digest must not be called for " + date)


class Recorder:
    """The two trusted callbacks, counting every call and what it was asked."""

    def __init__(self, *, digest: str = DIGEST, uids: list[str] | None = None) -> None:
        self.digest = digest
        self.uids = [uid(1)] if uids is None else list(uids)
        self.allocated: list[str] = []
        self.dates: list[str] = []

    def allocate(self) -> str:
        allocated = self.uids[len(self.allocated)]
        self.allocated.append(allocated)
        return allocated

    def day(self, date: str) -> str:
        self.dates.append(date)
        return self.digest


class Boom(Exception):
    """A trusted callback's own failure, which must arrive unchanged."""


class CommandCase(unittest.TestCase):
    maxDiff = None

    @contextmanager
    def refused(self, code: str, field: str | None = None) -> Iterator[Any]:
        with self.assertRaises(ReportDocumentError) as caught:
            yield caught
        self.assertEqual(caught.exception.code, code)
        if field is not None:
            self.assertEqual(caught.exception.field, field)

    def run_command(
        self,
        doc: dict[str, Any],
        operation: str,
        request: dict[str, Any],
        *,
        recorder: Recorder | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_report_uid": None if operation == "create" else uid(1),
            "idempotency_key": KEY,
            "path": ROUTE,
            "request_digest": DIGEST,
            "now": LATER,
            "allocate_report_uid": forbidden_uid,
            "current_day_digest": forbidden_digest,
        }
        if recorder is not None:
            arguments["allocate_report_uid"] = recorder.allocate
            arguments["current_day_digest"] = recorder.day
        arguments.update(overrides)
        return execute_report_command(doc, operation, request, **arguments)

    def created(self, **overrides: Any) -> dict[str, Any]:
        """One saved document holding a single fresh draft, ledger and all."""

        result = self.run_command(
            document(), "create", create_request(**overrides), recorder=Recorder()
        )
        return result["document_to_save"]

    def assertNoContent(self, error: ReportDocumentError) -> None:
        message = str(error)
        self.assertTrue(message and "\n" not in message)
        for secret in (KEY, ROUTE, DIGEST, WORKSPACE, uid(1)):
            self.assertNotIn(secret, message)
            self.assertNotIn(secret, error.field or "")


# 1. The module composes accepted pure code and reaches for nothing else.


class ImportPurityTest(CommandCase):
    STDLIB = {"__future__", "collections", "datetime", "json", "re", "typing"}
    PRODUCT = {
        MODULE,
        "workstack.report_documents",
        "workstack.report_json",
        "workstack.reporting",
    }
    FORBIDDEN_CALLS = (
        "datetime.now", "utcnow", "date.today", "time.time", "time.monotonic",
        "uuid4", "uuid1", "getrandbits", "open(", "os.environ", "Path(",
        "read_text", "write_text", "read_bytes", "write_bytes",
    )

    @staticmethod
    def source(name: str) -> str:
        path = ROOT.joinpath(*name.split(".")).with_suffix(".py")
        return path.read_text(encoding="utf-8")

    def closure(self, entry: str) -> tuple[set[str], set[str]]:
        product: set[str] = set()
        stdlib: set[str] = set()
        pending = [entry]
        while pending:
            name = pending.pop()
            if name in product:
                continue
            product.add(name)
            for node in ast.walk(ast.parse(self.source(name))):
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

    def test_the_closure_is_the_accepted_pure_modules_and_stdlib_only(self) -> None:
        product, stdlib = self.closure(MODULE)
        self.assertEqual(product, self.PRODUCT)
        self.assertTrue(stdlib <= self.STDLIB, sorted(stdlib - self.STDLIB))

    def test_no_store_repository_server_or_frontend_is_reachable(self) -> None:
        reached = self.closure(MODULE)[0] - {MODULE}
        banned = ("store", "storage", "server", "repository", "service", "http",
                  "agent", "maintenance", "transport")
        for name in sorted(reached):
            for word in banned:
                self.assertNotIn(word, name.rsplit(".", 1)[-1])

    def test_the_module_never_reads_a_clock_identity_or_path(self) -> None:
        text = self.source(MODULE)
        for name in self.FORBIDDEN_CALLS:
            self.assertNotIn(name, text)

    def test_no_executable_string_literal_names_a_file(self) -> None:
        """`reports.json` may be described in prose; it may never be built."""

        tree = ast.parse(self.source(MODULE))
        documentation = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in documentation:
                continue
            for fragment in (".json", "/", "\\", "~", ".py"):
                self.assertNotIn(fragment, node.value)

    def test_only_one_public_name_is_exported_beside_the_shared_error(self) -> None:
        module = importlib.import_module(MODULE)
        self.assertEqual(
            sorted(module.__all__), ["ReportDocumentError", "execute_report_command"]
        )


# 2. The public function's shape is itself part of the frozen contract.


class SignatureTest(CommandCase):
    def test_the_parameters_are_exactly_the_contract_and_nothing_else(self) -> None:
        signature = inspect.signature(execute_report_command)
        positional = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
        ]
        keyword = sorted(
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        )
        self.assertEqual(positional, ["document", "operation", "request"])
        self.assertEqual(
            keyword,
            [
                "allocate_report_uid", "current_day_digest", "idempotency_key",
                "now", "path", "request_digest", "target_report_uid",
            ],
        )
        self.assertEqual(len(signature.parameters), 10)

    def test_no_parameter_lets_a_caller_vary_the_method(self) -> None:
        self.assertNotIn("method", inspect.signature(execute_report_command).parameters)
        with self.assertRaises(TypeError):
            self.run_command(document(), "create", create_request(), method="PUT")

    def test_the_ledger_records_the_one_verb_the_contract_fixes(self) -> None:
        saved = self.created()
        self.assertEqual(saved["idempotency"][-1]["method"], "POST")
        self.assertEqual(saved["idempotency"][-1]["path"], ROUTE)
        self.assertEqual(saved["idempotency"][-1]["request_digest"], DIGEST)


# 3. Create: status, exact keys, one allocation, one source read.


class CreateTest(CommandCase):
    def test_a_create_answers_201_with_the_exact_data_keys(self) -> None:
        recorder = Recorder()
        result = self.run_command(
            document(), "create", create_request(), recorder=recorder
        )

        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["response_status"], 201)
        body = result["response_body"]
        self.assertEqual(set(body), {"data", "meta"})
        self.assertEqual(body["meta"], {"replayed": False})
        self.assertEqual(set(body["data"]), DATA_KEYS["create"])
        self.assertEqual(set(body["data"]["content_entry"]), CONTENT_KEYS)

    def test_the_created_report_carries_the_allocated_identity_and_the_instant(self) -> None:
        recorder = Recorder()
        data = self.run_command(
            document(), "create", create_request(), recorder=recorder
        )["response_body"]["data"]

        self.assertEqual(recorder.allocated, [uid(1)])
        self.assertEqual(data["uid"], uid(1))
        self.assertEqual(data["state"], "draft")
        self.assertEqual(data["revision"], 1)
        self.assertEqual(data["created_at"], LATER)
        self.assertEqual(data["updated_at"], LATER)
        self.assertEqual(data["period"], {"kind": "day", "date": DATE})
        self.assertEqual(data["content_entry"]["content_revision"], 1)
        self.assertEqual(data["content_entry"]["markdown"], "# first")
        self.assertEqual(data["content_entry"]["authored_at"], LATER)

    def test_a_create_reads_its_day_exactly_once_and_is_never_stale(self) -> None:
        recorder = Recorder()
        result = self.run_command(
            document(), "create", create_request(), recorder=recorder
        )
        self.assertEqual(recorder.dates, [DATE])
        self.assertIs(result["response_body"]["data"]["source_stale"], False)

    def test_a_moved_source_refuses_the_create_and_saves_nothing(self) -> None:
        recorder = Recorder(digest=OTHER_DIGEST)
        source = document()
        before = canonical(source)

        with self.refused("report_source_changed", "source_digest") as caught:
            self.run_command(source, "create", create_request(), recorder=recorder)

        self.assertNoContent(caught.exception)
        self.assertEqual(recorder.dates, [DATE])
        self.assertEqual(recorder.allocated, [uid(1)])
        self.assertEqual(canonical(source), before)

    def test_the_identity_is_allocated_once_even_though_the_plan_runs_twice(self) -> None:
        recorder = Recorder(uids=[uid(1), uid(2)])
        result = self.run_command(
            document(), "create", create_request(), recorder=recorder
        )
        self.assertEqual(recorder.allocated, [uid(1)])
        self.assertEqual(result["document_to_save"]["reports"][0]["uid"], uid(1))


# 4. Ordering: target, limit and collision settle before any source is read.


class OrderingTest(CommandCase):
    def test_a_duplicate_period_refuses_before_the_source_is_ever_read(self) -> None:
        recorder = Recorder(uids=[uid(2)])
        source = document(reports=[report()])

        with self.refused("report_duplicate_period", "period"):
            self.run_command(source, "create", create_request(), recorder=recorder)

        self.assertEqual(recorder.dates, [])
        self.assertEqual(recorder.allocated, [uid(2)])

    def test_a_colliding_allocation_refuses_before_the_source_is_ever_read(self) -> None:
        recorder = Recorder()
        held = report(period={"kind": "day", "date": "2026-09-05"})
        source = document(reports=[held])

        with self.refused("report_body_invalid", "report_uid"):
            self.run_command(source, "create", create_request(), recorder=recorder)

        self.assertEqual(recorder.dates, [])

    def test_a_stale_revision_refuses_before_the_source_is_ever_read(self) -> None:
        source = document(reports=[report()])
        with self.refused("report_revision_conflict", "expected_revision"):
            self.run_command(
                source,
                "revise",
                transition_request("revise", expected_revision=7),
                recorder=Recorder(),
            )

    def test_a_forbidden_transition_refuses_before_the_source_is_ever_read(self) -> None:
        recorder = Recorder()
        source = document(reports=[archived()])

        with self.refused("report_state_invalid", "document"):
            self.run_command(
                source, "finalize", transition_request("finalize"), recorder=recorder
            )

        self.assertEqual(recorder.dates, [])

    def test_a_replay_precedes_the_transition_check_entirely(self) -> None:
        """An archived report still replays a finalize that already answered."""

        source = document(reports=[archived()], ledger=[receipt(response_status=200)])

        result = self.run_command(source, "finalize", transition_request("finalize"))

        self.assertIsNone(result["document_to_save"])
        self.assertEqual(result["response_status"], 200)
        self.assertIs(result["response_body"]["meta"]["replayed"], True)


# 5. Every transition: status, exact keys, staleness, source reads.


class TransitionTest(CommandCase):
    def transition(
        self,
        operation: str,
        *,
        reports: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> tuple[dict[str, Any], Recorder]:
        recorder = Recorder(**{k: v for k, v in overrides.items() if k == "digest"})
        held = reports if reports is not None else [
            archived() if operation == "restore" else report()
        ]
        result = self.run_command(
            document(reports=held),
            operation,
            transition_request(operation),
            recorder=recorder,
        )
        return result, recorder

    def test_each_operation_answers_its_status_with_its_exact_data_keys(self) -> None:
        for operation in ("revise", "finalize", "archive", "restore"):
            with self.subTest(operation=operation):
                result, _ = self.transition(operation)
                self.assertEqual(result["response_status"], STATUSES[operation])
                self.assertEqual(set(result["response_body"]["data"]), DATA_KEYS[operation])
                self.assertEqual(result["response_body"]["meta"], {"replayed": False})

    def test_the_transition_table_is_the_accepted_one(self) -> None:
        expected = {
            "revise": "draft", "finalize": "finalized",
            "archive": "archived", "restore": "draft",
        }
        for operation, state in expected.items():
            with self.subTest(operation=operation):
                result, _ = self.transition(operation)
                data = result["response_body"]["data"]
                self.assertEqual(data["state"], state)
                self.assertEqual(data["revision"], 2)
                self.assertEqual(data["updated_at"], LATER)

    def test_revise_reports_reopened_only_when_it_reopened_a_finalized_report(self) -> None:
        for state, reopened in (("draft", False), ("finalized", True)):
            with self.subTest(state=state):
                result, _ = self.transition("revise", reports=[report(state=state)])
                self.assertIs(result["response_body"]["data"]["reopened"], reopened)

    def test_revise_appends_the_next_content_entry(self) -> None:
        result, _ = self.transition("revise")
        entry = result["response_body"]["data"]["content_entry"]
        self.assertEqual(set(entry), CONTENT_KEYS)
        self.assertEqual(entry["content_revision"], 2)
        self.assertEqual(entry["document_revision"], 2)
        self.assertEqual(entry["markdown"], "# second")

    def test_revise_and_finalize_read_their_day_exactly_once(self) -> None:
        for operation in ("revise", "finalize"):
            with self.subTest(operation=operation):
                _, recorder = self.transition(operation)
                self.assertEqual(recorder.dates, [DATE])

    def test_staleness_is_the_held_digest_against_the_stored_one(self) -> None:
        for operation in ("revise", "finalize"):
            for digest, stale in ((DIGEST, False), (OTHER_DIGEST, True)):
                with self.subTest(operation=operation, stale=stale):
                    result, _ = self.transition(operation, digest=digest)
                    self.assertIs(result["response_body"]["data"]["source_stale"], stale)

    def test_a_moved_source_never_refuses_a_transition(self) -> None:
        result, _ = self.transition("revise", digest=OTHER_DIGEST)
        self.assertEqual(result["response_status"], 200)
        self.assertIsNotNone(result["document_to_save"])

    def test_archive_and_restore_omit_every_source_field_and_read_nothing(self) -> None:
        for operation in ("archive", "restore"):
            with self.subTest(operation=operation):
                held = [archived()] if operation == "restore" else [report()]
                result = self.run_command(
                    document(reports=held), operation, transition_request(operation)
                )
                data = result["response_body"]["data"]
                self.assertNotIn("source_stale", data)
                self.assertNotIn("content_entry", data)
                self.assertNotIn("reopened", data)
                self.assertEqual(set(data), SUMMARY_KEYS)

    def test_no_transition_allocates_an_identity(self) -> None:
        for operation in ("revise", "finalize", "archive", "restore"):
            with self.subTest(operation=operation):
                held = [archived()] if operation == "restore" else [report()]
                self.run_command(
                    document(reports=held),
                    operation,
                    transition_request(operation),
                    allocate_report_uid=forbidden_uid,
                    current_day_digest=Recorder().day,
                )


# 6. The response never carries authored history, and neither does the receipt.


class ReceiptTest(CommandCase):
    def test_a_successful_miss_always_saves_its_own_receipt(self) -> None:
        result = self.run_command(
            document(), "create", create_request(), recorder=Recorder()
        )
        saved = result["document_to_save"]
        self.assertEqual(len(saved["idempotency"]), 1)
        stored = saved["idempotency"][0]
        self.assertEqual(set(stored), LEDGER_KEYS)
        self.assertEqual(stored["key"], KEY)
        self.assertEqual(stored["response_status"], 201)
        self.assertEqual(stored["created_at"], LATER)
        self.assertEqual(stored["response_body"], result["response_body"])

    def test_no_revisions_key_reaches_a_response_or_a_receipt(self) -> None:
        for operation in ("create", "revise", "finalize", "archive", "restore"):
            with self.subTest(operation=operation):
                if operation == "create":
                    source, request = document(), create_request()
                else:
                    held = [archived()] if operation == "restore" else [report()]
                    source, request = document(reports=held), transition_request(operation)
                result = self.run_command(
                    source, operation, request, recorder=Recorder()
                )
                self.assertFalse(holds(result["response_body"], "revisions"))
                stored = result["document_to_save"]["idempotency"][-1]
                self.assertFalse(holds(stored["response_body"], "revisions"))

    def test_the_saved_document_still_keeps_the_authored_history(self) -> None:
        saved = self.run_command(
            document(reports=[report()]),
            "revise",
            transition_request("revise"),
            recorder=Recorder(),
        )["document_to_save"]
        self.assertEqual(len(saved["reports"][0]["revisions"]), 2)


# 7. Replay: recorded answer, no callbacks, no save.


class ReplayTest(CommandCase):
    def test_the_second_identical_command_replays_and_saves_nothing(self) -> None:
        first = self.run_command(
            document(), "create", create_request(), recorder=Recorder()
        )

        second = self.run_command(
            first["document_to_save"], "create", create_request(), now=stamp(7)
        )

        self.assertIsNone(second["document_to_save"])
        self.assertEqual(second["response_status"], first["response_status"])
        self.assertEqual(second["response_body"]["data"], first["response_body"]["data"])
        self.assertEqual(second["response_body"]["meta"], {"replayed": True})

    def test_a_replay_calls_neither_trusted_callback(self) -> None:
        saved = self.created()
        # `forbidden_uid` and `forbidden_digest` are the defaults, so a call
        # to either fails this test outright.
        self.run_command(saved, "create", create_request(), now=stamp(7))

    def test_a_replay_survives_ten_later_mutations_of_the_same_report(self) -> None:
        first = self.run_command(
            document(), "create", create_request(), recorder=Recorder()
        )
        moved = first["document_to_save"]
        for index, operation in enumerate(("revise", "finalize", "archive")):
            moved = self.run_command(
                moved,
                operation,
                transition_request(operation, expected_revision=index + 1),
                recorder=Recorder(),
                idempotency_key="later-{:08d}".format(index),
                now=stamp(7, 12 + index),
            )["document_to_save"]

        replay = self.run_command(moved, "create", create_request(), now=stamp(8))

        self.assertIsNone(replay["document_to_save"])
        self.assertEqual(replay["response_body"]["data"], first["response_body"]["data"])
        self.assertEqual(moved["reports"][0]["state"], "archived")

    def test_a_replay_survives_the_source_moving_underneath_it(self) -> None:
        saved = self.created()
        replay = self.run_command(
            saved,
            "create",
            create_request(),
            recorder=Recorder(digest=OTHER_DIGEST),
            now=stamp(7),
        )
        self.assertIsNone(replay["document_to_save"])
        self.assertIs(replay["response_body"]["data"]["source_stale"], False)

    def test_the_recorded_receipt_is_left_exactly_as_it_was(self) -> None:
        saved = self.created()
        before = canonical(saved)
        self.run_command(saved, "create", create_request(), now=stamp(7))
        self.assertEqual(canonical(saved), before)
        self.assertIs(saved["idempotency"][0]["response_body"]["meta"]["replayed"], False)


# 8. Idempotency conflict, expiry and capacity are the accepted ones.


class LedgerTest(CommandCase):
    def test_the_same_key_with_a_different_request_is_a_conflict(self) -> None:
        saved = self.created()
        for changed in ({"request_digest": OTHER_DIGEST}, {"path": "/api/v1/reports/x"}):
            with self.subTest(changed=str(changed)):
                with self.refused("idempotency_conflict", "key") as caught:
                    self.run_command(
                        saved, "create", create_request(), now=stamp(7), **changed
                    )
                self.assertNoContent(caught.exception)

    def test_an_expired_receipt_is_a_miss_and_is_pruned_from_what_is_saved(self) -> None:
        stale = receipt(created_at=stamp(1), request_digest=OTHER_DIGEST)
        result = self.run_command(
            document(ledger=[stale]),
            "create",
            create_request(),
            recorder=Recorder(),
            now="2026-10-06T12:00:00Z",
        )
        ledger = result["document_to_save"]["idempotency"]
        self.assertEqual([record["key"] for record in ledger], [KEY])
        self.assertEqual(ledger[0]["created_at"], "2026-10-06T12:00:00Z")

    def test_an_unexpired_receipt_survives_alongside_the_new_one(self) -> None:
        older = receipt(key="older-00000001", created_at=NOW)
        result = self.run_command(
            document(ledger=[older]), "create", create_request(), recorder=Recorder()
        )
        ledger = result["document_to_save"]["idempotency"]
        self.assertEqual([record["key"] for record in ledger], ["older-00000001", KEY])

    def test_a_full_ledger_refuses_with_a_retry_after_rather_than_evicting(self) -> None:
        records = [
            receipt(key="fill-{:010d}".format(index), created_at=NOW)
            for index in range(1_000)
        ]
        source = document(ledger=records)
        before = canonical(source)

        with self.refused("report_idempotency_capacity", "idempotency") as caught:
            self.run_command(source, "create", create_request(), recorder=Recorder())

        self.assertGreater(caught.exception.retry_after_seconds, 0)
        self.assertNoContent(caught.exception)
        self.assertEqual(canonical(source), before)


# 9. Identity admission: create allocates, everything else addresses.


class TargetIdentityTest(CommandCase):
    def test_a_create_carrying_a_target_is_refused(self) -> None:
        with self.refused("report_body_invalid", "report_uid") as caught:
            self.run_command(
                document(),
                "create",
                create_request(),
                recorder=Recorder(),
                target_report_uid=uid(1),
            )
        self.assertNoContent(caught.exception)

    def test_a_transition_without_a_target_is_refused(self) -> None:
        for operation in ("revise", "finalize", "archive", "restore"):
            with self.subTest(operation=operation):
                with self.refused("report_body_invalid", "report_uid"):
                    self.run_command(
                        document(reports=[report()]),
                        operation,
                        transition_request(operation),
                        recorder=Recorder(),
                        target_report_uid=None,
                    )

    def test_an_unknown_target_is_not_found(self) -> None:
        with self.refused("report_not_found", "report_uid") as caught:
            self.run_command(
                document(reports=[report()]),
                "finalize",
                transition_request("finalize"),
                recorder=Recorder(),
                target_report_uid=uid(9),
            )
        self.assertNoContent(caught.exception)


# 10. Existing refusals pass through; no new code is ever introduced.


class RefusalTest(CommandCase):
    def test_body_defects_are_refused_by_the_accepted_admission(self) -> None:
        cases = (
            ("create", create_request(template="weekly-v1"), "report_template_unsupported"),
            ("create", create_request(markdown=None), "report_body_invalid"),
            (
                "create",
                create_request(period={"kind": "week", "date": DATE}),
                "report_body_invalid",
            ),
            ("promote", create_request(), "report_body_invalid"),
        )
        for operation, request, code in cases:
            with self.subTest(code=code, operation=operation):
                with self.refused(code) as caught:
                    self.run_command(
                        document(), operation, request, recorder=Recorder()
                    )
                self.assertNoContent(caught.exception)

    def test_a_workspace_the_document_does_not_belong_to_is_refused(self) -> None:
        other = "1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d"
        with self.refused("report_body_invalid", "workspace_uid"):
            self.run_command(
                document(reports=[report()]),
                "create",
                create_request(
                    workspace_uid=other,
                    period={"kind": "day", "date": "2026-09-05"},
                ),
                recorder=Recorder(),
            )

    def test_only_the_accepted_codes_can_leave_this_module(self) -> None:
        accepted = {
            "report_body_invalid", "report_template_unsupported", "report_not_found",
            "report_state_invalid", "report_revision_conflict", "report_duplicate_period",
            "report_revision_limit", "report_document_limit", "report_storage_full",
            "report_source_changed", "idempotency_conflict", "report_idempotency_capacity",
        }
        text = ImportPurityTest.source(MODULE)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(("report_", "idempotency_")):
                    self.assertIn(node.value, accepted | {"report_uid"})


# 11. Trusted callbacks are not wrapped, and their failure saves nothing.


class CallbackFailureTest(CommandCase):
    def test_an_allocation_failure_arrives_unchanged_with_nothing_saved(self) -> None:
        source = document()
        before = canonical(source)

        def explode() -> str:
            raise Boom("allocation")

        with self.assertRaises(Boom):
            self.run_command(
                source, "create", create_request(), allocate_report_uid=explode
            )
        self.assertEqual(canonical(source), before)

    def test_a_source_read_failure_arrives_unchanged_with_nothing_saved(self) -> None:
        def explode(date: str) -> str:
            raise Boom(date)

        for operation in ("create", "revise", "finalize"):
            with self.subTest(operation=operation):
                if operation == "create":
                    source, request = document(), create_request()
                else:
                    source = document(reports=[report()])
                    request = transition_request(operation)
                before = canonical(source)
                with self.assertRaises(Boom):
                    self.run_command(
                        source,
                        operation,
                        request,
                        allocate_report_uid=Recorder().allocate,
                        current_day_digest=explode,
                    )
                self.assertEqual(canonical(source), before)


# 12. Nothing in, nothing out, is shared or mutated.


class IsolationTest(CommandCase):
    def test_no_argument_is_mutated_by_a_successful_command(self) -> None:
        source = document(reports=[report()])
        request = transition_request("revise")
        before = (canonical(source), canonical(request))

        self.run_command(source, "revise", request, recorder=Recorder())

        self.assertEqual((canonical(source), canonical(request)), before)

    def test_the_response_shares_no_mutable_reference_with_the_request(self) -> None:
        request = create_request()
        data = self.run_command(
            document(), "create", request, recorder=Recorder()
        )["response_body"]["data"]

        self.assertIsNot(data["period"], request["period"])
        data["period"]["date"] = "1999-01-01"
        self.assertEqual(request["period"]["date"], DATE)

    def test_the_response_shares_no_mutable_reference_with_what_is_saved(self) -> None:
        result = self.run_command(
            document(), "create", create_request(), recorder=Recorder()
        )
        saved = result["document_to_save"]
        body = result["response_body"]

        self.assertIsNot(body, saved["idempotency"][0]["response_body"])
        body["data"]["period"]["date"] = "1999-01-01"
        body["data"]["content_entry"]["markdown"] = "# tampered"
        self.assertEqual(saved["reports"][0]["period"]["date"], DATE)
        stored = saved["idempotency"][0]["response_body"]
        self.assertEqual(stored["data"]["period"]["date"], DATE)
        self.assertEqual(saved["reports"][0]["revisions"][0]["markdown"], "# first")

    def test_a_replayed_body_shares_nothing_with_the_document_it_came_from(self) -> None:
        saved = self.created()
        replay = self.run_command(saved, "create", create_request(), now=stamp(7))
        body = replay["response_body"]

        self.assertIsNot(body, saved["idempotency"][0]["response_body"])
        body["meta"]["replayed"] = "tampered"
        self.assertIs(saved["idempotency"][0]["response_body"]["meta"]["replayed"], False)

    def test_the_same_arguments_compose_the_same_answer_twice(self) -> None:
        first = self.run_command(
            document(reports=[report()]),
            "finalize",
            transition_request("finalize"),
            recorder=Recorder(),
        )
        second = self.run_command(
            document(reports=[report()]),
            "finalize",
            transition_request("finalize"),
            recorder=Recorder(),
        )
        self.assertEqual(canonical(first), canonical(second))


if __name__ == "__main__":
    unittest.main()
