"""GET route admission and the read handlers the v1 table dispatches to.

Precedence is table order and nothing else: the frozen CLI owner-read table is
consulted before the general v1 table, and the first regular expression that
matches wins. No handler here opens a transaction of its own beyond the single
Store read its projection performs.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote

from .http_route_types import GetRoute, V1_GET_ROUTES
from .cli_read_http import CLI_GET_ROUTES
from .report_documents_http import is_report_route_alias
from .server_errors import RequestError
from .service import DomainError


REVIEW_MIN_DAYS = 1
REVIEW_MAX_DAYS = 31


def _single_valued_query(
    query: str, allowed: set[str], defaults: dict[str, list[str]], message: str
) -> dict[str, list[str]]:
    """One frozen shape check: unknown keys and repeats are the same refusal."""

    parsed = parse_qs(query, keep_blank_values=True)
    if set(parsed) - allowed:
        raise RequestError("invalid_query", message, 400)
    for name, fallback in defaults.items():
        if len(parsed.get(name, fallback)) != 1:
            raise RequestError("invalid_query", message, 400)
    return parsed


def _reject_query(query: str, message: str) -> None:
    if query:
        raise RequestError("invalid_query", message, 400)


class ReadRouteMixin:
    """The v1 GET surface: route selection plus the read handlers."""

    @staticmethod
    def _match_v1_get_route(path: str) -> tuple[GetRoute | None, re.Match[str] | None]:
        for route in CLI_GET_ROUTES:
            match = route.match(path)
            if match is not None:
                return route, match
        for route in V1_GET_ROUTES:
            match = route.match(path)
            if match is not None:
                return route, match
        return None, None

    def _handle_v1_get(self, parsed: Any) -> None:
        route, match = self._match_v1_get_route(parsed.path)
        # `urlparse` strips a trailing `;params` run off the last path segment,
        # so a report route can be reached by a target the router never saw.
        # For the report surface that alias is not a route: it is answered
        # here, before the handler, so it opens nothing and records nothing.
        # Every other endpoint keeps the spelling tolerance it shipped with.
        if (
            route is None
            or match is None
            or is_report_route_alias(route.handler, self.path)
        ):
            self.send_api_error("not_found", "API endpoint not found", 404)
            return
        getattr(self, route.handler)(parsed, match)

    def _get_session(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": {"csrf_token": self.server.csrf_token}})

    def _get_health(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": {"api_version": "v1", "status": "ready"}})

    def _get_sync_status(self, parsed: Any, match: re.Match[str]) -> None:
        _reject_query(parsed.query, "sync status query is invalid")
        self.send_json({"data": self.stack.store.sync_status()})

    def _get_sync_rebind_preview(self, parsed: Any, match: re.Match[str]) -> None:
        _reject_query(parsed.query, "sync rebind preview query is invalid")
        self.send_json({"data": self.stack.store.workspace_rebind_preview()})

    def _get_sync_events(self, parsed: Any, match: re.Match[str]) -> None:
        query = _single_valued_query(
            parsed.query, {"after"}, {"after": ["0"]}, "sync event query is invalid"
        )
        try:
            after = int(query.get("after", ["0"])[0])
            result = self.stack.store.sync_events(after)
        except ValueError as error:
            raise RequestError("invalid_query", str(error), 400) from error
        self.send_json({"data": result})

    def _get_events(self, parsed: Any, match: re.Match[str]) -> None:
        _reject_query(parsed.query, "event stream query is invalid")
        raw_cursor = self._header_once("Last-Event-ID") or "0"
        try:
            after = int(raw_cursor)
            if after < 0:
                raise ValueError
        except ValueError as error:
            raise RequestError(
                "invalid_header", "Last-Event-ID is invalid", 400
            ) from error
        self.send_sync_event(after)

    def _get_storage(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": self.stack.storage_status()})

    def _get_workspace(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": self.stack.workspace_projection()})

    def _get_search(self, parsed: Any, match: re.Match[str]) -> None:
        query = _single_valued_query(
            parsed.query,
            {"q", "limit"},
            {"q": [], "limit": ["30"]},
            "search query is invalid",
        )
        try:
            limit = int(query.get("limit", ["30"])[0])
            result = self.stack.search_projection(query["q"][0], limit)
        except (ValueError, DomainError) as error:
            raise RequestError("invalid_query", str(error), 400) from error
        self.send_json({"data": result})

    def _get_checkpoint_audit(self, parsed: Any, match: re.Match[str]) -> None:
        """The complete audit view. No query parameter is accepted."""

        if parse_qs(parsed.query, keep_blank_values=True):
            raise RequestError(
                "invalid_query", "checkpoint audit accepts no query parameters", 400
            )
        self.send_json({"data": self.stack.list_checkpoint_audit()})

    def _get_review(self, parsed: Any, match: re.Match[str]) -> None:
        query = _single_valued_query(
            parsed.query,
            {"date", "days"},
            {"date": [], "days": ["7"]},
            "review query is invalid",
        )
        try:
            days = int(query.get("days", ["7"])[0])
        except ValueError as error:
            raise RequestError("invalid_query", "review days is invalid", 400) from error
        if days < REVIEW_MIN_DAYS or days > REVIEW_MAX_DAYS:
            raise RequestError(
                "invalid_query", "review days must be between 1 and 31", 400
            )
        self.send_json(
            {"data": self.stack.review_projection(query["date"][0], days)}
        )

    def _get_work_sessions(self, parsed: Any, match: re.Match[str]) -> None:
        _reject_query(parsed.query, "work session query is invalid")
        self.send_json({"data": self.stack.work_sessions_projection()})

    def _get_objective(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json(
            {"data": self.stack.objective_detail(unquote(match.group(1)))}
        )

    def _get_snapshot(self, parsed: Any, match: re.Match[str]) -> None:
        artifact = self.stack.planning_snapshot(unquote(match.group(1)))
        self.send_json({
            "data": {
                "snapshot": artifact.snapshot,
                "digest": artifact.digest,
                "filename": artifact.filename,
                "omissions": list(artifact.omissions),
            }
        })

    def _get_task(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": self.stack.task_detail(unquote(match.group(1)))})

    def _get_captures(self, parsed: Any, match: re.Match[str]) -> None:
        query = _single_valued_query(
            parsed.query, {"status"}, {"status": ["inbox"]}, "capture query is invalid"
        )
        status = query.get("status", ["inbox"])[0]
        self.send_json({"data": self.stack.list_captures(status)})


__all__ = ("ReadRouteMixin",)
