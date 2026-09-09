"""Tests for the real SSH-backed ``BackupPort`` and its bounded transport.

Nothing here runs a real ``ssh``, reaches a host, reads a credential, installs
anything or touches a live store.  Both legs are real in the way that matters:

*   the argv and the request document are the ones the port would actually
    send, asserted token by token and key by key;
*   the *far side* is the product's own frozen adapter,
    ``desktop/python-webview-shell/remote_update_maintenance.py``, executed as a
    real local child process against a disposable store built by the product's
    own service facade.  The archives are real archives and every digest is
    recomputed by ``maintenance.verify_backup`` rather than asserted literally.

``_RemoteHarness`` is the stand-in for the SSH hop.  It checks the argv it was
given, translates the remote namespace in the request document into the
temporary directory that stands for that remote's disk, and runs the adapter
there.  Its ``behaviour`` knob is how a lost reply, a channel that never
started, an absent helper and a foreign document are produced -- each one
without any special case inside the code under test.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from workstack import maintenance  # noqa: E402
from workstack.service import WorkStack  # noqa: E402
from workstack.store import Store  # noqa: E402

import remote_update_backup_port as PORT  # noqa: E402
import remote_update_flow_outcomes as OUTCOMES  # noqa: E402
import remote_update_maintenance_transport as TRANSPORT  # noqa: E402
from remote_update_flow_contract import (  # noqa: E402
    BackupOutcome,
    LostResponse,
    PortRefusal,
    RestoreOutcome,
)

ADAPTER_PATH = SHELL / "remote_update_maintenance.py"

SSH_EXECUTABLE = "ssh.exe"
REMOTE_ALIAS = "workstack-remote-fixture"
REMOTE_PYTHON = "/opt/workstack/runtime/bin/python3"
REMOTE_APP_DIR = "/opt/workstack/1.0.13"
REMOTE_HELPER = f"{REMOTE_APP_DIR}/desktop/python-webview-shell/remote_update_maintenance.py"
REMOTE_WORKSPACE = "/srv/workstack/planning-store"
REMOTE_STATE = "/srv/workstack/update-operations"
REMOTE_BACKUPS = "/srv/workstack/update-backups"

TASK_TITLE = "Planning fact that must survive the remote update"
NOTE_TEXT = "Context this workspace still has to hold after a restore"

RUN = "run"
NOT_STARTED = "not_started"
NO_STDIN = "no_stdin"
DROP_REPLY = "drop_reply"
FOREIGN_REPLY = "foreign_reply"
HANG = "hang"
SHORT_TIMEOUT = 0.2


def _idle_observation(request: dict) -> tuple[int, bytes, bytes, bool]:
    """A finished not-run observation, without spawning the real adapter."""

    restore = request["kind"] == TRANSPORT.KIND_OBSERVE_RESTORE
    document = {
        "schema_version": TRANSPORT.HELPER_SCHEMA_VERSION,
        "tool": TRANSPORT.HELPER_TOOL,
        "kind": TRANSPORT.KIND_RESTORE_BACKUP if restore else TRANSPORT.KIND_CREATE_BACKUP,
        "operation_id": request["operation_id"],
        "status": "unknown",
        "code": "restore_not_run" if restore else "backup_not_run",
        "attempted": False,
        "observation": True,
    }
    return (0, json.dumps(document).encode("utf-8"), b"", False)


class _Sink:
    """A stdin pipe that delivers the whole payload when it is closed."""

    def __init__(self, deliver) -> None:
        self._deliver = deliver
        self._buffer = io.BytesIO()
        self._closed = False

    def write(self, payload: bytes) -> int:
        return self._buffer.write(payload)

    def close(self) -> None:
        # A real pipe is closed again when the exchange settles its handles;
        # the request must still be delivered exactly once.
        if self._closed:
            return
        self._closed = True
        self._deliver(self._buffer.getvalue())


class _Channel:
    """One stand-in child process, owning exactly one request and one reply."""

    def __init__(self, harness, command: list[str]) -> None:
        self._harness = harness
        self.command = command
        self.stdin = _Sink(self._deliver)
        self.stdout: io.BytesIO | None = None
        self.stderr: io.BytesIO | None = None
        self.returncode: int | None = None
        self._code = 0
        self._hangs = False
        self.killed = False

    def _deliver(self, payload: bytes) -> None:
        code, out, err, hangs = self._harness.serve(self.command, payload)
        self._code = code
        self._hangs = hangs
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)

    def wait(self, timeout: float | None = None) -> int:
        # Hang is a closed answer, not a wall-clock race: the caller already
        # applied its bound, so this process simply never exits until killed.
        if self._hangs and not self.killed:
            raise subprocess.TimeoutExpired(self.command, timeout or 0.0)
        self.returncode = -9 if self.killed else self._code
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _RemoteHarness:
    """The SSH hop: it checks the argv, then runs the real adapter locally."""

    def __init__(self, disk: "_RemoteDisk") -> None:
        self.disk = disk
        self.behaviour = RUN
        # A behaviour applies to the kinds named here.  By default only the
        # mutating calls are disturbed, so a test can lose exactly the reply
        # that matters without also breaking the read-only preflight.
        self.affected = {TRANSPORT.KIND_CREATE_BACKUP, TRANSPORT.KIND_RESTORE_BACKUP}
        self.commands: list[list[str]] = []
        self.requests: list[dict] = []
        self.helper_paths: set[str] = {REMOTE_HELPER}

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if self.behaviour == NOT_STARTED:
            raise OSError("the fixture refused to create a child")
        channel = _Channel(self, list(command))
        if self.behaviour == NO_STDIN:
            channel.stdin = None  # type: ignore[assignment]
        return channel

    def serve(self, command: list[str], payload: bytes) -> tuple[int, bytes, bytes, bool]:
        request = json.loads(payload.decode("utf-8"))
        self.requests.append(dict(request))
        if not any(helper in command[-1] for helper in self.helper_paths):
            # Exactly what an application directory predating the adapter does:
            # the interpreter starts, finds no such file and prints nothing on
            # stdout.  No document, so nothing was answered.
            return (2, b"", b"python3: no such file\n", False)
        disturbed = request["kind"] in self.affected
        if disturbed and self.behaviour == HANG:
            return (0, b"", b"", True)
        if self.behaviour == HANG and request["kind"] in (
            TRANSPORT.KIND_OBSERVE_BACKUP,
            TRANSPORT.KIND_OBSERVE_RESTORE,
        ):
            # A hung mutation still needs a finished read-only preflight.
            # Running the real adapter here would consume the short port timeout
            # inside stdin.close(), so the create/restore would never be issued.
            return _idle_observation(request)
        code, out, err = self.disk.run_adapter(self.disk.translate(request))
        if not disturbed:
            return (code, out, err, False)
        if self.behaviour == DROP_REPLY:
            return (code, b"", err, False)
        if self.behaviour == FOREIGN_REPLY:
            foreign = json.dumps({"schema_version": "other/9", "tool": "x"}).encode()
            return (code, foreign, err, False)
        return (code, out, err, False)


class _RemoteDisk:
    """The disposable store, receipts and archives standing in for a remote."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "planning-store"
        self.state = root / "update-operations"
        self.backups = root / "update-backups"
        self.stack = WorkStack(Store(self.workspace))
        self.stack.add_task(TASK_TITLE, priority="P1")
        self.stack.add_note(NOTE_TEXT)
        with Store(self.workspace).consistent_read() as readiness:
            self.workspace_id = readiness.workspace_uid
        self._map = {
            REMOTE_WORKSPACE: str(self.workspace),
            REMOTE_STATE: str(self.state),
            REMOTE_BACKUPS: str(self.backups),
        }

    def translate(self, request: dict) -> dict:
        """Turn the remote namespace into this disk's real directories."""

        document = dict(request)
        for field in ("workspace_dir", "state_root", "backup_root"):
            document[field] = self._map[document[field]]
        return document

    def run_adapter(self, request: dict) -> tuple[int, bytes, bytes]:
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(ADAPTER_PATH)],
            input=json.dumps(request).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            timeout=180,
        )
        return (process.returncode, process.stdout, process.stderr)

    def archives(self, operation: str) -> list[Path]:
        directory = self.backups / operation
        if not directory.is_dir():
            return []
        return sorted(item for item in directory.iterdir() if item.is_file())

    def receipts(self) -> list[str]:
        return sorted(os.listdir(self.state)) if self.state.is_dir() else []

    def drop_completion(self, operation: str) -> None:
        """Leave the attempt bound and the completion missing, as a crash does."""

        completed = self.state / f"{operation}.completed.json"
        assert completed.is_file(), completed
        completed.unlink()
        assert (self.state / f"{operation}.attempt.json").is_file()

    def titles(self) -> list[str]:
        return sorted(item["title"] for item in WorkStack(Store(self.workspace)).list_tasks(status="all"))


