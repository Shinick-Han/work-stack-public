"""Deadline-bounded HTTP exchange and JSON close-shape for chat transport.

Internal helper for ``od_client.py``. Not a public Adapter API. Imports only
the sibling config helper; it does not import ``od_client``.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any


def _load_by_path(stem: str) -> Any:
    module_name = f"opendocuments_{stem}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


try:
    from . import od_client_config as _config
except ImportError:
    _config = _load_by_path("od_client_config")

CHAT_PATH = _config.CHAT_PATH
MAX_RESPONSE_BYTES = _config.MAX_RESPONSE_BYTES
MAX_SOURCES = _config.MAX_SOURCES
MAX_JSON_DEPTH = _config.MAX_JSON_DEPTH
_error = _config._error

_READ_CHUNK = 16_384
_DEADLINE_SLICE_SECONDS = 0.05


class _DeadlineExceeded(Exception):
    """Overall response deadline elapsed."""


class _ResponseTooLarge(Exception):
    """Upstream body exceeded the transport cap."""


_TRANSPORT_FAILURES = (
    _DeadlineExceeded,
    _ResponseTooLarge,
    TimeoutError,
    ssl.SSLError,
    ConnectionRefusedError,
    ConnectionAbortedError,
    BrokenPipeError,
    OSError,
    http.client.IncompleteRead,
    http.client.HTTPException,
)


class _DeadlineSocket:
    """Connected socket whose recv returns to Python at bounded intervals.

    ``http.client`` header and body reads both makefile this object, so a
    peer that emits another byte before each inactivity timeout cannot keep
    one kernel wait alive past the overall deadline. Slice timeouts retry
    the same recv; they are not POST retries.
    """

    __slots__ = ("_sock", "_deadline")

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        return self._timed_recv(self._sock.recv, *args, **kwargs)

    def recv_into(self, *args: Any, **kwargs: Any) -> int:
        return self._timed_recv(self._sock.recv_into, *args, **kwargs)

    def _timed_recv(self, op: Any, *args: Any, **kwargs: Any) -> Any:
        while True:
            left = _remaining(self._deadline)
            self._sock.settimeout(min(left, _DEADLINE_SLICE_SECONDS))
            try:
                return op(*args, **kwargs)
            except TimeoutError:
                continue

    def makefile(
        self,
        mode: str = "r",
        buffering: int | None = None,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> io.BufferedReader | socket.SocketIO:
        if "b" not in mode:
            raise OSError("deadline wrapper requires binary makefile")
        raw = socket.SocketIO(self, "rb")
        inner = self._sock
        io_refs = getattr(inner, "_io_refs", None)
        if isinstance(io_refs, int):
            inner._io_refs = io_refs + 1
        if buffering == 0:
            return raw
        size = io.DEFAULT_BUFFER_SIZE if buffering is None or buffering < 0 else buffering
        return io.BufferedReader(raw, size)

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def gettimeout(self) -> float | None:
        return self._sock.gettimeout()

    def close(self) -> None:
        self._sock.close()

    def fileno(self) -> int:
        return self._sock.fileno()

    def shutdown(self, how: int) -> None:
        self._sock.shutdown(how)

    def _decref_socketios(self) -> None:
        decref = getattr(self._sock, "_decref_socketios", None)
        if decref is not None:
            decref()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


class EphemeralChatResponse:
    """Parsed upstream JSON for a trusted in-process mapper only.

    ``repr`` / ``str`` never include the payload. ``take_for_mapper``
    detaches the object once. This is not a promise that Python memory
    can be securely erased, and the payload must not be printed or
    persisted by this transport.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def take_for_mapper(self) -> dict[str, Any]:
        payload = self._payload
        self._payload = None
        if payload is None:
            raise ValueError("ephemeral response already taken")
        return payload

    def __repr__(self) -> str:
        return "EphemeralChatResponse(redacted)"

    __str__ = __repr__


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _DeadlineExceeded()
    return left


def _set_timeout(connection: http.client.HTTPConnection, deadline: float) -> None:
    left = _remaining(deadline)
    connection.timeout = left
    sock = getattr(connection, "sock", None)
    if sock is not None:
        sock.settimeout(left)


def _is_timeout_oserror(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    text = str(exc).lower()
    return "timed out" in text


def _close_quiet(connection: http.client.HTTPConnection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except OSError:
        return


def _open_connection(
    scheme: str, host: str, port: int, remaining: float
) -> http.client.HTTPConnection:
    if scheme == "https":
        context = ssl.create_default_context()
        return http.client.HTTPSConnection(host, port, timeout=remaining, context=context)
    return http.client.HTTPConnection(host, port, timeout=remaining)


def _install_deadline_socket(connection: http.client.HTTPConnection, deadline: float) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None:
        connection.sock = _DeadlineSocket(sock, deadline)


def _declared_too_large(content_length: str | None) -> bool:
    if content_length is None:
        return False
    try:
        declared = int(content_length)
    except (TypeError, ValueError):
        return False
    return declared > MAX_RESPONSE_BYTES


def _read_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    deadline: float,
) -> bytes:
    buf = bytearray()
    while True:
        _remaining(deadline)
        _set_timeout(connection, deadline)
        take = min(_READ_CHUNK, MAX_RESPONSE_BYTES - len(buf) + 1)
        reader = getattr(response, "read1", None)
        piece = reader(take) if reader is not None else response.read(take)
        if not piece:
            break
        buf.extend(piece)
        if len(buf) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge()
    return bytes(buf)


def _discard_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    deadline: float,
) -> None:
    try:
        _read_body(response, connection, deadline)
    except (_ResponseTooLarge, _DeadlineExceeded, OSError, http.client.HTTPException):
        return


