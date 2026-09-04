"""Black-box CLI E2E contract for P0 agent status, context, and checkpoint."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import Mock, patch

from workstack import cli
from workstack.agent_authority import admit_authority
from workstack.agent_cli_contract import (
    CHECKPOINT_COMMAND,
    CONTEXT_COMMAND,
    STATUS_COMMAND,
    AuthorityAdmission,
    RuntimeDependencies,
    parse_checkpoint_packet,
)
from workstack.agent_commands import COMMANDS
from workstack.agent_local_backend import create_local_backend
from workstack.agent_runtime import run_agent_command
from workstack.agent_transport import create_running_server_backend
from workstack.service import WorkStack
from workstack.store import Store


CONTRACT = "workstack.cli.v1"
WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
OTHER_UID = "22222222-2222-4222-8222-222222222222"
INTENT_ID = "agent:te:checkpoint-0001"
TODAY = datetime.date(2026, 9, 2)
HOST = "127.0.0.1"
PORT = 8765
CSRF = "csrf-canary-value"
SECRET_TASK = "TOP-SECRET-WRONG-WORKSPACE-TASK"
CANARIES = (CSRF, "token-canary-value", "cookie-canary-value", "raw-body-canary")
SESSION = (200, {"data": {"csrf_token": CSRF}})
STORAGE = (
    200,
    {"data": {"store_schema_version": 3, "workspace_id": WORKSPACE_UID}},
)
SYNC = (200, {"data": {"state": "in-sync"}})
NEW_COMMANDS = (STATUS_COMMAND, CONTEXT_COMMAND, CHECKPOINT_COMMAND)
Response = tuple[int, dict[str, object]]
Step = Response | BaseException | Callable[[dict[str, object]], Response]


class RecordingRequester:
    """Strict JsonRequester fake used only at the public HTTP seam."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
    ) -> Response:
        call: dict[str, object] = {
            "body": body,
            "headers": headers,
            "host": host,
            "method": method,
            "path": path,
            "port": port,
        }
        self.calls.append(call)
        if not self.steps:
            raise AssertionError("unexpected request: {!r}".format(call))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(call)
        return step


