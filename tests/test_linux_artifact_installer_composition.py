"""Composition contract: real builder output must pass real installer admission.

The builder and the standalone Linux installer engine are developed apart, so
this module joins them without a stand-in on either side. The builder really
runs over a temporary git source and wheelhouse, and the bytes it writes are fed
verbatim into ``remote_provision_installer._admit_artifact``. Nothing here
mutates the archive or the installer's admission rules on the accepted path.

Temporary source/wheelhouse/absent output only. No live .artifacts, profile,
home, network, SSH or production-artifact claim.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import random
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from test_linux_remote_artifact import BUILDER
from test_linux_remote_artifact import FIXTURE_ROOT
from test_linux_remote_artifact import ROOT
from test_linux_remote_artifact import TARGET
from test_linux_remote_artifact import happy_fixture


SHELL = ROOT / "desktop" / "python-webview-shell"


def load_installer():
    """Import the shipped installer engine exactly as the remote host would."""

    if str(SHELL) not in sys.path:
        sys.path.insert(0, str(SHELL))
    path = SHELL / "remote_provision_installer.py"
    spec = importlib.util.spec_from_file_location("remote_provision_installer", path)
    if spec is None or spec.loader is None:
        raise AssertionError("installer module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALLER = load_installer()


def rewrite_member_modes(archive_bytes: bytes, external_attr: int) -> bytes:
    """Re-emit an archive with a different external mode on every member.

    Only the negative case uses this. It reproduces the shape the QR packager
    used to leave behind (a bare permission with no file-type bit) so the test
    proves the installer still refuses it and the builder no longer emits it.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for info in source.infolist():
                clone = zipfile.ZipInfo(info.filename, date_time=BUILDER.ZIP_TIMESTAMP)
                clone.create_system = BUILDER.ZIP_CREATE_SYSTEM
                clone.compress_type = zipfile.ZIP_DEFLATED
                clone.external_attr = external_attr
                output.writestr(clone, source.read(info))
    return buffer.getvalue()


def resign_sidecar(sidecar_bytes: bytes, archive_bytes: bytes) -> bytes:
    """Restate the sidecar over rewritten archive bytes, keeping it canonical."""

    document = json.loads(sidecar_bytes.decode("utf-8"))
    document["archive"]["size"] = len(archive_bytes)
    document["archive"]["sha256"] = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    return BUILDER.canonical_json(document)


