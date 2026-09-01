from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
MODULE_PATH = SHELL / "connection_registry_compat.py"
SPEC = importlib.util.spec_from_file_location("connection_registry_compat_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


ACTIVE_PROFILE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LOCAL_PROFILE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
INACTIVE_SSH_PROFILE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"
WORKSPACE_C = "33333333-3333-4333-8333-333333333333"


def _registry(root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile_id": ACTIVE_PROFILE,
        "profiles": [
            {
                "profile_id": ACTIVE_PROFILE,
                "label": "Company active",
                "kind": "ssh",
                "enabled": True,
                "live_updates": False,
                "ssh_host_alias": "work-linux",
                "remote_app_dir": "/srv/workstack/app",
                "remote_data_dir": "/srv/workstack/active",
                "expected_workspace_id": WORKSPACE_A,
                "preferred_forward_port": 18765,
                "remote_port": 8877,
            },
            {
                "profile_id": LOCAL_PROFILE,
                "label": "Personal local",
                "kind": "local",
                "enabled": False,
                "live_updates": True,
                "data_dir": str((root / "local-ssot").resolve()),
                "expected_workspace_id": WORKSPACE_B,
            },
            {
                "profile_id": INACTIVE_SSH_PROFILE,
                "label": "Archive remote",
                "kind": "ssh",
                "enabled": False,
                "live_updates": True,
                "ssh_host_alias": "archive-linux",
                "remote_app_dir": "/opt/workstack/app",
                "remote_data_dir": "/opt/workstack/archive",
                "expected_workspace_id": WORKSPACE_C,
                "preferred_forward_port": 28765,
                "remote_port": 8765,
            },
        ],
    }


class ConnectionRegistryCompatibilityTest(unittest.TestCase):
    def test_active_profile_exports_generated_legacy_mirror_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = MODULE.save_connection_registry(root, _registry(root))
            digest = MODULE.connection_registry_digest(registry)

            exported = MODULE.export_active_legacy_mirror(
                root, expected_registry_digest=digest
            )

            mirror = json.loads((root / MODULE.LEGACY_MIRROR_FILE).read_text("utf-8"))
            self.assertEqual(
                mirror,
                {
                    "storage_mode": "ssh-remote",
                    "ssh_host_alias": "work-linux",
                    "remote_app_dir": "/srv/workstack/app",
                    "remote_data_dir": "/srv/workstack/active",
                    "local_forward_port": 18765,
                    "workspace_id": WORKSPACE_A,
                    "remote_port": 8877,
                },
            )
            self.assertEqual(exported.profile_id, ACTIVE_PROFILE)
            self.assertEqual(exported.registry_digest, digest)
            receipt = json.loads(
                (root / MODULE.LEGACY_MIRROR_RECEIPT_FILE).read_text("utf-8")
            )
            self.assertEqual(receipt["authority"], "connection-registry")
            self.assertEqual(receipt["profile_id"], ACTIVE_PROFILE)
            self.assertEqual(receipt["registry_sha256"], digest)

    def test_corrupt_or_hostile_mirror_is_never_imported_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = MODULE.save_connection_registry(root, _registry(root))
            digest = MODULE.connection_registry_digest(registry)
            mirror_path = root / MODULE.LEGACY_MIRROR_FILE
            mirror_path.write_text(
                json.dumps(
                    {
                        "storage_mode": "ssh-remote",
                        "workspace_id": WORKSPACE_C,
                        "hostile": "must-not-be-read",
                    }
                ),
                encoding="utf-8",
            )

            MODULE.export_active_legacy_mirror(root, expected_registry_digest=digest)

            loaded = MODULE.load_connection_registry(root)
            self.assertEqual(loaded, registry)
            regenerated = json.loads(mirror_path.read_text("utf-8"))
            self.assertEqual(regenerated["workspace_id"], WORKSPACE_A)
            self.assertNotIn("hostile", regenerated)

    def test_local_active_profile_exports_local_legacy_shape_with_other_profiles_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = _registry(root)
            document["active_profile_id"] = LOCAL_PROFILE
            profiles = document["profiles"]
            profiles[0]["enabled"] = False  # type: ignore[index]
            profiles[1]["enabled"] = True  # type: ignore[index]
            registry = MODULE.save_connection_registry(root, document)

            MODULE.export_active_legacy_mirror(
                root,
                expected_registry_digest=MODULE.connection_registry_digest(registry),
            )

            mirror = json.loads((root / MODULE.LEGACY_MIRROR_FILE).read_text("utf-8"))
            self.assertEqual(mirror, {"storage_mode": "local"})
            self.assertEqual(MODULE.load_connection_registry(root), registry)

    def test_confirmed_remote_rebind_uses_cas_and_preserves_all_other_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = MODULE.save_connection_registry(root, _registry(root))
            digest = MODULE.connection_registry_digest(before)

            result = MODULE.rebind_active_remote_workspace(
                root,
                expected_registry_digest=digest,
                expected_profile_id=ACTIVE_PROFILE,
                expected_previous_workspace_id=WORKSPACE_A,
                observed_workspace_id=WORKSPACE_C,
                confirmation_workspace_id=WORKSPACE_C,
            )

            after = result.registry
            self.assertEqual(after.active_profile_id, before.active_profile_id)
            self.assertEqual(after.profiles[1:], before.profiles[1:])
            self.assertEqual(after.profiles[0].label, before.profiles[0].label)
            self.assertEqual(after.profiles[0].live_updates, before.profiles[0].live_updates)
            self.assertEqual(after.profiles[0].remote_data_dir, before.profiles[0].remote_data_dir)
            self.assertEqual(after.profiles[0].expected_workspace_id, WORKSPACE_C)
            self.assertEqual(result.previous_workspace_id, WORKSPACE_A)
            self.assertEqual(result.current_workspace_id, WORKSPACE_C)
            self.assertNotEqual(result.registry_digest, digest)

    def test_rebind_refuses_stale_digest_without_overwriting_newer_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = MODULE.save_connection_registry(root, _registry(root))
            stale = MODULE.connection_registry_digest(original)
            document = MODULE.registry_to_document(original)
            document["profiles"][0]["label"] = "Concurrent edit"  # type: ignore[index]
            current = MODULE.save_connection_registry(root, document)

            with self.assertRaisesRegex(RuntimeError, "stale registry digest"):
                MODULE.rebind_active_remote_workspace(
                    root,
                    expected_registry_digest=stale,
                    expected_profile_id=ACTIVE_PROFILE,
                    expected_previous_workspace_id=WORKSPACE_A,
                    observed_workspace_id=WORKSPACE_C,
                    confirmation_workspace_id=WORKSPACE_C,
                )

            self.assertEqual(MODULE.load_connection_registry(root), current)

    def test_rebind_requires_exact_explicit_confirmation_and_active_remote_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = MODULE.save_connection_registry(root, _registry(root))
            digest = MODULE.connection_registry_digest(registry)
            cases = (
                {"confirmation_workspace_id": WORKSPACE_B},
                {"expected_profile_id": INACTIVE_SSH_PROFILE},
                {"expected_previous_workspace_id": WORKSPACE_B},
            )
            for override in cases:
                with self.subTest(override=override):
                    arguments = {
                        "expected_registry_digest": digest,
                        "expected_profile_id": ACTIVE_PROFILE,
                        "expected_previous_workspace_id": WORKSPACE_A,
                        "observed_workspace_id": WORKSPACE_C,
                        "confirmation_workspace_id": WORKSPACE_C,
                    }
                    arguments.update(override)
                    with self.assertRaises(RuntimeError):
                        MODULE.rebind_active_remote_workspace(root, **arguments)
            self.assertEqual(MODULE.load_connection_registry(root), registry)

    def test_export_and_rebind_reject_malformed_or_stale_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = MODULE.save_connection_registry(root, _registry(root))
            current = MODULE.connection_registry_digest(registry)
            for digest in ("", "sha256:" + "0" * 64):
                with self.subTest(digest=digest):
                    with self.assertRaisesRegex(RuntimeError, "registry digest"):
                        MODULE.export_active_legacy_mirror(
                            root, expected_registry_digest=digest
                        )
            self.assertEqual(MODULE.connection_registry_digest(registry), current)
            self.assertFalse((root / MODULE.LEGACY_MIRROR_FILE).exists())


if __name__ == "__main__":
    unittest.main()