class RecordingBackend:
    """Records exactly which public backend operations were selected."""

    def __init__(
        self,
        *,
        status: dict[str, object] | None = None,
        context: dict[str, object] | None = None,
        checkpoint: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.status_result = status
        self.context_result = context
        self.checkpoint_result = checkpoint
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def status(self, *, request) -> dict[str, object]:
        self.calls.append(("status", request))
        if self.error is not None:
            raise self.error
        if self.status_result is None:
            raise AssertionError("status was not expected")
        return self.status_result

    def context(self, *, request, today) -> dict[str, object]:
        self.calls.append(("context", (request, today)))
        if self.error is not None:
            raise self.error
        if self.context_result is None:
            raise AssertionError("context was not expected")
        return self.context_result

    def checkpoint(self, *, request) -> dict[str, object]:
        self.calls.append(("checkpoint", request))
        if self.error is not None:
            raise self.error
        if self.checkpoint_result is None:
            raise AssertionError("checkpoint was not expected")
        return self.checkpoint_result


class TraceStore(Store):
    """Public Store with observable construction and transaction bounds."""

    def __init__(self, root: Path, *, events: list[tuple[object, ...]]) -> None:
        events.append(("store-construct", Path(root)))
        self.trace_events = events
        super().__init__(root)

    @contextlib.contextmanager
    def transaction(self):
        self.trace_events.append(("transaction-enter", Path(self.root)))
        with super().transaction():
            yield
        self.trace_events.append(("transaction-exit", Path(self.root)))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _checkpoint_bytes(
    *,
    task_id: str = "T-0001",
    done: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> bytes:
    packet: dict[str, object] = {
        "blockers": [],
        "date": "2026-09-02",
        "done": ["implemented the e2e seam"] if done is None else done,
        "next": ["compose I1 I2 I3"],
        "task_id": task_id,
    }
    if extra:
        packet.update(extra)
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _status_payload() -> dict[str, object]:
    return {
        "actual_workspace_uid": WORKSPACE_UID,
        "capability_reason": None,
        "capability_supported": True,
        "contract": CONTRACT,
        "data_dir_available": True,
        "exclusive_local_available": False,
        "expected_workspace_uid": WORKSPACE_UID,
        "ready": True,
        "running_server_available": True,
        "storage_format": "v3",
    }


def _context_payload(*, task_id: str) -> dict[str, object]:
    return {
        "entries": [
            {
                "blockers": [],
                "date": "2026-09-02",
                "done": ["reviewed"],
                "next": ["execute"],
                "task_id": task_id,
            }
        ],
        "task": {
            "detail": "Execute the next bounded step",
            "due": None,
            "id": task_id,
            "priority": "P1",
            "revision": 1,
            "status": "started",
            "title": "Ship agent E2E",
            "uid": "33333333-3333-4333-8333-333333333333",
        },
        "transport": "running-server",
        "workspace_uid": WORKSPACE_UID,
    }


def _checkpoint_payload(*, task_id: str, replayed: bool = False) -> dict[str, object]:
    return {
        "commit_state": "committed",
        "entry": {
            "blockers": [],
            "date": "2026-09-02",
            "done": ["implemented the e2e seam"],
            "next": ["compose I1 I2 I3"],
            "task": "Ship agent E2E",
            "task_id": task_id,
        },
        "replayed": replayed,
        "transport": "running-server",
        "workspace_uid": WORKSPACE_UID,
    }


def _http_entry(*, task_id: str, replayed: bool, status: int = 201) -> Response:
    return (
        status,
        {
            "data": {
                "blockers": [],
                "date": "2026-09-02",
                "done": ["implemented the e2e seam"],
                "next": ["compose I1 I2 I3"],
                "task": "Ship agent E2E",
                "task_id": task_id,
            },
            "meta": {"replayed": replayed},
        },
    )


def _agent_subcommands() -> set[str]:
    root = cli.parser()
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction) and "agent" in action.choices:
            agent = action.choices["agent"]
            for sub in agent._actions:
                if isinstance(sub, argparse._SubParsersAction):
                    return set(sub.choices)
    raise AssertionError("agent subcommands are not registered on the public parser")


def _looks_like_envelope(stdout: str) -> bool:
    text = stdout.strip()
    if not text:
        return False
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and "contract" in value


def load_envelope(stdout: str) -> dict[str, object]:
    raw = stdout.encode("utf-8")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise AssertionError("stdout is not one JSON object plus LF: {!r}".format(stdout))
    value = json.loads(stdout)
    if type(value) is not dict or value.get("contract") != CONTRACT:
        raise AssertionError("stdout is not one parseable agent envelope")
    return value


def command_args(
    action: str,
    *,
    data_dir: Path | str | None,
    workspace_uid: str | None,
    task: str | None = None,
    intent_id: str | None = None,
    checkpoint_raw: bytes | None = None,
) -> Namespace:
    values: dict[str, object] = {
        "action": action,
        "data_dir": None if data_dir is None else str(data_dir),
        "workspace_uid": workspace_uid,
    }
    if action == CONTEXT_COMMAND:
        values["task"] = task
    if action == CHECKPOINT_COMMAND:
        values["intent_id"] = intent_id
        values["checkpoint_raw"] = checkpoint_raw
    return Namespace(**values)


class IsolatedAuthorityTest(unittest.TestCase):
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
        self.events: list[tuple[object, ...]] = []
        self.local_calls: list[AuthorityAdmission] = []
        self.online_calls: list[Path] = []

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def make_v3(self, *, uid: str = WORKSPACE_UID, title: str = "Ship agent E2E") -> str:
        store = Store(self.data_dir)
        stack = WorkStack(store)
        workspace = store.load("workspace.json")
        workspace["id"] = uid
        store.save("workspace.json", workspace)
        task = stack.add_task(title, detail="bounded E2E task")
        return task["id"]

    def make_v4(self) -> None:
        self.data_dir.mkdir(parents=True)
        _write_json(
            self.data_dir / "workspace.json",
            {"version": 2, "id": WORKSPACE_UID, "name": "v4 authority"},
        )
        _write_json(
            self.data_dir / "store.json",
            {"format": "workstack.ssot", "schema_version": 4},
        )

    def write_owner(self, store: Store | None = None) -> Store:
        owner = store if store is not None else Store(self.data_dir)
        owner.write_server_info(HOST, PORT)
        return owner

    def store_factory(self, *, root: Path) -> Store:
        return TraceStore(root, events=self.events)

    def forbidden_store(self, *, root: Path) -> Store:
        self.events.append(("store-construct", Path(root)))
        raise AssertionError("Store construction is forbidden before preflight")

    def admit(
        self,
        *,
        data_dir: Path,
        expected_workspace_uid: str,
    ) -> AuthorityAdmission:
        self.events.append(("admit", Path(data_dir), expected_workspace_uid))
        return admit_authority(
            data_dir=data_dir,
            expected_workspace_uid=expected_workspace_uid,
        )

    def create_local(
        self,
        *,
        admission: AuthorityAdmission,
        store_factory,
    ):
        self.local_calls.append(admission)
        self.events.append(("local-backend", Path(admission.data_dir)))
        return create_local_backend(
            admission=admission,
            store_factory=store_factory,
        )

    def forbidden_local(self, *, admission: AuthorityAdmission, store_factory):
        self.local_calls.append(admission)
        self.events.append(("local-backend", Path(admission.data_dir)))
        raise AssertionError("local backend must not be constructed")

    def create_online(
        self,
        *,
        server_info_path: Path,
        expected_workspace_uid: str,
        request_json,
    ):
        self.online_calls.append(Path(server_info_path))
        self.events.append(("online-backend", Path(server_info_path)))
        return create_running_server_backend(
            server_info_path=server_info_path,
            expected_workspace_uid=expected_workspace_uid,
            request_json=request_json,
        )

    def fake_online(
        self,
        backend: RecordingBackend,
        *,
        server_info_path: Path,
        expected_workspace_uid: str,
        request_json,
    ):
        self.online_calls.append(Path(server_info_path))
        self.events.append(("online-backend", Path(server_info_path)))
        self.assertEqual(expected_workspace_uid, WORKSPACE_UID)
        self.assertIsNotNone(request_json)
        return backend

    def fake_local(
        self,
        backend: RecordingBackend,
        *,
        admission: AuthorityAdmission,
        store_factory,
    ):
        self.local_calls.append(admission)
        self.events.append(("local-backend", Path(admission.data_dir)))
        store_factory(root=admission.data_dir)
        return backend

    def dependencies(
        self,
        *,
        request_json=None,
        store_factory=None,
        create_local_backend=None,
        create_running_server_backend=None,
        admit_authority=None,
    ) -> RuntimeDependencies:
        return RuntimeDependencies(
            admit_authority=self.admit if admit_authority is None else admit_authority,
            create_local_backend=(
                self.create_local if create_local_backend is None else create_local_backend
            ),
            create_running_server_backend=(
                self.create_online
                if create_running_server_backend is None
                else create_running_server_backend
            ),
            request_json=RecordingRequester() if request_json is None else request_json,
            store_factory=self.store_factory if store_factory is None else store_factory,
            today=lambda: TODAY,
        )

    def run_command(
        self,
        action: str,
        *,
        dependencies: RuntimeDependencies,
        data_dir: Path | str | None = None,
        workspace_uid: str | None = WORKSPACE_UID,
        task: str | None = None,
        intent_id: str | None = None,
        checkpoint_raw: bytes | None = None,
        stdin: bytes = b"",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = command_args(
            action,
            data_dir=self.data_dir if data_dir is None else data_dir,
            workspace_uid=workspace_uid,
            task=task,
            intent_id=intent_id,
            checkpoint_raw=checkpoint_raw,
        )
        with patch.object(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin))):
            code = run_agent_command(
                args=args,
                stdout=stdout,
                stderr=stderr,
                dependencies=dependencies,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_main(self, argv: list[str], *, stdin: bytes = b"") -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin))):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = cli.main(argv)
                except SystemExit as error:
                    code = error.code if isinstance(error.code, int) else 2
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_no_canaries(self, *texts: str) -> None:
        rendered = "\n".join(texts)
        for canary in CANARIES + (SECRET_TASK, str(self.home)):
            self.assertNotIn(canary, rendered)

    def assert_failure(
        self,
        code: int,
        stdout: str,
        stderr: str,
        *,
        error_code: str,
        command: str,
    ) -> dict[str, object]:
        self.assertEqual(code, 1)
        envelope = load_envelope(stdout)
        self.assertNotIn("data", envelope)
        error = envelope["error"]
        self.assertEqual(error["code"], error_code)
        self.assertEqual(error["details"], {})
        self.assertIsInstance(error["message"], str)
        self.assertEqual(envelope["meta"]["command"], command)
        self.assert_no_canaries(stdout, stderr)
        return envelope

    def assert_success(
        self,
        code: int,
        stdout: str,
        stderr: str,
        *,
        command: str,
        transport: str,
    ) -> dict[str, object]:
        self.assertEqual(code, 0)
        envelope = load_envelope(stdout)
        self.assertNotIn("error", envelope)
        self.assertEqual(envelope["meta"]["command"], command)
        self.assertEqual(envelope["meta"]["transport"], transport)
        self.assertEqual(envelope["meta"]["workspace_uid"], WORKSPACE_UID)
        self.assert_no_canaries(stdout, stderr)
        return envelope