class BackupPortTestCase(unittest.TestCase):
    """Shared disposable remote plus a port wired to the fixture SSH hop."""

    def setUp(self) -> None:
        import tempfile

        self.temporary = tempfile.TemporaryDirectory()
        self.disk = _RemoteDisk(Path(self.temporary.name))
        self.harness = _RemoteHarness(self.disk)
        self.backup_id = str(uuid.uuid4())
        self.restore_id = str(uuid.uuid4())
        self.port = self.make_port()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def target(self, *, helper: str = REMOTE_HELPER) -> TRANSPORT.MaintenanceTarget:
        return TRANSPORT.MaintenanceTarget(
            ssh_host_alias=REMOTE_ALIAS,
            remote_python=REMOTE_PYTHON,
            remote_helper_path=helper,
            remote_workspace_dir=REMOTE_WORKSPACE,
            workspace_id=self.disk.workspace_id,
            remote_state_root=REMOTE_STATE,
            remote_backup_root=REMOTE_BACKUPS,
        )

    def make_port(self, **kwargs) -> PORT.RemoteMaintenanceBackupPort:
        target = kwargs.pop("target", None) or self.target()
        return PORT.RemoteMaintenanceBackupPort(
            target,
            ssh_executable=SSH_EXECUTABLE,
            timeout=180.0,
            process_factory=self.harness,
            **kwargs,
        )

    def verified_backup(self) -> BackupOutcome:
        outcome = self.port.create_verified(self.backup_id)
        self.assertEqual(outcome.status, "verified")
        return outcome


