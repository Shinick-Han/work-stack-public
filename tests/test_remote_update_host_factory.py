"""The production composition, and the registry activation it really performs.

Two halves, both against real code.

``build_remote_update_flow`` is exercised as the host calls it: real
``RemoteUpdateInstallPorts``, real ``RemoteOwnerStopPort``, real
``RemoteMaintenanceBackupPort``, real ``RemoteUpdateJournal`` and a real
``RemoteUpdateFlow`` in the explicitly selected code-first order.  What is
asserted is that the object graph is that graph -- no stub port, no simulated
flow -- and that every missing input is a refusal rather than a partial
construction.

``RegistryFlowActivationPort`` is exercised against a disposable desktop state
root holding a real ``connection-registry.json``, the real
``ConnectionRegistryMutationService`` and the real
``RegistryActivationAdapter``.  Activation really writes a receipt and really
moves the registry's selection, and the proof it does that on is minted from
the candidate's own served identity: a probe reporting another workspace, or
anything short of ``ready``, refuses with the registry byte-for-byte unchanged.

Nothing here opens a socket, runs ``ssh``, reads a credential or touches a
live store.  The SSH transport is a simulated stand-in (``process_factory``)
and the candidate identity probe is an injected callable; both are declared as
substitutes, and every decision they feed is made by production code.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
TESTS = ROOT / "tests"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import connection_registry as REGISTRY  # noqa: E402
import remote_update_host_factory as FACTORY  # noqa: E402
import remote_update_host_factory_activation as ACTIVATION  # noqa: E402
from connection_registry import ConnectionRegistry, SshConnectionProfile  # noqa: E402
from connection_registry_mutations import (  # noqa: E402
    ConnectionRegistryMutationService,
    current_registry_snapshot,
)
from profile_inspection import SshProfileMetadata  # noqa: E402
from remote_update_activation_adapter import RegistryActivationAdapter  # noqa: E402
from remote_update_backup_port import (  # noqa: E402
    RemoteMaintenanceBackupPort,
    RestoreSource,
)
from remote_update_flow import RemoteUpdateFlow  # noqa: E402
from remote_update_flow_contract import (  # noqa: E402
    ACTION_PREPARE_CODE_ONLY,
    PortRefusal,
    PrepareOutcome,
)
from remote_provision_driver import select_artifact  # noqa: E402
from remote_update_install_ports import RemoteUpdateInstallPorts  # noqa: E402
from remote_update_install_ports_inspection import (  # noqa: E402
    MODE_TRANSACTIONAL,
    MODE_VERIFIED_UNPACK,
    LoopbackSession,
)
from remote_update_journal import RemoteUpdateJournal  # noqa: E402
from remote_update_owner_port import RemoteOwnerStopPort  # noqa: E402
from ssot_connection import RemoteConnectionProfile  # noqa: E402
from workstack import REMOTE_PROTOCOL_VERSION, __version__  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DRIVER_TESTS = load_module(TESTS / "test_remote_provision_driver.py", "host_factory_driver_fakes")
# The real archive/sidecar builder the installer's own tests use.  The host now
# reads helper capability and the served root digest out of the ADMITTED
# manifest, so this lane needs a genuine zip with a genuine manifest, not a
# synthetic byte string that only satisfies the sidecar arithmetic.
INSTALLER_TESTS = load_module(
    TESTS / "test_remote_provision_installer.py", "host_factory_installer_fakes"
)

PROFILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_PROFILE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE = "22222222-2222-4222-8222-222222222222"
ALIAS = "fixture-remote"
CURRENT_APP = "/fixture/driver-owner/app"
TARGET_APP = "/fixture/driver-owner/app-1.2.3"
DATA_DIR = "/fixture/driver-owner/data"
REMOTE_PYTHON = "/fixture/driver-owner/opt/python"
SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ATTEMPT = "attempt-0001"
OPERATION = "op-activation-0001"

MAX_FILE_LINES = 800
MAX_FUNCTION_LINES = 100
MAX_CCN = 15
PRODUCTION = (
    SHELL / "remote_update_host_factory.py",
    SHELL / "remote_update_host_recovery_gate.py",
    SHELL / "remote_update_host_factory_activation.py",
    SHELL / "remote_update_host_surface.py",
    SHELL / "remote_update_host_surface_record.py",
    SHELL / "remote_update_host_surface_view.py",
)

#: The document a helper-bearing bundle declares for the remote's own root.
SERVED_UI_HTML = b"<!doctype html><html><body>work stack remote</body></html>\n"


def bundle_blobs(*, helpers: bool = True, served: bool = True) -> dict[str, bytes]:
    """The payload of a Linux bundle, with or without this release's helpers."""

    blobs = dict(INSTALLER_TESTS.PAYLOAD_FILES)
    if helpers:
        for relative in FACTORY.REQUIRED_HELPERS:
            blobs[relative] = ("# %s\n" % relative).encode("ascii")
    if served:
        blobs[FACTORY.SERVED_UI_RELATIVE] = SERVED_UI_HTML
    return blobs