class AgentParserContractTests(IsolatedAuthorityTest):
    def test_parser_help_exposes_exactly_three_new_commands_and_legacy_apply(self) -> None:
        self.assertEqual(tuple(COMMANDS), NEW_COMMANDS)
        names = _agent_subcommands()
        self.assertEqual(names, {"apply", *NEW_COMMANDS})
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text):
            with self.assertRaises(SystemExit) as raised:
                cli.parser().parse_args(["agent", "-h"])
        self.assertEqual(raised.exception.code, 0)
        rendered = help_text.getvalue()
        for name in names:
            self.assertRegex(rendered, r"\b{}\b".format(name))
        apply_ns = cli.parser().parse_args(
            ["agent", "apply", "--stdin", "--intent-id", "intent-123"]
        )
        self.assertEqual(apply_ns.domain, "agent")
        self.assertEqual(apply_ns.action, "apply")
        self.assertTrue(apply_ns.stdin)
        self.assertEqual(apply_ns.intent_id, "intent-123")
        with self.assertRaises(SystemExit) as missing:
            cli.parser().parse_args(["agent", "apply"])
        self.assertEqual(missing.exception.code, 2)
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(
                [
                    "--data-dir",
                    str(self.data_dir),
                    "agent",
                    "--workspace-uid",
                    WORKSPACE_UID,
                    "bind",
                ]
            )

    def test_new_commands_require_data_dir_workspace_uid_and_command_flags(self) -> None:
        data = str(self.data_dir)
        status = [
            "--data-dir",
            data,
            "agent",
            "--workspace-uid",
            WORKSPACE_UID,
            "status",
        ]
        context = status[:-1] + ["context", "--task", "T-0001"]
        checkpoint = status[:-1] + [
            "checkpoint",
            "--intent-id",
            INTENT_ID,
            "--stdin",
        ]
        parsed_status = cli.parser().parse_args(status)
        parsed_context = cli.parser().parse_args(context)
        parsed_checkpoint = cli.parser().parse_args(checkpoint)
        for parsed, action in (
            (parsed_status, STATUS_COMMAND),
            (parsed_context, CONTEXT_COMMAND),
            (parsed_checkpoint, CHECKPOINT_COMMAND),
        ):
            self.assertEqual(parsed.action, action)
            self.assertEqual(parsed.data_dir, data)
            self.assertEqual(parsed.workspace_uid, WORKSPACE_UID)
        self.assertEqual(parsed_context.task, "T-0001")
        self.assertEqual(parsed_checkpoint.intent_id, INTENT_ID)
        self.assertTrue(parsed_checkpoint.stdin)

        runtime_failures = (
            ["agent", "--workspace-uid", WORKSPACE_UID, "status"],
            ["--data-dir", data, "agent", "status"],
        )
        for argv in runtime_failures:
            code, stdout, stderr = self.invoke_main(argv)
            self.assert_failure(
                code,
                stdout,
                stderr,
                error_code="invalid_authority",
                command="agent.status",
            )

        parser_failures = (
            ["--data-dir", data, "agent", "--workspace-uid", WORKSPACE_UID, "context"],
            [
                "--data-dir",
                data,
                "agent",
                "--workspace-uid",
                WORKSPACE_UID,
                "checkpoint",
                "--stdin",
            ],
            [
                "--data-dir",
                data,
                "agent",
                "--workspace-uid",
                WORKSPACE_UID,
                "checkpoint",
                "--intent-id",
                INTENT_ID,
            ],
        )
        for argv in parser_failures:
            code, stdout, stderr = self.invoke_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertFalse(_looks_like_envelope(stdout), stdout)
            self.assertTrue(stderr or stdout)


