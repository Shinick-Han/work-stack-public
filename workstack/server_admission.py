"""Admission checks every request passes before a handler is reached.

Order is the contract here: Host is proven loopback first, then a mutation
proves same-origin and CSRF, then the body is read under its route limit and
only then is an idempotency key demanded. Nothing in this module reads or
writes the Store; it decides whether a request is allowed to become one.
"""

from __future__ import annotations

import json
import re
import secrets
import select
from typing import Any
from urllib.parse import urlsplit

from .capture import canonical_digest
from .server_errors import RequestError


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CAPTURE_BODY_LIMIT = 64 * 1024
DEFAULT_BODY_LIMIT = 1024 * 1024


# The exact attributed client header and its only accepted value.
AGENT_CLIENT_HEADER = "X-WorkStack-Client"
AGENT_CLIENT_VALUE = "agent-cli-v1"

_IDEMPOTENCY_KEY_GRAMMAR = re.compile(r"[A-Za-z0-9._:-]{8,128}")


def _split_host_header(host_header: str) -> tuple[str, int | None]:
    if any(char in host_header for char in "\r\n/@"):
        raise RequestError("invalid_host", "Host header is invalid", 400)
    try:
        parsed = urlsplit("//" + host_header)
        return (parsed.hostname or "").casefold(), parsed.port
    except ValueError as error:
        raise RequestError("invalid_host", "Host header is invalid", 400) from error


def _same_origin(parsed: Any, origin_port: int | None, hostname: str, port: int) -> bool:
    return (
        parsed.scheme == "http"
        and (parsed.hostname or "").casefold() == hostname
        and origin_port == port
        and parsed.username is None
        and parsed.password is None
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


class RequestAdmissionMixin:
    """Header, origin, authorization and body admission for the handler."""

    def _agent_client_origin(self) -> str | None:
        """The attributed Agent CLI origin, or None when the header is absent.

        A missing header is ordinary and yields no origin and no Agent notice.
        Anything present must be exactly one occurrence of the exact frozen
        value: `_header_once` alone would accept a padded or unknown value, so
        the value is compared exactly rather than trimmed or folded.
        """

        values = self.headers.get_all(AGENT_CLIENT_HEADER, [])
        if not values:
            return None
        if len(values) != 1 or values[0] != AGENT_CLIENT_VALUE:
            raise RequestError(
                "invalid_header",
                "{} must occur once with its exact value".format(AGENT_CLIENT_HEADER),
                400,
            )
        return AGENT_CLIENT_VALUE

    def _header_once(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise RequestError("invalid_header", "{} must occur once".format(name), 400)
        return values[0] if values else None

    def _host_parts(self) -> tuple[str, int]:
        host_header = self._header_once("Host")
        if not host_header:
            raise RequestError("invalid_host", "Host header is required", 400)
        hostname, port = _split_host_header(host_header)
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
        if not _same_origin(parsed, origin_port, hostname, port):
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

    def _discard_rejected_body(self, declared: int) -> None:
        """Steal kernel-buffered rejected bytes; never wait on a lying length."""

        self.close_connection = True
        leftover = min(declared, DEFAULT_BODY_LIMIT)
        sock = self.connection
        while leftover > 0:
            ready, _w, _x = select.select([sock], [], [], 0)
            if not ready:
                break
            chunk = sock.recv(min(leftover, 65536))
            if not chunk:
                break
            leftover -= len(chunk)

    def _declared_body_length(self, maximum: int) -> int:
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
            self._discard_rejected_body(length)
            raise RequestError(
                "body_too_large", "request body exceeds {} bytes".format(maximum), 413
            )
        return length

    def read_json(self, maximum: int = DEFAULT_BODY_LIMIT) -> tuple[dict[str, Any], str]:
        self._require_json_content_type()
        length = self._declared_body_length(maximum)
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
        if not _IDEMPOTENCY_KEY_GRAMMAR.fullmatch(value):
            raise RequestError("invalid_idempotency_key", "Idempotency-Key is invalid", 400)
        return value

    def _refuse_unsupported_idempotency_key(self, message: str) -> None:
        """CLI write routes accept no key; the refusal precedes the service."""

        if self.headers.get_all("Idempotency-Key") is not None:
            raise RequestError("unsupported_idempotency_key", message, 400)


__all__ = (
    "AGENT_CLIENT_HEADER",
    "AGENT_CLIENT_VALUE",
    "CAPTURE_BODY_LIMIT",
    "DEFAULT_BODY_LIMIT",
    "LOOPBACK_HOSTS",
    "RequestAdmissionMixin",
)
