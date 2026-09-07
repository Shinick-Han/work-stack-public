"""Contract tests for the fail-closed owner-authority primitive."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workstack import owner_authority as owner_authority_module
from workstack.owner_authority import (
    AUTHORITY_STATES,
    COMMIT_UNKNOWN,
    EXCLUSIVE_LOCAL_HELD,
    OWNER_ROUTE_REQUIRED,
    OWNER_UNAVAILABLE,
    RECOVERY_OR_SYNC_BLOCKED,
    WORKSPACE_MISMATCH,
    acquire_owner_authority,
)
from workstack.store import DEFAULTS, JOURNAL_NAME, LOCK_NAME, Store, _FileLease


OTHER_UID = "4d36e96e-e325-41ce-bfc1-08002be10318"
V4_UID = "11111111-1111-4111-8111-111111111111"


def _listing(directory: Path) -> dict[str, bytes | None]:
    if not directory.exists():
        return {}
    return {
        path.name: (
            path.read_bytes() if path.is_file() and not path.is_symlink() else None
        )
        for path in directory.iterdir()
    }


def _journal() -> dict[str, object]:
    value = {"version": 3, "tasks": []}
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "version": 1,
        "operation_id": "operation-1",
        "created_at": "2026-08-31T10:00:00Z",
        "writes": [
            {
                "name": "backlog.json",
                "value": value,
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            }
        ],
    }


class _AcquireCounter:
    def __init__(self) -> None:
        self.count = 0
        self._real = _FileLease.acquire

    def wrapper(self, lease: _FileLease) -> None:
        self.count += 1
        return self._real(lease)


class OwnerAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.data_dir = self.home / "data"
        self.runtime_dir = self.home / "runtime"
        environment = patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.runtime_dir)}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _fresh_store(self, data_dir: Path | None = None) -> tuple[Store, str]:
        root = self.data_dir if data_dir is None else data_dir
        store = Store(root)
        readiness = store.initialize()
        return store, readiness.workspace_uid

    def test_required_states_are_exactly_the_six_named_outcomes(self) -> None:
        self.assertEqual(
            AUTHORITY_STATES,
            {
                COMMIT_UNKNOWN,
                EXCLUSIVE_LOCAL_HELD,
                OWNER_ROUTE_REQUIRED,
                OWNER_UNAVAILABLE,
                RECOVERY_OR_SYNC_BLOCKED,
                WORKSPACE_MISMATCH,
            },
        )

    def test_no_owner_acquires_once_retains_handle_and_releases_once(self) -> None:
        _store, uid = self._fresh_store()
        counter = _AcquireCounter()

        def count_acquire(lease: _FileLease) -> None:
            return counter.wrapper(lease)

        with patch.object(_FileLease, "acquire", count_acquire):
            authority = acquire_owner_authority(
                data_dir=self.data_dir, expected_workspace_uid=uid
            )
            self.assertEqual(authority.state, EXCLUSIVE_LOCAL_HELD)
            self.assertIs(authority.lease, authority.store._server_lease)
            self.assertEqual(counter.count, 1)

            def operation(store: Store) -> str:
                with store.transaction():
                    with store.transaction():
                        loaded = store.load("workspace.json")
                    with store.consistent_read():
                        self.assertEqual(loaded["id"], uid)
                return loaded["id"]

            self.assertEqual(authority.run(operation), uid)
            self.assertEqual(counter.count, 1)
            self.assertIsNone(authority.store._server_lease)
            self.assertTrue(authority._released)

            again = acquire_owner_authority(
                data_dir=self.data_dir, expected_workspace_uid=uid
            )
            self.assertEqual(again.state, EXCLUSIVE_LOCAL_HELD)
            self.assertEqual(counter.count, 2)
            again.release()

    def test_hung_owner_holding_lease_is_owner_route_with_zero_local_operation(
        self,
    ) -> None:
        store, uid = self._fresh_store()
        store.write_server_info("127.0.0.1", 9)
        blocker = _FileLease(self.data_dir / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        ran: list[object] = []

        def operation(local: Store) -> None:
            ran.append(local.root)
            local.load("workspace.json")

        authority = acquire_owner_authority(
            data_dir=self.data_dir, expected_workspace_uid=uid
        )
        self.assertEqual(authority.state, OWNER_ROUTE_REQUIRED)
        self.assertIsNone(authority.lease)
        self.assertIsNone(authority.run(operation))
        self.assertEqual(ran, [])
        self.assertTrue(store.server_info_path.is_file())

    def test_stale_metadata_with_free_lease_is_local_without_network(self) -> None:
        store, uid = self._fresh_store()
        store.write_server_info("127.0.0.1", 9)
        before = store.server_info_path.read_bytes()

        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("network must not be used")

        with patch("socket.create_connection", forbidden), patch(
            "http.client.HTTPConnection", forbidden
        ), patch("urllib.request.urlopen", forbidden):
            authority = acquire_owner_authority(
                data_dir=self.data_dir, expected_workspace_uid=uid
            )
            self.assertEqual(authority.state, EXCLUSIVE_LOCAL_HELD)
            observed = authority.run(lambda held: held.load("workspace.json")["id"])
        self.assertEqual(observed, uid)
        self.assertEqual(store.server_info_path.read_bytes(), before)

    def test_workspace_mismatch_performs_zero_mutation(self) -> None:
        self._fresh_store()
        before_data = _listing(self.data_dir)
        before_runtime = _listing(self.runtime_dir)
        authority = acquire_owner_authority(
            data_dir=self.data_dir, expected_workspace_uid=OTHER_UID
        )
        self.assertEqual(authority.state, WORKSPACE_MISMATCH)
        self.assertIsNone(authority.store)
        self.assertIsNone(authority.lease)
        self.assertEqual(_listing(self.data_dir), before_data)
        self.assertEqual(_listing(self.runtime_dir), before_runtime)

    def test_invalid_and_recovery_and_sync_perform_zero_mutation(self) -> None:
        empty = self.home / "empty"
        empty.mkdir()
        empty_before = _listing(empty)
        invalid = acquire_owner_authority(
            data_dir=empty, expected_workspace_uid=OTHER_UID
        )
        self.assertEqual(invalid.state, RECOVERY_OR_SYNC_BLOCKED)
        self.assertEqual(_listing(empty), empty_before)

        v4 = self.home / "v4"
        v4.mkdir()
        (v4 / "workspace.json").write_text(
            json.dumps(
                {"version": 2, "id": V4_UID, "name": "v4 authority"},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (v4 / "store.json").write_text(
            json.dumps(
                {"format": "workstack.ssot", "schema_version": 4},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        v4_before = _listing(v4)
        blocked_format = acquire_owner_authority(
            data_dir=v4, expected_workspace_uid=V4_UID
        )
        self.assertEqual(blocked_format.state, RECOVERY_OR_SYNC_BLOCKED)
        self.assertEqual(_listing(v4), v4_before)

        _store, uid = self._fresh_store()
        journal_path = self.data_dir / JOURNAL_NAME
        journal_path.write_text(json.dumps(_journal()), encoding="utf-8")
        journal_before = _listing(self.data_dir)
        recovered = acquire_owner_authority(
            data_dir=self.data_dir, expected_workspace_uid=uid
        )
        self.assertEqual(recovered.state, RECOVERY_OR_SYNC_BLOCKED)
        self.assertIsNone(recovered.lease)
        self.assertEqual(_listing(self.data_dir), journal_before)
        self.assertTrue(journal_path.is_file())

        other, other_uid = self._fresh_store(self.home / "sync")
        backlog = other.path("backlog.json")
        original = backlog.read_bytes()
        backlog.write_bytes(original + b"\n")
        mutated = backlog.read_bytes()
        listing = _listing(other.root)
        sync_blocked = acquire_owner_authority(
            data_dir=other.root, expected_workspace_uid=other_uid
        )
        self.assertEqual(sync_blocked.state, RECOVERY_OR_SYNC_BLOCKED)
        self.assertIsNone(sync_blocked.lease)
        self.assertEqual(backlog.read_bytes(), mutated)
        self.assertEqual(_listing(other.root), listing)

    def test_exception_releases_owned_lease_once_without_hiding_primary(self) -> None:
        _store, uid = self._fresh_store()

        class Primary(RuntimeError):
            pass

        def boom(_store: Store) -> None:
            raise Primary("keep-primary")

        authority = acquire_owner_authority(
            data_dir=self.data_dir, expected_workspace_uid=uid
        )
        self.assertEqual(authority.state, EXCLUSIVE_LOCAL_HELD)
        with self.assertRaises(Primary) as raised:
            authority.run(boom)
        self.assertEqual(str(raised.exception), "keep-primary")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(authority._released)
        self.assertIsNone(authority.store._server_lease)

        replacement = acquire_owner_authority(
            data_dir=self.data_dir, expected_workspace_uid=uid
        )
        self.assertEqual(replacement.state, EXCLUSIVE_LOCAL_HELD)
        replacement.release()

    def test_two_workspaces_never_share_an_authority_or_lease(self) -> None:
        first_dir = self.home / "ws-a"
        second_dir = self.home / "ws-b"
        _first, first_uid = self._fresh_store(first_dir)
        _second, second_uid = self._fresh_store(second_dir)
        first = acquire_owner_authority(
            data_dir=first_dir, expected_workspace_uid=first_uid
        )
        second = acquire_owner_authority(
            data_dir=second_dir, expected_workspace_uid=second_uid
        )
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.assertEqual(first.state, EXCLUSIVE_LOCAL_HELD)
        self.assertEqual(second.state, EXCLUSIVE_LOCAL_HELD)
        self.assertIsNot(first, second)
        self.assertIsNot(first.store, second.store)
        self.assertIsNot(first.lease, second.lease)
        self.assertNotEqual(first.store.root, second.store.root)
        self.assertNotEqual(first.lease.path, second.lease.path)
        self.assertEqual(first.workspace_uid, first_uid)
        self.assertEqual(second.workspace_uid, second_uid)
        self.assertNotEqual(first.workspace_uid, second.workspace_uid)

    def test_commit_unknown_never_acquires_falls_back_or_retries(self) -> None:
        _store, uid = self._fresh_store()
        before = _listing(self.data_dir)
        counter = _AcquireCounter()

        def count_acquire(lease: _FileLease) -> None:
            return counter.wrapper(lease)

        with patch.object(_FileLease, "acquire", count_acquire):
            first = acquire_owner_authority(
                data_dir=self.data_dir,
                expected_workspace_uid=uid,
                owner_mutation="possible_send",
            )
            second = acquire_owner_authority(
                data_dir=self.data_dir,
                expected_workspace_uid=uid,
                owner_mutation="unknown",
            )
        self.assertEqual(first.state, COMMIT_UNKNOWN)
        self.assertEqual(second.state, COMMIT_UNKNOWN)
        self.assertIsNone(first.lease)
        self.assertIsNone(second.store)
        self.assertEqual(counter.count, 0)
        self.assertEqual(_listing(self.data_dir), before)
        self.assertIsNone(first.run(lambda store: store.load("workspace.json")))

    def test_lease_held_without_metadata_is_owner_unavailable(self) -> None:
        _store, uid = self._fresh_store()
        blocker = _FileLease(self.data_dir / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        authority = acquire_owner_authority(
            data_dir=self.data_dir, expected_workspace_uid=uid
        )
        self.assertEqual(authority.state, OWNER_UNAVAILABLE)
        self.assertIsNone(authority.lease)

    def test_module_does_not_import_network_or_pid_heuristics(self) -> None:
        tree = ast.parse(
            Path(owner_authority_module.__file__).read_text(encoding="utf-8")
        )
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(
            imported
            & {"http", "urllib", "socket", "requests", "subprocess", "psutil"}
        )
        source = Path(owner_authority_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.getpid", source)
        self.assertNotIn("st_mtime", source)

    def test_store_keeps_max_commit_events_rationale_and_reexports_lease(self) -> None:
        root = Path(__file__).resolve().parents[1]
        store_source = (root / "workstack" / "store.py").read_text(encoding="utf-8")
        self.assertIn(
            "The most events ONE successful _commit_prepared_locked can emit.",
            store_source,
        )
        self.assertIn(
            "tests drive each real branch instead of asserting a",
            store_source,
        )
        self.assertIn("# constant.", store_source)
        self.assertIn("from .file_lease import StoreLockedError, _FileLease", store_source)
        self.assertNotIn("\nclass StoreLockedError(", store_source)
        self.assertNotIn("\nclass _FileLease:", store_source)
        import workstack.file_lease as file_lease_module
        import workstack.store as store_module

        self.assertIs(store_module.StoreLockedError, file_lease_module.StoreLockedError)
        self.assertIs(store_module._FileLease, file_lease_module._FileLease)

    def test_lease_extraction_does_not_create_a_circular_import(self) -> None:
        root = Path(__file__).resolve().parents[1]
        lease_source = (root / "workstack" / "file_lease.py").read_text(encoding="utf-8")
        tree = ast.parse(lease_source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(imported & {"workstack.store", ".store", "store"})
        self.assertNotIn("from .store", lease_source)
        self.assertNotIn("from workstack.store", lease_source)
        self.assertNotIn("import workstack.store", lease_source)
        probe = (
            "import workstack.file_lease as lease\n"
            "import workstack.store as store\n"
            "import workstack.owner_authority as authority\n"
            "assert lease.StoreLockedError is store.StoreLockedError\n"
            "assert lease._FileLease is store._FileLease\n"
            "assert authority._FileLease is lease._FileLease\n"
            "assert not hasattr(lease, 'Store')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
