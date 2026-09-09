"""Focused tests for the bounded remote provision-install driver.

Nothing here runs a real SSH client, installs anything, touches a user
directory, reaches a company host, or mutates a store.  Every input is
synthetic, and both remote legs are fake: usually in-process objects, and in
the last two classes a local Python child standing in for ``ssh`` so the real
pipe, timeout, and cleanup boundary is exercised without a network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import time
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
SCRIPTS = ROOT / "scripts"
# ``scripts`` is deliberately kept off sys.path: scripts/remote_provision_plan.py
# shares a basename with the shell module the driver imports, and putting the
# directory on the path shadows it.  The CLI is loaded by file location instead.
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from workstack import REMOTE_PROTOCOL_VERSION, __version__  # noqa: E402

import bounded_process_exchange as EXCHANGE  # noqa: E402
import remote_provision_driver as DRIVER  # noqa: E402

CLI_PATH = SCRIPTS / "remote_provision_install.py"
CLI_SPEC = importlib.util.spec_from_file_location("remote_provision_install_cli", CLI_PATH)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
CLI = importlib.util.module_from_spec(CLI_SPEC)
sys.modules[CLI_SPEC.name] = CLI
CLI_SPEC.loader.exec_module(CLI)


#: Long enough that a 0.2 second exchange timeout always fires first, short
#: enough that the post-kill join in the exchange settles inside its grace.
STALL_SECONDS = 1.0

ALIAS = "fixture-linux"
SSH = "ssh.exe"
OWNER = "driver_owner"
REMOTE_PYTHON = "/fixture/driver-owner/opt/python"
INSTALL_ROOT = "/fixture/driver-owner/app-next"
DATA_ROOT = "/fixture/driver-owner/data"
WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
OTHER_UID = "22222222-2222-4222-8222-222222222222"
MANIFEST_DIGEST = "sha256:" + "ab" * 32
ARCHIVE_BYTES = b"PK\x05\x06" + b"synthetic-archive-bytes" * 4
ARCHIVE_DIGEST = "sha256:" + hashlib.sha256(ARCHIVE_BYTES).hexdigest()


def sidecar_document(
    *,
    product_version: str = __version__,
    protocol_version: int = REMOTE_PROTOCOL_VERSION,
    archive_bytes: bytes = ARCHIVE_BYTES,
    manifest: str = MANIFEST_DIGEST,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product_version": product_version,
        "remote_protocol_version": protocol_version,
        "source_commit": "0" * 40,
        "target_id": "cp312-manylinux_2_17_x86_64",
        "artifact_manifest_sha256": manifest,
        "archive": {
            "name": f"WorkStack-Linux-{product_version}-cp312-manylinux_2_17_x86_64.zip",
            "size": len(archive_bytes),
            "sha256": "sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
        },
    }


def sidecar_bytes(**overrides: object) -> bytes:
    return json.dumps(sidecar_document(**overrides)).encode("utf-8")


def make_profile(**overrides: object) -> DRIVER.DriverProfile:
    fields: dict[str, object] = {
        "ssh_host_alias": ALIAS,
        "remote_python": REMOTE_PYTHON,
        "remote_app_dir": INSTALL_ROOT,
        "remote_data_dir": DATA_ROOT,
        "expected_workspace_id": WORKSPACE_UID,
    }
    fields.update(overrides)
    return DRIVER.DriverProfile(**fields)  # type: ignore[arg-type]


def make_artifact(**overrides: object) -> DRIVER.ArtifactSelection:
    archive = overrides.pop("archive_bytes", ARCHIVE_BYTES)
    return DRIVER.select_artifact(archive, sidecar_bytes(archive_bytes=archive, **overrides))


def facts_document(
    *,
    install_exists: bool = False,
    install_symlink: bool = False,
    install_owner: str | None = None,
    install_product: str | None = None,
    install_protocol: int | None = None,
    install_digest: str | None = None,
    data_exists: bool = True,
    data_symlink: bool = False,
    data_owner: str | None = OWNER,
    workspace_id: str | None = WORKSPACE_UID,
    python_version: str = "3.12.10",
) -> dict[str, object]:
    return {
        "os": "linux",
        "python": {"path": REMOTE_PYTHON, "version": python_version},
        "install": {
            "exists": install_exists,
            "owner": install_owner,
            "symlink": install_symlink,
            "product_version": install_product,
            "protocol_version": install_protocol,
            "digest": install_digest,
        },
        "data": {
            "exists": data_exists,
            "owner": data_owner,
            "symlink": data_symlink,
            "workspace_id": workspace_id,
        },
    }


class FakeStdin:
    def __init__(self, *, stall: bool = False) -> None:
        self.buffer = bytearray()
        self.closed = False
        self.stall = stall

    def write(self, data: bytes) -> int:
        if self.stall:
            time.sleep(STALL_SECONDS)
        self.buffer.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class StallingStream:
    """A pipe read that only returns once the child has been killed."""

    def __init__(self, seconds: float = STALL_SECONDS) -> None:
        self.seconds = seconds
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        time.sleep(self.seconds)
        return b""

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """One SSH exchange, entirely in process. Records that it was killed."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        stall_stdout: bool = False,
        stall_stdin: bool = False,
    ) -> None:
        self.stdin = FakeStdin(stall=stall_stdin)
        self.stdout = StallingStream() if stall_stdout else io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.killed = False
        self._stall = stall_stdout or stall_stdin

    def wait(self, timeout: float | None = None) -> int:
        if self._stall and not self.killed:
            raise subprocess.TimeoutExpired(cmd=SSH, timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._stall = False
        self.returncode = -9


class RecordingFactory:
    """Hands out the queued processes and records every argv it was given."""

    def __init__(self, *processes: FakeProcess) -> None:
        self.queue = list(processes)
        self.commands: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.commands.append(list(command))
        self.kwargs.append(dict(kwargs))
        if not self.queue:
            raise AssertionError("the driver spawned more processes than the test allowed")
        return self.queue.pop(0)

    @property
    def called(self) -> int:
        return len(self.commands)


def probe_process(facts: dict[str, object]) -> FakeProcess:
    line = json.dumps(facts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return FakeProcess(stdout=line.encode("ascii") + b"\n", returncode=0)


def success_line(
    *,
    workspace_uid: str = WORKSPACE_UID,
    product_version: str = __version__,
    protocol_version: int = REMOTE_PROTOCOL_VERSION,
    digest: str = ARCHIVE_DIGEST,
    manifest: str = MANIFEST_DIGEST,
) -> bytes:
    document = {
        "schema_version": 1,
        "outcome": "installed",
        "workspace_uid": workspace_uid,
        "product_version": product_version,
        "remote_protocol_version": protocol_version,
        "artifact_digest": digest,
        "artifact_manifest_sha256": manifest,
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"


def refuse_line(code: str) -> bytes:
    document = {"schema_version": 1, "outcome": "refused", "code": code}
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"


def inspect_with(facts: dict[str, object], **overrides: object):
    """Run one read-only inspection against synthetic facts."""

    profile = overrides.pop("profile", make_profile())
    artifact = overrides.pop("artifact", make_artifact())
    factory = RecordingFactory(probe_process(facts))
    inspection = DRIVER.inspect_remote_target(
        profile,
        overrides.pop("owner", OWNER),
        artifact,
        ssh_executable=SSH,
        process_factory=factory,
    )
    return inspection, artifact, factory


def forge_inspection(inspection, **overrides):
    """Rebuild an inspection by hand, the way a caller holding one could.

    This is the shape the review demonstrated: ordinary editable dictionaries
    in a frozen wrapper, carrying whatever decision the caller wants.
    """

    fields: dict[str, object] = {
        "binding": inspection.binding,
        "profile": inspection.profile,
        "owner": inspection.owner,
        "facts": json.loads(json.dumps(DRIVER.plain(inspection.facts))),
        "plan": json.loads(json.dumps(DRIVER.plain(inspection.plan))),
        "evidence": inspection.evidence,
    }
    fields.update(overrides)
    return DRIVER.DriverInspection(**fields)  # type: ignore[arg-type]


class ArtifactSelectionTests(unittest.TestCase):
    def test_sidecar_binds_the_exact_archive(self) -> None:
        artifact = make_artifact()
        self.assertEqual(artifact.digest, ARCHIVE_DIGEST)
        self.assertEqual(artifact.manifest_digest, MANIFEST_DIGEST)
        self.assertEqual(artifact.product_version, __version__)

    def test_sidecar_for_another_archive_is_refused(self) -> None:
        other = sidecar_bytes(archive_bytes=b"PK\x05\x06different-bytes")
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.select_artifact(ARCHIVE_BYTES, other)
        self.assertEqual(caught.exception.code, "DRIVER_ARTIFACT_INVALID")

    def test_sidecar_size_must_match_the_archive(self) -> None:
        document = sidecar_document()
        archive = document["archive"]
        assert isinstance(archive, dict)
        archive["size"] = len(ARCHIVE_BYTES) + 1
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.select_artifact(ARCHIVE_BYTES, json.dumps(document).encode("utf-8"))
        self.assertEqual(caught.exception.code, "DRIVER_ARTIFACT_INVALID")


class ReadOnlyInspectionTests(unittest.TestCase):
    def test_facts_are_collected_for_the_selected_profile(self) -> None:
        inspection, _artifact, factory = inspect_with(facts_document())
        self.assertEqual(factory.called, 1)
        command = factory.commands[0]
        self.assertEqual(command[0], SSH)
        self.assertIn(ALIAS, command)
        self.assertIn(INSTALL_ROOT, command[-1])
        self.assertIn(DATA_ROOT, command[-1])
        self.assertIn(OWNER, command[-1])
        self.assertEqual(inspection.decision, "install_needed")
        self.assertEqual(inspection.plan["mode"], "plan_only")

    def test_the_plan_target_comes_from_the_profile_not_the_caller(self) -> None:
        inspection, _artifact, _factory = inspect_with(facts_document())
        plan = inspection.plan["plan"]
        assert isinstance(plan, Mapping)
        self.assertEqual(plan["install_root"], INSTALL_ROOT)
        self.assertEqual(plan["data_root"], DATA_ROOT)
        self.assertEqual(plan["digest"], ARCHIVE_DIGEST)

    def test_inspection_reports_no_activation(self) -> None:
        inspection, _artifact, _factory = inspect_with(facts_document())
        document = DRIVER.render_inspection(inspection)
        self.assertEqual(document["activation"], "not_activated")
        self.assertEqual(document["stage"], "inspect")

    def test_strict_host_key_checking_stays_on_for_the_probe(self) -> None:
        _inspection, _artifact, factory = inspect_with(facts_document())
        command = factory.commands[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)


class RefusedPlanTests(unittest.TestCase):
    def test_an_existing_install_root_refuses_and_spawns_no_install(self) -> None:
        facts = facts_document(
            install_exists=True,
            install_owner=OWNER,
            install_product="0.9.0",
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest="sha256:" + "cd" * 32,
        )
        inspection, artifact, factory = inspect_with(facts)
        self.assertEqual(inspection.decision, "refused")
        codes = {item["code"] for item in inspection.plan["diagnostics"]}
        self.assertIn("INSTALL_CONTENT_CONFLICT", codes)

        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=install_factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_PLAN_REFUSED")
        self.assertEqual(install_factory.called, 0)
        self.assertEqual(factory.called, 1)

    def test_an_artifact_for_another_product_version_refuses_the_plan(self) -> None:
        artifact = make_artifact(product_version="0.0.1-not-this-desktop")
        inspection, _artifact, _factory = inspect_with(facts_document(), artifact=artifact)
        self.assertEqual(inspection.decision, "refused")
        codes = {item["code"] for item in inspection.plan["diagnostics"]}
        self.assertIn("REMOTE_APP_MISMATCH", codes)


class ApplyGateTests(unittest.TestCase):
    def test_apply_defaults_to_off_and_spawns_nothing(self) -> None:
        inspection, artifact, _factory = inspect_with(facts_document())
        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection, artifact, ssh_executable=SSH, process_factory=install_factory
            )
        self.assertEqual(caught.exception.code, "DRIVER_APPLY_NOT_REQUESTED")
        self.assertEqual(install_factory.called, 0)

    def test_a_truthy_non_true_apply_is_not_an_apply(self) -> None:
        inspection, artifact, _factory = inspect_with(facts_document())
        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply="yes",  # type: ignore[arg-type]
                ssh_executable=SSH,
                process_factory=install_factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_APPLY_NOT_REQUESTED")
        self.assertEqual(install_factory.called, 0)

    def test_a_replanned_profile_cannot_ride_a_stale_inspection(self) -> None:
        inspection, artifact, _factory = inspect_with(facts_document())
        moved = forge_inspection(
            inspection,
            profile=make_profile(remote_app_dir="/fixture/driver-owner/app-elsewhere"),
        )
        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                moved,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=install_factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_BINDING_MISMATCH")
        self.assertEqual(install_factory.called, 0)

    def test_a_swapped_artifact_cannot_ride_a_stale_inspection(self) -> None:
        inspection, _artifact, _factory = inspect_with(facts_document())
        other = make_artifact(archive_bytes=b"PK\x05\x06another-archive-entirely")
        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                other,
                apply=True,
                ssh_executable=SSH,
                process_factory=install_factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_BINDING_MISMATCH")
        self.assertEqual(install_factory.called, 0)


class WrongTargetTests(unittest.TestCase):
    def test_a_missing_data_root_is_refused_before_ssh(self) -> None:
        facts = facts_document(data_exists=False, data_owner=None, workspace_id=None)
        inspection, artifact, _factory = inspect_with(facts)
        self.assertEqual(inspection.decision, "install_needed")
        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=install_factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_WORKSPACE_UNCONFIRMED")
        self.assertEqual(install_factory.called, 0)

    def test_another_workspace_is_refused_by_the_plan(self) -> None:
        facts = facts_document(workspace_id=OTHER_UID)
        inspection, _artifact, _factory = inspect_with(facts)
        self.assertEqual(inspection.decision, "refused")
        codes = {item["code"] for item in inspection.plan["diagnostics"]}
        self.assertIn("REMOTE_WORKSPACE_MISMATCH", codes)

    def test_an_existing_install_root_is_never_a_fresh_target(self) -> None:
        facts = facts_document(
            install_exists=True,
            install_owner=OWNER,
            install_product=__version__,
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest=ARCHIVE_DIGEST,
        )
        inspection, artifact, _factory = inspect_with(facts)
        # The planner calls this "current"; it is still not an install.
        self.assertEqual(inspection.decision, "current")
        install_factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=install_factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_PLAN_REFUSED")
        self.assertEqual(install_factory.called, 0)


class InstallExchangeTests(unittest.TestCase):
    def _apply(self, process: FakeProcess, **overrides: object):
        """Inspect, then apply against a revalidation probe plus one install."""

        facts = facts_document()
        inspection, artifact, _factory = inspect_with(facts)
        factory = RecordingFactory(probe_process(facts), process)
        outcome = DRIVER.apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=SSH,
            process_factory=factory,
            **overrides,  # type: ignore[arg-type]
        )
        return inspection, outcome, factory

    def test_an_exact_receipt_is_an_install(self) -> None:
        process = FakeProcess(stdout=success_line(), returncode=0)
        inspection, outcome, factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_INSTALLED)
        self.assertIsNone(outcome.code)
        assert outcome.receipt is not None
        self.assertEqual(outcome.receipt["artifact_digest"], ARCHIVE_DIGEST)
        self.assertEqual(factory.called, 2, "one revalidation probe, then one install")
        self.assertTrue(process.stdin.closed)
        self.assertGreater(len(process.stdin.buffer), 0)
        document = DRIVER.render_outcome(inspection, outcome)
        self.assertEqual(document["activation"], "not_activated")

    def test_the_install_argv_is_the_fixed_shape_with_strict_host_keys(self) -> None:
        process = FakeProcess(stdout=success_line(), returncode=0)
        _inspection, _outcome, factory = self._apply(process)
        command = factory.commands[1]
        self.assertEqual(command[0], SSH)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("PermitLocalCommand=no", command)
        self.assertIn("--", command)
        self.assertNotIn("-o", command[command.index("--") :])
        for option in command:
            self.assertNotIn("StrictHostKeyChecking=no", option)
            self.assertNotIn("UserKnownHostsFile", option)
        self.assertNotIn("shell", factory.kwargs[1])

    def test_a_receipt_for_another_workspace_is_unknown_not_installed(self) -> None:
        process = FakeProcess(stdout=success_line(workspace_uid=OTHER_UID), returncode=0)
        _inspection, outcome, _factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "DRIVER_RECEIPT_UNRECOGNISED")
        self.assertIsNotNone(outcome.guidance)

    def test_a_receipt_for_another_artifact_is_unknown_not_installed(self) -> None:
        process = FakeProcess(
            stdout=success_line(digest="sha256:" + "ef" * 32), returncode=0
        )
        _inspection, outcome, _factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)

    def test_a_truncated_success_line_is_unknown(self) -> None:
        process = FakeProcess(stdout=success_line()[:-8], returncode=0)
        _inspection, outcome, _factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)

    def test_a_clean_refusal_is_reported_with_its_code(self) -> None:
        process = FakeProcess(stderr=refuse_line("INSTALL_ROOT_EXISTS"), returncode=2)
        _inspection, outcome, _factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_REFUSED)
        self.assertEqual(outcome.code, "INSTALL_ROOT_EXISTS")
        self.assertIsNone(outcome.guidance)

    def test_unrecognised_remote_text_never_reaches_the_report(self) -> None:
        leak = b"ssh: Permission denied for user secret@internal.example\n"
        process = FakeProcess(stderr=leak, returncode=255)
        _inspection, outcome, _factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_REFUSED)
        self.assertEqual(outcome.code, "REMOTE_INSTALL_FAILED")
        rendered = DRIVER.encode_document(
            DRIVER.render_outcome(_inspection, outcome)
        ).decode("ascii")
        self.assertNotIn("secret", rendered)
        self.assertNotIn("internal.example", rendered)

    def test_an_ambiguous_commit_stays_unknown_and_is_not_retried(self) -> None:
        process = FakeProcess(
            stderr=refuse_line("REMOTE_INSTALL_COMMIT_UNKNOWN"), returncode=2
        )
        _inspection, outcome, factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "REMOTE_INSTALL_COMMIT_UNKNOWN")
        assert outcome.guidance is not None
        self.assertIn(".workstack-install.json", outcome.guidance)
        self.assertEqual(factory.called, 2)

    def test_a_stalled_remote_is_killed_and_reported_unknown(self) -> None:
        process = FakeProcess(stall_stdout=True, returncode=0)
        _inspection, outcome, factory = self._apply(process, timeout=0.2)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "DRIVER_TIMEOUT")
        self.assertTrue(process.killed)
        self.assertEqual(outcome.cleanup, DRIVER.CLEANUP_SETTLED)
        self.assertEqual(factory.called, 2)

    def test_oversized_remote_output_is_bounded_and_unknown(self) -> None:
        flood = b"x" * (DRIVER.MAX_STDOUT + 64) + b"\n"
        process = FakeProcess(stdout=flood, returncode=0)
        _inspection, outcome, _factory = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "DRIVER_OVERSIZE")
        self.assertTrue(process.killed)


