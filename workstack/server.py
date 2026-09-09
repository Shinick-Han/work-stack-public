"""Loopback-only HTTP server for the Work Stack application.

This module is the composition point, not the implementation: it owns the
socket server, the request handler's identity and the two process entry points.
The request surface itself lives in cohesive collaborators that the handler
mixes in — admission, transport, error mapping and the GET/POST/PATCH route
tables — so each verb entry point here reads as the ordering contract it is.
Route declarations and the previously module-level names stay importable from
here for the consumers and tests that already read them from this module.
"""

from __future__ import annotations

import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from urllib.parse import urlparse

from .service import WorkStack
from .mutation_service import MutationNoticeHttpMixin
from .report_documents_http import ReportDocumentsHttpMixin
from .reporting_http import DailyReportPreviewHttpMixin; from .weekly_reporting_http import WeeklyReportPreviewHttpMixin
from .capture_observation_http import CaptureObservationHttpMixin
from .cli_read_http import CLI_GET_ROUTES, CliReadHttpMixin
from .knowledge_attempt_guard import KnowledgeAttemptGuard
from .knowledge_captures_http import KnowledgeCapturesHttpMixin
from .knowledge_execution_http import KnowledgeExecutionHttpMixin
from .knowledge_execution_runtime import KnowledgeDriverBinding, admit_drivers
from .knowledge_requests_http import KnowledgeRequestsHttpMixin
from .knowledge_verification_guard import KnowledgeVerificationGuard
from .knowledge_verification_http import KnowledgeVerificationHttpMixin
# Route declarations live in .http_route_types; they stay importable from this
# module for the consumers and tests that already read them from here.
from .http_route_types import GetRoute, IDEMPOTENT_POST_ROUTES, PostRoute, V1_GET_ROUTES, V1_POST_ROUTES, _get_route, _post_route
from .server_admission import (
    AGENT_CLIENT_HEADER,
    AGENT_CLIENT_VALUE,
    CAPTURE_BODY_LIMIT,
    DEFAULT_BODY_LIMIT,
    LOOPBACK_HOSTS,
    RequestAdmissionMixin,
)
from .server_errors import ErrorDispatchMixin, RequestError
from .server_patch_routes import PatchRouteMixin
from .server_post_routes import PostRouteMixin
from .server_read_routes import ReadRouteMixin
from .server_transport import (
    FRONTEND_ROOT,
    LEGACY_WEB_ROOT,
    PROJECT_ROOT,
    ResponseTransportMixin,
)


class WorkStackHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        stack: WorkStack,
        *,
        public_port: int | None = None,
        knowledge_drivers: Mapping[str, KnowledgeDriverBinding] | None = None,
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
        # The operator's trusted execution configuration, copied and admitted
        # here -- before the store lease is taken and before the socket exists
        # -- so an invalid binding refuses to start a server rather than
        # becoming a refusal some later request discovers. The default is the
        # empty registry, which is the explicit "no driver configured" state.
        self.knowledge_drivers = admit_drivers(knowledge_drivers)
        # One attempt guard per owner incarnation. Instances share nothing, so
        # a request this server did not itself issue -- including every request
        # issued before a restart -- is ineligible for automatic execution.
        self.knowledge_attempt_guard = KnowledgeAttemptGuard()
        # One verification gate per owner incarnation, separate from the
        # attempt guard above: a search attempt is spent once and never given
        # back, while a source check is a read-only observation the user may
        # repeat, so the two share no registry, bound or vocabulary.
        self.knowledge_verification_guard = KnowledgeVerificationGuard()
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


class Handler(
    CliReadHttpMixin,
    KnowledgeCapturesHttpMixin,
    KnowledgeExecutionHttpMixin,
    # Listed before the released verification mixin it extends: the saved
    # source-observation routes take the same single verification gate through
    # the same accessor, and override nothing on the released verify route.
    CaptureObservationHttpMixin,
    KnowledgeVerificationHttpMixin,
    KnowledgeRequestsHttpMixin,
    DailyReportPreviewHttpMixin,
    WeeklyReportPreviewHttpMixin,
    ReportDocumentsHttpMixin,
    MutationNoticeHttpMixin,
    ReadRouteMixin,
    PostRouteMixin,
    PatchRouteMixin,
    RequestAdmissionMixin,
    ResponseTransportMixin,
    ErrorDispatchMixin,
    BaseHTTPRequestHandler,
):
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
                self._handle_v1_patch(path)
                return
            self._handle_legacy_patch(path)
        except BaseException as error:
            self._dispatch_error(error)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            self._validate_host()
            self._handle_delete(path)
        except BaseException as error:
            self._dispatch_error(error)


def create_server(
    stack: WorkStack,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    public_port: int | None = None,
    knowledge_drivers: Mapping[str, KnowledgeDriverBinding] | None = None,
) -> WorkStackHTTPServer:
    return WorkStackHTTPServer(
        (host, port),
        stack,
        public_port=public_port,
        knowledge_drivers=knowledge_drivers,
    )


def serve(
    stack: WorkStack,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    public_port: int | None = None,
    knowledge_drivers: Mapping[str, KnowledgeDriverBinding] | None = None,
) -> None:
    server = create_server(
        stack, host, port, public_port=public_port, knowledge_drivers=knowledge_drivers
    )
    print("work-stack web: http://{}:{}/".format(host, server.actual_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = (
    "AGENT_CLIENT_HEADER",
    "AGENT_CLIENT_VALUE",
    "CAPTURE_BODY_LIMIT",
    "CLI_GET_ROUTES",
    "DEFAULT_BODY_LIMIT",
    "FRONTEND_ROOT",
    "GetRoute",
    "Handler",
    "IDEMPOTENT_POST_ROUTES",
    "KnowledgeDriverBinding",
    "LEGACY_WEB_ROOT",
    "LOOPBACK_HOSTS",
    "PROJECT_ROOT",
    "PostRoute",
    "RequestError",
    "V1_GET_ROUTES",
    "V1_POST_ROUTES",
    "WorkStackHTTPServer",
    "_get_route",
    "_post_route",
    "create_server",
    "serve",
)