class AgentDispatchContractTests(IsolatedAuthorityTest):
    def test_new_command_dispatch_precedes_legacy_default_store_construction(self) -> None:
        task_id = self.make_v3()
        argv_cases = (
            [
                "--data-dir",
                str(self.data_dir),
                "agent",
                "--workspace-uid",
                WORKSPACE_UID,
                "status",
            ],
            [
                "--data-dir",
                str(self.data_dir),
                "agent",
                "--workspace-uid",
                WORKSPACE_UID,
                "context",
                "--task",
                task_id,
            ],
            [
                "--data-dir",
                str(self.data_dir),
                "agent",
                "--workspace-uid",
                WORKSPACE_UID,
                "checkpoint",
                "--intent-id",
                INTENT_ID,
                "--stdin",
            ],
        )
        packets = {
            CHECKPOINT_COMMAND: _checkpoint_bytes(task_id=task_id),
        }
        for argv in argv_cases:
            action = "checkpoint" if "checkpoint" in argv else argv[-1]
            with self.subTest(action=action):
                with patch.object(
                    cli,
                    "Store",
                    side_effect=AssertionError("legacy default Store must not run"),
                ) as legacy_store:
                    code, stdout, stderr = self.invoke_main(
                        argv,
                        stdin=packets.get(action, b""),
                    )
                legacy_store.assert_not_called()
                self.assertIn(code, {0, 1})
                load_envelope(stdout)
                self.assert_no_canaries(stdout, stderr)

    def test_existing_agent_apply_stays_on_characterized_legacy_path(self) -> None:
        store = Mock()
        with (
            patch.object(cli, "Store", return_value=store) as store_type,
            patch.object(cli, "apply_agent_update", return_value=8) as apply_update,
        ):
            code, stdout, stderr = self.invoke_main(
                ["agent", "apply", "--stdin", "--intent-id", "intent-123"],
                stdin=b"agent",
            )
        self.assertEqual(code, 8)
        store_type.assert_called_once()
        apply_update.assert_called_once_with(store, b"agent", "intent-123")
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")