class BuilderInstallerCompositionTests(unittest.TestCase):
    """Every case here builds a real artifact and admits it with the real engine."""

    def setUp(self) -> None:
        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
        self.workspace = tempfile.TemporaryDirectory(dir=str(FIXTURE_ROOT))
        self.base = Path(self.workspace.name)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def build(self, name: str) -> tuple[bytes, bytes, Path]:
        """Run the real builder at the version the shipped installer admits."""

        source, wheels = happy_fixture(self.base / name, product_version=INSTALLER.PRODUCT)
        self.wheelhouse = wheels
        archive, sidecar = BUILDER.build_artifact(source, wheels, self.base / (name + "-out"), TARGET)
        return archive.read_bytes(), sidecar.read_bytes(), archive

    def test_canonical_writer_applies_level_nine_to_member_bytes(self) -> None:
        from scripts.linux_remote_artifact_zip import create_canonical_zip

        rng = random.Random(17)
        data = bytes(rng.randrange(2) + 65 for _ in range(50000))
        def compressed(level):
            encoder = zlib.compressobj(level, zlib.DEFLATED, -15)
            return encoder.compress(data) + encoder.flush()
        expected = compressed(9)
        self.assertTrue(expected != compressed(-1), "sentinel must distinguish default compression")
        staging = self.base / "compression"
        (staging / "payload").mkdir(parents=True)
        (staging / "artifact.json").write_bytes(b"{}")
        (staging / "payload/sample.bin").write_bytes(data)
        archive = self.base / "compression.zip"
        create_canonical_zip(staging, archive, [{"path": "sample.bin"}], lambda code, detail: ValueError(code, detail))
        raw = archive.read_bytes()
        with zipfile.ZipFile(archive) as handle:
            member = handle.getinfo("payload/sample.bin")
            filename_size, extra_size = struct.unpack_from("<HH", raw, member.header_offset + 26)
            start = member.header_offset + 30 + filename_size + extra_size
            self.assertEqual(expected, raw[start:start + member.compress_size])
            self.assertEqual(data, handle.read(member))

    def test_builder_output_is_admitted_by_the_installer_engine(self) -> None:
        archive_bytes, sidecar_bytes, archive = self.build("admit")
        self.assertEqual(INSTALLER.ARCHIVE_NAME, archive.name)

        admitted = INSTALLER._admit_artifact(archive_bytes, sidecar_bytes)

        self.assertEqual("sha256:" + hashlib.sha256(archive_bytes).hexdigest(), admitted["digest"])
        blobs = admitted["blobs"]
        for required in INSTALLER.REQUIRED:
            self.assertIn(required, blobs)
        self.assertEqual({str(item["path"]) for item in admitted["files"]}, set(blobs))
        for record in admitted["files"]:
            data = blobs[str(record["path"])]
            self.assertEqual(record["size"], len(data))
            self.assertEqual(record["sha256"], "sha256:" + hashlib.sha256(data).hexdigest())

    def test_every_builder_member_satisfies_the_installer_member_gate(self) -> None:
        archive_bytes, _sidecar_bytes, _archive = self.build("members")
        self.assertTrue(INSTALLER._eocd_plain(archive_bytes))
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as handle:
            infos = handle.infolist()
            self.assertTrue(infos)
            self.assertLessEqual(len(infos), INSTALLER.MAX_MEMBERS)
            for info in infos:
                self.assertFalse(INSTALLER._zip_rejected(info), info.filename)
                self.assertEqual(0o100644, info.external_attr >> 16, info.filename)
                self.assertEqual(BUILDER.ZIP_TIMESTAMP, tuple(info.date_time), info.filename)
                self.assertEqual(b"", info.extra, info.filename)

    def test_admitted_manifest_keeps_the_shipped_wheel_provenance(self) -> None:
        archive_bytes, _sidecar_bytes, _archive = self.build("provenance")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as handle:
            manifest = json.loads(handle.read("artifact.json").decode("utf-8"))
        compressed = 0
        for record in manifest["wheels"]:
            shipped = self.wheelhouse / str(record["filename"])
            self.assertTrue(shipped.is_file(), record["filename"])
            self.assertEqual(record["sha256"], "sha256:" + hashlib.sha256(shipped.read_bytes()).hexdigest())
            tags = list(record["tags"])
            self.assertEqual(sorted(tags), tags)
            self.assertEqual(INSTALLER._wheel_filename_tags(
                str(record["filename"]), str(record["distribution"]), str(record["version"])
            ), frozenset(tags))
            if len(tags) > 1:
                compressed += 1
        # rpds-py and unicodedata2 really ship compressed platform tag sets.
        self.assertEqual(2, compressed)

    def test_installer_still_refuses_a_bare_permission_archive(self) -> None:
        archive_bytes, sidecar_bytes, _archive = self.build("bare")
        # The historical QR-packager shape: 0644 with no regular-file type bit.
        rewritten = rewrite_member_modes(archive_bytes, 0o644 << 16)
        self.assertNotEqual(archive_bytes, rewritten)

        with self.assertRaises(INSTALLER.InstallerError) as raised:
            INSTALLER._admit_artifact(rewritten, resign_sidecar(sidecar_bytes, rewritten))

        self.assertEqual("REMOTE_ARTIFACT_INVALID", raised.exception.code)

    def test_installer_still_refuses_a_symlink_typed_member(self) -> None:
        archive_bytes, sidecar_bytes, _archive = self.build("symlink")
        rewritten = rewrite_member_modes(archive_bytes, 0o120644 << 16)

        with self.assertRaises(INSTALLER.InstallerError) as raised:
            INSTALLER._admit_artifact(rewritten, resign_sidecar(sidecar_bytes, rewritten))

        self.assertEqual("REMOTE_ARTIFACT_INVALID", raised.exception.code)

    def test_independent_builds_admit_the_same_payload_roster(self) -> None:
        first_archive, first_sidecar, _first = self.build("repeat-a")
        second_archive, second_sidecar, _second = self.build("repeat-b")
        # Independent fixtures differ by commit, so only the shape is compared.
        first = INSTALLER._admit_artifact(first_archive, first_sidecar)
        second = INSTALLER._admit_artifact(second_archive, second_sidecar)
        self.assertEqual(set(first["blobs"]), set(second["blobs"]))


if __name__ == "__main__":
    unittest.main()
