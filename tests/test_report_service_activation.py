"""Activation oracles: the released `WorkStack` is now the report host.

The accepted repository suite proved the mixin's behavior through a test host
it composed itself, because `service.py` had not activated it. This suite
proves the thing that host stood in for. Every claim here is made against the
released `WorkStack` constructed the ordinary way — no subclass, no test-only
composition, no fake repository — over a genuine disposable `Store` under a
temporary directory. If the inheritance were removed, or if the mixin were
composed onto something other than the released application object, these
cases would fail rather than quietly keep passing against a local double.

Four questions are asked that only activation can answer. Does the released
host carry the three report surfaces, and are they the mixin's own? Does the
composition a plain `WorkStack()` builds for itself — its `Store` and the
`StoreDocumentRepository` it wraps around that same `Store` — satisfy the
capability rule, so create, replay, read and list work through the real
journal? Does a store that was on disk at schema 3 answer them too, once
opening it has upgraded it to schema 5 and given it a `reports.json` it never
had? And does a composition that is genuinely not the released one — the
experimental v4 application — still refuse on every surface before a report
document is opened?

The fifth question is about the digest, and it is asked twice. The one-line
day digest now lives on a foundation leaf instead of inside the transport
adapter, so this suite asserts that the leaf, the re-export the HTTP module
keeps, and the function the activated host actually calls are one object; and
then that the digest the read-only preview answers with, for a day, equals the
digest the stored report was written against for that same day. The first is
identity, the second is meaning. A move that preserved the import path but
changed the bytes would pass one and fail the other.

Expectations are restated by hand rather than imported from the code under
test, and the two private seams the contract names — `_utc_now` and
`_allocate_report_uid` — are the only things patched.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock
from urllib.parse import urlencode

from workstack import report_repository_service as adapter
from workstack import report_source_digest as leaf
from workstack import reporting_http as transport
from workstack import service as application
from workstack.capture import canonical_digest
from workstack.checkpoint_change import build_checkpoint_facts
from workstack.report_repository_service import (
    ReportRepositoryServiceError,
    ReportRepositoryServiceMixin,
)
from workstack.report_source_digest import day_source_digest
from workstack.reporting import TEMPLATE_DAILY_V1
from workstack.reporting_http import daily_preview_payload
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
from workstack.store_rosters import REPORTS_DOCUMENT_NAME, V3_DOCUMENT_NAMES


DATE = "2026-09-06"
GENERATED = "2026-09-06T11:59:00Z"
NOW = "2026-09-06T12:00:00Z"
ROUTE = "/api/v1/reports"
UID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FOREIGN_WORKSPACE = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"

# The three surfaces activation is supposed to have put on the released host.
REPORT_SURFACES = (
    "list_report_documents",
    "get_report_document",
    "execute_report_document_command",
)
# Restated from the contract, not imported from the modules under test.
LIST_KEYS = {"workspace_uid", "reports", "omitted_count", "cursor"}
READ_KEYS = {
    "uid", "template", "period", "state", "revision", "content_revision",
    "source_digest", "archived_from_state", "created_at", "updated_at",
    "revisions", "source_stale",
}
OTHER_DOCUMENTS = tuple(sorted(name for name in DEFAULTS if name != "reports.json"))


def _relative_imports(module: Any) -> set[str]:
    """The intra-package modules one module imports, by their dotted names."""

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            imported.add(node.module)
    return imported


class _Trace:
    """What one activated call actually opened, saved and projected."""

    def __init__(self) -> None:
        self.loads: list[str] = []
        self.saves: list[tuple[tuple[str, ...], str | None]] = []
        self.projections: list[tuple[Any, int]] = []


class _ActivatedCase(unittest.TestCase):
    """One disposable Store and the released `WorkStack` opened over it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.stack = WorkStack(Store(self.root))
        self.owner = self.stack.store.readiness.workspace_uid

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- fixtures --------------------------------------------------------
    def source_digest(self, date: str = DATE, stack: Any = None) -> str:
        host = self.stack if stack is None else stack
        return day_source_digest(date=date, day=host.review_projection(date, 1)["day"])

    def create_body(
        self, *, date: str = DATE, owner: str | None = None, stack: Any = None
    ) -> dict[str, Any]:
        return {
            "workspace_uid": self.owner if owner is None else owner,
            "template": TEMPLATE_DAILY_V1,
            "period": {"kind": "day", "date": date},
            "source_digest": self.source_digest(date, stack),
            "source_generated_at": GENERATED,
            "markdown": "# Day",
        }

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

        def load(repository: Any, document: WorkspaceDocument) -> dict[str, Any]:
            trace.loads.append(document.value)
            return real_load(repository, document)

        def save_many(
            repository: Any, writes: Any, operation_id: str | None = None
        ) -> None:
            trace.saves.append((tuple(sorted(i.value for i in writes)), operation_id))
            real_save(repository, writes, operation_id)

        def projection(host_self: Any, date: Any, days: int = 7) -> dict[str, Any]:
            trace.projections.append((date, days))
            return real_projection(host_self, date, days)

        with mock.patch.object(StoreDocumentRepository, "load", load), \
                mock.patch.object(StoreDocumentRepository, "save_many", save_many), \
                mock.patch.object(type(host), "review_projection", projection), \
                mock.patch.object(adapter, "_utc_now", lambda: now), \
                mock.patch.object(adapter, "_allocate_report_uid", lambda: uid):
            yield trace

    # -- calls -----------------------------------------------------------
    def command(
        self,
        operation: str,
        request: object,
        key: object,
        *,
        target: object = None,
        owner: str | None = None,
        stack: Any = None,
    ) -> dict[str, Any]:
        host = self.stack if stack is None else stack
        return host.execute_report_document_command(
            operation,
            request,
            workspace_uid=self.owner if owner is None else owner,
            target_report_uid=target,
            idempotency_key=key,
            path=ROUTE,
            request_digest=canonical_digest(request),
        )

    def create(
        self,
        key: str = "report-create-0001",
        *,
        stack: Any = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        host = self.stack if stack is None else stack
        request = self.create_body(stack=host) if body is None else body
        with self.traced(stack=host):
            return self.command("create", request, key, stack=host)

    # -- store observation -----------------------------------------------
    def reports_bytes(self, root: Path | None = None) -> bytes:
        return ((self.root if root is None else root) / "reports.json").read_bytes()

    def other_document_bytes(self, root: Path | None = None) -> dict[str, bytes]:
        base = self.root if root is None else root
        return {name: (base / name).read_bytes() for name in OTHER_DOCUMENTS}

    def document_bytes(self, roots: tuple[Path, ...]) -> dict[str, bytes]:
        return {
            f"{root}/{name}": (root / name).read_bytes()
            for root in roots
            for name in sorted(DEFAULTS)
            if (root / name).exists()
        }

    # -- shared exercises ------------------------------------------------
    def exercise_lifecycle(self, stack: Any, root: Path) -> None:
        """Create, replay, read and list once, through one activated host.

        This is the whole point of activation, so it is asserted the same way
        wherever the store came from: a fresh store, or one upgraded out of
        schema 3. The replay is made from a second `WorkStack` opened over the
        same directory, so the recorded answer is proved to survive a restart
        rather than a warm in-process cache.
        """

        owner = stack.store.readiness.workspace_uid
        body = self.create_body(owner=owner, stack=stack)
        others_before = self.other_document_bytes(root)
        with self.traced(stack=stack) as trace:
            created = self.command(
                "create", body, "report-create-0001", owner=owner, stack=stack
            )
        self.assertEqual(created["status"], 201)
        self.assertEqual(created["body"]["meta"], {"replayed": False})
        self.assertEqual(created["body"]["data"]["uid"], UID)
        self.assertEqual(created["body"]["data"]["state"], "draft")
        self.assertIs(created["body"]["data"]["source_stale"], False)
        self.assertEqual(trace.loads.count("reports"), 1)
        self.assertEqual(
            trace.saves, [(("reports",), "report-create-report-create-0001")]
        )
        self.assertEqual(self.other_document_bytes(root), others_before)

        stored = self.reports_bytes(root)
        restarted = WorkStack(Store(root))
        with self.traced(stack=restarted) as replayed_trace:
            replayed = self.command(
                "create", body, "report-create-0001", owner=owner, stack=restarted
            )
        self.assertEqual(replayed["status"], created["status"])
        self.assertEqual(replayed["body"]["data"], created["body"]["data"])
        self.assertEqual(replayed["body"]["meta"], {"replayed": True})
        self.assertEqual(replayed_trace.saves, [])
        self.assertEqual(replayed_trace.projections, [])
        self.assertEqual(self.reports_bytes(root), stored)

        report = stack.get_report_document(workspace_uid=owner, report_uid=UID)
        self.assertEqual(set(report), READ_KEYS)
        self.assertIs(report["source_stale"], False)
        self.assertEqual(report["source_digest"], body["source_digest"])
        self.assertEqual(len(report["revisions"]), 1)

        page = stack.list_report_documents(workspace_uid=owner)
        self.assertEqual(set(page), LIST_KEYS)
        self.assertEqual(page["workspace_uid"], owner)
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])
        self.assertEqual([row["uid"] for row in page["reports"]], [UID])
        self.assertNotIn("markdown", page["reports"][0])

    def refuse_every_surface(self, stack: Any, *roots: Path) -> None:
        """No report surface answers, and no watched document byte moves."""

        watched = roots or (self.root,)
        before = self.document_bytes(watched)
        with self.traced(stack=stack) as trace:
            with self.assertRaises(ReportRepositoryServiceError) as listed:
                stack.list_report_documents(workspace_uid=self.owner)
            with self.assertRaises(ReportRepositoryServiceError) as read:
                stack.get_report_document(workspace_uid=self.owner, report_uid=UID)
            with self.assertRaises(ReportRepositoryServiceError) as written:
                self.command(
                    "create", self.create_body(), "report-key-0001", stack=stack
                )
        for raised in (listed, read, written):
            self.assertEqual(raised.exception.code, "report_capability_unavailable")
            self.assertEqual(
                str(raised.exception),
                "this storage composition does not support report documents",
            )
        self.assertEqual(trace.loads.count("reports"), 0)
        self.assertEqual(trace.saves, [])
        self.assertEqual(self.document_bytes(watched), before)