class AgentAuthorityRefusalTests(IsolatedAuthorityTest):
    def test_missing_v4_and_uid_mismatch_fail_before_store_task_and_mutation(self) -> None:
        missing = self.home / "missing authority"
        v4_root = self.home / "v4 authority"
        mismatch_root = self.home / "mismatch authority"
        cases = (
            (
                "missing",
                missing,
                WORKSPACE_UID,
                "invalid_authority",
                STATUS_COMMAND,
                None,
            ),
            (
                "v4",
                v4_root,
                WORKSPACE_UID,
                "capability_not_enabled",
                CONTEXT_COMMAND,
                "T-0001",
            ),
            (
                "uid-mismatch",
                mismatch_root,
                OTHER_UID,
                "workspace_mismatch",
                CHECKPOINT_COMMAND,
                None,
            ),
        )
        for name, data_dir, uid, error_code, action, task in cases:
            with self.subTest(name=name):
                self.events.clear()
                self.local_calls.clear()
                self.online_calls.clear()
                if name == "v4":
                    self.data_dir = data_dir
                    self.make_v4()
                elif name == "uid-mismatch":
                    self.data_dir = data_dir
                    self.make_v3(title=SECRET_TASK)
                before = tuple(sorted(p.as_posix() for p in self.home.rglob("*")))
                code, stdout, stderr = self.run_command(
                    action,
                    dependencies=self.dependencies(store_factory=self.forbidden_store),
                    data_dir=data_dir,
                    workspace_uid=uid,
                    task=task,
                    intent_id=INTENT_ID if action == CHECKPOINT_COMMAND else None,
                    checkpoint_raw=(
                        _checkpoint_bytes() if action == CHECKPOINT_COMMAND else None
                    ),
                )
                after = tuple(sorted(p.as_posix() for p in self.home.rglob("*")))
                self.assertEqual(before, after)
                envelope = self.assert_failure(
                    code,
                    stdout,
                    stderr,
                    error_code=error_code,
                    command="agent.{}".format(action),
                )
                self.assertNotIn("commit_state", envelope["meta"])
                self.assertEqual(
                    [event[0] for event in self.events],
                    ["admit"],
                )
                self.assertEqual(self.local_calls, [])
                self.assertEqual(self.online_calls, [])
                self.assertNotIn(SECRET_TASK, stdout)
                self.assertNotIn(SECRET_TASK, stderr)