def bundle_bytes(**kwargs: object) -> tuple[bytes, bytes]:
    return INSTALLER_TESTS.make_artifact(blobs=bundle_blobs(**kwargs))


def make_artifact(**kwargs: object):
    """One real, helper-bearing artifact, admitted the way the host admits it."""

    return select_artifact(*bundle_bytes(**kwargs))


def install_root_with_bundle(root: Path, **kwargs: object) -> Path:
    """Write the published pair this installation offers, at its exact name."""

    archive, sidecar = FACTORY.bundle_paths(root, __version__)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive_bytes, sidecar_bytes = bundle_bytes(**kwargs)
    archive.write_bytes(archive_bytes)
    sidecar.write_bytes(sidecar_bytes)
    return root


def complexity(function: ast.AST) -> int:
    score = 1
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(0, len(node.values) - 1)
        elif isinstance(node, ast.Try):
            score += len(node.handlers) + int(bool(node.orelse))
        elif isinstance(node, ast.Match):
            score += max(0, len(node.cases) - 1)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(generator.ifs) for generator in node.generators)
    return score


def ssh_profile(**overrides: object) -> SshConnectionProfile:
    fields: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "label": "Fixture remote",
        "ssh_host_alias": ALIAS,
        "remote_app_dir": CURRENT_APP,
        "remote_data_dir": DATA_DIR,
        "expected_workspace_id": WORKSPACE,
        "preferred_forward_port": 18765,
        "remote_python": REMOTE_PYTHON,
    }
    fields.update(overrides)
    return SshConnectionProfile(**fields)  # type: ignore[arg-type]


def remote_profile(**overrides: object) -> RemoteConnectionProfile:
    fields: dict[str, object] = {
        "ssh_host_alias": ALIAS,
        "remote_app_dir": CURRENT_APP,
        "remote_data_dir": DATA_DIR,
        "local_forward_port": 18765,
        "workspace_id": WORKSPACE,
        "remote_python": REMOTE_PYTHON,
    }
    fields.update(overrides)
    return RemoteConnectionProfile(**fields)  # type: ignore[arg-type]


def registry_of(active: str, *profiles: SshConnectionProfile) -> ConnectionRegistry:
    return ConnectionRegistry(
        schema_version=REGISTRY.REGISTRY_SCHEMA_VERSION,
        active_profile_id=active,
        profiles=tuple(profiles),
    )


def served_metadata(workspace: str = WORKSPACE) -> SshProfileMetadata:
    return SshProfileMetadata(
        actual_workspace_id=workspace,
        product_version=__version__,
        protocol_version=REMOTE_PROTOCOL_VERSION,
    )


class ScriptedSsh:
    """A simulated SSH transport: records argv, replays queued answers.

    This is a *substitute for the transport only*.  Nothing it returns is a
    decision; every document it emits is read and judged by the same production
    code that would read a real remote's bytes.
    """

    def __init__(self, *processes) -> None:
        self.queue = list(processes)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object):
        self.commands.append(list(command))
        if not self.queue:
            raise AssertionError("more SSH exchanges than this test allowed")
        return self.queue.pop(0)


