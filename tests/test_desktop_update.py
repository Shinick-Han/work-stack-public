from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_update.py"
SPEC = importlib.util.spec_from_file_location("workstack_update_test", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def manifest_payload(installer: bytes = b"setup", sidecar: bytes | None = None) -> dict[str, object]:
    installer_digest = hashlib.sha256(installer).hexdigest()
    sidecar = sidecar or f"{installer_digest}  WorkStack-Setup-1.0.5.ps1\n".encode()
    return {
        "schema_version": 1,
        "channel": "stable",
        "version": "1.0.5",
        "published_at": "2026-09-01T00:00:00Z",
        "release_url": "https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.5",
        "minimum_remote_protocol": 1,
        "installer": {
            "name": "WorkStack-Setup-1.0.5.ps1",
            "url": "https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.5/WorkStack-Setup-1.0.5.ps1",
            "sha256": installer_digest,
            "size": len(installer),
        },
        "checksum": {
            "name": "WorkStack-Setup-1.0.5.ps1.sha256",
            "url": "https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.5/WorkStack-Setup-1.0.5.ps1.sha256",
            "sha256": hashlib.sha256(sidecar).hexdigest(),
            "size": len(sidecar),
        },
    }


class DesktopUpdateContractTests(unittest.TestCase):
    def test_accepts_one_exact_stable_github_release_manifest(self) -> None:
        payload = manifest_payload()
        manifest = MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4")
        self.assertEqual(manifest.version, "1.0.5")
        self.assertTrue(manifest.is_newer)
        self.assertEqual(manifest.installer.name, "WorkStack-Setup-1.0.5.ps1")

    def test_reports_current_version_and_rejects_rollback_and_noncanonical_versions(self) -> None:
        payload = manifest_payload()
        payload["version"] = "1.0.4"
        payload["release_url"] = "https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.4"
        for kind, suffix in (("installer", ""), ("checksum", ".sha256")):
            asset = payload[kind]  # type: ignore[index]
            name = f"WorkStack-Setup-1.0.4.ps1{suffix}"
            asset["name"] = name  # type: ignore[index]
            asset["url"] = f"https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.4/{name}"  # type: ignore[index]
        self.assertFalse(
            MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4").is_newer,
        )

        payload["version"] = "1.0.3"
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "older") as raised:
            MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4")
        self.assertIsInstance(raised.exception, MODULE.OlderUpdateManifest)
        self.assertEqual(raised.exception.version, "1.0.3")
        self.assertEqual(raised.exception.installed_version, "1.0.4")

        payload["version"] = "01.0.5"
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "version"):
            MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4")

    def test_rejects_cross_repository_and_filename_substitution(self) -> None:
        payload = manifest_payload()
        payload["installer"]["url"] = "https://example.invalid/WorkStack-Setup-1.0.5.ps1"  # type: ignore[index]
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "GitHub release URL"):
            MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4")

        payload = manifest_payload()
        payload["installer"]["name"] = "other.ps1"  # type: ignore[index]
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "filename"):
            MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4")

    def test_manifest_validation_precedence_remains_fail_closed(self) -> None:
        protocol_first = manifest_payload()
        protocol_first["minimum_remote_protocol"] = 0
        protocol_first["version"] = "invalid"
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "minimum_remote_protocol"):
            MODULE.parse_update_manifest(
                json.dumps(protocol_first).encode(), current_version="1.0.4"
            )

        release_first = manifest_payload()
        release_first["release_url"] = "https://example.invalid/release"
        release_first["published_at"] = "invalid"
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "release_url"):
            MODULE.parse_update_manifest(
                json.dumps(release_first).encode(), current_version="1.0.4"
            )

        installer_first = manifest_payload()
        installer_first["installer"]["name"] = "invalid.ps1"  # type: ignore[index]
        installer_first["checksum"]["name"] = "invalid.sha256"  # type: ignore[index]
        with self.assertRaisesRegex(MODULE.UpdateValidationError, "installer filename"):
            MODULE.parse_update_manifest(
                json.dumps(installer_first).encode(), current_version="1.0.4"
            )

    def test_downloads_only_exact_size_digest_and_sidecar_content(self) -> None:
        installer = b"verified setup body"
        digest = hashlib.sha256(installer).hexdigest()
        sidecar = f"{digest}  WorkStack-Setup-1.0.5.ps1\n".encode()
        manifest = MODULE.parse_update_manifest(
            json.dumps(manifest_payload(installer, sidecar)).encode(),
            current_version="1.0.4",
        )

        bodies = {manifest.installer.url: installer, manifest.checksum.url: sidecar}
        with tempfile.TemporaryDirectory() as directory:
            downloaded = MODULE.download_update(
                manifest,
                Path(directory),
                fetch=lambda url, limit: bodies[url],
            )
            self.assertEqual(downloaded.setup_path.read_bytes(), installer)
            self.assertEqual(downloaded.checksum_path.read_bytes(), sidecar)
            self.assertEqual(downloaded.version, "1.0.5")

    def test_download_failure_leaves_no_committed_update(self) -> None:
        installer = b"verified setup body"
        payload = manifest_payload(installer)
        manifest = MODULE.parse_update_manifest(json.dumps(payload).encode(), current_version="1.0.4")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MODULE.UpdateValidationError, "digest"):
                MODULE.download_update(
                    manifest,
                    root,
                    fetch=lambda url, limit: b"x" * limit,
                )
            self.assertFalse((root / "1.0.5" / "ready.json").exists())

    def test_preferences_default_to_automatic_download_and_install_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferences = MODULE.load_update_preferences(root)
            self.assertTrue(preferences.auto_check)
            self.assertTrue(preferences.auto_download)
            self.assertTrue(preferences.install_on_exit)
            MODULE.save_update_preferences(root, MODULE.UpdatePreferences(False, False, False))
            self.assertEqual(
                MODULE.load_update_preferences(root),
                MODULE.UpdatePreferences(False, False, False),
            )


if __name__ == "__main__":
    unittest.main()
