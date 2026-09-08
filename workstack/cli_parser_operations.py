"""Argparse construction for the operational domains.

Agent, snapshot, storage, maintenance and graph each own one parser here. The
storage tree is built in three steps because its migration and v4-backup
subtrees are separate command families that happen to share a parent; the
subparsers are still added in the order the shipped help output lists them.
"""

from __future__ import annotations

import argparse


def _add_context_arguments(agent_context: argparse.ArgumentParser) -> None:
    """`agent context` flags. `--view` is opt-in: omitting it keeps core-v1,
    and an unknown value is a parser refusal (exit 2), never a silent default.
    """

    agent_context.add_argument("--task", required=True)
    agent_context.add_argument(
        "--view", choices=("core-v1", "planning-v1"), default="core-v1"
    )


def add_agent_parser(sub: argparse._SubParsersAction) -> None:
    agent = sub.add_parser(
        "agent",
        help="apply one revision-guarded agent update without editing Store files",
    )
    agent.add_argument("--workspace-uid")
    agent_sub = agent.add_subparsers(dest="action", required=True)
    agent_apply = agent_sub.add_parser("apply")
    agent_apply.add_argument("--stdin", action="store_true", required=True)
    agent_apply.add_argument("--intent-id", required=True)
    agent_sub.add_parser("status")
    agent_context = agent_sub.add_parser("context")
    _add_context_arguments(agent_context)
    agent_checkpoint = agent_sub.add_parser("checkpoint")
    agent_checkpoint.add_argument("--intent-id", required=True)
    agent_checkpoint.add_argument("--stdin", action="store_true", required=True)


def add_snapshot_parser(sub: argparse._SubParsersAction) -> None:
    snapshot = sub.add_parser("snapshot", help="review and export one planning snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="action", required=True)
    snapshot_preview = snapshot_sub.add_parser("preview")
    snapshot_preview.add_argument("task")
    snapshot_export = snapshot_sub.add_parser("export")
    snapshot_export.add_argument("task")
    snapshot_export.add_argument("--out", required=True)
    snapshot_export.add_argument("--expected-revision", required=True, type=int)
    snapshot_export.add_argument("--expected-digest", required=True)
    snapshot_export.add_argument("--confirm-disclosure", action="store_true")


def _add_migration_parsers(storage_sub: argparse._SubParsersAction) -> None:
    migration = storage_sub.add_parser("migration", help="plan or verify an explicit v3-to-v4 copy")
    migration_sub = migration.add_subparsers(dest="migration_action", required=True)
    migration_plan = migration_sub.add_parser("plan")
    migration_plan.add_argument("source")
    migration_plan.add_argument("--candidate")
    migration_plan.add_argument("--backup")
    migration_preview = migration_sub.add_parser("preview")
    migration_preview.add_argument("source")
    migration_preview.add_argument("--candidate-created-at", required=True)
    migration_preview.add_argument("--candidate")
    migration_preview.add_argument("--backup")
    migration_execute = migration_sub.add_parser("execute")
    migration_execute.add_argument("source")
    migration_execute.add_argument("--candidate-created-at", required=True)
    migration_execute.add_argument("--candidate")
    migration_execute.add_argument("--backup")
    migration_execute.add_argument("--expected-source-digest", required=True)
    migration_execute.add_argument("--expected-conversion-digest", required=True)
    migration_verify = migration_sub.add_parser("verify")
    migration_verify.add_argument("--source", required=True)
    migration_verify.add_argument("--candidate", required=True)
    migration_verify.add_argument("--backup", required=True)
    migration_verify.add_argument("--receipt", required=True)
    migration_receipt = migration_sub.add_parser("receipt")
    migration_receipt.add_argument("path")
    migration_resume = migration_sub.add_parser("resume")
    migration_resume.add_argument("source")
    migration_resume.add_argument("--candidate-created-at", required=True)
    migration_resume.add_argument("--candidate", required=True)
    migration_resume.add_argument("--backup", required=True)
    migration_resume.add_argument("--expected-source-digest", required=True)
    migration_resume.add_argument("--expected-conversion-digest", required=True)


def _add_v4_backup_parsers(storage_sub: argparse._SubParsersAction) -> None:
    v4_backup = storage_sub.add_parser(
        "v4-backup", help="explicitly create, verify, or restore an inactive v4 authority"
    )
    v4_backup_sub = v4_backup.add_subparsers(dest="v4_backup_action", required=True)
    v4_backup_create = v4_backup_sub.add_parser("create")
    v4_backup_create.add_argument("source")
    v4_backup_create.add_argument("--out", required=True)
    v4_backup_verify = v4_backup_sub.add_parser("verify")
    v4_backup_verify.add_argument("archive")
    v4_backup_restore = v4_backup_sub.add_parser("restore")
    v4_backup_restore.add_argument("archive")
    v4_backup_restore.add_argument("--to", required=True)


def add_storage_parser(sub: argparse._SubParsersAction) -> None:
    storage = sub.add_parser("storage", help="inspect a local SSOT without modifying it")
    storage_sub = storage.add_subparsers(dest="action", required=True)
    storage_validate = storage_sub.add_parser("validate")
    storage_validate.add_argument("path", help="candidate v3 or v4 SSOT directory")
    _add_migration_parsers(storage_sub)
    _add_v4_backup_parsers(storage_sub)


def add_maintenance_parser(sub: argparse._SubParsersAction) -> None:
    maintenance = sub.add_parser("maintenance", help="verify, back up, restore, relocate, or initialize local data")
    maintenance_sub = maintenance.add_subparsers(dest="action", required=True)
    backup = maintenance_sub.add_parser("backup")
    backup.add_argument("--out", required=True, help="backup directory")
    verify = maintenance_sub.add_parser("verify")
    verify.add_argument("archive")
    restore = maintenance_sub.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--to", required=True, help="destination data directory")
    restore.add_argument("--replace", action="store_true")
    restore.add_argument("--safety-backups", help="required when replacing an existing store")
    relocate = maintenance_sub.add_parser("relocate")
    relocate.add_argument("--to", required=True, help="empty destination data directory")
    maintenance_sub.add_parser(
        "initialize", help="create a new empty workspace in an absent or empty data directory"
    )


def add_graph_parser(sub: argparse._SubParsersAction) -> None:
    graph = sub.add_parser("graph", help="export or serve the web dashboard")
    graph_sub = graph.add_subparsers(dest="action", required=True)
    export = graph_sub.add_parser("export")
    export.add_argument("--out", default="graph-data.json")
    server = graph_sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument(
        "--public-port",
        type=int,
        help="additional loopback Host port accepted behind a local SSH forward",
    )
    server.add_argument(
        "--exit-with-parent",
        action="store_true",
        help="on Linux, stop the server when its SSH session parent exits",
    )
    server.add_argument(
        "--seed-demo",
        action="store_true",
        help="copy tracked demo fixtures only when runtime data is empty",
    )


__all__ = (
    "add_agent_parser",
    "add_graph_parser",
    "add_maintenance_parser",
    "add_snapshot_parser",
    "add_storage_parser",
)
