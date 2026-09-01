from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
MODULE_PATH = SHELL / "connection_registry_startup.py"
SPEC = importlib.util.spec_from_file_location(
    "connection_registry_startup_test", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"
PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _write_workspace(data_dir: Path, workspace_id: str = WORKSPACE_A) -> bytes:
    data_dir.mkdir(parents=True)
    payload = json.dumps(
        {"id": workspace_id, "name": "Do not mutate me"}, separators=(",", ":")
    ).encode("utf-8")
    (data_dir / "workspace.json").write_bytes(payload)
    for name in MODULE.MINIMUM_LOCAL_STORE_FILES - {"workspace.json"}:
        (data_dir / name).write_text("{}", encoding="utf-8")
    return payload


def _remote_legacy(workspace_id: str = WORKSPACE_A) -> dict[str, object]:
    return {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "local_forward_port": 18765,
        "workspace_id": workspace_id,
        "remote_port": 8765,
    }


def _local_registry(root: Path, *, enabled: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile_id": PROFILE_A,
        "profiles": [
            {
                "profile_id": PROFILE_A,
                "label": "Local",
                "kind": "local",
                "enabled": enabled,
                "live_updates": True,
                "data_dir": str((root / "ssot").resolve()),
                "expected_workspace_id": WORKSPACE_A,
            }
        ],
    }


class ConnectionRegistryMigrationTest(unittest.TestCase):
    def test_local_absent_singleton_migrates_same_authority_without_store_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            original = _write_workspace(data_dir)

            migrated = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(data_dir.resolve()),
            )

            profile = migrated.profiles[0]
            self.assertIsInstance(profile, MODULE.LocalConnectionProfile)
            self.assertEqual(profile.label, "ssot")
            self.assertEqual(profile.expected_workspace_id, WORKSPACE_A)
            self.assertEqual((data_dir / "workspace.json").read_bytes(), original)
            self.assertTrue((root / MODULE.LEGACY_ABSENT_MARKER).is_file())
            self.assertFalse((root / MODULE.LEGACY_BACKUP_FILE).exists())
            self.assertTrue((root / MODULE.MIGRATION_RECEIPT_FILE).is_file())

            selection = MODULE.select_active_profile_for_startup(root)
            self.assertIsInstance(selection, MODULE.LocalStartupSelection)
            self.assertEqual(selection.data_dir, data_dir.resolve())
            self.assertEqual(
                selection.backup_dir,
                root / "workspace-backups" / profile.profile_id,
            )

    def test_restart_keeps_registry_path_when_legacy_config_points_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected-ssot"
            stale_default = root / "stale-default"
            _write_workspace(selected, WORKSPACE_A)
            _write_workspace(stale_default, WORKSPACE_B)

            first = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(selected.resolve()),
            )
            restarted = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(stale_default.resolve()),
            )
            selection = MODULE.select_active_profile_for_startup(root)

            self.assertEqual(restarted, first)
            self.assertIsInstance(selection, MODULE.LocalStartupSelection)
            self.assertEqual(selection.data_dir, selected.resolve())
            self.assertEqual(selection.label, "selected-ssot")

    def test_profile_id_is_stable_for_installation_and_workspace_authority(self) -> None:
        ids: list[str] = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                data_dir = root / "ssot"
                _write_workspace(data_dir)
                registry = MODULE.ensure_connection_registry(
                    root,
                    installation_identity="stable-install",
                    local_data_dir=str(data_dir.resolve()),
                )
                ids.append(registry.profiles[0].profile_id)
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], "00000000-0000-0000-0000-000000000000")

    def test_remote_migration_requires_read_only_actual_identity_and_selects_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_payload = json.dumps(_remote_legacy(), indent=2).encode("utf-8")
            (root / MODULE.LEGACY_CONNECTION_FILE).write_bytes(legacy_payload)
            seen: list[object] = []

            def inspect(profile: object) -> str:
                seen.append(profile)
                return WORKSPACE_A

            registry = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-remote",
                remote_identity_reader=inspect,
            )

            self.assertEqual(len(seen), 1)
            self.assertIsInstance(registry.profiles[0], MODULE.SshConnectionProfile)
            self.assertEqual(
                (root / MODULE.LEGACY_BACKUP_FILE).read_bytes(), legacy_payload
            )
            receipt_text = (root / MODULE.MIGRATION_RECEIPT_FILE).read_text("utf-8")
            self.assertNotIn("work-linux", receipt_text)
            self.assertNotIn("/srv/", receipt_text)

            selection = MODULE.select_active_profile_for_startup(
                root, remote_identity_reader=inspect
            )
            self.assertIsInstance(selection, MODULE.SshStartupSelection)
            self.assertEqual(selection.ssh_host_alias, "work-linux")
            self.assertEqual(selection.remote_data_dir, "/srv/workstack/ssot")
            self.assertEqual(selection.preferred_forward_port, 18765)

    def test_remote_identity_mismatch_fails_closed_before_registry_or_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / MODULE.LEGACY_CONNECTION_FILE).write_text(
                json.dumps(_remote_legacy()), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    remote_identity_reader=lambda _profile: WORKSPACE_B,
                )
            self.assertFalse((root / MODULE.LEGACY_BACKUP_FILE).exists())
            self.assertFalse((root / MODULE.MIGRATION_INTENT_FILE).exists())
            self.assertFalse((root / MODULE.REGISTRY_FILE).exists())
            self.assertFalse((root / MODULE.MIGRATION_RECEIPT_FILE).exists())

    def test_interruption_after_backup_replays_backup_not_changed_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = json.dumps(_remote_legacy()).encode("utf-8")
            legacy_path = root / MODULE.LEGACY_CONNECTION_FILE
            legacy_path.write_bytes(original)

            def interrupt(phase: str) -> None:
                if phase == "backup-saved":
                    raise MODULE.MigrationInterrupted("power loss")

            with self.assertRaises(MODULE.MigrationInterrupted):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    remote_identity_reader=lambda _profile: WORKSPACE_A,
                    interruption_hook=interrupt,
                )
            legacy_path.write_text(json.dumps(_remote_legacy(WORKSPACE_B)), "utf-8")

            resumed = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                remote_identity_reader=lambda _profile: WORKSPACE_A,
            )
            self.assertEqual(resumed.profiles[0].expected_workspace_id, WORKSPACE_A)
            self.assertEqual((root / MODULE.LEGACY_BACKUP_FILE).read_bytes(), original)

    def test_interruption_after_registry_resumes_only_matching_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / MODULE.LEGACY_CONNECTION_FILE).write_text(
                json.dumps(_remote_legacy()), "utf-8"
            )

            def interrupt(phase: str) -> None:
                if phase == "registry-saved":
                    raise MODULE.MigrationInterrupted("power loss")

            with self.assertRaises(MODULE.MigrationInterrupted):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    remote_identity_reader=lambda _profile: WORKSPACE_A,
                    interruption_hook=interrupt,
                )
            self.assertTrue((root / MODULE.REGISTRY_FILE).is_file())
            self.assertFalse((root / MODULE.MIGRATION_RECEIPT_FILE).exists())

            resumed = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                remote_identity_reader=lambda _profile: WORKSPACE_A,
            )
            self.assertTrue((root / MODULE.MIGRATION_RECEIPT_FILE).is_file())
            self.assertEqual(resumed.profiles[0].expected_workspace_id, WORKSPACE_A)

    def test_existing_registry_without_migration_artifacts_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            _write_workspace(data_dir)
            expected = MODULE.save_connection_registry(root, _local_registry(root))
            (root / MODULE.LEGACY_CONNECTION_FILE).write_text(
                json.dumps(_remote_legacy(WORKSPACE_B)), "utf-8"
            )

            loaded = MODULE.ensure_connection_registry(
                root,
                installation_identity="ignored",
                remote_identity_reader=lambda _profile: WORKSPACE_B,
            )
            self.assertEqual(loaded, expected)
            self.assertFalse((root / MODULE.MIGRATION_RECEIPT_FILE).exists())

    def test_valid_receipt_never_reverse_imports_changed_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_path = root / MODULE.LEGACY_CONNECTION_FILE
            legacy_path.write_text(json.dumps(_remote_legacy()), "utf-8")
            migrated = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                remote_identity_reader=lambda _profile: WORKSPACE_A,
            )
            legacy_path.write_text("not json and must not be read", "utf-8")

            loaded = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                remote_identity_reader=lambda _profile: WORKSPACE_B,
            )
            self.assertEqual(loaded, migrated)

    def test_receipt_is_history_and_does_not_pin_later_valid_registry_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            _write_workspace(data_dir)
            migrated = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(data_dir.resolve()),
            )
            profile = migrated.profiles[0]
            edited = MODULE.ConnectionRegistry(
                schema_version=1,
                active_profile_id=profile.profile_id,
                profiles=(
                    MODULE.LocalConnectionProfile(
                        profile_id=profile.profile_id,
                        label="Renamed legitimately",
                        data_dir=profile.data_dir,
                        expected_workspace_id=profile.expected_workspace_id,
                        live_updates=False,
                    ),
                ),
            )
            MODULE.save_connection_registry(root, edited)

            loaded = MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(data_dir.resolve()),
            )

            self.assertEqual(loaded.profiles[0].label, "Renamed legitimately")
            self.assertFalse(loaded.profiles[0].live_updates)

    def test_receipt_still_detects_authority_intent_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            _write_workspace(data_dir)
            MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(data_dir.resolve()),
            )
            intent_path = root / MODULE.MIGRATION_INTENT_FILE
            intent = json.loads(intent_path.read_text("utf-8"))
            intent["workspace_id"] = WORKSPACE_B
            intent_path.write_text(json.dumps(intent), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "intent digest"):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    local_data_dir=str(data_dir.resolve()),
                )

    def test_live_lock_contention_waits_finitely_and_non_owner_cannot_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            _write_workspace(data_dir)
            lock = root / MODULE.MIGRATION_LOCK_FILE
            with MODULE._migration_lock(root, 0, 0.01):
                started = time.monotonic()
                with self.assertRaisesRegex(
                    RuntimeError, "migration is already in progress"
                ):
                    MODULE.ensure_connection_registry(
                        root,
                        installation_identity="install-A",
                        local_data_dir=str(data_dir.resolve()),
                        lock_timeout=0.05,
                        lock_poll_interval=0.01,
                    )
                self.assertGreaterEqual(time.monotonic() - started, 0.04)
                with self.assertRaisesRegex(
                    RuntimeError, "migration is already in progress"
                ):
                    with MODULE._migration_lock(root, 0, 0.01):
                        self.fail("A non-owner acquired the live migration lock")
                self.assertTrue(lock.is_file())
                self.assertFalse((root / MODULE.REGISTRY_FILE).exists())

            MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(data_dir.resolve()),
                lock_timeout=0.1,
                lock_poll_interval=0.01,
            )
            self.assertTrue(lock.is_file())

    def test_stale_lock_file_is_not_deleted_and_does_not_mean_live_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            _write_workspace(data_dir)
            lock = root / MODULE.MIGRATION_LOCK_FILE
            lock.write_bytes(b"historical-lock-file")

            MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                local_data_dir=str(data_dir.resolve()),
            )

            self.assertEqual(lock.read_bytes(), b"historical-lock-file")

    def test_os_releases_migration_lock_when_owner_process_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = (
                "import sys,time;"
                f"sys.path.insert(0,{str(SHELL)!r});"
                "import connection_registry_startup as module;"
                "from pathlib import Path;"
                "lock=module._migration_lock(Path(sys.argv[1]),0,0.01);"
                "lock.__enter__();"
                "print('locked',flush=True);"
                "time.sleep(60)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                child.kill()
                child.wait(timeout=5)
                with MODULE._migration_lock(root, 0.5, 0.01):
                    self.assertTrue((root / MODULE.MIGRATION_LOCK_FILE).is_file())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    def test_atomic_new_publish_never_overwrites_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "authority.json"
            target.write_bytes(b"first-owner")

            with self.assertRaisesRegex(RuntimeError, "already exists"):
                MODULE._atomic_write_new(target, b"second-owner", "authority")

            self.assertEqual(target.read_bytes(), b"first-owner")

    def test_local_interruption_binds_original_path_and_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            _write_workspace(first, WORKSPACE_A)
            _write_workspace(second, WORKSPACE_B)

            def interrupt(phase: str) -> None:
                if phase == "intent-saved":
                    raise MODULE.MigrationInterrupted("power loss")

            with self.assertRaises(MODULE.MigrationInterrupted):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    local_data_dir=str(first.resolve()),
                    interruption_hook=interrupt,
                )

            with self.assertRaisesRegex(RuntimeError, "migration input.*does not match"):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    local_data_dir=str(second.resolve()),
                )
            self.assertFalse((root / MODULE.REGISTRY_FILE).exists())

    def test_partial_local_store_is_rejected_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "partial"
            data_dir.mkdir()
            (data_dir / "workspace.json").write_text(
                json.dumps({"id": WORKSPACE_A}), encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "required Store file"):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    local_data_dir=str(data_dir.resolve()),
                )
            self.assertFalse((root / MODULE.REGISTRY_FILE).exists())

    def test_local_identity_rejects_link_like_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "parent" / "ssot"
            _write_workspace(data_dir)
            profile = MODULE.LocalConnectionProfile(
                profile_id=PROFILE_A,
                label="Local",
                data_dir=str(data_dir.resolve()),
                expected_workspace_id=WORKSPACE_A,
            )
            original = MODULE._is_link_like

            with mock.patch.object(
                MODULE,
                "_is_link_like",
                side_effect=lambda path: path == data_dir.parent or original(path),
            ):
                with self.assertRaisesRegex(RuntimeError, "link or junction"):
                    MODULE.read_local_workspace_identity(profile)

    def test_corrupt_or_partial_receipt_blocks_without_legacy_import(self) -> None:
        receipt_cases = (b"{", json.dumps({"schema_version": 1}).encode("utf-8"))
        for payload in receipt_cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / MODULE.MIGRATION_RECEIPT_FILE).write_bytes(payload)
                    (root / MODULE.LEGACY_CONNECTION_FILE).write_text(
                        json.dumps(_remote_legacy()), "utf-8"
                    )
                    with self.assertRaisesRegex(RuntimeError, "Migration receipt"):
                        MODULE.ensure_connection_registry(
                            root,
                            installation_identity="install-A",
                            remote_identity_reader=lambda _profile: WORKSPACE_A,
                        )
                    self.assertFalse((root / MODULE.REGISTRY_FILE).exists())
                    self.assertFalse((root / MODULE.LEGACY_BACKUP_FILE).exists())


