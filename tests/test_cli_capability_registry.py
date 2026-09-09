from __future__ import annotations

import argparse
import ast
import dataclasses
import unittest
from pathlib import Path

from workstack import cli_capabilities as caps
from workstack.cli import parser as build_parser


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "workstack" / "cli_capabilities.py"
SUBTASK_OPERATIONS = ("add", "start", "done", "drop", "reopen")


def _row(capability: caps.CliCapability) -> tuple[object, ...]:
    return (
        capability.command_key,
        capability.entity_kind,
        capability.operation,
        capability.requires_revision,
        capability.requires_idempotency,
        capability.online_route,
        capability.offline_route,
        capability.event_kind,
        capability.undoable,
        capability.approval_required,
    )


def _by_key() -> dict[str, caps.CliCapability]:
    return {item.command_key: item for item in caps.CAPABILITIES}


class ParserRegistryParity(unittest.TestCase):
    def test_parser_walk_and_registry_walk_share_the_same_keys(self) -> None:
        self.assertEqual(
            caps.parser_command_keys(build_parser()),
            caps.registry_command_keys(),
        )

    def test_every_subtask_operation_is_a_registry_key(self) -> None:
        keys = caps.registry_command_keys()
        for operation in SUBTASK_OPERATIONS:
            with self.subTest(operation=operation):
                self.assertIn("backlog.subtask." + operation, keys)

    def test_backlog_subtask_is_one_parser_leaf_with_five_operations(self) -> None:
        parser = build_parser()
        backlog = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                backlog = action.choices["backlog"]
                break
        self.assertIsNotNone(backlog)
        subtask = None
        for action in backlog._actions:
            if isinstance(action, argparse._SubParsersAction):
                subtask = action.choices["subtask"]
                break
        self.assertIsNotNone(subtask)
        choices = [
            action.choices
            for action in subtask._actions
            if action.dest == "operation"
        ]
        self.assertEqual(len(choices), 1)
        self.assertEqual(tuple(choices[0]), SUBTASK_OPERATIONS)


class FailClosed(unittest.TestCase):
    def test_duplicate_command_key_fails_closed(self) -> None:
        row = _row(caps.CAPABILITIES[0])
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry((row, row), caps.EXCLUSION_ROWS)

    def test_incomplete_row_fails_closed(self) -> None:
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry((("backlog.add",),), caps.EXCLUSION_ROWS)

    def test_empty_field_fails_closed(self) -> None:
        row = list(_row(caps.CAPABILITIES[0]))
        row[0] = ""
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry((tuple(row),), caps.EXCLUSION_ROWS)

    def test_unknown_online_route_fails_closed(self) -> None:
        row = list(_row(caps.CAPABILITIES[0]))
        row[5] = "invented_route"
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry((tuple(row),), caps.EXCLUSION_ROWS)

    def test_unknown_offline_route_fails_closed(self) -> None:
        row = list(_row(caps.CAPABILITIES[0]))
        row[6] = "invented_offline"
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry((tuple(row),), caps.EXCLUSION_ROWS)

    def test_unknown_event_kind_fails_closed(self) -> None:
        row = list(_row(caps.CAPABILITIES[0]))
        row[7] = "invented_event"
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry((tuple(row),), caps.EXCLUSION_ROWS)

    def test_unknown_exclusion_kind_fails_closed(self) -> None:
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry(
                caps.CAPABILITY_ROWS,
                (("POST /api/v1/sync/adopt", "invented", "named"),),
            )

    def test_empty_exclusion_justification_fails_closed(self) -> None:
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.compile_cli_registry(
                caps.CAPABILITY_ROWS,
                (("POST /api/v1/sync/adopt", "http_post", ""),),
            )

    def test_parser_mixing_subcommands_and_operation_fails_closed(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="domain", required=True)
        child = sub.add_parser("mixed")
        child.add_subparsers(dest="action", required=True).add_parser("inner")
        child.add_argument("operation", choices=("add",))
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.parser_command_keys(parser)

    def test_walker_refuses_multiple_subparser_groups(self) -> None:
        parser = argparse.ArgumentParser()
        group = parser.add_subparsers(dest="one", required=True)
        group.add_parser("a")
        parser._actions.append(group)
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.parser_command_keys(parser)


