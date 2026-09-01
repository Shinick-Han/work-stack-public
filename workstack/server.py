"""Loopback-only HTTP server for the Work Stack application."""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from .capture import CaptureValidationError, canonical_digest
from .store import StoreAdoptionConflictError, StoreExternalChangeError
from .service import (
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    ReplyReceiptConflictError,
    RevisionConflictError,
    SnapshotExportConflictError,
    SourceRevisionConflictError,
    StaleCaptureError,
    WorkSessionConflictError,
    WorkStack,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_WEB_ROOT = PROJECT_ROOT / "web"
FRONTEND_ROOT = PROJECT_ROOT / "frontend" / "dist"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CAPTURE_BODY_LIMIT = 64 * 1024
DEFAULT_BODY_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class PostRoute:
    name: str
    pattern: re.Pattern[str]
    handler: str

    def match(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)


def _post_route(name: str, path_pattern: str, handler: str) -> PostRoute:
    return PostRoute(name, re.compile(path_pattern), handler)


V1_POST_ROUTES = (
    _post_route("sync_adopt", r"/api/v1/sync/adopt", "_post_sync_adopt"),
    _post_route("sync_rebind", r"/api/v1/sync/rebind-workspace", "_post_sync_rebind"),
    _post_route("task_create", r"/api/v1/tasks", "_post_task_create"),
    _post_route("work_session_create", r"/api/v1/work-sessions", "_post_work_session_create"),
    _post_route("work_session_action", r"/api/v1/work-sessions/([^/]+)/(pause|resume|stop|worklog)", "_post_work_session_action"),
    _post_route("backup", r"/api/v1/maintenance/backup", "_post_backup"),
    _post_route("snapshot_export", r"/api/v1/tasks/([^/]+)/snapshot/export", "_post_snapshot_export"),
    _post_route("task_note", r"/api/v1/tasks/([^/]+)/notes", "_post_task_note"),
    _post_route("task_subtask", r"/api/v1/tasks/([^/]+)/subtasks", "_post_task_subtask"),
    _post_route("objective_create", r"/api/v1/objectives", "_post_objective_create"),
    _post_route("key_result_create", r"/api/v1/objectives/([^/]+)/key-results", "_post_key_result_create"),
    _post_route("note_create", r"/api/v1/notes", "_post_note_create"),
    _post_route("review_checkin", r"/api/v1/review/checkin", "_post_review_checkin"),
    _post_route("review_entry", r"/api/v1/review/entries", "_post_review_entry"),
    _post_route("capture_ingest", r"/api/v1/captures", "_post_capture_ingest"),
    _post_route("capture_link", r"/api/v1/captures/([^/]+)/link", "_post_capture_link"),
    _post_route("capture_action_task", r"/api/v1/captures/([^/]+)/actions/([^/]+)/task", "_post_capture_action_task"),
    _post_route("capture_task", r"/api/v1/captures/([^/]+)/task", "_post_capture_task"),
    _post_route("capture_dismiss", r"/api/v1/captures/([^/]+)/dismiss", "_post_capture_dismiss"),
    _post_route("reply_create", r"/api/v1/replies", "_post_reply_create"),
    _post_route("reply_receipt", r"/api/v1/replies/([^/]+)/receipt", "_post_reply_receipt"),
)

IDEMPOTENT_POST_ROUTES = frozenset({
    "sync_adopt",
    "sync_rebind",
    "task_create",
    "work_session_create",
    "work_session_action",
    "task_note",
    "task_subtask",
    "objective_create",
    "key_result_create",
    "note_create",
    "review_checkin",
    "review_entry",
    "reply_create",
    "reply_receipt",
})


@dataclass(frozen=True)
class GetRoute:
    pattern: re.Pattern[str]
    handler: str

    def match(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)


def _get_route(path_pattern: str, handler: str) -> GetRoute:
    return GetRoute(re.compile(path_pattern), handler)


V1_GET_ROUTES = (
    _get_route(r"/api/v1/session", "_get_session"),
    _get_route(r"/api/v1/health", "_get_health"),
    _get_route(r"/api/v1/sync/status", "_get_sync_status"),
    _get_route(r"/api/v1/sync/rebind-preview", "_get_sync_rebind_preview"),
    _get_route(r"/api/v1/sync/events", "_get_sync_events"),
    _get_route(r"/api/v1/events", "_get_events"),
    _get_route(r"/api/v1/storage", "_get_storage"),
    _get_route(r"/api/v1/workspace", "_get_workspace"),
    _get_route(r"/api/v1/search", "_get_search"),
    _get_route(r"/api/v1/review", "_get_review"),
    _get_route(r"/api/v1/work-sessions", "_get_work_sessions"),
    _get_route(r"/api/v1/objectives/([^/]+)", "_get_objective"),
    _get_route(r"/api/v1/tasks/([^/]+)/snapshot", "_get_snapshot"),
    _get_route(r"/api/v1/tasks/([^/]+)", "_get_task"),
    _get_route(r"/api/v1/captures", "_get_captures"),
)


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class WorkStackHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        stack: WorkStack,
        *,
        public_port: int | None = None,
    ) -> None:
        host, port = address
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                "non-loopback binding is disabled; use an authenticated reverse proxy"
            )
        if public_port is not None and (
            isinstance(public_port, bool)
            or not isinstance(public_port, int)
            or not 1 <= public_port <= 65_535
        ):
            raise ValueError("public_port must be an integer from 1 to 65535")
        self.stack = stack
        self.csrf_token = secrets.token_urlsafe(32)
        self.capture_token = secrets.token_urlsafe(48)
        self._lease = stack.store.server_lease()
        self._lease.__enter__()
        self._runtime_closed = False
        socket_ready = False
        try:
            stack.store.initialize()
            if host == "::1":
                self.address_family = socket.AF_INET6
            super().__init__(address, Handler)
            socket_ready = True
            actual_host, actual_port = self.server_address[:2]
            self.accepted_host_ports = frozenset(
                (int(actual_port),)
                if public_port is None
                else (int(actual_port), public_port)
            )
            published_host = host if host != "localhost" else "127.0.0.1"
            if actual_host == "0.0.0.0":
                raise ValueError("server resolved to a non-loopback address")
            stack.store.write_runtime_secret(self.capture_token)
            stack.store.write_server_info(published_host, int(actual_port))
        except BaseException:
            stack.store.clear_server_runtime()
            if socket_ready:
                super().server_close()
            self._lease.__exit__(None, None, None)
            self._runtime_closed = True
            raise

    @property
    def actual_port(self) -> int:
        return int(self.server_address[1])

    def server_close(self) -> None:
        if self._runtime_closed:
            return
        self._runtime_closed = True
        try:
            self.stack.store.clear_server_runtime()
            super().server_close()
        finally:
            self._lease.__exit__(None, None, None)


