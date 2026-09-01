from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workstack.storage.runtime import (
    RuntimeLayoutError,
    authority_key,
    resolve_runtime_authority,
)


WORKSPACE_UID = "11111111-1111-1111-1111-111111111111"


class RuntimeAuthorityTests(unittest.TestCase):
    def test_identity_binds_workspace_and_exact_authority_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "authority-a"
            second = root / "authority-b"
            self.assertEqual(authority_key(first, WORKSPACE_UID), authority_key(first, WORKSPACE_UID))
            self.assertNotEqual(authority_key(first, WORKSPACE_UID), authority_key(second, WORKSPACE_UID))

    def test_runtime_paths_are_local_and_content_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = resolve_runtime_authority(root / "ssot", root / "runtime", WORKSPACE_UID)
            self.assertTrue(result.runtime_root.is_relative_to((root / "runtime").resolve()))
            self.assertEqual(result.journal_path.name, "write-journal.v2.json")
            self.assertEqual(result.idempotency_path.name, "idempotency-ledger.v1.json")

    def test_runtime_and_authority_may_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for runtime in (root / "ssot", root / "ssot" / ".runtime"):
                with self.subTest(runtime=runtime), self.assertRaisesRegex(
                    RuntimeLayoutError, "RUNTIME_AUTHORITY_OVERLAP"
                ):
                    resolve_runtime_authority(root / "ssot", runtime, WORKSPACE_UID)

    def test_invalid_workspace_uid_is_content_free(self) -> None:
        with self.assertRaisesRegex(RuntimeLayoutError, "WORKSPACE_UID_INVALID"):
            authority_key("private-authority-name", "private-invalid-uid")


if __name__ == "__main__":
    unittest.main()