def host_inputs(root: Path, **overrides: object) -> FACTORY.RemoteUpdateHostInputs:
    fields: dict[str, object] = {
        "state_root": root,
        "profile": ssh_profile(),
        "remote_profile": remote_profile(),
        "owner": DRIVER_TESTS.OWNER,
        "session_token": "session-token-fixture",
        "session_id": SESSION_ID,
        "update_attempt_id": ATTEMPT,
        "artifact": make_artifact(),
        "target_app_dir": TARGET_APP,
        "current_app_dir": CURRENT_APP,
        "remote_state_root": "/fixture/driver-owner/.workstack-maintenance/state",
        "remote_backup_root": "/fixture/driver-owner/.workstack-maintenance/backups",
        "ssh_executable": DRIVER_TESTS.SSH,
        "observe_current": lambda: LoopbackSession(workspace_id=WORKSPACE),
        "install_mode": MODE_VERIFIED_UNPACK,
        "process_factory": ScriptedSsh(),
        "profile_probe": lambda _profile: served_metadata(),
    }
    fields.update(overrides)
    return FACTORY.RemoteUpdateHostInputs(**fields)  # type: ignore[arg-type]


class StructuralTests(unittest.TestCase):
    def test_new_production_stays_inside_repo_metrics(self) -> None:
        for path in PRODUCTION:
            source = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), MAX_FILE_LINES, path.name)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                with self.subTest(file=path.name, function=node.name):
                    self.assertLessEqual(length, MAX_FUNCTION_LINES)
                    self.assertLessEqual(complexity(node), MAX_CCN)


class CompositionTests(unittest.TestCase):
    """The factory builds the real graph, in the real prepare-first order."""

    def build(self, **overrides: object) -> FACTORY.ComposedRemoteUpdate:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return FACTORY.build_remote_update_flow(
            host_inputs(Path(directory.name), **overrides)
        )

    def test_every_port_is_the_production_adapter(self) -> None:
        composed = self.build()
        self.assertIsInstance(composed.flow, RemoteUpdateFlow)
        self.assertIsInstance(composed.install, RemoteUpdateInstallPorts)
        self.assertIsInstance(composed.owner, RemoteOwnerStopPort)
        self.assertIsInstance(composed.backup, RemoteMaintenanceBackupPort)
        self.assertIsInstance(composed.activation, ACTIVATION.RegistryFlowActivationPort)
        self.assertIsInstance(composed.journal, RemoteUpdateJournal)
        ports = composed.flow._ports  # noqa: SLF001 - the composition is the subject
        self.assertIsInstance(ports.prepare, FACTORY.CodeOnlyStagingPorts)
        self.assertIs(ports.preview, ports.prepare)
        self.assertIs(ports.probe, ports.prepare)
        self.assertIs(ports.verification, ports.prepare)
        self.assertIs(ports.owner, composed.owner)
        self.assertIs(ports.backup, composed.backup)
        self.assertIs(ports.activation, composed.activation)

    def test_the_flow_runs_the_selected_code_first_order(self) -> None:
        composed = self.build()
        order = composed.flow._order  # noqa: SLF001 - the selected order is the subject
        self.assertTrue(order.prepare_before_stop)
        self.assertLess(order.prepare, order.backup)
        self.assertIn(ACTION_PREPARE_CODE_ONLY, set(order.actions.values()))
        self.assertEqual(composed.flow.snapshot().stage, "idle")

    def test_the_page_is_offered_one_session_and_one_workspace(self) -> None:
        composed = self.build()
        self.assertEqual(composed.session_id, SESSION_ID)
        self.assertEqual(composed.workspace_id, WORKSPACE)
        self.assertEqual(composed.profile_id, PROFILE_ID)
        self.assertEqual(composed.target_app_dir, TARGET_APP)

    def test_the_verified_backup_source_is_carried_into_the_port(self) -> None:
        source = RestoreSource(operation_id="backup-op", backup_digest="sha256:" + "a" * 64)
        composed = self.build(restore_source=source)
        self.assertEqual(composed.backup.restore_source, source)

    def test_the_journal_is_bound_to_this_attempt(self) -> None:
        composed = self.build()
        binding = composed.journal.binding
        self.assertEqual(binding.workspace_id, WORKSPACE)
        self.assertEqual(binding.profile_id, PROFILE_ID)
        self.assertEqual(binding.update_attempt_id, ATTEMPT)