class CommandAndRequestShapeTest(BackupPortTestCase):
    """The argv and the request document, exactly as they would be sent."""

    def test_the_command_is_the_audited_openssh_shape_running_the_helper(self) -> None:
        command = TRANSPORT.build_command(self.target(), SSH_EXECUTABLE)

        self.assertEqual(
            command,
            [
                SSH_EXECUTABLE, "-T",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10",
                "-o", "PermitLocalCommand=no",
                "-o", "ClearAllForwardings=yes",
                "--", REMOTE_ALIAS,
                f"{REMOTE_PYTHON} -I -B {REMOTE_HELPER}",
            ],
        )

    def test_no_operation_detail_ever_becomes_an_argv_token(self) -> None:
        self.verified_backup()
        digest = self.port.restore_source.backup_digest

        for command in self.harness.commands:
            joined = " ".join(command)
            self.assertNotIn(self.backup_id, joined)
            self.assertNotIn(digest, joined)
            self.assertNotIn(self.disk.workspace_id, joined)
            self.assertNotIn(str(self.disk.workspace), joined)

    def test_each_kind_sends_exactly_the_adapter_s_admitted_keys(self) -> None:
        common = {
            "backup_root", "kind", "operation_id",
            "state_root", "workspace_dir", "workspace_id",
        }
        digest = "sha256:" + "b" * 64

        for kind in (TRANSPORT.KIND_CREATE_BACKUP, TRANSPORT.KIND_OBSERVE_BACKUP):
            request = TRANSPORT.build_request(
                self.target(), kind=kind, operation_id=self.backup_id
            )
            self.assertEqual(set(request), common, kind)
            self.assertEqual(request["workspace_dir"], REMOTE_WORKSPACE)

        for kind in (TRANSPORT.KIND_RESTORE_BACKUP, TRANSPORT.KIND_OBSERVE_RESTORE):
            request = TRANSPORT.build_request(
                self.target(),
                kind=kind,
                operation_id=self.restore_id,
                source_operation_id=self.backup_id,
                backup_digest=digest,
            )
            self.assertEqual(
                set(request), common | {"source_operation_id", "backup_digest"}, kind
            )

    def test_a_source_offered_for_an_unsourced_kind_is_refused(self) -> None:
        with self.assertRaises(TRANSPORT.MaintenanceTargetError):
            TRANSPORT.build_request(
                self.target(),
                kind=TRANSPORT.KIND_CREATE_BACKUP,
                operation_id=self.backup_id,
                backup_digest="sha256:" + "c" * 64,
            )

    def test_a_windows_style_helper_path_never_becomes_a_command(self) -> None:
        with self.assertRaises(TRANSPORT.MaintenanceTargetError):
            TRANSPORT.build_command(
                self.target(helper=r"C:\opt\remote_update_maintenance.py"), SSH_EXECUTABLE
            )

    def test_an_alias_that_could_be_read_as_an_option_is_refused(self) -> None:
        target = TRANSPORT.MaintenanceTarget(
            ssh_host_alias="-oProxyCommand=touch /tmp/x",
            remote_python=REMOTE_PYTHON,
            remote_helper_path=REMOTE_HELPER,
            remote_workspace_dir=REMOTE_WORKSPACE,
            workspace_id=self.disk.workspace_id,
            remote_state_root=REMOTE_STATE,
            remote_backup_root=REMOTE_BACKUPS,
        )

        with self.assertRaises(TRANSPORT.MaintenanceTargetError):
            TRANSPORT.build_command(target, SSH_EXECUTABLE)


