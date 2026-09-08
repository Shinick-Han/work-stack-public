"""Compatibility seams after extracting cli_writer collaborators."""

from __future__ import annotations

import ast
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote as urllib_quote

from workstack import cli_writer
from workstack import cli_writer_owner
from workstack import cli_writer_records
from workstack import cli_writer_transport


ROOT = Path(__file__).resolve().parents[1]
COLLABORATORS = (
    "workstack/cli_writer_owner.py",
    "workstack/cli_writer_transport.py",
    "workstack/cli_writer_planning.py",
    "workstack/cli_writer_tasks.py",
    "workstack/cli_writer_subtasks.py",
    "workstack/cli_writer_records.py",
)
FORBIDDEN_IMPORTS = frozenset(
    {
        "workstack.cli",
        "workstack.server",
        "workstack.cli_routing",
    }
)
PUBLIC_FORWARDERS = (
    "forward_note",
    "forward_objective",
    "forward_key_result",
    "forward_task_note",
    "forward_task_status",
    "forward_subtask",
    "forward_subtask_status",
    "forward_checkin",
    "forward_worklog_entry",
    "forward_backlog_add",
    "forward_okr_link",
    "forward_okr_progress",
    "read_owner_binding",
    "expected_workspace_uid",
    "owner_metadata_state",
    "new_idempotency_key",
    "_forward_write",
    "_preflight",
    "_origin",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = "workstack"
                suffix = node.module or ""
                names.add(f"{package}.{suffix}" if suffix else package)
            elif node.module:
                names.add(node.module)
    return names


class CliWriterRefactorContract(unittest.TestCase):
    def test_exception_identities_stay_on_the_facade(self) -> None:
        self.assertIs(cli_writer.WriterTransportError, cli_writer_owner.WriterTransportError)
        self.assertIs(cli_writer.CommitUnknownError, cli_writer_owner.CommitUnknownError)
        self.assertTrue(
            issubclass(cli_writer.CommitUnknownError, cli_writer.WriterTransportError)
        )
        self.assertIs(cli_writer.CommitUnknownError, cli_writer_transport.CommitUnknownError)

    def test_public_forwarders_and_injected_types_remain_callables(self) -> None:
        for name in PUBLIC_FORWARDERS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(cli_writer, name)))
        self.assertIs(cli_writer.quote, urllib_quote)
        self.assertIs(cli_writer.RequestJson, cli_writer_owner.RequestJson)
        self.assertIs(cli_writer.CoordinatesReader, cli_writer_owner.CoordinatesReader)

    def test_collaborators_do_not_import_cli_server_or_routing(self) -> None:
        for relative in COLLABORATORS + ("workstack/cli_writer.py",):
            imported = _imported_modules(ROOT / relative)
            with self.subTest(path=relative):
                self.assertTrue(imported.isdisjoint(FORBIDDEN_IMPORTS), imported)

    def test_csrf_origin_and_idempotency_key_stay_on_the_write_headers(self) -> None:
        headers = cli_writer._write_headers(
            "127.0.0.1", 9, "csrf-token", "POST", "cli-note-same", None, False
        )
        self.assertEqual(headers["Origin"], "http://127.0.0.1:9")
        self.assertEqual(headers["X-WorkStack-CSRF"], "csrf-token")
        self.assertEqual(headers["Idempotency-Key"], "cli-note-same")
        replay = cli_writer._write_headers(
            "127.0.0.1", 9, "csrf-token", "POST", "cli-note-same", None, False
        )
        self.assertEqual(headers["Idempotency-Key"], replay["Idempotency-Key"])

    def test_advertisement_read_uses_patched_cli_writer_open(self) -> None:
        hits: list[object] = []
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "server.json"
            path.write_text(
                json.dumps({"version": 1, "host": "127.0.0.1", "port": 9}),
                encoding="utf-8",
            )
            store = types.SimpleNamespace(server_info_path=path)

            def deny(target, mode="r", *args, **kwargs):
                hits.append((target, mode))
                raise PermissionError("fixture denial")

            with mock.patch.object(cli_writer, "open", deny, create=True):
                with self.assertRaises(cli_writer.WriterTransportError):
                    cli_writer.read_owner_binding(store)
        self.assertEqual(hits, [(path, "rb")])

    def test_checkin_clock_uses_patched_cli_writer_datetime(self) -> None:
        sentinel = object()
        with mock.patch.object(cli_writer, "datetime", sentinel):
            self.assertIs(cli_writer_records._clock(), sentinel)
