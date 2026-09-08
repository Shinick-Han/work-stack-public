"""CLI admission under acquire_owner_authority: one lease, fail closed."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from unittest import mock

from workstack import cli, cli_capabilities, cli_reads, cli_routing
from workstack.owner_authority import (
    EXCLUSIVE_LOCAL_HELD,
    OWNER_ROUTE_REQUIRED,
    WORKSPACE_MISMATCH,
    acquire_owner_authority,
)
from workstack.store import LOCK_NAME, Store, _FileLease


OTHER_UID = "4d36e96e-e325-41ce-bfc1-08002be10318"


class _IsolatedRuntime(unittest.TestCase):
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
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR", "WORK_STACK_HOME")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore)
        from workstack.service import WorkStack

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.store.load("workspace.json")["id"]

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(self.root), *arguments])
        return code, out.getvalue(), err.getvalue()


class ParserCapabilityEquality(_IsolatedRuntime):
    def test_main_fails_closed_on_an_uncovered_command_key(self) -> None:
        with mock.patch.object(
            cli_capabilities, "command_key_from_parsed", return_value="invented.command"
        ):
            code, _out, err = self.run_cli("backlog", "list")
        self.assertEqual(code, 2)
        self.assertIn("uncovered command", err)

    def test_every_owner_mutation_has_a_writer(self) -> None:
        writers = cli_routing.owner_writer_keys()
        for item in cli_capabilities.CAPABILITIES:
            if item.online_route.startswith(("owner_post:", "owner_patch:")):
                with self.subTest(key=item.command_key):
                    self.assertIn(item.command_key, writers)


class HeldLeaseAndRelease(_IsolatedRuntime):
    def test_list_holds_the_writer_lease_then_releases_it(self) -> None:
        from workstack.service import WorkStack

        held: list[bool] = []
        original = WorkStack.list_tasks

        def wrapped(stack: WorkStack, status: str = "active"):
            held.append(stack.store._server_lease is not None)
            return original(stack, status)

        WorkStack.list_tasks = wrapped  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(WorkStack, "list_tasks", original))

        code, _out, err = self.run_cli("backlog", "list")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(held, [True])

        again = acquire_owner_authority(
            data_dir=self.root, expected_workspace_uid=self.workspace_uid
        )
        self.assertEqual(again.state, EXCLUSIVE_LOCAL_HELD)
        again.release()


class ReadyOwnerForwarding(_IsolatedRuntime):
    def test_ready_owner_note_forwards_once_without_a_second_local_write(self) -> None:
        from workstack.server import create_server

        before = json.loads((self.root / "notes.json").read_text(encoding="utf-8"))["notes"]
        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code, out, err = self.run_cli("note", "Owner routed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(payload["text"], "Owner routed")
        after = json.loads((self.root / "notes.json").read_text(encoding="utf-8"))["notes"]
        self.assertEqual(len(after), len(before) + 1)


class RefusalWithoutFallback(_IsolatedRuntime):
    def test_workspace_mismatch_pin_does_not_write(self) -> None:
        arguments = cli.parser().parse_args(
            ["--data-dir", str(self.root), "note", "Must not land"]
        )
        arguments.workspace_uid = OTHER_UID
        before = (self.root / "notes.json").read_bytes()
        with self.assertRaises(OSError):
            cli_routing.dispatch_ordinary(
                arguments,
                cli_capabilities.require_capability("note"),
                run_local=cli._execute_stack,
                owner_writer_for=cli._owner_forwarded_write,
                emit=cli.emit,
            )
        self.assertEqual((self.root / "notes.json").read_bytes(), before)
        self.assertEqual(
            acquire_owner_authority(
                data_dir=self.root, expected_workspace_uid=OTHER_UID
            ).state,
            WORKSPACE_MISMATCH,
        )

    def test_stale_metadata_with_free_lease_writes_locally(self) -> None:
        self.store.write_server_info("127.0.0.1", 9)
        code, out, err = self.run_cli("note", "Stale metadata")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(json.loads(out)["text"], "Stale metadata")
        notes = json.loads((self.root / "notes.json").read_text(encoding="utf-8"))["notes"]
        self.assertEqual(notes[-1]["text"], "Stale metadata")

    def test_unreachable_held_owner_refuses_without_local_write(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.store.write_server_info("127.0.0.1", dead_port)
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        before = (self.root / "notes.json").read_bytes()
        code, _out, err = self.run_cli("note", "Unreachable owner")
        self.assertEqual(code, 2)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual((self.root / "notes.json").read_bytes(), before)
        self.assertEqual(
            acquire_owner_authority(
                data_dir=self.root, expected_workspace_uid=self.workspace_uid
            ).state,
            OWNER_ROUTE_REQUIRED,
        )

    def test_empty_directory_read_does_not_create_a_workspace(self) -> None:
        empty = self.home / "empty"
        empty.mkdir()
        listing = {path.name: path.is_file() for path in empty.iterdir()}
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(empty), "backlog", "list"])
        self.assertEqual(code, 2)
        self.assertFalse((empty / "workspace.json").exists())
        self.assertEqual(
            {path.name: path.is_file() for path in empty.iterdir()}, listing
        )


class TwoWorkspaceSeparation(_IsolatedRuntime):
    def test_writes_in_one_workspace_do_not_appear_in_the_other(self) -> None:
        other_root = self.home / "other"
        from workstack.service import WorkStack

        other_store = Store(other_root)
        WorkStack(other_store)
        code, _out, err = self.run_cli("backlog", "add", "Only in A")
        self.assertEqual(code, 0, msg=err)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            other_code = cli.main(["--data-dir", str(other_root), "backlog", "list"])
        self.assertEqual(other_code, 0, msg=err.getvalue())
        listed = json.loads(out.getvalue())
        self.assertEqual(listed, [])
        mine = json.loads(self.run_cli("backlog", "list")[1])
        self.assertEqual([task["title"] for task in mine], ["Only in A"])


class OwnerReadRefusal(_IsolatedRuntime):
    def test_owner_held_list_refuses_local_json_when_the_owner_is_unreachable(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.store.write_server_info("127.0.0.1", dead_port)
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        before = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
            if path.is_file() and path.name.endswith(".json")
        }
        code, _out, err = self.run_cli("backlog", "list")
        self.assertEqual(code, 2)
        self.assertNotIn(cli_reads.OWNER_READ_REFUSAL, err)
        self.assertNotEqual(err.strip(), "")
        after = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
            if path.is_file() and path.name.endswith(".json")
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
