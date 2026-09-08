"""Read-only owner GET routes for the six parity CLI commands.

These handlers call the same WorkStack methods the exclusive-local CLI uses.
They do not project GUI payloads, execute subprocesses, or write Store
documents. Route admission is one frozen GET table; the HTTP path selects the
command, never a request body.
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from workstack.cli_read_contract import (
    ENTITY_ID_LIMIT,
    QUERY_LIMIT,
    CliReadHttpError,
    canonical_workspace_uid,
    invalid_query,
    require_entity_id,
)
from workstack.http_route_types import GetRoute, _get_route


SYNC_STATES = frozenset({"external-change-detected", "in-sync", "invalid"})

BACKLOG_LIST_PATH = "/api/v1/cli/backlog"
BACKLOG_SHOW_PATH = "/api/v1/cli/backlog/{id}"
OKR_LIST_PATH = "/api/v1/cli/okr"
OKR_ROLLUP_PATH = "/api/v1/cli/okr/rollup"
WORKLOG_LIST_PATH = "/api/v1/cli/worklog"
WEEKLY_PATH = "/api/v1/cli/weekly"

CLI_GET_ROUTES: tuple[GetRoute, ...] = (
    _get_route(r"/api/v1/cli/okr/rollup", "_get_cli_okr_rollup"),
    _get_route(r"/api/v1/cli/backlog/([^/]+)", "_get_cli_backlog_show"),
    _get_route(r"/api/v1/cli/backlog", "_get_cli_backlog_list"),
    _get_route(r"/api/v1/cli/okr", "_get_cli_okr_list"),
    _get_route(r"/api/v1/cli/worklog", "_get_cli_worklog_list"),
    _get_route(r"/api/v1/cli/weekly", "_get_cli_weekly"),
)


def _parse_query(query: str, allowed: frozenset[str], required: frozenset[str]) -> dict[str, str]:
    if type(query) is not str or len(query) > QUERY_LIMIT:
        raise invalid_query()
    parsed = parse_qs(query, keep_blank_values=True)
    if set(parsed) - allowed or required - set(parsed):
        raise invalid_query()
    values: dict[str, str] = {}
    for key, items in parsed.items():
        if len(items) != 1 or type(items[0]) is not str:
            raise invalid_query()
        values[key] = items[0]
    canonical_workspace_uid(values["workspace_uid"])
    return values


def _require_owner_identity(stack: Any, workspace_uid: str) -> None:
    storage = stack.storage_status()
    owner_uid = storage.get("workspace_id") if isinstance(storage, dict) else None
    if canonical_workspace_uid(owner_uid) != workspace_uid:
        raise CliReadHttpError(
            "workspace_mismatch",
            "workspace identity does not match this data directory",
            409,
        )
    status = stack.store.sync_status()
    state = status.get("state") if isinstance(status, dict) else None
    if type(state) is not str or state not in SYNC_STATES:
        raise CliReadHttpError(
            "store_sync_required",
            "the running Work Stack store is not in-sync; resolve synchronization first",
            409,
        )
    if state != "in-sync":
        raise CliReadHttpError(
            "store_sync_required",
            "the running Work Stack store is not in-sync; resolve synchronization first",
            409,
            {
                "state": status.get("state"),
                "generation": status.get("generation"),
                "changed_files": status.get("changed_files", []),
            },
        )


def _coherent(stack: Any, read: Callable[[], Any]) -> Any:
    """Run one owner-route read under a single outer Store transaction.

    ``list_tasks`` and ``get_task`` load the Task and Activity documents in two
    separate store transactions, and this handler runs on a threading server, so
    an owner write can commit between those loads and hand the reader a Task
    revision that its Activity facts contradict. One outer boundary makes every
    route return a complete before-or-after snapshot. The already transactional
    readers nest re-entrantly and are unchanged.
    """

    with stack.store.transaction():
        return read()


def backlog_list_payload(stack: Any, query: str) -> list[dict[str, Any]]:
    values = _parse_query(query, frozenset({"workspace_uid", "status"}), frozenset({"workspace_uid"}))
    _require_owner_identity(stack, values["workspace_uid"])
    return _coherent(stack, lambda: stack.list_tasks(values.get("status", "active")))


def backlog_show_payload(stack: Any, query: str, task_id: str) -> dict[str, Any]:
    values = _parse_query(query, frozenset({"workspace_uid"}), frozenset({"workspace_uid"}))
    _require_owner_identity(stack, values["workspace_uid"])
    entity_id = require_entity_id(task_id)
    return _coherent(stack, lambda: stack.get_task(entity_id))


def okr_list_payload(stack: Any, query: str) -> list[dict[str, Any]]:
    values = _parse_query(query, frozenset({"workspace_uid", "status"}), frozenset({"workspace_uid"}))
    _require_owner_identity(stack, values["workspace_uid"])
    return _coherent(stack, lambda: stack.list_objectives(values.get("status", "active")))


def okr_rollup_payload(stack: Any, query: str) -> list[dict[str, Any]]:
    values = _parse_query(query, frozenset({"workspace_uid"}), frozenset({"workspace_uid"}))
    _require_owner_identity(stack, values["workspace_uid"])
    return _coherent(stack, stack.objective_rollup)


def worklog_list_payload(stack: Any, query: str) -> dict[str, Any]:
    values = _parse_query(query, frozenset({"workspace_uid", "date"}), frozenset({"workspace_uid"}))
    _require_owner_identity(stack, values["workspace_uid"])
    return _coherent(stack, lambda: stack.list_worklog(values.get("date")))


def weekly_payload(stack: Any, query: str) -> dict[str, Any]:
    values = _parse_query(
        query, frozenset({"workspace_uid", "end", "days"}), frozenset({"workspace_uid"})
    )
    _require_owner_identity(stack, values["workspace_uid"])
    days = 7
    if "days" in values:
        raw = values["days"]
        if type(raw) is not str or not re.fullmatch(r"-?\d+", raw):
            raise invalid_query()
        days = int(raw)
    end = values.get("end")
    return _coherent(stack, lambda: stack.weekly_report(end, days))


def _serve_cli_get(
    handler: Any,
    parsed: Any,
    loader: Callable[[], Any],
) -> None:
    handler._require_browser_mutation()
    if handler.headers.get_all("Idempotency-Key") is not None:
        handler.send_api_error(
            "unsupported_idempotency_key",
            "CLI reads do not accept Idempotency-Key",
            400,
        )
        return
    if parsed.params:
        handler.send_api_error("not_found", "API endpoint not found", 404)
        return
    try:
        payload = loader()
    except CliReadHttpError as error:
        handler.send_api_error(error.code, error.message, error.status, error.details)
        return
    handler.send_json({"data": payload})


class CliReadHttpMixin:
    """Thin GET adapters; Host/Origin/CSRF stay on the existing handler."""

    def _get_cli_backlog_list(self, parsed: Any, match: re.Match[str]) -> None:
        _serve_cli_get(self, parsed, lambda: backlog_list_payload(self.stack, parsed.query))

    def _get_cli_backlog_show(self, parsed: Any, match: re.Match[str]) -> None:
        task_id = unquote(match.group(1))
        _serve_cli_get(
            self, parsed, lambda: backlog_show_payload(self.stack, parsed.query, task_id)
        )

    def _get_cli_okr_list(self, parsed: Any, match: re.Match[str]) -> None:
        _serve_cli_get(self, parsed, lambda: okr_list_payload(self.stack, parsed.query))

    def _get_cli_okr_rollup(self, parsed: Any, match: re.Match[str]) -> None:
        _serve_cli_get(self, parsed, lambda: okr_rollup_payload(self.stack, parsed.query))

    def _get_cli_worklog_list(self, parsed: Any, match: re.Match[str]) -> None:
        _serve_cli_get(self, parsed, lambda: worklog_list_payload(self.stack, parsed.query))

    def _get_cli_weekly(self, parsed: Any, match: re.Match[str]) -> None:
        _serve_cli_get(self, parsed, lambda: weekly_payload(self.stack, parsed.query))


__all__ = (
    "BACKLOG_LIST_PATH",
    "BACKLOG_SHOW_PATH",
    "CLI_GET_ROUTES",
    "CliReadHttpError",
    "CliReadHttpMixin",
    "ENTITY_ID_LIMIT",
    "OKR_LIST_PATH",
    "OKR_ROLLUP_PATH",
    "QUERY_LIMIT",
    "canonical_workspace_uid",
    "invalid_query",
    "WEEKLY_PATH",
    "WORKLOG_LIST_PATH",
    "require_entity_id",
)
