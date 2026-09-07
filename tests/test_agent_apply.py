from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workstack import cli, owner_authority
from workstack.agent_apply_admission import parse_apply_packet
from workstack.cli import apply_agent_update
from workstack.file_lease import _FileLease
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import LOCK_NAME, Store


WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
OTHER_UID = "22222222-2222-4222-8222-222222222222"


class AgentApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Agent-owned update", detail="Before")
        self.workspace_id = self.store.load("workspace.json")["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def packet(self, **changes: object) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task["id"],
            "expected_revision": 0,
            "changes": changes,
        }

    def test_applies_one_revision_guarded_update_when_server_is_offline(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = apply_agent_update(
                self.store,
                self.packet(title="Updated by agent", priority="P1"),
                "agent.update.0001",
            )

        self.assertEqual(result, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["meta"]["mode"], "exclusive-local-store")
        self.assertEqual(receipt["data"]["revision"], 1)
        self.assertEqual(self.stack.get_task(self.task["id"])["title"], "Updated by agent")

        with self.assertRaisesRegex(Exception, "revision is stale"):
            apply_agent_update(
                self.store,
                self.packet(title="Unsafe replay"),
                "agent.update.0001",
            )

    def test_forwards_through_the_running_server_instead_of_opening_store(self) -> None:
        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = apply_agent_update(
                    Store(self.root),
                    self.packet(detail="Committed through server"),
                    "agent.update.0002",
                )
            self.assertEqual(result, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["meta"]["mode"], "running-server")
            self.assertEqual(receipt["data"]["detail"], "Committed through server")
            self.assertEqual(receipt["data"]["revision"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rejects_wrong_workspace_and_unbounded_or_unknown_fields(self) -> None:
        wrong = self.packet(title="Wrong")
        wrong["workspace_id"] = "00000000-0000-4000-8000-000000000001"
        with self.assertRaisesRegex(ValueError, "does not match"):
            apply_agent_update(self.store, wrong, "agent.update.0003")
        unknown = {
            "workspace_id": self.workspace_id,
            "task_id": self.task["id"],
            "expected_revision": 0,
            "changes": {"revision": 99},
        }
        with self.assertRaisesRegex(ValueError, "supported mutable"):
            parse_apply_packet(json.dumps(unknown).encode("utf-8"))
        with self.assertRaisesRegex(ValueError, "32 KiB"):
            parse_apply_packet(b"{" + b"x" * (32 * 1024))
        huge = (
            b'{"workspace_id":"'
            + self.workspace_id.encode("ascii")
            + b'","task_id":"T-0001","expected_revision":'
            + b"1" * 5000
            + b',"changes":{"title":"X"}}'
        )
        with self.assertRaises(ValueError) as ctx:
            parse_apply_packet(huge)
        self.assertEqual(str(ctx.exception), "stdin must contain one UTF-8 JSON object")
        self.assertIsNone(ctx.exception.__cause__)
        with patch(
            "workstack.agent_apply_admission.json.loads",
            side_effect=RecursionError,
        ):
            with self.assertRaises(ValueError) as nested:
                parse_apply_packet(b'{"workspace_id":"x"}')
        self.assertEqual(str(nested.exception), "stdin must contain one UTF-8 JSON object")
        self.assertIsNone(nested.exception.__cause__)

    def _forward_requests(self, on_verify: object) -> list[str]:
        calls: list[str] = []

        def fake(
            host: str,
            port: int,
            method: str,
            path: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            calls.append("{} {}".format(method, path))
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": self.workspace_id}}
            if method == "PATCH":
                raise OSError("patch lost")
            if callable(on_verify):
                return on_verify()
            if isinstance(on_verify, BaseException):
                raise on_verify
            return on_verify  # type: ignore[return-value]

        with patch.object(cli, "_request_json", fake):
            with patch.object(cli, "_server_coordinates", return_value=("127.0.0.1", 9)):
                with self.assertRaisesRegex(OSError, "commit is unknown"):
                    apply_agent_update(
                        self.store,
                        self.packet(title="Lost"),
                        "agent.update.loss.1",
                        route="running-server",
                    )
        return calls

    def test_response_loss_get_failure_malformed_and_mismatch_stay_commit_unknown(self) -> None:
        lost = self._forward_requests(OSError("get lost"))
        self.assertEqual([item.split()[0] for item in lost].count("PATCH"), 1)
        malformed = self._forward_requests((200, {"data": {"not": "a-task"}}))
        self.assertEqual([item.split()[0] for item in malformed].count("PATCH"), 1)
        mismatch = self._forward_requests(
            (
                200,
                {
                    "data": {
                        "task": {
                            "id": self.task["id"],
                            "revision": 0,
                            "title": "Before",
                        }
                    }
                },
            )
        )
        self.assertEqual([item.split()[0] for item in mismatch].count("PATCH"), 1)
        bool_revision = self._forward_requests(
            (
                200,
                {
                    "data": {
                        "task": {
                            "id": self.task["id"],
                            "revision": True,
                            "title": "Lost",
                        }
                    }
                },
            )
        )
        self.assertEqual([item.split()[0] for item in bool_revision].count("PATCH"), 1)
        wrong_id = self._forward_requests(
            (
                200,
                {
                    "data": {
                        "task": {
                            "id": "T-9999",
                            "revision": 1,
                            "title": "Lost",
                        }
                    }
                },
            )
        )
        self.assertEqual([item.split()[0] for item in wrong_id].count("PATCH"), 1)

    def test_response_loss_accepts_only_exact_next_revision_match(self) -> None:
        calls: list[str] = []

        def fake(
            host: str,
            port: int,
            method: str,
            path: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            calls.append(method)
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": self.workspace_id}}
            if method == "PATCH":
                raise OSError("patch lost")
            return 200, {
                "data": {
                    "task": {
                        "id": self.task["id"],
                        "revision": 1,
                        "title": "Verified",
                    }
                }
            }

        output = io.StringIO()
        with patch.object(cli, "_request_json", fake):
            with patch.object(cli, "_server_coordinates", return_value=("127.0.0.1", 9)):
                with contextlib.redirect_stdout(output):
                    result = apply_agent_update(
                        self.store,
                        self.packet(title="Verified"),
                        "agent.update.loss.2",
                        route="running-server",
                    )
        self.assertEqual(result, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["meta"]["mode"], "running-server")
        self.assertTrue(receipt["meta"]["verified_after_transport_loss"])
        self.assertEqual(calls.count("PATCH"), 1)


def _tree_bytes(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return ()
    entries: list[tuple[object, ...]] = []
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "dir"))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        else:
            entries.append((relative, "other"))
    return tuple(entries)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class AgentApplyAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.data_dir = self.home / "data"
        self.runtime_dir = self.home / "runtime"
        self.env = patch.dict(
            os.environ,
            {"WORK_STACK_RUNTIME": str(self.runtime_dir)},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def invoke(self, argv: list[str], stdin: bytes) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin))):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def apply_argv(
        self,
        data_dir: Path | str,
        workspace_uid: str,
        *,
        intent_id: str = "agent.update.admit.0001",
    ) -> list[str]:
        return [
            "--data-dir",
            str(data_dir),
            "agent",
            "--workspace-uid",
            workspace_uid,
            "apply",
            "--stdin",
            "--intent-id",
            intent_id,
        ]

    def make_v3(self, data_dir: Path | None = None, uid: str = WORKSPACE_UID) -> str:
        root = data_dir or self.data_dir
        store = Store(root)
        stack = WorkStack(store)
        workspace = store.load("workspace.json")
        workspace["id"] = uid
        store.save("workspace.json", workspace)
        return stack.add_task("Agent-owned update", detail="Before")["id"]

    def packet(
        self,
        task_id: str,
        *,
        workspace_id: str = WORKSPACE_UID,
        expected_revision: object = 0,
        **changes: object,
    ) -> bytes:
        return json.dumps({
            "workspace_id": workspace_id,
            "task_id": task_id,
            "expected_revision": expected_revision,
            "changes": changes or {"title": "Updated by agent"},
        }).encode("utf-8")

    @contextlib.contextmanager
    def forbid_owner_store_and_io(self):
        with (
            patch(
                "workstack.owner_authority.Store",
                side_effect=AssertionError("Store factory"),
            ) as factory,
            patch.object(cli, "WorkStack", side_effect=AssertionError("WorkStack")),
            patch.object(cli, "_request_json", side_effect=AssertionError("HTTP")),
            patch("http.client.HTTPConnection", side_effect=AssertionError("HTTP")),
        ):
            yield factory

    def assert_refused_before_store(
        self,
        argv: list[str],
        stdin: bytes,
        needle: str,
    ) -> None:
        before = _tree_bytes(self.home)
        with self.forbid_owner_store_and_io() as factory:
            status, stdout, stderr = self.invoke(argv, stdin)
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn(needle, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("digits", stderr.lower())
        factory.assert_not_called()
        self.assertEqual(_tree_bytes(self.home), before)

    def test_public_refusals_never_instantiate_owner_store_factory(self) -> None:
        missing = self.home / "missing-authority"
        unknown = self.home / "unknown-authority"
        unknown.mkdir()
        v4_root = self.home / "v4-authority"
        v4_root.mkdir()
        _write_json(
            v4_root / "workspace.json",
            {"version": 2, "id": WORKSPACE_UID, "name": "v4 authority"},
        )
        _write_json(
            v4_root / "store.json",
            {"format": "workstack.ssot", "schema_version": 4},
        )
        self.make_v3()
        valid = self.packet("T-0001")
        bool_revision = json.dumps({
            "workspace_id": WORKSPACE_UID,
            "task_id": "T-0001",
            "expected_revision": True,
            "changes": {"title": "Nope"},
        }).encode("utf-8")
        cases = (
            (["agent", "apply", "--stdin", "--intent-id", "intent-123"], b"{}", "invalid_authority"),
            (self.apply_argv(missing, WORKSPACE_UID), valid, "invalid_authority"),
            (self.apply_argv(unknown, WORKSPACE_UID), valid, "invalid_authority"),
            (self.apply_argv(v4_root, WORKSPACE_UID), valid, "capability_not_enabled"),
            (self.apply_argv(self.data_dir, OTHER_UID), valid, "workspace_mismatch"),
            (
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                self.packet("T-0001", workspace_id=OTHER_UID),
                "workspace_mismatch",
            ),
            (self.apply_argv(self.data_dir, WORKSPACE_UID), b"{", "UTF-8 JSON object"),
            (
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                b"{" + b"x" * (32 * 1024),
                "32 KiB",
            ),
            (
                self.apply_argv(self.data_dir, WORKSPACE_UID, intent_id="short"),
                valid,
                "intent_id",
            ),
            (self.apply_argv(self.data_dir, WORKSPACE_UID), bool_revision, "expected_revision"),
            (
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                (
                    b'{"workspace_id":"'
                    + WORKSPACE_UID.encode("ascii")
                    + b'","task_id":"T-0001","expected_revision":'
                    + b"1" * 5000
                    + b',"changes":{"title":"Nope"}}'
                ),
                "UTF-8 JSON object",
            ),
        )
        for argv, stdin, needle in cases:
            with self.subTest(needle=needle, stdin_len=len(stdin)):
                self.assert_refused_before_store(argv, stdin, needle)

    def test_packet_for_b_does_not_request_or_write_b_when_admitting_a(self) -> None:
        dir_a = self.home / "auth-a"
        dir_b = self.home / "auth-b"
        task_a = self.make_v3(dir_a, WORKSPACE_UID)
        task_b = self.make_v3(dir_b, OTHER_UID)
        store_b = Store(dir_b)
        stack_b = WorkStack(store_b)
        server = create_server(stack_b, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        store_a = Store(dir_a)
        store_a.server_info_path.write_bytes(store_b.server_info_path.read_bytes())
        before_b = (dir_b / "backlog.json").read_bytes()
        blocker = _FileLease(dir_a / LOCK_NAME)
        blocker.acquire()
        try:
            with self.forbid_owner_store_and_io() as factory:
                status, stdout, stderr = self.invoke(
                    self.apply_argv(dir_a, WORKSPACE_UID),
                    self.packet(
                        task_b,
                        workspace_id=OTHER_UID,
                        title="Must not land on B",
                    ),
                )
            factory.assert_not_called()
            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertIn("workspace_mismatch", stderr)
            self.assertEqual((dir_b / "backlog.json").read_bytes(), before_b)
            self.assertEqual(stack_b.get_task(task_b)["title"], "Agent-owned update")
            self.assertEqual(task_a, "T-0001")
        finally:
            blocker.release()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_held_lease_revalidates_uid_sync_and_recovery_before_workstack(self) -> None:
        original_run = owner_authority.OwnerAuthority.run
        cases = (
            ("uid", "does not match this Store"),
            ("journal", "blocked by recovery or synchronization"),
            ("sync", "blocked by recovery or synchronization"),
        )
        for kind, needle in cases:
            with self.subTest(kind=kind):
                root = self.home / kind
                task_id = self.make_v3(root)
                backlog = (root / "backlog.json").read_bytes()

                def run_with_injection(authority, operation, inject=kind):
                    store = authority.store
                    if inject == "uid":
                        payload = json.loads(
                            (store.root / "workspace.json").read_text(encoding="utf-8")
                        )
                        payload["id"] = OTHER_UID
                        (store.root / "workspace.json").write_text(
                            json.dumps(payload),
                            encoding="utf-8",
                        )
                    elif inject == "journal":
                        (store.root / ".workstack-journal.json").write_text(
                            "{}",
                            encoding="utf-8",
                        )
                    else:
                        store.store_manifest_path.write_text("{}", encoding="utf-8")
                    with (
                        patch.object(
                            cli, "WorkStack", side_effect=AssertionError("WorkStack")
                        ),
                        patch.object(
                            Store, "initialize", side_effect=AssertionError("initialize")
                        ),
                    ):
                        return original_run(authority, operation)

                with patch.object(
                    owner_authority.OwnerAuthority, "run", run_with_injection
                ):
                    status, stdout, stderr = self.invoke(
                        self.apply_argv(root, WORKSPACE_UID),
                        self.packet(task_id, title="Must not land"),
                    )
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn(needle, stderr)
                self.assertNotIn("WorkStack", stderr)
                self.assertEqual((root / "backlog.json").read_bytes(), backlog)

    def test_owned_server_invalid_unreachable_and_uid_mismatch_never_fall_back(self) -> None:
        task_id = self.make_v3()
        workspace = (self.data_dir / "workspace.json").read_bytes()
        backlog = (self.data_dir / "backlog.json").read_bytes()
        store = Store(self.data_dir)

        store.server_info_path.write_bytes(b"not-json")
        blocker = _FileLease(self.data_dir / LOCK_NAME)
        blocker.acquire()
        try:
            status, stdout, stderr = self.invoke(
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                self.packet(task_id, title="Must not land"),
            )
        finally:
            blocker.release()
        self.assertEqual(status, 2)
        self.assertIn("runtime metadata is invalid", stderr)
        self.assertEqual((self.data_dir / "workspace.json").read_bytes(), workspace)
        self.assertEqual((self.data_dir / "backlog.json").read_bytes(), backlog)

        _write_json(
            store.server_info_path,
            {"version": 1, "host": "127.0.0.1", "port": 1},
        )
        blocker.acquire()
        try:
            status, stdout, stderr = self.invoke(
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                self.packet(task_id, title="Must not land"),
            )
        finally:
            blocker.release()
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual((self.data_dir / "backlog.json").read_bytes(), backlog)

        dir_b = self.home / "auth-b"
        self.make_v3(dir_b, OTHER_UID)
        store_b = Store(dir_b)
        stack_b = WorkStack(store_b)
        server = create_server(stack_b, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before_b = (dir_b / "backlog.json").read_bytes()
        store.server_info_path.write_bytes(store_b.server_info_path.read_bytes())
        blocker.acquire()
        try:
            status, stdout, stderr = self.invoke(
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                self.packet(task_id, title="Must not land"),
            )
        finally:
            blocker.release()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(status, 2)
        self.assertIn("does not match the running server", stderr)
        self.assertEqual((self.data_dir / "backlog.json").read_bytes(), backlog)
        self.assertEqual((dir_b / "backlog.json").read_bytes(), before_b)

    def test_admitted_offline_apply_uses_exclusive_local_store(self) -> None:
        task_id = self.make_v3()
        status, stdout, stderr = self.invoke(
            self.apply_argv(self.data_dir, WORKSPACE_UID),
            self.packet(task_id, title="Updated by agent", priority="P1"),
        )
        self.assertEqual(status, 0, stderr)
        receipt = json.loads(stdout)
        self.assertEqual(receipt["meta"]["mode"], "exclusive-local-store")
        self.assertEqual(receipt["data"]["title"], "Updated by agent")
        self.assertEqual(receipt["data"]["revision"], 1)

    def test_admitted_running_owner_forwards_instead_of_local_write(self) -> None:
        task_id = self.make_v3()
        stack = WorkStack(Store(self.data_dir))
        server = create_server(stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, stdout, stderr = self.invoke(
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                self.packet(task_id, detail="Committed through server"),
            )
            self.assertEqual(status, 0, stderr)
            receipt = json.loads(stdout)
            self.assertEqual(receipt["meta"]["mode"], "running-server")
            self.assertEqual(receipt["data"]["detail"], "Committed through server")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_unavailable_owner_does_not_fall_back_or_mutate(self) -> None:
        task_id = self.make_v3()
        workspace = (self.data_dir / "workspace.json").read_bytes()
        backlog = (self.data_dir / "backlog.json").read_bytes()
        blocker = _FileLease(self.data_dir / LOCK_NAME)
        blocker.acquire()
        try:
            status, stdout, stderr = self.invoke(
                self.apply_argv(self.data_dir, WORKSPACE_UID),
                self.packet(task_id, title="Must not land"),
            )
        finally:
            blocker.release()
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("writer authority is unavailable", stderr)
        self.assertEqual((self.data_dir / "workspace.json").read_bytes(), workspace)
        self.assertEqual((self.data_dir / "backlog.json").read_bytes(), backlog)


if __name__ == "__main__":
    unittest.main()
