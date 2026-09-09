"""The normal path, end to end, through the composed production flow.

One update attempt is driven from ``idle`` to ``update_ready`` over the flow
the host factory actually builds: the real ``RemoteUpdateFlow`` in the selected
code-first order, the real install/preview/probe/verification ports, the real
authenticated owner stop port, the real maintenance-backed backup port, the
real registry activation, and the real durable journal. Nothing in the flow,
the ports or the decisions is doubled.

**What is simulated, and what is not.**  Two things stand in, and both are
transport only:

* ``RemoteHost`` replaces the ``ssh`` child process.  It is a bounded
  in-process fixture holding what a Linux host would hold -- which application
  directories exist, what they contain, whether the owner is still running,
  which backups have been taken -- and it speaks each protocol's real document
  schema.  Every document it emits is parsed, admitted and judged by the same
  production code that would read a real remote's bytes.  **No SSH runs, no
  socket is opened to any host, and nothing outside this process is touched.**
* the desktop's own loopback origin is a real bounded ``http.server`` from the
  served-UI lane's own tests, so the served identity is a genuinely measured
  digest rather than an asserted one.

The registry, the activation receipts, the binding store, the journal and the
session record are all real files under a disposable state root, written by
production code and read back from disk.

**This is not a Linux run.**  It proves the composition, the ordering, the
identities and the recovery; it does not prove that a real remote host behaves
as this fixture does.  That remains an integration gate.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
TESTS = ROOT / "tests"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import connection_registry as REGISTRY  # noqa: E402
import remote_update_host_factory as FACTORY  # noqa: E402
import remote_update_host_recovery_gate as GATE  # noqa: E402
import remote_update_maintenance_transport as MAINTENANCE  # noqa: E402
import remote_update_owner_observation as OBSERVATION  # noqa: E402
import remote_verified_unpack as UNPACK  # noqa: E402
import remote_stop_result as STOP  # noqa: E402
from connection_registry_mutations import ConnectionRegistryMutationService  # noqa: E402
from remote_update_activation_adapter import RegistryActivationAdapter  # noqa: E402
from remote_update_flow_contract import StartupEvidence  # noqa: E402
from remote_update_install_ports_inspection import (  # noqa: E402
    MODE_VERIFIED_UNPACK,
    LoopbackSession,
)
from remote_update_journal import RemoteUpdateJournal  # noqa: E402
from workstack import REMOTE_PROTOCOL_VERSION, __version__  # noqa: E402

from tests.test_remote_update_host_factory import (  # noqa: E402
    ALIAS,
    SERVED_UI_HTML,
    ATTEMPT,
    CURRENT_APP,
    DATA_DIR,
    PROFILE_ID,
    SESSION_ID,
    TARGET_APP,
    WORKSPACE,
    DRIVER_TESTS,
    make_artifact,
    registry_of,
    remote_profile,
    served_metadata,
    ssh_profile,
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UI_TESTS = load_module(TESTS / "test_remote_ui_observation.py", "journey_ui_helpers")

REMOTE_STATE = "/fixture/driver-owner/.workstack-maintenance/state"
REMOTE_BACKUPS = "/fixture/driver-owner/.workstack-maintenance/backups"
BACKUP_DIGEST = "sha256:" + "3c" * 32
#: The digest the admitted manifest declares for the document the remote serves
#: at its root.  The loopback origin below serves exactly those bytes, so the
#: verification compares a measured digest against an admitted one.
EXPECTED_SERVED_UI = "sha256:" + hashlib.sha256(SERVED_UI_HTML).hexdigest()
SESSION_TOKEN = "fixture-session-token"


class Channel:
    """One stand-in child: at most one request in, exactly one reply out."""

    def __init__(self, host: "RemoteHost", command: list[str]) -> None:
        self._host = host
        self.command = command
        self.stdin = _Sink(self._deliver)
        payload = host.answer(command, None)
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(b"")
        self.returncode: int | None = None
        self._code = 0
        self.killed = False

    def _deliver(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(self._host.answer(self.command, payload))

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._code
        return self._code

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _Sink:
    def __init__(self, deliver) -> None:
        self._deliver = deliver
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._deliver(bytes(self.buffer))


def line(document: dict[str, object]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"


class RemoteHost:
    """A bounded in-process stand-in for the Linux host's SSH surface.

    It owns state, not verdicts: which application directories are there and
    what they contain, whether the owner is still running, and which backups
    exist. Each protocol answers in its own real schema.
    """

    def __init__(self, artifact, target_app_dir: str = TARGET_APP) -> None:
        self.artifact = artifact
        self.target_app_dir = target_app_dir
        self.apps: dict[str, dict[str, object]] = {
            CURRENT_APP: {
                "product": "1.0.8",
                "protocol": REMOTE_PROTOCOL_VERSION,
                "digest": "sha256:" + "11" * 32,
            }
        }
        self.owner_running = True
        self.backups: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self.stop_requests = 0
        self.unpack_operations: list[str] = []
        #: Set to make a placed candidate stop re-verifying, the way a tampered
        #: or half-written tree would.
        self.unpack_identity_broken = False

    # -- the two seams the production ports call ------------------------

    def process(self, command: list[str], **_kwargs: object) -> Channel:
        self.commands.append(list(command))
        return Channel(self, list(command))

    def run(self, command: list[str]) -> subprocess.CompletedProcess:
        """The owner port's injected command runner: stop, then observe."""

        self.commands.append(list(command))
        remote = command[-1]
        if OBSERVATION.OBSERVE_COMMAND in remote:
            return subprocess.CompletedProcess(command, 0, self._observation(), b"")
        self.stop_requests += 1
        self.owner_running = False
        return subprocess.CompletedProcess(command, 0, self._stop_result(), b"")

    # -- protocol dispatch ----------------------------------------------

    def answer(self, command: list[str], payload: bytes | None) -> bytes:
        remote = command[-1]
        if FACTORY.MAINTENANCE_RELATIVE in remote:
            return b"" if payload is None else self._maintenance(payload)
        operation = self._unpack_operation(remote)
        if operation is not None:
            return b"" if payload is None else self._unpack(operation, remote)
        return line(self._facts(self._app_dir_of(remote)))

    @staticmethod
    def _unpack_operation(remote: str) -> str | None:
        for operation in ("place", "inspect", "verify"):
            if f" {operation} --app-dir" in remote:
                return operation
        return None

    def _app_dir_of(self, remote: str) -> str:
        return self.target_app_dir if self.target_app_dir in remote else CURRENT_APP

    # -- provision inspection --------------------------------------------

    def _facts(self, app_dir: str) -> dict[str, object]:
        installed = self.apps.get(app_dir)
        if installed is None:
            return DRIVER_TESTS.facts_document(
                install_exists=False, workspace_id=WORKSPACE
            )
        return DRIVER_TESTS.facts_document(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=str(installed["product"]),
            install_protocol=int(installed["protocol"]),  # type: ignore[arg-type]
            install_digest=str(installed["digest"]),
            workspace_id=WORKSPACE,
        )

    # -- verified unpack --------------------------------------------------

    def _unpack(self, operation: str, remote: str) -> bytes:
        self.unpack_operations.append(operation)
        app_dir = self._app_dir_of(remote)
        if operation == "place":
            self.apps[app_dir] = {
                "product": self.artifact.product_version,
                "protocol": self.artifact.protocol_version,
                "digest": self.artifact.digest,
            }
        placed = app_dir in self.apps and not self.unpack_identity_broken
        if operation == "verify" and placed:
            # The frozen helper answers a re-verification with the nested
            # identity_verified document, not with the flat placement one.
            return line(
                {
                    "activation": "not_activated",
                    "atomic_directory_publish": False,
                    "method": "verified_unpack",
                    "outcome": UNPACK.OUTCOME_UNPACKED,
                    "placement": UNPACK.PLACEMENT_IDENTITY,
                    "receipt": self._unpack_receipt(app_dir),
                    "ssot_accessed": False,
                }
            )
        return line(
            {
                "schema_version": 1,
                "outcome": UNPACK.OUTCOME_UNPACKED if placed else UNPACK.OUTCOME_NOT_READY,
                "placement": UNPACK.PLACEMENT_READY if placed else UNPACK.PLACEMENT_ABSENT,
                "app_dir": app_dir,
                "artifact_digest": self.artifact.digest,
                "artifact_manifest_sha256": self.artifact.manifest_digest,
                "product_version": self.artifact.product_version,
                "remote_protocol_version": self.artifact.protocol_version,
            }
        )

    def _unpack_receipt(self, app_dir: str) -> dict[str, object]:
        return {
            "activation": "not_activated",
            "app_dir": app_dir,
            "artifact_digest": self.artifact.digest,
            "artifact_manifest_sha256": self.artifact.manifest_digest,
            "atomic_directory_publish": False,
            "files_verified": 1,
            "imports": "PASS",
            "method": "verified_unpack",
            "placement": UNPACK.PLACEMENT_READY,
            "product_version": self.artifact.product_version,
            "remote_protocol_version": self.artifact.protocol_version,
            "schema_version": 1,
            "ssot_accessed": False,
        }

    # -- authenticated owner stop ----------------------------------------

    def _stop_result(self) -> bytes:
        return line(
            {
                "schema_version": STOP.SCHEMA_VERSION,
                "code": STOP.STOP_CONFIRMED_EXIT,
                "state": "dead",
                "token_available": True,
                "pidfd_available": True,
                "process_exit": "verified",
            }
        )

    def _observation(self) -> bytes:
        released = "verified" if not self.owner_running else "failed"
        return line(
            {
                "schema_version": OBSERVATION.SCHEMA_VERSION,
                "binding": OBSERVATION.BINDING_BOUND,
                "owner_state": "dead" if not self.owner_running else "live",
                "token_available": True,
                "process_exit": "verified" if not self.owner_running else "unknown",
                "listener_release": released,
                "lease_release": released,
            }
        )

    # -- supported maintenance -------------------------------------------

    def _maintenance(self, payload: bytes) -> bytes:
        request = json.loads(payload.decode("utf-8"))
        kind = request["kind"]
        operation = request["operation_id"]
        if kind == MAINTENANCE.KIND_OBSERVE_BACKUP:
            digest = self.backups.get(operation)
            if digest is None:
                return self._reply("failed", code="backup_not_run", attempted=False)
            return self._reply("verified", digest=digest)
        if kind == MAINTENANCE.KIND_CREATE_BACKUP:
            self.backups[operation] = BACKUP_DIGEST
            return self._reply("verified", digest=BACKUP_DIGEST)
        if kind == MAINTENANCE.KIND_OBSERVE_RESTORE:
            return self._reply("failed", code="restore_not_run", attempted=False)
        return self._reply("failed", code="restore_not_run", attempted=False)

    @staticmethod
    def _reply(
        status: str,
        *,
        digest: str | None = None,
        code: str = "",
        attempted: bool = True,
    ) -> bytes:
        document: dict[str, object] = {
            "schema_version": MAINTENANCE.HELPER_SCHEMA_VERSION,
            "tool": MAINTENANCE.HELPER_TOOL,
            "status": status,
            "attempted": attempted,
        }
        if digest is not None:
            document["backup_digest"] = digest
        if code:
            document["code"] = code
        return line(document)


