"""Structural tests for the extracted HTTP route declaration module.

`workstack.http_route_types` holds the GET/POST route declaration types, their
factories and the two route tables that used to live inline in
`workstack.server`. The extraction is only safe if three things stayed true,
and each is asserted here against the real modules rather than a restatement:

1. The names are still reachable from `workstack.server`, and are the *same
   objects* — a copy would let the two modules drift apart silently.
2. Matching is still anchored. `PostRoute.match` / `GetRoute.match` delegate to
   `fullmatch`, so a pattern may never answer for a longer path that merely
   starts with it.
3. The tables still carry the same entries in the same order, and every handler
   they name is really defined on the composed `Handler`.
"""

from __future__ import annotations

import re
import unittest

from workstack import http_route_types, server


class RouteAliasTest(unittest.TestCase):
    """server.py re-exports the declarations; consumers importing from there win."""

    def test_the_server_module_exposes_the_same_objects(self) -> None:
        for name in (
            "GetRoute",
            "PostRoute",
            "V1_GET_ROUTES",
            "V1_POST_ROUTES",
            "IDEMPOTENT_POST_ROUTES",
            "_get_route",
            "_post_route",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(server, name), getattr(http_route_types, name))


class RouteDataclassTest(unittest.TestCase):
    """The declarations stay frozen values that match on the whole path."""

    def test_a_post_route_is_a_frozen_value(self) -> None:
        route = http_route_types._post_route("demo", r"/api/v1/demo", "_post_demo")
        self.assertEqual(route.name, "demo")
        self.assertEqual(route.handler, "_post_demo")
        self.assertIsInstance(route.pattern, re.Pattern)
        self.assertEqual(route, http_route_types.PostRoute(
            "demo", route.pattern, "_post_demo"
        ))
        with self.assertRaises(Exception):
            route.name = "other"  # type: ignore[misc]

    def test_a_get_route_is_a_frozen_value(self) -> None:
        route = http_route_types._get_route(r"/api/v1/demo", "_get_demo")
        self.assertEqual(route.handler, "_get_demo")
        self.assertIsInstance(route.pattern, re.Pattern)
        self.assertEqual(route, http_route_types.GetRoute(route.pattern, "_get_demo"))
        with self.assertRaises(Exception):
            route.handler = "_get_other"  # type: ignore[misc]

    def test_matching_is_anchored_at_both_ends(self) -> None:
        """`fullmatch`, not `match`: a prefix hit is not a route hit."""

        post = http_route_types._post_route("demo", r"/api/v1/demo", "_post_demo")
        get = http_route_types._get_route(r"/api/v1/demo", "_get_demo")
        for route in (post, get):
            with self.subTest(route=type(route).__name__):
                self.assertIsNotNone(route.match("/api/v1/demo"))
                self.assertIsNone(route.match("/api/v1/demo/extra"))
                self.assertIsNone(route.match("/prefix/api/v1/demo"))

    def test_a_match_returns_the_captured_groups(self) -> None:
        route = http_route_types._get_route(r"/api/v1/tasks/([^/]+)", "_get_task")
        match = route.match("/api/v1/tasks/T-1")
        assert match is not None
        self.assertEqual(match.groups(), ("T-1",))


class RouteTableTest(unittest.TestCase):
    """Order and wiring are contract, not incidental."""

    def test_the_get_table_keeps_its_declared_order(self) -> None:
        self.assertEqual(
            [route.handler for route in http_route_types.V1_GET_ROUTES],
            [
                "_get_session",
                "_get_health",
                "_get_sync_status",
                "_get_sync_rebind_preview",
                "_get_sync_events",
                "_get_events",
                "_get_storage",
                "_get_workspace",
                "_get_search",
                "_get_daily_report_preview",
                "_get_weekly_report_preview",
                "_get_report_documents",
                "_get_report_document",
                "_get_checkpoint_audit",
                "_get_review",
                "_get_work_sessions",
                "_get_objective",
                "_get_snapshot",
                "_get_task",
                "_get_captures",
                "_get_mutation_notices",
            ],
        )

    def test_the_post_table_keeps_its_declared_order(self) -> None:
        self.assertEqual(
            [route.name for route in http_route_types.V1_POST_ROUTES],
            [
                "sync_adopt",
                "sync_rebind",
                "task_create",
                "work_session_create",
                "work_session_action",
                "backup",
                "snapshot_export",
                "task_deletion_preview",
                "task_note",
                "task_subtask",
                "objective_create",
                "key_result_create",
                "note_create",
                "review_checkin",
                "cli_checkin",
                "cli_worklog_entry",
                "cli_backlog_add",
                "cli_okr_link",
                "cli_okr_progress",
                "review_entry",
                "checkpoint_transition",
                "capture_ingest",
                "capture_link",
                "capture_action_task",
                "capture_task",
                "capture_dismiss",
                "reply_create",
                "reply_receipt",
                "report_create",
                "report_action",
                "mutation_notice_undo",
            ],
        )

    def test_post_route_names_are_unique(self) -> None:
        names = [route.name for route in http_route_types.V1_POST_ROUTES]
        self.assertEqual(len(names), len(set(names)))

    def test_every_declared_handler_exists_on_the_handler(self) -> None:
        for route in http_route_types.V1_GET_ROUTES:
            with self.subTest(handler=route.handler):
                self.assertTrue(callable(getattr(server.Handler, route.handler, None)))
        for route in http_route_types.V1_POST_ROUTES:
            with self.subTest(handler=route.handler):
                self.assertTrue(callable(getattr(server.Handler, route.handler, None)))

    def test_idempotent_names_are_a_subset_of_the_post_table(self) -> None:
        self.assertLessEqual(
            http_route_types.IDEMPOTENT_POST_ROUTES,
            {route.name for route in http_route_types.V1_POST_ROUTES},
        )

    def test_the_report_document_route_still_excludes_the_previews(self) -> None:
        pattern = next(
            route.pattern
            for route in http_route_types.V1_GET_ROUTES
            if route.handler == "_get_report_document"
        )
        self.assertIsNone(pattern.fullmatch("/api/v1/reports/daily-preview"))
        self.assertIsNone(pattern.fullmatch("/api/v1/reports/weekly-preview"))
        self.assertIsNotNone(pattern.fullmatch("/api/v1/reports/RPT-1"))


if __name__ == "__main__":
    unittest.main()
