"""Behavioural tests for the Adapter OpenDocuments chat transport.

The module under test is loaded by explicit file path. Tests do not import
Work Stack core, do not install dependencies, and use fixture secrets only.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import os
import socket
import ssl
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / "integrations" / "opendocuments" / "od_client.py"
HAPPY_CHAT_PATH = (
    ROOT / "contracts" / "fixtures" / "opendocuments-adapter" / "input" / "happy-chat.json"
)

SPEC = importlib.util.spec_from_file_location("opendocuments_od_client", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
OD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OD
SPEC.loader.exec_module(OD)

FIXTURE_KEY = "fixture-od-api-key-0001"
WRONG_KEY = "fixture-od-api-key-0002"
AMBIENT_KEY = "ambient-od-api-key-MUST-NOT-BE-SENT"
PROXY_SECRET = "proxy-user:proxy-pass-MUST-NOT-FORWARD"
WORKSPACE_ID = "ws_opendocuments_fixture_1"
QUERY = "Where is the release checklist?"
HAPPY_CHAT = json.loads(HAPPY_CHAT_PATH.read_text(encoding="utf-8"))
HAPPY_BYTES = json.dumps(HAPPY_CHAT, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
PASSWORD_CANARY = "should-be-dropped"
DEPTH_CANARY = "nested-depth-canary"


def _nested_arrays(count: int) -> bytes:
    return b"[" * count + b"0" + b"]" * count


def _nested_objects(count: int) -> bytes:
    return b'{"k":' * count + b"0" + b"}" * count


def _chat_with_extra(extra: bytes) -> bytes:
    return (
        b'{"queryId":"22222222-2222-4222-8222-222222222222","answer":"x","sources":[],'
        b'"canary":"' + DEPTH_CANARY.encode("ascii") + b'","extra":' + extra + b"}"
    )


def _config(origin: str, **overrides: object) -> OD.TrustedBackendConfig:
    values = {
        "origin": origin,
        "api_key": FIXTURE_KEY,
        "workspace_id": WORKSPACE_ID,
        "profile": OD.CORPUS_ONLY_PROFILE,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return OD.TrustedBackendConfig(**values)


class _FakeServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__((host, port), handler)
        self.host = host
        self.received: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self.mode = "ok"
        self.expected_key = FIXTURE_KEY
        self.response_body = HAPPY_BYTES
        self.redirect_to = ""
        self.drip_seconds = 1.5


def _record(server: _FakeServer, handler: BaseHTTPRequestHandler) -> bytes:
    length_header = handler.headers.get("Content-Length") or "0"
    try:
        length = int(length_header)
    except ValueError:
        length = 0
    body = handler.rfile.read(length) if length > 0 else b""
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    item = {
        "method": handler.command,
        "path": handler.path,
        "key": handler.headers.get("X-API-Key"),
        "content_type": handler.headers.get("Content-Type"),
        "cookie": handler.headers.get("Cookie"),
        "body": parsed,
        "raw": body,
    }
    with server.lock:
        server.received.append(item)
    return body


def _handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            _record(self.server, self)  # type: ignore[arg-type]
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_POST(self) -> None:
            server: _FakeServer = self.server  # type: ignore[assignment]
            _record(server, self)
            mode = server.mode
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", server.redirect_to)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if mode == "auth":
                if self.headers.get("X-API-Key") != server.expected_key:
                    payload = b'{"error":"Invalid or expired API key","secret":"' + FIXTURE_KEY.encode("ascii") + b'"}'
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            if mode == "oversize":
                payload = b"x" * (OD.MAX_RESPONSE_BYTES + 1)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                return
            if mode == "drip":
                payload = HAPPY_BYTES
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                time.sleep(server.drip_seconds)
                try:
                    self.wfile.write(payload)
                except OSError:
                    return
                return
            if mode == "malformed":
                payload = b"{not-json"
            elif mode == "nonfinite":
                payload = (
                    b'{"queryId":"22222222-2222-4222-8222-222222222222","answer":"x",'
                    b'"sources":[],"confidence":{"score":NaN,"level":"none","reason":"x"},'
                    b'"route":"rag","profile":"fast"}'
                )
            elif mode == "duplicate":
                payload = (
                    b'{"queryId":"22222222-2222-4222-8222-222222222222","queryId":"other",'
                    b'"answer":"x","sources":[],"route":"rag","profile":"fast"}'
                )
            elif mode == "deep":
                nested = "{" * (OD.MAX_JSON_DEPTH + 2) + "}" * (OD.MAX_JSON_DEPTH + 2)
                payload = (
                    b'{"queryId":"22222222-2222-4222-8222-222222222222","answer":"x","sources":[],'
                    + b'"extra":' + nested.encode("ascii") + b"}"
                )
            elif mode == "too_many_sources":
                sources = [{"documentId": "x"}] * (OD.MAX_SOURCES + 1)
                payload = json.dumps(
                    {
                        "queryId": "22222222-2222-4222-8222-222222222222",
                        "answer": "x",
                        "sources": sources,
                    },
                    separators=(",", ":"),
                ).encode("ascii")
            else:
                payload = server.response_body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@contextlib.contextmanager
def fake_http_server(host: str = "127.0.0.1", mode: str = "ok", **state: object):
    server = _FakeServer(host, 0, _handler())
    server.mode = mode
    for name, value in state.items():
        setattr(server, name, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _origin_for(server: _FakeServer) -> str:
    host = server.host
    if ":" in host:
        return f"http://[{host}]:{server.server_address[1]}"
    return f"http://{host}:{server.server_address[1]}"


@contextlib.contextmanager
def trickle_http_server(interval: float, body: bytes):
    """Loopback peer that emits a finite HTTP response one byte at a time."""

    message = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = int(server_sock.getsockname()[1])
    stop = threading.Event()
    received = {"n": 0}

    def _serve() -> None:
        server_sock.settimeout(5.0)
        try:
            conn, _unused = server_sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5.0)
            try:
                buf = b""
                while b"\r\n\r\n" not in buf and len(buf) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                header_end = buf.find(b"\r\n\r\n")
                rest = buf[header_end + 4 :] if header_end >= 0 else b""
                length = 0
                header_blob = buf[:header_end] if header_end >= 0 else buf
                for line in header_blob.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        try:
                            length = int(line.split(b":", 1)[1])
                        except ValueError:
                            length = 0
                while len(rest) < length:
                    chunk = conn.recv(min(4096, length - len(rest)))
                    if not chunk:
                        break
                    rest += chunk
                received["n"] += 1
            except OSError:
                return
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            for index in range(len(message)):
                if stop.is_set():
                    return
                try:
                    conn.sendall(message[index : index + 1])
                except OSError:
                    return
                time.sleep(interval)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield {"port": port, "message": message, "received": received}
    finally:
        stop.set()
        try:
            server_sock.close()
        except OSError:
            pass
        thread.join(timeout=8)


class OpenDocumentsClientTests(unittest.TestCase):
    def _assert_closed_error(self, result: dict[str, object], code: str, *secrets: str) -> None:
        self.assertEqual(set(result), {"ok", "error"})
        self.assertFalse(result["ok"])
        error = result["error"]
        assert isinstance(error, dict)
        self.assertEqual(set(error), {"code", "message"})
        self.assertEqual(error["code"], code)
        self.assertIn(code, OD.ERROR_CODES)
        self.assertIsInstance(error["message"], str)
        haystack = repr(result) + str(result) + error["message"]
        for secret in secrets:
            if secret:
                self.assertNotIn(secret, haystack)

    def test_public_signature_is_query_plus_trusted_config_only(self) -> None:
        signature = inspect.signature(OD.post_opendocuments_chat)
        self.assertEqual(list(signature.parameters), ["query", "config"])

    def test_positive_loopback_posts_chat_once_and_hands_payload_to_trusted_caller(self) -> None:
        with fake_http_server() as server:
            config = _config(_origin_for(server))
            with mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": f"http://{PROXY_SECRET}@10.0.0.1:8080",
                    "HTTPS_PROXY": f"http://{PROXY_SECRET}@10.0.0.1:8080",
                    "ALL_PROXY": f"http://{PROXY_SECRET}@10.0.0.1:8080",
                    "OD_API_KEY": AMBIENT_KEY,
                    "OPENDOCUMENTS_API_KEY": AMBIENT_KEY,
                },
                clear=False,
            ):
                result = OD.post_opendocuments_chat(QUERY, config)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["outcome"], "received")
            payload = result["response"].take_for_mapper()
            self.assertEqual(payload["answer"], HAPPY_CHAT["answer"])
            self.assertEqual(payload["sources"], HAPPY_CHAT["sources"])
            self.assertIn(PASSWORD_CANARY, json.dumps(payload))
            with self.assertRaises(ValueError):
                result["response"].take_for_mapper()
            with self.assertRaises(TypeError):
                json.dumps(result)
            received = server.received
            self.assertEqual(len(received), 1)
            item = received[0]
            self.assertEqual(item["method"], "POST")
            self.assertEqual(item["path"], "/api/v1/chat")
            self.assertEqual(item["key"], FIXTURE_KEY)
            self.assertIsNone(item["cookie"])
            self.assertEqual(item["body"], {"query": QUERY, "profile": "fast", "workspaceId": WORKSPACE_ID})
            leak = repr(result) + str(result) + repr(config) + str(config)
            for secret in (FIXTURE_KEY, AMBIENT_KEY, PROXY_SECRET, QUERY, PASSWORD_CANARY, HAPPY_CHAT["answer"]):
                self.assertNotIn(secret, leak)

    def test_auth_refusal_does_not_surface_http_body_or_key(self) -> None:
        with fake_http_server(mode="auth", expected_key=FIXTURE_KEY) as server:
            config = _config(_origin_for(server), api_key=WRONG_KEY)
            result = OD.post_opendocuments_chat(QUERY, config)
        self._assert_closed_error(
            result,
            "auth_refused",
            FIXTURE_KEY,
            WRONG_KEY,
            QUERY,
            "/api/v1/chat",
            "Invalid or expired API key",
        )
        self.assertEqual(len(server.received), 1)

    def test_redirect_does_not_forward_the_api_key(self) -> None:
        with fake_http_server() as target:
            target_origin = _origin_for(target)
            with fake_http_server(mode="redirect", redirect_to=f"{target_origin}/api/v1/chat") as source:
                config = _config(_origin_for(source))
                result = OD.post_opendocuments_chat(QUERY, config)
            self._assert_closed_error(result, "redirect_refused", FIXTURE_KEY, QUERY, "/api/v1/chat", "Location")
            self.assertEqual(len(source.received), 1)
            self.assertEqual(source.received[0]["path"], "/api/v1/chat")
            self.assertEqual(source.received[0]["key"], FIXTURE_KEY)
            self.assertEqual(target.received, [])

    def test_oversized_response_is_refused(self) -> None:
        with fake_http_server(mode="oversize") as server:
            result = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(result, "response_too_large", FIXTURE_KEY, QUERY, "x" * 40)

    def test_slow_drip_uses_overall_deadline(self) -> None:
        body = b'{"answer":"x","sources":[]}'
        interval = 0.05
        timeout_seconds = 0.25
        with trickle_http_server(interval, body) as peer:
            message = peer["message"]
            stream_seconds = len(message) * interval
            self.assertLess(interval, timeout_seconds)
            self.assertGreater(stream_seconds, timeout_seconds * 3)
            config = _config(f"http://127.0.0.1:{peer['port']}", timeout_seconds=timeout_seconds)
            started = time.monotonic()
            result = OD.post_opendocuments_chat(QUERY, config)
            elapsed = time.monotonic() - started
            self.assertEqual(peer["received"]["n"], 1)
        self._assert_closed_error(result, "outcome_unknown", FIXTURE_KEY, QUERY, "answer")
        self.assertLess(elapsed, timeout_seconds + 0.65)
        self.assertLess(elapsed, stream_seconds / 2)

    def test_malformed_json_is_refused_without_raw_body(self) -> None:
        with fake_http_server(mode="malformed") as server:
            result = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(result, "malformed_response", FIXTURE_KEY, QUERY, "not-json")

    def test_nonfinite_numbers_are_refused(self) -> None:
        with fake_http_server(mode="nonfinite") as server:
            result = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(result, "malformed_response", FIXTURE_KEY, "NaN")

    def test_duplicate_keys_are_refused(self) -> None:
        with fake_http_server(mode="duplicate") as server:
            result = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(result, "malformed_response", FIXTURE_KEY, "other")

    def test_json_depth_and_source_count_are_bounded(self) -> None:
        over_arrays = _chat_with_extra(_nested_arrays(OD.MAX_JSON_DEPTH - 1))
        with fake_http_server(response_body=over_arrays) as server:
            deep = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(deep, "malformed_response", FIXTURE_KEY, DEPTH_CANARY, QUERY)

        over_objects = _chat_with_extra(_nested_objects(OD.MAX_JSON_DEPTH - 1))
        with fake_http_server(response_body=over_objects) as server:
            deep_obj = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(deep_obj, "malformed_response", FIXTURE_KEY, DEPTH_CANARY)

        deep_1100 = _chat_with_extra(_nested_arrays(1100))
        self.assertLess(len(deep_1100), OD.MAX_RESPONSE_BYTES)
        with fake_http_server(response_body=deep_1100) as server:
            too_deep = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(too_deep, "malformed_response", FIXTURE_KEY, DEPTH_CANARY)

        with fake_http_server(mode="too_many_sources") as server:
            many = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self._assert_closed_error(many, "malformed_response", FIXTURE_KEY)

    def test_json_at_depth_bound_is_accepted(self) -> None:
        at_bound = _chat_with_extra(_nested_arrays(OD.MAX_JSON_DEPTH - 2))
        with fake_http_server(response_body=at_bound) as server:
            result = OD.post_opendocuments_chat(QUERY, _config(_origin_for(server)))
        self.assertTrue(result["ok"], result)
        payload = result["response"].take_for_mapper()
        self.assertEqual(payload["answer"], "x")
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["canary"], DEPTH_CANARY)

    def test_non_loopback_http_is_refused_without_network(self) -> None:
        with mock.patch.object(OD.http.client, "HTTPConnection") as ctor:
            result = OD.post_opendocuments_chat(QUERY, _config("http://example.test"))
        ctor.assert_not_called()
        self._assert_closed_error(result, "origin_refused", FIXTURE_KEY, QUERY, "example.test")

    def test_localhost_and_userinfo_query_fragment_http_are_refused(self) -> None:
        cases = (
            "http://localhost:9",
            "http://127.0.0.2:9",
            "http://user:s3cret-userinfo@127.0.0.1:9",
            "http://127.0.0.1:9/?q=1",
            "http://127.0.0.1:9/#frag",
            "http://127.0.0.1:9/api/v1/chat",
        )
        for origin in cases:
            with self.subTest(origin=origin):
                with mock.patch.object(OD.http.client, "HTTPConnection") as ctor:
                    result = OD.post_opendocuments_chat(QUERY, _config(origin))
                ctor.assert_not_called()
                self._assert_closed_error(
                    result, "origin_refused", FIXTURE_KEY, QUERY, "s3cret-userinfo", "/api/v1/chat"
                )

    def test_http_ipv6_loopback_literal_is_allowed(self) -> None:
        with mock.patch.object(OD.http.client, "HTTPConnection", side_effect=ConnectionRefusedError) as ctor:
            result = OD.post_opendocuments_chat(QUERY, _config("http://[::1]:65535"))
        ctor.assert_called()
        self.assertEqual(ctor.call_args[0][0], "::1")
        self._assert_closed_error(result, "origin_unreachable", FIXTURE_KEY, QUERY)

    def test_partial_post_send_broken_pipe_is_outcome_unknown(self) -> None:
        constructed: list[object] = []

        class PartialSendConnection:
            def __init__(self, host: str, port: int, timeout: object = None) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                self.sock = None
                self.request_calls = 0
                constructed.append(self)

            def connect(self) -> None:
                self.sock = mock.Mock()

            def request(self, method: str, url: str, body: object = None, headers: object = None) -> None:
                self.request_calls += 1
                raise BrokenPipeError(32, "Broken pipe")

            def getresponse(self) -> None:
                raise AssertionError("getresponse must not run after a failed request")

            def close(self) -> None:
                return None

        with mock.patch.object(OD.http.client, "HTTPConnection", PartialSendConnection):
            result = OD.post_opendocuments_chat(QUERY, _config("http://127.0.0.1:9"))
        self.assertEqual(len(constructed), 1)
        conn = constructed[0]
        assert isinstance(conn, PartialSendConnection)
        self.assertEqual(conn.request_calls, 1)
        self._assert_closed_error(result, "outcome_unknown", FIXTURE_KEY, QUERY, "/api/v1/chat", "Broken pipe")

    def test_pre_connect_refusal_is_origin_unreachable(self) -> None:
        constructed: list[object] = []

        class RefusedConnection:
            def __init__(self, host: str, port: int, timeout: object = None) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                self.sock = None
                self.request_calls = 0
                constructed.append(self)

            def connect(self) -> None:
                raise ConnectionRefusedError("refused before send")

            def request(self, method: str, url: str, body: object = None, headers: object = None) -> None:
                self.request_calls += 1

            def close(self) -> None:
                return None

        with mock.patch.object(OD.http.client, "HTTPConnection", RefusedConnection):
            result = OD.post_opendocuments_chat(QUERY, _config("http://127.0.0.1:9"))
        self.assertEqual(len(constructed), 1)
        conn = constructed[0]
        assert isinstance(conn, RefusedConnection)
        self.assertEqual(conn.request_calls, 0)
        self._assert_closed_error(result, "origin_unreachable", FIXTURE_KEY, QUERY, "refused before send")

    def test_https_uses_certificate_verification(self) -> None:
        contexts: list[ssl.SSLContext] = []
        real = ssl.create_default_context

        def _wrap(*args: object, **kwargs: object) -> ssl.SSLContext:
            context = real(*args, **kwargs)
            contexts.append(context)
            return context

        with mock.patch("ssl.create_default_context", _wrap), mock.patch.object(
            OD.http.client, "HTTPSConnection", side_effect=ConnectionRefusedError
        ) as ctor:
            result = OD.post_opendocuments_chat(
                QUERY, _config("https://opendocuments.example.test")
            )
        ctor.assert_called()
        kwargs = ctor.call_args.kwargs
        self.assertIn("context", kwargs)
        self.assertTrue(contexts)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)
        self._assert_closed_error(result, "origin_unreachable", FIXTURE_KEY, QUERY)

    def test_invalid_query_is_refused_locally(self) -> None:
        origin = "http://127.0.0.1:9"
        with mock.patch.object(OD.http.client, "HTTPConnection") as ctor:
            for query in ("", " " + QUERY, QUERY + " ", "\n" + QUERY, "a" * (OD.MAX_QUERY_CHARS + 1), "a\x00b"):
                with self.subTest(query=repr(query)):
                    result = OD.post_opendocuments_chat(query, _config(origin))
                    self._assert_closed_error(result, "invalid_query", FIXTURE_KEY, QUERY, query.strip()[:20])
        ctor.assert_not_called()

    def test_request_overrides_on_config_are_refused(self) -> None:
        origin = "http://127.0.0.1:9"
        with mock.patch.object(OD.http.client, "HTTPConnection") as ctor:
            extra = {
                "origin": origin,
                "api_key": FIXTURE_KEY,
                "workspace_id": WORKSPACE_ID,
                "profile": "fast",
                "timeout_seconds": 5.0,
                "conversation_id": "convo-1",
                "collection_id": "col-1",
                "url": "http://127.0.0.1:9/other",
                "route": "/api/v1/documents",
            }
            result = OD.post_opendocuments_chat(QUERY, extra)
        ctor.assert_not_called()
        self._assert_closed_error(result, "invalid_config", FIXTURE_KEY, QUERY, "convo-1", "/api/v1/documents")

    def test_non_corpus_profile_is_refused(self) -> None:
        origin = "http://127.0.0.1:9"
        with mock.patch.object(OD.http.client, "HTTPConnection") as ctor:
            for profile in ("balanced", "precise", "custom"):
                with self.subTest(profile=profile):
                    result = OD.post_opendocuments_chat(QUERY, _config(origin, profile=profile))
                    self._assert_closed_error(result, "invalid_config", FIXTURE_KEY, profile)
        ctor.assert_not_called()

    def test_config_repr_redacts_api_key(self) -> None:
        config = _config("https://opendocuments.example.test")
        text = repr(config) + str(config)
        self.assertIn("<redacted>", text)
        self.assertNotIn(FIXTURE_KEY, text)
        self.assertIn("opendocuments.example.test", text)


if __name__ == "__main__":
    unittest.main()