class NoActivationTests(unittest.TestCase):
    def test_the_driver_imports_no_activation_or_ssot_module(self) -> None:
        source = (SHELL / "remote_provision_driver.py").read_text(encoding="utf-8")
        for banned in (
            "ssot_connection",
            "connection_registry",
            "remote_owner",
            "workstack_desktop",
            "v4_activation_binding",
            "connection_activation_evidence",
        ):
            self.assertNotIn(banned, source, f"driver must not reach {banned}")

    def test_the_driver_never_starts_a_server_or_migrates_data(self) -> None:
        source = (SHELL / "remote_provision_driver.py").read_text(encoding="utf-8")
        for banned in (
            "join_serve_command",
            "serve_tokens",
            "stop_owned",
            "stop-owned",
            "session_token",
            "store_schema",
            "migration",
        ):
            self.assertNotIn(banned, source, f"driver must not reach {banned}")

    def test_the_driver_uses_no_shell(self) -> None:
        source = (SHELL / "remote_provision_driver.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)


class CommandLineSurfaceTests(unittest.TestCase):
    def test_there_is_no_force_steal_or_delete_flag(self) -> None:
        parser = CLI.build_parser()
        options = {
            option for action in parser._actions for option in action.option_strings
        }
        for banned in (
            "--force",
            "--overwrite",
            "--replace",
            "--steal",
            "--delete",
            "--remove",
            "--retry",
            "--activate",
            "--no-strict-host-key-checking",
        ):
            self.assertNotIn(banned, options)

    def test_every_target_field_is_required_so_no_host_is_chosen_for_you(self) -> None:
        parser = CLI.build_parser()
        required = {
            action.option_strings[0]
            for action in parser._actions
            if action.required and action.option_strings
        }
        for field in (
            "--ssh-host-alias",
            "--ssh-executable",
            "--remote-python",
            "--install-root",
            "--data-root",
            "--owner",
            "--expected-workspace-uid",
            "--archive",
            "--sidecar",
            "--expect-digest",
        ):
            self.assertIn(field, required)

    def test_apply_is_off_by_default(self) -> None:
        arguments = CLI.build_parser().parse_args(
            [
                "--ssh-host-alias", ALIAS,
                "--ssh-executable", SSH,
                "--remote-python", REMOTE_PYTHON,
                "--install-root", INSTALL_ROOT,
                "--data-root", DATA_ROOT,
                "--owner", OWNER,
                "--expected-workspace-uid", WORKSPACE_UID,
                "--archive", "archive.zip",
                "--sidecar", "sidecar.json",
                "--expect-digest", ARCHIVE_DIGEST,
            ]
        )
        self.assertFalse(arguments.apply)

    def test_the_cli_reads_no_ssh_config_and_no_store(self) -> None:
        source = CLI_PATH.read_text(encoding="utf-8")
        for banned in (
            "ssh_config_discovery",
            "ssot_connection",
            "connection_registry",
            "find_ssh_executable",
            "known_hosts",
        ):
            self.assertNotIn(banned, source, f"CLI must not reach {banned}")

    def test_an_archive_that_is_not_the_expected_digest_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="ws-driver-cli-") as directory:
            root = Path(directory)
            archive = root / "archive.zip"
            sidecar = root / "sidecar.json"
            archive.write_bytes(ARCHIVE_BYTES)
            sidecar.write_bytes(sidecar_bytes())
            self.assertEqual(
                CLI.load_artifact(str(archive), str(sidecar), ARCHIVE_DIGEST).digest,
                ARCHIVE_DIGEST,
            )
            with self.assertRaises(DRIVER.DriverError) as caught:
                CLI.load_artifact(str(archive), str(sidecar), "sha256:" + "00" * 32)
            self.assertEqual(caught.exception.code, "DRIVER_ARTIFACT_INVALID")


