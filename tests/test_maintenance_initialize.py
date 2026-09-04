"""``maintenance initialize``: one fresh Store, only into an absent or empty directory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from workstack.maintenance import StoreInitializationRefused, initialize_store
from workstack.store import (
    DEFAULTS,
    JOURNAL_NAME,
    LOCK_NAME,
    STORE_SCHEMA_VERSION,
    Store,
    StoreLockedError,
    _FileLease,
)


ROOT = Path(__file__).resolve().parents[1]


def _listing(directory: Path) -> dict[str, bytes | None]:
    return {
        path.name: (
            path.read_bytes() if path.is_file() and not path.is_symlink() else None
        )
        for path in directory.iterdir()
    }


class InitializeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.root / "runtime")}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_initialize_creates_a_complete_fresh_v3_store_in_an_absent_directory(self) -> None:
        destination = self.root / "new"

        receipt = initialize_store(destination)

        self.assertEqual(receipt.destination, destination.resolve())
        for name in DEFAULTS:
            self.assertTrue((destination / name).is_file(), name)
        metadata = json.loads((destination / "store-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["migrations"]["identity"]["origin"], "fresh")
        workspace = json.loads((destination / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(workspace["id"], receipt.workspace_id)
        parsed = uuid.UUID(receipt.workspace_id)
        self.assertEqual(str(parsed), receipt.workspace_id)
        self.assertNotEqual(parsed.int, 0)
        self.assertEqual(receipt.store_schema_version, STORE_SCHEMA_VERSION)
        self.assertFalse((destination / JOURNAL_NAME).exists())

        again = Store(destination).initialize()
        self.assertEqual(again.workspace_uid, receipt.workspace_id)
        self.assertEqual(again.migration_origin, "fresh")

    def test_initialize_accepts_an_empty_or_lease_only_directory(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        lease_only = self.root / "lease-only"
        lease_only.mkdir()
        (lease_only / LOCK_NAME).write_bytes(b"\0")
        workspace_ids: set[str] = set()
        for destination in (empty, lease_only):
            with self.subTest(destination=destination.name):
                receipt = initialize_store(destination)
                for name in DEFAULTS:
                    self.assertTrue((destination / name).is_file(), name)
                self.assertEqual(
                    json.loads((destination / "workspace.json").read_text(encoding="utf-8"))["id"],
                    receipt.workspace_id,
                )
                workspace_ids.add(receipt.workspace_id)
        self.assertEqual(len(workspace_ids), 2)

    def test_initialize_refuses_any_entry_without_writes(self) -> None:
        eight = sorted(set(DEFAULTS) - {"store-meta.json"})
        cases: list[tuple[str, dict[str, bytes]]] = [
            (name, {name: b"{}"}) for name in sorted(DEFAULTS)
        ]
        cases += [
            (JOURNAL_NAME, {JOURNAL_NAME: b"{}"}),
            ("store.json", {"store.json": b"{}"}),
            ("desktop.ini", {"desktop.ini": b"[.ShellClassInfo]"}),
            ("v2-roster", {name: b"{}" for name in eight}),
        ]
        for label, contents in cases:
            with self.subTest(case=label):
                destination = self.root / label.replace(".", "-")
                destination.mkdir()
                for name, body in contents.items():
                    (destination / name).write_bytes(body)
                before = _listing(destination)

                with self.assertRaisesRegex(
                    StoreInitializationRefused, "already contains|not a directory"
                ):
                    initialize_store(destination)

                self.assertEqual(_listing(destination), before)
                if "store-meta.json" not in contents:
                    self.assertFalse((destination / "store-meta.json").exists())

        with self.subTest(case="dangling-link"):
            destination = self.root / "dangling-link"
            destination.mkdir()
            try:
                os.symlink(destination / "missing-target", destination / "workspace.json")
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symbolic links are not permitted here: {error}")
            before = _listing(destination)
            with self.assertRaisesRegex(StoreInitializationRefused, "already contains"):
                initialize_store(destination)
            self.assertEqual(_listing(destination), before)

        with self.subTest(case="regular-file"):
            destination = self.root / "regular-file"
            destination.write_bytes(b"not a directory")
            with self.assertRaisesRegex(StoreInitializationRefused, "not a directory"):
                initialize_store(destination)
            self.assertEqual(destination.read_bytes(), b"not a directory")

    def test_initialize_refuses_when_another_writer_holds_the_lease(self) -> None:
        destination = self.root / "leased"
        destination.mkdir()
        lease = _FileLease(destination / LOCK_NAME)
        lease.acquire()
        self.addCleanup(lease.release)

        with self.assertRaises(StoreLockedError):
            initialize_store(destination)

        self.assertEqual(sorted(path.name for path in destination.iterdir()), [LOCK_NAME])


class LauncherInitializeTest(unittest.TestCase):
    def test_run_work_stack_launcher_initializes_a_fresh_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            command = [
                sys.executable,
                "-X",
                "utf8",
                "-B",
                str(ROOT / "run_work_stack.py"),
                "--data-dir",
                str(data),
                "maintenance",
                "initialize",
            ]
            environment = {**os.environ, "WORK_STACK_RUNTIME": str(root / "runtime")}

            first = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            receipt = json.loads(first.stdout)
            self.assertEqual(
                set(receipt), {"destination", "workspace_id", "store_schema_version"}
            )
            self.assertEqual(Path(receipt["destination"]), data.resolve())
            for name in DEFAULTS:
                self.assertTrue((data / name).is_file(), name)
            self.assertEqual(
                json.loads((data / "workspace.json").read_text(encoding="utf-8"))["id"],
                receipt["workspace_id"],
            )
            before = {path.name: path.read_bytes() for path in data.iterdir()}

            second = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(second.returncode, 2, second.stdout)
            self.assertTrue(
                second.stderr.startswith("error: destination already contains entries"),
                second.stderr,
            )
            self.assertEqual({path.name: path.read_bytes() for path in data.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
