"""Real-Store oracles for the released report repository and query adapter.

Every claim about persistence here is made against a genuine disposable
`Store` under a temporary directory and the real `WorkStack`, which now
composes the mixin through `service.py` itself, so `_ReportStack` is that
released host under a local name. No fake repository, in-memory document map
or stubbed transaction stands in for the store: the success, refusal, crash,
restart and byte-equality cases all commit through the actual journal and
read the bytes back off disk.

Two kinds of doubles do appear, and neither carries a claim. The composition
refusals need compositions that are genuinely wrong — a released adapter bound
to a *different* released Store, an independent object that merely satisfies
the repository protocol, and a real experimental v4 application built from a
converted v3 store — so those are constructed for real. And the two private
seams the contract names, `_utc_now` and `_allocate_report_uid`, are patched
to make instants and identities deterministic, exactly as the contract says
tests may.

Expectations are restated from the contract by hand — the admission order, the
status per operation, the response key sets, the operation id, the caps — so
an implementation that changes its meaning fails here rather than agreeing
with itself.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from workstack import report_repository_service as adapter
from workstack.capture import canonical_digest
from workstack.checkpoint_change import build_checkpoint_facts
from workstack.report_documents import (
    MAX_REPORTS_BYTES,
    REPORT_LEDGER_MAX_RECORDS,
    ReportDocumentError,
)
from workstack.report_documents import _LEDGER_KEY as MODEL_LEDGER_KEY
from workstack.report_queries import LIST_PAGE_SIZE, ReportQueryError
from workstack.report_repository_service import (
    ReportRepositoryServiceError,
    ReportRepositoryServiceMixin,
)
from workstack.reporting import MAX_MARKDOWN_CHARS, TEMPLATE_DAILY_V1
from workstack.reporting_http import day_source_digest
from workstack.service import WorkStack
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.document_repository import (
    StoreDocumentRepository,
    WorkspaceDocument,
)
from workstack.storage.experimental_application import (
    create_experimental_v4_application,
)
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.store import DEFAULTS, Store


MODULE_PATH = Path(adapter.__file__)
DATE = "2026-09-06"
OTHER_DATE = "2026-09-05"
GENERATED = "2026-09-06T11:59:00Z"
NOW = "2026-09-06T12:00:00Z"
LATER = "2026-09-06T13:00:00Z"
MUCH_LATER = "2026-10-20T13:00:00Z"
ROUTE = "/api/v1/reports"
UID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_UID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FOREIGN_WORKSPACE = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"

# Restated from the contract rather than imported from the adapter.
SERVICE_CODES = {
    "report_capability_unavailable",
    "workspace_mismatch",
    "store_sync_required",
    "idempotency_key_required",
    "invalid_idempotency_key",
}
SUMMARY_KEYS = {
    "uid", "workspace_uid", "template", "period", "source_digest",
    "source_generated_at", "state", "revision", "archived_from_state",
    "archived_at", "archive_note", "created_at", "updated_at",
}
DATA_KEYS = {
    "create": SUMMARY_KEYS | {"content_entry", "source_stale"},
    "revise": SUMMARY_KEYS | {"content_entry", "reopened", "source_stale"},
    "finalize": SUMMARY_KEYS | {"source_stale"},
    "archive": SUMMARY_KEYS,
    "restore": SUMMARY_KEYS,
}
STATUSES = {"create": 201, "revise": 200, "finalize": 200, "archive": 200, "restore": 200}
LIST_KEYS = {"workspace_uid", "reports", "omitted_count", "cursor"}
READ_KEYS = {
    "uid", "template", "period", "state", "revision", "content_revision",
    "source_digest", "archived_from_state", "created_at", "updated_at",
    "revisions", "source_stale",
}
OTHER_DOCUMENTS = tuple(sorted(name for name in DEFAULTS if name != "reports.json"))


class _ReportStack(WorkStack):
    """The real released host, which `service.py` has now activated the mixin on."""


class _GenericRepository:
    """Satisfies the repository protocol and nothing else — not a capability."""

    def __init__(self, inner: StoreDocumentRepository) -> None:
        self._inner = inner
        self._store = inner._store

    def load(self, document: WorkspaceDocument) -> dict[str, Any]:
        return self._inner.load(document)

    def save(self, document: WorkspaceDocument, value: object) -> None:
        self._inner.save(document, value)

    def save_many(self, writes: Any, operation_id: str | None = None) -> None:
        self._inner.save_many(writes, operation_id)

    def total_bytes(self) -> int:
        return self._inner.total_bytes()


class _Trace:
    """What the adapter actually did, in order, for one call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == kind]

    def reports_loads(self) -> list[tuple[Any, ...]]:
        return [call for call in self.of("load") if call[1] == "reports"]

    def saves(self) -> list[tuple[Any, ...]]:
        return self.of("save")

    def projections(self) -> list[tuple[Any, ...]]:
        return self.of("project")

    def outer(self, kind: str) -> list[tuple[Any, ...]]:
        """Only the entries that opened a lock nothing else was already holding."""

        return [call for call in self.of(kind) if call[1] == 0]


def _digest_of(body: object) -> str:
    return canonical_digest(body)