class RetainedEvidenceTests(unittest.TestCase):
    """B1: what a caller kept hold of is never accepted as live proof."""

    def _refused(self):
        """One inspection whose live facts refuse: the install root exists."""

        facts = facts_document(
            install_exists=True,
            install_owner=OWNER,
            install_product="0.9.0",
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest="sha256:" + "cd" * 32,
        )
        inspection, artifact, _factory = inspect_with(facts)
        self.assertEqual(inspection.decision, "refused")
        return inspection, artifact

    def _refuse_apply(self, inspection, artifact) -> str:
        """Apply and require that not one process was created."""

        factory = RecordingFactory()
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=factory,
            )
        self.assertEqual(factory.called, 0, "a refusal must not create a process")
        return caught.exception.code

    def test_retained_facts_and_plan_cannot_be_edited_in_place(self) -> None:
        inspection, _artifact = self._refused()
        with self.assertRaises(TypeError):
            inspection.plan["decision"] = "install_needed"  # type: ignore[index]
        install = inspection.facts["install"]
        assert isinstance(install, Mapping)
        with self.assertRaises(TypeError):
            install["exists"] = False  # type: ignore[index]
        self.assertEqual(inspection.decision, "refused")

    def test_the_review_counterexample_creates_no_install_process(self) -> None:
        """The exact scenario the review demonstrated, now refused."""

        inspection, artifact = self._refused()
        forged = forge_inspection(inspection)
        plan = forged.plan
        assert isinstance(plan, dict)
        plan["decision"] = "install_needed"
        facts = forged.facts
        assert isinstance(facts, dict)
        install = facts["install"]
        assert isinstance(install, dict)
        install["exists"] = False
        self.assertEqual(forged.decision, "install_needed")
        self.assertEqual(self._refuse_apply(forged, artifact), "DRIVER_EVIDENCE_MISMATCH")

    def test_an_edited_facts_document_alone_is_a_mismatch(self) -> None:
        inspection, artifact, _factory = inspect_with(facts_document())
        forged = forge_inspection(inspection)
        facts = forged.facts
        assert isinstance(facts, dict)
        data = facts["data"]
        assert isinstance(data, dict)
        data["workspace_id"] = OTHER_UID
        self.assertEqual(self._refuse_apply(forged, artifact), "DRIVER_EVIDENCE_MISMATCH")

    def test_a_hand_built_inspection_is_unissued_even_when_it_is_consistent(self) -> None:
        """A forged document that digests to its own evidence is still not proof."""

        inspection, artifact = self._refused()
        forged = forge_inspection(inspection)
        plan = forged.plan
        assert isinstance(plan, dict)
        plan["decision"] = "install_needed"
        facts = forged.facts
        assert isinstance(facts, dict)
        install = facts["install"]
        assert isinstance(install, dict)
        install["exists"] = False
        consistent = forge_inspection(
            forged,
            facts=facts,
            plan=plan,
            evidence=DRIVER.compute_evidence(forged.binding, facts, plan),
        )
        self.assertEqual(
            self._refuse_apply(consistent, artifact), "DRIVER_EVIDENCE_UNISSUED"
        )

    def test_an_inspection_carrying_no_evidence_is_unissued(self) -> None:
        inspection, artifact, _factory = inspect_with(facts_document())
        self.assertEqual(
            self._refuse_apply(forge_inspection(inspection, evidence=""), artifact),
            "DRIVER_EVIDENCE_UNISSUED",
        )

    def test_a_malformed_retained_document_is_refused_not_parsed(self) -> None:
        inspection, artifact, _factory = inspect_with(facts_document())
        broken = forge_inspection(inspection, facts={"install": object()})
        self.assertEqual(self._refuse_apply(broken, artifact), "DRIVER_EVIDENCE_INVALID")

    def test_a_frozen_inspection_still_renders_as_plain_json(self) -> None:
        inspection, _artifact, _factory = inspect_with(facts_document())
        document = DRIVER.render_inspection(inspection)
        decoded = json.loads(DRIVER.encode_document(document))
        self.assertEqual(decoded["evidence"], inspection.evidence)
        self.assertEqual(decoded["plan"]["decision"], "install_needed")
        self.assertEqual(decoded["activation"], "not_activated")


