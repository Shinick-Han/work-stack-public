"""Focused contract tests for ordinary ``worklog latest-checkpoint``.

Parser, capability, transport, and Agent-compatibility cases do not import the
R58 core module. Runtime emit/local-owner matching waits for that independently
accepted dependency and is skipped until ``workstack.checkpoint_facts`` exists.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import traceback
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from workstack import cli, cli_capabilities as caps, cli_reads
from workstack.cli_parser_root import parser
from workstack.cli_read_http import CLI_GET_ROUTES
from workstack.cli_read_transport import forward_read, read_path
from workstack.cli_writer import OWNER_PRESENT, WriterTransportError
from workstack.service import WorkStack
from workstack.store import DEFAULTS, LOCK_NAME, Store, _FileLease


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CORE_AVAILABLE = importlib.util.find_spec("workstack.checkpoint_facts") is not None
UID = "11111111-1111-4111-8111-111111111111"
TASK = "T-0001"
AUDIT_PATH = "/api/v1/review/checkpoints"
RAW_HTTP_BODY = "RAW-SECRET-400-BODY"
FOREIGN_UID = "22222222-2222-4222-8222-222222222222"
PUBLIC_PORT = 28765
AGENT_FIXTURE_SHA256 = (
    "28540cac90d879ff0821d13752da0ecb80b8e3eaf984224c72c0c78b79061444"
)


def _parse(*arguments: str):
    return parser().parse_args(["worklog", "latest-checkpoint", *arguments])


class ParserAndRegistryTests(unittest.TestCase):
    def test_default_format_is_json(self) -> None:
        parsed = _parse("--workspace-uid", UID, "--task", TASK)
        self.assertEqual(parsed.action, "latest-checkpoint")
        self.assertEqual(parsed.workspace_uid, UID)
        self.assertEqual(parsed.task, TASK)
        self.assertEqual(parsed.format, "json")

    def test_explicit_json_and_markdown_formats(self) -> None:
        json_parsed = _parse(
            "--workspace-uid", UID, "--task", TASK, "--format", "json"
        )
        markdown_parsed = _parse(
            "--workspace-uid", UID, "--task", TASK, "--format", "markdown"
        )
        self.assertEqual(json_parsed.format, "json")
        self.assertEqual(markdown_parsed.format, "markdown")

    def _refuse(self, *arguments: str) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                parser().parse_args(["worklog", "latest-checkpoint", *arguments])
        self.assertEqual(ctx.exception.code, 2)
        return err.getvalue()

    def test_workspace_uid_and_task_are_required(self) -> None:
        missing_uid = self._refuse("--task", TASK)
        missing_task = self._refuse("--workspace-uid", UID)
        self.assertIn("--workspace-uid", missing_uid)
        self.assertIn("--task", missing_task)

    def test_unknown_format_is_argparse_refusal(self) -> None:
        err = self._refuse(
            "--workspace-uid", UID, "--task", TASK, "--format", "yaml"
        )
        self.assertNotEqual(err.strip(), "")

    def test_legacy_worklog_list_still_omits_workspace_pin(self) -> None:
        parsed = parser().parse_args(["worklog", "list"])
        self.assertEqual(parsed.action, "list")
        self.assertIsNone(getattr(parsed, "workspace_uid", None))

    def test_leaf_is_admitted_owner_get_without_http_exclusion(self) -> None:
        capability = caps.require_capability(caps.LATEST_CHECKPOINT_KEY)
        self.assertEqual(capability.command_key, "worklog.latest-checkpoint")
        self.assertEqual(
            capability.online_route, "owner_get:/api/v1/review/checkpoints"
        )
        self.assertEqual(capability.offline_route, "stack:worklog")
        self.assertEqual(caps.command_family(capability), caps.FAMILY_ADMITTED)
        self.assertIn(caps.LATEST_CHECKPOINT_KEY, caps.SKILL_ADVERTISED_KEYS)
        self.assertNotIn(
            "GET /api/v1/review/checkpoints",
            {item.surface for item in caps.EXCLUSIONS},
        )
        self.assertIn("worklog.latest-checkpoint", cli_reads.PARITY_READ_KEYS)
        self.assertEqual(cli_reads.OWNER_HTTP_READ_PARITY, cli_reads.PARITY_READ_KEYS)
        self.assertNotIn(
            "_get_checkpoint_audit",
            [route.handler for route in CLI_GET_ROUTES],
        )


class TransportTests(unittest.TestCase):
    def test_read_path_is_audit_get_without_query(self) -> None:
        path = read_path(
            "worklog.latest-checkpoint",
            Namespace(workspace_uid=UID, task=TASK, format="json"),
            UID,
        )
        self.assertEqual(path, AUDIT_PATH)
        self.assertNotIn("?", path)
        listed = read_path(
            "worklog.list", Namespace(date="2026-09-02"), UID
        )
        self.assertIn("workspace_uid=", listed)
        self.assertTrue(listed.startswith("/api/v1/cli/worklog?"))


class _OwnerTransportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.uid = self.store.load("workspace.json")["id"]
        self.store.write_server_info("127.0.0.1", 34567)
        self.calls: list[tuple[str, str]] = []

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def request_json(
        self,
        _host: str,
        _port: int,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append((method, path))
        if path == "/api/v1/session":
            return 200, {"data": {"csrf_token": "csrf-token"}}
        if path == "/api/v1/storage":
            return 200, {"data": {"workspace_id": self.uid}}
        if path == "/api/v1/sync/status":
            return 200, {"data": {"state": "in-sync"}}
        if path == AUDIT_PATH:
            return self.audit_response()
        raise AssertionError("unexpected path " + path)

    def audit_response(self) -> tuple[int, dict[str, object]]:
        return 200, {"data": {"entries": []}}

    def forward(self, command_key: str = "worklog.latest-checkpoint") -> object:
        return forward_read(
            self.store,
            OWNER_PRESENT,
            command_key,
            Namespace(workspace_uid=self.uid, task=TASK, format="json", date=None),
            coordinates_reader=lambda _store: None,
            request_json=self.request_json,
        )


class OwnerAuditTransportTests(_OwnerTransportCase):
    def test_one_audit_get_without_query_after_preflight(self) -> None:
        payload = self.forward()
        self.assertEqual(payload, {"entries": []})
        self.assertEqual(
            self.calls,
            [
                ("GET", "/api/v1/session"),
                ("GET", "/api/v1/storage"),
                ("GET", "/api/v1/sync/status"),
                ("GET", AUDIT_PATH),
            ],
        )
        self.assertEqual(sum(1 for _method, path in self.calls if path == AUDIT_PATH), 1)

    def test_http_error_uses_fixed_message_not_raw_body(self) -> None:
        def audit_error() -> tuple[int, dict[str, object]]:
            return 400, {
                "error": {"code": "invalid_query", "message": RAW_HTTP_BODY}
            }

        self.audit_response = audit_error  # type: ignore[method-assign]
        with self.assertRaises(WriterTransportError) as ctx:
            self.forward()
        self.assertNotIn(RAW_HTTP_BODY, str(ctx.exception))
        self.assertIn("refused the checkpoint audit", str(ctx.exception))
        self.assertEqual(sum(1 for _method, path in self.calls if path == AUDIT_PATH), 1)

    def test_http_404_does_not_use_generic_not_found_body(self) -> None:
        def audit_missing() -> tuple[int, dict[str, object]]:
            return 404, {
                "error": {"code": "not_found", "message": RAW_HTTP_BODY}
            }

        self.audit_response = audit_missing  # type: ignore[method-assign]
        with self.assertRaises(WriterTransportError) as ctx:
            self.forward()
        self.assertNotIn(RAW_HTTP_BODY, str(ctx.exception))

    def test_old_read_http_error_still_uses_raw_message(self) -> None:
        def list_error(
            _host: str,
            _port: int,
            method: str,
            path: str,
            **_kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            self.calls.append((method, path))
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": self.uid}}
            if path == "/api/v1/sync/status":
                return 200, {"data": {"state": "in-sync"}}
            return 400, {"error": {"message": "invalid task status"}}

        with self.assertRaises(ValueError) as ctx:
            forward_read(
                self.store,
                OWNER_PRESENT,
                "worklog.list",
                Namespace(date=None),
                coordinates_reader=lambda _store: None,
                request_json=list_error,
            )
        self.assertEqual(str(ctx.exception), "invalid task status")


class CompatibilityTests(unittest.TestCase):
    def test_agent_contract_fixture_bytes_are_unchanged(self) -> None:
        from workstack.agent_cli_contract import contract_fixture_bytes

        actual = contract_fixture_bytes()
        self.assertEqual(hashlib.sha256(actual).hexdigest(), AGENT_FIXTURE_SHA256)
        self.assertIn(b"workstack.cli.v1", actual)
        self.assertNotIn(b"workstack.checkpoint-facts.v1", actual)
        self.assertNotIn(b"latest-checkpoint", actual)

    def test_worklog_list_still_pretty_prints(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "data"
        runtime = Path(temporary.name) / "runtime"
        scratch = Path(temporary.name) / "tmp"
        runtime.mkdir()
        scratch.mkdir()
        saved = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }

        def restore() -> None:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        os.environ["WORK_STACK_RUNTIME"] = str(runtime)
        os.environ["TEMP"] = str(scratch)
        os.environ["TMP"] = str(scratch)
        os.environ["TMPDIR"] = str(scratch)
        self.addCleanup(restore)
        WorkStack(Store(root))
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(root), "worklog", "list"])
        self.assertEqual(code, 0, msg=err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertIn("\n  ", out.getvalue())
        self.assertIsInstance(payload, dict)


def _run_cli(root: Path, *arguments: str, forbid_local: bool = False) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.ExitStack() as contexts:
        contexts.enter_context(contextlib.redirect_stdout(out))
        contexts.enter_context(contextlib.redirect_stderr(err))
        if forbid_local:
            contexts.enter_context(
                mock.patch.object(
                    cli, "WorkStack", side_effect=AssertionError("local fallback")
                )
            )
        try:
            code = cli.main(["--data-dir", str(root), *arguments])
        except SystemExit as error:
            code = int(error.code or 2)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(CORE_AVAILABLE, "R58 checkpoint_facts core is not on this checkout")
class RuntimeLeafTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        created = self.stack.add_task("Checkpoint CLI task", "", "P2", None, [], [], None, [])
        self.task_id = created["id"]
        self.stack.add_worklog(created["id"], ["done item"], ["next item"], [], "2026-09-05")
        self.uid = self.store.load("workspace.json")["id"]
        self.owner = None
        self.endpoints: list[tuple[object, threading.Thread]] = []
        self.argv = (
            "worklog",
            "latest-checkpoint",
            "--workspace-uid",
            self.uid,
            "--task",
            self.task_id,
        )

    def _restore(self) -> None:
        for server, thread in self.endpoints:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.endpoints = []
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def snapshot(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in DEFAULTS}

    def start_owner(self):
        from workstack.server import create_server

        server = create_server(self.stack, "127.0.0.1", 0, public_port=PUBLIC_PORT)
        server.errors = []

        def handle_error(_request, _address):
            server.errors.append(traceback.format_exc())

        server.handle_error = handle_error
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        self.endpoints.append((server, thread))
        self.owner = server
        return server

    def test_local_and_owner_stdout_match(self) -> None:
        local_code, local_out, local_err = _run_cli(self.root, *self.argv)
        self.assertEqual(local_code, 0, msg=local_err)
        self.assertNotEqual(local_out, "")
        self.assertNotIn("\n  ", local_out)
        self.start_owner()
        before = self.snapshot()
        owner_code, owner_out, owner_err = _run_cli(
            self.root, *self.argv, forbid_local=True
        )
        self.assertEqual(owner_code, 0, msg=owner_err)
        self.assertEqual(owner_out, local_out)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_markdown_uses_same_local_and_owner_document(self) -> None:
        argv = (*self.argv, "--format", "markdown")
        local_code, local_out, local_err = _run_cli(self.root, *argv)
        self.assertEqual(local_code, 0, msg=local_err)
        self.start_owner()
        owner_code, owner_out, owner_err = _run_cli(
            self.root, *argv, forbid_local=True
        )
        self.assertEqual(owner_code, 0, msg=owner_err)
        self.assertEqual(owner_out, local_out)

    def test_invalid_pin_has_empty_stdout_and_no_fallback(self) -> None:
        before = self.snapshot()
        code, out, err = _run_cli(
            self.root,
            "worklog",
            "latest-checkpoint",
            "--workspace-uid",
            FOREIGN_UID,
            "--task",
            self.task_id,
            forbid_local=True,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(self.snapshot(), before)

    def test_invalid_metadata_has_empty_stdout_and_no_fallback(self) -> None:
        self.store.server_info_path.write_text("{", encoding="utf-8")
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        before = self.snapshot()
        code, out, err = _run_cli(self.root, *self.argv, forbid_local=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(self.snapshot(), before)

    def test_owner_http_error_has_empty_stdout_no_raw_body_no_fallback(self) -> None:
        self.store.write_server_info("127.0.0.1", 34567)
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        calls: list[str] = []

        def request_json(_host, _port, method, path, **_kwargs):
            del method
            calls.append(path)
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": self.uid}}
            if path == "/api/v1/sync/status":
                return 200, {"data": {"state": "in-sync"}}
            if path == AUDIT_PATH:
                return 400, {
                    "error": {"code": "invalid_query", "message": RAW_HTTP_BODY}
                }
            raise AssertionError(path)

        before = self.snapshot()
        with mock.patch.object(cli, "_request_json", request_json):
            code, out, err = _run_cli(self.root, *self.argv, forbid_local=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(RAW_HTTP_BODY, err)
        self.assertIn("refused the checkpoint audit", err)
        self.assertEqual(calls.count(AUDIT_PATH), 1)
        self.assertEqual(self.snapshot(), before)

    def test_oversize_render_has_empty_stdout(self) -> None:
        import workstack.checkpoint_facts as core

        def boom(*_args, **_kwargs):
            raise ValueError("checkpoint facts output is too large")

        with mock.patch.object(core, "render_checkpoint_facts", boom):
            code, out, err = _run_cli(self.root, *self.argv)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("too large", err)

    def test_owner_refusal_does_not_fall_back_locally(self) -> None:
        self.store.write_server_info("127.0.0.1", 34567)
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)

        def request_json(_host, _port, method, path, **_kwargs):
            del method
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": self.uid}}
            if path == "/api/v1/sync/status":
                return 200, {"data": {"state": "in-sync"}}
            return 404, {"error": {"code": "not_found", "message": RAW_HTTP_BODY}}

        with mock.patch.object(cli, "_request_json", request_json):
            code, out, err = _run_cli(self.root, *self.argv, forbid_local=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(RAW_HTTP_BODY, err)


if __name__ == "__main__":
    unittest.main()
