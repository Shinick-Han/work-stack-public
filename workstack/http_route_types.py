"""Route declaration types and tables for the loopback HTTP server.

The GET and POST route tables are declaration data, not request handling:
each entry names a compiled path pattern and the ``Handler`` method that
serves it. They live here so ``server.py`` stays the request-handling
module, and are re-exported from there for existing consumers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PostRoute:
    name: str
    pattern: re.Pattern[str]
    handler: str

    def match(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)


def _post_route(name: str, path_pattern: str, handler: str) -> PostRoute:
    return PostRoute(name, re.compile(path_pattern), handler)


V1_POST_ROUTES = (
    _post_route("sync_adopt", r"/api/v1/sync/adopt", "_post_sync_adopt"),
    _post_route("sync_rebind", r"/api/v1/sync/rebind-workspace", "_post_sync_rebind"),
    _post_route("task_create", r"/api/v1/tasks", "_post_task_create"),
    _post_route("work_session_create", r"/api/v1/work-sessions", "_post_work_session_create"),
    _post_route("work_session_action", r"/api/v1/work-sessions/([^/]+)/(pause|resume|stop|worklog)", "_post_work_session_action"),
    _post_route("backup", r"/api/v1/maintenance/backup", "_post_backup"),
    _post_route("snapshot_export", r"/api/v1/tasks/([^/]+)/snapshot/export", "_post_snapshot_export"),
    _post_route("task_deletion_preview", r"/api/v1/tasks/([^/]+)/deletion-preview", "_post_deletion_preview"),
    _post_route("task_note", r"/api/v1/tasks/([^/]+)/notes", "_post_task_note"),
    _post_route("task_subtask", r"/api/v1/tasks/([^/]+)/subtasks", "_post_task_subtask"),
    _post_route("objective_create", r"/api/v1/objectives", "_post_objective_create"),
    _post_route("key_result_create", r"/api/v1/objectives/([^/]+)/key-results", "_post_key_result_create"),
    _post_route("note_create", r"/api/v1/notes", "_post_note_create"),
    _post_route("review_checkin", r"/api/v1/review/checkin", "_post_review_checkin"),
    _post_route("cli_checkin", r"/api/v1/cli/worklog/checkin", "_post_cli_checkin"),
    _post_route("cli_worklog_entry", r"/api/v1/cli/worklog/add", "_post_cli_worklog_entry"),
    _post_route("cli_backlog_add", r"/api/v1/cli/backlog/add", "_post_cli_backlog_add"),
    _post_route("cli_okr_link", r"/api/v1/cli/okr/link", "_post_cli_okr_link"),
    _post_route("cli_okr_progress", r"/api/v1/cli/okr/progress", "_post_cli_okr_progress"),
    _post_route("review_entry", r"/api/v1/review/entries", "_post_review_entry"),
    _post_route(
        "checkpoint_transition",
        r"/api/v1/review/checkpoints/([^/]+)/transitions",
        "_post_checkpoint_transition",
    ),
    _post_route("capture_ingest", r"/api/v1/captures", "_post_capture_ingest"),
    _post_route("capture_link", r"/api/v1/captures/([^/]+)/link", "_post_capture_link"),
    _post_route("capture_action_task", r"/api/v1/captures/([^/]+)/actions/([^/]+)/task", "_post_capture_action_task"),
    _post_route("capture_task", r"/api/v1/captures/([^/]+)/task", "_post_capture_task"),
    _post_route("capture_dismiss", r"/api/v1/captures/([^/]+)/dismiss", "_post_capture_dismiss"),
    _post_route("reply_create", r"/api/v1/replies", "_post_reply_create"),
    _post_route("reply_receipt", r"/api/v1/replies/([^/]+)/receipt", "_post_reply_receipt"),
    _post_route("report_create", r"/api/v1/reports", "_post_report_create"),
    _post_route("report_action", r"/api/v1/reports/([^/]+)/(revisions|finalize|archive|restore)", "_post_report_action"),
    _post_route("mutation_notice_undo", r"/api/v1/mutation-notices/([^/]+)/undo", "_post_mutation_notice_undo"),
)

IDEMPOTENT_POST_ROUTES = frozenset({
    "sync_adopt",
    "sync_rebind",
    "task_create",
    "work_session_create",
    "work_session_action",
    "task_note",
    "task_subtask",
    "objective_create",
    "key_result_create",
    "note_create",
    "review_checkin",
    "review_entry",
    "checkpoint_transition",
    "reply_create",
    "reply_receipt",
    "mutation_notice_undo",
    "report_create",
    "report_action",
})


@dataclass(frozen=True)
class GetRoute:
    pattern: re.Pattern[str]
    handler: str

    def match(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)


def _get_route(path_pattern: str, handler: str) -> GetRoute:
    return GetRoute(re.compile(path_pattern), handler)


V1_GET_ROUTES = (
    _get_route(r"/api/v1/session", "_get_session"),
    _get_route(r"/api/v1/health", "_get_health"),
    _get_route(r"/api/v1/sync/status", "_get_sync_status"),
    _get_route(r"/api/v1/sync/rebind-preview", "_get_sync_rebind_preview"),
    _get_route(r"/api/v1/sync/events", "_get_sync_events"),
    _get_route(r"/api/v1/events", "_get_events"),
    _get_route(r"/api/v1/storage", "_get_storage"),
    _get_route(r"/api/v1/workspace", "_get_workspace"),
    _get_route(r"/api/v1/search", "_get_search"),
    _get_route(r"/api/v1/reports/daily-preview", "_get_daily_report_preview"), _get_route(r"/api/v1/reports/weekly-preview", "_get_weekly_report_preview"),
    # Registered after the two shipped previews, and structurally unable to
    # take their paths: the report-document read excludes those exact
    # segments, so route order is not the only thing protecting them.
    _get_route(r"/api/v1/reports", "_get_report_documents"),
    _get_route(r"/api/v1/reports/(?!daily-preview$|weekly-preview$)([^/]+)", "_get_report_document"),
    _get_route(r"/api/v1/review/checkpoints", "_get_checkpoint_audit"),
    _get_route(r"/api/v1/review", "_get_review"),
    _get_route(r"/api/v1/work-sessions", "_get_work_sessions"),
    _get_route(r"/api/v1/objectives/([^/]+)", "_get_objective"),
    _get_route(r"/api/v1/tasks/([^/]+)/snapshot", "_get_snapshot"),
    _get_route(r"/api/v1/tasks/([^/]+)", "_get_task"),
    _get_route(r"/api/v1/captures", "_get_captures"),
    _get_route(r"/api/v1/mutation-notices", "_get_mutation_notices"),
)
