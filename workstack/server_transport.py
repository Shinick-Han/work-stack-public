"""Response emission and static asset delivery for the loopback handler.

Every byte the handler writes leaves through `_send_bytes`, so the constant
security headers, the request-id echo and the broken-pipe tolerance are stated
once. Static delivery keeps its own header set because it is not an API
envelope and must not advertise a request id.
"""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .sse_events import SseEncodingError, encode_sync_stream


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_WEB_ROOT = PROJECT_ROOT / "web"
FRONTEND_ROOT = PROJECT_ROOT / "frontend" / "dist"

_TEXTUAL_CONTENT_TYPES = frozenset({"application/javascript", "application/json"})


def _static_content_type(name: str) -> str:
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in _TEXTUAL_CONTENT_TYPES:
        content_type += "; charset=utf-8"
    return content_type


class ResponseTransportMixin:
    """The single writer for API envelopes, downloads and static assets."""

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-WorkStack-Request-Id", self.request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A closed browser tab is an expected transport event, not a product error.
            return

    def send_json(self, value: object, status: int = HTTPStatus.OK) -> None:
        self._send_bytes(
            status,
            "application/json; charset=utf-8",
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def send_snapshot(self, body: bytes, filename: str, digest: str) -> None:
        self._send_bytes(HTTPStatus.OK, "application/json; charset=utf-8", body, (
            ("Content-Disposition", 'attachment; filename="{}"'.format(filename)),
            ("X-WorkStack-Snapshot-Digest", digest),
        ))

    def send_backup(
        self, body: bytes, filename: str, digest: str, workspace_id: str
    ) -> None:
        self._send_bytes(HTTPStatus.OK, "application/zip", body, (
            ("Content-Disposition", 'attachment; filename="{}"'.format(filename)),
            ("X-WorkStack-Backup-Digest", digest),
            ("X-WorkStack-Workspace-Id", workspace_id),
        ))

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
        try:
            # One frame per retained id, in order, instead of collapsing the
            # batch onto latest_event_id. The whole batch is encoded before any
            # header is written, so a bad record can never produce a partial
            # body that is then abandoned mid-stream.
            body = encode_sync_stream(payload, after)
        except SseEncodingError:
            # The batch came from this process, not from the request, so this is
            # an internal fault rather than a client cursor error. The reason is
            # deliberately not echoed: it would carry payload detail.
            self.send_api_error(
                "internal_error",
                "sync event stream could not be encoded",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_bytes(HTTPStatus.OK, "text/event-stream; charset=utf-8", body)

    def _static_candidate(self, request_path: str) -> Path | None:
        """The file a static request resolves to, or None when it escapes."""

        if FRONTEND_ROOT.is_dir() and (FRONTEND_ROOT / "index.html").is_file():
            root = FRONTEND_ROOT.resolve()
            relative = unquote(request_path).lstrip("/") or "index.html"
            candidate = (root / relative).resolve()
            if not candidate.is_relative_to(root):
                return None
            if not candidate.is_file():
                candidate = root / "index.html"
            return candidate
        if request_path not in ("/", "/index.html"):
            return None
        return LEGACY_WEB_ROOT / "index.html"

    def _serve_static(self, request_path: str) -> None:
        candidate = self._static_candidate(request_path)
        if candidate is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = candidate.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = _static_content_type(candidate.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


__all__ = (
    "FRONTEND_ROOT",
    "LEGACY_WEB_ROOT",
    "PROJECT_ROOT",
    "ResponseTransportMixin",
)