class ConnectionRegistryStartupSelectionTest(unittest.TestCase):
    def test_startup_rejects_remote_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / MODULE.LEGACY_CONNECTION_FILE).write_text(
                json.dumps(_remote_legacy()), "utf-8"
            )
            MODULE.ensure_connection_registry(
                root,
                installation_identity="install-A",
                remote_identity_reader=lambda _profile: WORKSPACE_A,
            )
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                MODULE.select_active_profile_for_startup(
                    root, remote_identity_reader=lambda _profile: WORKSPACE_B
                )

    def test_missing_disabled_and_absent_active_profiles_fail_closed(self) -> None:
        cases = (
            {
                "schema_version": 1,
                "active_profile_id": PROFILE_A,
                "profiles": [],
            },
            _local_registry(Path("C:/safe-work-stack"), enabled=False),
            {
                "schema_version": 1,
                "active_profile_id": None,
                "profiles": [],
            },
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / MODULE.REGISTRY_FILE).write_text(json.dumps(raw), "utf-8")
                    with self.assertRaises(RuntimeError):
                        MODULE.select_active_profile_for_startup(root)

    def test_root_local_and_remote_paths_are_rejected_before_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe_local = _local_registry(root)
            unsafe_local["profiles"][0]["data_dir"] = "C:\\"  # type: ignore[index]
            with self.assertRaisesRegex(RuntimeError, "root"):
                MODULE.save_connection_registry(root, unsafe_local)

            unsafe_remote = {
                **_remote_legacy(),
                "remote_data_dir": "/",
            }
            (root / MODULE.LEGACY_CONNECTION_FILE).write_text(
                json.dumps(unsafe_remote), "utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "root"):
                MODULE.ensure_connection_registry(
                    root,
                    installation_identity="install-A",
                    remote_identity_reader=lambda _profile: WORKSPACE_A,
                )

    def test_startup_selection_does_not_create_backup_or_store_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "ssot"
            _write_workspace(data_dir)
            MODULE.save_connection_registry(root, _local_registry(root))

            selection = MODULE.select_active_profile_for_startup(root)

            self.assertEqual(selection.data_dir, data_dir.resolve())
            self.assertFalse(selection.backup_dir.exists())


if __name__ == "__main__":
    unittest.main()