# -------------------------------------------------------------- activation


class ActivationShapeTest(unittest.TestCase):
    """The inheritance itself, and the direction of the edges that allow it."""

    def test_the_released_workstack_inherits_the_report_mixin(self) -> None:
        self.assertTrue(issubclass(WorkStack, ReportRepositoryServiceMixin))
        self.assertIn(ReportRepositoryServiceMixin, WorkStack.__mro__)

    def test_every_report_surface_on_the_host_is_the_mixin_method(self) -> None:
        for name in REPORT_SURFACES:
            with self.subTest(surface=name):
                self.assertIs(
                    getattr(WorkStack, name),
                    getattr(ReportRepositoryServiceMixin, name),
                )

    def test_activation_adds_no_report_surface_of_its_own(self) -> None:
        """`service.py` composes the mixin; it does not reimplement it."""

        own = vars(WorkStack)
        for name in REPORT_SURFACES + ("_held_day_digest", "_opened_reports"):
            with self.subTest(surface=name):
                self.assertNotIn(name, own)

    def test_the_application_imports_the_adapter_and_not_the_transport(self) -> None:
        imported = _relative_imports(application)
        self.assertIn("report_repository_service", imported)
        self.assertNotIn("reporting_http", imported)

    def test_the_adapter_no_longer_reaches_up_into_the_transport(self) -> None:
        imported = _relative_imports(adapter)
        self.assertIn("report_source_digest", imported)
        self.assertNotIn("reporting_http", imported)
        self.assertNotIn("service", imported)

    def test_the_digest_leaf_imports_only_the_canonical_encoder(self) -> None:
        self.assertEqual(_relative_imports(leaf), {"capture"})

    def test_the_transport_keeps_the_released_import_path(self) -> None:
        self.assertIn("report_source_digest", _relative_imports(transport))


