"""Loopback-only HTTP server for the Work Stack application."""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from .capture import CaptureValidationError, canonical_digest
from .store import StoreExternalChangeError
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


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class WorkStackHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], stack: WorkStack) -> None:
        host, port = address
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                "non-loopback binding is disabled; use an authenticated reverse proxy"
            )
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
        if hostname not in LOOPBACK_HOSTS or port != self.server.actual_port:
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
        elif isinstance(error, DomainError):
            self.send_api_error(error.code, str(error), 400, error.details)
        elif isinstance(error, (ValueError, json.JSONDecodeError)):
            self.send_api_error("invalid_request", str(error), 400)
        else:
            raise error

    def do_GET(self) -> None:
        try:
            self._validate_host()
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/state":
                self.send_json(self.stack.snapshot())
                return
            if path == "/api/v1/session":
                self.send_json({"data": {"csrf_token": self.server.csrf_token}})
                return
            if path == "/api/v1/health":
                self.send_json({
                    "data": {
                        "api_version": "v1",
                        "status": "ready",
                    }
                })
                return
            if path == "/api/v1/sync/status":
                if parsed.query:
                    raise RequestError("invalid_query", "sync status query is invalid", 400)
                self.send_json({"data": self.stack.store.sync_status()})
                return
            if path == "/api/v1/sync/events":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                    raise RequestError("invalid_query", "sync event query is invalid", 400)
                try:
                    after = int(query.get("after", ["0"])[0])
                    result = self.stack.store.sync_events(after)
                except ValueError as error:
                    raise RequestError("invalid_query", str(error), 400) from error
                self.send_json({"data": result})
                return
            if path == "/api/v1/events":
                if parsed.query:
                    raise RequestError("invalid_query", "event stream query is invalid", 400)
                raw_cursor = self._header_once("Last-Event-ID") or "0"
                try:
                    after = int(raw_cursor)
                    if after < 0:
                        raise ValueError
                except ValueError as error:
                    raise RequestError("invalid_header", "Last-Event-ID is invalid", 400) from error
                self.send_sync_event(after)
                return
            if path == "/api/v1/storage":
                self.send_json({"data": self.stack.storage_status()})
                return
            if path == "/api/v1/workspace":
                self.send_json({"data": self.stack.workspace_projection()})
                return
            if path == "/api/v1/search":
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
                return
            if path == "/api/v1/review":
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
                    raise RequestError("invalid_query", "review days must be between 1 and 31", 400)
                self.send_json({"data": self.stack.review_projection(query["date"][0], days)})
                return
            if path == "/api/v1/work-sessions":
                if parsed.query:
                    raise RequestError("invalid_query", "work session query is invalid", 400)
                self.send_json({"data": self.stack.work_sessions_projection()})
                return
            objective_match = re.fullmatch(r"/api/v1/objectives/([^/]+)", path)
            if objective_match:
                self.send_json(
                    {"data": self.stack.objective_detail(unquote(objective_match.group(1)))}
                )
                return
            snapshot_match = re.fullmatch(r"/api/v1/tasks/([^/]+)/snapshot", path)
            if snapshot_match:
                artifact = self.stack.planning_snapshot(
                    unquote(snapshot_match.group(1))
                )
                self.send_json({
                    "data": {
                        "snapshot": artifact.snapshot,
                        "digest": artifact.digest,
                        "filename": artifact.filename,
                        "omissions": list(artifact.omissions),
                    }
                })
                return
            task_match = re.fullmatch(r"/api/v1/tasks/([^/]+)", path)
            if task_match:
                self.send_json({"data": self.stack.task_detail(unquote(task_match.group(1)))})
                return
            if path == "/api/v1/captures":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"status"} or len(query.get("status", ["inbox"])) != 1:
                    raise RequestError("invalid_query", "capture query is invalid", 400)
                status = query.get("status", ["inbox"])[0]
                self.send_json({"data": self.stack.list_captures(status)})
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._validate_host()
            if path.startswith("/api/v1/"):
                is_ingest = path == "/api/v1/captures"
                is_task_create = path == "/api/v1/tasks"
                is_sync_adopt = path == "/api/v1/sync/adopt"
                snapshot_export_match = re.fullmatch(
                    r"/api/v1/tasks/([^/]+)/snapshot/export", path
                )
                receipt_match = re.fullmatch(r"/api/v1/replies/([^/]+)/receipt", path)
                is_reply_mutation = path == "/api/v1/replies" or receipt_match is not None
                work_session_action_match = re.fullmatch(
                    r"/api/v1/work-sessions/([^/]+)/(pause|resume|stop|worklog)", path
                )
                is_work_session_mutation = (
                    path == "/api/v1/work-sessions" or work_session_action_match is not None
                )
                is_idempotent_creation = (
                    is_task_create
                    or path
                    in {
                        "/api/v1/objectives",
                        "/api/v1/notes",
                        "/api/v1/review/checkin",
                        "/api/v1/review/entries",
                    }
                    or re.fullmatch(r"/api/v1/tasks/[^/]+/(?:notes|subtasks)", path)
                    is not None
                    or re.fullmatch(r"/api/v1/objectives/[^/]+/key-results", path)
                    is not None
                    or is_work_session_mutation
                )
                maximum = CAPTURE_BODY_LIMIT if path.startswith("/api/v1/captures") else DEFAULT_BODY_LIMIT
                # Consume the bounded JSON body before returning an authorization
                # error.  Closing a Windows socket with unread request bytes can
                # reset the connection before the client receives the JSON error.
                body, request_digest = self.read_json(maximum)
                if not (is_ingest and self._has_agent_bearer()):
                    self._require_browser_mutation()
                idempotency_key = (
                    self._idempotency_key()
                    if path.startswith("/api/v1/captures")
                    or is_reply_mutation
                    or is_idempotent_creation
                    else ""
                )
                if is_sync_adopt:
                    if set(body) != {"expected_generation", "expected_manifest_digest"}:
                        raise RequestError(
                            "invalid_body",
                            "sync adoption requires expected_generation and expected_manifest_digest",
                            400,
                        )
                    result = self.stack.store.adopt_external_change(
                        body["expected_generation"], body["expected_manifest_digest"]
                    )
                    self.send_json({"data": result})
                    return
                if is_task_create:
                    result = self.stack.create_task_v1(body, idempotency_key, path=path)
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/work-sessions":
                    if set(body) != {"task_id"} or not isinstance(body["task_id"], str):
                        raise RequestError(
                            "invalid_body",
                            "work session creation requires only task_id",
                            400,
                        )
                    result = self.stack.start_work_session_v1(
                        body, idempotency_key, path=path
                    )
                    self.send_json(result["body"], result["status"])
                    return
                if work_session_action_match:
                    session_id = unquote(work_session_action_match.group(1))
                    action = work_session_action_match.group(2)
                    if action == "worklog":
                        if (
                            set(body) != {"done", "next", "blockers"}
                            or any(
                                not isinstance(body[field], list)
                                or any(not isinstance(item, str) for item in body[field])
                                for field in ("done", "next", "blockers")
                            )
                        ):
                            raise RequestError(
                                "invalid_body",
                                "work session worklog requires string arrays for done, next, and blockers",
                                400,
                            )
                        result = self.stack.record_work_session_v1(
                            session_id, body, idempotency_key, path=path
                        )
                    else:
                        if body:
                            raise RequestError(
                                "invalid_body",
                                "work session transitions require an empty body",
                                400,
                            )
                        result = self.stack.transition_work_session_v1(
                            session_id, action, body, idempotency_key, path=path
                        )
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/maintenance/backup":
                    if set(body) != {"confirmed"} or body["confirmed"] is not True:
                        raise RequestError(
                            "invalid_body",
                            "backup download requires explicit confirmation",
                            400,
                        )
                    download = self.stack.create_backup_download()
                    self.send_backup(
                        download.body,
                        download.filename,
                        download.digest,
                        download.workspace_id,
                    )
                    return
                if snapshot_export_match:
                    if set(body) != {
                        "disclosure_confirmed",
                        "expected_revision",
                        "expected_digest",
                    }:
                        raise RequestError(
                            "invalid_body",
                            "snapshot export confirmation has unknown or missing fields",
                            400,
                        )
                    artifact = self.stack.confirmed_snapshot_export(
                        unquote(snapshot_export_match.group(1)),
                        body["expected_revision"],
                        body["expected_digest"],
                        body["disclosure_confirmed"],
                    )
                    self.send_snapshot(
                        artifact.canonical_bytes, artifact.filename, artifact.digest
                    )
                    return
                task_note_match = re.fullmatch(r"/api/v1/tasks/([^/]+)/notes", path)
                if task_note_match:
                    if set(body) != {"text", "revision"}:
                        raise RequestError(
                            "invalid_body",
                            "task note requires only text and revision",
                            400,
                        )
                    task_id = unquote(task_note_match.group(1))
                    result = self.stack.add_task_note_v1(
                        task_id,
                        body,
                        idempotency_key,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                task_subtask_match = re.fullmatch(r"/api/v1/tasks/([^/]+)/subtasks", path)
                if task_subtask_match:
                    if set(body) != {"title", "priority", "revision"}:
                        raise RequestError(
                            "invalid_body",
                            "subtask creation requires only title, priority, and revision",
                            400,
                        )
                    task_id = unquote(task_subtask_match.group(1))
                    result = self.stack.add_subtask_v1(
                        task_id,
                        body,
                        idempotency_key,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/objectives":
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
                    result = self.stack.create_objective_v1(
                        body, idempotency_key, path=path
                    )
                    self.send_json(result["body"], result["status"])
                    return
                key_result_match = re.fullmatch(
                    r"/api/v1/objectives/([^/]+)/key-results", path
                )
                if key_result_match:
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
                    result = self.stack.add_key_result_v1(
                        unquote(key_result_match.group(1)),
                        body,
                        idempotency_key,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/notes":
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
                    result = self.stack.create_note_v1(body, idempotency_key, path=path)
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/review/checkin":
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
                    result = self.stack.checkin_v1(body, idempotency_key, path=path)
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/review/entries":
                    if (
                        set(body) != {"date", "task_id", "done", "next", "blockers"}
                        or not isinstance(body["date"], str)
                        or not isinstance(body["task_id"], str)
                        or any(
                            not isinstance(body[field], list)
                            or any(not isinstance(item, str) for item in body[field])
                            for field in ("done", "next", "blockers")
                        )
                    ):
                        raise RequestError(
                            "invalid_body",
                            "review entry requires date, task_id, and string arrays for done, next, and blockers",
                            400,
                        )
                    result = self.stack.add_worklog_v1(body, idempotency_key, path=path)
                    self.send_json(result["body"], result["status"])
                    return
                if is_ingest:
                    result = self.stack.ingest_capture(
                        body, idempotency_key, request_digest, path=path
                    )
                    self.send_json(result["body"], result["status"])
                    return
                link_match = re.fullmatch(r"/api/v1/captures/([^/]+)/link", path)
                if link_match:
                    if set(body) != {"task_id"} or not isinstance(body["task_id"], str):
                        raise RequestError("invalid_body", "link requires only task_id", 400)
                    result = self.stack.link_capture(
                        unquote(link_match.group(1)),
                        body["task_id"],
                        idempotency_key,
                        request_digest,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                action_match = re.fullmatch(
                    r"/api/v1/captures/([^/]+)/actions/([^/]+)/task", path
                )
                if action_match:
                    if set(body) - {"objective_ids"} or not isinstance(body.get("objective_ids", []), list):
                        raise RequestError("invalid_body", "conversion accepts only objective_ids", 400)
                    result = self.stack.convert_capture_action(
                        unquote(action_match.group(1)),
                        unquote(action_match.group(2)),
                        body.get("objective_ids", []),
                        idempotency_key,
                        request_digest,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                task_match = re.fullmatch(r"/api/v1/captures/([^/]+)/task", path)
                if task_match:
                    result = self.stack.create_task_from_capture(
                        unquote(task_match.group(1)),
                        body,
                        idempotency_key,
                        request_digest,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                dismiss_match = re.fullmatch(r"/api/v1/captures/([^/]+)/dismiss", path)
                if dismiss_match:
                    if body:
                        raise RequestError("invalid_body", "dismiss requires an empty object", 400)
                    result = self.stack.dismiss_capture(
                        unquote(dismiss_match.group(1)),
                        idempotency_key,
                        request_digest,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                if path == "/api/v1/replies":
                    result = self.stack.approve_reply(
                        body,
                        idempotency_key,
                        request_digest,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                if receipt_match:
                    result = self.stack.apply_reply_receipt(
                        unquote(receipt_match.group(1)),
                        body,
                        idempotency_key,
                        request_digest,
                        path=path,
                    )
                    self.send_json(result["body"], result["status"])
                    return
                self.send_api_error("not_found", "API endpoint not found", 404)
                return

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


def create_server(stack: WorkStack, host: str = "127.0.0.1", port: int = 8765) -> WorkStackHTTPServer:
    return WorkStackHTTPServer((host, port), stack)


def serve(stack: WorkStack, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(stack, host, port)
    print("work-stack web: http://{}:{}/".format(host, server.actual_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