class JourneyLane:
    """A disposable desktop state root, a real registry, and one composed flow."""

    def __init__(self, directory: str, base_url: str) -> None:
        self.root = Path(directory)
        self.base_url = base_url
        self.artifact = make_artifact()
        self.remote = RemoteHost(self.artifact)
        REGISTRY.save_connection_registry(
            self.root, registry_of(PROFILE_ID, ssh_profile())
        )
        self.service = ConnectionRegistryMutationService(self.root, proof_ttl_seconds=60)
        self.adapter = RegistryActivationAdapter(self.root, mutation_service=self.service)
        # What the desktop persists between the two processes: the staging the
        # install port may re-verify, and the archive a restore is bound to.
        self.prepared = None
        self.restore_source = None
        #: Whether this lane's stand-in durable record accepts writes at all.
        self.writes = True

    def inputs(self, **overrides: object) -> FACTORY.RemoteUpdateHostInputs:
        fields: dict[str, object] = {
            "state_root": self.root,
            "profile": ssh_profile(),
            "remote_profile": remote_profile(),
            "owner": DRIVER_TESTS.OWNER,
            "session_token": SESSION_TOKEN,
            "session_id": SESSION_ID,
            "update_attempt_id": ATTEMPT,
            "artifact": self.artifact,
            "target_app_dir": TARGET_APP,
            "current_app_dir": CURRENT_APP,
            "remote_state_root": REMOTE_STATE,
            "remote_backup_root": REMOTE_BACKUPS,
            "ssh_executable": DRIVER_TESTS.SSH,
            "observe_current": lambda: LoopbackSession(
                base_url=self.base_url,
                workspace_id=WORKSPACE,
                desktop_version=__version__,
            ),
            "install_mode": MODE_VERIFIED_UNPACK,
            "process_factory": self.remote.process,
            "stop_runner": self.remote.run,
            "profile_probe": lambda _profile: served_metadata(),
            "mutation_service": self.service,
            "activation_adapter": self.adapter,
            "expected_served_ui_sha256": EXPECTED_SERVED_UI,
            "retained_preparation": self.prepared,
            "restore_source": self.restore_source,
            "on_prepared": self._remember_prepared,
            "on_restore_source": self._remember_restore_source,
        }
        fields.update(overrides)
        return FACTORY.RemoteUpdateHostInputs(**fields)  # type: ignore[arg-type]

    def _remember_prepared(self, retained) -> bool:
        """Stand in for the host's durable side record, in this lane only.

        Answers the way the host's own write answers: ``True`` means the
        evidence is on disk.  ``writes`` may be turned off to make every write
        fail, which is what the durability gate is supposed to notice.
        """

        if not self.writes:
            return False
        self.prepared = retained
        return True

    def _remember_restore_source(self, source) -> bool:
        if not self.writes:
            return False
        self.restore_source = source
        return True

    def compose(self, **overrides: object) -> FACTORY.ComposedRemoteUpdate:
        return FACTORY.build_remote_update_flow(self.inputs(**overrides))

    def live_app_dir(self) -> str:
        registry = REGISTRY.load_connection_registry(self.root)
        selected = next(
            profile
            for profile in registry.profiles
            if profile.profile_id == PROFILE_ID
        )
        return selected.remote_app_dir



