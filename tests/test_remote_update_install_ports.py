"""Ports compose real inspect/apply/unpack/UI observation. Transport is faked.

Prepare success always goes through apply_remote_install or the unpack payload
helper, never a stub PrepareOutcome. No company network, SSOT, or install.
"""

from __future__ import annotations

import ast
import importlib.util
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

import remote_provision_driver as DRIVER  # noqa: E402
import remote_update_install_ports as PORTS  # noqa: E402
import remote_update_install_ports_inspection as INSPECT  # noqa: E402
import remote_update_install_ports_transport as TRANSPORT  # noqa: E402
from remote_provision_artifact import admit_artifact  # noqa: E402
from remote_update_flow_contract import LostResponse, PortRefusal  # noqa: E402
from workstack import REMOTE_PROTOCOL_VERSION, __version__  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DRIVER_TESTS = load_module(TESTS / "test_remote_provision_driver.py", "install_ports_driver_fakes")
UNPACK_TESTS = load_module(TESTS / "test_remote_verified_unpack.py", "install_ports_unpack_helpers")
UI_TESTS = load_module(TESTS / "test_remote_ui_observation.py", "install_ports_ui_helpers")

CURRENT_APP = "/fixture/driver-owner/app"
TARGET_APP = "/fixture/driver-owner/app-next"
DATA_ROOT = "/fixture/driver-owner/data"
SCRATCH_PARENT = "/fixture/driver-owner"
MAX_FILE_LINES = 800
MAX_FUNCTION_LINES = 100
MAX_CCN = 15
PRODUCTION = (
    SHELL / "remote_update_install_ports.py",
    SHELL / "remote_update_install_ports_transport.py",
    SHELL / "remote_update_install_ports_inspection.py",
)


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


def current_profile(**overrides: object) -> DRIVER.DriverProfile:
    fields = {"remote_app_dir": CURRENT_APP, "remote_data_dir": DATA_ROOT}
    fields.update(overrides)
    return DRIVER_TESTS.make_profile(**fields)


def capability_block(
    publication: str = "unknown",
    method: str = "unknown",
    *,
    filesystem: str = "other",
    scratch: str = "not_requested",
    commit: str = "unknown",
    nfs_publish: str = "not_applicable",
    noexec: bool | None = False,
) -> dict[str, object]:
    return {
        "publication": publication,
        "method": method,
        "commit": commit,
        "scratch": scratch,
        "filesystem": filesystem,
        "noexec": noexec,
        "nfs_publish": nfs_publish,
    }