class Handler(BaseHTTPRequestHandler):
    server: WorkStackHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        # Deliberately omit request lines, query strings, bodies, and authorization data.
        return

    @property
    def stack(self) -> WorkStack:
        return self.server.stack

    @property
    def request_id(self) -> str:
        value = getattr(self, "_workstack_request_id", None)
        if value is None:
            value = secrets.token_hex(8)
            self._workstack_request_id = value
        return value

    def send_json(self, value: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-WorkStack-Request-Id", self.request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A closed browser tab is an expected transport event, not a product error.
            return

    def send_snapshot(
        self, body: bytes, filename: str, digest: str
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
        self.send_header("X-WorkStack-Snapshot-Digest", digest)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-WorkStack-Request-Id", self.request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def send_backup(
        self,
        body: bytes,
        filename: str,
        digest: str,
        workspace_id: str,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
        self.send_header("X-WorkStack-Backup-Digest", digest)
        self.send_header("X-WorkStack-Workspace-Id", workspace_id)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-WorkStack-Request-Id", self.request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def send_api_error(
        self,
        code: str,
        message: str,
        status: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.send_json(
            {"error": {"code": code, "message": message, "details": details or {}}},
            status,
        )

    def send_sync_event(self, after: int) -> None:
        payload = self.stack.store.wait_for_sync_events(after)
        body = (
            "retry: 3000\n"
            "id: {event_id}\n"
            "event: sync\n"
            "data: {data}\n\n"
        ).format(
            event_id=payload["latest_event_id"],
            data=json.dumps(
                {
                    "generation": payload["generation"],
                    "state": payload["state"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-WorkStack-Request-Id", self.request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _header_once(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise RequestError("invalid_header", "{} must occur once".format(name), 400)
        return values[0] if values else None

    def _host_parts(self) -> tuple[str, int]:
        host_header = self._header_once("Host")
        if not host_header:
            raise RequestError("invalid_host", "Host header is required", 400)
        if any(char in host_header for char in "\r\n/@"):
            raise RequestError("invalid_host", "Host header is invalid", 400)
        try:
            parsed = urlsplit("//" + host_header)
            hostname = (parsed.hostname or "").casefold()
            port = parsed.port
        except ValueError as error:
            raise RequestError("invalid_host", "Host header is invalid", 400) from error
        if hostname not in LOOPBACK_HOSTS or port not in self.server.accepted_host_ports:
            raise RequestError("invalid_host", "Host does not match the loopback server", 400)
        return hostname, port

    def _validate_host(self) -> tuple[str, int]:
        return self._host_parts()

    def _require_browser_mutation(self) -> None:
        hostname, port = self._host_parts()
        origin = self._header_once("Origin")
        if not origin:
            raise RequestError("origin_required", "same-origin Origin is required", 403)
        try:
            parsed = urlsplit(origin)
            origin_port = parsed.port
        except ValueError as error:
            raise RequestError("invalid_origin", "Origin is invalid", 403) from error
        if (
            parsed.scheme != "http"
            or (parsed.hostname or "").casefold() != hostname
            or origin_port != port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RequestError("invalid_origin", "Origin is not same-origin", 403)
        csrf = self._header_once("X-WorkStack-CSRF")
        if not csrf or not secrets.compare_digest(csrf, self.server.csrf_token):
            raise RequestError("invalid_csrf", "CSRF token is missing or invalid", 403)

    def _has_agent_bearer(self) -> bool:
        authorization = self._header_once("Authorization")
        if authorization is None:
            return False
        if not authorization.startswith("Bearer "):
            raise RequestError("invalid_authorization", "capture authorization is invalid", 401)
        token = authorization[7:]
        if not token or not secrets.compare_digest(token, self.server.capture_token):
            raise RequestError("invalid_authorization", "capture authorization is invalid", 401)
        return True

    def _require_json_content_type(self) -> None:
        content_type = self._header_once("Content-Type") or ""
        media_type = content_type.split(";", 1)[0].strip().casefold()
        if media_type != "application/json":
            raise RequestError(
                "unsupported_media_type", "Content-Type must be application/json", 415
            )

    def read_json(self, maximum: int = DEFAULT_BODY_LIMIT) -> tuple[dict[str, Any], str]:
        self._require_json_content_type()
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("invalid_body", "chunked request bodies are not accepted", 400)
        raw_length = self._header_once("Content-Length")
        if raw_length is None:
            raise RequestError("length_required", "Content-Length is required", 411)
        try:
            length = int(raw_length)
        except ValueError as error:
            raise RequestError("invalid_body", "Content-Length is invalid", 400) from error
        if length < 0:
            raise RequestError("invalid_body", "Content-Length is invalid", 400)
        if length > maximum:
            raise RequestError(
                "body_too_large", "request body exceeds {} bytes".format(maximum), 413
            )
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError("invalid_body", "request body is incomplete", 400)
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestError("invalid_json", "request body is not valid UTF-8 JSON", 400) from error
        if not isinstance(value, dict):
            raise RequestError("invalid_body", "request body must be a JSON object", 400)
        return value, canonical_digest(value)

    def _idempotency_key(self) -> str:
        value = self._header_once("Idempotency-Key")
        if value is None:
            raise RequestError("idempotency_key_required", "Idempotency-Key is required", 400)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
            raise RequestError("invalid_idempotency_key", "Idempotency-Key is invalid", 400)
        return value

    def _dispatch_error(self, error: BaseException) -> None:
        if isinstance(error, RequestError):
            self.send_api_error(error.code, str(error), error.status, error.details)
        elif isinstance(error, NotFoundError):
            self.send_api_error(error.code, str(error), 404, error.details)
        elif isinstance(
            error,
            (
                RevisionConflictError,
                IdempotencyConflictError,
                ReplyReceiptConflictError,
                SnapshotExportConflictError,
                StaleCaptureError,
                SourceRevisionConflictError,
                WorkSessionConflictError,
            ),
        ):
            self.send_api_error(error.code, str(error), 409, error.details)
        elif isinstance(error, CaptureValidationError):
            self.send_api_error(error.code, str(error), 400, error.details)
        elif isinstance(error, StoreExternalChangeError):
            self.send_api_error(
                "store_sync_required",
                str(error),
                409,
                {
                    "state": (
                        "invalid"
                        if error.status.get("status") == "external-change-invalid"
                        else error.status.get("status")
                    ),
                    "generation": error.status.get("generation"),
                    "changed_files": error.status.get("changed_files", []),
                },
            )
        elif isinstance(error, StoreAdoptionConflictError):
            self.send_api_error("idempotency_conflict", str(error), 409)
        elif isinstance(error, DomainError):
            self.send_api_error(error.code, str(error), 400, error.details)
        elif isinstance(error, (ValueError, json.JSONDecodeError)):
            self.send_api_error("invalid_request", str(error), 400)
        else:
            raise error

    @staticmethod
    def _match_v1_get_route(path: str) -> tuple[GetRoute | None, re.Match[str] | None]:
        for route in V1_GET_ROUTES:
            match = route.match(path)
            if match is not None:
                return route, match
        return None, None

    def _handle_v1_get(self, parsed: Any) -> None:
        route, match = self._match_v1_get_route(parsed.path)
        if route is None or match is None:
            self.send_api_error("not_found", "API endpoint not found", 404)
            return
        getattr(self, route.handler)(parsed, match)

    def _get_session(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": {"csrf_token": self.server.csrf_token}})

    def _get_health(self, parsed: Any, match: re.Match[str]) -> None:
        self.send_json({"data": {"api_version": "v1", "status": "ready"}})

    def _get_sync_status(self, parsed: Any, match: re.Match[str]) -> None:
        if parsed.query:
            raise RequestError("invalid_query", "sync status query is invalid", 400)
        self.send_json({"data": self.stack.store.sync_status()})

    def _get_sync_rebind_preview(self, parsed: Any, match: re.Match[str]) -> None:
        if parsed.query:
            raise RequestError("invalid_query", "sync rebind preview query is invalid", 400)
        self.send_json({"data": self.stack.store.workspace_rebind_preview()})

    def _get_sync_events(self, parsed: Any, match: re.Match[str]) -> None:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
            raise RequestError("invalid_query", "sync event query is invalid", 400)
        try:
            after = int(query.get("after", ["0"])[0])
            result = self.stack.store.sync_events(after)
        except ValueError as error:
            raise RequestError("invalid_query", str(error), 400) from error
        self.send_json({"data": result})

    def _get_events(self, parsed: Any, match: re.Match[str]) -> None:
        if parsed.query:
            raise RequestError("invalid_query", "event stream query is invalid", 400)
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
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            set(query) - {"q", "limit"}
            or len(query.get("q", [])) != 1
            or len(query.get("limit", ["30"])) != 1
        ):
            raise RequestError("invalid_query", "search query is invalid", 400)
        try:
            limit = int(query.get("limit", ["30"])[0])
            result = self.stack.search_projection(query["q"][0], limit)
        except (ValueError, DomainError) as error:
            raise RequestError("invalid_query", str(error), 400) from error
        self.send_json({"data": result})

    def _get_review(self, parsed: Any, match: re.Match[str]) -> None:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            set(query) - {"date", "days"}
            or len(query.get("date", [])) != 1
            or len(query.get("days", ["7"])) != 1
        ):
            raise RequestError("invalid_query", "review query is invalid", 400)
        try:
            days = int(query.get("days", ["7"])[0])
        except ValueError as error:
            raise RequestError("invalid_query", "review days is invalid", 400) from error
        if days < 1 or days > 31:
            raise RequestError(
                "invalid_query", "review days must be between 1 and 31", 400
            )
        self.send_json(
            {"data": self.stack.review_projection(query["date"][0], days)}
        )

    def _get_work_sessions(self, parsed: Any, match: re.Match[str]) -> None:
        if parsed.query:
            raise RequestError("invalid_query", "work session query is invalid", 400)
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
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {"status"} or len(query.get("status", ["inbox"])) != 1:
            raise RequestError("invalid_query", "capture query is invalid", 400)
        status = query.get("status", ["inbox"])[0]
        self.send_json({"data": self.stack.list_captures(status)})

    def do_GET(self) -> None:
        try:
            self._validate_host()
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/state":
                self.send_json(self.stack.snapshot())
                return
            if path.startswith("/api/v1/"):
                self._handle_v1_get(parsed)
                return
            if path.startswith("/api/"):
                self.send_api_error("not_found", "API endpoint not found", 404)
                return
            self._serve_static(path)
        except BaseException as error:
            self._dispatch_error(error)


    def _serve_static(self, request_path: str) -> None:
        if FRONTEND_ROOT.is_dir() and (FRONTEND_ROOT / "index.html").is_file():
            root = FRONTEND_ROOT.resolve()
            relative = unquote(request_path).lstrip("/") or "index.html"
            candidate = (root / relative).resolve()
            if not candidate.is_relative_to(root):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not candidate.is_file():
                candidate = root / "index.html"
        else:
            if request_path not in ("/", "/index.html"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = LEGACY_WEB_ROOT / "index.html"
        try:
            body = candidate.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _match_v1_post_route(path: str) -> tuple[PostRoute | None, re.Match[str] | None]:
        for route in V1_POST_ROUTES:
            match = route.match(path)
            if match is not None:
                return route, match
        return None, None

    @staticmethod
    def _post_needs_idempotency(path: str, route: PostRoute | None) -> bool:
        return path.startswith("/api/v1/captures") or (
            route is not None and route.name in IDEMPOTENT_POST_ROUTES
        )

    def _send_service_result(self, result: dict[str, Any]) -> None:
        self.send_json(result["body"], result["status"])

    def _handle_v1_post(self, path: str) -> None:
        route, match = self._match_v1_post_route(path)
        maximum = (
            CAPTURE_BODY_LIMIT
            if path.startswith("/api/v1/captures")
            else DEFAULT_BODY_LIMIT
        )
        # Consume the bounded JSON body before returning an authorization
        # error. Closing a Windows socket with unread request bytes can reset
        # the connection before the client receives the JSON error.
        body, request_digest = self.read_json(maximum)
        is_agent_ingest = (
            route is not None
            and route.name == "capture_ingest"
            and self._has_agent_bearer()
        )
        if not is_agent_ingest:
            self._require_browser_mutation()
        idempotency_key = (
            self._idempotency_key()
            if self._post_needs_idempotency(path, route)
            else ""
        )
        if route is None or match is None:
            self.send_api_error("not_found", "API endpoint not found", 404)
            return
        handler = getattr(self, route.handler)
        handler(path, match, body, request_digest, idempotency_key)

    def _handle_legacy_post(self, path: str) -> None:
        self.read_json()
        self._require_browser_mutation()
        if path == "/api/tasks":
            self.send_api_error(
                "legacy_task_writer_disabled",
                "Use POST /api/v1/tasks with Idempotency-Key",
                410,
            )
            return
        if path in {"/api/objectives", "/api/worklog", "/api/notes"}:
            self.send_api_error(
                "legacy_writer_disabled",
                "Use the corresponding versioned /api/v1 endpoint",
                410,
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _post_sync_adopt(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"expected_generation", "expected_manifest_digest"}:
            raise RequestError(
                "invalid_body",
                "sync adoption requires expected_generation and expected_manifest_digest",
                400,
            )
        result = self.stack.store.adopt_external_change(
            body["expected_generation"],
            body["expected_manifest_digest"],
            idempotency_key,
        )
        self.send_json({"data": result})

    def _post_sync_rebind(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        expected = {
            "confirmed",
            "expected_manifest_workspace_id",
            "expected_candidate_workspace_id",
            "expected_manifest_digest",
            "expected_candidate_digest",
        }
        if set(body) != expected:
            raise RequestError(
                "invalid_body",
                "sync rebind body is invalid",
                400,
            )
        result = self.stack.store.rebind_workspace_identity(
            confirmed=body["confirmed"],
            expected_manifest_workspace_id=body["expected_manifest_workspace_id"],
            expected_candidate_workspace_id=body["expected_candidate_workspace_id"],
            expected_manifest_digest=body["expected_manifest_digest"],
            expected_candidate_digest=body["expected_candidate_digest"],
            idempotency_key=idempotency_key,
        )
        self.send_json({
            "data": {
                "state": result["state"],
                "workspace_id": result["workspace_id"],
                "generation": result["generation"],
                "recovery_receipt_digest": result["recovery"]["receipt_digest"],
                "planning_mutated": result["recovery"]["planning_mutated"],
            }
        })

    def _post_task_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.create_task_v1(body, idempotency_key, path=path)
        )

    def _post_work_session_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"task_id"} or not isinstance(body["task_id"], str):
            raise RequestError(
                "invalid_body", "work session creation requires only task_id", 400
            )
        self._send_service_result(
            self.stack.start_work_session_v1(body, idempotency_key, path=path)
        )

    def _post_work_session_action(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        session_id = unquote(match.group(1))
        action = match.group(2)
        if action == "worklog":
            self._post_work_session_log(
                path, session_id, body, idempotency_key
            )
            return
        if body:
            raise RequestError(
                "invalid_body", "work session transitions require an empty body", 400
            )
        self._send_service_result(
            self.stack.transition_work_session_v1(
                session_id, action, body, idempotency_key, path=path
            )
        )

    def _post_work_session_log(
        self, path: str, session_id: str, body: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        fields = ("done", "next", "blockers")
        if set(body) != set(fields) or any(
            not isinstance(body[field], list)
            or any(not isinstance(item, str) for item in body[field])
            for field in fields
        ):
            raise RequestError(
                "invalid_body",
                "work session worklog requires string arrays for done, next, and blockers",
                400,
            )
        self._send_service_result(
            self.stack.record_work_session_v1(
                session_id, body, idempotency_key, path=path
            )
        )

    def _post_backup(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"confirmed"} or body["confirmed"] is not True:
            raise RequestError(
                "invalid_body", "backup download requires explicit confirmation", 400
            )
        download = self.stack.create_backup_download()
        self.send_backup(
            download.body, download.filename, download.digest, download.workspace_id
        )

    def _post_snapshot_export(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        expected = {
            "disclosure_confirmed", "expected_revision", "expected_digest"
        }
        if set(body) != expected:
            raise RequestError(
                "invalid_body",
                "snapshot export confirmation has unknown or missing fields",
                400,
            )
        artifact = self.stack.confirmed_snapshot_export(
            unquote(match.group(1)),
            body["expected_revision"],
            body["expected_digest"],
            body["disclosure_confirmed"],
        )
        self.send_snapshot(
            artifact.canonical_bytes, artifact.filename, artifact.digest
        )

    def _post_task_note(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"text", "revision"}:
            raise RequestError(
                "invalid_body", "task note requires only text and revision", 400
            )
        self._send_service_result(
            self.stack.add_task_note_v1(
                unquote(match.group(1)), body, idempotency_key, path=path
            )
        )

    def _post_task_subtask(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"title", "priority", "revision"}:
            raise RequestError(
                "invalid_body",
                "subtask creation requires only title, priority, and revision",
                400,
            )
        self._send_service_result(
            self.stack.add_subtask_v1(
                unquote(match.group(1)), body, idempotency_key, path=path
            )
        )

    def _post_objective_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"objective", "quarter"}
            or not isinstance(body["objective"], str)
            or not isinstance(body["quarter"], str)
        ):
            raise RequestError(
                "invalid_body",
                "objective creation requires only string objective and quarter fields",
                400,
            )
        self._send_service_result(
            self.stack.create_objective_v1(body, idempotency_key, path=path)
        )

    def _post_key_result_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"text", "target", "revision"}
            or not isinstance(body["text"], str)
            or not isinstance(body["target"], str)
            or type(body["revision"]) is not int
        ):
            raise RequestError(
                "invalid_body",
                "key result creation requires only text, target, and revision",
                400,
            )
        self._send_service_result(
            self.stack.add_key_result_v1(
                unquote(match.group(1)), body, idempotency_key, path=path
            )
        )

    def _post_note_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"text", "links"}
            or not isinstance(body["text"], str)
            or not isinstance(body["links"], list)
            or any(not isinstance(link, str) for link in body["links"])
        ):
            raise RequestError(
                "invalid_body",
                "note creation requires only string text and a string links array",
                400,
            )
        self._send_service_result(
            self.stack.create_note_v1(body, idempotency_key, path=path)
        )

    def _post_review_checkin(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"date", "time"}
            or not isinstance(body["date"], str)
            or not isinstance(body["time"], str)
        ):
            raise RequestError(
                "invalid_body",
                "review check-in requires only string date and time fields",
                400,
            )
        self._send_service_result(
            self.stack.checkin_v1(body, idempotency_key, path=path)
        )

    def _post_review_entry(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        fields = ("done", "next", "blockers")
        if (
            set(body) != {"date", "task_id", *fields}
            or not isinstance(body["date"], str)
            or not isinstance(body["task_id"], str)
            or any(
                not isinstance(body[field], list)
                or any(not isinstance(item, str) for item in body[field])
                for field in fields
            )
        ):
            raise RequestError(
                "invalid_body",
                "review entry requires date, task_id, and string arrays for done, next, and blockers",
                400,
            )
        self._send_service_result(
            self.stack.add_worklog_v1(body, idempotency_key, path=path)
        )

    def _post_capture_ingest(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.ingest_capture(
                body, idempotency_key, request_digest, path=path
            )
        )

    def _post_capture_link(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"task_id"} or not isinstance(body["task_id"], str):
            raise RequestError("invalid_body", "link requires only task_id", 400)
        self._send_service_result(
            self.stack.link_capture(
                unquote(match.group(1)), body["task_id"], idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_capture_action_task(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) - {"objective_ids"} or not isinstance(
            body.get("objective_ids", []), list
        ):
            raise RequestError(
                "invalid_body", "conversion accepts only objective_ids", 400
            )
        self._send_service_result(
            self.stack.convert_capture_action(
                unquote(match.group(1)), unquote(match.group(2)),
                body.get("objective_ids", []), idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_capture_task(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.create_task_from_capture(
                unquote(match.group(1)), body, idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_capture_dismiss(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if body:
            raise RequestError(
                "invalid_body", "dismiss requires an empty object", 400
            )
        self._send_service_result(
            self.stack.dismiss_capture(
                unquote(match.group(1)), idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_reply_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.approve_reply(
                body, idempotency_key, request_digest, path=path
            )
        )

    def _post_reply_receipt(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.apply_reply_receipt(
                unquote(match.group(1)), body, idempotency_key,
                request_digest, path=path,
            )
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            self._validate_host()
            if path.startswith("/api/v1/"):
                self._handle_v1_post(path)
            else:
                self._handle_legacy_post(path)
        except BaseException as error:
            self._dispatch_error(error)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            self._validate_host()
            if path.startswith("/api/v1/"):
                body, _ = self.read_json()
                self._require_browser_mutation()
                subtask_match = re.fullmatch(
                    r"/api/v1/tasks/([^/]+)/subtasks/([^/]+)", path
                )
                if subtask_match:
                    if set(body) != {"status", "revision"}:
                        raise RequestError(
                            "invalid_body",
                            "subtask update requires only status and revision",
                            400,
                        )
                    task_id = unquote(subtask_match.group(1))
                    self.stack.set_subtask_status(
                        task_id,
                        unquote(subtask_match.group(2)),
                        body["status"],
                        body["revision"],
                    )
                    self.send_json({"data": self.stack.get_task(task_id)})
                    return
                key_result_match = re.fullmatch(
                    r"/api/v1/objectives/([^/]+)/key-results/([^/]+)", path
                )
                if key_result_match:
                    allowed = {"text", "target", "progress", "status", "revision"}
                    mutable = allowed - {"revision"}
                    if (
                        "revision" not in body
                        or not (set(body) & mutable)
                        or not set(body) <= allowed
                        or any(
                            field in body and not isinstance(body[field], str)
                            for field in ("text", "target", "status")
                        )
                        or (
                            "progress" in body
                            and type(body["progress"]) is not int
                        )
                    ):
                        raise RequestError(
                            "invalid_body",
                            "key result update requires revision and at least one supported field",
                            400,
                        )
                    self.send_json({
                        "data": self.stack.patch_key_result_v1(
                            unquote(key_result_match.group(1)),
                            unquote(key_result_match.group(2)),
                            body,
                        )
                    })
                    return
                objective_match = re.fullmatch(r"/api/v1/objectives/([^/]+)", path)
                if objective_match:
                    allowed = {"objective", "quarter", "status", "revision"}
                    mutable = allowed - {"revision"}
                    if (
                        "revision" not in body
                        or not (set(body) & mutable)
                        or not set(body) <= allowed
                        or any(
                            field in body and not isinstance(body[field], str)
                            for field in ("objective", "quarter", "status")
                        )
                    ):
                        raise RequestError(
                            "invalid_body",
                            "objective update requires revision and at least one supported field",
                            400,
                        )
                    self.send_json({
                        "data": self.stack.patch_objective_v1(
                            unquote(objective_match.group(1)), body
                        )
                    })
                    return
                match = re.fullmatch(r"/api/v1/tasks/([^/]+)", path)
                if not match:
                    self.send_api_error("not_found", "API endpoint not found", 404)
                    return
                self.send_json({"data": self.stack.patch_task(unquote(match.group(1)), body)})
                return
            self.read_json()
            self._require_browser_mutation()
            match = re.fullmatch(r"/api/tasks/([^/]+)", path)
            if match:
                self.send_api_error(
                    "legacy_writer_disabled",
                    "Use PATCH /api/v1/tasks/{id}",
                    410,
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except BaseException as error:
            self._dispatch_error(error)


def create_server(
    stack: WorkStack,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    public_port: int | None = None,
) -> WorkStackHTTPServer:
    return WorkStackHTTPServer((host, port), stack, public_port=public_port)


def serve(
    stack: WorkStack,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    public_port: int | None = None,
) -> None:
    server = create_server(stack, host, port, public_port=public_port)
    print("work-stack web: http://{}:{}/".format(host, server.actual_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