class BrittleJournal:
    """The real journal, with the final anchor release made to fail on demand.

    Only the *clearing* record -- the empty one the flow writes when the update
    is whole -- is intercepted, and only while armed.  Everything else is the
    real journal's own behaviour, written to the same real file.
    """

    def __init__(self, journal) -> None:
        self._journal = journal
        self.fail_clear = False
        self.clears = 0
        self.refused = 0

    def load(self):
        return self._journal.load()

    def record(self, entries) -> None:
        if not tuple(entries):
            self.clears += 1
            if self.fail_clear:
                self.refused += 1
                raise RuntimeError("the journal anchor could not be released")
        self._journal.record(entries)


class DurableEvidenceLane(unittest.TestCase):
    """One attempt whose host-side record can be made to refuse writes."""

    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.server = stack.enter_context(UI_TESTS.running_server())
        self.server.workspace_id = WORKSPACE
        self.server.html = SERVED_UI_HTML
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        self.lane = JourneyLane(directory, UI_TESTS.base_url(self.server))

    def advance_to(self, flow, expected: str, limit: int = 8) -> str:
        codes: list[str] = []
        for _ in range(limit):
            snapshot = flow.advance()
            codes.append(snapshot.code)
            if snapshot.code == expected:
                return snapshot.code
        raise AssertionError(f"never reached {expected}; saw {codes}")