class FamiliesAreNotAliased(unittest.TestCase):
    def test_agent_apply_is_skill_advertised_with_revision_not_idempotency(self) -> None:
        catalog = _by_key()
        apply = catalog[caps.AGENT_APPLY_KEY]
        self.assertIn(apply.command_key, caps.SKILL_ADVERTISED_KEYS)
        self.assertNotIn(apply.command_key, caps.AGENT_ENVELOPE_KEYS)
        self.assertEqual(caps.command_family(apply), caps.FAMILY_AGENT_APPLY)
        self.assertTrue(apply.requires_revision)
        self.assertFalse(apply.requires_idempotency)
        skill = frozenset(
            item.surface for item in caps.EXCLUSIONS if item.kind == "skill"
        )
        self.assertNotIn(caps.AGENT_APPLY_KEY, skill)

    def test_agent_apply_is_not_an_envelope_command(self) -> None:
        catalog = _by_key()
        apply = catalog[caps.AGENT_APPLY_KEY]
        self.assertNotIn(apply.command_key, caps.AGENT_ENVELOPE_KEYS)
        for key in caps.AGENT_ENVELOPE_KEYS:
            other = catalog[key]
            with self.subTest(key=key):
                self.assertNotEqual(apply.command_key, other.command_key)
                self.assertNotEqual(apply.online_route, other.online_route)
                self.assertNotEqual(apply.offline_route, other.offline_route)

    def test_worklog_checkpoint_state_is_not_agent_checkpoint(self) -> None:
        catalog = _by_key()
        state = catalog[caps.CHECKPOINT_STATE_KEY]
        checkpoint = catalog["agent.checkpoint"]
        self.assertNotEqual(state.command_key, checkpoint.command_key)
        self.assertNotEqual(state.online_route, checkpoint.online_route)
        self.assertNotEqual(state.offline_route, checkpoint.offline_route)
        self.assertNotEqual(state.event_kind, checkpoint.event_kind)

    def test_agent_apply_is_not_worklog_checkpoint_state(self) -> None:
        catalog = _by_key()
        apply = catalog[caps.AGENT_APPLY_KEY]
        state = catalog[caps.CHECKPOINT_STATE_KEY]
        self.assertNotEqual(apply.command_key, state.command_key)
        self.assertNotEqual(apply.online_route, state.online_route)
        self.assertNotEqual(apply.offline_route, state.offline_route)
        self.assertNotEqual(apply.event_kind, state.event_kind)

    def test_envelope_commands_are_distinct_from_each_other(self) -> None:
        catalog = _by_key()
        status = catalog["agent.status"]
        context = catalog["agent.context"]
        checkpoint = catalog["agent.checkpoint"]
        self.assertEqual(status.offline_route, context.offline_route)
        self.assertEqual(status.offline_route, checkpoint.offline_route)
        self.assertNotEqual(status.online_route, context.online_route)
        self.assertNotEqual(status.online_route, checkpoint.online_route)
        self.assertNotEqual(context.online_route, checkpoint.online_route)
        self.assertNotEqual(status.command_key, context.command_key)
        self.assertNotEqual(status.command_key, checkpoint.command_key)
        self.assertNotEqual(context.command_key, checkpoint.command_key)


class NamedExclusions(unittest.TestCase):
    def test_every_exclusion_is_named_and_justified(self) -> None:
        self.assertGreater(len(caps.EXCLUSIONS), 0)
        for item in caps.EXCLUSIONS:
            with self.subTest(kind=item.kind, surface=item.surface):
                self.assertTrue(item.surface)
                self.assertTrue(item.justification)
                self.assertIn(item.kind, caps.EXCLUSION_KINDS)

    def test_skill_exclusions_are_exactly_the_unadvertised_parser_keys(self) -> None:
        keys = frozenset(caps.registry_command_keys())
        skill = frozenset(
            item.surface for item in caps.EXCLUSIONS if item.kind == "skill"
        )
        self.assertTrue(skill)
        self.assertTrue(skill <= keys)
        self.assertEqual(keys - skill, caps.SKILL_ADVERTISED_KEYS)

    def test_http_exclusions_are_not_parser_command_keys(self) -> None:
        keys = frozenset(caps.registry_command_keys())
        for item in caps.EXCLUSIONS:
            if item.kind.startswith("http"):
                with self.subTest(surface=item.surface):
                    self.assertNotIn(item.surface, keys)

    def test_reads_maintenance_capture_and_destructive_ops_are_registered(self) -> None:
        keys = caps.registry_command_keys()
        required = (
            "backlog.list",
            "backlog.show",
            "okr.list",
            "okr.rollup",
            "worklog.list",
            "worklog.latest-checkpoint",
            "weekly",
            "snapshot.preview",
            "capture.ingest",
            "report.create",
            "maintenance.backup",
            "maintenance.verify",
            "maintenance.restore",
            "maintenance.relocate",
            "maintenance.initialize",
            "snapshot.export",
            "storage.migration.execute",
            "storage.v4-backup.restore",
        )
        for key in required:
            with self.subTest(key=key):
                self.assertIn(key, keys)


