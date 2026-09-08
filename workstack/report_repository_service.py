"""Released repository transaction and query adapter for report documents.

This is the one layer between the accepted pure report leaves and a live
workspace: it holds the Store, admits the composition, opens `reports.json`
exactly once per call, and saves at most once. It decides nothing the model
already decides. Replay, revision conflicts, state transitions, receipts,
caps and the exact response fields belong to `report_command_service` and
`report_documents`; list order, cursors and projections belong to
`report_queries`. Nothing here reconstructs any of them.

There is one public mixin and one content-free service error. The mixin is
composed onto the existing `WorkStack` shape and uses only three things from
its host: `self.store`, `self.documents` and `self.review_projection`. It adds
no protocol, no baseline, no waiver, no scheduler and no compatibility
fallback, and it never exposes a Store, a repository, a clock or a UUID
generator to an HTTP or frontend caller: `_utc_now()` and
`_allocate_report_uid()` are private seams of this module.

Capability is exact attribution, not protocol conformance. The released
`StoreDocumentRepository` must wrap the released `Store`, and it must be the
same `Store` object that transacts, holds the lease and carries the workspace
identity — otherwise the documents would be written in one store while the
transaction and the recorded identity belonged to another. This is the rule
`service.py` already applies to released composition, restated here because a
report write is durable evidence. Anything else, including the experimental v4
adapter, is refused before `reports.json` is opened.

Mutations hold exactly one outer `store.transaction()` and check, in order,
the workspace identity, store synchronization, the idempotency key and only
then the body. Queries hold one `store.consistent_read()` snapshot and check
synchronization again after projecting, so a store that changed underneath the
read refuses rather than answering from a snapshot that no longer exists.

Service refusals carry a fixed code and a fixed sentence. No identity, path,
markdown, digest, document content, changed-file name or wrapped adapter
exception ever reaches one. Model and query refusals propagate unchanged.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any, NoReturn

from .report_command_service import execute_report_command as _execute_report_command
from .report_documents import (
    normalize_report_request as _normalize_report_request,
    validate_reports_document as _validate_reports_document,
)
from .report_documents import ReportDocumentError as _ReportDocumentError
from .report_queries import LIST_PAGE_SIZE as _LIST_PAGE_SIZE
from .report_queries import (
    _admit_report_uid as _admit_report_uid,
    list_report_documents as _list_report_documents,
    read_report_document as _read_report_document,
)
from .report_source_digest import day_source_digest as _day_source_digest
from .storage.document_repository import (
    StoreDocumentRepository as _StoreDocumentRepository,
    WorkspaceDocument as _WorkspaceDocument,
)
from .store import Store as _Store


__all__ = ["ReportRepositoryServiceError", "ReportRepositoryServiceMixin"]

# Restated from the contract rather than imported from the model's private
# namespace. The key is admitted here because the accepted composer normalizes
# the body before it consults the ledger, and the contract fixes key admission
# ahead of body semantics. The test suite asserts the two patterns agree.
_LEDGER_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")

# One fixed sentence per code. These say what was refused and nothing about
# what was supplied, so a refusal can be returned to a browser verbatim.
_MESSAGES = {
    "report_capability_unavailable": (
        "this storage composition does not support report documents"
    ),
    "workspace_mismatch": "workspace_uid does not match the owning Store",
    "store_sync_required": (
        "authoritative store changed outside Work Stack; review synchronization status"
    ),
    "idempotency_key_required": "an idempotency key is required",
    "invalid_idempotency_key": "the idempotency key is invalid",
}

_IN_SYNC = "in-sync"


class ReportRepositoryServiceError(RuntimeError):
    """A content-free service refusal: a fixed code and a fixed sentence."""

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code


def _refuse(code: str) -> NoReturn:
    raise ReportRepositoryServiceError(code)


def _utc_now() -> str:
    """Canonical UTC seconds — the one instant a single command is stamped with."""

    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _allocate_report_uid() -> str:
    """A canonical lowercase UUIDv4 identity, allocated only by a create."""

    return str(uuid.uuid4())


def _admitted_repository(host: object) -> tuple[Any, Any]:
    """Return the released Store and the repository attributed to it, or refuse.

    Exact types are checked first and identity second. A released adapter can
    legitimately wrap a *different* released Store, and that composition must
    not transact: the outer transaction, the writer lease and the workspace
    identity behind the recorded receipt would belong to one Store while the
    document bytes landed in another. Protocol conformance and method presence
    are not capability, so no attribute probing stands in for either check, and
    the refusal names no adapter.
    """

    store = getattr(host, "store", None)
    documents = getattr(host, "documents", None)
    if type(store) is not _Store or type(documents) is not _StoreDocumentRepository:
        _refuse("report_capability_unavailable")
    if documents._store is not store:
        _refuse("report_capability_unavailable")
    return store, documents


def _require_owner(readiness: object, workspace_uid: object) -> None:
    """The admitted Store identity must be the identity the query asked about.

    A Store with no readiness has no admitted owner at all, so no supplied
    identity can be proved to own it and the same content-free mismatch is the
    honest answer.
    """

    owner = getattr(readiness, "workspace_uid", None)
    if type(owner) is not str or type(workspace_uid) is not str:
        _refuse("workspace_mismatch")
    if owner != workspace_uid:
        _refuse("workspace_mismatch")


def _require_in_sync(store: Any) -> None:
    """Refuse while the authoritative store carries a change Work Stack did not make.

    Nothing is adopted, rebound or repaired: this observes the state and
    refuses, leaving the resolution to the existing synchronization surface.
    """

    if store.sync_status()["state"] != _IN_SYNC:
        _refuse("store_sync_required")


def _admitted_key(value: object) -> str:
    """Admit the idempotency key before any body semantics are consulted."""

    if value is None or (type(value) is str and not value):
        _refuse("idempotency_key_required")
    if type(value) is not str or _LEDGER_KEY.fullmatch(value) is None:
        _refuse("invalid_idempotency_key")
    return value


def _stored_report_date(
    document: object, workspace_uid: object, report_uid: object
) -> str:
    """Locate only the target's stored `period.date` on the held document.

    The accepted read projection requires the current source digest, and
    computing one costs a day projection. A read of a report that is not there
    must not spend it, so identity is admitted with the accepted query leaf,
    the already loaded document is validated privately, and the target is
    located first. A wrong type or noncanonical UUIDv4 is `invalid_query`.
    Only a canonical identity missing from the document is `report_not_found`.
    Neither case reads a day.
    """

    target = _admit_report_uid(report_uid)
    top = _validate_reports_document(document, workspace_uid=workspace_uid)
    reports: Any = top["reports"]
    for report in reports:
        if report["uid"] == target:
            return str(report["period"]["date"])
    raise _ReportDocumentError("report_not_found", "report_uid")


class ReportRepositoryServiceMixin:
    """Report transactions and queries over the released document repository.

    Composed onto the existing `WorkStack` shape. Every method admits the
    composition before opening `reports.json`, and none of them writes Activity
    or any of the other nine authoritative documents.
    """

    # ------------------------------------------------------------- queries

    def list_report_documents(
        self,
        /,
        *,
        workspace_uid: object,
        state: object = "active",
        limit: object = _LIST_PAGE_SIZE,
        cursor: object = None,
    ) -> dict[str, object]:
        """Return one bounded list page from a single held snapshot.

        The page is the accepted list envelope: `(updated_at DESC, uid ASC)`,
        the fixed page size, an owner- and filter-bound cursor, an
        `omitted_count`, no markdown and no `source_stale`. No day is projected
        here — a list that read a day per row would turn a page into a scan.
        """

        store, documents = _admitted_repository(self)
        with store.consistent_read() as readiness:
            document = self._opened_reports(store, documents, readiness, workspace_uid)
            page = _list_report_documents(
                document,
                workspace_uid=workspace_uid,
                state=state,
                limit=limit,
                cursor=cursor,
            )
            _require_in_sync(store)
            return page

    def get_report_document(
        self, /, *, workspace_uid: object, report_uid: object
    ) -> dict[str, object]:
        """Return one report projection, its history and its source staleness.

        Exactly one day is projected, and only the one the stored report
        already names, inside the same snapshot the document was read from. A
        digest computed outside that snapshot — or taken from a preview — would
        answer `source_stale` about a day nobody read.
        """

        store, documents = _admitted_repository(self)
        with store.consistent_read() as readiness:
            document = self._opened_reports(store, documents, readiness, workspace_uid)
            date = _stored_report_date(document, workspace_uid, report_uid)
            report = _read_report_document(
                document,
                workspace_uid=workspace_uid,
                report_uid=report_uid,
                current_source_digest=self._held_day_digest(date),
            )
            _require_in_sync(store)
            return report

    # ----------------------------------------------------------- mutations

    def execute_report_document_command(
        self,
        operation: object,
        request: object,
        /,
        *,
        workspace_uid: object,
        target_report_uid: object,
        idempotency_key: object,
        path: object,
        request_digest: object,
    ) -> dict[str, object]:
        """Run one report mutation under exactly one held writer transaction.

        The admission order is fixed and is the order a caller can reason
        about: whose store this is, whether it may be written at all, whether
        the retry identity is well formed, and only then what the body says. A
        body that is valid but names a different workspace than the route
        already admitted is the same content-free mismatch, not a second
        opinion about ownership.

        The accepted composer stays authoritative for everything after that.
        This method contributes the held transaction, the single document load,
        the one captured instant, the two private seams and — for a fresh
        mutation only — the single save.
        """

        store, documents = _admitted_repository(self)
        with store.transaction():
            _require_owner(store.readiness, workspace_uid)
            _require_in_sync(store)
            key = _admitted_key(idempotency_key)
            body = _normalize_report_request(operation, request)
            if body["workspace_uid"] != workspace_uid:
                _refuse("workspace_mismatch")
            document = documents.load(_WorkspaceDocument.REPORTS)
            outcome = _execute_report_command(
                document,
                operation,
                body,
                target_report_uid=target_report_uid,
                idempotency_key=key,
                path=path,
                request_digest=request_digest,
                now=_utc_now(),
                allocate_report_uid=_allocate_report_uid,
                current_day_digest=self._held_day_digest,
            )
            return self._settled(documents, operation, key, outcome)

    # ------------------------------------------------------------- private

    @staticmethod
    def _opened_reports(
        store: Any, documents: Any, readiness: object, workspace_uid: object
    ) -> dict[str, Any]:
        """Admit the held snapshot and open `reports.json` exactly once."""

        _require_owner(readiness, workspace_uid)
        _require_in_sync(store)
        return documents.load(_WorkspaceDocument.REPORTS)

    def _held_day_digest(self, date: object) -> str:
        """The canonical digest of one bounded day, read inside the held lock.

        This is the callback the accepted composer and the read projection both
        consume. It reuses the host's own single-day projection and the
        existing digest function rather than hashing anything itself, so the
        source a report is compared against is the source the preview showed.
        """

        projection = self.review_projection(date, 1)
        return _day_source_digest(date=date, day=projection["day"])

    @staticmethod
    def _settled(
        documents: Any, operation: object, key: str, outcome: dict[str, object]
    ) -> dict[str, object]:
        """Save a fresh mutation once, save a replay never, answer both the same.

        Only `reports.json` is written. A replay returns the status and body
        that were recorded the first time and touches no document at all, so a
        retry arriving after ten later mutations still costs zero bytes.
        """

        saved = outcome["document_to_save"]
        if saved is not None:
            documents.save_many(
                {_WorkspaceDocument.REPORTS: saved},
                operation_id=f"report-{operation}-{key}",
            )
        return {
            "status": outcome["response_status"],
            "body": outcome["response_body"],
        }