class RevalidationTests(unittest.TestCase):
    """B1: the install is authorised by a second reading, not the first."""

    def _apply_against(self, inspected: dict[str, object], current: dict[str, object]) -> str:
        inspection, artifact, _factory = inspect_with(inspected)
        self.assertEqual(inspection.decision, "install_needed")
        factory = RecordingFactory(probe_process(current))
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=factory,
            )
        self.assertEqual(factory.called, 1, "only the read-only revalidation probe ran")
        return caught.exception.code

    def test_a_target_occupied_since_inspect_refuses_before_the_install(self) -> None:
        occupied = facts_document(
            install_exists=True,
            install_owner=OWNER,
            install_product="0.9.0",
            install_protocol=REMOTE_PROTOCOL_VERSION,
            install_digest="sha256:" + "cd" * 32,
        )
        self.assertEqual(
            self._apply_against(facts_document(), occupied), "DRIVER_PLAN_REFUSED"
        )

    def test_a_workspace_that_changed_since_inspect_refuses(self) -> None:
        self.assertEqual(
            self._apply_against(facts_document(), facts_document(workspace_id=OTHER_UID)),
            "DRIVER_PLAN_REFUSED",
        )

    def test_a_target_that_merely_changed_refuses_as_changed(self) -> None:
        """Still installable, but no longer the target that was inspected."""

        self.assertEqual(
            self._apply_against(facts_document(), facts_document(python_version="3.13.1")),
            "DRIVER_TARGET_CHANGED",
        )

    def test_a_target_that_cannot_be_read_again_refuses(self) -> None:
        """Refusing to verify is a refusal, never an implicit approval."""

        inspection, artifact, _factory = inspect_with(facts_document())
        factory = RecordingFactory(FakeProcess(stderr=b"SSH_AUTH_FAILED\n", returncode=255))
        with self.assertRaises(DRIVER.DriverError) as caught:
            DRIVER.apply_remote_install(
                inspection,
                artifact,
                apply=True,
                ssh_executable=SSH,
                process_factory=factory,
            )
        self.assertEqual(caught.exception.code, "DRIVER_REVALIDATION_FAILED")
        self.assertEqual(factory.called, 1)

    def test_the_revalidation_leg_is_the_read_only_probe(self) -> None:
        facts = facts_document()
        inspection, artifact, _factory = inspect_with(facts)
        factory = RecordingFactory(
            probe_process(facts), FakeProcess(stdout=success_line(), returncode=0)
        )
        outcome = DRIVER.apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=SSH,
            process_factory=factory,
        )
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_INSTALLED)
        probe_command, install_command = factory.commands
        self.assertNotEqual(probe_command[-1], install_command[-1])
        for command in (probe_command, install_command):
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("BatchMode=yes", command)