class RefusalTests(unittest.TestCase):
    """Each missing input refuses; none of them yields a half-built flow."""

    def refuse(self, **overrides: object) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.build_remote_update_flow(
                host_inputs(Path(directory.name), **overrides)
            )
        return caught.exception.code

    def test_a_local_selection_is_not_a_remote_update(self) -> None:
        self.assertEqual(self.refuse(profile=None), FACTORY.REFUSED_NO_PROFILE)

    def test_no_session_token_refuses_before_anything_is_built(self) -> None:
        self.assertEqual(self.refuse(session_token=""), FACTORY.REFUSED_NO_TOKEN)

    def test_no_admitted_artifact_refuses(self) -> None:
        self.assertEqual(self.refuse(artifact=None), FACTORY.REFUSED_NO_ARTIFACT)

    def test_a_workspace_the_runtime_does_not_serve_refuses(self) -> None:
        self.assertEqual(
            self.refuse(remote_profile=remote_profile(workspace_id=OTHER_WORKSPACE)),
            FACTORY.REFUSED_INPUTS,
        )

    def test_a_target_inside_the_current_application_refuses(self) -> None:
        self.assertEqual(
            self.refuse(target_app_dir=f"{CURRENT_APP}/next"), FACTORY.REFUSED_TARGET
        )

    def test_a_target_inside_the_store_refuses(self) -> None:
        self.assertEqual(
            self.refuse(target_app_dir=f"{DATA_DIR}/next"), FACTORY.REFUSED_TARGET
        )

    def test_a_transactional_install_cannot_be_the_code_first_prepare(self) -> None:
        self.assertEqual(
            self.refuse(install_mode=MODE_TRANSACTIONAL), FACTORY.REFUSED_MODE
        )

    def test_an_unreadable_journal_refuses_rather_than_starting_fresh(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "remote-update-journal.json").write_bytes(b"{ not json")
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.build_remote_update_flow(host_inputs(root))
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_JOURNAL)


class CodeOnlyStagingTests(unittest.TestCase):
    """The bridge to the staging entry point install-ports does not yet expose."""

    def ports(self, mode: str = MODE_VERIFIED_UNPACK) -> RemoteUpdateInstallPorts:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        inputs = host_inputs(Path(directory.name), install_mode=mode)
        return FACTORY._install_ports(inputs, WORKSPACE)  # noqa: SLF001

    def test_code_only_staging_is_the_real_ports_own_prepare(self) -> None:
        real = self.ports()
        bridge = FACTORY.CodeOnlyStagingPorts(real)
        self.assertIs(bridge.ports, real)
        issued: list[str] = []

        def record(operation_id: str):
            issued.append(operation_id)
            return PrepareOutcome(status="unknown")

        real.prepare = record  # type: ignore[method-assign]
        bridge.prepare_code_only("stage-op")
        self.assertEqual(issued, ["stage-op"])
        self.assertEqual(bridge.staged_operation_id, "stage-op")

    def test_it_refuses_to_pretend_a_transactional_install_is_code_only(self) -> None:
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.CodeOnlyStagingPorts(self.ports(MODE_TRANSACTIONAL))
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_MODE)

    def test_every_other_call_reaches_the_real_ports_unchanged(self) -> None:
        real = self.ports()
        bridge = FACTORY.CodeOnlyStagingPorts(real)
        for name in ("prepare", "observe", "describe", "probe", "verify"):
            self.assertTrue(callable(getattr(bridge, name)), name)
        self.assertEqual(bridge.ports.inputs.target_app_dir, TARGET_APP)


