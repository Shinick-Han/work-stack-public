"""Deterministic report command composition over the pure report model.

This is the one composition step between `workstack.report_documents` and the
transactional layer that has not arrived yet. It owns the order the contract
fixes — admit, replay, plan, read the source, build the body, record the
receipt — and nothing else. It has no class hierarchy, no protocol, no
scheduler and no generic command framework: there is exactly one public
function, and calling it twice with the same arguments produces the same
answer both times.

Purity is inherited rather than re-argued. Nothing here opens a file, reads a
clock, allocates a UUID, imports a Store, a repository, a server or a
frontend, or names the persisted document by path. Every instant, identity
and source digest arrives as an argument or as a caller-supplied callback,
so the module can be exercised in full without a workspace existing.

The two callbacks are trusted. `allocate_report_uid()` is create-only and is
called at most once per command; `current_day_digest(date)` returns the digest
of the day snapshot the caller is already holding. Neither is wrapped: a
callback that raises propagates unchanged, and no document is produced when it
does, because a command that could not read its own source has not happened.

Refusals introduce nothing. Every error out of here is an existing
content-free `ReportDocumentError` raised by the accepted model, apart from
the target-identity shape check below, which reuses `report_body_invalid`.

The later repository adapter is the only permitted caller. It owns query-UID
admission and sync, holds one transaction, supplies these arguments from it,
and saves `document_to_save` exactly once — never on a replay, which returns
`None` there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

from .report_documents import (
    ReportDocumentError,
    append_report_receipt,
    normalize_report_request,
    plan_report_mutation,
    prepare_report_replay,
)


__all__ = ["ReportDocumentError", "execute_report_command"]

# The ledger records one verb only, so the caller never gets to vary it and a
# receipt can never be replayed against a method it was not written for.
_METHOD = "POST"
_CREATED = 201
_OK = 200

# Which operations produce which response fields — restated here because the
# response envelope is this module's own contract, not the model's.
_ENTRY_OPERATIONS = frozenset({"create", "revise"})
_SOURCE_OPERATIONS = frozenset({"create", "revise", "finalize"})


def _refuse(field: str) -> NoReturn:
    """The existing body-defect refusal, with an allowlisted structural field."""

    raise ReportDocumentError("report_body_invalid", field)


def _target(operation: str, target_report_uid: object) -> object:
    """Create allocates its own identity; nothing else may invent one.

    A create that arrives carrying a target is refused rather than having the
    stray value ignored, because a caller that believed it was addressing an
    existing report must not be answered with a brand new one.
    """

    if operation == "create":
        if target_report_uid is not None:
            _refuse("report_uid")
        return None
    if target_report_uid is None:
        _refuse("report_uid")
    return target_report_uid


def _plan_created(
    pruned: dict[str, object],
    request: object,
    asserted: object,
    now: object,
    allocate_report_uid: Callable[[], object],
    current_day_digest: Callable[[object], object],
) -> dict[str, object]:
    """Settle everything a create can be refused for before reading a source.

    The first plan uses the digest the client already had admitted as the
    provisional current one, so its source check passes by construction and
    the target, the document limit, the period collision and the calendar
    ordering are the only things that can refuse. Only a create that survived
    all of that spends a source read, and the second plan — the authoritative
    one — is the one that decides whether the source moved underneath it.
    """

    allocated = allocate_report_uid()
    settled = plan_report_mutation(
        pruned,
        "create",
        request,
        report_uid=allocated,
        current_source_digest=asserted,
        now=now,
    )
    current = current_day_digest(settled["report"]["period"]["date"])
    return plan_report_mutation(
        pruned,
        "create",
        request,
        report_uid=allocated,
        current_source_digest=current,
        now=now,
    )


def _planned(
    pruned: dict[str, object],
    operation: str,
    request: object,
    body: dict[str, object],
    target: object,
    now: object,
    allocate_report_uid: Callable[[], object],
    current_day_digest: Callable[[object], object],
) -> tuple[dict[str, object], bool | None]:
    """Return the authoritative plan and the staleness flag it deserves.

    `None` means the operation has no source field at all: archiving and
    restoring do not depend on what the day looks like now, so they must not
    read it, and a caller that stubbed the callback to explode still sees them
    succeed.
    """

    if operation == "create":
        plan = _plan_created(
            pruned,
            request,
            body["source_digest"],
            now,
            allocate_report_uid,
            current_day_digest,
        )
        # A create that reached here replanned against the real digest and was
        # not refused, so its source is current by construction.
        return plan, False
    plan = plan_report_mutation(pruned, operation, request, report_uid=target, now=now)
    if operation not in _SOURCE_OPERATIONS:
        return plan, None
    report = plan["report"]
    current = current_day_digest(report["period"]["date"])
    return plan, current != report["source_digest"]


def _data(
    operation: str, plan: dict[str, Any], source_stale: bool | None
) -> dict[str, Any]:
    """The response payload: the planned report, plus what this operation adds.

    The plan's report summary already excludes the authored history, so no
    `revisions` key can reach a response body or the receipt that stores one.
    """

    data = dict(plan["report"])
    if operation in _ENTRY_OPERATIONS:
        data["content_entry"] = plan["content_entry"]
    if operation == "revise":
        data["reopened"] = plan["reopened"]
    if source_stale is not None:
        data["source_stale"] = source_stale
    return data


def execute_report_command(
    document: object,
    operation: object,
    request: object,
    /,
    *,
    target_report_uid: object,
    idempotency_key: object,
    path: object,
    request_digest: object,
    now: object,
    allocate_report_uid: Callable[[], object],
    current_day_digest: Callable[[object], object],
) -> dict[str, object]:
    """Compose one report mutation and return what to answer and what to save.

    `document` is the complete `reports.json` value the caller loaded in its
    held transaction, `operation` is exactly one of create, revise, finalize,
    archive or restore, and `request` is the mutation body, admitted solely by
    `normalize_report_request`. `idempotency_key`, the exact route `path` and
    the caller's canonical raw-body `request_digest` reach the ledger
    unchanged. `now` is canonical UTC seconds supplied by the caller.

    The returned mapping is::

        {"document_to_save": dict | None,
         "response_status": int,
         "response_body": {"data": dict, "meta": {"replayed": bool}}}

    An exact replay is answered from the receipt that recorded it: the status
    and body are the original ones with `meta.replayed` flipped to true, no
    callback is called, no transition is planned, and `document_to_save` is
    `None`, so a retry after ten later mutations saves nothing and still gets
    its first answer. Every other outcome is either a refusal or a fresh
    result carrying the document its own receipt is already appended to —
    there is no successful path that returns without one.

    Nothing returned shares a mutable reference with anything passed in or
    with anything a callback returned, so the caller may keep, mutate or
    serialize the result without disturbing the document it read.
    """

    body = normalize_report_request(operation, request)
    # `normalize_report_request` has proved `operation` is one of the five
    # exact names, so it is a `str` from here on.
    name = str(operation)
    target = _target(name, target_report_uid)
    prepared = prepare_report_replay(
        document,
        workspace_uid=body["workspace_uid"],
        key=idempotency_key,
        method=_METHOD,
        path=path,
        request_digest=request_digest,
        now=now,
    )
    replay = prepared["replay"]
    if replay is not None:
        return {
            "document_to_save": None,
            "response_status": replay["response_status"],
            "response_body": replay["response_body"],
        }
    plan, source_stale = _planned(
        prepared["document"],
        name,
        request,
        body,
        target,
        now,
        allocate_report_uid,
        current_day_digest,
    )
    status = _CREATED if name == "create" else _OK
    data = _data(name, plan, source_stale)
    response_body = {"data": data, "meta": {"replayed": False}}
    saved = append_report_receipt(
        plan["document"],
        workspace_uid=body["workspace_uid"],
        key=idempotency_key,
        method=_METHOD,
        path=path,
        request_digest=request_digest,
        response_status=status,
        response_body=response_body,
        now=now,
    )
    return {
        "document_to_save": saved,
        "response_status": status,
        "response_body": response_body,
    }