class UnkillableProcess(FakeProcess):
    """A child the OS refuses to kill."""

    def kill(self) -> None:
        raise OSError(1, "operation not permitted")


class UnreapableProcess(FakeProcess):
    """A child that takes the kill but never becomes reapable."""

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd=SSH, timeout=timeout)


def exchange_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("workstack-bounded-exchange")
    ]


def await_exchange_threads(limit: float = 10.0) -> None:
    """Let a deliberately unsettled reader finish before the next test looks."""

    deadline = time.monotonic() + limit
    while exchange_threads() and time.monotonic() < deadline:
        time.sleep(0.02)


class CleanupFailureTests(unittest.TestCase):
    """B2: a cleanup that did not work is reported, never swallowed."""

    def _apply(self, process: FakeProcess, **overrides: object):
        facts = facts_document()
        inspection, artifact, _factory = inspect_with(facts)
        factory = RecordingFactory(probe_process(facts), process)
        outcome = DRIVER.apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=SSH,
            process_factory=factory,
            timeout=0.2,
            **overrides,  # type: ignore[arg-type]
        )
        return inspection, outcome

    def _assert_unsettled_but_unknown(self, outcome) -> None:
        """The commit stays unknown; cleanup failure adds to it, never subtracts."""

        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "DRIVER_TIMEOUT")
        self.assertEqual(outcome.cleanup, DRIVER.CLEANUP_UNSETTLED)
        assert outcome.guidance is not None
        self.assertIn("could not be confirmed", outcome.guidance)
        self.assertIn(".workstack-install.json", outcome.guidance)
        self.assertNotIn("retry the install", outcome.guidance)

    def test_a_refused_kill_is_reported_and_the_commit_stays_unknown(self) -> None:
        _inspection, outcome = self._apply(UnkillableProcess(stall_stdout=True))
        self._assert_unsettled_but_unknown(outcome)

    def test_a_child_that_never_reaps_is_reported_and_stays_unknown(self) -> None:
        _inspection, outcome = self._apply(UnreapableProcess(stall_stdout=True))
        self._assert_unsettled_but_unknown(outcome)

    def test_cleanup_failure_reaches_the_rendered_report(self) -> None:
        inspection, outcome = self._apply(UnkillableProcess(stall_stdout=True))
        document = DRIVER.render_outcome(inspection, outcome)
        self.assertEqual(document["cleanup"], DRIVER.CLEANUP_UNSETTLED)
        self.assertEqual(document["outcome"], DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(document["activation"], "not_activated")

    def test_an_ordinary_timeout_settles_and_says_so(self) -> None:
        process = FakeProcess(stall_stdout=True)
        _inspection, outcome = self._apply(process)
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.cleanup, DRIVER.CLEANUP_SETTLED)
        self.assertNotIn("could not be confirmed", outcome.guidance or "")
        self.assertTrue(process.killed)
        self.assertEqual(exchange_threads(), [])

    def test_a_settled_exchange_closes_its_streams(self) -> None:
        process = FakeProcess(stall_stdout=True)
        self._apply(process)
        assert isinstance(process.stdout, StallingStream)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stdin.closed)