class ActivationLane:
    """A disposable state root with a real registry and the real services."""

    def __init__(self, directory: str, *, workspace: str = WORKSPACE) -> None:
        self.root = Path(directory)
        self.profile = ssh_profile(expected_workspace_id=workspace)
        self.other = ssh_profile(
            profile_id=OTHER_PROFILE_ID,
            label="Another remote",
            ssh_host_alias="fixture-other",
            remote_app_dir="/fixture/other/app",
            remote_data_dir="/fixture/other/data",
            expected_workspace_id=OTHER_WORKSPACE,
            preferred_forward_port=18766,
        )
        REGISTRY.save_connection_registry(
            self.root, registry_of(PROFILE_ID, self.profile, self.other)
        )
        self.service = ConnectionRegistryMutationService(self.root, proof_ttl_seconds=60)
        self.adapter = RegistryActivationAdapter(self.root, mutation_service=self.service)

    def port(self, probe=None, *, target: str = TARGET_APP, workspace: str = WORKSPACE):
        return ACTIVATION.RegistryFlowActivationPort(
            self.root,
            profile_id=PROFILE_ID,
            target_app_dir=target,
            expected_workspace_id=workspace,
            adapter=self.adapter,
            mutation_service=self.service,
            probe=probe or (lambda _profile: served_metadata()),
        )

    def live(self) -> ConnectionRegistry:
        return REGISTRY.load_connection_registry(self.root)

    def raw(self) -> bytes:
        return (self.root / "connection-registry.json").read_bytes()


class RegistryActivationTests(unittest.TestCase):
    """The activation really moves the registry, and only on a served identity."""

    def lane(self) -> ActivationLane:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return ActivationLane(directory.name)

    def test_activation_writes_a_pending_receipt_and_selects_the_candidate(self) -> None:
        lane = self.lane()
        outcome = lane.port().activate(OPERATION)
        self.assertEqual(outcome.status, "verified")
        self.assertIs(outcome.committed, True)
        self.assertEqual(outcome.state, "pending")
        self.assertIs(outcome.restart_required, True)
        live = lane.live()
        self.assertEqual(live.active_profile_id, PROFILE_ID)
        selected = next(
            profile for profile in live.profiles if profile.profile_id == PROFILE_ID
        )
        self.assertEqual(selected.remote_app_dir, TARGET_APP)
        self.assertEqual(selected.expected_workspace_id, WORKSPACE)
        self.assertEqual(selected.remote_data_dir, DATA_DIR)

    def test_the_proof_comes_from_the_candidate_and_not_the_current_app(self) -> None:
        lane = self.lane()
        seen: list[str] = []

        def probe(profile: SshConnectionProfile) -> SshProfileMetadata:
            seen.append(profile.remote_app_dir)
            return served_metadata()

        lane.port(probe).activate(OPERATION)
        self.assertEqual(seen, [TARGET_APP])

    def test_a_candidate_serving_another_workspace_writes_nothing(self) -> None:
        lane = self.lane()
        before = lane.raw()
        with self.assertRaises(PortRefusal) as caught:
            lane.port(lambda _profile: served_metadata(OTHER_WORKSPACE)).activate(OPERATION)
        self.assertEqual(caught.exception.code, ACTIVATION.REFUSED_IDENTITY)
        self.assertEqual(lane.raw(), before)

    def test_an_unreachable_candidate_writes_nothing(self) -> None:
        lane = self.lane()
        before = lane.raw()

        def refuse(_profile: SshConnectionProfile) -> SshProfileMetadata:
            raise RuntimeError("the candidate did not answer")

        with self.assertRaises(PortRefusal) as caught:
            lane.port(refuse).activate(OPERATION)
        self.assertEqual(caught.exception.code, ACTIVATION.REFUSED_PROBE)
        self.assertEqual(lane.raw(), before)

    def test_a_target_that_is_the_current_application_is_not_an_update(self) -> None:
        lane = self.lane()
        before = lane.raw()
        with self.assertRaises(PortRefusal) as caught:
            lane.port(target=CURRENT_APP).activate(OPERATION)
        self.assertEqual(caught.exception.code, ACTIVATION.REFUSED_TARGET)
        self.assertEqual(lane.raw(), before)

    def test_confirming_puts_the_activation_in_force(self) -> None:
        lane = self.lane()
        lane.port().activate(OPERATION)
        confirmed = lane.port().confirm(OPERATION)
        self.assertEqual(confirmed.status, "verified")
        self.assertIs(confirmed.committed, True)
        self.assertEqual(confirmed.state, "confirmed")

    def test_the_host_can_read_which_operation_the_registry_selects(self) -> None:
        lane = self.lane()
        port = lane.port()
        port.activate(OPERATION)
        observed = port.observe_receipt(OPERATION)
        self.assertEqual(observed.selection, "selected")
        self.assertEqual(observed.profile_id, PROFILE_ID)

    def test_observing_an_operation_that_never_issued_writes_nothing(self) -> None:
        """An absent binding is a refusal, never an identity to reconcile."""

        lane = self.lane()
        before = lane.raw()
        with self.assertRaises(PortRefusal) as caught:
            lane.port().observe(OPERATION)
        self.assertEqual(caught.exception.code, "binding_absent")
        self.assertEqual(lane.raw(), before)

    def test_observing_an_issued_activation_issues_nothing(self) -> None:
        lane = self.lane()
        port = lane.port()
        port.activate(OPERATION)
        after_activation = lane.raw()
        outcome = port.observe(OPERATION)
        self.assertEqual(outcome.state, "pending")
        self.assertEqual(lane.raw(), after_activation)

    def test_rollback_puts_the_previous_selection_back(self) -> None:
        lane = self.lane()
        port = lane.port()
        port.activate(OPERATION)
        rolled = port.rollback(OPERATION)
        self.assertEqual(rolled.status, "verified")
        self.assertIs(rolled.previous_activation_selected, True)
        selected = next(
            profile
            for profile in lane.live().profiles
            if profile.profile_id == PROFILE_ID
        )
        self.assertEqual(selected.remote_app_dir, CURRENT_APP)


