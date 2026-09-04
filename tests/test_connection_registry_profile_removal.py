"""T-0001 conformance: removing a saved inactive connection profile.

The UI deletes a profile by saving a registry candidate that omits exactly one
inactive profile through the existing metadata save route. Nothing in the
backend is added for this: these tests establish, against the real
``ConnectionRegistryMutationService``, that the existing route already permits
an inactive removal, refuses an active removal, refuses a stale compare-and-set
without losing a concurrent change, and never writes outside the registry file.

No Store is constructed and no SSOT is inspected: the local profile points at a
directory holding sentinel bytes, and those bytes and that file list must be
identical after every case.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
SPEC = importlib.util.spec_from_file_location(
    "connection_registry_mutations_profile_removal_test",
    SHELL / "connection_registry_mutations.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
import connection_registry as REGISTRY  # noqa: E402

PROFILE_ACTIVE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_INACTIVE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORKSPACE_ACTIVE = "11111111-1111-4111-8111-111111111111"
WORKSPACE_INACTIVE = "22222222-2222-4222-8222-222222222222"

SENTINEL_FILES = {
    "workspace.json": b'{"id":"11111111-1111-4111-8111-111111111111"}',
    "notes.json": b'{"version":1,"notes":[]}',
}


def local_profile(data_dir: Path, **changes: object):
    values = {
        "profile_id": PROFILE_ACTIVE,
        "label": "Local authority",
        "data_dir": str(data_dir.absolute()),
        "expected_workspace_id": WORKSPACE_ACTIVE,
        "enabled": True,
        "live_updates": True,
    }
    values.update(changes)
    return REGISTRY.LocalConnectionProfile(**values)


def ssh_profile(**changes: object):
    values = {
        "profile_id": PROFILE_INACTIVE,
        "label": "Saved remote",
        "ssh_host_alias": "work-linux",
        # Remote coordinates stay plain strings: nothing here contacts SSH.
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "expected_workspace_id": WORKSPACE_INACTIVE,
        "preferred_forward_port": 18765,
        "remote_port": 8765,
        "enabled": True,
        "live_updates": True,
    }
    values.update(changes)
    return REGISTRY.SshConnectionProfile(**values)


def registry(active: str, *profiles: object):
    return REGISTRY.ConnectionRegistry(1, active, tuple(profiles))


class InactiveProfileRemovalTest(unittest.TestCase):
    """Removal is an ordinary metadata save that omits one inactive profile."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "ssot"
        self.data_dir.mkdir()
        for name, payload in SENTINEL_FILES.items():
            (self.data_dir / name).write_bytes(payload)

        self.active = local_profile(self.data_dir)
        self.inactive = ssh_profile()
        self.current = registry(PROFILE_ACTIVE, self.active, self.inactive)
        REGISTRY.save_connection_registry(self.root, self.current)
        self.digest = MODULE.registry_digest(self.current)
        self.service = MODULE.ConnectionRegistryMutationService(self.root)

    # -- helpers ---------------------------------------------------------

    def stored(self):
        stored = REGISTRY.load_connection_registry(self.root)
        assert stored is not None
        return stored

    def profile_ids(self) -> list[str]:
        return sorted(profile.profile_id for profile in self.stored().profiles)

    def assert_ssot_untouched(self) -> None:
        """The SSOT directory is never a target of a connection-entry removal."""

        self.assertEqual(
            sorted(entry.name for entry in self.data_dir.iterdir()),
            sorted(SENTINEL_FILES),
        )
        for name, payload in SENTINEL_FILES.items():
            self.assertEqual((self.data_dir / name).read_bytes(), payload)

    # -- the supported removal -------------------------------------------

    def test_removing_the_inactive_profile_succeeds_and_keeps_the_active_one(self) -> None:
        candidate = registry(PROFILE_ACTIVE, self.active)

        saved, digest = self.service.save_metadata(
            candidate, expected_registry_digest=self.digest
        )

        self.assertEqual(self.profile_ids(), [PROFILE_ACTIVE])
        self.assertEqual(self.stored().active_profile_id, PROFILE_ACTIVE)
        self.assertEqual([p.profile_id for p in saved.profiles], [PROFILE_ACTIVE])
        self.assertEqual(digest, MODULE.registry_digest(self.stored()))
        self.assertNotEqual(digest, self.digest)
        self.assert_ssot_untouched()

    def test_removal_writes_only_the_registry_file(self) -> None:
        before = {
            path: path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

        self.service.save_metadata(
            registry(PROFILE_ACTIVE, self.active), expected_registry_digest=self.digest
        )

        after = {
            path: path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }
        changed = {path for path in before if before[path] != after.get(path)}
        added = set(after) - set(before)
        removed = set(before) - set(after)
        self.assertEqual(removed, set())
        # The registry document is the only content written. The mutation lock
        # is the service's own serialization file, created beside it; nothing
        # else in the state root, and nothing at all under the SSOT directory,
        # is touched by removing a connection entry.
        self.assertEqual(
            sorted(path.name for path in changed | added),
            ["connection-registry-mutation.lock", "connection-registry.json"],
        )
        self.assertFalse(
            [path for path in changed | added if self.data_dir in path.parents],
            "a connection-entry removal must not write inside the SSOT directory",
        )
        self.assert_ssot_untouched()

    # -- the refusals ----------------------------------------------------

    def test_removing_the_active_profile_is_refused(self) -> None:
        candidate = registry(PROFILE_ACTIVE, self.inactive)

        with self.assertRaises(RuntimeError):
            self.service.save_metadata(candidate, expected_registry_digest=self.digest)

        self.assertEqual(self.profile_ids(), sorted([PROFILE_ACTIVE, PROFILE_INACTIVE]))
        self.assertEqual(self.stored().active_profile_id, PROFILE_ACTIVE)
        self.assert_ssot_untouched()

    def test_stale_compare_and_set_refuses_and_preserves_a_newer_rename(self) -> None:
        renamed = replace(self.inactive, label="Renamed by someone else")
        REGISTRY.save_connection_registry(
            self.root, registry(PROFILE_ACTIVE, self.active, renamed)
        )

        with self.assertRaises(MODULE.RegistryConflictError):
            self.service.save_metadata(
                registry(PROFILE_ACTIVE, self.active),
                expected_registry_digest=self.digest,
            )

        stored = self.stored()
        self.assertEqual(self.profile_ids(), sorted([PROFILE_ACTIVE, PROFILE_INACTIVE]))
        surviving = [p for p in stored.profiles if p.profile_id == PROFILE_INACTIVE]
        self.assertEqual(len(surviving), 1)
        self.assertEqual(surviving[0].label, "Renamed by someone else")
        self.assert_ssot_untouched()

    def test_a_replayed_removal_against_the_consumed_digest_is_refused(self) -> None:
        self.service.save_metadata(
            registry(PROFILE_ACTIVE, self.active), expected_registry_digest=self.digest
        )

        with self.assertRaises(MODULE.RegistryConflictError):
            self.service.save_metadata(
                registry(PROFILE_ACTIVE, self.active),
                expected_registry_digest=self.digest,
            )

        self.assertEqual(self.profile_ids(), [PROFILE_ACTIVE])
        self.assert_ssot_untouched()


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