def _canonical_length(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _mutable_ids(value: object, found: set[int] | None = None) -> set[int]:
    """Identities of every container in a tree, for proving detachment."""

    found = set() if found is None else found
    if type(value) is dict:
        found.add(id(value))
        for item in value.values():
            _mutable_ids(item, found)
    elif type(value) is list:
        found.add(id(value))
        for item in value:
            _mutable_ids(item, found)
    return found


class _ServiceCase(unittest.TestCase):
    """One disposable Store, one real host, and the helpers every case reuses."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.stack = _ReportStack(Store(self.root))
        self.owner = self.stack.store.readiness.workspace_uid

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- fixtures --------------------------------------------------------
    def source_digest(self, date: str = DATE) -> str:
        projection = self.stack.review_projection(date, 1)
        return day_source_digest(date=date, day=projection["day"])

    def create_body(
        self,
        *,
        date: str = DATE,
        digest: str | None = None,
        markdown: str = "# Day",
        workspace_uid: str | None = None,
    ) -> dict[str, Any]:
        return {
            "workspace_uid": self.owner if workspace_uid is None else workspace_uid,
            "template": TEMPLATE_DAILY_V1,
            "period": {"kind": "day", "date": date},
            "source_digest": self.source_digest(date) if digest is None else digest,
            "source_generated_at": GENERATED,
            "markdown": markdown,
        }

    def transition_body(self, revision: int, **extra: Any) -> dict[str, Any]:
        return {"workspace_uid": self.owner, "expected_revision": revision, **extra}

    # -- tracing ---------------------------------------------------------
    @contextmanager
    def traced(
        self, *, now: str = NOW, uid: str = UID, stack: Any = None
    ) -> Iterator[_Trace]:
        host = self.stack if stack is None else stack
        trace = _Trace()
        real_load = StoreDocumentRepository.load
        real_save = StoreDocumentRepository.save_many
        real_projection = type(host).review_projection
        real_transaction = Store.transaction
        real_read = Store.consistent_read

        def transaction(store: Store) -> Any:
            trace.calls.append(("transaction", int(getattr(store._local, "depth", 0))))
            return real_transaction(store)

        def consistent_read(store: Store) -> Any:
            trace.calls.append(("read", int(getattr(store._local, "depth", 0))))
            return real_read(store)

        def load(repository: Any, document: WorkspaceDocument) -> dict[str, Any]:
            trace.calls.append(("load", document.value))
            return real_load(repository, document)

        def save_many(
            repository: Any, writes: Any, operation_id: str | None = None
        ) -> None:
            names = tuple(sorted(item.value for item in writes))
            trace.calls.append(("save", names, operation_id))
            real_save(repository, writes, operation_id)

        def projection(self: Any, date: Any, days: int = 7) -> dict[str, Any]:
            trace.calls.append(("project", date, days))
            return real_projection(self, date, days)

        def utc_now() -> str:
            trace.calls.append(("now", now))
            return now

        def allocate() -> str:
            trace.calls.append(("uid", uid))
            return uid

        with mock.patch.object(StoreDocumentRepository, "load", load), \
                mock.patch.object(StoreDocumentRepository, "save_many", save_many), \
                mock.patch.object(Store, "transaction", transaction), \
                mock.patch.object(Store, "consistent_read", consistent_read), \
                mock.patch.object(type(host), "review_projection", projection), \
                mock.patch.object(adapter, "_utc_now", utc_now), \
                mock.patch.object(adapter, "_allocate_report_uid", allocate):
            yield trace

    # -- calls -----------------------------------------------------------
    def command(
        self,
        operation: object,
        request: object,
        key: object,
        *,
        target: object = None,
        path: object = ROUTE,
        request_digest: object = None,
        workspace_uid: object = None,
        stack: Any = None,
    ) -> dict[str, Any]:
        host = self.stack if stack is None else stack
        return host.execute_report_document_command(
            operation,
            request,
            workspace_uid=self.owner if workspace_uid is None else workspace_uid,
            target_report_uid=target,
            idempotency_key=key,
            path=path,
            request_digest=(
                _digest_of(request) if request_digest is None else request_digest
            ),
        )

    def create(self, key: str = "report-create-0001", **kwargs: Any) -> dict[str, Any]:
        body = kwargs.pop("body", None) or self.create_body(**kwargs)
        with self.traced(now=kwargs.pop("now", NOW)):
            return self.command("create", body, key)

    # -- store observation ------------------------------------------------
    def other_document_bytes(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in OTHER_DOCUMENTS}

    def stored_reports(self) -> dict[str, Any]:
        return json.loads((self.root / "reports.json").read_text(encoding="utf-8"))

    def spoil_store(self) -> None:
        """A change Work Stack did not make, valid enough to stay readable."""

        path = self.root / "workspace.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["name"] = "Renamed Outside Work Stack"
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def assert_service_error(self, raised: Any, code: str) -> None:
        error = raised.exception
        self.assertIsInstance(error, ReportRepositoryServiceError)
        self.assertEqual(error.code, code)
        self.assertEqual(str(error), adapter._MESSAGES[code])
        forbidden = (
            self.owner, str(self.root), "reports.json", UID, "sha256:",
            "StoreDocumentRepository", "ExperimentalV4StoreAdapter", "workspace.json",
        )
        for secret in forbidden:
            self.assertNotIn(secret, str(error))


# ---------------------------------------------------------------- capability


class CapabilityAdmissionTest(_ServiceCase):
    def test_attributed_released_composition_is_admitted(self) -> None:
        result = self.create()
        self.assertEqual(result["status"], 201)
        self.assertIs(self.stack.documents._store, self.stack.store)

    @staticmethod
    def document_bytes(roots: tuple[Path, ...]) -> dict[str, bytes]:
        return {
            f"{root}/{name}": (root / name).read_bytes()
            for root in roots
            for name in sorted(DEFAULTS)
            if (root / name).exists()
        }

    def refuse_every_surface(self, stack: Any, *roots: Path) -> None:
        watched = roots or (self.root,)
        before = self.document_bytes(watched)
        with self.traced(stack=stack) as trace:
            with self.assertRaises(ReportRepositoryServiceError) as listed:
                stack.list_report_documents(workspace_uid=self.owner)
            with self.assertRaises(ReportRepositoryServiceError) as read:
                stack.get_report_document(workspace_uid=self.owner, report_uid=UID)
            with self.assertRaises(ReportRepositoryServiceError) as written:
                self.command("create", self.create_body(), "report-key-0001", stack=stack)
        for raised in (listed, read, written):
            self.assert_service_error(raised, "report_capability_unavailable")
        self.assertEqual(trace.reports_loads(), [])
        self.assertEqual(trace.saves(), [])
        self.assertEqual(self.document_bytes(watched), before)

    def test_released_adapter_bound_to_a_different_store_is_refused(self) -> None:
        other = Path(self.temporary.name) / "other"
        foreign = Store(other)
        stack = _ReportStack(
            Store(self.root), document_repository=StoreDocumentRepository(foreign)
        )
        self.assertIsNot(stack.documents._store, stack.store)
        self.assertIs(type(stack.documents), StoreDocumentRepository)
        self.assertIs(type(stack.documents._store), Store)
        self.refuse_every_surface(stack, self.root, other)

    def test_generic_repository_satisfying_the_protocol_is_refused(self) -> None:
        store = Store(self.root)
        stack = _ReportStack(
            store, document_repository=_GenericRepository(StoreDocumentRepository(store))
        )
        self.assertIs(stack.documents._store, stack.store)
        for name in ("load", "save", "save_many", "total_bytes"):
            self.assertTrue(callable(getattr(stack.documents, name)))
        self.refuse_every_surface(stack)

    def test_experimental_v4_adapter_is_refused_before_any_report_load(self) -> None:
        stack = _V4Composition(self).stack()
        self.assertIsNot(type(stack.store), Store)
        self.assertIs(type(stack.documents), StoreDocumentRepository)
        self.refuse_every_surface(stack, self.root)

    def test_admission_precedes_every_document_open(self) -> None:
        source = inspect.getsource(adapter)
        self.assertIn("_admitted_repository(self)", source)
        self.assertEqual(source.count("_admitted_repository(self)"), 3)


class _V4Composition:
    """A genuine experimental v4 application converted from a real v3 store."""

    def __init__(self, case: _ServiceCase) -> None:
        self.case = case
        self.base = Path(case.temporary.name) / "v4"
        self.base.mkdir()
        legacy = WorkStack(Store(self.base / "v3"))
        legacy.add_task("Conversion source task")
        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(documents, candidate_created_at=NOW)
        self.authority = self.base / "authority"
        self.authority.mkdir()
        self._write_conversion()
        self.runtime = resolve_runtime_authority(
            self.authority,
            self.base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.authority), generation=0),
            expected_digest=None,
        )
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )

    def _write(self, relative: str, body: bytes) -> None:
        path = self.authority.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def _write_conversion(self) -> None:
        conversion = self.conversion
        self._write("store.json", canonical_json_bytes(dict(conversion.store)))
        self._write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
        for kind, records in conversion.records.items():
            for record in records:
                uid = str(record["uid"])
                self._write(
                    f"records/{kind}/{uid[:2]}/{uid}.json",
                    canonical_json_bytes(dict(record)),
                )
        segments: dict[tuple[str, str], list[dict]] = {}
        for kind, events in conversion.streams.items():
            for event in events:
                segments.setdefault((kind, str(event["created_at"])[:7]), []).append(
                    dict(event)
                )
        for (kind, month), events in sorted(segments.items()):
            body = b"".join(
                canonical_json_bytes(event) + b"\n"
                for event in sorted(events, key=lambda item: item["sequence"])
            )
            self._write(f"streams/{kind}/{month}.ndjson", body)

    def stack(self) -> _ReportStack:
        application = create_experimental_v4_application(
            self.authority,
            self.runtime,
            enable_v4_application=True,
            checkpoint_facts=build_checkpoint_facts,
            clock=lambda: NOW,
            uid_factory=lambda: UID,
            today=lambda: NOW[:10],
            task_note_source_indexes=self.conversion.task_note_source_indexes,
        )
        return _ReportStack(application.store, initialize=False)


# ------------------------------------------------------------ ingress order


class MutationIngressOrderTest(_ServiceCase):
    def spoiled_everything(self, **overrides: Any) -> dict[str, Any]:
        arguments = {
            "operation": "create",
            "request": self.create_body(workspace_uid=FOREIGN_WORKSPACE),
            "key": "bad key",
            "workspace_uid": self.owner,
        }
        arguments.update(overrides)
        return arguments

    def refuse(self, code: str, **overrides: Any) -> _Trace:
        arguments = self.spoiled_everything(**overrides)
        with self.traced() as trace:
            with self.assertRaises(ReportRepositoryServiceError) as raised:
                self.command(
                    arguments["operation"],
                    arguments["request"],
                    arguments["key"],
                    workspace_uid=arguments["workspace_uid"],
                )
        self.assert_service_error(raised, code)
        return trace

    def test_query_uid_is_judged_before_sync_key_and_body(self) -> None:
        self.spoil_store()
        trace = self.refuse("workspace_mismatch", workspace_uid=FOREIGN_WORKSPACE)
        self.assertEqual(trace.reports_loads(), [])
        self.assertEqual(trace.saves(), [])
        self.assertEqual(trace.of("uid"), [])

    def test_sync_is_judged_before_key_and_body(self) -> None:
        self.spoil_store()
        trace = self.refuse("store_sync_required")
        self.assertEqual(trace.reports_loads(), [])
        self.assertEqual(trace.saves(), [])

    def test_key_is_judged_before_body_semantics(self) -> None:
        trace = self.refuse("invalid_idempotency_key")
        self.assertEqual(trace.reports_loads(), [])

    def test_absent_key_is_named_as_absent(self) -> None:
        for absent in (None, ""):
            with self.subTest(absent=absent):
                trace = self.refuse("idempotency_key_required", key=absent)
                self.assertEqual(trace.reports_loads(), [])

    def test_malformed_keys_are_refused_as_invalid(self) -> None:
        for key in ("short", "x" * 129, "has space", "curly{}", 12345678, b"12345678"):
            with self.subTest(key=key):
                self.refuse("invalid_idempotency_key", key=key)

    def test_body_workspace_must_equal_the_admitted_query_workspace(self) -> None:
        trace = self.refuse("workspace_mismatch", key="report-key-000001")
        self.assertEqual(trace.reports_loads(), [])
        self.assertEqual(trace.saves(), [])

    def test_admitted_key_pattern_matches_the_model_ledger_pattern(self) -> None:
        self.assertEqual(adapter._LEDGER_KEY.pattern, MODEL_LEDGER_KEY.pattern)

    def test_every_refusal_leaves_all_other_documents_byte_identical(self) -> None:
        before = self.other_document_bytes()
        reports_before = (self.root / "reports.json").read_bytes()
        for code, overrides in (
            ("workspace_mismatch", {"workspace_uid": FOREIGN_WORKSPACE}),
            ("invalid_idempotency_key", {}),
            ("idempotency_key_required", {"key": None}),
        ):
            with self.subTest(code=code):
                self.refuse(code, **overrides)
        self.assertEqual(self.other_document_bytes(), before)
        self.assertEqual((self.root / "reports.json").read_bytes(), reports_before)


# ------------------------------------------------------------ fresh mutation


class FreshMutationTest(_ServiceCase):
    def test_one_load_one_save_one_uid_one_instant_one_projection(self) -> None:
        body = self.create_body()
        before = self.other_document_bytes()
        with self.traced() as trace:
            result = self.command("create", body, "report-create-0001")
        self.assertEqual(len(trace.reports_loads()), 1)
        self.assertEqual(
            trace.saves(),
            [("save", ("reports",), "report-create-report-create-0001")],
        )
        self.assertEqual(len(trace.of("uid")), 1)
        self.assertEqual(len(trace.of("now")), 1)
        self.assertEqual(trace.projections(), [("project", DATE, 1)])
        self.assertEqual(len(trace.outer("transaction")), 1)
        self.assertEqual(trace.outer("read"), [])
        self.assertEqual(result["status"], 201)
        self.assertEqual(set(result["body"]), {"data", "meta"})
        self.assertEqual(result["body"]["meta"], {"replayed": False})
        self.assertEqual(set(result["body"]["data"]), DATA_KEYS["create"])
        self.assertEqual(result["body"]["data"]["uid"], UID)
        self.assertEqual(result["body"]["data"]["state"], "draft")
        self.assertIs(result["body"]["data"]["source_stale"], False)
        self.assertEqual(self.other_document_bytes(), before)

    def test_only_reports_json_is_written(self) -> None:
        before = self.other_document_bytes()
        self.create()
        self.assertEqual(self.other_document_bytes(), before)
        stored = self.stored_reports()
        self.assertEqual(len(stored["reports"]), 1)
        self.assertEqual(len(stored["idempotency"]), 1)
        receipt = stored["idempotency"][0]
        self.assertEqual(receipt["method"], "POST")
        self.assertEqual(receipt["path"], ROUTE)
        self.assertEqual(receipt["response_status"], 201)
        self.assertIs(receipt["response_body"]["meta"]["replayed"], False)

    def test_activity_is_untouched_by_a_report_write(self) -> None:
        before = (self.root / "activity.json").read_bytes()
        self.create()
        self.assertEqual((self.root / "activity.json").read_bytes(), before)

    def test_a_create_whose_source_moved_is_refused_without_a_write(self) -> None:
        body = self.create_body()
        self.stack.checkin(time="09:30", date=DATE)
        before = self.other_document_bytes()
        stored_before = (self.root / "reports.json").read_bytes()
        with self.traced() as trace:
            with self.assertRaises(ReportDocumentError) as raised:
                self.command("create", body, "report-create-0001")
        self.assertEqual(raised.exception.code, "report_source_changed")
        self.assertEqual(trace.saves(), [])
        self.assertEqual(len(trace.reports_loads()), 1)
        self.assertEqual((self.root / "reports.json").read_bytes(), stored_before)
        self.assertEqual(self.other_document_bytes(), before)


# ------------------------------------------------------------------- replay


class ReplayAfterRestartTest(_ServiceCase):
    def test_replay_after_restart_returns_the_recorded_answer_and_saves_nothing(
        self,
    ) -> None:
        body = self.create_body()
        first = self.create(body=body)
        stored_before = (self.root / "reports.json").read_bytes()
        others_before = self.other_document_bytes()

        restarted = _ReportStack(Store(self.root))
        with self.traced(stack=restarted) as trace:
            second = self.command(
                "create", body, "report-create-0001", stack=restarted
            )
        self.assertEqual(second["status"], first["status"])
        self.assertEqual(second["body"]["data"], first["body"]["data"])
        self.assertEqual(second["body"]["meta"], {"replayed": True})
        self.assertEqual(trace.saves(), [])
        self.assertEqual(trace.of("uid"), [])
        self.assertEqual(trace.projections(), [])
        self.assertEqual(len(trace.reports_loads()), 1)
        self.assertEqual((self.root / "reports.json").read_bytes(), stored_before)
        self.assertEqual(self.other_document_bytes(), others_before)

    def test_same_key_with_a_changed_path_or_digest_conflicts(self) -> None:
        body = self.create_body()
        self.create(body=body)
        for overrides in (
            {"path": ROUTE + "/other"},
            {"request_digest": "sha256:" + "9" * 64},
        ):
            with self.subTest(overrides=overrides):
                with self.traced() as trace:
                    with self.assertRaises(ReportDocumentError) as raised:
                        self.command("create", body, "report-create-0001", **overrides)
                self.assertEqual(raised.exception.code, "idempotency_conflict")
                self.assertEqual(trace.saves(), [])

    def test_the_service_surface_cannot_vary_the_ledger_method(self) -> None:
        parameters = set(
            inspect.signature(
                ReportRepositoryServiceMixin.execute_report_document_command
            ).parameters
        )
        self.assertNotIn("method", parameters)
        self.create()
        self.assertEqual(self.stored_reports()["idempotency"][0]["method"], "POST")

    def test_expiry_is_inherited_so_an_expired_key_is_no_longer_a_replay(self) -> None:
        self.create()
        with self.traced(now=LATER):
            self.command("archive", self.transition_body(1, note=None), "report-arch-01",
                         target=UID)
        body = self.transition_body(1, note=None)
        with self.traced(now=MUCH_LATER):
            with self.assertRaises(ReportDocumentError) as raised:
                self.command("archive", body, "report-arch-01", target=UID)
        self.assertEqual(raised.exception.code, "report_revision_conflict")


# -------------------------------------------------------------- transitions


class TransitionTest(_ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.create()

    def run_operation(self, operation: str, key: str, body: dict[str, Any], **kw: Any):
        with self.traced(now=kw.pop("now", LATER)) as trace:
            result = self.command(operation, body, key, target=UID, **kw)
        return result, trace

    def test_revise_finalize_archive_restore_status_and_keys(self) -> None:
        expected = [
            ("revise", {"markdown": "# Revised", "note": "why"}, 1, "draft"),
            ("finalize", {}, 2, "finalized"),
            ("archive", {"note": "done"}, 3, "archived"),
            ("restore", {}, 4, "finalized"),
        ]
        for index, (operation, extra, revision, state) in enumerate(expected):
            with self.subTest(operation=operation):
                result, _ = self.run_operation(
                    operation,
                    f"report-{operation}-000{index}",
                    self.transition_body(revision, **extra),
                )
                self.assertEqual(result["status"], STATUSES[operation])
                self.assertEqual(set(result["body"]["data"]), DATA_KEYS[operation])
                self.assertEqual(result["body"]["data"]["state"], state)
                self.assertEqual(result["body"]["data"]["revision"], revision + 1)

    def test_revise_reports_a_current_source_as_not_stale(self) -> None:
        result, trace = self.run_operation(
            "revise", "report-revise-0001",
            self.transition_body(1, markdown="# Again", note=None),
        )
        self.assertIs(result["body"]["data"]["source_stale"], False)
        self.assertEqual(trace.projections(), [("project", DATE, 1)])

    def test_revise_and_finalize_report_a_moved_source_as_stale(self) -> None:
        self.stack.checkin(time="10:15", date=DATE)
        revised, _ = self.run_operation(
            "revise", "report-revise-0002",
            self.transition_body(1, markdown="# Moved", note=None),
        )
        self.assertIs(revised["body"]["data"]["source_stale"], True)
        finalized, _ = self.run_operation(
            "finalize", "report-final-0002", self.transition_body(2)
        )
        self.assertIs(finalized["body"]["data"]["source_stale"], True)

    def test_archive_and_restore_read_no_source_at_all(self) -> None:
        _, archived = self.run_operation(
            "archive", "report-arch-0001", self.transition_body(1, note="filed")
        )
        self.assertEqual(archived.projections(), [])
        _, restored = self.run_operation(
            "restore", "report-rest-0001", self.transition_body(2)
        )
        self.assertEqual(restored.projections(), [])

    def test_every_successful_write_leaves_the_other_nine_untouched(self) -> None:
        before = self.other_document_bytes()
        self.run_operation(
            "revise", "report-revise-0003",
            self.transition_body(1, markdown="# Untouched", note=None),
        )
        self.run_operation("finalize", "report-final-0003", self.transition_body(2))
        self.assertEqual(self.other_document_bytes(), before)

    def test_each_transition_loads_reports_once_and_saves_once(self) -> None:
        _, trace = self.run_operation(
            "finalize", "report-final-0004", self.transition_body(1)
        )
        self.assertEqual(len(trace.reports_loads()), 1)
        self.assertEqual(
            trace.saves(),
            [("save", ("reports",), "report-finalize-report-final-0004")],
        )
        self.assertEqual(trace.of("uid"), [])


# ------------------------------------------------------------- caps and load


class InheritedCapsTest(_ServiceCase):
    def receipt(self, index: int, filler: str = "") -> dict[str, Any]:
        return {
            "key": "planted-receipt-{:06d}".format(index),
            "method": "POST",
            "path": ROUTE,
            "request_digest": "sha256:" + "{:064x}".format(index),
            "response_status": 200,
            "response_body": {
                "data": {"filler": filler} if filler else {},
                "meta": {"replayed": False},
            },
            "created_at": NOW,
        }

    def plant(self, document: dict[str, Any]) -> None:
        self.stack.documents.save_many(
            {WorkspaceDocument.REPORTS: document}, operation_id="planted-fixture"
        )

    def test_body_caps_are_inherited(self) -> None:
        body = self.create_body(markdown="x" * (MAX_MARKDOWN_CHARS + 1))
        with self.assertRaises(ReportDocumentError) as raised:
            self.command("create", body, "report-create-0001")
        self.assertEqual(raised.exception.code, "report_body_invalid")
        self.assertEqual(raised.exception.field, "markdown")

    def test_receipt_caps_are_inherited(self) -> None:
        """An answer too large to record refuses the write instead of dropping it."""

        wide = "가" * MAX_MARKDOWN_CHARS
        body = self.create_body(markdown=wide)
        before = (self.root / "reports.json").read_bytes()
        with self.traced() as trace:
            with self.assertRaises(ReportDocumentError) as raised:
                self.command("create", body, "report-create-000001")
        self.assertEqual(raised.exception.code, "report_body_invalid")
        self.assertEqual(raised.exception.field, "response_body")
        self.assertEqual(trace.saves(), [])
        self.assertEqual((self.root / "reports.json").read_bytes(), before)

    def test_ledger_capacity_is_inherited_with_a_retry_hint(self) -> None:
        self.create()
        document = self.stored_reports()
        document["idempotency"].extend(
            self.receipt(index) for index in range(REPORT_LEDGER_MAX_RECORDS - 1)
        )
        self.plant(document)
        with self.traced(now=LATER) as trace:
            with self.assertRaises(ReportDocumentError) as raised:
                self.command(
                    "archive", self.transition_body(1, note=None),
                    "report-arch-000001", target=UID,
                )
        self.assertEqual(raised.exception.code, "report_idempotency_capacity")
        self.assertGreaterEqual(raised.exception.retry_after_seconds, 1)
        self.assertEqual(trace.saves(), [])

    def test_storage_cap_is_inherited(self) -> None:
        self.create()
        document = self.stored_reports()
        block = "z" * 180_000
        index = 0
        while _canonical_length(document) + 190_000 < MAX_REPORTS_BYTES:
            document["idempotency"].append(self.receipt(index, block))
            index += 1
        headroom = MAX_REPORTS_BYTES - _canonical_length(document)
        document["idempotency"].append(
            self.receipt(index, "z" * max(0, headroom - 600))
        )
        self.assertLess(_canonical_length(document), MAX_REPORTS_BYTES)
        self.plant(document)
        with self.traced(now=LATER) as trace:
            with self.assertRaises(ReportDocumentError) as raised:
                self.command(
                    "archive", self.transition_body(1, note=None),
                    "report-arch-000002", target=UID,
                )
        self.assertEqual(raised.exception.code, "report_storage_full")
        self.assertEqual(trace.saves(), [])


# --------------------------------------------------------- journal recovery


class ForcedInterruptionTest(_ServiceCase):
    def test_interruption_after_journal_publication_recovers_both(self) -> None:
        body = self.create_body()
        real_write = Store._atomic_write_locked

        def interrupted(store: Store, path: Path, value: Any) -> None:
            if path.name == "reports.json":
                raise KeyboardInterrupt("power lost after journal publication")
            real_write(store, path, value)

        with self.traced():
            with mock.patch.object(Store, "_atomic_write_locked", interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    self.command("create", body, "report-create-0001")

        self.assertTrue(self.stack.store.journal_path.exists())
        self.assertEqual(self.stored_reports()["reports"], [])

        recovered = _ReportStack(Store(self.root))
        self.assertFalse(recovered.store.journal_path.exists())
        document = self.stored_reports()
        self.assertEqual(len(document["reports"]), 1)
        self.assertEqual(document["reports"][0]["uid"], UID)
        self.assertEqual(len(document["idempotency"]), 1)
        self.assertEqual(document["idempotency"][0]["key"], "report-create-0001")

        with self.traced(stack=recovered) as trace:
            replayed = self.command(
                "create", body, "report-create-0001", stack=recovered
            )
        self.assertIs(replayed["body"]["meta"]["replayed"], True)
        self.assertEqual(replayed["status"], 201)
        self.assertEqual(trace.saves(), [])
        self.assertEqual(trace.of("uid"), [])
        self.assertEqual(len(self.stored_reports()["reports"]), 1)


# ------------------------------------------------------------------ queries


class HeldQueryTest(_ServiceCase):
    def test_list_uses_one_snapshot_and_never_projects_a_day(self) -> None:
        self.create()
        with self.traced() as trace:
            page = self.stack.list_report_documents(workspace_uid=self.owner)
        self.assertEqual(len(trace.reports_loads()), 1)
        self.assertEqual(trace.projections(), [])
        self.assertEqual(trace.saves(), [])
        self.assertEqual(len(trace.outer("read")), 1)
        self.assertEqual(trace.outer("transaction"), [])
        self.assertEqual(set(page), LIST_KEYS)
        self.assertEqual(page["workspace_uid"], self.owner)
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])
        self.assertEqual(len(page["reports"]), 1)
        row = page["reports"][0]
        self.assertNotIn("markdown", row)
        self.assertNotIn("source_stale", row)
        self.assertNotIn("revisions", row)

    def test_list_default_page_size_is_the_accepted_one(self) -> None:
        signature = inspect.signature(
            ReportRepositoryServiceMixin.list_report_documents
        )
        self.assertEqual(signature.parameters["limit"].default, LIST_PAGE_SIZE)
        self.assertEqual(signature.parameters["state"].default, "active")

    def test_read_projects_exactly_the_one_stored_date(self) -> None:
        self.create()
        with self.traced() as trace:
            report = self.stack.get_report_document(
                workspace_uid=self.owner, report_uid=UID
            )
        self.assertEqual(len(trace.reports_loads()), 1)
        self.assertEqual(trace.projections(), [("project", DATE, 1)])
        self.assertEqual(len(trace.outer("read")), 1)
        self.assertEqual(trace.outer("transaction"), [])
        self.assertEqual(set(report), READ_KEYS)
        self.assertIs(report["source_stale"], False)
        self.assertEqual(len(report["revisions"]), 1)

    def test_read_reports_a_moved_source_as_stale(self) -> None:
        self.create()
        self.stack.checkin(time="08:00", date=DATE)
        report = self.stack.get_report_document(
            workspace_uid=self.owner, report_uid=UID
        )
        self.assertIs(report["source_stale"], True)

    def test_a_missing_canonical_target_refuses_before_projecting_any_day(self) -> None:
        self.create()
        with self.traced() as trace:
            with self.assertRaises(ReportDocumentError) as raised:
                self.stack.get_report_document(
                    workspace_uid=self.owner, report_uid=OTHER_UID
                )
        self.assertEqual(raised.exception.code, "report_not_found")
        self.assertEqual(raised.exception.field, "report_uid")
        self.assertEqual(trace.projections(), [])
        self.assertEqual(len(trace.reports_loads()), 1)

    def test_a_malformed_report_uid_is_invalid_query_before_projecting_any_day(
        self,
    ) -> None:
        self.create()
        for missing in ("not-a-uuid", None, 7, UID.upper()):
            with self.subTest(missing=missing):
                with self.traced() as trace:
                    with self.assertRaises(ReportQueryError) as raised:
                        self.stack.get_report_document(
                            workspace_uid=self.owner, report_uid=missing
                        )
                self.assertEqual(raised.exception.code, "invalid_query")
                self.assertEqual(raised.exception.field, "report_uid")
                self.assertEqual(str(raised.exception), "report query is invalid")
                self.assertEqual(trace.projections(), [])
                self.assertEqual(len(trace.reports_loads()), 1)

    def test_queries_refuse_a_foreign_workspace_before_loading(self) -> None:
        self.create()
        with self.traced() as trace:
            with self.assertRaises(ReportRepositoryServiceError) as listed:
                self.stack.list_report_documents(workspace_uid=FOREIGN_WORKSPACE)
            with self.assertRaises(ReportRepositoryServiceError) as read:
                self.stack.get_report_document(
                    workspace_uid=FOREIGN_WORKSPACE, report_uid=UID
                )
        for raised in (listed, read):
            self.assert_service_error(raised, "workspace_mismatch")
        self.assertEqual(trace.reports_loads(), [])

    def test_queries_refuse_a_store_that_changed_before_the_read(self) -> None:
        self.create()
        self.spoil_store()
        with self.traced() as trace:
            with self.assertRaises(ReportRepositoryServiceError) as listed:
                self.stack.list_report_documents(workspace_uid=self.owner)
            with self.assertRaises(ReportRepositoryServiceError) as read:
                self.stack.get_report_document(
                    workspace_uid=self.owner, report_uid=UID
                )
        for raised in (listed, read):
            self.assert_service_error(raised, "store_sync_required")
        self.assertEqual(trace.reports_loads(), [])

    def spoiling(self, name: str) -> Any:
        """Wrap one accepted projection so the store changes after it runs."""

        real = getattr(adapter, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            answer = real(*args, **kwargs)
            self.spoil_store()
            return answer

        return mock.patch.object(adapter, name, wrapper)

    def test_a_store_that_changes_after_the_projection_refuses(self) -> None:
        self.create()
        with self.spoiling("_list_report_documents"):
            with self.assertRaises(ReportRepositoryServiceError) as listed:
                self.stack.list_report_documents(workspace_uid=self.owner)
        self.assert_service_error(listed, "store_sync_required")

    def test_a_store_that_changes_after_the_read_projection_refuses(self) -> None:
        self.create()
        with self.spoiling("_read_report_document"):
            with self.assertRaises(ReportRepositoryServiceError) as read:
                self.stack.get_report_document(
                    workspace_uid=self.owner, report_uid=UID
                )
        self.assert_service_error(read, "store_sync_required")

    def test_query_refusals_propagate_accepted_query_errors_unchanged(self) -> None:
        self.create()
        with self.assertRaises(ReportQueryError) as raised:
            self.stack.list_report_documents(workspace_uid=self.owner, limit=10)
        self.assertEqual(raised.exception.code, "invalid_query")
        self.assertEqual(raised.exception.field, "limit")
        with self.assertRaises(ReportQueryError) as cursor:
            self.stack.list_report_documents(workspace_uid=self.owner, cursor="short")
        self.assertEqual(cursor.exception.code, "report_cursor_invalid")

    def test_queries_write_nothing_at_all(self) -> None:
        self.create()
        before = self.other_document_bytes()
        reports_before = (self.root / "reports.json").read_bytes()
        self.stack.list_report_documents(workspace_uid=self.owner)
        self.stack.get_report_document(workspace_uid=self.owner, report_uid=UID)
        self.assertEqual(self.other_document_bytes(), before)
        self.assertEqual((self.root / "reports.json").read_bytes(), reports_before)


# --------------------------------------------------------------- detachment


class DetachmentTest(_ServiceCase):
    def test_the_answer_shares_no_container_with_the_request_or_the_document(
        self,
    ) -> None:
        body = self.create_body()
        request_ids = _mutable_ids(body)
        with self.traced():
            result = self.command("create", body, "report-create-0001")
        answer_ids = _mutable_ids(result)
        self.assertEqual(answer_ids & request_ids, set())
        body["markdown"] = "# Mutated after the call"
        body["period"]["date"] = OTHER_DATE
        stored = self.stored_reports()
        self.assertEqual(stored["reports"][0]["period"]["date"], DATE)
        self.assertEqual(stored["reports"][0]["revisions"][0]["markdown"], "# Day")

    def test_mutating_the_answer_cannot_reach_the_saved_document(self) -> None:
        body = self.create_body()
        with self.traced():
            result = self.command("create", body, "report-create-0001")
        result["body"]["data"]["state"] = "tampered"
        result["body"]["meta"]["replayed"] = "tampered"
        restarted = _ReportStack(Store(self.root))
        with self.traced(stack=restarted):
            replayed = self.command(
                "create", body, "report-create-0001", stack=restarted
            )
        self.assertEqual(replayed["body"]["data"]["state"], "draft")
        self.assertEqual(replayed["body"]["meta"], {"replayed": True})

    def test_the_source_projection_is_not_captured_by_any_answer(self) -> None:
        captured: list[dict[str, Any]] = []
        real = _ReportStack.review_projection

        def recording(host: Any, date: Any, days: int = 7) -> dict[str, Any]:
            answer = real(host, date, days)
            captured.append(answer)
            return answer

        with mock.patch.object(_ReportStack, "review_projection", recording), \
                mock.patch.object(adapter, "_utc_now", lambda: NOW), \
                mock.patch.object(adapter, "_allocate_report_uid", lambda: UID):
            result = self.command("create", self.create_body(), "report-create-0001")
            report = self.stack.get_report_document(
                workspace_uid=self.owner, report_uid=UID
            )
        self.assertTrue(captured)
        projection_ids: set[int] = set()
        for answer in captured:
            projection_ids |= _mutable_ids(answer)
        self.assertEqual(_mutable_ids(result) & projection_ids, set())
        self.assertEqual(_mutable_ids(report) & projection_ids, set())

    def test_query_answers_share_nothing_with_each_other(self) -> None:
        self.create()
        first = self.stack.list_report_documents(workspace_uid=self.owner)
        second = self.stack.list_report_documents(workspace_uid=self.owner)
        self.assertEqual(first, second)
        self.assertEqual(_mutable_ids(first) & _mutable_ids(second), set())
        first["reports"][0]["state"] = "tampered"
        third = self.stack.list_report_documents(workspace_uid=self.owner)
        self.assertEqual(third["reports"][0]["state"], "draft")


# ------------------------------------------------------------- module shape


class ModuleShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MODULE_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def functions(self) -> list[ast.AST]:
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

    @staticmethod
    def complexity(node: ast.AST) -> int:
        score = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                                  ast.IfExp, ast.Assert)):
                score += 1
            elif isinstance(child, ast.BoolOp):
                score += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                score += 1 + len(child.ifs)
        return score

    def test_the_module_is_within_the_size_budget(self) -> None:
        self.assertLessEqual(len(self.source.splitlines()), 800)

    def test_every_function_is_within_length_and_complexity(self) -> None:
        for node in self.functions():
            with self.subTest(function=node.name):
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 100)
                self.assertLessEqual(self.complexity(node), 15)

    def test_the_public_surface_is_exactly_two_names(self) -> None:
        self.assertEqual(
            adapter.__all__,
            ["ReportRepositoryServiceError", "ReportRepositoryServiceMixin"],
        )
        public = {
            name
            for name in vars(ReportRepositoryServiceMixin)
            if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "list_report_documents",
                "get_report_document",
                "execute_report_document_command",
            },
        )

    def test_the_error_is_a_runtime_error_with_a_code(self) -> None:
        self.assertTrue(issubclass(ReportRepositoryServiceError, RuntimeError))
        self.assertEqual(set(adapter._MESSAGES), SERVICE_CODES)
        for code in SERVICE_CODES:
            error = ReportRepositoryServiceError(code)
            self.assertEqual(error.code, code)
            self.assertTrue(str(error))

    def test_no_argument_exposes_a_store_repository_clock_or_uuid(self) -> None:
        forbidden = {"store", "documents", "repository", "now", "clock", "uid",
                     "uuid", "allocate_report_uid", "current_day_digest", "method"}
        for name in ("list_report_documents", "get_report_document",
                     "execute_report_document_command"):
            signature = inspect.signature(getattr(ReportRepositoryServiceMixin, name))
            with self.subTest(method=name):
                self.assertEqual(set(signature.parameters) & forbidden, set())

    def test_the_exact_service_surface(self) -> None:
        expected = {
            "list_report_documents": ["self", "workspace_uid", "state", "limit",
                                      "cursor"],
            "get_report_document": ["self", "workspace_uid", "report_uid"],
            "execute_report_document_command": [
                "self", "operation", "request", "workspace_uid",
                "target_report_uid", "idempotency_key", "path", "request_digest",
            ],
        }
        for name, parameters in expected.items():
            signature = inspect.signature(getattr(ReportRepositoryServiceMixin, name))
            with self.subTest(method=name):
                self.assertEqual(list(signature.parameters), parameters)

    def test_the_module_imports_no_transport_or_command_backend(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("service", "server", "cli", "sse_events", "reporting_http.server"):
            self.assertNotIn(forbidden, imported)
        self.assertNotIn("workstack.service", imported)

    def test_exactly_one_transaction_and_one_consistent_read_per_surface(self) -> None:
        self.assertEqual(self.source.count("with store.transaction():"), 1)
        self.assertEqual(
            self.source.count("with store.consistent_read() as readiness:"), 2
        )
        self.assertEqual(self.source.count("documents.save_many("), 1)
        self.assertEqual(
            self.source.count("documents.load(_WorkspaceDocument.REPORTS)"), 2
        )
        self.assertEqual(
            self.source.count("{_WorkspaceDocument.REPORTS: saved}"), 1
        )

    def test_the_adapter_reconstructs_no_accepted_behaviour(self) -> None:
        for forbidden in ("replayed", "receipt", "source_stale", "revision",
                          "omitted_count", "cursor ="):
            self.assertNotIn(f'"{forbidden}"', self.source)


if __name__ == "__main__":
    unittest.main()