class CandidateProjectionTests(unittest.TestCase):
    """The small decisions the activation is built from, on their own."""

    def test_only_the_application_directory_moves(self) -> None:
        prepared = ACTIVATION.candidate_profile(ssh_profile(), TARGET_APP)
        self.assertEqual(prepared.remote_app_dir, TARGET_APP)
        self.assertEqual(
            replace(prepared, remote_app_dir=CURRENT_APP), ssh_profile()
        )

    def test_a_disabled_profile_is_not_a_candidate(self) -> None:
        registry = registry_of(PROFILE_ID, ssh_profile(enabled=False))
        with self.assertRaises(ACTIVATION.CandidateProbeRefused):
            ACTIVATION.candidate_registry(
                registry, ACTIVATION.candidate_profile(ssh_profile(), TARGET_APP)
            )

    def test_a_ready_answer_for_another_workspace_is_not_an_identity(self) -> None:
        from profile_inspection import ProfileTestResult

        result = ProfileTestResult(
            profile_id=PROFILE_ID,
            kind="ssh",
            status="ready",
            actual_workspace_id=OTHER_WORKSPACE,
            product_version=__version__,
            protocol_version=REMOTE_PROTOCOL_VERSION,
        )
        with self.assertRaises(ACTIVATION.CandidateProbeRefused):
            ACTIVATION.require_served_identity(result, WORKSPACE)

    def test_a_preparation_receipt_is_not_a_probe(self) -> None:
        receipt = {"outcome": "installed", "artifact_digest": "sha256:" + "0" * 64}
        with self.assertRaises(ACTIVATION.CandidateProbeRefused) as caught:
            ACTIVATION.require_served_identity(receipt, WORKSPACE)
        self.assertEqual(caught.exception.code, ACTIVATION.REFUSED_PROBE)


