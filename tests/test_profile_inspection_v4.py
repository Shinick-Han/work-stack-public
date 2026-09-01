from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_profile_inspection import (
    MODULE,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    create_store,
    local_candidate,
    ssh_candidate,
    tree_hashes,
)
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.authority_inspection import inspect_inactive_v4_authority
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.migration import execute_v3_migration
from workstack.storage.reader import read_v4


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T12:00:00Z"


class ProfileInspectionV4Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        shutil.copytree(FIXTURE, source)
        execution = execute_v3_migration(source, candidate_created_at=CREATED_AT)
        self.v4 = execution.preview.paths.candidate_root

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v4_remains_disabled_by_default_and_inspection_is_read_only(self) -> None:
        before = tree_hashes(self.root)

        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(local_candidate(self.v4))

        self.assertEqual(raised.exception.code, "partial_store")
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(
                local_candidate(self.v4), enable_format_neutral=True
            )
        self.assertEqual(raised.exception.code, "v4_inspection_unavailable")
        self.assertEqual(tree_hashes(self.root), before)

    def test_v4_opt_in_reports_exact_manifest_and_inactive_capabilities(self) -> None:
        before = tree_hashes(self.root)
        expected_digest = build_v4_manifest(read_v4(self.v4)).digest

        result = MODULE.inspect_profile(
            local_candidate(self.v4),
            enable_format_neutral=True,
            format_neutral_local_inspector=inspect_inactive_v4_authority,
        )
        legacy_document = MODULE.profile_test_result_to_document(result)
        detailed = MODULE.profile_test_result_to_document(
            result, include_authority=True
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.actual_workspace_id, WORKSPACE_ID)
        self.assertEqual(result.authority.storage_format, "v4")
        self.assertEqual(result.authority.schema_version, 4)
        self.assertEqual(result.authority.authority_manifest_digest, expected_digest)
        self.assertEqual(
            detailed["capabilities"],
            {"read": True, "write": False, "migrate": False, "projection": True},
        )
        self.assertNotIn("storage_format", legacy_document)
        self.assertEqual(tree_hashes(self.root), before)

    def test_v4_identity_mismatch_and_invalid_or_mixed_format_fail_closed(self) -> None:
        mismatch = MODULE.inspect_profile(
            local_candidate(self.v4, OTHER_WORKSPACE_ID),
            enable_format_neutral=True,
            format_neutral_local_inspector=inspect_inactive_v4_authority,
        )
        self.assertEqual(mismatch.status, "identity_mismatch")

        mixed = self.root / "mixed"
        shutil.copytree(self.v4, mixed)
        (mixed / "backlog.json").write_bytes((FIXTURE / "backlog.json").read_bytes())
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(
                local_candidate(mixed),
                enable_format_neutral=True,
                format_neutral_local_inspector=inspect_inactive_v4_authority,
            )
        self.assertEqual(raised.exception.code, "mixed_store")

        future = self.root / "future"
        shutil.copytree(self.v4, future)
        store = read_v4(future).store
        value = dict(store)
        value["schema_version"] = 5
        (future / "store.json").write_bytes(canonical_json_bytes(value))
        with self.assertRaises(MODULE.ProfileInspectionError) as raised:
            MODULE.inspect_profile(
                local_candidate(future),
                enable_format_neutral=True,
                format_neutral_local_inspector=inspect_inactive_v4_authority,
            )
        self.assertEqual(raised.exception.code, "invalid_store")

    def test_v3_opt_in_reports_stable_exact_byte_roster_digest(self) -> None:
        data = self.root / "v3"
        workspace_id = create_store(data, self.root / "runtime")
        before = tree_hashes(self.root)

        first = MODULE.inspect_profile(
            local_candidate(data), enable_format_neutral=True
        )
        second = MODULE.inspect_profile(
            local_candidate(data), enable_format_neutral=True
        )

        self.assertEqual(first.actual_workspace_id, workspace_id)
        self.assertEqual(first.authority, second.authority)
        self.assertEqual(first.authority.storage_format, "v3")
        self.assertEqual(first.authority.schema_version, 3)
        self.assertRegex(first.authority.authority_manifest_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            MODULE.profile_test_result_to_document(first, include_authority=True)["capabilities"],
            {"read": True, "write": True, "migrate": True, "projection": True},
        )
        self.assertEqual(tree_hashes(self.root), before)

    def test_ssh_authority_metadata_is_retained_only_with_explicit_opt_in(self) -> None:
        inspection = MODULE.AuthorityInspection(
            "v4",
            4,
            "sha256:" + "a" * 64,
            MODULE.AuthorityCapabilities(True, False, False, True),
        )
        tester = mock.Mock(
            return_value=MODULE.SshProfileMetadata(
                WORKSPACE_ID, "1.0.6", 1, inspection
            )
        )

        legacy = MODULE.inspect_profile(ssh_candidate(), ssh_profile_tester=tester)
        detailed = MODULE.inspect_profile(
            ssh_candidate(),
            ssh_profile_tester=tester,
            enable_format_neutral=True,
        )

        self.assertIsNone(legacy.authority)
        self.assertEqual(detailed.authority, inspection)
        document = MODULE.profile_test_result_to_document(
            detailed, include_authority=True
        )
        self.assertEqual(document["storage_format"], "v4")
        self.assertEqual(document["authority_manifest_digest"], inspection.authority_manifest_digest)


if __name__ == "__main__":
    unittest.main()
