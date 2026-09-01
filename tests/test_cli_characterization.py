from __future__ import annotations

import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from workstack import cli
from workstack.service import DomainError
from workstack.storage.migration import StorageMigrationError


class _Parsed:
    def __init__(self, arguments: Namespace) -> None:
        self.arguments = arguments

    def parse_args(self, argv: list[str] | None) -> Namespace:
        return self.arguments


def _arguments(domain: str, action: str | None = None, **values: object) -> Namespace:
    defaults: dict[str, object] = {"data_dir": None, "domain": domain}
    if action is not None:
        defaults["action"] = action
    defaults.update(values)
    return Namespace(**defaults)


class CliMainCharacterizationTests(unittest.TestCase):
    def _invoke(
        self,
        arguments: Namespace,
        *,
        stack: Mock | None = None,
        stdin: bytes = b"",
    ) -> tuple[int, Mock, Mock]:
        store = Mock()
        work_stack = stack or Mock()
        with (
            patch.object(cli, "parser", return_value=_Parsed(arguments)),
            patch.object(cli, "Store", return_value=store) as store_type,
            patch.object(cli, "WorkStack", return_value=work_stack) as stack_type,
            patch.object(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin))),
        ):
            result = cli.main(["ignored"])
        return result, store_type, stack_type

    def test_capture_and_agent_bypass_work_stack_initialization(self) -> None:
        capture = _arguments("capture", "ingest", idempotency_key="capture-key")
        with patch.object(cli, "forward_capture", return_value=7) as forward:
            result, store_type, stack_type = self._invoke(capture, stdin=b"capture")
        self.assertEqual(result, 7)
        forward.assert_called_once_with(store_type.return_value, b"capture", "capture-key")
        stack_type.assert_not_called()

        agent = _arguments("agent", "apply", intent_id="intent-123")
        with patch.object(cli, "apply_agent_update", return_value=8) as apply:
            result, store_type, stack_type = self._invoke(agent, stdin=b"agent")
        self.assertEqual(result, 8)
        apply.assert_called_once_with(store_type.return_value, b"agent", "intent-123")
        stack_type.assert_not_called()

    def test_maintenance_actions_bypass_work_stack_and_emit_stable_receipts(self) -> None:
        cases = (
            (
                _arguments("maintenance", "backup", out="backups"),
                "backup_store",
                SimpleNamespace(
                    path=Path("backup.zip"), workspace_id="workspace", created_at="now",
                    digest="digest", file_count=8,
                ),
                {"path": "backup.zip", "workspace_id": "workspace", "created_at": "now", "digest": "digest", "file_count": 8},
            ),
            (
                _arguments("maintenance", "verify", archive="backup.zip"),
                "verify_backup",
                SimpleNamespace(
                    path=Path("backup.zip"), workspace_id="workspace", created_at="now",
                    digest="digest", file_count=8,
                ),
                {"path": "backup.zip", "workspace_id": "workspace", "created_at": "now", "digest": "digest", "file_count": 8},
            ),
            (
                _arguments(
                    "maintenance", "restore", archive="backup.zip", to="store",
                    replace=True, safety_backups="safety",
                ),
                "restore_store",
                SimpleNamespace(
                    destination=Path("store"), workspace_id="workspace",
                    backup_digest="digest", safety_backup=Path("safety.zip"),
                ),
                {"destination": "store", "workspace_id": "workspace", "backup_digest": "digest", "safety_backup": "safety.zip"},
            ),
            (
                _arguments("maintenance", "relocate", to="new-store"),
                "relocate_store",
                SimpleNamespace(
                    destination=Path("new-store"), workspace_id="workspace", backup_digest="digest",
                ),
                {"destination": "new-store", "workspace_id": "workspace", "backup_digest": "digest", "source_preserved": True},
            ),
        )
        for arguments, function_name, receipt, expected in cases:
            with self.subTest(action=arguments.action):
                with (
                    patch.object(cli, function_name, return_value=receipt),
                    patch.object(cli, "emit") as emit,
                ):
                    result, _, stack_type = self._invoke(arguments)
                self.assertEqual(result, 0)
                emit.assert_called_once_with(expected)
                stack_type.assert_not_called()

    def test_storage_validation_bypasses_store_construction_and_is_content_free(self) -> None:
        issue = SimpleNamespace(
            code="SCHEMA_VIOLATION",
            artifact="records/tasks/22/record.json",
            instance_path="/title",
            keyword="required",
        )
        report = SimpleNamespace(
            valid=False,
            format_version=4,
            workspace_uid="11111111-1111-4111-8111-111111111111",
            record_count=1,
            issues=(issue,),
        )
        arguments = _arguments("storage", "validate", path="candidate-store")
        with (
            patch.object(cli, "validate_storage_path", return_value=report) as validate,
            patch.object(cli, "emit") as emit,
        ):
            result, store_type, stack_type = self._invoke(arguments)

        self.assertEqual(result, 2)
        validate.assert_called_once_with("candidate-store")
        store_type.assert_not_called()
        stack_type.assert_not_called()
        emit.assert_called_once_with({
            "status": "invalid",
            "format_version": 4,
            "workspace_uid": "11111111-1111-4111-8111-111111111111",
            "record_count": 1,
            "issues": [{
                "code": "SCHEMA_VIOLATION",
                "artifact": "records/tasks/22/record.json",
                "instance_path": "/title",
                "keyword": "required",
            }],
        })

    def test_storage_migration_plan_bypasses_application_store_and_stack(self) -> None:
        plan = SimpleNamespace(
            paths=SimpleNamespace(
                source_root=Path("source"),
                candidate_root=Path("candidate"),
                backup_path=Path("backup.zip"),
            ),
            frozen=SimpleNamespace(
                aggregate_digest="sha256:" + "1" * 64,
                artifacts=tuple(range(9)),
            ),
        )
        arguments = _arguments(
            "storage",
            "migration",
            migration_action="plan",
            source="source",
            candidate=None,
            backup=None,
        )
        with (
            patch.object(cli, "plan_v3_migration", return_value=plan) as migrate,
            patch.object(cli, "emit") as emit,
        ):
            result, store_type, stack_type = self._invoke(arguments)

        self.assertEqual(result, 0)
        migrate.assert_called_once_with(
            "source", candidate_override=None, backup_override=None
        )
        store_type.assert_not_called()
        stack_type.assert_not_called()
        emit.assert_called_once_with({
            "status": "planned",
            "source_path": "source",
            "candidate_path": "candidate",
            "backup_path": "backup.zip",
            "source_digest": "sha256:" + "1" * 64,
            "source_file_count": 9,
            "activated": False,
        })

    def test_storage_migration_refusal_is_machine_readable(self) -> None:
        arguments = _arguments(
            "storage",
            "migration",
            migration_action="plan",
            source="source",
            candidate=None,
            backup=None,
        )
        with (
            patch.object(
                cli,
                "plan_v3_migration",
                side_effect=StorageMigrationError("SOURCE_DIRECTORY_REQUIRED"),
            ),
            patch.object(cli, "emit") as emit,
        ):
            result, store_type, stack_type = self._invoke(arguments)

        self.assertEqual(result, 2)
        store_type.assert_not_called()
        stack_type.assert_not_called()
        emit.assert_called_once_with({
            "status": "refused",
            "code": "SOURCE_DIRECTORY_REQUIRED",
        })

    def test_storage_migration_parser_requires_explicit_execute_digests(self) -> None:
        arguments = cli.parser().parse_args([
            "storage",
            "migration",
            "execute",
            "source",
            "--candidate-created-at",
            "2026-09-01T12:00:00Z",
            "--expected-source-digest",
            "sha256:" + "1" * 64,
            "--expected-conversion-digest",
            "sha256:" + "2" * 64,
        ])

        self.assertEqual(arguments.domain, "storage")
        self.assertEqual(arguments.action, "migration")
        self.assertEqual(arguments.migration_action, "execute")

    def test_backlog_dispatch_preserves_argument_mapping(self) -> None:
        stack = Mock()
        stack.add_task.return_value = {"id": "T-0001"}
        arguments = _arguments(
            "backlog", "add", title="Title", detail="Detail", priority="P1", due="2026-09-01",
            tag=["tag"], objective=["O-1"], parent="T-0000", depends_on=["T-0002"],
        )
        with patch.object(cli, "emit") as emit:
            result, _, stack_type = self._invoke(arguments, stack=stack)
        self.assertEqual(result, 0)
        stack_type.assert_called_once_with(stack_type.call_args.args[0], initialize=True)
        stack.add_task.assert_called_once_with(
            "Title", "Detail", "P1", "2026-09-01", ["tag"], ["O-1"], "T-0000", ["T-0002"]
        )
        emit.assert_called_once_with({"id": "T-0001"})

    def test_backlog_status_and_subtask_status_mappings_are_stable(self) -> None:
        for action, status in (("start", "started"), ("done", "done"), ("drop", "dropped"), ("reopen", "open")):
            stack = Mock()
            with self.subTest(action=action), patch.object(cli, "emit"):
                result, _, _ = self._invoke(_arguments("backlog", action, id="T-0001"), stack=stack)
            self.assertEqual(result, 0)
            stack.set_task_status.assert_called_once_with("T-0001", status)

            stack = Mock()
            with self.subTest(subtask=action), patch.object(cli, "emit"):
                result, _, _ = self._invoke(
                    _arguments(
                        "backlog", "subtask", operation=action, task="T-0001",
                        subtask_or_title="S-1", priority="P2",
                    ),
                    stack=stack,
                )
            self.assertEqual(result, 0)
            stack.set_subtask_status.assert_called_once_with("T-0001", "S-1", status)

    def test_okr_worklog_weekly_and_note_dispatch_are_stable(self) -> None:
        cases = (
            (_arguments("okr", "add-objective", text="Objective", quarter="2026-Q3"), "add_objective", ("Objective", "2026-Q3")),
            (_arguments("okr", "add-key-result", objective="O-1", text="KR", target="95%"), "add_key_result", ("O-1", "KR", "95%")),
            (_arguments("okr", "list", status="active"), "list_objectives", ("active",)),
            (_arguments("okr", "link", objective="O-1", task="T-0001"), "link_task", ("O-1", "T-0001")),
            (_arguments("okr", "progress", objective="O-1", key_result="KR-1", value=75), "set_key_result_progress", ("O-1", "KR-1", 75)),
            (_arguments("okr", "rollup"), "objective_rollup", ()),
            (_arguments("worklog", "checkin", time="09:00", date="2026-08-31"), "checkin", ("09:00", "2026-08-31")),
            (_arguments("worklog", "add", task="T-0001", done=["done"], next_items=["next"], blocker=["block"], date="2026-08-31"), "add_worklog", ("T-0001", ["done"], ["next"], ["block"], "2026-08-31")),
            (_arguments("worklog", "list", date="2026-08-31"), "list_worklog", ("2026-08-31",)),
            (_arguments("weekly", end="2026-08-31", days=7), "weekly_report", ("2026-08-31", 7)),
            (_arguments("note", text="Note", link=["T-0001"]), "add_note", ("Note", ["T-0001"])),
        )
        for arguments, method_name, expected_args in cases:
            stack = Mock()
            getattr(stack, method_name).return_value = {"method": method_name}
            with self.subTest(domain=arguments.domain, action=getattr(arguments, "action", None)), patch.object(cli, "emit") as emit:
                result, _, _ = self._invoke(arguments, stack=stack)
            self.assertEqual(result, 0)
            getattr(stack, method_name).assert_called_once_with(*expected_args)
            emit.assert_called_once_with({"method": method_name})

    def test_snapshot_preview_and_export_preserve_read_only_initialization_and_output(self) -> None:
        artifact = SimpleNamespace(
            snapshot={"revision": 4}, digest="digest", filename="snapshot.json",
            omissions=("field",), canonical_bytes=b"snapshot",
        )
        for action in ("preview", "export"):
            stack = Mock()
            stack.planning_snapshot.return_value = artifact
            stack.confirmed_snapshot_export.return_value = artifact
            arguments = (
                _arguments("snapshot", "preview", task="T-0001")
                if action == "preview"
                else _arguments(
                    "snapshot", "export", task="T-0001", out="snapshot.json",
                    expected_revision=4, expected_digest="digest", confirm_disclosure=True,
                )
            )
            with (
                self.subTest(action=action),
                patch.object(cli, "emit") as emit,
                patch.object(cli, "write_snapshot_file", return_value=Path("snapshot.json")) as write,
            ):
                result, _, stack_type = self._invoke(arguments, stack=stack)
            self.assertEqual(result, 0)
            self.assertFalse(stack_type.call_args.kwargs["initialize"])
            if action == "preview":
                stack.planning_snapshot.assert_called_once_with("T-0001")
                emit.assert_called_once_with({"snapshot": {"revision": 4}, "digest": "digest", "filename": "snapshot.json", "omissions": ["field"]})
                write.assert_not_called()
            else:
                stack.confirmed_snapshot_export.assert_called_once_with("T-0001", 4, "digest", True)
                write.assert_called_once_with("snapshot.json", b"snapshot")
                emit.assert_called_once_with({"path": "snapshot.json", "digest": "digest", "revision": 4})

    def test_graph_export_and_serve_preserve_side_effect_boundaries(self) -> None:
        stack = Mock()
        stack.snapshot.return_value = {"workspace": {"id": "workspace"}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "graph.json"
            result, _, _ = self._invoke(
                _arguments("graph", "export", out=str(output)), stack=stack
            )
            self.assertEqual(result, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), '{\n  "workspace": {\n    "id": "workspace"\n  }\n}\n')

        stack = Mock()
        with patch.object(cli, "serve") as serve:
            result, _, _ = self._invoke(
                _arguments("graph", "serve", host="127.0.0.1", port=8765, seed_demo=True),
                stack=stack,
            )
        self.assertEqual(result, 0)
        stack.store.seed_demo.assert_called_once_with(cli.PROJECT_DATA)
        serve.assert_called_once_with(stack, "127.0.0.1", 8765)

    def test_domain_and_value_errors_keep_stderr_shape_and_exit_two(self) -> None:
        for error, expected in (
            (DomainError("stale revision"), "error: invalid_request: stale revision\n"),
            (ValueError("invalid input"), "error: invalid input\n"),
            (OSError("store unavailable"), "error: store unavailable\n"),
        ):
            stack = Mock()
            stack.list_tasks.side_effect = error
            stderr = io.StringIO()
            with self.subTest(error=type(error).__name__), patch.object(cli.sys, "stderr", stderr):
                result, _, _ = self._invoke(
                    _arguments("backlog", "list", status="active"), stack=stack
                )
            self.assertEqual(result, 2)
            self.assertEqual(stderr.getvalue(), expected)


if __name__ == "__main__":
    unittest.main()
