"""Compatibility contract for the HTTP handler and CLI parser extraction.

The transport surface moved out of `workstack/server.py` into cohesive
collaborators, and the argparse construction moved out of `workstack/cli.py`
into the `cli_parser_*` builders. Nothing a consumer, a test double or a
monkeypatch seam can observe was allowed to move with it, so this suite pins
the observable surface rather than the file layout: the names that must stay
importable, the handler methods every declared route still resolves to, the
route-match precedence, and the exact published domain order of the CLI.
"""

from __future__ import annotations

import argparse
import importlib
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from workstack import cli, cli_parser_root, server
from workstack.cli_read_http import CLI_GET_ROUTES
from workstack.http_route_types import V1_GET_ROUTES, V1_POST_ROUTES


SERVER_PUBLIC_NAMES = (
    "CAPTURE_BODY_LIMIT",
    "DEFAULT_BODY_LIMIT",
    "GetRoute",
    "Handler",
    "IDEMPOTENT_POST_ROUTES",
    "LOOPBACK_HOSTS",
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

CLI_PUBLIC_NAMES = (
    "apply_agent_update",
    "emit",
    "forward_capture",
    "forward_checkpoint_state",
    "main",
    "parser",
    "_request_json",
    "_server_coordinates",
)

EXTRACTED_MODULES = (
    "workstack.cli_parser_operations",
    "workstack.cli_parser_planning",
    "workstack.cli_parser_root",
    "workstack.server_admission",
    "workstack.server_errors",
    "workstack.server_patch_routes",
    "workstack.server_post_routes",
    "workstack.server_read_routes",
    "workstack.server_transport",
)

PUBLISHED_DOMAINS = (
    "backlog",
    "okr",
    "worklog",
    "weekly",
    "note",
    "capture",
    "agent",
    "snapshot",
    "storage",
    "maintenance",
    "graph",
)


class ExtractedModuleImportTest(unittest.TestCase):
    def test_each_extracted_module_imports_on_its_own(self) -> None:
        """No collaborator may depend on being imported through its facade."""

        for name in EXTRACTED_MODULES:
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))


class ServerSurfaceTest(unittest.TestCase):
    def test_public_names_stay_importable_from_the_server_module(self) -> None:
        for name in SERVER_PUBLIC_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(server, name), name)

    def test_every_declared_route_resolves_to_a_handler_method(self) -> None:
        routes = list(CLI_GET_ROUTES) + list(V1_GET_ROUTES) + list(V1_POST_ROUTES)
        for route in routes:
            with self.subTest(route=route.handler):
                self.assertTrue(callable(getattr(server.Handler, route.handler, None)))

    def test_cli_owner_reads_keep_precedence_over_the_general_get_table(self) -> None:
        route, match = server.Handler._match_v1_get_route("/api/v1/cli/okr/rollup")
        self.assertIsNotNone(match)
        self.assertIn(route, CLI_GET_ROUTES)
        self.assertEqual(route.handler, "_get_cli_okr_rollup")

    def test_unknown_get_path_matches_no_route(self) -> None:
        route, match = server.Handler._match_v1_get_route("/api/v1/not-a-route")
        self.assertIsNone(route)
        self.assertIsNone(match)

    def test_post_route_table_order_is_the_match_order(self) -> None:
        route, match = server.Handler._match_v1_post_route("/api/v1/captures")
        self.assertIsNotNone(match)
        self.assertEqual(route.name, "capture_ingest")

    def test_handler_class_level_patching_still_overrides_a_mixin_method(self) -> None:
        """`mock.patch.object(Handler, ...)` is a load-bearing test seam."""

        original = server.Handler._get_workspace
        with mock.patch.object(server.Handler, "_get_workspace", lambda *a, **k: None):
            self.assertIsNot(server.Handler._get_workspace, original)
        self.assertIs(server.Handler._get_workspace, original)

    def test_body_limits_keep_their_published_values(self) -> None:
        self.assertEqual(server.CAPTURE_BODY_LIMIT, 64 * 1024)
        self.assertEqual(server.DEFAULT_BODY_LIMIT, 1024 * 1024)

    def test_request_error_carries_its_code_status_and_details(self) -> None:
        error = server.RequestError("invalid_body", "message", 400, {"field": "x"})
        self.assertIsInstance(error, ValueError)
        self.assertEqual(
            (error.code, error.status, error.details, str(error)),
            ("invalid_body", 400, {"field": "x"}, "message"),
        )


class CliParserSurfaceTest(unittest.TestCase):
    def test_public_names_stay_importable_from_the_cli_module(self) -> None:
        for name in CLI_PUBLIC_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(cli, name), name)

    def test_cli_parser_is_the_extracted_builder(self) -> None:
        self.assertIs(cli.parser, cli_parser_root.parser)

    def test_parser_returns_a_fresh_argument_parser_each_call(self) -> None:
        first = cli.parser()
        second = cli.parser()
        self.assertIsInstance(first, argparse.ArgumentParser)
        self.assertIsNot(first, second)

    def test_published_domain_order_is_unchanged(self) -> None:
        actions = [
            action
            for action in cli.parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(actions), 1)
        self.assertEqual(tuple(actions[0].choices), PUBLISHED_DOMAINS)

    def test_module_level_parser_stays_patchable_for_main(self) -> None:
        """`patch.object(cli, "parser", ...)` must still steer `main`."""

        sentinel = argparse.ArgumentParser(prog="work-stack")
        sentinel.add_argument("--data-dir")
        sentinel.add_subparsers(dest="domain", required=True).add_parser("weekly")
        with mock.patch.object(cli, "parser", return_value=sentinel):
            with mock.patch.object(
                cli, "_dispatch_parsed", return_value=0
            ) as dispatch:
                self.assertEqual(cli.main(["weekly"]), 0)
        self.assertEqual(dispatch.call_count, 1)

    def test_agent_context_view_choice_is_still_a_parser_refusal(self) -> None:
        stream = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(stream), redirect_stderr(stream):
                cli.parser().parse_args(
                    ["agent", "context", "--task", "t", "--view", "planning-v9"]
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
