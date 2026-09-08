"""Black-box CLI E2E contract for P0 agent status, context, and checkpoint."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator
from unittest.mock import patch

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
from workstack.file_lease import _FileLease
from workstack.service import WorkStack
from workstack.store import JOURNAL_NAME, LOCK_NAME, Store


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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


_OWNER_PROCESS_SOURCE = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])

from workstack.file_lease import _FileLease

lease = _FileLease(Path(sys.argv[1]))
lease.acquire()
print("held", flush=True)
sys.stdin.readline()
lease.release()
"""


@contextlib.contextmanager
def owner_process(lock_path: Path) -> Iterator["subprocess.Popen[str]"]:
    """Run a real second process that owns the canonical writer lease.

    Runtime metadata is only an advertisement. The running-server route is
    lawful exactly while another compliant writer actually holds
    ``.workstack.lock``, so these tests contend with a genuine foreign process
    rather than with a file that merely exists.
    """

    process = subprocess.Popen(
        [sys.executable, "-c", _OWNER_PROCESS_SOURCE, str(lock_path), str(REPOSITORY_ROOT)],
        cwd=str(REPOSITORY_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = process.stdout.readline().strip()
        if ready != "held":
            raise AssertionError(
                "owner process did not take the lease: {!r} {!r}".format(
                    ready, process.stderr.read()
                )
            )
        yield process
    finally:
        try:
            print("release", file=process.stdin, flush=True)
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=60)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


@contextlib.contextmanager
def count_lease_attempts() -> Iterator[list[Path]]:
    """Record every real writer-lease acquisition attempted inside the block.

    Both outcomes are recorded, so one entry proves the single non-blocking
    attempt and any further entry proves a forbidden second acquisition.
    """

    attempts: list[Path] = []
    real = _FileLease.acquire

    def counting(lease: _FileLease) -> None:
        attempts.append(lease.path)
        real(lease)

    with patch.object(_FileLease, "acquire", counting):
        yield attempts


def _blocked_journal(*, value: dict[str, object]) -> dict[str, object]:
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "created_at": "2026-09-02T10:00:00Z",
        "operation_id": "agent-e2e-operation-1",
        "version": 1,
        "writes": [
            {
                "name": "backlog.json",
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                "value": value,
            }
        ],
    }


class IsolatedAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # Cleanups run last-in-first-out, so a per-test owner process entered
        # with enterContext releases the lock file before the directory that
        # holds it is removed.
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.data_dir = self.home / "data"
        self.runtime_dir = self.home / "runtime"
        self.env = patch.dict(
            os.environ,
            {"WORK_STACK_RUNTIME": str(self.runtime_dir)},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.events: list[tuple[object, ...]] = []
        self.local_calls: list[AuthorityAdmission] = []
        self.online_calls: list[Path] = []

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

    def make_v5_metadata(self) -> None:
        self.data_dir.mkdir(parents=True)
        _write_json(
            self.data_dir / "workspace.json",
            {"version": 2, "id": WORKSPACE_UID, "name": "v5 authority"},
        )
        _write_json(
            self.data_dir / "store-meta.json",
            {"version": 2, "store_schema_version": 5, "migrations": {}},
        )
        _write_json(
            self.data_dir / "reports.json",
            {"version": 1, "reports": [], "idempotency": []},
        )

    def write_owner(self, store: Store | None = None) -> Store:
        owner = store if store is not None else Store(self.data_dir)
        owner.write_server_info(HOST, PORT)
        return owner

    def lock_path(self) -> Path:
        return Store(self.data_dir).root / LOCK_NAME

    def held_owner(self):
        """Contend with a real foreign writer that owns the real lease."""

        return owner_process(self.lock_path())

    def assert_lease_released(self) -> None:
        """The retained handle is released exactly once, at command end."""

        store = Store(self.data_dir)
        lease = store.try_acquire_writer_lease()
        self.assertIsNotNone(lease)
        store.release_writer_lease(lease)

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

    def test_legacy_agent_apply_requires_explicit_authority_before_store(self) -> None:
        with patch(
            "workstack.owner_authority.Store",
            side_effect=AssertionError("Store factory"),
        ) as store_type:
            code, stdout, stderr = self.invoke_main(
                ["agent", "apply", "--stdin", "--intent-id", "intent-123"],
                stdin=b"agent",
            )
        self.assertEqual(code, 2)
        store_type.assert_not_called()
        self.assertEqual(stdout, "")
        self.assertIn("invalid_authority", stderr)

    def test_legacy_agent_apply_succeeds_on_admitted_synthetic_authority(self) -> None:
        task_id = self.make_v3()
        packet = json.dumps({
            "workspace_id": WORKSPACE_UID,
            "task_id": task_id,
            "expected_revision": 0,
            "changes": {"title": "Applied after admission"},
        }).encode("utf-8")
        code, stdout, stderr = self.invoke_main(
            [
                "--data-dir",
                str(self.data_dir),
                "agent",
                "--workspace-uid",
                WORKSPACE_UID,
                "apply",
                "--stdin",
                "--intent-id",
                "intent-apply-1",
            ],
            stdin=packet,
        )
        self.assertEqual(code, 0, stderr)
        receipt = json.loads(stdout)
        self.assertEqual(receipt["meta"]["mode"], "exclusive-local-store")
        self.assertEqual(receipt["data"]["title"], "Applied after admission")
        self.assertEqual(receipt["data"]["revision"], 1)


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

    def test_v5_metadata_is_admitted_store_free_before_store_construction(self) -> None:
        self.make_v5_metadata()
        before = tuple(sorted(p.as_posix() for p in self.home.rglob("*")))
        admissions: list[AuthorityAdmission] = []

        def capturing_admit(
            *,
            data_dir: Path,
            expected_workspace_uid: str,
        ) -> AuthorityAdmission:
            self.events.append(("admit", Path(data_dir), expected_workspace_uid))
            admission = admit_authority(
                data_dir=data_dir,
                expected_workspace_uid=expected_workspace_uid,
            )
            admissions.append(admission)
            return admission

        code, _stdout, _stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=self.dependencies(
                admit_authority=capturing_admit,
                store_factory=self.forbidden_store,
            ),
        )
        after = tuple(sorted(p.as_posix() for p in self.home.rglob("*")))
        self.assertEqual(before, after)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0].storage_format, "v5")
        self.assertEqual(admissions[0].workspace_uid, WORKSPACE_UID)
        self.assertEqual(
            [event[0] for event in self.events],
            ["admit", "store-construct"],
        )
        self.assertEqual(self.local_calls, [])
        self.assertNotEqual(code, 0)

    def test_exclusive_local_v5_status_and_context_require_activated_store(self) -> None:
        from workstack.store import DEFAULTS, STORE_SCHEMA_VERSION

        if STORE_SCHEMA_VERSION != 5 or "reports.json" not in DEFAULTS:
            self.skipTest(
                "blocked pending v5 Store activation and composition"
            )
        task_id = self.make_v3()
        dependencies = self.dependencies(
            create_running_server_backend=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("running-server backend must not be selected")
            ),
        )
        status_code, status_stdout, status_stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=dependencies,
        )
        status = self.assert_success(
            status_code,
            status_stdout,
            status_stderr,
            command="agent.status",
            transport="exclusive-local",
        )
        self.assertEqual(status["data"]["storage_format"], "v5")
        context_code, context_stdout, context_stderr = self.run_command(
            CONTEXT_COMMAND,
            dependencies=dependencies,
            task=task_id,
        )
        self.assert_success(
            context_code,
            context_stdout,
            context_stderr,
            command="agent.context",
            transport="exclusive-local",
        )


