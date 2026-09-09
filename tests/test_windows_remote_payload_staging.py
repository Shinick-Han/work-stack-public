"""The Windows installer must ship exactly the Linux artifact it was given.

Every case here stages real temporary files through the real stager, which
applies the product's own ``remote_provision_artifact`` admission. Nothing is
downloaded, no archive is unpacked, no installer is built or executed, and no
SSH, host or SSOT is touched: the archive is opaque bytes throughout, exactly
as it is to the packaging step under test.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
STAGER_PATH = ROOT / "scripts" / "windows" / "Stage-WorkStackRemoteBundle.py"

for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from workstack import REMOTE_PROTOCOL_VERSION, __version__  # noqa: E402

import remote_provision_installer as INSTALLER  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("workstack_remote_bundle_stager", STAGER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
STAGER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = STAGER
_SPEC.loader.exec_module(STAGER)

MANIFEST_DIGEST = "sha256:" + "cd" * 32
#: Opaque to the stager, and deliberately not a real zip: this stage admits a
#: selected pair, it does not open archives.
ARCHIVE_BYTES = b"PK\x03\x04" + b"linux-remote-artifact-payload" * 8
CANONICAL_STEM = "WorkStack-Linux-%s-%s" % (__version__, INSTALLER.TARGET_ID)


def sidecar_document(
    *,
    product_version: str = __version__,
    protocol_version: int = REMOTE_PROTOCOL_VERSION,
    archive_bytes: bytes = ARCHIVE_BYTES,
    manifest: str = MANIFEST_DIGEST,
    declared_size: int | None = None,
    declared_digest: str | None = None,
) -> dict:
    """The sidecar the Linux builder writes beside the archive it just made."""

    digest = declared_digest or ("sha256:" + hashlib.sha256(archive_bytes).hexdigest())
    return {
        "schema_version": 1,
        "product_version": product_version,
        "remote_protocol_version": protocol_version,
        "source_commit": "0" * 40,
        "target_id": INSTALLER.TARGET_ID,
        "artifact_manifest_sha256": manifest,
        "archive": {
            "name": "WorkStack-Linux-%s-%s.zip" % (product_version, INSTALLER.TARGET_ID),
            "size": len(archive_bytes) if declared_size is None else declared_size,
            "sha256": digest,
        },
    }


class RemotePayloadStagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.mkdtemp(prefix="workstack-remote-stage-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.base = Path(self.temporary)
        self.release = self.base / "release"
        self.release.mkdir()
        self.payload = self.base / "payload"
        self.payload.mkdir()
        self.archive = self.release / (CANONICAL_STEM + ".zip")
        self.sidecar = self.release / (CANONICAL_STEM + ".json")
        self.archive.write_bytes(ARCHIVE_BYTES)
        self.write_sidecar()

    def write_sidecar(self, **overrides: object) -> None:
        document = sidecar_document(**overrides)  # type: ignore[arg-type]
        self.sidecar.write_bytes(json.dumps(document).encode("utf-8"))

    def stage(self, *, expect_version: str = __version__) -> dict:
        return STAGER.stage(ROOT, self.payload, self.archive, self.sidecar, expect_version)

    def refuse(self, *, expect_version: str = __version__) -> STAGER.StagingError:
        with self.assertRaises(STAGER.StagingError) as raised:
            self.stage(expect_version=expect_version)
        return raised.exception

    def remote(self) -> Path:
        return self.payload / "remote"

    def staged_names(self) -> list[str]:
        remote = self.remote()
        if not remote.is_dir():
            return []
        return sorted(path.name for path in remote.iterdir())

    # -- the paired positive path ------------------------------------------

    def test_an_admitted_pair_lands_at_the_canonical_destination_unchanged(self) -> None:
        document = self.stage()

        self.assertEqual(
            [CANONICAL_STEM + ".json", CANONICAL_STEM + ".zip"], self.staged_names()
        )
        self.assertEqual(ARCHIVE_BYTES, (self.remote() / (CANONICAL_STEM + ".zip")).read_bytes())
        self.assertEqual(
            self.sidecar.read_bytes(), (self.remote() / (CANONICAL_STEM + ".json")).read_bytes()
        )
        self.assertEqual("remote/" + CANONICAL_STEM + ".zip", document["archive"])
        self.assertEqual("remote/" + CANONICAL_STEM + ".json", document["sidecar"])
        self.assertEqual(__version__, document["product_version"])
        self.assertEqual(REMOTE_PROTOCOL_VERSION, document["remote_protocol_version"])
        self.assertEqual(
            "sha256:" + hashlib.sha256(ARCHIVE_BYTES).hexdigest(), document["artifact_digest"]
        )
        self.assertEqual(MANIFEST_DIGEST, document["artifact_manifest_sha256"])
        self.assertEqual(len(ARCHIVE_BYTES), document["archive_size"])

    def test_the_staged_archive_is_the_name_the_remote_installer_expects(self) -> None:
        self.stage()

        self.assertEqual(INSTALLER.ARCHIVE_NAME, CANONICAL_STEM + ".zip")
        self.assertTrue((self.remote() / INSTALLER.ARCHIVE_NAME).is_file())

    def test_the_destination_never_comes_from_the_input_filename(self) -> None:
        """An input named anything at all still lands at the canonical name."""

        hostile = self.release / "..--..--WorkStack-Linux-9.9.9-evil.zip"
        self.archive.rename(hostile)
        self.archive = hostile

        self.stage()

        self.assertEqual(
            [CANONICAL_STEM + ".json", CANONICAL_STEM + ".zip"], self.staged_names()
        )
        self.assertEqual([], sorted(path.name for path in self.payload.iterdir() if path.name != "remote"))

    # -- refusals: the pair itself -----------------------------------------

    def test_a_missing_archive_refuses_and_stages_nothing(self) -> None:
        self.archive.unlink()

        error = self.refuse()

        self.assertEqual("INPUT", error.code)
        self.assertFalse(self.remote().exists())

    def test_a_missing_sidecar_refuses_and_stages_nothing(self) -> None:
        self.sidecar.unlink()

        error = self.refuse()

        self.assertEqual("INPUT", error.code)
        self.assertFalse(self.remote().exists())

    def test_one_file_used_as_both_inputs_refuses(self) -> None:
        self.sidecar = self.archive

        error = self.refuse()

        self.assertEqual("INPUT", error.code)
        self.assertFalse(self.remote().exists())

    def test_a_corrupt_archive_digest_refuses_and_stages_nothing(self) -> None:
        self.write_sidecar(declared_digest="sha256:" + "00" * 32)

        error = self.refuse()

        self.assertEqual("ADMISSION", error.code)
        self.assertIn("digest", error.detail)
        self.assertFalse(self.remote().exists())

    def test_an_archive_rewritten_after_its_sidecar_refuses(self) -> None:
        self.archive.write_bytes(ARCHIVE_BYTES + b"tampered")

        error = self.refuse()

        self.assertEqual("ADMISSION", error.code)
        self.assertFalse(self.remote().exists())

    def test_a_declared_size_that_does_not_match_refuses(self) -> None:
        self.write_sidecar(declared_size=len(ARCHIVE_BYTES) + 1)

        error = self.refuse()

        self.assertEqual("ADMISSION", error.code)
        self.assertIn("size", error.detail)
        self.assertFalse(self.remote().exists())

    def test_a_sidecar_that_is_not_one_json_object_refuses(self) -> None:
        self.sidecar.write_bytes(b"[]")

        error = self.refuse()

        self.assertEqual("ADMISSION", error.code)
        self.assertFalse(self.remote().exists())

    def test_an_empty_archive_refuses(self) -> None:
        self.archive.write_bytes(b"")

        error = self.refuse()

        self.assertEqual("ADMISSION", error.code)
        self.assertFalse(self.remote().exists())

    # -- refusals: identity -------------------------------------------------

    def test_an_artifact_for_another_version_refuses(self) -> None:
        superseded = "1.0.12"
        self.write_sidecar(product_version=superseded)

        error = self.refuse()

        self.assertEqual("VERSION", error.code)
        self.assertIn(superseded, error.detail)
        self.assertIn(__version__, error.detail)
        self.assertFalse(self.remote().exists())

    def test_a_windows_build_version_the_artifact_does_not_carry_refuses(self) -> None:
        error = self.refuse(expect_version="1.0.12")

        self.assertEqual("VERSION", error.code)
        self.assertFalse(self.remote().exists())

    def test_a_pair_agreeing_on_a_version_the_engine_does_not_ship_refuses(self) -> None:
        """Both sides can agree and still be wrong for the shipped engine."""

        unknown = "9.9.9"
        self.write_sidecar(product_version=unknown)

        error = self.refuse(expect_version=unknown)

        self.assertEqual("VERSION", error.code)
        self.assertIn(INSTALLER.PRODUCT, error.detail)
        self.assertFalse(self.remote().exists())

    def test_a_non_release_version_string_refuses_before_any_path_is_built(self) -> None:
        error = self.refuse(expect_version="../../evil")

        self.assertEqual("VERSION", error.code)
        self.assertFalse(self.remote().exists())

    def test_an_artifact_for_an_unsupported_remote_protocol_refuses(self) -> None:
        self.write_sidecar(protocol_version=INSTALLER.PROTOCOL + 41)

        error = self.refuse()

        self.assertEqual("PROTOCOL", error.code)
        self.assertIn(str(INSTALLER.PROTOCOL), error.detail)
        self.assertFalse(self.remote().exists())

    # -- refusals: the destination ------------------------------------------

    def test_an_existing_remote_directory_is_never_overwritten(self) -> None:
        self.remote().mkdir()
        occupied = self.remote() / (CANONICAL_STEM + ".zip")
        occupied.write_bytes(b"an earlier artifact")

        error = self.refuse()

        self.assertEqual("DESTINATION", error.code)
        self.assertEqual(b"an earlier artifact", occupied.read_bytes())

    def test_a_second_staging_call_refuses_rather_than_replacing_the_first(self) -> None:
        self.stage()
        staged = (self.remote() / (CANONICAL_STEM + ".zip")).read_bytes()

        error = self.refuse()

        self.assertEqual("DESTINATION", error.code)
        self.assertEqual(staged, (self.remote() / (CANONICAL_STEM + ".zip")).read_bytes())

    def test_a_source_already_inside_the_payload_refuses(self) -> None:
        inside = self.payload / "smuggled.zip"
        inside.write_bytes(ARCHIVE_BYTES)
        self.archive = inside

        error = self.refuse()

        self.assertEqual("INPUT", error.code)
        self.assertFalse(self.remote().exists())
        self.assertEqual(ARCHIVE_BYTES, inside.read_bytes())

    def test_a_payload_root_that_does_not_exist_refuses(self) -> None:
        self.payload = self.base / "absent"

        error = self.refuse()

        self.assertEqual("DESTINATION", error.code)
        self.assertFalse(self.payload.exists())


class RemotePayloadStagerCommandTest(unittest.TestCase):
    """The command surface the builder actually invokes.

    The builder reads exit status and one JSON summary line, so both are
    exercised through a real child process.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.mkdtemp(prefix="workstack-remote-stage-cli-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.base = Path(self.temporary)
        self.payload = self.base / "payload"
        self.payload.mkdir()
        self.archive = self.base / (CANONICAL_STEM + ".zip")
        self.sidecar = self.base / (CANONICAL_STEM + ".json")
        self.archive.write_bytes(ARCHIVE_BYTES)
        self.sidecar.write_bytes(json.dumps(sidecar_document()).encode("utf-8"))

    def run_stager(self, *, expect_version: str = __version__) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(STAGER_PATH),
                "--source-root", str(ROOT),
                "--payload", str(self.payload),
                "--archive", str(self.archive),
                "--sidecar", str(self.sidecar),
                "--expect-version", expect_version,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    def test_the_command_succeeds_and_prints_one_json_summary_line(self) -> None:
        completed = self.run_stager()

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(1, len(lines), completed.stdout)
        document = json.loads(lines[0])
        self.assertEqual("remote/" + CANONICAL_STEM + ".zip", document["archive"])
        self.assertEqual(__version__, document["product_version"])
        self.assertTrue((self.payload / "remote" / (CANONICAL_STEM + ".zip")).is_file())

    def test_the_command_fails_closed_and_prints_no_summary_on_refusal(self) -> None:
        self.archive.write_bytes(ARCHIVE_BYTES + b"tampered")

        completed = self.run_stager()

        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stdout.strip())
        self.assertIn("ADMISSION", completed.stderr)
        self.assertFalse((self.payload / "remote").exists())


if __name__ == "__main__":
    unittest.main()