class ExchangeCleanupTests(unittest.TestCase):
    """B2 at the extraction boundary: handles are kept and settled, or reported."""

    def test_a_reader_that_outlives_the_grace_is_reported_not_closed_under(self) -> None:
        process = FakeProcess(stall_stdout=True)
        assert isinstance(process.stdout, StallingStream)
        process.stdout.seconds = 0.5
        self.addCleanup(await_exchange_threads)
        exchange = EXCHANGE.BoundedExchange(process)
        grace = EXCHANGE.SETTLE_GRACE_SECONDS
        EXCHANGE.SETTLE_GRACE_SECONDS = 0.05
        try:
            exchange.write(b"payload", 1.0)
            with self.assertRaises(EXCHANGE.ExchangeError) as caught:
                exchange.drain(1024, 1024, 0.1)
        finally:
            EXCHANGE.SETTLE_GRACE_SECONDS = grace
        self.assertEqual(caught.exception.code, EXCHANGE.TIMEOUT)
        self.assertEqual(caught.exception.cleanup, EXCHANGE.CLEANUP_UNSETTLED)
        self.assertIn("reader", exchange.cleanup_failures)
        self.assertTrue(process.killed, "the child is still killed first")
        self.assertFalse(
            process.stdout.closed, "a pipe is never closed under a live reader"
        )

    def test_a_kill_failure_and_a_reap_failure_are_both_named(self) -> None:
        exchange = EXCHANGE.BoundedExchange(UnkillableProcess(stall_stdout=True))
        with self.assertRaises(EXCHANGE.ExchangeError):
            exchange.drain(1024, 1024, 0.1)
        self.assertIn("kill", exchange.cleanup_failures)
        self.assertIn("reap", exchange.cleanup_failures)

    def test_the_exchange_only_ever_stops_the_process_it_was_given(self) -> None:
        mine = FakeProcess(stall_stdout=True)
        someone_else = FakeProcess(stdout=success_line(), returncode=0)
        exchange = EXCHANGE.BoundedExchange(mine)
        with self.assertRaises(EXCHANGE.ExchangeError):
            exchange.drain(1024, 1024, 0.1)
        self.assertTrue(mine.killed)
        self.assertFalse(someone_else.killed, "no process this exchange did not create")

    def test_a_command_that_cannot_start_is_not_a_process(self) -> None:
        def refuse(_command: list[str], **_kwargs: object) -> object:
            raise OSError(2, "no such file")

        with self.assertRaises(EXCHANGE.ExchangeError) as caught:
            EXCHANGE.start_exchange(refuse, [SSH])
        self.assertEqual(caught.exception.code, EXCHANGE.NOT_STARTED)