class ImmutableImport(unittest.TestCase):
    def test_registry_entries_are_frozen(self) -> None:
        self.assertIsInstance(caps.CAPABILITIES, tuple)
        self.assertIsInstance(caps.EXCLUSIONS, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            caps.CAPABILITIES[0].command_key = "aliased"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            caps.EXCLUSIONS[0].surface = "aliased"  # type: ignore[misc]

    def test_import_uses_only_stdlib_data_modules(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        allowed = {"argparse", "dataclasses"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIsNotNone(node.module)
                self.assertIn(node.module.split(".")[0], allowed | {"__future__"})
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)

    def test_parsed_argv_keys_equal_the_registry_for_every_leaf(self) -> None:
        parser = build_parser()
        argv_by_key = {
            "backlog.add": ["backlog", "add", "Title"],
            "backlog.list": ["backlog", "list"],
            "backlog.show": ["backlog", "show", "T-0001"],
            "backlog.start": ["backlog", "start", "T-0001"],
            "backlog.done": ["backlog", "done", "T-0001"],
            "backlog.drop": ["backlog", "drop", "T-0001"],
            "backlog.reopen": ["backlog", "reopen", "T-0001"],
            "backlog.note": ["backlog", "note", "T-0001", "text"],
            "backlog.subtask.add": ["backlog", "subtask", "add", "T-0001", "title"],
            "backlog.subtask.start": ["backlog", "subtask", "start", "T-0001", "S-1"],
            "backlog.subtask.done": ["backlog", "subtask", "done", "T-0001", "S-1"],
            "backlog.subtask.drop": ["backlog", "subtask", "drop", "T-0001", "S-1"],
            "backlog.subtask.reopen": ["backlog", "subtask", "reopen", "T-0001", "S-1"],
            "okr.add-objective": ["okr", "add-objective", "Obj"],
            "okr.add-key-result": ["okr", "add-key-result", "O-1", "KR"],
            "okr.list": ["okr", "list"],
            "okr.link": ["okr", "link", "O-1", "T-0001"],
            "okr.progress": ["okr", "progress", "O-1", "KR-1", "10"],
            "okr.rollup": ["okr", "rollup"],
            "worklog.checkin": ["worklog", "checkin"],
            "worklog.add": ["worklog", "add", "T-0001"],
            "worklog.checkpoint-state": [
                "worklog", "checkpoint-state", "CP-1",
                "--stdin", "--idempotency-key", "intent-01",
            ],
            "worklog.list": ["worklog", "list"],
            "worklog.latest-checkpoint": [
                "worklog", "latest-checkpoint",
                "--workspace-uid", "11111111-1111-4111-8111-111111111111",
                "--task", "T-0001",
            ],
            "weekly": ["weekly"],
            "note": ["note", "hello"],
            "capture.ingest": ["capture", "ingest", "--stdin"],
            "agent.apply": ["agent", "apply", "--stdin", "--intent-id", "intent-01"],
            "agent.status": ["agent", "status"],
            "agent.context": ["agent", "context", "--task", "T-0001"],
            "agent.checkpoint": [
                "agent", "checkpoint", "--intent-id", "intent-01", "--stdin",
            ],
            "snapshot.preview": ["snapshot", "preview", "T-0001"],
            "snapshot.export": [
                "snapshot", "export", "T-0001", "--out", "out.json",
                "--expected-revision", "1", "--expected-digest", "d",
            ],
            "storage.validate": ["storage", "validate", "path"],
            "storage.migration.plan": ["storage", "migration", "plan", "src"],
            "storage.migration.preview": [
                "storage", "migration", "preview", "src",
                "--candidate-created-at", "t",
            ],
            "storage.migration.execute": [
                "storage", "migration", "execute", "src",
                "--candidate-created-at", "t",
                "--expected-source-digest", "d",
                "--expected-conversion-digest", "e",
            ],
            "storage.migration.verify": [
                "storage", "migration", "verify",
                "--source", "s", "--candidate", "c", "--backup", "b", "--receipt", "r",
            ],
            "storage.migration.receipt": ["storage", "migration", "receipt", "path"],
            "storage.migration.resume": [
                "storage", "migration", "resume", "src",
                "--candidate-created-at", "t", "--candidate", "c", "--backup", "b",
                "--expected-source-digest", "d", "--expected-conversion-digest", "e",
            ],
            "storage.v4-backup.create": [
                "storage", "v4-backup", "create", "src", "--out", "out.zip",
            ],
            "storage.v4-backup.verify": ["storage", "v4-backup", "verify", "a.zip"],
            "storage.v4-backup.restore": [
                "storage", "v4-backup", "restore", "a.zip", "--to", "dest",
            ],
            "maintenance.backup": ["maintenance", "backup", "--out", "out"],
            "maintenance.verify": ["maintenance", "verify", "archive"],
            "maintenance.restore": ["maintenance", "restore", "archive", "--to", "dest"],
            "maintenance.relocate": ["maintenance", "relocate", "--to", "dest"],
            "maintenance.initialize": ["maintenance", "initialize"],
            "graph.export": ["graph", "export"],
            "graph.serve": ["graph", "serve"],
            "report.create": ["report", "create", "--date", "2026-08-30"],
        }
        self.assertEqual(set(argv_by_key), set(caps.registry_command_keys()))
        for key, argv in argv_by_key.items():
            with self.subTest(key=key):
                parsed = caps.command_key_from_parsed(parser.parse_args(argv))
                self.assertEqual(parsed, key)
                self.assertEqual(caps.require_capability(key).command_key, key)

    def test_uncovered_command_fails_closed(self) -> None:
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.require_capability("invented.command")
        with self.assertRaises(caps.CapabilityRegistryError):
            caps.command_key_from_parsed(argparse.Namespace(domain=None))

    def test_families_keep_apply_envelope_checkpoint_path_and_serve_distinct(self) -> None:
        by_key = {item.command_key: item for item in caps.CAPABILITIES}
        self.assertEqual(
            caps.command_family(by_key["agent.apply"]),
            caps.FAMILY_AGENT_APPLY,
        )
        self.assertEqual(
            caps.command_family(by_key["agent.status"]),
            caps.FAMILY_AGENT_ENVELOPE,
        )
        self.assertEqual(
            caps.command_family(by_key["worklog.checkpoint-state"]),
            caps.FAMILY_OWNER_REQUIRED,
        )
        self.assertEqual(
            caps.command_family(by_key["storage.validate"]),
            caps.FAMILY_PATH_TOOL,
        )
        self.assertEqual(
            caps.command_family(by_key["graph.serve"]),
            caps.FAMILY_PROCESS_OWNER,
        )
        self.assertEqual(
            caps.command_family(by_key["backlog.list"]),
            caps.FAMILY_ADMITTED,
        )
        self.assertEqual(
            caps.command_family(by_key["worklog.latest-checkpoint"]),
            caps.FAMILY_ADMITTED,
        )
        self.assertEqual(
            caps.command_family(by_key["report.create"]),
            caps.FAMILY_OWNER_REQUIRED,
        )

    def test_source_does_not_mention_store_network_environment_or_clock(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
        forbidden = {
            "Store",
            "WorkStack",
            "environ",
            "getenv",
            "datetime",
            "time",
            "now",
            "today",
            "socket",
            "urlopen",
            "HTTPConnection",
            "serve",
        }
        self.assertEqual(names & forbidden, set())


if __name__ == "__main__":
    unittest.main()