class RealBackupTest(BackupPortTestCase):
    """The port drives the real adapter against the disposable store."""

    def test_create_verified_takes_one_real_verified_archive(self) -> None:
        outcome = self.port.create_verified(self.backup_id)

        self.assertEqual(outcome, BackupOutcome(status="verified", migration_required=None))
        archives = self.disk.archives(self.backup_id)
        self.assertEqual(len(archives), 1, archives)
        artifact = maintenance.verify_backup(archives[0])
        self.assertEqual(artifact.workspace_id, self.disk.workspace_id)
        self.assertEqual(self.port.restore_source.backup_digest, artifact.digest)
        self.assertEqual(self.port.restore_source.operation_id, self.backup_id)

    def test_a_verified_backup_is_preceded_by_a_read_only_observation(self) -> None:
        self.verified_backup()

        kinds = [request["kind"] for request in self.harness.requests]
        self.assertEqual(kinds, ["observe_backup", "create_backup"])
        self.assertEqual(
            [request["operation_id"] for request in self.harness.requests],
            [self.backup_id, self.backup_id],
        )

    def test_observing_a_backup_that_never_ran_says_not_run(self) -> None:
        outcome = self.port.observe(self.backup_id)

        self.assertEqual(outcome, BackupOutcome(status="not_run", migration_required=None))
        self.assertEqual(self.disk.receipts(), [])

    def test_observing_a_verified_backup_answers_without_a_second_backup(self) -> None:
        self.verified_backup()
        before = self.disk.archives(self.backup_id)

        outcome = self.port.observe(self.backup_id)

        self.assertEqual(outcome.status, "verified")
        self.assertEqual(self.disk.archives(self.backup_id), before)

    def test_a_workspace_held_by_its_owner_refuses_with_no_archive(self) -> None:
        with Store(self.disk.workspace).server_lease():
            with self.assertRaises(PortRefusal) as raised:
                self.port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.code, "workspace_held_by_owner")
        self.assertEqual(self.disk.archives(self.backup_id), [])
        self.assertIsNone(self.port.restore_source)