class AgentBackendSelectionTests(IsolatedAuthorityTest):
    def test_live_owner_selects_http_without_store_transaction(self) -> None:
        task_id = self.make_v3()
        self.write_owner()
        requester = RecordingRequester(SESSION, STORAGE)
        backend = RecordingBackend(
            status=_status_payload(),
            context=_context_payload(task_id=task_id),
            checkpoint=_checkpoint_payload(task_id=task_id),
        )
        dependencies = self.dependencies(
            request_json=requester,
            create_local_backend=self.forbidden_local,
            create_running_server_backend=lambda **kwargs: self.fake_online(
                backend, **kwargs
            ),
        )
        for action, kwargs in (
            (STATUS_COMMAND, {}),
            (CONTEXT_COMMAND, {"task": task_id}),
            (
                CHECKPOINT_COMMAND,
                {
                    "intent_id": INTENT_ID,
                    "checkpoint_raw": _checkpoint_bytes(task_id=task_id),
                },
            ),
        ):
            with self.subTest(action=action):
                self.events.clear()
                backend.calls.clear()
                code, stdout, stderr = self.run_command(
                    action,
                    dependencies=dependencies,
                    **kwargs,
                )
                envelope = self.assert_success(
                    code,
                    stdout,
                    stderr,
                    command="agent.{}".format(action),
                    transport="running-server",
                )
                self.assertEqual([call[0] for call in backend.calls], [action])
                self.assertIn(("online-backend", Store(self.data_dir).server_info_path), self.events)
                self.assertTrue(any(event[0] == "store-construct" for event in self.events))
                self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
                self.assertFalse(any(event[0] == "local-backend" for event in self.events))
                self.assertIn("data", envelope)

        http = RecordingRequester(SESSION, STORAGE, SYNC)
        http_dependencies = self.dependencies(
            request_json=http,
            create_local_backend=self.forbidden_local,
        )
        self.events.clear()
        code, stdout, stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=http_dependencies,
        )
        self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.status",
            transport="running-server",
        )
        self.assertEqual(
            [call["path"] for call in http.calls],
            ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
        )
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
        self.assertEqual(self.local_calls, [])

    def test_absent_owner_uses_exclusive_local_and_ends_in_existing_transaction(self) -> None:
        task_id = self.make_v3()
        self.assertFalse(Store(self.data_dir).server_info_path.is_file())
        dependencies = self.dependencies(
            create_running_server_backend=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("running-server backend must not be selected")
            ),
        )
        for action, kwargs in (
            (STATUS_COMMAND, {}),
            (CONTEXT_COMMAND, {"task": task_id}),
            (
                CHECKPOINT_COMMAND,
                {
                    "intent_id": INTENT_ID,
                    "checkpoint_raw": _checkpoint_bytes(task_id=task_id),
                },
            ),
        ):
            with self.subTest(action=action):
                self.events.clear()
                self.local_calls.clear()
                code, stdout, stderr = self.run_command(
                    action,
                    dependencies=dependencies,
                    **kwargs,
                )
                envelope = self.assert_success(
                    code,
                    stdout,
                    stderr,
                    command="agent.{}".format(action),
                    transport="exclusive-local",
                )
                self.assertTrue(any(event[0] == "store-construct" for event in self.events))
                self.assertIn(("transaction-enter", self.data_dir.resolve()), self.events)
                self.assertIn(("transaction-exit", self.data_dir.resolve()), self.events)
                self.assertEqual(len(self.local_calls), 1)
                self.assertFalse(any(event[0] == "online-backend" for event in self.events))
                self.assertIn("data", envelope)

    def test_dead_owner_fails_without_local_backend_or_fallback(self) -> None:
        self.make_v3()
        self.write_owner()
        requester = RecordingRequester(OSError("connection refused"))
        dependencies = self.dependencies(
            request_json=requester,
            create_local_backend=self.forbidden_local,
        )
        code, stdout, stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=dependencies,
        )
        envelope = self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="owner_unavailable",
            command="agent.status",
        )
        self.assertNotIn("commit_state", envelope["meta"])
        self.assertTrue(any(event[0] == "online-backend" for event in self.events))
        self.assertFalse(any(event[0] == "local-backend" for event in self.events))
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
        self.assertEqual(len(requester.calls), 1)

        self.events.clear()
        invalid = Store(self.data_dir)
        invalid.server_info_path.write_text("{}", encoding="utf-8")
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                request_json=RecordingRequester(),
                create_local_backend=self.forbidden_local,
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(),
        )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="owner_unavailable",
            command="agent.checkpoint",
        )
        self.assertEqual(self.local_calls, [])
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))


