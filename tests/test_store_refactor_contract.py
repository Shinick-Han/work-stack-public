"""What the split of `workstack.store` into collaborators must not change.

The Store facade was distributed across a layout module, the manifest and
recovery record validators, and four composed mixins. Three things have to
survive that, and each is a real failure mode rather than a restatement of the
structure:

* released callers import a fixed set of names from `workstack.store`, and a
  re-export that resolves to a *copy* of the owning module's object is a silent
  divergence rather than an alias;
* the collaborators reach the store through `self`, so there must still be one
  writer lease and one reentrant transaction depth per Store, and a test that
  patches a Store method must still intercept the mixin that calls it;
* the collaborator modules must stay importable without the facade, which is
  what keeps the split acyclic.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack import (
    file_lease,
    store,
    store_document_validation,
    store_errors,
    store_layout,
    store_manifest_validation,
    store_rebind,
    store_recovery,
)
from workstack.store import Store, StoreLockedError, _FileLease

COLLABORATORS = (
    "workstack.store_layout",
    "workstack.store_manifest_validation",
    "workstack.store_recovery",
    "workstack.store_rebind",
    "workstack.store_schema_upgrade",
    "workstack.store_sync",
)

# Every name released callers reach through `workstack.store`, beside the module
# that now owns it. `Store` itself is the facade and is deliberately absent.
REEXPORTS = {
    store_layout: (
        "CAPTURE_TOKEN_NAME",
        "DEFAULTS",
        "JOURNAL_NAME",
        "LOCK_NAME",
        "MIGRATION_BACKUP_DIR",
        "SERVER_INFO_NAME",
        "STORE_MANIFEST_NAME",
        "STORE_MANIFEST_VERSION",
        "STORE_SCHEMA_VERSION",
        "SYNC_ADOPTION_RECEIPT_NAME",
        "CHANGE_NOTICE_TYPE",
        "MAX_COMMIT_EVENTS",
        "SYNC_REBIND_RECEIPT_NAME",
        "_default_for",
        "_serialized_json_bytes",
        "_store_meta_default",
        "_utc_stamp",
        "_workspace_default",
    ),
    store_manifest_validation: (
        "_task_semantics",
        "_validate_recovery_timestamp",
        "_validate_store_manifest_files",
        "_validate_store_manifest_header",
        "_validate_store_manifest_task",
        "_validate_store_manifest_tasks",
    ),
    store_recovery: ("_recovery_writes", "_validate_recovery_write"),
    store_rebind: ("_validated_rebind_artifact_name", "_validated_rebind_file_records"),
    store_errors: (
        "StoreAdoptionConflictError",
        "StoreCorruptError",
        "StoreExternalChangeError",
    ),
    store_document_validation: ("MAX_REVISION", "StoreReadiness"),
    file_lease: ("StoreLockedError", "_FileLease"),
}


class ModuleSurface(unittest.TestCase):
    def test_every_released_name_is_the_owning_module_s_own_object(self) -> None:
        for owner, names in REEXPORTS.items():
            for name in names:
                with self.subTest(module=owner.__name__, name=name):
                    self.assertIs(getattr(store, name), getattr(owner, name))

    def test_the_document_roster_is_still_the_frozen_current_roster(self) -> None:
        self.assertEqual(frozenset(store.DEFAULTS), store.store_rosters.V6_DOCUMENT_NAMES)

    def test_each_store_method_is_defined_by_exactly_one_class(self) -> None:
        """A name two classes in the MRO define is resolved by accident."""

        owners: dict[str, list[str]] = {}
        for klass in Store.__mro__:
            if klass is object:
                continue
            for name, value in vars(klass).items():
                if callable(value) or isinstance(value, (property, staticmethod)):
                    owners.setdefault(name, []).append(klass.__name__)
        shadowed = {name: found for name, found in owners.items() if len(found) > 1}
        self.assertEqual(shadowed, {})


class CollaboratorImports(unittest.TestCase):
    def test_a_collaborator_imports_without_pulling_in_the_facade(self) -> None:
        """A collaborator that needs `workstack.store` back would be a cycle."""

        for module in COLLABORATORS:
            with self.subTest(module=module):
                probe = (
                    "import sys, importlib;"
                    "importlib.import_module({!r});"
                    "print('workstack.store' in sys.modules)".format(module)
                )
                result = subprocess.run(
                    [sys.executable, "-c", probe],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), "False", result.stderr)


class ComposedStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_nested_write_reuses_the_one_lease_the_transaction_holds(self) -> None:
        """One lease per Store, and reentrancy takes no second one."""

        with self.store.transaction():
            outsider = _FileLease(self.root / store.LOCK_NAME)
            with self.assertRaises(StoreLockedError):
                outsider.acquire()
            notes = self.store.load("notes.json")
            notes["notes"].append({"id": "N-nested", "title": "Nested", "body": "x"})
            self.store.save("notes.json", notes)

        self.assertEqual(
            json.loads((self.root / "notes.json").read_text(encoding="utf-8"))["notes"][-1]["id"],
            "N-nested",
        )
        self.assertFalse(self.store.journal_path.exists())
        regained = _FileLease(self.root / store.LOCK_NAME)
        regained.acquire()
        regained.release()

    def test_sync_inspection_still_reads_through_the_instance_hash_seam(self) -> None:
        """The fault-injection seam the sync tests use survives composition."""

        poisoned = dict(self.store._read_manifest_locked()["files"])
        poisoned["notes.json"] = "sha256:" + "9" * 64
        with mock.patch.object(
            self.store, "_authoritative_hashes_locked", return_value=poisoned
        ):
            status = self.store.sync_status()
        self.assertEqual(status["state"], "external-change-detected")
        self.assertEqual(status["changed_files"], ["notes.json"])
        self.assertEqual(self.store.sync_status()["state"], "in-sync")

    def test_journal_replay_writes_through_the_stores_own_atomic_write(self) -> None:
        """Recovery is a mixin now; it must still use the Store's write seam."""

        notes = json.loads((self.root / "notes.json").read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-recovered", "title": "Recovered", "body": "y"})
        digest = store._compact_json(notes)
        self.store._atomic_write_locked(
            self.store.journal_path,
            {
                "version": 1,
                "operation_id": "refactor-contract-replay",
                "created_at": store._utc_stamp(),
                "writes": [
                    {
                        "name": "notes.json",
                        "value": notes,
                        "sha256": "sha256:" + hashlib.sha256(digest).hexdigest(),
                    }
                ],
            },
        )
        written: list[str] = []
        original = Store._atomic_write_locked

        def record(instance: Store, path: Path, value: object) -> None:
            written.append(path.name)
            original(instance, path, value)

        with mock.patch.object(Store, "_atomic_write_locked", record):
            with self.store.transaction():
                pass

        self.assertIn("notes.json", written)
        self.assertFalse(self.store.journal_path.exists())
        replayed = json.loads((self.root / "notes.json").read_text(encoding="utf-8"))
        self.assertEqual(replayed["notes"][-1]["id"], "N-recovered")


if __name__ == "__main__":
    unittest.main()