class RealRestoreTest(BackupPortTestCase):
    """A restore really puts the archive back, and says only what it proved."""

    def test_a_restore_returns_the_archived_data_without_claiming_rollback(self) -> None:
        self.verified_backup()
        self.disk.stack.add_task("Written after the backup was taken", priority="P2")
        self.assertIn("Written after the backup was taken", self.disk.titles())

        outcome = self.port.restore(self.restore_id)

        self.assertIsInstance(outcome, RestoreOutcome)
        self.assertEqual(outcome.status, "verified")
        # The whole point: the adapter restored through the current upgrade
        # planner, so it cannot say the pre-migration data is back and neither
        # may this port.
        self.assertIsNone(outcome.pre_migration_data)
        self.assertEqual(self.disk.titles(), [TASK_TITLE])

    def test_the_flow_reads_that_restore_as_unknown_not_verified(self) -> None:
        self.verified_backup()

        outcome = self.port.restore(self.restore_id)

        stage, code = OUTCOMES.classify_backup_restore(outcome)
        self.assertEqual(code, "restore_unknown")
        self.assertEqual(stage.value, "unknown")

    def test_a_restore_binds_the_backup_s_own_identity_and_digest(self) -> None:
        self.verified_backup()
        digest = self.port.restore_source.backup_digest

        self.port.restore(self.restore_id)

        issued = [r for r in self.harness.requests if r["kind"] == "restore_backup"]
        self.assertEqual(len(issued), 1, issued)
        self.assertEqual(issued[0]["operation_id"], self.restore_id)
        self.assertEqual(issued[0]["source_operation_id"], self.backup_id)
        self.assertEqual(issued[0]["backup_digest"], digest)

    def test_a_restore_with_no_known_source_refuses_before_any_process(self) -> None:
        with self.assertRaises(PortRefusal) as raised:
            self.port.restore(self.restore_id)

        self.assertEqual(raised.exception.code, PORT.CODE_RESTORE_SOURCE_UNKNOWN)
        self.assertEqual(self.harness.commands, [])

    def test_a_retained_source_lets_a_restarted_port_restore(self) -> None:
        self.verified_backup()
        retained = self.port.restore_source

        restarted = self.make_port(
            restore_source=PORT.RestoreSource(retained.operation_id, retained.backup_digest)
        )
        outcome = restarted.restore(self.restore_id)

        self.assertEqual(outcome.status, "verified")
        self.assertEqual(self.disk.titles(), [TASK_TITLE])

    def test_observing_a_restore_that_never_ran_settles_as_failed(self) -> None:
        self.verified_backup()

        outcome = self.port.observe_restore(self.restore_id)

        self.assertEqual(outcome, RestoreOutcome(status="failed", pre_migration_data=None))
        stage, code = OUTCOMES.classify_backup_restore(outcome)
        self.assertEqual(code, "restore_failed")