class AgentCommandEnvelopeTests(IsolatedAuthorityTest):
    def test_canonical_stdout_uses_binary_buffer_and_textio_fallback(self) -> None:
        self.make_v3()
        dependencies = self.dependencies()
        args = command_args(
            STATUS_COMMAND,
            data_dir=self.data_dir,
            workspace_uid=WORKSPACE_UID,
        )

        raw_stdout = io.BytesIO()
        windows_text_stdout = io.TextIOWrapper(
            raw_stdout,
            encoding="utf-8",
            newline="\r\n",
        )
        binary_stderr = io.StringIO()
        binary_code = run_agent_command(
            args=args,
            stdout=windows_text_stdout,
            stderr=binary_stderr,
            dependencies=dependencies,
        )
        windows_text_stdout.flush()
        rendered = raw_stdout.getvalue()
        self.assertEqual(binary_code, 0)
        self.assertTrue(rendered.endswith(b"\n"))
        self.assertNotIn(b"\r", rendered)
        self.assertEqual(rendered.count(b"\n"), 1)
        self.assertEqual(binary_stderr.getvalue(), "")
        self.assertEqual(
            json.loads(rendered), load_envelope(rendered.decode("utf-8"))
        )

        text_code, text_stdout, text_stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=dependencies,
        )
        self.assertEqual(text_code, 0)
        self.assertTrue(text_stdout.endswith("\n"))
        self.assertNotIn("\r", text_stdout)
        self.assertEqual(text_stdout.count("\n"), 1)
        self.assertEqual(text_stderr, "")
        load_envelope(text_stdout)

    def test_status_context_and_checkpoint_each_invoke_one_path_and_emit_one_envelope(self) -> None:
        task_id = "T-0001"
        backend = RecordingBackend(
            status=_status_payload(),
            context=_context_payload(task_id=task_id),
            checkpoint=_checkpoint_payload(task_id=task_id),
        )
        self.make_v3()
        self.write_owner()
        dependencies = self.dependencies(
            create_local_backend=self.forbidden_local,
            create_running_server_backend=lambda **kwargs: self.fake_online(
                backend, **kwargs
            ),
        )
        cases = (
            (STATUS_COMMAND, {}, "agent.status", {"workspace_uid", "transport", "command"}),
            (
                CONTEXT_COMMAND,
                {"task": task_id},
                "agent.context",
                {"workspace_uid", "transport", "command", "task_id"},
            ),
            (
                CHECKPOINT_COMMAND,
                {
                    "intent_id": INTENT_ID,
                    "checkpoint_raw": _checkpoint_bytes(task_id=task_id),
                },
                "agent.checkpoint",
                {
                    "workspace_uid",
                    "transport",
                    "command",
                    "task_id",
                    "intent_id",
                    "replayed",
                    "commit_state",
                },
            ),
        )
        for action, kwargs, command, meta_keys in cases:
            with self.subTest(action=action):
                backend.calls.clear()
                code, stdout, stderr = self.run_command(
                    action,
                    dependencies=dependencies,
                    **kwargs,
                )
                envelope = self.assert_success(
                    code,
                    stdout,
                    stderr,
                    command=command,
                    transport="running-server",
                )
                self.assertEqual([call[0] for call in backend.calls], [action])
                self.assertEqual(set(envelope["meta"]), meta_keys)
                self.assertIsInstance(envelope["data"], dict)
                self.assertEqual(stderr, "")