class DurableGateTests(DurableEvidenceLane):
    """Recovery evidence that did not land stops the next mutation, truthfully."""

    def test_an_unkept_preparation_reports_verified_and_still_blocks_the_stop(self) -> None:
        """The review's first counterexample, at the boundary it names.

        The staging really did verify on the remote, and the stage says so --
        restating it as a failure would be untrue about the remote.  What must
        not happen is the flow going on to stop the owner, back up, probe and
        move the registry while the evidence that makes the attempt
        recoverable is not on disk.
        """

        self.lane.writes = False
        composed = self.lane.compose()
        flow = composed.flow
        self.assertEqual(flow.advance().code, "preview_ready")
        # Truthful about the remote: the candidate really is staged.
        self.assertEqual(flow.advance().code, "prepare_ready")
        self.assertIn("place", self.lane.remote.unpack_operations)
        self.assertIsNone(self.lane.prepared)

        # ...and the next mutating stage is refused before it is issued.
        self.assertEqual(flow.advance().code, "stop_failed")
        self.assertEqual(self.lane.remote.stop_requests, 0)
        self.assertTrue(self.lane.remote.owner_running)
        self.assertEqual(self.lane.remote.backups, {})
        self.assertEqual(composed.gate.pending, (GATE.EVIDENCE_PREPARED,))

    def test_the_same_identity_persists_on_retry_and_lets_the_flow_continue(self) -> None:
        """The outstanding write is retried as itself, not as a new operation."""

        self.lane.writes = False
        composed = self.lane.compose()
        self.advance_to(composed.flow, "stop_failed")
        staged = composed.prepare.staged_operation_id
        self.assertIsNotNone(staged)

        self.lane.writes = True
        self.assertEqual(composed.flow.retry().code, "stop_verified")
        self.assertEqual(self.lane.remote.stop_requests, 1)
        self.assertIsNotNone(self.lane.prepared)
        # The recovered evidence names the staging that actually ran.
        self.assertEqual(self.lane.prepared.operation_id, staged)
        self.assertEqual(composed.gate.pending, ())

    def test_a_recording_callback_that_raises_is_a_write_that_did_not_land(self) -> None:
        def explode(_retained) -> bool:
            raise OSError("the state root is gone")

        composed = self.lane.compose(on_prepared=explode)
        self.advance_to(composed.flow, "prepare_ready")
        self.assertEqual(composed.flow.advance().code, "stop_failed")
        self.assertEqual(self.lane.remote.stop_requests, 0)

    def test_an_unkept_archive_reports_verified_and_still_blocks_the_probe(self) -> None:
        """The review's second write, at the first boundary that can see it.

        The archive becomes known during the backup, so the backup itself is
        already issued and really did verify.  The probe is the next thing that
        runs the new code, and it is what must not happen while the archive a
        restore would put back is unknown to the next process.
        """

        composed = self.lane.compose()
        self.advance_to(composed.flow, "prepare_ready")
        self.lane.writes = False
        self.assertEqual(composed.flow.advance().code, "stop_verified")
        self.assertEqual(composed.flow.advance().code, "backup_verified")
        self.assertIsNotNone(composed.backup.restore_source)
        self.assertIsNone(self.lane.restore_source)

        before = list(self.lane.remote.unpack_operations)
        self.assertEqual(composed.flow.advance().code, "probe_failed")
        self.assertEqual(self.lane.remote.unpack_operations, before)
        self.assertEqual(composed.gate.pending, (GATE.EVIDENCE_RESTORE_SOURCE,))

        self.lane.writes = True
        self.assertEqual(composed.flow.retry().code, "probe_verified")
        self.assertEqual(
            self.lane.restore_source.operation_id,
            composed.backup.restore_source.operation_id,
        )

    def test_nothing_activates_while_recovery_evidence_is_outstanding(self) -> None:
        composed = self.lane.compose()
        self.advance_to(composed.flow, "prepare_ready")
        self.lane.writes = False
        self.advance_to(composed.flow, "backup_verified")
        self.assertEqual(composed.flow.advance().code, "probe_failed")
        registry = REGISTRY.load_connection_registry(self.lane.root)
        selected = next(
            profile
            for profile in registry.profiles
            if profile.profile_id == PROFILE_ID
        )
        self.assertEqual(selected.remote_app_dir, CURRENT_APP)
        self.assertIsNone(composed.flow.retained_activation())