class LostResponseTest(BackupPortTestCase):
    """A reply that never arrived is the same identity, never a new mutation."""

    def test_a_dropped_reply_after_a_real_backup_is_a_lost_response(self) -> None:
        self.harness.behaviour = DROP_REPLY

        with self.assertRaises(LostResponse) as raised:
            self.port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.operation_id, self.backup_id)
        # The effect really did happen on the far side.
        self.assertEqual(len(self.disk.archives(self.backup_id)), 1)

    def test_reconciling_that_identity_answers_from_the_archive(self) -> None:
        self.harness.behaviour = DROP_REPLY
        with self.assertRaises(LostResponse):
            self.port.create_verified(self.backup_id)
        self.harness.behaviour = RUN

        outcome = self.port.observe(self.backup_id)

        self.assertEqual(outcome.status, "verified")
        self.assertEqual(len(self.disk.archives(self.backup_id)), 1)
        self.assertEqual(
            self.port.restore_source.backup_digest,
            maintenance.verify_backup(self.disk.archives(self.backup_id)[0]).digest,
        )

    def test_a_second_create_on_a_settled_identity_replays_it(self) -> None:
        self.harness.behaviour = DROP_REPLY
        with self.assertRaises(LostResponse):
            self.port.create_verified(self.backup_id)
        self.harness.behaviour = RUN

        outcome = self.port.create_verified(self.backup_id)

        self.assertEqual(outcome.status, "verified")
        issued = [r for r in self.harness.requests if r["kind"] == "create_backup"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(len(self.disk.archives(self.backup_id)), 1)

    def test_a_bound_backup_is_reconciled_from_its_archive_not_rerun(self) -> None:
        self.harness.behaviour = DROP_REPLY
        with self.assertRaises(LostResponse):
            self.port.create_verified(self.backup_id)
        self.harness.behaviour = RUN
        # The far side bound the attempt and then died before recording the
        # completion, which is the state an interrupted operation really leaves.
        self.disk.drop_completion(self.backup_id)

        outcome = self.port.create_verified(self.backup_id)

        self.assertEqual(outcome.status, "verified")
        issued = [r for r in self.harness.requests if r["kind"] == "create_backup"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(len(self.disk.archives(self.backup_id)), 1)

    def test_a_bound_backup_with_nothing_to_reconcile_stays_lost(self) -> None:
        self.harness.behaviour = DROP_REPLY
        with self.assertRaises(LostResponse):
            self.port.create_verified(self.backup_id)
        self.harness.behaviour = RUN
        self.disk.drop_completion(self.backup_id)
        self.disk.archives(self.backup_id)[0].unlink()

        with self.assertRaises(LostResponse):
            self.port.create_verified(self.backup_id)

        issued = [r for r in self.harness.requests if r["kind"] == "create_backup"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(self.disk.archives(self.backup_id), [])

    def test_a_dropped_restore_reply_reconciles_to_the_completion_on_record(self) -> None:
        self.verified_backup()
        self.harness.behaviour = DROP_REPLY

        with self.assertRaises(LostResponse) as raised:
            self.port.restore(self.restore_id)

        self.assertEqual(raised.exception.operation_id, self.restore_id)
        self.assertEqual(self.disk.titles(), [TASK_TITLE])

        self.harness.behaviour = RUN
        observed = self.port.observe_restore(self.restore_id)

        self.assertEqual(observed, RestoreOutcome(status="verified", pre_migration_data=None))
        issued = [r for r in self.harness.requests if r["kind"] == "restore_backup"]
        self.assertEqual(len(issued), 1)

    def test_a_restore_with_no_completion_on_record_stays_unknown(self) -> None:
        self.verified_backup()
        self.harness.behaviour = DROP_REPLY
        with self.assertRaises(LostResponse):
            self.port.restore(self.restore_id)
        self.harness.behaviour = RUN
        self.disk.drop_completion(self.restore_id)

        observed = self.port.observe_restore(self.restore_id)

        # Restored data that merely looks plausible is not evidence, and the
        # restore is never reissued to find out.
        self.assertEqual(observed, RestoreOutcome(status="unknown", pre_migration_data=None))
        stage, code = OUTCOMES.classify_backup_restore(observed)
        self.assertEqual(code, "restore_unknown")
        with self.assertRaises(LostResponse):
            self.port.restore(self.restore_id)
        issued = [r for r in self.harness.requests if r["kind"] == "restore_backup"]
        self.assertEqual(len(issued), 1)

    def test_a_reply_that_is_not_this_tool_s_document_is_lost(self) -> None:
        self.harness.behaviour = FOREIGN_REPLY

        with self.assertRaises(LostResponse):
            self.port.create_verified(self.backup_id)

    def test_a_timed_out_exchange_is_lost_not_refused(self) -> None:
        port = PORT.RemoteMaintenanceBackupPort(
            self.target(),
            ssh_executable=SSH_EXECUTABLE,
            timeout=SHORT_TIMEOUT,
            process_factory=self.harness,
        )
        self.harness.behaviour = HANG

        with self.assertRaises(LostResponse) as raised:
            port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.operation_id, self.backup_id)
        self.assertEqual(
            [request["kind"] for request in self.harness.requests],
            ["observe_backup", "create_backup"],
        )
        self.assertEqual(self.disk.archives(self.backup_id), [])
        self.assertEqual(self.disk.receipts(), [])

    def test_a_timed_out_restore_is_lost_not_refused(self) -> None:
        self.verified_backup()
        port = PORT.RemoteMaintenanceBackupPort(
            self.target(),
            ssh_executable=SSH_EXECUTABLE,
            timeout=SHORT_TIMEOUT,
            process_factory=self.harness,
            restore_source=self.port.restore_source,
        )
        self.harness.behaviour = HANG

        with self.assertRaises(LostResponse) as raised:
            port.restore(self.restore_id)

        self.assertEqual(raised.exception.operation_id, self.restore_id)
        issued = [request for request in self.harness.requests if request["kind"] == "restore_backup"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(
            [request["kind"] for request in self.harness.requests[-2:]],
            ["observe_restore", "restore_backup"],
        )


class PreEffectRefusalTest(BackupPortTestCase):
    """Only what provably never left this machine may be a refusal."""

    def test_a_channel_that_never_started_refuses_with_no_effect(self) -> None:
        self.harness.behaviour = NOT_STARTED

        with self.assertRaises(PortRefusal) as raised:
            self.port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.code, TRANSPORT.CODE_CHANNEL_NOT_STARTED)
        self.assertEqual(self.disk.receipts(), [])
        self.assertEqual(self.disk.archives(self.backup_id), [])

    def test_a_channel_with_no_stdin_refuses_with_no_effect(self) -> None:
        self.harness.behaviour = NO_STDIN

        with self.assertRaises(PortRefusal) as raised:
            self.port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.code, TRANSPORT.CODE_CHANNEL_NO_STDIN)
        self.assertEqual(self.disk.receipts(), [])

    def test_an_application_directory_without_the_adapter_refuses(self) -> None:
        """An install predating the helper answers nothing, and nothing ran."""

        port = self.make_port(target=self.target(helper="/opt/workstack/1.0.8/nonexistent.py"))

        with self.assertRaises(PortRefusal) as raised:
            port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.code, TRANSPORT.CODE_REPLY_UNRECOGNISED)
        self.assertEqual(self.disk.receipts(), [])
        self.assertEqual(self.disk.archives(self.backup_id), [])
        # Only the read-only preflight was ever sent.
        self.assertEqual([r["kind"] for r in self.harness.requests], ["observe_backup"])

    def test_an_unreachable_observation_refuses_and_settles_nothing(self) -> None:
        self.harness.behaviour = NOT_STARTED

        with self.assertRaises(PortRefusal):
            self.port.observe(self.backup_id)

    def test_a_timed_out_preflight_refuses_with_no_mutation(self) -> None:
        port = PORT.RemoteMaintenanceBackupPort(
            self.target(),
            ssh_executable=SSH_EXECUTABLE,
            timeout=SHORT_TIMEOUT,
            process_factory=self.harness,
        )
        self.harness.behaviour = HANG
        self.harness.affected = {TRANSPORT.KIND_OBSERVE_BACKUP}

        with self.assertRaises(PortRefusal) as raised:
            port.create_verified(self.backup_id)

        self.assertEqual(raised.exception.code, TRANSPORT.CODE_CHANNEL_LOST)
        self.assertEqual(self.disk.archives(self.backup_id), [])
        self.assertEqual(self.disk.receipts(), [])
        self.assertEqual([request["kind"] for request in self.harness.requests], ["observe_backup"])

    def test_an_operation_id_that_is_not_canonical_refuses_before_any_process(self) -> None:
        with self.assertRaises(PortRefusal) as raised:
            self.port.create_verified("not-a-uuid")

        self.assertEqual(raised.exception.code, TRANSPORT.CODE_REQUEST_UNBUILDABLE)
        self.assertEqual(self.harness.commands, [])