class LocalFakeSshFactory:
    """Runs a local Python child in place of ssh, over real OS pipes."""

    def __init__(self, *programs: str) -> None:
        self.programs = list(programs)
        self.processes: list[subprocess.Popen] = []

    def __call__(self, _command: list[str], **_kwargs: object) -> subprocess.Popen:
        if not self.programs:
            raise AssertionError("the driver spawned more processes than the test allowed")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-I", "-B", "-c", self.programs.pop(0)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        return process

    def close(self) -> None:
        for process in self.processes:
            if process.poll() is None:  # pragma: no cover - only on a test failure
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def emitting_child(payload: bytes, *, stream: str = "stdout", code: int = 0) -> str:
    """A child that consumes stdin, writes one exact line, and exits."""

    return (
        "import sys;sys.stdin.buffer.read();"
        f"sys.{stream}.buffer.write({payload!r});sys.{stream}.buffer.flush();"
        f"sys.exit({code})"
    )


SLEEPING_CHILD = "import time;time.sleep(30)"


class RealChildProcessTests(unittest.TestCase):
    """A real local child on real pipes -- no network, no ssh, no install."""

    def _factory(self, *programs: str) -> LocalFakeSshFactory:
        factory = LocalFakeSshFactory(*programs)
        self.addCleanup(factory.close)
        return factory

    def _facts_line(self, facts: dict[str, object]) -> bytes:
        text = json.dumps(facts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return text.encode("ascii") + b"\n"

    def test_a_real_exchange_installs_from_an_exact_success_line(self) -> None:
        facts = facts_document()
        line = self._facts_line(facts)
        inspect_factory = self._factory(emitting_child(line))
        artifact = make_artifact()
        inspection = DRIVER.inspect_remote_target(
            make_profile(),
            OWNER,
            artifact,
            ssh_executable=SSH,
            process_factory=inspect_factory,
        )
        self.assertEqual(inspection.decision, "install_needed")
        apply_factory = self._factory(emitting_child(line), emitting_child(success_line()))
        outcome = DRIVER.apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=SSH,
            process_factory=apply_factory,
            timeout=30.0,
            probe_timeout=30.0,
        )
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_INSTALLED)
        self.assertEqual(outcome.cleanup, DRIVER.CLEANUP_SETTLED)
        for process in apply_factory.processes:
            self.assertEqual(process.poll(), 0, "every real child was reaped")

    def test_a_real_child_that_never_reads_stdin_is_bounded_and_reaped(self) -> None:
        facts = facts_document()
        inspect_factory = self._factory(emitting_child(self._facts_line(facts)))
        artifact = make_artifact()
        inspection = DRIVER.inspect_remote_target(
            make_profile(),
            OWNER,
            artifact,
            ssh_executable=SSH,
            process_factory=inspect_factory,
        )
        apply_factory = self._factory(
            emitting_child(self._facts_line(facts)), SLEEPING_CHILD
        )
        started = time.monotonic()
        outcome = DRIVER.apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=SSH,
            process_factory=apply_factory,
            timeout=0.5,
            probe_timeout=30.0,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "DRIVER_TIMEOUT")
        self.assertEqual(outcome.cleanup, DRIVER.CLEANUP_SETTLED)
        self.assertLess(elapsed, 25.0, "the sleeping child never ran to completion")
        install_child = apply_factory.processes[-1]
        self.assertIsNotNone(install_child.poll(), "the real child was reaped")
        self.assertEqual(exchange_threads(), [])

    def test_a_real_child_refusal_never_leaks_its_stderr(self) -> None:
        leak = b"ssh: Permission denied for user secret@internal.example\n"
        facts = facts_document()
        inspect_factory = self._factory(emitting_child(self._facts_line(facts)))
        artifact = make_artifact()
        inspection = DRIVER.inspect_remote_target(
            make_profile(),
            OWNER,
            artifact,
            ssh_executable=SSH,
            process_factory=inspect_factory,
        )
        apply_factory = self._factory(
            emitting_child(self._facts_line(facts)),
            emitting_child(leak, stream="stderr", code=255),
        )
        outcome = DRIVER.apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=SSH,
            process_factory=apply_factory,
            timeout=30.0,
            probe_timeout=30.0,
        )
        self.assertEqual(outcome.outcome, DRIVER.OUTCOME_REFUSED)
        self.assertEqual(outcome.code, "REMOTE_INSTALL_FAILED")
        rendered = DRIVER.encode_document(
            DRIVER.render_outcome(inspection, outcome)
        ).decode("ascii")
        self.assertNotIn("secret", rendered)
        self.assertNotIn("internal.example", rendered)


if __name__ == "__main__":
    unittest.main()