def facts(*, capability: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    document = DRIVER_TESTS.facts_document(**overrides)
    if capability is not None:
        document["capability"] = capability
    return document


def current_facts() -> dict[str, object]:
    return facts(
        install_exists=True,
        install_owner=DRIVER_TESTS.OWNER,
        install_product=__version__,
        install_protocol=REMOTE_PROTOCOL_VERSION,
        install_digest=DRIVER_TESTS.ARCHIVE_DIGEST,
    )


def session(**overrides: object) -> PORTS.LoopbackSession:
    fields: dict[str, object] = {
        "base_url": None,
        "workspace_id": DRIVER_TESTS.WORKSPACE_UID,
        "desktop_version": __version__,
    }
    fields.update(overrides)
    return PORTS.LoopbackSession(**fields)  # type: ignore[arg-type]


def make_ports(
    factory: DRIVER_TESTS.RecordingFactory,
    *,
    mode: str = INSPECT.MODE_TRANSACTIONAL,
    artifact: DRIVER.ArtifactSelection | None = None,
    observe: object | None = None,
    scratch: str | None = None,
    timeout: float = 5.0,
    expected: str | None = None,
    expected_served: str | None = None,
) -> PORTS.RemoteUpdateInstallPorts:
    return PORTS.RemoteUpdateInstallPorts(
        PORTS.InstallPortInputs(
            current_profile=current_profile(
                expected_workspace_id=expected or DRIVER_TESTS.WORKSPACE_UID
            ),
            target_app_dir=TARGET_APP,
            artifact=artifact or DRIVER_TESTS.make_artifact(),
            expected_workspace_id=expected or DRIVER_TESTS.WORKSPACE_UID,
            owner=DRIVER_TESTS.OWNER,
            ssh_executable=DRIVER_TESTS.SSH,
            install_mode=mode,
            observe_current=observe or (lambda: session()),
            capability_scratch_parent=scratch,
            process_factory=factory,
            probe_timeout=timeout,
            install_timeout=timeout,
            expected_served_ui_sha256=expected_served,
        )
    )


def probes(*documents: dict[str, object]) -> tuple[DRIVER_TESTS.FakeProcess, ...]:
    return tuple(DRIVER_TESTS.probe_process(item) for item in documents)


def run_payload(source: bytes, operation: str, app_dir: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-", operation, "--app-dir", app_dir],
        input=source,
        capture_output=True,
        timeout=60,
        check=False,
    )


def nested_verify_document(
    artifact: DRIVER.ArtifactSelection,
    *,
    digest: str | None = None,
    manifest: str | None = None,
    placement: str = "identity_verified",
) -> dict[str, object]:
    return {
        "activation": "not_activated",
        "atomic_directory_publish": False,
        "method": "verified_unpack",
        "outcome": "unpacked_verified" if placement == "identity_verified" else "not_ready",
        "placement": placement,
        "receipt": {
            "activation": "not_activated",
            "artifact_digest": digest or artifact.digest,
            "artifact_manifest_sha256": manifest or artifact.manifest_digest,
            "atomic_directory_publish": False,
            "files_verified": 1,
            "imports": "PASS",
            "method": "verified_unpack",
            "placement": "ready_candidate",
            "product_version": artifact.product_version,
            "remote_protocol_version": artifact.protocol_version,
            "schema_version": 1,
            "ssot_accessed": False,
        },
        "ssot_accessed": False,
    }


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
                ccn = complexity(node)
                with self.subTest(file=path.name, function=node.name):
                    self.assertLessEqual(length, MAX_FUNCTION_LINES)
                    self.assertLessEqual(ccn, MAX_CCN)


class ConstructorTests(unittest.TestCase):
    def test_rejects_workspace_mismatch_and_target_overlap(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        with self.assertRaises(PORTS.InstallPortsConfigError):
            PORTS.RemoteUpdateInstallPorts(
                PORTS.InstallPortInputs(
                    current_profile=current_profile(),
                    target_app_dir=TARGET_APP,
                    artifact=artifact,
                    expected_workspace_id=DRIVER_TESTS.OTHER_UID,
                    owner=DRIVER_TESTS.OWNER,
                    ssh_executable=DRIVER_TESTS.SSH,
                    install_mode=INSPECT.MODE_TRANSACTIONAL,
                    observe_current=lambda: session(),
                )
            )
        with self.assertRaises(PORTS.InstallPortsConfigError):
            PORTS.RemoteUpdateInstallPorts(
                PORTS.InstallPortInputs(
                    current_profile=current_profile(),
                    target_app_dir=CURRENT_APP,
                    artifact=artifact,
                    expected_workspace_id=DRIVER_TESTS.WORKSPACE_UID,
                    owner=DRIVER_TESTS.OWNER,
                    ssh_executable=DRIVER_TESTS.SSH,
                    install_mode=INSPECT.MODE_TRANSACTIONAL,
                    observe_current=lambda: session(),
                )
            )

    def test_rejects_unselected_mode_and_data_scratch(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        with self.assertRaises(PORTS.InstallPortsConfigError):
            PORTS.RemoteUpdateInstallPorts(
                PORTS.InstallPortInputs(
                    current_profile=current_profile(),
                    target_app_dir=TARGET_APP,
                    artifact=artifact,
                    expected_workspace_id=DRIVER_TESTS.WORKSPACE_UID,
                    owner=DRIVER_TESTS.OWNER,
                    ssh_executable=DRIVER_TESTS.SSH,
                    install_mode="best_effort",
                    observe_current=lambda: session(),
                )
            )
        with self.assertRaises(PORTS.InstallPortsConfigError):
            PORTS.RemoteUpdateInstallPorts(
                PORTS.InstallPortInputs(
                    current_profile=current_profile(),
                    target_app_dir=TARGET_APP,
                    artifact=artifact,
                    expected_workspace_id=DRIVER_TESTS.WORKSPACE_UID,
                    owner=DRIVER_TESTS.OWNER,
                    ssh_executable=DRIVER_TESTS.SSH,
                    install_mode=INSPECT.MODE_VERIFIED_UNPACK,
                    observe_current=lambda: session(),
                    capability_scratch_parent=DATA_ROOT,
                )
            )
        with self.assertRaises(PORTS.InstallPortsConfigError):
            PORTS.RemoteUpdateInstallPorts(
                PORTS.InstallPortInputs(
                    current_profile=current_profile(),
                    target_app_dir=TARGET_APP,
                    artifact=artifact,
                    expected_workspace_id=DRIVER_TESTS.WORKSPACE_UID,
                    owner=DRIVER_TESTS.OWNER,
                    ssh_executable=DRIVER_TESTS.SSH,
                    install_mode=INSPECT.MODE_TRANSACTIONAL,
                    observe_current=lambda: session(),
                    expected_served_ui_sha256="sha256:" + "AB" * 32,
                )
            )


class PreviewTests(unittest.TestCase):
    def test_transactional_unavailable_is_not_silently_unpacked(self) -> None:
        factory = DRIVER_TESTS.RecordingFactory(
            *probes(
                facts(capability=capability_block("unavailable", filesystem="nfs", nfs_publish="failed", scratch="measured", commit="unavailable")),
                current_facts(),
            )
        )
        ports = make_ports(factory)
        preview = ports.describe()
        self.assertEqual(preview.install_capability, "unavailable")
        self.assertEqual(preview.install_method, "transactional")
        self.assertIsNone(preview.served_ui_version)
        self.assertEqual(preview.remote_version, __version__)
        self.assertEqual(factory.called, 2)
        self.assertTrue(all("provision-install" not in " ".join(cmd) for cmd in factory.commands))

    def test_verified_unpack_mode_stays_selected_when_publication_is_unavailable(self) -> None:
        factory = DRIVER_TESTS.RecordingFactory(
            *probes(
                facts(
                    capability=capability_block(
                        "unavailable",
                        filesystem="nfs",
                        nfs_publish="failed",
                        scratch="measured",
                        commit="unavailable",
                    )
                ),
                current_facts(),
            )
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK)
        preview = ports.describe()
        self.assertEqual(preview.install_capability, "available")
        self.assertEqual(preview.install_method, "verified_unpack")


class TransactionalPrepareTests(unittest.TestCase):
    def test_prepare_success_goes_through_apply_remote_install(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        absent = facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
        )
        ports = make_ports(factory, artifact=artifact)
        outcome = ports.prepare("prepare-1")
        self.assertEqual(outcome.status, "verified")
        self.assertEqual(outcome.method, "transactional")
        self.assertTrue(outcome.previous_app_retained)
        self.assertTrue(outcome.previous_profile_retained)
        joined = [" ".join(command) for command in factory.commands]
        self.assertTrue(any("provision-install" in item for item in joined))
        self.assertIsNotNone(ports._record)
        self.assertEqual(ports._record.receipt["artifact_digest"], artifact.digest)  # type: ignore[union-attr]

    def test_unavailable_capability_does_not_apply(self) -> None:
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(
                facts(capability=capability_block("unavailable", filesystem="nfs", nfs_publish="failed", scratch="measured", commit="unavailable"))
            ),
            DRIVER_TESTS.probe_process(current_facts()),
        )
        ports = make_ports(factory)
        outcome = ports.prepare("prepare-blocked")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.capability, "unavailable")
        self.assertEqual(outcome.method, "transactional")
        self.assertTrue(outcome.previous_app_retained)
        self.assertEqual(factory.called, 2)
        self.assertTrue(all("provision-install" not in " ".join(cmd) for cmd in factory.commands))

    def test_same_operation_observe_does_not_reissue(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        absent = facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
            capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"),
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        ports = make_ports(factory, artifact=artifact)
        first = ports.prepare("prepare-same")
        second = ports.prepare("prepare-same")
        self.assertEqual(first.status, "verified")
        self.assertEqual(second.status, "verified")
        self.assertEqual(sum("provision-install" in " ".join(cmd) for cmd in factory.commands), 1)

    def test_unknown_install_outcome_is_lost_and_observe_reconciles(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        absent = facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=b"not-a-receipt\n", returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(
                facts(
                    install_exists=True,
                    install_owner=DRIVER_TESTS.OWNER,
                    install_product=__version__,
                    install_protocol=REMOTE_PROTOCOL_VERSION,
                    install_digest=artifact.digest,
                )
            ),
        )
        ports = make_ports(factory, artifact=artifact)
        with self.assertRaises(LostResponse) as caught:
            ports.prepare("prepare-lost")
        self.assertEqual(caught.exception.operation_id, "prepare-lost")
        observed = ports.observe("prepare-lost")
        self.assertEqual(observed.status, "verified")
        self.assertEqual(sum("provision-install" in " ".join(cmd) for cmd in factory.commands), 1)

    def test_a_second_operation_id_does_not_reissue_after_a_sent_mutation(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        absent = facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
        )
        ports = make_ports(factory, artifact=artifact)
        ports.prepare("op-a")
        with self.assertRaises(PortRefusal) as caught:
            ports.prepare("op-b")
        self.assertEqual(caught.exception.code, "operation_id_mismatch")
        self.assertEqual(sum("provision-install" in " ".join(cmd) for cmd in factory.commands), 1)


class UnpackPrepareTests(unittest.TestCase):
    def test_prepare_success_uses_real_unpack_document_not_a_stub(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        document = {
            "activation": "not_activated",
            "artifact_digest": artifact.digest,
            "artifact_manifest_sha256": artifact.manifest_digest,
            "atomic_directory_publish": False,
            "files_verified": 1,
            "imports": "PASS",
            "method": "verified_unpack",
            "outcome": "unpacked_verified",
            "placement": "ready_candidate",
            "product_version": artifact.product_version,
            "remote_protocol_version": artifact.protocol_version,
            "schema_version": 1,
            "ssot_accessed": False,
        }
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(facts()),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(document), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        outcome = ports.prepare("unpack-1")
        self.assertEqual(outcome.status, "verified")
        self.assertEqual(outcome.method, "verified_unpack")
        self.assertTrue(outcome.previous_app_retained)
        remote = factory.commands[1][-1]
        self.assertIn("place --app-dir", remote)
        self.assertIn(TARGET_APP, remote)
        self.assertIn("BatchMode=yes", factory.commands[1])
        self.assertEqual(ports._record.receipt["artifact_digest"], artifact.digest)  # type: ignore[union-attr]

    def test_existing_target_does_not_overwrite(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        occupied = facts(install_exists=True, install_owner=DRIVER_TESTS.OWNER, install_product=__version__, install_protocol=REMOTE_PROTOCOL_VERSION, install_digest="sha256:" + "cd" * 32)
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(occupied),
            DRIVER_TESTS.probe_process(current_facts()),
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        outcome = ports.prepare("unpack-occupied")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.capability, "unavailable")
        self.assertTrue(outcome.previous_app_retained)
        self.assertEqual(factory.called, 2)
        self.assertTrue(all("place --app-dir" not in " ".join(cmd) for cmd in factory.commands))


class ProbeAndVerifyTests(unittest.TestCase):
    def test_probe_uses_preparation_receipt_identity(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        absent = facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        ports = make_ports(factory, artifact=artifact)
        ports.prepare("prep-probe")
        probed = ports.probe("prep-probe")
        self.assertEqual(probed.status, "verified")
        self.assertEqual(probed.workspace_match, "verified")
        self.assertEqual(probed.protocol_version, str(REMOTE_PROTOCOL_VERSION))
        self.assertIsNone(probed.served_ui_version)

    def test_verify_compares_runtime_served_html_and_workspace(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        expected = UI_TESTS.WORKSPACE
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
            workspace_id=expected,
        )
        absent = facts(
            workspace_id=expected,
            capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"),
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest, workspace_uid=expected), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        with UI_TESTS.running_server() as server:
            ports = make_ports(
                factory,
                artifact=artifact,
                expected=expected,
                expected_served=UI_TESTS.digest_for(UI_TESTS.CURRENT_HTML),
                observe=lambda: session(base_url=UI_TESTS.base_url(server), workspace_id=expected),
            )
            ports.prepare("prep-verify")
            verified = ports.verify("prep-verify")
        self.assertEqual(verified.status, "verified")
        self.assertEqual(verified.served_ui_version, UI_TESTS.digest_for(UI_TESTS.CURRENT_HTML))
        self.assertEqual(verified.remote_version, __version__)
        self.assertRegex(verified.served_ui_version, r"^sha256:[0-9a-f]{64}$")

    def test_verify_stays_unknown_without_loopback(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))),
            DRIVER_TESTS.probe_process(facts(capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"))),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        ports = make_ports(factory, artifact=artifact)
        ports.prepare("prep-unknown")
        verified = ports.verify("prep-unknown")
        self.assertEqual(verified.status, "unknown")
        self.assertIsNone(verified.served_ui_version)


class ConnectedFixTests(unittest.TestCase):
    def test_unpack_probe_and_verify_use_real_nested_identity(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        source = TRANSPORT.build_unpack_payload_source(artifact)
        expected = UI_TESTS.WORKSPACE
        expected_html = UI_TESTS.digest_for(UI_TESTS.CURRENT_HTML)
        candidate = facts(workspace_id=expected)
        with tempfile.TemporaryDirectory(prefix="workstack-install-ports-fix-") as tmp:
            target = str(Path(tmp) / "new-app")
            placed = run_payload(source, "place", target)
            self.assertEqual(placed.returncode, 0, placed.stderr.decode("utf-8", "replace"))
            place_doc = json.loads(placed.stdout.decode("utf-8"))
            inspected = run_payload(source, "inspect", target)
            inspect_doc = json.loads(inspected.stdout.decode("utf-8"))
            verified_helper = run_payload(source, "verify", target)
            verify_doc = json.loads(verified_helper.stdout.decode("utf-8"))
        self.assertNotIn("artifact_digest", verify_doc)
        self.assertEqual(verify_doc["placement"], "identity_verified")
        self.assertEqual(verify_doc["receipt"]["artifact_digest"], artifact.digest)
        self.assertEqual(inspect_doc["placement"], "ready_candidate")
        self.assertEqual(inspect_doc["receipt"]["artifact_digest"], artifact.digest)
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(candidate),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(place_doc), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(candidate),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(verify_doc), returncode=0),
            DRIVER_TESTS.probe_process(candidate),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(verify_doc), returncode=0),
        )
        with UI_TESTS.running_server() as server:
            ports = make_ports(
                factory,
                mode=INSPECT.MODE_VERIFIED_UNPACK,
                artifact=artifact,
                expected=expected,
                expected_served=expected_html,
                observe=lambda: session(base_url=UI_TESTS.base_url(server), workspace_id=expected),
            )
            prepared = ports.prepare("unpack-real")
            probed = ports.probe("unpack-real")
            verified = ports.verify("unpack-real")
        self.assertEqual(prepared.status, "verified")
        self.assertEqual(probed.status, "verified")
        self.assertEqual(probed.workspace_match, "verified")
        self.assertEqual(verified.status, "verified")
        self.assertEqual(verified.served_ui_version, expected_html)
        self.assertIn("verify --app-dir", " ".join(factory.commands[4]))
        self.assertIn("verify --app-dir", " ".join(factory.commands[6]))

    def test_failed_prepare_never_verifies_another_actors_path(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(
                facts(capability=capability_block("unavailable", filesystem="nfs", nfs_publish="failed", scratch="measured", commit="unavailable"))
            ),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        ports = make_ports(factory, artifact=artifact)
        outcome = ports.prepare("blocked-other")
        self.assertEqual(outcome.status, "failed")
        self.assertFalse(ports._record.issued)  # type: ignore[union-attr]
        probed = ports.probe("blocked-other")
        verified = ports.verify("blocked-other")
        self.assertNotEqual(probed.status, "verified")
        self.assertNotEqual(verified.status, "verified")
        self.assertEqual(factory.called, 2)

    def test_unrelated_unpack_receipt_is_rejected(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        place_doc = {
            "activation": "not_activated",
            "artifact_digest": artifact.digest,
            "artifact_manifest_sha256": artifact.manifest_digest,
            "atomic_directory_publish": False,
            "files_verified": 1,
            "imports": "PASS",
            "method": "verified_unpack",
            "outcome": "unpacked_verified",
            "placement": "ready_candidate",
            "product_version": artifact.product_version,
            "remote_protocol_version": artifact.protocol_version,
            "schema_version": 1,
            "ssot_accessed": False,
        }
        foreign = nested_verify_document(artifact, digest="sha256:" + "ab" * 32)
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(facts()),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(place_doc), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(facts()),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(foreign), returncode=0),
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        prepared = ports.prepare("unpack-foreign")
        probed = ports.probe("unpack-foreign")
        self.assertEqual(prepared.status, "verified")
        self.assertNotEqual(probed.status, "verified")

    def test_legacy_html_fails_admitted_served_digest(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        expected = UI_TESTS.WORKSPACE
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
            workspace_id=expected,
        )
        absent = facts(
            workspace_id=expected,
            capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"),
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest, workspace_uid=expected), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        with UI_TESTS.running_server() as server:
            server.html = UI_TESTS.LEGACY_HTML
            ports = make_ports(
                factory,
                artifact=artifact,
                expected=expected,
                expected_served=UI_TESTS.digest_for(UI_TESTS.CURRENT_HTML),
                observe=lambda: session(base_url=UI_TESTS.base_url(server), workspace_id=expected),
            )
            ports.prepare("prep-legacy")
            verified = ports.verify("prep-legacy")
        self.assertEqual(verified.status, "failed")
        self.assertEqual(verified.served_ui_version, UI_TESTS.digest_for(UI_TESTS.LEGACY_HTML))

    def test_observed_hash_without_expected_stays_unknown(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        expected = UI_TESTS.WORKSPACE
        installed = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
            workspace_id=expected,
        )
        absent = facts(
            workspace_id=expected,
            capability=capability_block("available", "transactional", scratch="measured", commit="renameat2_noreplace"),
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(stdout=DRIVER_TESTS.success_line(digest=artifact.digest, manifest=artifact.manifest_digest, workspace_uid=expected), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(installed),
        )
        with UI_TESTS.running_server() as server:
            ports = make_ports(
                factory,
                artifact=artifact,
                expected=expected,
                observe=lambda: session(base_url=UI_TESTS.base_url(server), workspace_id=expected),
            )
            ports.prepare("prep-no-expected")
            verified = ports.verify("prep-no-expected")
        self.assertEqual(verified.status, "unknown")
        self.assertEqual(verified.served_ui_version, UI_TESTS.digest_for(UI_TESTS.CURRENT_HTML))

    def test_early_unavailable_preserves_absent_and_unknown_retention(self) -> None:
        unavailable = facts(
            capability=capability_block(
                "unavailable", filesystem="nfs", nfs_publish="failed", scratch="measured", commit="unavailable"
            )
        )
        missing = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(unavailable),
            DRIVER_TESTS.probe_process(facts(install_exists=False)),
        )
        absent_ports = make_ports(missing)
        absent_outcome = absent_ports.prepare("prep-absent-app")
        self.assertEqual(absent_outcome.status, "failed")
        self.assertIs(absent_outcome.previous_app_retained, False)
        unknown = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(unavailable),
            DRIVER_TESTS.FakeProcess(stdout=b"not-facts\n", returncode=0),
        )
        unknown_ports = make_ports(unknown)
        unknown_outcome = unknown_ports.prepare("prep-unknown-app")
        self.assertEqual(unknown_outcome.status, "failed")
        self.assertIsNone(unknown_outcome.previous_app_retained)
        self.assertTrue(unknown_outcome.previous_profile_retained)

    def test_unpack_lost_response_observes_same_id_and_refuses_replay(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        verify_doc = nested_verify_document(artifact)
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(facts()),
            DRIVER_TESTS.FakeProcess(stdout=b"not-a-receipt\n", returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(verify_doc), returncode=0),
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        with self.assertRaises(LostResponse) as caught:
            ports.prepare("unpack-lost")
        self.assertEqual(caught.exception.operation_id, "unpack-lost")
        observed = ports.observe("unpack-lost")
        self.assertEqual(observed.status, "verified")
        self.assertIn("verify --app-dir", " ".join(factory.commands[3]))
        with self.assertRaises(PortRefusal) as refused:
            ports.prepare("unpack-other")
        self.assertEqual(refused.exception.code, "operation_id_mismatch")
        self.assertEqual(sum("place --app-dir" in " ".join(cmd) for cmd in factory.commands), 1)


class InspectionUnitTests(unittest.TestCase):
    def test_version_string_is_not_artifact_integrity(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        other = INSPECT.verify_outcome(
            runtime_digest="sha256:" + "ff" * 32,
            artifact=artifact,
            workspace=DRIVER_TESTS.WORKSPACE_UID,
            expected_workspace=DRIVER_TESTS.WORKSPACE_UID,
            served_ui="sha256:" + "ab" * 32,
            desktop_version=__version__,
            remote_version=artifact.product_version,
            protocol_version="1",
        )
        self.assertEqual(other.status, "failed")
        self.assertEqual(other.remote_version, artifact.product_version)
        nested = nested_verify_document(artifact)
        self.assertTrue(INSPECT.unpack_identity_matches(nested, artifact))
        self.assertEqual(INSPECT.unpack_runtime_digest(nested), artifact.digest)
        self.assertIsNone(INSPECT.unpack_runtime_digest(nested["receipt"]))
        foreign = nested_verify_document(artifact, digest="sha256:" + "cd" * 32)
        self.assertFalse(INSPECT.unpack_identity_matches(foreign, artifact))


class RetainedRecoveryTests(unittest.TestCase):
    """The public read-only recovery entry point, and what it refuses.

    A restarted desktop reaches PROBE and VERIFY with no in-memory operation.
    ``admit_retained_preparation`` is the only supported way back in, and it
    re-runs the frozen verified-unpack ``verify`` exchange rather than trusting
    the retained evidence: the evidence names the candidate, the receipt admits
    it.
    """

    def retained(self, artifact, **overrides: object) -> INSPECT.RetainedPreparation:
        fields: dict[str, object] = {
            "operation_id": "staged-in-a-previous-process",
            "target_app_dir": TARGET_APP,
            "expected_workspace_id": DRIVER_TESTS.WORKSPACE_UID,
            "artifact_digest": artifact.digest,
            "artifact_manifest_sha256": artifact.manifest_digest,
        }
        fields.update(overrides)
        return INSPECT.RetainedPreparation(**fields)  # type: ignore[arg-type]

    def recovery_factory(self, document: dict[str, object]) -> DRIVER_TESTS.RecordingFactory:
        return DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(document), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
        )

    def test_a_reverified_receipt_admits_the_retained_identity(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        staged = facts(
            install_exists=True,
            install_owner=DRIVER_TESTS.OWNER,
            install_product=artifact.product_version,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=artifact.digest,
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.FakeProcess(
                stdout=TRANSPORT.encode_line(nested_verify_document(artifact)), returncode=0
            ),
            DRIVER_TESTS.probe_process(current_facts()),
            DRIVER_TESTS.probe_process(staged),
            DRIVER_TESTS.FakeProcess(
                stdout=TRANSPORT.encode_line(nested_verify_document(artifact)), returncode=0
            ),
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        self.assertTrue(ports.admit_retained_preparation(self.retained(artifact)))
        record = ports._record  # noqa: SLF001 - the admitted record is the subject
        self.assertEqual(record.operation_id, "staged-in-a-previous-process")
        self.assertTrue(INSPECT.preparation_verified(record))
        self.assertIsNotNone(record.receipt)
        # The recovery read the candidate back; it never placed anything.
        self.assertIn("verify --app-dir", " ".join(factory.commands[0]))
        self.assertFalse(
            any("place --app-dir" in " ".join(command) for command in factory.commands)
        )
        # The read-only stage is answered under the RETAINED identity, which is
        # what the host maps its own fresh stage identity onto.
        probed = ports.probe("staged-in-a-previous-process")
        self.assertEqual(probed.status, "verified")
        with self.assertRaises(PortRefusal):
            ports.probe("a-fresh-stage-identity")

    def test_a_ready_candidate_is_not_a_reverified_identity(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        document = nested_verify_document(artifact, placement="ready_candidate")
        ports = make_ports(
            self.recovery_factory(document),
            mode=INSPECT.MODE_VERIFIED_UNPACK,
            artifact=artifact,
        )
        self.assertFalse(ports.admit_retained_preparation(self.retained(artifact)))
        self.assertIsNone(ports._record)  # noqa: SLF001 - nothing was fabricated

    def test_another_artifacts_receipt_never_admits_this_candidate(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        foreign = nested_verify_document(artifact, digest="sha256:" + "ab" * 32)
        ports = make_ports(
            self.recovery_factory(foreign),
            mode=INSPECT.MODE_VERIFIED_UNPACK,
            artifact=artifact,
        )
        self.assertFalse(ports.admit_retained_preparation(self.retained(artifact)))
        self.assertIsNone(ports._record)  # noqa: SLF001 - nothing was fabricated

    def test_a_lost_verify_exchange_admits_nothing(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.FakeProcess(stdout=b"", returncode=1)
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        self.assertFalse(ports.admit_retained_preparation(self.retained(artifact)))
        self.assertIsNone(ports._record)  # noqa: SLF001 - nothing was fabricated

    def test_evidence_for_another_selection_is_refused_before_any_exchange(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        factory = DRIVER_TESTS.RecordingFactory()
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        for overrides in (
            {"target_app_dir": "/fixture/driver-owner/app-elsewhere"},
            {"expected_workspace_id": DRIVER_TESTS.OTHER_UID},
            {"artifact_digest": "sha256:" + "ef" * 32},
            {"artifact_manifest_sha256": "sha256:" + "ef" * 32},
            {"operation_id": ""},
            {"method": INSPECT.MODE_TRANSACTIONAL},
        ):
            with self.subTest(**overrides):
                self.assertFalse(
                    ports.admit_retained_preparation(self.retained(artifact, **overrides))
                )
        self.assertFalse(factory.commands)
        self.assertIsNone(ports._record)  # noqa: SLF001 - nothing was fabricated

    def test_an_operation_already_in_flight_is_never_replaced(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        place_doc = dict(nested_verify_document(artifact)["receipt"])
        place_doc["outcome"] = "unpacked_verified"
        place_doc["placement"] = "ready_candidate"
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(facts()),
            DRIVER_TESTS.FakeProcess(stdout=TRANSPORT.encode_line(place_doc), returncode=0),
            DRIVER_TESTS.probe_process(current_facts()),
        )
        ports = make_ports(factory, mode=INSPECT.MODE_VERIFIED_UNPACK, artifact=artifact)
        self.assertEqual(ports.prepare("staged-here").status, "verified")
        self.assertFalse(ports.admit_retained_preparation(self.retained(artifact)))
        self.assertEqual(ports._record.operation_id, "staged-here")  # noqa: SLF001

    def test_recovery_is_not_offered_for_a_transactional_install(self) -> None:
        artifact = DRIVER_TESTS.make_artifact()
        factory = DRIVER_TESTS.RecordingFactory()
        ports = make_ports(factory, mode=INSPECT.MODE_TRANSACTIONAL, artifact=artifact)
        self.assertFalse(
            ports.admit_retained_preparation(
                self.retained(artifact, method=INSPECT.MODE_TRANSACTIONAL)
            )
        )
        self.assertFalse(
            ports.admit_retained_preparation(self.retained(artifact))
        )
        self.assertIsNone(ports._record)  # noqa: SLF001 - nothing was fabricated


if __name__ == "__main__":
    unittest.main()
