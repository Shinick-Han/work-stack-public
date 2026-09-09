"""Real loopback HTTP cases for bounded served-UI identity observation.

These tests start disposable local HTTP servers. They recompute SHA-256 of the
bytes the server actually wrote. They do not substitute product version or
source commit, and they do not mock the digest grammar in place of a fetch.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_ui_observation as OBS  # noqa: E402

WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_WORKSPACE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LEGACY_HTML = b"<!doctype html><html><body>legacy overview</body></html>\n"
CURRENT_HTML = b"<!doctype html><html><body>current dist shell</body></html>\n"
MISLABELLED_BODIES = (
    b'{"served":false,"workspace_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}',
    b"served=false\n",
    b"\x00\x01\x02\x03binary",
    b"<script>alert(1)</script>",
    b"not html at all",
    b"<!-- only a comment -->\n",
)
HTML_DOCUMENTS = (
    b"<!DOCTYPE HTML>\n<html><body>uppercase doctype</body></html>\n",
    b"\xef\xbb\xbf<!doctype html>\n<html><body>bom</body></html>\n",
    b'<!-- build 1.0.13 -->\n<html lang="en"><body>commented</body></html>\n',
    b"<html>\n<body>no doctype legacy</body>\n</html>\n",
    b'<!doctype html PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html></html>\n',
)
PATH_CANARY = r"C:\secret\workstack\index.html"
EXCEPTION_CANARY = "ValueError: exploded while hashing"
HTML_CANARY = "<script>alert(1)</script>"
DIGEST_RE = r"\Asha256:[0-9a-f]{64}\Z"
MAX_FUNCTION_LINES = 100
MAX_FILE_LINES = 800
MAX_CCN = 15
MODULE_PATH = SHELL / "remote_ui_observation.py"


class ObservationServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.lock = threading.Lock()
        self.hits: list[tuple[str, str]] = []
        self.html = CURRENT_HTML
        self.html_type = "text/html; charset=utf-8"
        self.workspace_id = WORKSPACE
        self.changed_workspace_id = ""
        self.product_version = "9.9.9"
        self.storage_status = 200
        self.root_status = 200
        self.storage_type = "application/json; charset=utf-8"
        self.storage_body: bytes | None = None
        self.root_location = ""
        self.storage_location = ""
        self.declared_root_length: int | None = None
        self.truncate_root = False
        self.drop_after_headers = False
        self.storage_delay_seconds = 0.0
        self.root_delay_seconds = 0.0
        self.root_body_drip_seconds = 0.0
        self.root_header_drip_seconds = 0.0

    def handle_error(self, request: object, client_address: object) -> None:
        """A client severed by its own deadline is expected, not a failure."""

        failure = sys.exc_info()[0]
        if failure is not None and issubclass(failure, OSError):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


def handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self._record()
            self.send_error(405)

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            path = self.path.split("?", 1)[0]
            server: ObservationServer = self.server  # type: ignore[assignment]
            if path == OBS.STORAGE_PATH:
                self._storage(server)
                return
            if path == OBS.ROOT_PATH:
                self._root(server)
                return
            self.send_error(404)

        def _record(self) -> None:
            server: ObservationServer = self.server  # type: ignore[assignment]
            with server.lock:
                server.hits.append((self.command, self.path.split("?", 1)[0]))

        def _storage(self, server: ObservationServer) -> None:
            if server.storage_delay_seconds:
                time.sleep(server.storage_delay_seconds)
            if server.storage_location:
                self._redirect(server.storage_status, server.storage_location)
                return
            workspace = server.workspace_id
            with server.lock:
                storage_reads = sum(1 for method, path in server.hits if path == OBS.STORAGE_PATH)
            if storage_reads > 1 and server.changed_workspace_id:
                workspace = server.changed_workspace_id
            if server.storage_body is not None:
                body = server.storage_body
            else:
                body = json.dumps(
                    {
                        "data": {
                            "workspace_id": workspace,
                            "product_version": server.product_version,
                            "source_commit": "68bb581ff4a083a3630da01547785d02b4882301",
                        }
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            self._write(server.storage_status, server.storage_type, body)

        def _root(self, server: ObservationServer) -> None:
            if server.root_delay_seconds:
                time.sleep(server.root_delay_seconds)
            if server.root_location:
                self._redirect(server.root_status, server.root_location)
                return
            body = server.html
            declared = len(body) if server.declared_root_length is None else server.declared_root_length
            if server.root_header_drip_seconds or server.root_body_drip_seconds:
                self._drip_root(server, body, declared)
                return
            self.send_response(server.root_status)
            self.send_header("Content-Type", server.html_type)
            self.send_header("Content-Length", str(declared))
            self.send_header("Connection", "close")
            self.end_headers()
            if server.drop_after_headers:
                return
            if server.truncate_root:
                self.wfile.write(body[: max(0, len(body) // 2)])
                return
            self.wfile.write(body)

        def _drip_root(self, server: ObservationServer, body: bytes, declared: int) -> None:
            """Answer byte by byte, each gap far below the socket timeout.

            The status line and headers are written raw so a slow header can
            be exercised as well as a slow body.
            """

            self.close_connection = True
            head = (
                f"HTTP/1.1 {server.root_status} OK\r\n"
                f"Content-Type: {server.html_type}\r\n"
                f"Content-Length: {declared}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            if not self._drip(head, server.root_header_drip_seconds):
                return
            self._drip(body, server.root_body_drip_seconds)

        def _drip(self, blob: bytes, delay: float) -> bool:
            try:
                for index in range(len(blob)):
                    self.wfile.write(blob[index : index + 1])
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
            except OSError:
                return False
            return True

        def _redirect(self, status: int, location: str) -> None:
            code = status if 300 <= status < 400 else 302
            self.send_response(code)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def _write(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    return Handler


@contextlib.contextmanager
def running_server() -> Iterator[ObservationServer]:
    server = ObservationServer(handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def base_url(server: ObservationServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/"


def digest_for(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def observe(server: ObservationServer, workspace: str = WORKSPACE) -> str:
    return OBS.observe_served_ui(base_url(server), workspace, timeout_seconds=1.5)


def timed_observe(server: ObservationServer, budget: float) -> tuple[str, float]:
    started = time.monotonic()
    result = OBS.observe_served_ui(base_url(server), WORKSPACE, timeout_seconds=budget)
    return result, time.monotonic() - started


def complexity(function: ast.AST) -> int:
    score = 1
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(0, len(node.values) - 1)
        elif isinstance(node, ast.Try):
            score += len(node.handlers) + int(bool(node.orelse))
        elif isinstance(node, ast.Match):
            score += max(0, len(node.cases) - 1)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(generator.ifs) for generator in node.generators)
    return score


class ServedUiIdentityTests(unittest.TestCase):
    def test_root_html_bytes_digest_matches_sha256(self) -> None:
        with running_server() as server:
            result = observe(server)
            self.assertEqual(result, digest_for(CURRENT_HTML))
            self.assertRegex(result, DIGEST_RE)
            self.assertEqual(
                server.hits,
                [
                    ("GET", OBS.STORAGE_PATH),
                    ("GET", OBS.ROOT_PATH),
                    ("GET", OBS.STORAGE_PATH),
                ],
            )

    def test_modified_served_bytes_change_the_digest(self) -> None:
        with running_server() as server:
            first = observe(server)
            server.html = CURRENT_HTML + b"<!--changed-->"
            second = observe(server)
        self.assertEqual(first, digest_for(CURRENT_HTML))
        self.assertEqual(second, digest_for(CURRENT_HTML + b"<!--changed-->"))
        self.assertNotEqual(first, second)

    def test_legacy_index_bytes_are_reported_not_inferred(self) -> None:
        with running_server() as server:
            server.html = LEGACY_HTML
            server.product_version = "1.0.12"
            result = observe(server)
        self.assertEqual(result, digest_for(LEGACY_HTML))
        self.assertNotIn("1.0.12", result)
        self.assertNotIn("68bb581", result)

    def test_product_version_does_not_become_identity(self) -> None:
        with running_server() as server:
            server.product_version = "1.0.13"
            result = observe(server)
        self.assertEqual(result, digest_for(CURRENT_HTML))
        self.assertNotEqual(result, "1.0.13")

    def test_wrong_workspace_uuid_is_unknown_and_skips_root(self) -> None:
        with running_server() as server:
            server.workspace_id = OTHER_WORKSPACE
            result = observe(server, WORKSPACE)
            self.assertEqual(result, OBS.UNKNOWN)
            self.assertEqual(server.hits, [("GET", OBS.STORAGE_PATH)])

    def test_mid_read_changed_uuid_is_unknown(self) -> None:
        with running_server() as server:
            server.changed_workspace_id = OTHER_WORKSPACE
            result = observe(server)
            self.assertEqual(result, OBS.UNKNOWN)
            self.assertEqual(
                [path for _method, path in server.hits],
                [OBS.STORAGE_PATH, OBS.ROOT_PATH, OBS.STORAGE_PATH],
            )

    def test_external_redirect_is_not_followed(self) -> None:
        with running_server() as target, running_server() as source:
            destination = base_url(target) + "stolen"
            source.root_status = 302
            source.root_location = destination
            result = observe(source)
            self.assertEqual(result, OBS.UNKNOWN)
            self.assertEqual(target.hits, [])
            self.assertNotIn(("GET", "/stolen"), source.hits)

    def test_userinfo_url_does_not_connect(self) -> None:
        with running_server() as server:
            url = f"http://user:secret@127.0.0.1:{server.server_address[1]}/"
            result = OBS.observe_served_ui(url, WORKSPACE, timeout_seconds=1.5)
            self.assertEqual(result, OBS.UNKNOWN)
            self.assertEqual(server.hits, [])

    def test_non_loopback_and_malformed_urls_are_unknown(self) -> None:
        with running_server() as server:
            port = server.server_address[1]
            cases = (
                f"http://192.0.2.1:{port}/",
                f"http://localhost:{port}/",
                f"https://127.0.0.1:{port}/",
                f"file://127.0.0.1:{port}/",
                f"http://127.0.0.1:{port}/index.html",
                f"http://127.0.0.1:{port}/?next=1",
                "not-a-url",
                "",
                None,
                1,
            )
            for url in cases:
                with self.subTest(url=url):
                    self.assertEqual(
                        OBS.observe_served_ui(url, WORKSPACE, timeout_seconds=1.5),
                        OBS.UNKNOWN,
                    )
            self.assertEqual(server.hits, [])

    def test_non_html_non_200_and_empty_root_are_unknown(self) -> None:
        with running_server() as server:
            server.html_type = "application/json"
            self.assertEqual(observe(server), OBS.UNKNOWN)
            server.html_type = "text/html; charset=utf-8"
            server.root_status = 500
            server.html = f"<html>{EXCEPTION_CANARY}{PATH_CANARY}</html>".encode("utf-8")
            self.assertEqual(observe(server), OBS.UNKNOWN)
            server.root_status = 200
            server.html = b""
            server.declared_root_length = 0
            self.assertEqual(observe(server), OBS.UNKNOWN)

    def test_too_large_root_is_unknown_without_leaking_html(self) -> None:
        payload = b"<html>" + HTML_CANARY.encode("ascii") + b"</html>"
        with running_server() as server:
            server.html = payload
            server.declared_root_length = OBS.MAX_HTML_BYTES + 1
            result = observe(server)
        self.assertEqual(result, OBS.UNKNOWN)
        self.assertNotIn(HTML_CANARY, result)

    def test_truncated_root_is_unknown(self) -> None:
        with running_server() as server:
            server.truncate_root = True
            server.declared_root_length = len(CURRENT_HTML) + 80
            self.assertEqual(observe(server), OBS.UNKNOWN)

    def test_malformed_storage_is_unknown(self) -> None:
        with running_server() as server:
            server.storage_body = b"{not-json " + PATH_CANARY.encode("ascii")
            result = observe(server)
            self.assertEqual(result, OBS.UNKNOWN)
            self.assertNotIn(PATH_CANARY, result)
            self.assertEqual(server.hits, [("GET", OBS.STORAGE_PATH)])

    def test_unknown_does_not_expose_exception_path_or_html(self) -> None:
        with running_server() as server:
            server.root_status = 500
            server.html_type = "text/html"
            server.html = (
                f"<html>{HTML_CANARY}{PATH_CANARY}{EXCEPTION_CANARY}</html>"
            ).encode("utf-8")
            result = observe(server)
        self.assertEqual(result, OBS.UNKNOWN)
        self.assertNotIn(HTML_CANARY, result)
        self.assertNotIn(PATH_CANARY, result)
        self.assertNotIn(EXCEPTION_CANARY, result)

    def test_http_proxy_env_is_not_used_for_loopback(self) -> None:
        with running_server() as proxy, running_server() as server:
            proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
            env = {
                "HTTP_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "https_proxy": proxy_url,
                "ALL_PROXY": proxy_url,
                "all_proxy": proxy_url,
            }
            with mock.patch.dict(os.environ, env, clear=False):
                result = observe(server)
            self.assertEqual(result, digest_for(CURRENT_HTML))
            self.assertEqual(proxy.hits, [])

    def test_failed_connection_is_unknown(self) -> None:
        with running_server() as server:
            port = server.server_address[1]
        result = OBS.observe_served_ui(
            f"http://127.0.0.1:{port}/", WORKSPACE, timeout_seconds=0.5
        )
        self.assertEqual(result, OBS.UNKNOWN)

    def test_json_or_plain_bytes_under_html_header_are_unknown(self) -> None:
        with running_server() as server:
            server.html_type = "text/html; charset=utf-8"
            for body in (*MISLABELLED_BODIES, b"<html", b"<!doctype html"):
                with self.subTest(body=body[:24]):
                    server.html = body
                    result = observe(server)
                    self.assertEqual(result, OBS.UNKNOWN)
                    self.assertNotEqual(result, digest_for(body))

    def test_html_document_variants_are_admitted_by_actual_bytes(self) -> None:
        with running_server() as server:
            server.html_type = "text/html; charset=utf-8"
            for body in HTML_DOCUMENTS:
                with self.subTest(body=body[:24]):
                    server.html = body
                    self.assertEqual(observe(server), digest_for(body))

    def test_absolute_deadline_bounds_all_three_requests(self) -> None:
        budget = 0.5
        delay = 0.4
        self.assertLess(delay, budget)  # no single request exceeds socket inactivity
        self.assertGreater(3 * delay, budget)  # but their sum exceeds the total budget
        with running_server() as server:
            server.storage_delay_seconds = delay
            server.root_delay_seconds = delay
            result, elapsed = timed_observe(server, budget)
        self.assertEqual(result, OBS.UNKNOWN)
        self.assertLess(elapsed, 1.0)

    def test_slow_drip_body_cannot_outlast_the_absolute_deadline(self) -> None:
        budget = 0.5
        gap = 0.04
        self.assertLess(gap, budget)  # every inter-byte gap stays under the timeout
        self.assertGreater(len(CURRENT_HTML) * gap, budget)  # total exceeds the budget
        with running_server() as server:
            server.root_body_drip_seconds = gap
            result, elapsed = timed_observe(server, budget)
        self.assertEqual(result, OBS.UNKNOWN)
        self.assertNotEqual(result, digest_for(CURRENT_HTML))
        self.assertLess(elapsed, 1.5)

    def test_slow_drip_headers_cannot_outlast_the_absolute_deadline(self) -> None:
        budget = 0.5
        gap = 0.02
        self.assertLess(gap, budget)
        with running_server() as server:
            server.root_header_drip_seconds = gap
            result, elapsed = timed_observe(server, budget)
        self.assertEqual(result, OBS.UNKNOWN)
        self.assertLess(elapsed, 1.5)

    def test_out_of_range_timeout_values_are_unknown(self) -> None:
        cases = (
            ("huge_int", 10**10000),
            ("huge_negative_int", -(10**10000)),
            ("infinity", float("inf")),
            ("nan", float("nan")),
            ("zero", 0),
            ("below_minimum", 0.01),
            ("above_maximum", OBS.MAX_HTML_BYTES),
            ("bool", True),
            ("string", "1.5"),
            ("none", None),
        )
        with running_server() as server:
            for label, value in cases:
                with self.subTest(case=label):
                    self.assertEqual(
                        OBS.observe_served_ui(base_url(server), WORKSPACE, timeout_seconds=value),
                        OBS.UNKNOWN,
                    )
            self.assertEqual(server.hits, [])

    def test_deeply_nested_storage_json_is_unknown(self) -> None:
        depth = 10000
        body = (b"[" * depth) + b"0" + (b"]" * depth)
        self.assertLess(len(body), OBS.MAX_STORAGE_BYTES)
        with running_server() as server:
            server.storage_body = body
            result = observe(server)
            self.assertEqual(result, OBS.UNKNOWN)
            self.assertEqual(server.hits, [("GET", OBS.STORAGE_PATH)])

    def test_host_handoff_is_stated_and_api_stays_small(self) -> None:
        self.assertIn("profile", OBS.HOST_SELECTION_HANDOFF)
        self.assertIn("session", OBS.HOST_SELECTION_HANDOFF)
        self.assertIn("generation", OBS.HOST_SELECTION_HANDOFF)
        self.assertEqual(OBS.UNKNOWN, "unknown")
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("urllib.request", source)
        self.assertNotIn("ProxyHandler", source)
        self.assertNotIn("import webview", source)
        self.assertNotIn("pywebview", source)
        self.assertIn("currently rendered WebView", source)
        self.assertIn("proof of every bundled file", source)
        self.assertIn("absolute budget for the whole observation", source)
        self.assertIn("must actually begin an HTML document", source)
        self.assertNotIn("except BaseException", source)

    def test_structural_limits(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), MAX_FILE_LINES)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            ccn = complexity(node)
            with self.subTest(function=node.name):
                self.assertLessEqual(length, MAX_FUNCTION_LINES)
                self.assertLessEqual(ccn, MAX_CCN)


if __name__ == "__main__":
    unittest.main()