def _consume_response(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    deadline: float,
) -> tuple[int, bytes] | dict[str, Any]:
    status = int(response.status)
    if 300 <= status <= 399:
        _discard_body(response, connection, deadline)
        return _error("redirect_refused", "redirects are refused")
    if _declared_too_large(response.getheader("Content-Length")):
        _discard_body(response, connection, deadline)
        return _error("response_too_large", "upstream response exceeded the transport cap")
    raw = _read_body(response, connection, deadline)
    return (status, raw)


def _classify_transport_failure(exc: BaseException, connected: bool) -> dict[str, Any]:
    if isinstance(exc, _DeadlineExceeded):
        return _error("outcome_unknown", "the overall response deadline elapsed")
    if isinstance(exc, _ResponseTooLarge):
        return _error("response_too_large", "upstream response exceeded the transport cap")
    if isinstance(exc, http.client.IncompleteRead):
        return _error("outcome_unknown", "the upstream connection closed before a complete response")
    if isinstance(exc, TimeoutError):
        return _error("outcome_unknown", "the overall response deadline elapsed")
    if isinstance(exc, ssl.SSLError):
        if connected:
            return _error("outcome_unknown", "the upstream connection was lost")
        return _error("origin_unreachable", "the origin could not be reached")
    if isinstance(exc, OSError):
        if _is_timeout_oserror(exc):
            return _error("outcome_unknown", "the overall response deadline elapsed")
        if connected:
            return _error("outcome_unknown", "the upstream connection was lost")
        return _error("origin_unreachable", "the origin could not be reached")
    if isinstance(exc, http.client.HTTPException):
        if connected:
            return _error("outcome_unknown", "the transport failed")
        return _error("origin_unreachable", "the transport failed")
    raise exc


def _exchange_chat(
    scheme: str,
    host: str,
    port: int,
    deadline: float,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, bytes] | dict[str, Any]:
    connection: http.client.HTTPConnection | None = None
    connected = False
    try:
        remaining = _remaining(deadline)
        connection = _open_connection(scheme, host, port, remaining)
        connection.connect()
        connected = True
        _install_deadline_socket(connection, deadline)
        _set_timeout(connection, deadline)
        connection.request("POST", CHAT_PATH, body=body, headers=headers)
        _set_timeout(connection, deadline)
        response = connection.getresponse()
        return _consume_response(response, connection, deadline)
    except _TRANSPORT_FAILURES as exc:
        return _classify_transport_failure(exc, connected)
    finally:
        _close_quiet(connection)


def _reject_nonfinite(token: str) -> None:
    raise ValueError("nonfinite JSON number")


def _parse_finite_float(token: str) -> float:
    value = float(token)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("nonfinite JSON number")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate object key")
        seen.add(key)
        result[key] = value
    return result


def _json_depth(value: object) -> int:
    deepest = 1
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, current = stack.pop()
        if current > deepest:
            deepest = current
        children: list[object] | None
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            children = None
        if not children:
            continue
        nested = current + 1
        if nested > deepest:
            deepest = nested
        if nested > MAX_JSON_DEPTH:
            return nested
        for item in children:
            stack.append((item, nested))
    return deepest


def _parse_chat_payload(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _error("malformed_response", "upstream JSON is not usable")
    try:
        payload = json.loads(
            text,
            strict=True,
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except RecursionError:
        return _error("malformed_response", "upstream JSON is not usable")
    except (json.JSONDecodeError, ValueError):
        return _error("malformed_response", "upstream JSON is not usable")
    if not isinstance(payload, dict):
        return _error("malformed_response", "upstream JSON is not usable")
    try:
        depth = _json_depth(payload)
    except RecursionError:
        return _error("malformed_response", "upstream JSON is not usable")
    if depth > MAX_JSON_DEPTH:
        return _error("malformed_response", "upstream JSON is not usable")
    if "sources" in payload:
        sources = payload["sources"]
        if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
            return _error("malformed_response", "upstream JSON is not usable")
    return {
        "ok": True,
        "outcome": "received",
        "response": EphemeralChatResponse(payload),
    }


def _result_from_status(status: int, raw: bytes) -> dict[str, Any]:
    if status == 401:
        return _error("auth_refused", "upstream refused authentication")
    if status != 200:
        return _error("upstream_refused", "upstream refused the request")
    return _parse_chat_payload(raw)