class AgentCheckpointSemanticsTests(IsolatedAuthorityTest):
    def test_checkpoint_preserves_exact_body_and_intent_and_rejects_foreign_uid(self) -> None:
        task_id = self.make_v3()
        self.write_owner()
        captured: list[object] = []

        class CapturingBackend(RecordingBackend):
            def checkpoint(inner_self, *, request):
                captured.append(request)
                return super().checkpoint(request=request)

        backend = CapturingBackend(checkpoint=_checkpoint_payload(task_id=task_id))
        raw = _checkpoint_bytes(task_id=task_id)
        expected = parse_checkpoint_packet(raw=raw, intent_id=INTENT_ID)
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                create_local_backend=self.forbidden_local,
                create_running_server_backend=lambda **kwargs: self.fake_online(
                    backend, **kwargs
                ),
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=raw,
        )
        envelope = self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.checkpoint",
            transport="running-server",
        )
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.task_id, expected.task_id)
        self.assertEqual(request.date, expected.date)
        self.assertEqual(request.done, expected.done)
        self.assertEqual(request.next, expected.next)
        self.assertEqual(request.blockers, expected.blockers)
        self.assertEqual(request.intent_id, INTENT_ID)
        self.assertEqual(envelope["meta"]["intent_id"], INTENT_ID)
        self.assertEqual(envelope["data"]["task_id"], task_id)
        self.assertNotIn("workspace_uid", envelope["data"])

        backend.calls.clear()
        captured.clear()
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                create_local_backend=self.forbidden_local,
                create_running_server_backend=lambda **kwargs: self.fake_online(
                    backend, **kwargs
                ),
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(
                task_id=task_id,
                extra={"workspace_uid": WORKSPACE_UID},
            ),
        )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="invalid_body",
            command="agent.checkpoint",
        )
        self.assertEqual(backend.calls, [])
        self.assertEqual(captured, [])

    def test_same_key_replay_across_restart_creates_one_worklog_entry(self) -> None:
        task_id = self.make_v3()
        raw = _checkpoint_bytes(task_id=task_id)
        dependencies = self.dependencies(
            create_running_server_backend=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("local replay must not use the online backend")
            ),
        )
        first = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=dependencies,
            intent_id=INTENT_ID,
            checkpoint_raw=raw,
        )
        replay = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=dependencies,
            intent_id=INTENT_ID,
            checkpoint_raw=raw,
        )
        first_env = self.assert_success(
            *first,
            command="agent.checkpoint",
            transport="exclusive-local",
        )
        replay_env = self.assert_success(
            *replay,
            command="agent.checkpoint",
            transport="exclusive-local",
        )
        self.assertIs(first_env["meta"]["replayed"], False)
        self.assertIs(replay_env["meta"]["replayed"], True)
        self.assertEqual(first_env["data"], replay_env["data"])

        reopened = Store(self.data_dir)
        restarted = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                store_factory=lambda *, root: TraceStore(root, events=self.events),
                create_running_server_backend=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("restarted local replay used online backend")
                ),
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=raw,
        )
        restarted_env = self.assert_success(
            *restarted,
            command="agent.checkpoint",
            transport="exclusive-local",
        )
        self.assertIs(restarted_env["meta"]["replayed"], True)
        days = reopened.load("worklog.json")["days"]
        matching = [
            entry
            for entry in days["2026-09-02"]["entries"]
            if entry["task_id"] == task_id
        ]
        self.assertEqual(len(matching), 1)

    def test_same_key_different_body_conflicts(self) -> None:
        task_id = self.make_v3()
        dependencies = self.dependencies(
            create_running_server_backend=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("conflict path must stay exclusive-local")
            ),
        )
        first = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=dependencies,
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id),
        )
        self.assert_success(
            *first,
            command="agent.checkpoint",
            transport="exclusive-local",
        )
        conflict = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=dependencies,
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id, done=["a different body"]),
        )
        self.assertNotEqual(conflict[0], 0)
        if _looks_like_envelope(conflict[1]):
            envelope = load_envelope(conflict[1])
            self.assertNotIn("data", envelope)
            self.assertNotEqual(envelope.get("error", {}).get("code"), "commit_unknown")
            self.assert_no_canaries(conflict[1], conflict[2])
        days = Store(self.data_dir).load("worklog.json")["days"]
        matching = [
            entry
            for entry in days["2026-09-02"]["entries"]
            if entry["task_id"] == task_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["done"], ["implemented the e2e seam"])

    def test_lost_response_follows_bounded_identical_replay_state_machine(self) -> None:
        task_id = self.make_v3()
        self.write_owner()
        lost_after_commit = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("response lost after commit"),
            _http_entry(task_id=task_id, replayed=True, status=200),
        )
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                request_json=lost_after_commit,
                create_local_backend=self.forbidden_local,
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id),
        )
        envelope = self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.checkpoint",
            transport="running-server",
        )
        posts = [call for call in lost_after_commit.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertEqual(posts[0]["headers"], posts[1]["headers"])
        self.assertEqual(posts[0]["headers"]["Idempotency-Key"], INTENT_ID)
        self.assertNotIn(b"workspace_uid", posts[0]["body"])
        self.assertIs(envelope["meta"]["replayed"], True)
        self.assertEqual(envelope["meta"]["commit_state"], "committed")
        self.assertFalse(any(event[0] == "local-backend" for event in self.events))

        lost_before_commit = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("request lost before server"),
            _http_entry(task_id=task_id, replayed=False),
        )
        self.events.clear()
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                request_json=lost_before_commit,
                create_local_backend=self.forbidden_local,
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id),
        )
        envelope = self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.checkpoint",
            transport="running-server",
        )
        posts = [call for call in lost_before_commit.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertIs(envelope["meta"]["replayed"], False)

        unverifiable = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("first response lost"),
            TimeoutError("replay response lost"),
        )
        self.events.clear()
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                request_json=unverifiable,
                create_local_backend=self.forbidden_local,
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id),
        )
        envelope = self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="commit_unknown",
            command="agent.checkpoint",
        )
        self.assertEqual(envelope["meta"]["commit_state"], "unknown")
        self.assertEqual(envelope["meta"]["transport"], "running-server")
        self.assertEqual(envelope["meta"]["intent_id"], INTENT_ID)
        self.assertEqual(envelope["meta"]["task_id"], task_id)
        posts = [call for call in unverifiable.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertEqual(
            posts[0]["headers"]["Idempotency-Key"],
            posts[1]["headers"]["Idempotency-Key"],
        )
        self.assertFalse(any(event[0] == "local-backend" for event in self.events))
        self.assertEqual(len(unverifiable.calls), 4)

        session_failure = RecordingRequester(OSError("session unavailable"))
        self.events.clear()
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self.dependencies(
                request_json=session_failure,
                create_local_backend=self.forbidden_local,
            ),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id),
        )
        envelope = self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="owner_unavailable",
            command="agent.checkpoint",
        )
        self.assertNotIn("commit_state", envelope["meta"])
        self.assertEqual(len(session_failure.calls), 1)
        self.assertFalse(any(call["method"] == "POST" for call in session_failure.calls))
        self.assertEqual(self.local_calls, [])


if __name__ == "__main__":
    unittest.main()
