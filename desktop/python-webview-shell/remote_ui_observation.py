"""Bounded observation of the selected loopback served UI entrypoint.

The coordinator host calls ``observe_served_ui`` with the currently selected
loopback base URL and expected workspace UUID. The function GETs
``/api/v1/storage`` before and after GET ``/``, and returns the SHA-256 of the
actual 200 HTML bytes as ``sha256:<64 lowercase hex>``. Anything unproven
returns the token ``unknown``.

This value is the digest of the served root entrypoint asset only. It is not
proof of every bundled file, not a reading of the currently rendered WebView
DOM, and not an inference from product version or source commit. A same-host
server that still serves legacy ``web/index.html`` is reported by those actual
bytes when the response is valid.

The root body must actually begin an HTML document. A JSON or plain-text body
answered under a ``text/html`` header is refused: the declared media type
alone never makes a payload the served UI entrypoint.

``timeout_seconds`` is an absolute budget for the whole observation, not a
per-socket inactivity timeout. One monotonic deadline bounds the before, root
and after requests together, including their connect, header and body phases,
so a drip-fed peer that stays under any single socket timeout still settles to
``unknown`` near the configured bound.

Host selection handoff: the caller must capture and compare the current
profile, session, and generation before and after this call. This module does
not read host selection state. A changed profile, session, or generation is a
host-side unknown even when the HTTP workspace UUID stayed the same.

Unknown never includes exception text, filesystem paths, or HTML. The
implementation uses ``http.client`` directly so loopback observation cannot
inherit process HTTP(S) proxies. It never writes, never sends credentials,
and never fetches an arbitrary URL: only ``/`` and ``/api/v1/storage`` on the
admitted loopback origin.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import threading
import time
import urllib.parse
import uuid
from typing import NamedTuple

UNKNOWN = "unknown"
STORAGE_PATH = "/api/v1/storage"
ROOT_PATH = "/"
MAX_URL_CHARS = 128
MAX_STORAGE_BYTES = 64 * 1024
MAX_HTML_BYTES = 256 * 1024
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 10.0
DEFAULT_TIMEOUT_SECONDS = 2.0
HOST_SELECTION_HANDOFF = (
    "Host must capture profile, session, and generation before and after "
    "observe_served_ui; a change is a host-side unknown, not measured here."
)

_MIN_OPERATION_SECONDS = 0.001
_PROLOGUE_BYTES = 1024
_MAX_PROLOGUE_COMMENTS = 4
_UTF8_BOM = b"\xef\xbb\xbf"
_HTML_SPACE = b" \t\r\n\f"
_HTML_OPENERS = (b"<!doctype html", b"<html")
_OPENER_ENDINGS = b" \t\r\n\f>"

_FAILURES = (
    OSError,
    TimeoutError,
    ValueError,
    TypeError,
    UnicodeError,
    json.JSONDecodeError,
    http.client.HTTPException,
)


class _Refuse(Exception):
    """Internal closed refusal. The public result is always UNKNOWN."""


class _Origin(NamedTuple):
    address: str
    port: int
    host_header: str


class _Deadline:
    """Absolute monotonic bound on the whole before/root/after observation."""

    __slots__ = ("_end",)

    def __init__(self, budget: float) -> None:
        self._end = time.monotonic() + budget

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def budget(self) -> float:
        remaining = self.remaining()
        if remaining <= _MIN_OPERATION_SECONDS:
            raise _Refuse()
        return remaining

    def expired(self) -> bool:
        return self.remaining() <= 0.0


def observe_served_ui(
    base_url: object,
    workspace_id: object,
    *,
    timeout_seconds: object = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Return the served root HTML digest, or ``unknown``.

    ``base_url`` and ``workspace_id`` are supplied by the host from the
    currently selected loopback origin. ``timeout_seconds`` bounds the whole
    observation, not one socket read. This function does not look up product
    version, source commit, WebView DOM, or other bundle files.
    """

    try:
        origin = _admit_origin(base_url)
        expected = _admit_workspace_id(workspace_id)
        budget = _admit_timeout(timeout_seconds)
        return _measure(origin, expected, _Deadline(budget))
    except _Refuse:
        return UNKNOWN
    except _FAILURES:
        return UNKNOWN


def _measure(origin: _Origin, expected: str, deadline: _Deadline) -> str:
    before = _read_workspace(origin, deadline)
    if before != expected:
        raise _Refuse()
    body = _read_html(origin, deadline)
    after = _read_workspace(origin, deadline)
    if after != expected:
        raise _Refuse()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _admit_origin(base_url: object) -> _Origin:
    parsed = _split_http_url(base_url)
    address = _loopback_address(parsed.hostname)
    port = 80 if parsed.port is None else parsed.port
    if type(port) is not int or port < 1 or port > 65535:
        raise _Refuse()
    return _Origin(address, port, _host_header(address, port))


def _split_http_url(base_url: object) -> urllib.parse.SplitResult:
    if type(base_url) is not str or not base_url or len(base_url) > MAX_URL_CHARS:
        raise _Refuse()
    if not base_url.isascii() or not base_url.isprintable() or " " in base_url:
        raise _Refuse()
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http":
        raise _Refuse()
    if parsed.username is not None or parsed.password is not None:
        raise _Refuse()
    if parsed.query or parsed.fragment:
        raise _Refuse()
    if parsed.path not in ("", "/"):
        raise _Refuse()
    return parsed


def _loopback_address(host: object) -> str:
    if type(host) is not str or not host:
        raise _Refuse()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise _Refuse() from None
    if not address.is_loopback:
        raise _Refuse()
    return format(address)


def _host_header(address: str, port: int) -> str:
    if ":" in address:
        return f"[{address}]:{port}"
    return f"{address}:{port}"