# --------------------------------------------------------------- lifecycle


class ActivatedLifecycleTest(_ActivatedCase):
    def test_a_plain_workstack_attributes_its_repository_to_its_own_store(self) -> None:
        self.assertIs(type(self.stack.store), Store)
        self.assertIs(type(self.stack.documents), StoreDocumentRepository)
        self.assertIs(self.stack.documents._store, self.stack.store)

    def test_create_replay_read_and_list_through_the_released_host(self) -> None:
        self.exercise_lifecycle(self.stack, self.root)

    def test_a_report_write_touches_no_other_authoritative_document(self) -> None:
        before = self.other_document_bytes()
        self.create()
        self.assertEqual(self.other_document_bytes(), before)
        stored = json.loads(self.reports_bytes().decode("utf-8"))
        self.assertEqual(len(stored["reports"]), 1)
        self.assertEqual(len(stored["idempotency"]), 1)
        self.assertEqual(stored["idempotency"][0]["response_status"], 201)

    def test_a_read_projects_exactly_the_one_stored_day(self) -> None:
        self.create()
        with self.traced() as trace:
            self.stack.get_report_document(workspace_uid=self.owner, report_uid=UID)
        self.assertEqual(trace.projections, [(DATE, 1)])
        self.assertEqual(trace.saves, [])

    def test_a_list_page_never_projects_a_day(self) -> None:
        self.create()
        with self.traced() as trace:
            self.stack.list_report_documents(workspace_uid=self.owner)
        self.assertEqual(trace.projections, [])
        self.assertEqual(trace.saves, [])

    def test_a_foreign_workspace_is_refused_on_the_activated_host(self) -> None:
        self.create()
        with self.traced() as trace:
            with self.assertRaises(ReportRepositoryServiceError) as raised:
                self.stack.list_report_documents(workspace_uid=FOREIGN_WORKSPACE)
        self.assertEqual(raised.exception.code, "workspace_mismatch")
        self.assertEqual(trace.loads.count("reports"), 0)