class CompletionOrderTests(DurableEvidenceLane):
    """The finished hook runs after the anchor is gone, or it does not run."""

    def compose(self, **overrides):
        journals: list[BrittleJournal] = []

        def factory(root, binding):
            journal = BrittleJournal(RemoteUpdateJournal(root, binding))
            journals.append(journal)
            return journal

        composed = self.lane.compose(journal_factory=factory, **overrides)
        return composed, journals[-1]

    def test_a_failed_anchor_release_never_reports_the_attempt_finished(self) -> None:
        """The root's counterexample: retiring the record before the clear.

        The side record is the only thing the next process can address the
        attempt with.  Retiring it while the journal still holds the activation
        anchor would leave that process an attempt it can name and no longer
        recover, so the hook has to be strictly behind the durable clear.
        """

        finished: list[object] = []
        composed, journal = self.compose(on_verified=lambda: finished.append(True))
        self.advance_to(composed.flow, "activate_pending_restart")
        composed.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=composed.flow.retained_activation().operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        journal.fail_clear = True
        with self.assertRaises(RuntimeError):
            composed.flow.advance()
        self.assertEqual(journal.refused, 1)
        self.assertEqual(finished, [])

    def test_a_released_anchor_reports_the_attempt_finished_once(self) -> None:
        finished: list[object] = []
        composed, journal = self.compose(on_verified=lambda: finished.append(True))
        self.advance_to(composed.flow, "activate_pending_restart")
        composed.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=composed.flow.retained_activation().operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        self.assertEqual(composed.flow.advance().code, "update_ready")
        self.assertEqual(finished, [True])