def _admit_workspace_id(value: object) -> str:
    if type(value) is not str:
        raise _Refuse()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise _Refuse() from None
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise _Refuse()
    return value


def _admit_timeout(value: object) -> float:
    """Admit the total observation budget without converting unbounded ints.

    int/float comparison is exact in Python, so an arbitrarily large integer
    is refused here instead of raising OverflowError inside ``float()``.
    """

    if isinstance(value, bool) or type(value) not in (int, float):
        raise _Refuse()
    if value != value or value < MIN_TIMEOUT_SECONDS or value > MAX_TIMEOUT_SECONDS:
        raise _Refuse()
    return float(value)


def _read_workspace(origin: _Origin, deadline: _Deadline) -> str:
    payload = _request(origin, STORAGE_PATH, deadline, MAX_STORAGE_BYTES, "application/json")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        # Bounded but deeply nested JSON exhausts the decoder. That is a
        # malformed response, not an exception that may escape this callable.
        raise _Refuse() from None
    if type(parsed) is not dict:
        raise _Refuse()
    data = parsed.get("data")
    if type(data) is not dict:
        raise _Refuse()
    return _admit_workspace_id(data.get("workspace_id"))


def _read_html(origin: _Origin, deadline: _Deadline) -> bytes:
    payload = _request(origin, ROOT_PATH, deadline, MAX_HTML_BYTES, "text/html")
    return _admit_html_document(payload)


def _admit_html_document(payload: bytes) -> bytes:
    """Require bytes that actually open an HTML document.

    Bounded prefix admission only: no parser and no asset framework. JSON or
    plain bytes mislabelled ``text/html`` never become a served UI identity.
    """

    head = payload[:_PROLOGUE_BYTES]
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM) :]
    if not _opens_html(_skip_prologue(head).lower()):
        raise _Refuse()
    return payload


def _skip_prologue(head: bytes) -> bytes:
    head = head.lstrip(_HTML_SPACE)
    for _ in range(_MAX_PROLOGUE_COMMENTS):
        if not head.startswith(b"<!--"):
            return head
        end = head.find(b"-->", 4)
        if end < 0:
            raise _Refuse()
        head = head[end + 3 :].lstrip(_HTML_SPACE)
    return head


def _opens_html(lowered: bytes) -> bool:
    for opener in _HTML_OPENERS:
        if not lowered.startswith(opener):
            continue
        rest = lowered[len(opener) :]
        if rest and rest[0] in _OPENER_ENDINGS:
            return True
    return False


def _request(
    origin: _Origin,
    path: str,
    deadline: _Deadline,
    limit: int,
    expected_media: str,
) -> bytes:
    connection = http.client.HTTPConnection(origin.address, origin.port, timeout=deadline.budget())
    guard = _arm(connection, deadline)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": origin.host_header,
                "Accept": expected_media,
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        _reject_transport(response, expected_media)
        payload = _read_body(response, connection, limit, deadline)
    finally:
        guard.cancel()
        connection.close()
    if deadline.expired():
        raise _Refuse()
    return payload


def _arm(connection: http.client.HTTPConnection, deadline: _Deadline) -> threading.Timer:
    """Sever this connection once the absolute observation deadline passes.

    Socket inactivity timeouts alone cannot bound a drip-fed header or body
    read loop, so the remaining total budget is enforced here as well.
    """

    guard = threading.Timer(max(0.0, deadline.remaining()), _sever, (connection,))
    guard.daemon = True
    guard.start()
    return guard


def _sever(connection: http.client.HTTPConnection) -> None:
    established = connection.sock
    if established is None:
        return
    try:
        established.shutdown(socket.SHUT_RDWR)
    except OSError:
        return


def _retime(connection: http.client.HTTPConnection, seconds: float) -> None:
    established = connection.sock
    if established is not None:
        established.settimeout(seconds)


def _reject_transport(response: http.client.HTTPResponse, expected_media: str) -> None:
    _reject_redirect(response)
    if response.status != 200:
        raise _Refuse()
    encoding = response.getheader("Content-Encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        raise _Refuse()
    if _media_type(response) != expected_media:
        raise _Refuse()


def _reject_redirect(response: http.client.HTTPResponse) -> None:
    if response.getheader("Location") is not None:
        raise _Refuse()
    if 300 <= response.status < 400:
        raise _Refuse()


def _media_type(response: http.client.HTTPResponse) -> str:
    raw = response.getheader("Content-Type")
    if type(raw) is not str or not raw:
        raise _Refuse()
    return raw.split(";", 1)[0].strip().lower()


def _declared_length(response: http.client.HTTPResponse) -> int | None:
    raw = response.getheader("Content-Length")
    if raw is None:
        return None
    try:
        length = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _Refuse() from None
    if length < 1:
        raise _Refuse()
    return length


def _read_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    limit: int,
    deadline: _Deadline,
) -> bytes:
    declared = _declared_length(response)
    if declared is not None and declared > limit:
        raise _Refuse()
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        _retime(connection, deadline.budget())
        piece = _read_chunk(response, limit + 1 - total)
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    payload = b"".join(chunks)
    if not payload or len(payload) > limit:
        raise _Refuse()
    if declared is not None and declared != len(payload):
        raise _Refuse()
    return payload


def _read_chunk(response: http.client.HTTPResponse, size: int) -> bytes:
    try:
        return response.read1(size)
    except http.client.IncompleteRead:
        raise _Refuse() from None


__all__ = (
    "DEFAULT_TIMEOUT_SECONDS",
    "HOST_SELECTION_HANDOFF",
    "MAX_HTML_BYTES",
    "MAX_STORAGE_BYTES",
    "ROOT_PATH",
    "STORAGE_PATH",
    "UNKNOWN",
    "observe_served_ui",
)
