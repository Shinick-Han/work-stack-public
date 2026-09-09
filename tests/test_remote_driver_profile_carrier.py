"""The remote knowledge-driver registry path, carried end to end.

One optional field -- ``knowledge_drivers_config`` -- travels from the operator's
SSH draft or registry profile to the remote serve command. This module executes
the real codecs, the real migration and the real generated mirror; the only
mocked thing is the command seam, whose new keyword is owned by another lane.

Nothing here reads a knowledge-driver file. The path is an absolute POSIX string
validated by the released remote path rule, and every value used below is one
that cannot exist on the Windows host running these tests -- which is the point:
admission never looks at a filesystem and never translates a path.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

# Imported under their real module names on purpose: the compatibility module
# resolves ``connection_registry`` itself, and a second copy loaded under a
# private name would give it a different ``ConnectionRegistry`` class than the
# one these tests construct.
import connection_registry as REGISTRY  # noqa: E402
import connection_registry_compat as COMPAT  # noqa: E402
import ssot_connection as SSOT  # noqa: E402

PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
REMOTE_PYTHON = "/srv/workstack/venv/bin/python"
DRIVERS_CONFIG = "/srv/workstack/etc/knowledge-drivers.json"
OTHER_DRIVERS_CONFIG = "/srv/workstack/etc/other-drivers.json"
SESSION_TOKEN = "r5pending-token-not-enforced-01"

# The exact canonical bytes a record without the new field produced before it
# existed, recomputed from the base commit's own modules. A record that never
# carried a driver registry must keep these bytes and this schema version.
LEGACY_REGISTRY_BYTES = (
    '{"schema_version":1,"active_profile_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
    '"profiles":[{"profile_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
    '"label":"Company engineering","enabled":true,"live_updates":true,'
    '"expected_workspace_id":"11111111-1111-4111-8111-111111111111","kind":"ssh",'
    '"ssh_host_alias":"work-linux","remote_app_dir":"/srv/workstack/app",'
    '"remote_data_dir":"/srv/workstack/engineering","preferred_forward_port":18765,'
    '"remote_port":8765,"remote_python":"/srv/workstack/venv/bin/python"}]}\n'
)
LEGACY_DRAFT_BYTES = (
    '{"storage_mode":"ssh-remote","ssh_host_alias":"work-linux",'
    '"remote_app_dir":"/srv/workstack/app",'
    '"remote_data_dir":"/srv/workstack/engineering","local_forward_port":18765,'
    '"workspace_id":"11111111-1111-4111-8111-111111111111","remote_port":8765,'
    '"remote_python":"/srv/workstack/venv/bin/python"}\n'
)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n"


def ssh_profile_document(**overrides: object) -> dict[str, object]:
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
        "remote_python": REMOTE_PYTHON,
    }
    value.update(overrides)
    return value


def registry_document(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile_id": PROFILE_A,
        "profiles": [ssh_profile_document(**overrides)],
    }


def remote_draft(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/engineering",
        "local_forward_port": 18765,
        "workspace_id": WORKSPACE_A,
        "remote_port": 8765,
        "remote_python": REMOTE_PYTHON,
    }
    payload.update(overrides)
    return payload


class LegacyRecordStabilityTest(unittest.TestCase):
    """A record written before the field keeps its exact bytes and version."""

    def test_registry_without_the_field_is_byte_identical_and_unversioned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            REGISTRY.save_connection_registry(root, registry_document())
            written = (root / REGISTRY.REGISTRY_FILE).read_text(encoding="utf-8")

        self.assertEqual(written, LEGACY_REGISTRY_BYTES)
        self.assertNotIn("knowledge_drivers_config", written)
        self.assertEqual(REGISTRY.REGISTRY_SCHEMA_VERSION, 1)
        loaded = REGISTRY.registry_from_document(json.loads(written))
        self.assertIsNone(loaded.profiles[0].knowledge_drivers_config)
        # Re-encoding the parsed record reproduces the same bytes, so a
        # load/save cycle never introduces the new key or an explicit null.
        self.assertEqual(canonical(REGISTRY.registry_to_document(loaded)), written)

    def test_draft_without_the_field_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            SSOT.save_connection_draft(root, remote_draft())
            written = (root / SSOT.REMOTE_CONNECTION_FILE).read_text(encoding="utf-8")

        self.assertEqual(written, LEGACY_DRAFT_BYTES)
        self.assertNotIn("knowledge_drivers_config", written)
        profile = SSOT.connection_profile_from_draft(remote_draft())
        self.assertIsNone(profile.knowledge_drivers_config)

    def test_the_field_is_appended_so_old_positional_construction_is_unchanged(
        self,
    ) -> None:
        remote = SSOT.RemoteConnectionProfile(
            "work-linux", "/srv/workstack/app", "/srv/workstack/engineering",
            18765, WORKSPACE_A, 8765, REMOTE_PYTHON,
        )
        ssh = REGISTRY.SshConnectionProfile(
            PROFILE_A, "Company engineering", "work-linux", "/srv/workstack/app",
            "/srv/workstack/engineering", WORKSPACE_A, 18765, 8765, True, True,
            "ssh", REMOTE_PYTHON,
        )
        self.assertEqual(remote.remote_python, REMOTE_PYTHON)
        self.assertIsNone(remote.knowledge_drivers_config)
        self.assertEqual(ssh.remote_python, REMOTE_PYTHON)
        self.assertIsNone(ssh.knowledge_drivers_config)


class CarriedPathSurvivesEveryRoundTripTest(unittest.TestCase):
    """One admitted path, through every persistence seam this lane owns."""

    def test_draft_registry_migration_and_generated_mirror_all_carry_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            # 1. The SSOT draft: normalize, save, load, and build the profile.
            normalized = SSOT.save_connection_draft(
                root, remote_draft(knowledge_drivers_config=DRIVERS_CONFIG)
            )
            self.assertEqual(normalized["knowledge_drivers_config"], DRIVERS_CONFIG)
            reloaded = SSOT.load_connection_draft(root)
            self.assertEqual(reloaded, normalized)
            self.assertEqual(
                SSOT.load_remote_connection_profile(root).knowledge_drivers_config,
                DRIVERS_CONFIG,
            )
            # Appended last, so the older keys keep their exact order.
            written = (root / SSOT.REMOTE_CONNECTION_FILE).read_text(encoding="utf-8")
            self.assertEqual(
                written,
                LEGACY_DRAFT_BYTES[:-2]
                + ',"knowledge_drivers_config":"%s"}\n' % DRIVERS_CONFIG,
            )

            # 2. The legacy draft migrates into a registry profile.
            migrated = REGISTRY.migrate_singleton_draft(
                reloaded, profile_id=PROFILE_A, label="Company engineering"
            )
            self.assertEqual(
                migrated.profiles[0].knowledge_drivers_config, DRIVERS_CONFIG
            )

            # 3. The registry saves and loads it, and its document carries it.
            saved = REGISTRY.save_connection_registry(root, migrated)
            self.assertEqual(
                saved.profiles[0].knowledge_drivers_config, DRIVERS_CONFIG
            )
            loaded = REGISTRY.load_connection_registry(root)
            self.assertEqual(
                loaded.profiles[0].knowledge_drivers_config, DRIVERS_CONFIG
            )
            document = REGISTRY.registry_to_document(loaded)
            self.assertEqual(
                document["profiles"][0]["knowledge_drivers_config"], DRIVERS_CONFIG
            )

            # 4. Back out through the legacy singleton draft.
            self.assertEqual(
                REGISTRY.singleton_draft_from_registry(loaded)[
                    "knowledge_drivers_config"
                ],
                DRIVERS_CONFIG,
            )

            # 5. The generated downgrade mirror, written from the registry.
            export = COMPAT.export_active_legacy_mirror(
                root,
                expected_registry_digest=COMPAT.connection_registry_digest(loaded),
            )
            mirror = json.loads(export.path.read_text(encoding="utf-8"))
            self.assertEqual(mirror["knowledge_drivers_config"], DRIVERS_CONFIG)

            # 6. The mirror is a valid draft again, and reaches a live profile.
            profile = SSOT.connection_profile_from_draft(mirror)
            self.assertEqual(profile.knowledge_drivers_config, DRIVERS_CONFIG)

    def test_a_profile_without_the_field_never_gains_a_null_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            migrated = REGISTRY.migrate_singleton_draft(
                remote_draft(), profile_id=PROFILE_A, label="Company engineering"
            )
            self.assertIsNone(migrated.profiles[0].knowledge_drivers_config)
            saved = REGISTRY.save_connection_registry(root, migrated)
            document = REGISTRY.registry_to_document(saved)
            self.assertNotIn("knowledge_drivers_config", document["profiles"][0])
            self.assertNotIn(
                "knowledge_drivers_config",
                REGISTRY.singleton_draft_from_registry(saved),
            )
            export = COMPAT.export_active_legacy_mirror(
                root,
                expected_registry_digest=COMPAT.connection_registry_digest(saved),
            )
            mirror_text = export.path.read_text(encoding="utf-8")
            self.assertNotIn("knowledge_drivers_config", mirror_text)


class CommandSeamTest(unittest.TestCase):
    """The keyword reaches the serve command only when a path is configured."""

    def profile(self, **overrides: object) -> object:
        return SSOT.connection_profile_from_draft(remote_draft(**overrides))

    def test_the_keyword_is_passed_only_when_the_profile_carries_a_path(self) -> None:
        recorded: list[dict[str, object]] = []

        def recorder(**kwargs: object) -> str:
            recorded.append(kwargs)
            return "recorded-serve-command"

        with mock.patch.object(SSOT, "join_serve_command", recorder):
            SSOT.build_remote_server_command(
                self.profile(knowledge_drivers_config=DRIVERS_CONFIG),
                session_token=SESSION_TOKEN,
            )
            SSOT.build_remote_server_command(self.profile(), session_token=SESSION_TOKEN)

        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[0]["knowledge_drivers_config"], DRIVERS_CONFIG)
        # Absent, not None: a profile with no configured registry produces the
        # exact call it produced before this field existed.
        self.assertNotIn("knowledge_drivers_config", recorded[1])
        self.assertEqual(
            set(recorded[0]) - set(recorded[1]), {"knowledge_drivers_config"}
        )

    def test_a_profile_without_the_path_still_builds_the_released_command(self) -> None:
        """Unmocked: the real contract still produces today's serve command."""

        command = SSOT.build_remote_server_command(
            self.profile(), session_token=SESSION_TOKEN
        )
        self.assertIn("/srv/workstack/app", command)
        self.assertIn(SESSION_TOKEN, command)
        self.assertNotIn("knowledge-drivers", command)