class ContractConformanceTest(BackupPortTestCase):
    """The port is the flow's ``BackupPort``, method for method."""

    def test_it_offers_exactly_the_backup_port_protocol(self) -> None:
        import inspect

        from remote_update_flow_contract import BackupPort

        expected = {
            name: str(inspect.signature(getattr(BackupPort, name)))
            for name in ("create_verified", "observe", "restore", "observe_restore")
        }
        actual = {
            name: str(inspect.signature(getattr(PORT.RemoteMaintenanceBackupPort, name)))
            for name in expected
        }

        self.assertEqual(actual, expected)

    def test_the_flow_maps_a_verified_backup_onto_its_own_gate(self) -> None:
        outcome = self.port.create_verified(self.backup_id)

        fact, stage, code = OUTCOMES.classify_backup_create(outcome, None)
        self.assertEqual(code, "backup_verified")
        self.assertEqual(stage.value, "backup")
        self.assertEqual(fact.status, "verified")

    def test_the_flow_reads_an_unobserved_backup_as_not_run(self) -> None:
        outcome = self.port.observe(self.backup_id)

        _, stage, code = OUTCOMES.classify_backup_create(outcome, None)
        self.assertEqual(code, "backup_not_run")
        self.assertEqual(stage.value, "failed")


class AttemptedSuccessTest(BackupPortTestCase):
    """A verified answer is a success even though the adapter marks it attempted.

    ``attempted`` says the operation is durably bound, which is what makes an
    *unresolved* answer a lost response.  It never demotes an answer the far
    side actually verified: only a non-success carrying it stays open.
    """

    def test_a_real_verified_backup_is_attempted_and_still_a_success(self) -> None:
        outcome = self.port.create_verified(self.backup_id)

        served = [r for r in self.harness.requests if r["kind"] == "create_backup"]
        self.assertEqual(len(served), 1)
        # The adapter really did report attempted=true alongside status=verified.
        code, stdout, _ = self.disk.run_adapter(
            self.disk.translate(
                dict(served[0], kind=TRANSPORT.KIND_OBSERVE_BACKUP)
            )
        )
        document = json.loads(stdout.decode("utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(document["status"], "verified")
        self.assertIs(document["attempted"], True)
        self.assertEqual(outcome.status, "verified")

    def test_a_real_verified_restore_is_attempted_and_still_a_success(self) -> None:
        self.verified_backup()

        outcome = self.port.restore(self.restore_id)

        served = [r for r in self.harness.requests if r["kind"] == "restore_backup"]
        _, stdout, _ = self.disk.run_adapter(
            self.disk.translate(dict(served[0], kind=TRANSPORT.KIND_OBSERVE_RESTORE))
        )
        document = json.loads(stdout.decode("utf-8"))
        self.assertEqual(document["status"], "verified")
        self.assertIs(document["attempted"], True)
        self.assertEqual(outcome.status, "verified")

    def test_only_a_non_success_carrying_attempted_stays_open(self) -> None:
        verified = {"status": "verified", "attempted": True, "code": "backup_verified"}
        unknown = {"status": "unknown", "attempted": True, "code": "maintenance_io_unknown"}
        refused = {"status": "failed", "code": "workspace_held_by_owner"}
        bound = {"status": "failed", "attempted": True, "code": "maintenance_refused"}
        answered = TRANSPORT.EFFECT_UNKNOWN

        self.assertIs(
            self.port._answer(self.backup_id, TRANSPORT.MaintenanceReply(verified, answered, "")),
            verified,
        )
        for document in (unknown, bound):
            with self.assertRaises(LostResponse, msg=document):
                self.port._answer(
                    self.backup_id, TRANSPORT.MaintenanceReply(document, answered, "")
                )
        with self.assertRaises(PortRefusal) as raised:
            self.port._answer(
                self.backup_id, TRANSPORT.MaintenanceReply(refused, answered, "")
            )
        self.assertEqual(raised.exception.code, "workspace_held_by_owner")


class BoundedFactsTest(BackupPortTestCase):
    """Only typed outcomes and closed-set codes leave this port."""

    def test_a_code_outside_the_closed_set_becomes_the_unrecognised_code(self) -> None:
        self.assertEqual(PORT.bounded_code("workspace_held_by_owner"), "workspace_held_by_owner")
        self.assertEqual(PORT.bounded_code("/srv/workstack/planning-store"), PORT.CODE_UNRECOGNISED)
        self.assertEqual(PORT.bounded_code(None), PORT.CODE_UNRECOGNISED)

    def test_every_adapter_code_this_port_may_publish_exists_in_the_adapter(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")

        for code in PORT.ADAPTER_CODES:
            self.assertIn(f'"{code}"', source, code)

    def test_the_transport_reply_never_carries_adapter_stderr_text(self) -> None:
        self.harness.behaviour = FOREIGN_REPLY
        self.harness.affected = {TRANSPORT.KIND_OBSERVE_BACKUP}
        request = TRANSPORT.build_request(
            self.target(),
            kind=TRANSPORT.KIND_OBSERVE_BACKUP,
            operation_id=self.backup_id,
        )

        reply = TRANSPORT.exchange(
            self.target(),
            request,
            ssh_executable=SSH_EXECUTABLE,
            timeout=60.0,
            process_factory=self.harness,
        )

        self.assertIsNone(reply.document)
        self.assertEqual(reply.effect, TRANSPORT.EFFECT_UNKNOWN)
        self.assertEqual(reply.code, TRANSPORT.CODE_REPLY_UNRECOGNISED)

    def test_neither_module_reaches_a_store_a_file_or_a_credential(self) -> None:
        for name in ("remote_update_backup_port.py", "remote_update_maintenance_transport.py"):
            lines = (SHELL / name).read_text(encoding="utf-8").splitlines()
            imports = [line for line in lines if line.startswith(("import ", "from "))]
            code = [line for line in lines if not line.lstrip().startswith("#")]

            for forbidden in ("shutil", "workstack.store", "workstack.maintenance", "os.environ"):
                self.assertFalse(
                    [line for line in imports if forbidden in line], f"{name}: {forbidden}"
                )
            for forbidden in ("open(", "rmtree", "unlink(", "session_token", "generate_session"):
                self.assertFalse(
                    [line for line in code if forbidden in line], f"{name}: {forbidden}"
                )


if __name__ == "__main__":
    unittest.main()