class AgentBackendSelectionTests(IsolatedAuthorityTest):
    def test_live_owner_selects_http_without_store_transaction(self) -> None:
        task_id = self.make_v3()
        self.write_owner()
        self.enterContext(self.held_owner())
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

    def test_held_owner_http_failure_never_falls_back_or_reacquires(self) -> None:
        self.make_v3()
        self.write_owner()
        self.enterContext(self.held_owner())
        requester = RecordingRequester(OSError("connection refused"))
        dependencies = self.dependencies(
            request_json=requester,
            create_local_backend=self.forbidden_local,
        )
        with count_lease_attempts() as attempts:
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
        # Exactly one refused attempt: a dead HTTP endpoint is never evidence
        # that the retained foreign lease may be taken.
        self.assertEqual(attempts, [self.lock_path()])

        self.events.clear()
        invalid = Store(self.data_dir)
        invalid.server_info_path.write_text("{}", encoding="utf-8")
        blind = RecordingRequester()
        with count_lease_attempts() as attempts:
            code, stdout, stderr = self.run_command(
                CHECKPOINT_COMMAND,
                dependencies=self.dependencies(
                    request_json=blind,
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
        self.assertEqual(blind.calls, [])
        self.assertEqual(attempts, [self.lock_path()])

    def test_held_owner_without_advertisement_refuses_before_any_probe(self) -> None:
        self.make_v3()
        self.enterContext(self.held_owner())
        self.assertFalse(Store(self.data_dir).server_info_path.exists())
        requester = RecordingRequester()
        with count_lease_attempts() as attempts:
            code, stdout, stderr = self.run_command(
                CHECKPOINT_COMMAND,
                dependencies=self.dependencies(
                    request_json=requester,
                    create_local_backend=self.forbidden_local,
                    create_running_server_backend=lambda **kwargs: (
                        _ for _ in ()
                    ).throw(AssertionError("no advertised owner may be routed to")),
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
        self.assertEqual(requester.calls, [])
        self.assertEqual(self.local_calls, [])
        self.assertFalse(any(event[0] == "online-backend" for event in self.events))
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
        self.assertEqual(attempts, [self.lock_path()])


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
        self.enterContext(self.held_owner())
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
        self.enterContext(self.held_owner())
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
        self.enterContext(self.held_owner())
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


class AgentLeaseFirstRecoveryTests(IsolatedAuthorityTest):
    """The retained writer lease, never runtime metadata, selects the backend."""

    def _forbid_online(self, **kwargs):
        self.events.append(("online-backend", Path(kwargs["server_info_path"])))
        raise AssertionError("a free writer lease must never route to HTTP")

    def _local_dependencies(self, requester: RecordingRequester) -> RuntimeDependencies:
        return self.dependencies(
            request_json=requester,
            create_running_server_backend=self._forbid_online,
        )

    def test_stale_advertisement_over_free_lease_stays_local_without_probe(self) -> None:
        task_id = self.make_v3()
        owner = self.write_owner()
        advertisement = owner.server_info_path.read_bytes()
        requester = RecordingRequester()
        dependencies = self._local_dependencies(requester)
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
                with count_lease_attempts() as attempts:
                    code, stdout, stderr = self.run_command(
                        action,
                        dependencies=dependencies,
                        **kwargs,
                    )
                self.assert_success(
                    code,
                    stdout,
                    stderr,
                    command="agent.{}".format(action),
                    transport="exclusive-local",
                )
                self.assertFalse(
                    any(event[0] == "online-backend" for event in self.events)
                )
                self.assertIn(("transaction-enter", self.data_dir.resolve()), self.events)
                # One non-blocking attempt for the whole command: the handle taken
                # before the backend exists is the one every transaction reuses.
                self.assertEqual(attempts, [self.lock_path()])
                self.assertEqual(requester.calls, [])
                self.assert_lease_released()
        # A stale advertisement is diagnostic evidence, never authority; recovery
        # neither deletes nor rewrites it.
        self.assertEqual(owner.server_info_path.read_bytes(), advertisement)

    def test_corrupted_advertisement_over_free_lease_stays_local(self) -> None:
        self.make_v3()
        store = Store(self.data_dir)
        store.server_info_path.parent.mkdir(parents=True, exist_ok=True)
        store.server_info_path.write_text("{}", encoding="utf-8")
        requester = RecordingRequester()
        with count_lease_attempts() as attempts:
            code, stdout, stderr = self.run_command(
                STATUS_COMMAND,
                dependencies=self._local_dependencies(requester),
            )
        envelope = self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.status",
            transport="exclusive-local",
        )
        self.assertIs(envelope["data"]["exclusive_local_available"], True)
        self.assertIs(envelope["data"]["running_server_available"], False)
        self.assertEqual(requester.calls, [])
        self.assertEqual(attempts, [self.lock_path()])
        self.assertEqual(store.server_info_path.read_text(encoding="utf-8"), "{}")
        self.assert_lease_released()

    def test_local_checkpoint_commits_under_the_retained_handle(self) -> None:
        task_id = self.make_v3()
        self.write_owner()
        code, stdout, stderr = self.run_command(
            CHECKPOINT_COMMAND,
            dependencies=self._local_dependencies(RecordingRequester()),
            intent_id=INTENT_ID,
            checkpoint_raw=_checkpoint_bytes(task_id=task_id),
        )
        envelope = self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.checkpoint",
            transport="exclusive-local",
        )
        self.assertEqual(envelope["meta"]["commit_state"], "committed")
        days = Store(self.data_dir).load("worklog.json")["days"]
        matching = [
            entry
            for entry in days["2026-09-02"]["entries"]
            if entry["task_id"] == task_id
        ]
        self.assertEqual(len(matching), 1)
        self.assert_lease_released()

    def test_out_of_sync_store_keeps_the_existing_local_status_envelope(self) -> None:
        self.make_v3()
        self.write_owner()
        store = Store(self.data_dir)
        backlog = store.path("backlog.json")
        backlog.write_bytes(backlog.read_bytes() + b" ")
        requester = RecordingRequester()
        code, stdout, stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=self._local_dependencies(requester),
        )
        envelope = self.assert_success(
            code,
            stdout,
            stderr,
            command="agent.status",
            transport="exclusive-local",
        )
        # An unsynchronized store is still exclusively local: the advertisement
        # does not turn it into an owner route and it does not become a refusal.
        self.assertIs(envelope["data"]["ready"], False)
        self.assertEqual(envelope["data"]["capability_reason"], "store_sync_required")
        self.assertIs(envelope["data"]["capability_supported"], True)
        self.assertIs(envelope["data"]["exclusive_local_available"], True)
        self.assertIs(envelope["data"]["running_server_available"], False)
        self.assertEqual(requester.calls, [])
        self.assert_lease_released()

    def test_blocked_recovery_journal_refuses_and_preserves_the_evidence(self) -> None:
        self.make_v3()
        store = Store(self.data_dir)
        backlog = store.path("backlog.json")
        backlog.write_bytes(backlog.read_bytes() + b" ")
        unowned = backlog.read_bytes()
        journal_path = self.data_dir.resolve() / JOURNAL_NAME
        journal_path.write_text(
            json.dumps(
                _blocked_journal(value={"items": [], "version": 3}),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        journal = journal_path.read_bytes()
        requester = RecordingRequester()
        with count_lease_attempts() as attempts:
            code, stdout, stderr = self.run_command(
                CHECKPOINT_COMMAND,
                dependencies=self._local_dependencies(requester),
                intent_id=INTENT_ID,
                checkpoint_raw=_checkpoint_bytes(),
            )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="internal_error",
            command="agent.checkpoint",
        )
        self.assertEqual(requester.calls, [])
        # The refusal now precedes the writer lease instead of trailing it: an
        # unreplayable journal never reaches Store construction at all.
        self.assertEqual(attempts, [])
        self.assertEqual([event[0] for event in self.events], ["admit"])
        self.assertEqual(self.local_calls, [])
        # The pending journal and the unowned bytes are retained, not laundered.
        self.assertEqual(journal_path.read_bytes(), journal)
        self.assertEqual(backlog.read_bytes(), unowned)
        self.assert_lease_released()

    def _tree_bytes(self, *, exclude: frozenset[str] = frozenset()) -> dict[str, bytes]:
        """Every authoritative and runtime byte, keyed by path.

        Recovery evidence spans both trees: the journal and the documents live
        under the data directory, while the committed manifest and the owner
        advertisement live under the runtime directory. Comparing the whole
        pair is what makes "nothing was replayed" a byte claim rather than a
        claim about the one file the test happened to remember.
        """

        snapshot: dict[str, bytes] = {}
        for base in (self.data_dir, self.runtime_dir):
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.name not in exclude:
                    snapshot[str(path)] = path.read_bytes()
        return snapshot

    def _valid_journal_bytes(self) -> bytes:
        """A journal whose intended value is exactly the committed baseline.

        Every field, digest and target of this journal is valid, so storage
        recovery would replay it, rewrite ``backlog.json`` and advance the
        manifest. That is precisely the case the agent path must refuse: the
        replay is safe *as storage*, and still unauthorized as the side effect
        of answering ``agent status``.
        """

        backlog = Store(self.data_dir).path("backlog.json")
        value = json.loads(backlog.read_text(encoding="utf-8"))
        return json.dumps(
            _blocked_journal(value=value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def test_valid_recovery_journal_refuses_all_commands_before_any_route(self) -> None:
        task_id = self.make_v3()
        store = Store(self.data_dir)
        journal_path = store.journal_path
        journal = self._valid_journal_bytes()
        journal_path.write_bytes(journal)

        def absent() -> None:
            self.assertFalse(store.server_info_path.exists())

        def stale() -> None:
            self.write_owner(store)

        def corrupt() -> None:
            store.server_info_path.write_text("{}", encoding="utf-8")

        commands = (
            (STATUS_COMMAND, {}),
            (CONTEXT_COMMAND, {"task": task_id}),
            (
                CHECKPOINT_COMMAND,
                {
                    "intent_id": INTENT_ID,
                    "checkpoint_raw": _checkpoint_bytes(task_id=task_id),
                },
            ),
        )
        for metadata, prepare in (
            ("absent", absent),
            ("stale", stale),
            ("corrupt", corrupt),
        ):
            prepare()
            for action, kwargs in commands:
                with self.subTest(metadata=metadata, action=action):
                    self.events.clear()
                    self.local_calls.clear()
                    self.online_calls.clear()
                    requester = RecordingRequester()
                    before = self._tree_bytes()
                    with count_lease_attempts() as attempts:
                        code, stdout, stderr = self.run_command(
                            action,
                            dependencies=self.dependencies(
                                request_json=requester,
                                store_factory=self.forbidden_store,
                                create_local_backend=self.forbidden_local,
                                create_running_server_backend=self._forbid_online,
                            ),
                            **kwargs,
                        )
                    self.assert_failure(
                        code,
                        stdout,
                        stderr,
                        error_code="internal_error",
                        command="agent.{}".format(action),
                    )
                    # Admission is the only thing that ran: no Store, no
                    # backend of either kind, no HTTP, no lease attempt.
                    self.assertEqual([event[0] for event in self.events], ["admit"])
                    self.assertEqual(self.local_calls, [])
                    self.assertEqual(self.online_calls, [])
                    self.assertEqual(requester.calls, [])
                    self.assertEqual(attempts, [])
                    self.assertEqual(self._tree_bytes(), before)
                    self.assertEqual(journal_path.read_bytes(), journal)
        self.assert_lease_released()
        # The refusals above are not vacuous. The producer's own storage layer
        # still replays this exact journal when a Store is initialized directly,
        # which is lawful storage behaviour; the guard withholds that recovery
        # from agent commands, it does not depend on the journal being broken.
        WorkStack(Store(self.data_dir))
        self.assertFalse(journal_path.exists())

    def test_journal_landing_before_acquisition_is_caught_under_the_lease(self) -> None:
        self.make_v3()
        self.write_owner()
        store = Store(self.data_dir)
        journal_path = store.journal_path
        journal = self._valid_journal_bytes()
        self.assertFalse(journal_path.exists())
        releases: list[Path] = []
        acquire = _FileLease.acquire
        release = _FileLease.release

        def barrier(lease: _FileLease) -> None:
            # The window a preflight can never cover: an interrupted writer
            # lands a pending journal after admission read the directory and
            # before this process owns the lease.
            journal_path.write_bytes(journal)
            acquire(lease)

        def counting_release(lease: _FileLease) -> None:
            releases.append(lease.path)
            release(lease)

        requester = RecordingRequester()
        with patch.object(_FileLease, "acquire", barrier):
            with patch.object(_FileLease, "release", counting_release):
                code, stdout, stderr = self.run_command(
                    STATUS_COMMAND,
                    dependencies=self.dependencies(
                        request_json=requester,
                        create_local_backend=self.forbidden_local,
                        create_running_server_backend=self._forbid_online,
                    ),
                )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="internal_error",
            command="agent.status",
        )
        # The recheck happens while the lease is genuinely retained, so the
        # answer cannot go stale, and the handle taken for the decision is
        # given back exactly once instead of being released and reacquired.
        self.assertEqual(releases, [self.lock_path()])
        self.assertEqual(self.local_calls, [])
        self.assertEqual(requester.calls, [])
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
        self.assertFalse(any(event[0] == "local-backend" for event in self.events))
        self.assertEqual(journal_path.read_bytes(), journal)
        self.assert_lease_released()

    def test_backend_construction_and_command_failures_release_the_lease(self) -> None:
        self.make_v3()
        self.write_owner()

        def failing_construction(*, admission: AuthorityAdmission, store_factory):
            self.local_calls.append(admission)
            store_factory(root=admission.data_dir)
            raise RuntimeError("backend construction failed after the lease was taken")

        code, stdout, stderr = self.run_command(
            STATUS_COMMAND,
            dependencies=self.dependencies(
                create_local_backend=failing_construction,
                create_running_server_backend=self._forbid_online,
            ),
        )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="internal_error",
            command="agent.status",
        )
        self.assertEqual(len(self.local_calls), 1)
        self.assert_lease_released()

        exploding = RecordingBackend(error=RuntimeError("command failed mid-flight"))
        code, stdout, stderr = self.run_command(
            CONTEXT_COMMAND,
            dependencies=self.dependencies(
                create_local_backend=lambda **kwargs: self.fake_local(
                    exploding, **kwargs
                ),
                create_running_server_backend=self._forbid_online,
            ),
            task="T-0001",
        )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="internal_error",
            command="agent.context",
        )
        self.assert_lease_released()

    def test_held_owner_identity_mismatch_never_falls_back_locally(self) -> None:
        self.make_v3()
        self.write_owner()
        self.enterContext(self.held_owner())
        foreign = RecordingRequester(
            SESSION,
            (200, {"data": {"store_schema_version": 3, "workspace_id": OTHER_UID}}),
        )
        with count_lease_attempts() as attempts:
            code, stdout, stderr = self.run_command(
                STATUS_COMMAND,
                dependencies=self.dependencies(
                    request_json=foreign,
                    create_local_backend=self.forbidden_local,
                ),
            )
        self.assert_failure(
            code,
            stdout,
            stderr,
            error_code="workspace_mismatch",
            command="agent.status",
        )
        self.assertEqual(self.local_calls, [])
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
        self.assertEqual(attempts, [self.lock_path()])

    def test_commit_unknown_never_reacquires_or_falls_back_locally(self) -> None:
        task_id = self.make_v3()
        self.write_owner()
        self.enterContext(self.held_owner())
        unverifiable = RecordingRequester(
            SESSION,
            STORAGE,
            OSError("first response lost"),
            TimeoutError("replay response lost"),
        )
        with count_lease_attempts() as attempts:
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
        self.assertEqual(self.local_calls, [])
        self.assertFalse(any(event[0] == "transaction-enter" for event in self.events))
        # No second acquisition, no local backend, no new intent after an
        # unverifiable send.
        self.assertEqual(attempts, [self.lock_path()])
        posts = [call for call in unverifiable.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertEqual(
            posts[0]["headers"]["Idempotency-Key"],
            posts[1]["headers"]["Idempotency-Key"],
        )


if __name__ == "__main__":
    unittest.main()
