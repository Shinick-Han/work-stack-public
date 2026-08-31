from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workstack.service import DomainError, WorkStack
from workstack.store import Store


class TaskRelationshipIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store_bytes(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.root.glob("*.json")}

    def test_parent_cycle_is_rejected_without_persistence(self) -> None:
        root = self.stack.add_task("Root")
        child = self.stack.add_task("Child", parent_id=root["id"])
        grandchild = self.stack.add_task("Grandchild", parent_id=child["id"])
        before = self._store_bytes()

        with self.assertRaisesRegex(DomainError, "parent relationship would create a cycle"):
            self.stack.patch_task(
                root["id"], {"parent_id": grandchild["id"], "revision": root["revision"]}
            )

        self.assertEqual(self._store_bytes(), before)
        self.assertIsNone(self.stack.get_task(root["id"])["parent_id"])

    def test_dependency_cycle_is_rejected_but_acyclic_rewire_succeeds(self) -> None:
        first = self.stack.add_task("First")
        second = self.stack.add_task("Second", dependencies=[first["id"]])
        third = self.stack.add_task("Third", dependencies=[second["id"]])
        before = self._store_bytes()

        with self.assertRaisesRegex(DomainError, "dependency relationship would create a cycle"):
            self.stack.patch_task(
                first["id"], {"dependencies": [third["id"]], "revision": first["revision"]}
            )

        self.assertEqual(self._store_bytes(), before)
        changed = self.stack.patch_task(
            third["id"], {"dependencies": [first["id"]], "revision": third["revision"]}
        )
        self.assertEqual(changed["dependencies"], [first["id"]])
        self.assertEqual(changed["revision"], 1)


if __name__ == "__main__":
    unittest.main()
