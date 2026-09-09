"""Unpack stdin payload executes the real helper; SSH argv stays csh-safe."""

from __future__ import annotations

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

import importlib.util

import remote_command_contract as COMMAND  # noqa: E402
import remote_provision_command as INSTALL_ARGV  # noqa: E402
import remote_update_install_ports_transport as TRANSPORT  # noqa: E402
from remote_provision_artifact import admit_artifact  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UNPACK_TESTS = load_module(TESTS / "test_remote_verified_unpack.py", "install_ports_unpack_transport")
DRIVER_TESTS = load_module(TESTS / "test_remote_provision_driver.py", "install_ports_driver_transport")


def run_payload(source: bytes, operation: str, app_dir: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-", operation, "--app-dir", app_dir],
        input=source,
        capture_output=True,
        timeout=60,
        check=False,
    )


class UnpackPayloadExecutionTests(unittest.TestCase):
    def test_generated_payload_places_inspects_and_verifies_without_touching_old_app(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        source = TRANSPORT.build_unpack_payload_source(artifact)
        self.assertIn(b"place_verified_unpack", source)
        self.assertIn(b"inspect_unpack_target", source)
        self.assertIn(b"verify_unpack_identity", source)
        compile(source, "<unpack-payload>", "exec")
        with tempfile.TemporaryDirectory(prefix="workstack-install-ports-") as tmp:
            base = Path(tmp)
            old_app = base / "existing-app"
            data = base / "existing-data"
            old_app.mkdir()
            data.mkdir()
            (old_app / "sentinel").write_text("old release", encoding="utf-8")
            (data / "workspace.json").write_text("must remain", encoding="utf-8")
            target = base / "new-app"
            placed = run_payload(source, "place", str(target))
            self.assertEqual(placed.returncode, 0, placed.stderr.decode("utf-8", "replace"))
            document = json.loads(placed.stdout.decode("utf-8"))
            self.assertEqual(document["outcome"], "unpacked_verified")
            self.assertEqual(document["placement"], "ready_candidate")
            self.assertEqual(document["activation"], "not_activated")
            self.assertFalse(document["ssot_accessed"])
            self.assertEqual(document["artifact_digest"], artifact.digest)
            self.assertEqual((old_app / "sentinel").read_text(encoding="utf-8"), "old release")
            self.assertEqual((data / "workspace.json").read_text(encoding="utf-8"), "must remain")
            inspected = run_payload(source, "inspect", str(target))
            inspect_doc = json.loads(inspected.stdout.decode("utf-8"))
            self.assertEqual(inspect_doc["placement"], "ready_candidate")
            verified = run_payload(source, "verify", str(target))
            verify_doc = json.loads(verified.stdout.decode("utf-8"))
            self.assertEqual(verify_doc["placement"], "identity_verified")
            self.assertEqual(verify_doc["activation"], "not_activated")

    def test_payload_refuses_an_existing_target_without_overwrite(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        source = TRANSPORT.build_unpack_payload_source(artifact)
        with tempfile.TemporaryDirectory(prefix="workstack-install-ports-") as tmp:
            target = Path(tmp) / "occupied"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            placed = run_payload(source, "place", str(target))
            self.assertNotEqual(placed.returncode, 0)
            document = json.loads(placed.stdout.decode("utf-8"))
            self.assertEqual(document["outcome"], "not_ready")
            self.assertEqual(document["code"], "APP_DIRECTORY_ALREADY_EXISTS")
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


class UnpackArgvTests(unittest.TestCase):
    def test_ssh_argv_matches_provision_batch_options_and_csh_safe_tokens(self) -> None:
        profile = DRIVER_TESTS.make_profile()
        command = TRANSPORT.build_ssh_unpack_command(
            profile, DRIVER_TESTS.SSH, "place", profile.remote_app_dir
        )
        install = INSTALL_ARGV.build_ssh_provision_install_command(
            profile, DRIVER_TESTS.OWNER, DRIVER_TESTS.SSH
        )
        self.assertEqual(command[0], DRIVER_TESTS.SSH)
        self.assertEqual(command[1:-2], list(TRANSPORT.OPENSSH_BATCH))
        self.assertEqual(install[1:-2], list(TRANSPORT.OPENSSH_BATCH))
        remote = command[-1]
        for fragment in COMMAND.FORBIDDEN_FRAGMENTS:
            self.assertNotIn(fragment, remote)
        self.assertIn("place --app-dir " + profile.remote_app_dir, remote)
        self.assertTrue(remote.startswith(profile.remote_python))
        tokens = TRANSPORT.unpack_remote_tokens(profile.remote_python, "inspect", profile.remote_app_dir)
        joined = COMMAND.join_remote_tokens(tokens)
        self.assertEqual(joined, " ".join(tokens))
        self.assertNotIn("'", joined)
        self.assertNotIn('"', joined)

    def test_exchange_writes_the_real_payload_to_stdin(self) -> None:
        archive, sidecar = UNPACK_TESTS.make_artifact()
        artifact = admit_artifact(archive, sidecar)
        source = TRANSPORT.build_unpack_payload_source(artifact)
        process = DRIVER_TESTS.FakeProcess(
            stdout=TRANSPORT.encode_line({"outcome": "unpacked_verified", "placement": "ready_candidate"}),
            returncode=0,
        )

        def factory(command: list[str], **_kwargs: object) -> object:
            self.assertEqual(command[0], DRIVER_TESTS.SSH)
            self.assertIn("place --app-dir", command[-1])
            return process

        result = TRANSPORT.run_unpack_exchange(
            factory,
            TRANSPORT.build_ssh_unpack_command(
                DRIVER_TESTS.make_profile(), DRIVER_TESTS.SSH, "place", TARGET_PLACEHOLDER
            ),
            source,
            5.0,
        )
        self.assertEqual(result.kind, "output")
        self.assertIsNotNone(result.document)
        written = bytes(process.stdin.buffer)
        self.assertEqual(written, source)
        self.assertTrue(process.stdin.closed)


TARGET_PLACEHOLDER = "/fixture/driver-owner/app-next"


if __name__ == "__main__":
    unittest.main()