class RefusalTest(unittest.TestCase):
    """Every refusal family, on both the draft and the registry surface."""

    INVALID = {
        "explicit null": None,
        "empty string": "",
        "non-string integer": 7,
        "non-string boolean": True,
        "relative path": "etc/knowledge-drivers.json",
        "bare name": "knowledge-drivers.json",
        "windows drive path": "C:\\ProgramData\\workstack\\drivers.json",
        "windows unc path": "\\\\server\\share\\drivers.json",
        "parent traversal": "/srv/workstack/../../etc/drivers.json",
        "trailing slash": "/srv/workstack/etc/",
        "filesystem root": "/",
        "control character": "/srv/workstack/etc/dr\nivers.json",
        "space in path": "/srv/work stack/drivers.json",
    }

    def test_the_draft_refuses_every_invalid_configured_path(self) -> None:
        for name, value in self.INVALID.items():
            with self.subTest(case=name):
                with self.assertRaises(RuntimeError):
                    SSOT.validate_connection_draft(
                        remote_draft(knowledge_drivers_config=value)
                    )

    def test_the_registry_refuses_every_invalid_configured_path(self) -> None:
        for name, value in self.INVALID.items():
            with self.subTest(case=name):
                with self.assertRaises(RuntimeError):
                    REGISTRY.validate_connection_registry(
                        registry_document(knowledge_drivers_config=value)
                    )

    def test_migration_refuses_an_invalid_configured_path(self) -> None:
        with self.assertRaises(RuntimeError):
            REGISTRY.migrate_singleton_draft(
                dict(remote_draft(), knowledge_drivers_config="relative/path.json"),
                profile_id=PROFILE_A,
            )

    def test_only_ssh_profiles_and_drafts_may_carry_it(self) -> None:
        with self.assertRaises(RuntimeError) as draft_refusal:
            SSOT.validate_connection_draft(
                {"storage_mode": "local", "knowledge_drivers_config": DRIVERS_CONFIG}
            )
        self.assertIn("unsupported fields", str(draft_refusal.exception))

        local = {
            "profile_id": PROFILE_A,
            "label": "Local work",
            "kind": "local",
            "enabled": True,
            "live_updates": True,
            "data_dir": str((ROOT / ".test-local-ssot").resolve()),
            "expected_workspace_id": WORKSPACE_A,
            "knowledge_drivers_config": DRIVERS_CONFIG,
        }
        with self.assertRaises(RuntimeError) as profile_refusal:
            REGISTRY.validate_connection_registry(
                {
                    "schema_version": 1,
                    "active_profile_id": PROFILE_A,
                    "profiles": [local],
                }
            )
        self.assertIn("unsupported fields", str(profile_refusal.exception))

        with self.assertRaises(RuntimeError):
            REGISTRY.migrate_singleton_draft(
                {"storage_mode": "local", "knowledge_drivers_config": DRIVERS_CONFIG},
                local_data_dir=str((ROOT / ".test-local-ssot").resolve()),
                local_workspace_id=WORKSPACE_A,
            )

    def test_an_unknown_neighbouring_key_is_still_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            SSOT.validate_connection_draft(
                remote_draft(
                    knowledge_drivers_config=DRIVERS_CONFIG,
                    knowledge_drivers_configs=DRIVERS_CONFIG,
                )
            )
        with self.assertRaises(RuntimeError):
            REGISTRY.validate_connection_registry(
                registry_document(
                    knowledge_drivers_config=DRIVERS_CONFIG,
                    knowledge_driver_config=DRIVERS_CONFIG,
                )
            )


