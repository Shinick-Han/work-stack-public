"""Argparse construction for the planning and record-keeping domains.

Every builder takes the root subparser action and adds exactly the parser its
domain owns, in the order and with the flags, choices, defaults and help text
the shipped `work-stack --help` output already advertises. Nothing here reads
configuration, touches a Store or decides a route.
"""

from __future__ import annotations

import argparse


def add_backlog_parser(sub: argparse._SubParsersAction) -> None:
    backlog = sub.add_parser("backlog", help="manage projects and tasks")
    backlog_sub = backlog.add_subparsers(dest="action", required=True)
    add = backlog_sub.add_parser(
        "add", description="Create a task; running-owner requests are limited to 1 MiB."
    )
    add.add_argument("title")
    add.add_argument("--detail", default="")
    add.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")
    add.add_argument("--due")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--objective", action="append", default=[])
    add.add_argument("--parent")
    add.add_argument("--depends-on", action="append", default=[])
    listing = backlog_sub.add_parser("list")
    listing.add_argument("--status", default="active")
    show = backlog_sub.add_parser("show")
    show.add_argument("id")
    for action in ("start", "done", "drop", "reopen"):
        command = backlog_sub.add_parser(action)
        command.add_argument("id")
    note = backlog_sub.add_parser("note")
    note.add_argument("id")
    note.add_argument("text")
    subtask = backlog_sub.add_parser("subtask")
    subtask.add_argument("operation", choices=("add", "start", "done", "drop", "reopen"))
    subtask.add_argument("task")
    subtask.add_argument("subtask_or_title")
    subtask.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")


def add_okr_parser(sub: argparse._SubParsersAction) -> None:
    okr = sub.add_parser("okr", help="manage objectives and key results")
    okr_sub = okr.add_subparsers(dest="action", required=True)
    add_objective = okr_sub.add_parser("add-objective")
    add_objective.add_argument("text")
    add_objective.add_argument("--quarter")
    add_key_result = okr_sub.add_parser("add-key-result")
    add_key_result.add_argument("objective")
    add_key_result.add_argument("text")
    add_key_result.add_argument("--target", default="")
    okr_list = okr_sub.add_parser("list")
    okr_list.add_argument("--status", default="active")
    link = okr_sub.add_parser(
        "link", description="Link an objective and task; running-owner requests are limited to 1 MiB."
    )
    link.add_argument("objective")
    link.add_argument("task")
    progress = okr_sub.add_parser(
        "progress", description="Update key result progress; running-owner requests are limited to 1 MiB."
    )
    progress.add_argument("objective")
    progress.add_argument("key_result")
    progress.add_argument("value", type=int)
    okr_sub.add_parser("rollup")


def add_worklog_parser(sub: argparse._SubParsersAction) -> None:
    worklog = sub.add_parser("worklog", help="record daily progress")
    worklog_sub = worklog.add_subparsers(dest="action", required=True)
    checkin = worklog_sub.add_parser(
        "checkin", description="Record checkin; running-owner requests are limited to 1 MiB."
    )
    checkin.add_argument("--time")
    checkin.add_argument("--date")
    worklog_add = worklog_sub.add_parser(
        "add", description="Append worklog evidence; running-owner requests are limited to 1 MiB."
    )
    worklog_add.add_argument("task")
    worklog_add.add_argument("--done", action="append", default=[])
    worklog_add.add_argument("--next", dest="next_items", action="append", default=[])
    worklog_add.add_argument("--blocker", action="append", default=[])
    worklog_add.add_argument("--date")
    checkpoint_state = worklog_sub.add_parser("checkpoint-state")
    checkpoint_state.add_argument("checkpoint")
    checkpoint_state.add_argument("--stdin", action="store_true", required=True)
    checkpoint_state.add_argument("--idempotency-key", required=True)
    worklog_list = worklog_sub.add_parser("list")
    worklog_list.add_argument("--date")


def add_weekly_parser(sub: argparse._SubParsersAction) -> None:
    weekly = sub.add_parser("weekly", help="aggregate daily records")
    weekly.add_argument("--end")
    weekly.add_argument("--days", type=int, default=7)


def add_note_parser(sub: argparse._SubParsersAction) -> None:
    notes = sub.add_parser("note", help="add a graph note")
    notes.add_argument("text")
    notes.add_argument("--link", action="append", default=[])


def add_capture_parser(sub: argparse._SubParsersAction) -> None:
    capture = sub.add_parser("capture", help="send sanitized Capture Packet v1 data")
    capture_sub = capture.add_subparsers(dest="action", required=True)
    ingest = capture_sub.add_parser("ingest")
    ingest.add_argument("--stdin", action="store_true", required=True)
    ingest.add_argument("--idempotency-key")


__all__ = (
    "add_backlog_parser",
    "add_capture_parser",
    "add_note_parser",
    "add_okr_parser",
    "add_weekly_parser",
    "add_worklog_parser",
)
