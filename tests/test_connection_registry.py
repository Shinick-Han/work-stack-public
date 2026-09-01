from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "connection_registry.py"
SPEC = importlib.util.spec_from_file_location("connection_registry_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"


def ssh_profile(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "profile_id": PROFILE_A,
        "label": "Company engineering",
        "kind": "ssh",
        "enabled": True,
        "live_updates": True,
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/engineering",
        "expected_workspace_id": WORKSPACE_A,
        "preferred_forward_port": 18765,
        "remote_port": 8765,
    }
    value.update(overrides)
    return value


def local_profile(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "profile_id": PROFILE_B,
        "label": "Local work",
        "kind": "local",
        "enabled": True,
        "live_updates": False,
        "data_dir": str((ROOT / ".test-local-ssot").resolve()),
        "expected_workspace_id": WORKSPACE_B,
    }
    value.update(overrides)
    return value


def registry(*profiles: dict[str, object], active: str | None = PROFILE_A) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile_id": active,
        "profiles": list(profiles),
    }


class ConnectionRegistryTest(unittest.TestCase):
    def test_strict_versioned_union_round_trips_local_and_ssh_profiles(self) -> None:
        raw = registry(ssh_profile(), local_profile())
        parsed = MODULE.registry_from_document(raw)

        self.assertEqual(MODULE.registry_to_document(parsed), raw)
        self.assertIsInstance(parsed.profiles[0], MODULE.SshConnectionProfile)
        self.assertIsInstance(parsed.profiles[1], MODULE.LocalConnectionProfile)
        self.assertEqual(parsed.active_profile_id, PROFILE_A)

    def test_rejects_unknown_fields_at_every_schema_level(self) -> None:
        cases = (
            {**registry(ssh_profile()), "surprise": True},
            registry(ssh_profile(surprise=True)),
            registry(local_profile(surprise=True), active=PROFILE_B),
            registry(ssh_profile(kind="local")),
            registry(local_profile(kind="ssh"), active=PROFILE_B),
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "unsupported fields|is missing"
            ):
                MODULE.registry_from_document(value)

    def test_rejects_noncanonical_or_nil_ids_and_duplicate_profile_ids(self) -> None:
        invalid = (
            registry(ssh_profile(profile_id=PROFILE_A.upper())),
            registry(ssh_profile(expected_workspace_id="00000000-0000-0000-0000-000000000000")),
            registry(ssh_profile(), ssh_profile(label="Other endpoint")),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                MODULE.registry_from_document(value)

    def test_duplicate_workspace_identity_is_explicitly_reported(self) -> None:
        second = ssh_profile(
            profile_id=PROFILE_B,
            label="Backup-looking endpoint",
            ssh_host_alias="work-linux-alt",
            enabled=False,
        )
        parsed = MODULE.registry_from_document(registry(ssh_profile(), second))

        self.assertEqual(
            parsed.duplicate_authorities,
            (
                MODULE.DuplicateWorkspaceAuthority(
                    WORKSPACE_A, (PROFILE_A, PROFILE_B)
                ),
            ),
        )
        self.assertEqual(len(parsed.profiles), 2)

    def test_rejects_ambiguous_enabled_authority_and_disabled_active_profile(self) -> None:
        duplicate = ssh_profile(
            profile_id=PROFILE_B,
            label="Ambiguous endpoint",
            ssh_host_alias="work-linux-alt",
        )
        with self.assertRaisesRegex(RuntimeError, "at most one enabled profile"):
            MODULE.registry_from_document(registry(ssh_profile(), duplicate))

        with self.assertRaisesRegex(RuntimeError, "enabled profile"):
            MODULE.registry_from_document(
                registry(ssh_profile(enabled=False), active=PROFILE_A)
            )

    def test_distinct_workspace_identities_are_not_reported_as_duplicates(self) -> None:
        parsed = MODULE.registry_from_document(
            registry(ssh_profile(), local_profile())
        )
        self.assertEqual(parsed.duplicate_authorities, ())

    def test_singleton_ssh_migration_is_lossless_after_legacy_normalization(self) -> None:
        legacy = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app/",
            "remote_data_dir": "/srv/workstack/engineering/",
            "local_forward_port": 18765,
            "workspace_id": WORKSPACE_A,
        }
        migrated = MODULE.migrate_singleton_draft(
            legacy, profile_id=PROFILE_A, label="Company engineering"
        )

        self.assertEqual(
            MODULE.singleton_draft_from_registry(migrated),
            {
                **legacy,
                "remote_app_dir": "/srv/workstack/app",
                "remote_data_dir": "/srv/workstack/engineering",
                "remote_port": 8765,
            },
        )
        profile = migrated.profiles[0]
        self.assertEqual(profile.expected_workspace_id, WORKSPACE_A)
        self.assertEqual(profile.preferred_forward_port, 18765)

    def test_singleton_local_migration_requires_explicit_existing_authority(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires local_data_dir"):
            MODULE.migrate_singleton_draft(
                {"storage_mode": "local"}, profile_id=PROFILE_B
            )

        migrated = MODULE.migrate_singleton_draft(
            {"storage_mode": "local"},
            profile_id=PROFILE_B,
            local_data_dir=str((ROOT / ".test-local-ssot").resolve()),
            local_workspace_id=WORKSPACE_B,
        )
        self.assertEqual(
            MODULE.singleton_draft_from_registry(migrated),
            {"storage_mode": "local"},
        )
        self.assertEqual(migrated.profiles[0].expected_workspace_id, WORKSPACE_B)

    def test_singleton_migration_does_not_replace_an_explicit_invalid_profile_id(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "profile_id"):
            MODULE.migrate_singleton_draft(
                {"storage_mode": "local"},
                profile_id="",
                local_data_dir=str((ROOT / ".test-local-ssot").resolve()),
                local_workspace_id=WORKSPACE_B,
            )

    def test_singleton_migration_does_not_replace_an_explicit_blank_label(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "label"):
            MODULE.migrate_singleton_draft(
                {"storage_mode": "local"},
                profile_id=PROFILE_B,
                label="",
                local_data_dir=str((ROOT / ".test-local-ssot").resolve()),
                local_workspace_id=WORKSPACE_B,
            )

    def test_save_is_canonical_atomic_durable_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = MODULE.save_connection_registry(root, registry(ssh_profile()))
            path = root / MODULE.REGISTRY_FILE
            expected = MODULE.registry_to_document(saved)

            self.assertEqual(MODULE.load_connection_registry(root), saved)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                json.dumps(expected, ensure_ascii=True, separators=(",", ":")) + "\n",
            )
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_interrupted_replace_preserves_previous_registry_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / MODULE.REGISTRY_FILE
            original = json.dumps(
                registry(ssh_profile()), ensure_ascii=True, separators=(",", ":")
            ).encode() + b"\n"
            target.write_bytes(original)
            replacement = registry(local_profile(), active=PROFILE_B)

            with mock.patch.object(MODULE.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "Could not save"):
                    MODULE.save_connection_registry(root, replacement)

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(
                [path.name for path in root.iterdir()], [MODULE.REGISTRY_FILE]
            )

    def test_loader_rejects_oversized_document_before_json_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / MODULE.REGISTRY_FILE).write_bytes(
                b"{" + b" " * MODULE.MAX_REGISTRY_BYTES + b"}"
            )
            with self.assertRaisesRegex(RuntimeError, "too large"):
                MODULE.load_connection_registry(root)

    def test_profile_values_reject_wrong_boolean_port_path_and_blank_label(self) -> None:
        invalid = (
            ssh_profile(enabled=1),
            ssh_profile(preferred_forward_port=True),
            ssh_profile(remote_data_dir="/srv/../private"),
            ssh_profile(ssh_host_alias="work; calc"),
            ssh_profile(ssh_host_alias="-V"),
            ssh_profile(label="   "),
            local_profile(data_dir=r"\\server\share\workstack"),
            local_profile(data_dir=r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1"),
            local_profile(data_dir="/"),
            local_profile(data_dir="C:\\"),
        )
        for profile in invalid:
            with self.subTest(profile=profile), self.assertRaises(RuntimeError):
                MODULE.registry_from_document(registry(profile))

    def test_missing_registry_is_not_confused_with_an_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(MODULE.load_connection_registry(Path(directory)))

        empty = MODULE.registry_from_document(registry(active=None))
        self.assertEqual(empty.profiles, ())
        self.assertIsNone(empty.active_profile_id)


if __name__ == "__main__":
    unittest.main()