# ------------------------------------------------------------- composition


class ActivatedCompositionTest(_ActivatedCase):
    def test_a_repository_bound_to_a_different_store_is_still_refused(self) -> None:
        """Activation composes the mixin; it does not relax the capability rule."""

        other = Path(self.temporary.name) / "other"
        stack = WorkStack(
            Store(self.root), document_repository=StoreDocumentRepository(Store(other))
        )
        self.assertIsNot(stack.documents._store, stack.store)
        self.refuse_every_surface(stack, self.root, other)


# ---------------------------------------------------------------- migration


class MigratedStoreTest(_ActivatedCase):
    """A store that was schema 3 on disk answers once opening upgraded it."""

    def v3_authority(self, root: Path) -> None:
        """Nine real v3 payloads and the metadata record a v3 build carried.

        Taken the way the accepted migration suite takes it: this build can
        only write schema 5, so a genuine v3 fixture is a fresh store stepped
        back — the same nine payloads, the version they were written under,
        and no reports evidence.
        """

        with tempfile.TemporaryDirectory() as scratch:
            Store(Path(scratch)).initialize()
            values = {
                name: json.loads((Path(scratch) / name).read_text(encoding="utf-8"))
                for name in V3_DOCUMENT_NAMES
            }
        values["store-meta.json"]["store_schema_version"] = 3
        del values["store-meta.json"]["migrations"]["reports"]
        del values["store-meta.json"]["migrations"]["knowledge"]
        root.mkdir(parents=True, exist_ok=True)
        for name, value in values.items():
            (root / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )

    def test_an_upgraded_v3_store_answers_every_report_surface(self) -> None:
        root = Path(self.temporary.name) / "legacy"
        self.v3_authority(root)
        self.assertFalse((root / REPORTS_DOCUMENT_NAME).exists())

        migrated = WorkStack(Store(root))

        self.assertEqual(migrated.store.readiness.schema_version, 6)
        self.assertTrue((root / REPORTS_DOCUMENT_NAME).exists())
        metadata = json.loads((root / "store-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["store_schema_version"], 6)
        self.assertEqual(metadata["migrations"]["reports"]["origin"], "migrated_v3")
        self.exercise_lifecycle(migrated, root)

    def test_the_upgrade_leaves_an_empty_report_document_to_write_into(self) -> None:
        root = Path(self.temporary.name) / "legacy"
        self.v3_authority(root)

        migrated = WorkStack(Store(root))

        page = migrated.list_report_documents(
            workspace_uid=migrated.store.readiness.workspace_uid
        )
        self.assertEqual(page["reports"], [])
        self.assertEqual(page["omitted_count"], 0)
        self.assertIsNone(page["cursor"])


# --------------------------------------------------------- experimental v4


class ExperimentalV4RefusalTest(_ActivatedCase):
    """Activation must not turn an unreleased composition into a capability."""

    def build(self) -> WorkStack:
        base = Path(self.temporary.name) / "v4"
        base.mkdir()
        legacy = WorkStack(Store(base / "v3"))
        legacy.add_task("Conversion source task")
        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        conversion = convert_v3_documents(documents, candidate_created_at=NOW)
        authority = base / "authority"
        authority.mkdir()
        self.write_conversion(authority, conversion)
        runtime = resolve_runtime_authority(
            authority, base / "runtime", str(conversion.store["workspace_uid"])
        )
        runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(
            runtime.manifest_path,
            build_v4_manifest(read_v4(authority), generation=0),
            expected_digest=None,
        )
        runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(conversion.idempotency_ledger))
        )
        experimental = create_experimental_v4_application(
            authority,
            runtime,
            enable_v4_application=True,
            checkpoint_facts=build_checkpoint_facts,
            clock=lambda: NOW,
            uid_factory=lambda: UID,
            today=lambda: NOW[:10],
            task_note_source_indexes=conversion.task_note_source_indexes,
        )
        return WorkStack(experimental.store, initialize=False)

    @staticmethod
    def write_conversion(authority: Path, conversion: Any) -> None:
        def write(relative: str, body: bytes) -> None:
            path = authority.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

        write("store.json", canonical_json_bytes(dict(conversion.store)))
        write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
        for kind, records in conversion.records.items():
            for record in records:
                uid = str(record["uid"])
                write(
                    f"records/{kind}/{uid[:2]}/{uid}.json",
                    canonical_json_bytes(dict(record)),
                )
        segments: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for kind, events in conversion.streams.items():
            for event in events:
                key = (kind, str(event["created_at"])[:7])
                segments.setdefault(key, []).append(dict(event))
        for (kind, month), events in sorted(segments.items()):
            write(
                f"streams/{kind}/{month}.ndjson",
                b"".join(
                    canonical_json_bytes(event) + b"\n"
                    for event in sorted(events, key=lambda item: item["sequence"])
                ),
            )

    def test_the_v4_application_is_refused_on_every_activated_surface(self) -> None:
        stack = self.build()
        self.assertTrue(issubclass(type(stack), ReportRepositoryServiceMixin))
        self.assertIsNot(type(stack.store), Store)
        self.assertIs(type(stack.documents), StoreDocumentRepository)
        self.refuse_every_surface(stack, self.root)