class IdentityAndIsolationTest(unittest.TestCase):
    """What a changed path does change, and what it deliberately does not."""

    def test_a_changed_path_changes_the_profile_and_the_canonical_document(
        self,
    ) -> None:
        first = SSOT.connection_profile_from_draft(
            remote_draft(knowledge_drivers_config=DRIVERS_CONFIG)
        )
        second = SSOT.connection_profile_from_draft(
            remote_draft(knowledge_drivers_config=OTHER_DRIVERS_CONFIG)
        )
        self.assertNotEqual(first, second)

        one = REGISTRY.registry_to_document(
            REGISTRY.registry_from_document(
                registry_document(knowledge_drivers_config=DRIVERS_CONFIG)
            )
        )
        other = REGISTRY.registry_to_document(
            REGISTRY.registry_from_document(
                registry_document(knowledge_drivers_config=OTHER_DRIVERS_CONFIG)
            )
        )
        self.assertNotEqual(canonical(one), canonical(other))
        self.assertNotEqual(
            COMPAT.connection_registry_digest(one),
            COMPAT.connection_registry_digest(other),
        )

    def test_the_configured_path_is_not_part_of_the_live_session_identity(
        self,
    ) -> None:
        """A restart applies a changed path; it is not a different live server.

        ``owned_execution_identity`` answers "is this the server this session
        started?" -- alias, install root, data root and interpreter. The driver
        registry path is configuration the remote reads at startup, so probing
        the same running owner must still recognise its own session token.
        """

        active = SSOT.connection_profile_from_draft(
            remote_draft(knowledge_drivers_config=DRIVERS_CONFIG)
        )
        changed = SSOT.connection_profile_from_draft(
            remote_draft(knowledge_drivers_config=OTHER_DRIVERS_CONFIG)
        )
        self.assertEqual(
            SSOT.owned_execution_identity(active),
            SSOT.owned_execution_identity(changed),
        )
        self.assertEqual(
            SSOT.session_token_for_self_probe(active, changed, SESSION_TOKEN),
            SESSION_TOKEN,
        )
        # The existing authorization is untouched: a different alias, install
        # root, data root or interpreter still carries no token.
        for field, value in (
            ("ssh_host_alias", "other-linux"),
            ("remote_app_dir", "/opt/workstack/app"),
            ("remote_data_dir", "/opt/workstack/ssot"),
            ("remote_python", "/usr/bin/python3"),
        ):
            with self.subTest(field=field):
                foreign = SSOT.connection_profile_from_draft(
                    remote_draft(
                        knowledge_drivers_config=DRIVERS_CONFIG, **{field: value}
                    )
                )
                self.assertIsNone(
                    SSOT.session_token_for_self_probe(active, foreign, SESSION_TOKEN)
                )

    def test_admission_reads_no_file_and_translates_no_windows_path(self) -> None:
        """The path is admitted as a remote string, never as a local lookup."""

        absent = "/srv/workstack/etc/definitely-absent-on-this-host.json"
        self.assertFalse(Path(absent).exists())
        normalized = SSOT.validate_connection_draft(
            remote_draft(knowledge_drivers_config=absent)
        )
        # Returned verbatim: no drive letter, no separator translation, and no
        # existence check stood between the operator's value and the profile.
        self.assertEqual(normalized["knowledge_drivers_config"], absent)
        self.assertNotIn("\\", str(normalized["knowledge_drivers_config"]))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            REGISTRY.save_connection_registry(
                root, registry_document(knowledge_drivers_config=absent)
            )
            # Only the registry file itself was written: admitting a driver
            # registry path creates and consults nothing else.
            self.assertEqual(
                sorted(item.name for item in root.iterdir()),
                [REGISTRY.REGISTRY_FILE],
            )

    def test_the_carrier_modules_import_no_core_adapter(self) -> None:
        for name in (
            "ssot_connection",
            "connection_registry",
            "connection_registry_compat",
        ):
            with self.subTest(module=name):
                source = (SHELL / f"{name}.py").read_text(encoding="utf-8")
                self.assertNotIn("import workstack", source)
                self.assertNotIn("from workstack", source)


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