class NormalJourneyTests(unittest.TestCase):
    """One attempt, from the desktop's first click to a verified update."""

    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        # The served-UI lane's own bounded loopback server: a real HTTP origin
        # on 127.0.0.1, so the served identity is measured rather than asserted.
        self.server = stack.enter_context(UI_TESTS.running_server())
        self.server.workspace_id = WORKSPACE
        self.server.html = SERVED_UI_HTML
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        self.lane = JourneyLane(directory, UI_TESTS.base_url(self.server))

    def advance_to(self, flow, expected: str, limit: int = 8) -> str:
        codes: list[str] = []
        for _ in range(limit):
            snapshot = flow.advance()
            codes.append(snapshot.code)
            if snapshot.code == expected:
                return snapshot.code
        raise AssertionError(f"never reached {expected}; saw {codes}")

    def test_the_whole_normal_path_reaches_a_pending_activation(self) -> None:
        composed = self.lane.compose()
        flow = composed.flow
        self.assertEqual(flow.advance().code, "preview_ready")
        self.assertEqual(flow.advance().code, "prepare_ready")
        self.assertEqual(flow.advance().code, "stop_verified")
        self.assertEqual(flow.advance().code, "backup_verified")
        self.assertEqual(flow.advance().code, "probe_verified")
        self.assertEqual(flow.advance().code, "activate_pending_restart")
        # The candidate was staged before the owner was stopped, which is the
        # whole point of the selected order.
        self.assertEqual(self.lane.remote.unpack_operations[0], "place")
        self.assertEqual(self.lane.remote.stop_requests, 1)
        # The registry really moved to the prepared application.
        self.assertEqual(self.lane.live_app_dir(), TARGET_APP)

    def test_the_backup_ran_after_the_stop_and_before_the_probe(self) -> None:
        flow = self.lane.compose().flow
        self.advance_to(flow, "backup_verified")
        self.assertEqual(self.lane.remote.stop_requests, 1)
        self.assertFalse(self.lane.remote.owner_running)
        self.assertEqual(len(self.lane.remote.backups), 1)

    def test_the_verified_backup_identity_reaches_the_host_record(self) -> None:
        composed = self.lane.compose()
        self.advance_to(composed.flow, "backup_verified")
        source = composed.backup.restore_source
        self.assertIsNotNone(source)
        self.assertEqual(source.backup_digest, BACKUP_DIGEST)

    def test_nothing_activates_before_the_backup_is_verified(self) -> None:
        """A store that cannot be backed up never reaches the registry."""

        lane = self.lane
        original = lane.remote._maintenance

        def refuse(payload: bytes) -> bytes:
            request = json.loads(payload.decode("utf-8"))
            if request["kind"] == MAINTENANCE.KIND_CREATE_BACKUP:
                return RemoteHost._reply(
                    "failed", code="backup_target_invalid", attempted=False
                )
            return original(payload)

        lane.remote._maintenance = refuse  # type: ignore[method-assign]
        flow = lane.compose().flow
        self.advance_to(flow, "stop_verified")
        snapshot = flow.advance()
        self.assertEqual(snapshot.stage, "failed")
        self.assertEqual(snapshot.code, "backup_failed")
        self.assertEqual(lane.live_app_dir(), CURRENT_APP)

    def test_a_restart_recovers_the_retained_receipt_and_verifies(self) -> None:
        """The second process finds the pending activation and finishes it."""

        first = self.lane.compose()
        self.advance_to(first.flow, "activate_pending_restart")
        retained = first.flow.retained_activation()
        self.assertIsNotNone(retained)

        # A wholly new composition, as a restarted desktop builds one: same
        # attempt id, same journal file, nothing carried in memory.
        second = self.lane.compose()
        recovered = second.flow.retained_activation()
        self.assertEqual(recovered, retained)
        self.assertEqual(second.flow.snapshot().code, "activate_pending_restart")

        evidence = StartupEvidence(
            selected_activation_id=retained.operation_id,
            authority_selected="verified",
            workspace_match="verified",
        )
        self.assertEqual(
            second.flow.resume_after_restart(evidence).code, "activate_committed"
        )
        self.assertEqual(second.flow.advance().code, "update_ready")

    def test_an_unobserved_restart_leaves_the_receipt_pending(self) -> None:
        composed = self.lane.compose()
        self.advance_to(composed.flow, "activate_pending_restart")
        retained = composed.flow.retained_activation()
        resumed = composed.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=retained.operation_id,
                authority_selected="unknown",
                workspace_match="unknown",
            )
        )
        self.assertEqual(resumed.code, "activate_restart_not_selected")
        self.assertEqual(composed.flow.retained_activation(), retained)

    def test_a_restart_that_selected_another_workspace_fails_closed(self) -> None:
        composed = self.lane.compose()
        self.advance_to(composed.flow, "activate_pending_restart")
        retained = composed.flow.retained_activation()
        resumed = composed.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=retained.operation_id,
                authority_selected="verified",
                workspace_match="failed",
            )
        )
        self.assertEqual(resumed.code, "activate_workspace_mismatch")

    def test_the_journal_is_a_real_file_the_next_process_reads(self) -> None:
        composed = self.lane.compose()
        self.advance_to(composed.flow, "activate_pending_restart")
        path = RemoteUpdateJournal(self.lane.root, composed.journal.binding).path
        self.assertTrue(path.is_file())
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["binding"]["update_attempt_id"], ATTEMPT)
        self.assertTrue(document["records"])

    def test_the_verification_measures_the_served_identity(self) -> None:
        composed = self.lane.compose()
        self.advance_to(composed.flow, "activate_pending_restart")
        retained = composed.flow.retained_activation()
        composed.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=retained.operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        snapshot = composed.flow.advance()
        self.assertEqual(snapshot.code, "update_ready")
        self.assertEqual(snapshot.versions["remote"], self.lane.artifact.product_version)
        self.assertIsNotNone(snapshot.versions["served_ui"])

    def test_the_activation_was_proven_by_the_candidates_own_identity(self) -> None:
        seen: list[str] = []
        lane = self.lane
        composed = lane.compose(
            profile_probe=lambda profile: (
                seen.append(profile.remote_app_dir) or served_metadata()
            )
        )
        self.advance_to(composed.flow, "activate_pending_restart")
        self.assertEqual(seen, [TARGET_APP])
        self.assertIsNotNone(composed.activation.last_candidate_probe)

    def test_a_candidate_that_serves_another_workspace_never_activates(self) -> None:
        composed = self.lane.compose(
            profile_probe=lambda _profile: served_metadata(
                "22222222-2222-4222-8222-222222222222"
            )
        )
        self.advance_to(composed.flow, "probe_verified")
        snapshot = composed.flow.advance()
        self.assertEqual(snapshot.stage, "failed")
        self.assertEqual(snapshot.code, "activate_failed")
        self.assertEqual(self.lane.live_app_dir(), CURRENT_APP)

    def test_a_remote_serving_other_bytes_is_not_a_verified_update(self) -> None:
        """The served identity is compared to the ADMITTED manifest digest.

        A well-formed root document that is not the one this bundle declares
        fails the verification rather than passing it because some digest was
        measurable.
        """

        composed = self.lane.compose()
        self.advance_to(composed.flow, "activate_pending_restart")
        retained = composed.flow.retained_activation()
        composed.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=retained.operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        self.server.html = b"<!doctype html><html><body>some other build</body></html>"
        snapshot = composed.flow.advance()
        self.assertEqual(snapshot.stage, "failed")
        self.assertEqual(snapshot.code, "verify_failed")

    def test_the_restart_reads_the_candidate_back_without_staging_again(self) -> None:
        """Recovery re-verifies the placed candidate; it never re-places it."""

        first = self.lane.compose()
        self.advance_to(first.flow, "activate_pending_restart")
        retained = first.flow.retained_activation()
        self.assertIsNotNone(self.lane.prepared)

        second = self.lane.compose()
        before = list(self.lane.remote.unpack_operations)
        second.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=retained.operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        self.assertEqual(second.flow.advance().code, "update_ready")
        after = self.lane.remote.unpack_operations[len(before):]
        self.assertIn("verify", after)
        self.assertNotIn("place", after)
        record = second.install._record  # noqa: SLF001 - the admitted record is the subject
        self.assertEqual(record.operation_id, self.lane.prepared.operation_id)
        self.assertEqual(record.prepare.status, "verified")

    def test_a_tampered_candidate_is_not_readable_after_the_restart(self) -> None:
        first = self.lane.compose()
        self.advance_to(first.flow, "activate_pending_restart")
        retained = first.flow.retained_activation()
        second = self.lane.compose()
        self.lane.remote.unpack_identity_broken = True
        second.flow.resume_after_restart(
            StartupEvidence(
                selected_activation_id=retained.operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        snapshot = second.flow.advance()
        self.assertEqual(snapshot.stage, "failed")
        self.assertIsNone(second.install._record)  # noqa: SLF001 - nothing fabricated

    def test_no_ssh_child_process_is_ever_created(self) -> None:
        """Every remote exchange went to the in-process fixture, not to ssh."""

        composed = self.lane.compose()
        self.advance_to(composed.flow, "activate_pending_restart")
        self.assertTrue(self.lane.remote.commands)
        for command in self.lane.remote.commands:
            self.assertEqual(command[0], DRIVER_TESTS.SSH)
            self.assertIn(ALIAS, command)
        self.assertTrue(
            any(DATA_DIR in command[-1] for command in self.lane.remote.commands)
        )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