# ------------------------------------------------------------ digest parity


class DayDigestParityTest(_ActivatedCase):
    def test_the_leaf_and_the_transport_export_one_function(self) -> None:
        self.assertIs(transport.day_source_digest, leaf.day_source_digest)
        self.assertIs(adapter._day_source_digest, leaf.day_source_digest)
        self.assertEqual(
            leaf.day_source_digest.__module__, "workstack.report_source_digest"
        )

    def test_the_digest_is_the_canonical_encoding_of_the_bounded_day(self) -> None:
        day = self.stack.review_projection(DATE, 1)["day"]
        self.assertEqual(
            day_source_digest(date=DATE, day=day),
            canonical_digest({"date": DATE, "day": day}),
        )

    def test_the_host_seam_answers_the_same_digest_as_the_leaf(self) -> None:
        day = self.stack.review_projection(DATE, 1)["day"]
        self.assertEqual(
            self.stack._held_day_digest(DATE),
            canonical_digest({"date": DATE, "day": day}),
        )

    def test_the_preview_and_the_stored_report_agree_byte_for_byte(self) -> None:
        """One day, one digest — whichever surface the caller asked."""

        query = urlencode({
            "date": DATE,
            "template": TEMPLATE_DAILY_V1,
            "workspace_uid": self.owner,
        })
        preview = daily_preview_payload(self.stack, query)
        created = self.create(body={
            "workspace_uid": self.owner,
            "template": TEMPLATE_DAILY_V1,
            "period": {"kind": "day", "date": DATE},
            "source_digest": preview["source_digest"],
            "source_generated_at": GENERATED,
            "markdown": "# Day",
        })
        self.assertEqual(created["status"], 201)
        self.assertIs(created["body"]["data"]["source_stale"], False)
        self.assertEqual(
            created["body"]["data"]["source_digest"], preview["source_digest"]
        )
        report = self.stack.get_report_document(
            workspace_uid=self.owner, report_uid=UID
        )
        self.assertEqual(report["source_digest"], preview["source_digest"])
        self.assertIs(report["source_stale"], False)

    def test_a_day_that_moved_makes_the_stored_report_stale(self) -> None:
        """The parity claim has teeth only if a changed day changes the digest."""

        before = self.source_digest()
        self.create()
        self.stack.checkin(time="08:00", date=DATE)
        self.assertNotEqual(self.source_digest(), before)
        report = self.stack.get_report_document(
            workspace_uid=self.owner, report_uid=UID
        )
        self.assertIs(report["source_stale"], True)


if __name__ == "__main__":
    unittest.main()