class BundleTests(unittest.TestCase):
    """Selecting the installed Linux bundle through the existing admission."""

    def test_the_candidate_directory_is_a_sibling_named_by_version(self) -> None:
        self.assertEqual(
            FACTORY.candidate_app_dir(CURRENT_APP, "1.2.3"), f"{CURRENT_APP}-1.2.3"
        )

    def test_the_maintenance_helper_runs_from_the_staged_tree(self) -> None:
        self.assertEqual(
            FACTORY.maintenance_helper_path(TARGET_APP),
            f"{TARGET_APP}/{FACTORY.MAINTENANCE_RELATIVE}",
        )

    def test_bundle_paths_name_the_published_artifact(self) -> None:
        archive, sidecar = FACTORY.bundle_paths(Path("C:/install"), "1.2.3")
        self.assertEqual(archive.name, "WorkStack-Linux-1.2.3-cp312-manylinux_2_17_x86_64.zip")
        self.assertEqual(sidecar.suffix, ".json")
        self.assertEqual(archive.parent.name, FACTORY.BUNDLE_DIRECTORY)

    def test_an_absent_bundle_is_no_capability_rather_than_an_error(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.admitted_bundle(root / "absent.zip", root / "absent.json")
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_NO_ARTIFACT)

    def written(self, **kwargs: object) -> tuple[Path, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        archive = root / "bundle.zip"
        sidecar = root / "bundle.json"
        archive_bytes, sidecar_bytes = bundle_bytes(**kwargs)
        archive.write_bytes(archive_bytes)
        sidecar.write_bytes(sidecar_bytes)
        return archive, sidecar

    def test_a_real_bundle_is_admitted_through_the_existing_selection(self) -> None:
        archive, sidecar = self.written()
        admitted = FACTORY.admitted_bundle(archive, sidecar)
        self.assertEqual(admitted.artifact.digest, select_artifact(
            archive.read_bytes(), sidecar.read_bytes()
        ).digest)
        self.assertEqual(admitted.artifact.product_version, __version__)

    def test_the_expected_served_digest_comes_from_the_admitted_manifest(self) -> None:
        """The verification compares against the bundle's own root document."""

        archive, sidecar = self.written()
        admitted = FACTORY.admitted_bundle(archive, sidecar)
        expected = "sha256:" + hashlib.sha256(SERVED_UI_HTML).hexdigest()
        self.assertEqual(admitted.expected_served_ui_sha256, expected)

    def test_the_same_version_without_the_helpers_is_still_refused(self) -> None:
        """Product version is not a capability, and this is the counterexample.

        The bundle carries this desktop's exact declared version and is
        otherwise entirely valid; what it does not carry is the helpers the
        staged tree has to execute from. A version comparison would offer the
        page and discover the absent executable halfway through a stop.
        """

        archive, sidecar = self.written(helpers=False)
        self.assertEqual(
            select_artifact(archive.read_bytes(), sidecar.read_bytes()).product_version,
            __version__,
        )
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.admitted_bundle(archive, sidecar)
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_BUNDLE_INCOMPATIBLE)

    def test_each_required_helper_is_checked_on_its_own(self) -> None:
        for missing in FACTORY.REQUIRED_HELPERS:
            blobs = bundle_blobs()
            del blobs[missing]
            archive_bytes, sidecar_bytes = INSTALLER_TESTS.make_artifact(blobs=blobs)
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            root = Path(directory.name)
            (root / "bundle.zip").write_bytes(archive_bytes)
            (root / "bundle.json").write_bytes(sidecar_bytes)
            with self.subTest(missing=missing):
                with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
                    FACTORY.admitted_bundle(root / "bundle.zip", root / "bundle.json")
                self.assertEqual(
                    caught.exception.code, FACTORY.REFUSED_BUNDLE_INCOMPATIBLE
                )

    def test_a_bundle_with_no_served_root_document_is_refused(self) -> None:
        archive, sidecar = self.written(served=False)
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.admitted_bundle(archive, sidecar)
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_BUNDLE_NO_SERVED_UI)

    def test_a_sidecar_that_does_not_describe_the_archive_is_refused(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        archive = root / "bundle.zip"
        sidecar = root / "bundle.json"
        archive_bytes, sidecar_bytes = bundle_bytes()
        archive.write_bytes(archive_bytes + b"tampered")
        sidecar.write_bytes(sidecar_bytes)
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as caught:
            FACTORY.admitted_bundle(archive, sidecar)
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_NO_ARTIFACT)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
