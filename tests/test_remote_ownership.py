"""Wave 2 R2: owner receipt, stop-owned, and dead-owner recovery. No live SSH."""

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

import remote_entry as ENTRY  # noqa: E402
from remote_command_contract import token_hash  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWN_TOKEN = "r5pending-token-not-enforced-01"
FOREIGN_TOKEN = "foreign-owner-token-not-enforced-02"


class FakeProcessController:
    def __init__(self, pid: int = 4242, start: str = "start-1", alive: bool | None = True) -> None:
        self.pid = pid
        self.start = start
        self.alive = alive
        self.terminated: list[int] = []

    def current_pid(self) -> int:
        return self.pid

    def start_identity(self, pid: int) -> str | None:
        if pid == self.pid:
            return self.start
        return None

    def is_alive(self, pid: int, start_identity: str) -> bool | None:
        if pid != self.pid or start_identity != self.start:
            return False
        return self.alive

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        self.alive = False


def _write_probe_fixture(root: Path) -> tuple[Path, Path]:
    app = root / "app"
    data = root / "data"
    (app / "workstack").mkdir(parents=True)
    data.mkdir()
    (app / "workstack" / "__init__.py").write_text(
        '__version__ = "1.0.7"\nREMOTE_PROTOCOL_VERSION = 1\n',
        encoding="utf-8",
    )
    (app / "run_work_stack.py").write_text("print(1)\n", encoding="utf-8")
    (data / "workspace.json").write_text(
        json.dumps({"id": WORKSPACE_ID, "name": "probe", "version": 2}),
        encoding="utf-8",
    )
    (data / "store-meta.json").write_text(
        json.dumps({"schema_version": 3}),
        encoding="utf-8",
    )
    return app, data


def _write_receipt(data: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "data_dir_digest": ENTRY.local_path_digest(data),
        "app_dir_digest": "a" * 64,
        "pid": 4242,
        "start_identity": "start-1",
        "release_id": "1.0.7",
        "token_hash": token_hash(OWN_TOKEN),
    }
    payload.update(overrides)
    (data / ENTRY.OWNER_FILENAME).write_text(
        json.dumps(payload, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class RemoteOwnershipTest(unittest.TestCase):
    def tearDown(self) -> None:
        ENTRY.set_process_controller(None)

    def test_own_owner_stop_terminates_only_matching_pid(self) -> None:
        controller = FakeProcessController()
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            ENTRY.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual(controller.terminated, [4242])
            self.assertFalse((data / ENTRY.OWNER_FILENAME).exists())

    def test_foreign_owner_stop_refuses_and_does_not_kill(self) -> None:
        controller = FakeProcessController()
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(ENTRY.EntryError, "REMOTE_LOCK_OWNED"):
                ENTRY.run_stop_owned(data, FOREIGN_TOKEN)
            self.assertEqual(controller.terminated, [])
            self.assertTrue((data / ENTRY.OWNER_FILENAME).exists())

    def test_dead_owner_is_reclaimed_without_kill(self) -> None:
        controller = FakeProcessController(alive=False)
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            payload = ENTRY.run_probe(_app, data)
            self.assertEqual(controller.terminated, [])
            self.assertFalse((data / ENTRY.OWNER_FILENAME).exists())
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(decoded["workspace_id"], WORKSPACE_ID)

    def test_malformed_owner_receipt_blocks_without_kill(self) -> None:
        controller = FakeProcessController()
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data, extra="nope")
            with self.assertRaisesRegex(ENTRY.EntryError, "owner receipt shape"):
                ENTRY.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual(controller.terminated, [])
            self.assertTrue((data / ENTRY.OWNER_FILENAME).exists())

    def test_ambiguous_liveness_blocks_without_kill(self) -> None:
        controller = FakeProcessController(alive=None)
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(ENTRY.EntryError, "ambiguous"):
                ENTRY.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual(controller.terminated, [])
            self.assertTrue((data / ENTRY.OWNER_FILENAME).exists())

    def test_live_owner_blocks_probe_and_serve(self) -> None:
        controller = FakeProcessController()
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(ENTRY.EntryError, "REMOTE_LOCK_OWNED"):
                ENTRY.run_probe(app, data)
            parsed = ENTRY.parse_remote_entry_argv(
                [
                    "serve",
                    "--app-dir",
                    str(app),
                    "--data-dir",
                    str(data),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8765",
                    "--public-port",
                    "18765",
                    "--session-token",
                    FOREIGN_TOKEN,
                ]
            )
            with self.assertRaisesRegex(ENTRY.EntryError, "REMOTE_LOCK_OWNED"):
                ENTRY.run_serve(parsed)
            self.assertEqual(controller.terminated, [])

    def test_owner_receipt_stores_hash_not_raw_token(self) -> None:
        controller = FakeProcessController()
        ENTRY.set_process_controller(controller)
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            parsed = ENTRY.parse_remote_entry_argv(
                [
                    "serve",
                    "--app-dir",
                    str(app),
                    "--data-dir",
                    str(data),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8765",
                    "--public-port",
                    "18765",
                    "--session-token",
                    OWN_TOKEN,
                    "--exit-with-parent",
                ]
            )
            with mock.patch.object(ENTRY.os, "execv"):
                ENTRY.run_serve(parsed)
            raw = (data / ENTRY.OWNER_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(OWN_TOKEN, raw)
        receipt = json.loads(raw)
        self.assertEqual(set(receipt), set(ENTRY.OWNER_KEYS))
        self.assertEqual(receipt["token_hash"], token_hash(OWN_TOKEN))
        self.assertEqual(receipt["pid"], 4242)


if __name__ == "__main__":
    unittest.main()
