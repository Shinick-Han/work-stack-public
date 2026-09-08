"""The receipt guard, proved against a real second process.

Comparing receipt bytes and then unlinking by pathname is two operations, and
no filesystem call fuses them, so the only thing that can stop a competing
worker deleting a freshly published receipt is that the two workers take the
same lock.  These tests run that competition for real: a second interpreter
races the first, and the same race is run twice, once through the guarded entry
point and once through the unguarded core, so the guard is shown to be what
makes the difference rather than asserted to be.

Everything here is task-owned: two processes this file starts, a temporary
directory it creates, and injected process controllers.  No signal is sent to
anything, and the real writer lease is never opened.
"""

from __future__ import annotations

import json
import os
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

import remote_owner as OWNER  # noqa: E402
import remote_receipt_guard as GUARD  # noqa: E402
from remote_command_contract import token_hash  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWN_TOKEN = "r5pending-token-not-enforced-01"
HOST_IDENTITY = "b" * 64
BOOT_IDENTITY = "c" * 64
OLD_PID = 4242
OLD_START = "start-1"
NEW_PID = 5151
NEW_START = "start-9"
BARRIER_BUDGET_SECONDS = 30.0

# The competing worker. It reclaims the receipt it can see, and it pauses at
# exactly the moment the review's probe injected a replacement: after the byte
# comparison has passed and before the unlink. GUARDED decides whether it does
# that through the guarded entry point or through the unguarded core.
_COMPETITOR_SRC = '''
import json, sys, time
from pathlib import Path

SHELL, DATA, READY, RELEASE, RESULT, GUARDED = sys.argv[1:7]
sys.path.insert(0, SHELL)

import remote_owner as OWNER
import remote_receipt_guard as GUARD


class Controller:
    """Reports the old owner gone and any newer owner still running."""

    def __init__(self):
        self.pid = {new_pid}
        self.start = "{new_start}"

    def current_pid(self):
        return self.pid

    def start_identity(self, pid):
        return self.start if pid == self.pid else None

    def observe(self, pid, start_identity):
        if pid != self.pid or start_identity != self.start:
            return "exited"
        return "live"

    def open_owned_process(self, pid, start_identity):
        raise AssertionError("a reclaim must never signal anything")

    def host_identity(self):
        return "{host}"

    def boot_identity(self):
        return "{boot}"


data = Path(DATA)
real_unlink = OWNER.unlink_owner_receipt


def paused_unlink(directory):
    """Stall between the passed comparison and the unlink it authorised."""

    Path(READY).write_text("at-unlink\\n")
    deadline = time.monotonic() + {budget}
    while time.monotonic() < deadline and not Path(RELEASE).exists():
        time.sleep(0.01)
    return real_unlink(directory)


OWNER.unlink_owner_receipt = paused_unlink
OWNER.set_process_controller(Controller())
Path(READY).write_text("started\\n")

outcome = {{"guarded": GUARDED}}
try:
    if GUARDED == "yes":
        OWNER.reclaim_or_refuse_owner(data)
    else:
        OWNER._reclaim_or_refuse_owner_locked(data)
    outcome["refused"] = None
except OWNER.EntryError as error:
    outcome["refused"] = str(error)
Path(RESULT).write_text(json.dumps(outcome))
'''.format(
    new_pid=NEW_PID,
    new_start=NEW_START,
    host=HOST_IDENTITY,
    boot=BOOT_IDENTITY,
    budget=BARRIER_BUDGET_SECONDS,
)


def _receipt_bytes(data: Path, *, pid: int, start: str) -> bytes:
    payload = {
        "workspace_id": WORKSPACE_ID,
        "data_dir_digest": OWNER.local_path_digest(data),
        "app_dir_digest": "a" * 64,
        "pid": pid,
        "start_identity": start,
        "release_id": "1.0.7",
        "token_hash": token_hash(OWN_TOKEN),
        "host_identity": HOST_IDENTITY,
        "boot_identity": BOOT_IDENTITY,
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _wait_for(path: Path, *, seconds: float = BARRIER_BUDGET_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


class ReceiptGuardFileTest(unittest.TestCase):
    """The guard is its own inode, and it stays one."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)

    def test_the_guard_inode_survives_every_hold(self) -> None:
        path = GUARD.guard_path(self.data)
        with GUARD.owner_receipt_guard(self.data):
            self.assertTrue(path.is_file())
            first = path.stat().st_ino
        self.assertTrue(path.is_file(), "the guard inode was unlinked")
        with GUARD.owner_receipt_guard(self.data):
            self.assertEqual(path.stat().st_ino, first)
        self.assertTrue(path.is_file())

    def test_the_guard_is_not_the_real_writer_lease(self) -> None:
        lease = self.data / ".workstack.lock"
        lease.write_bytes(b"real writer lease, owned by the store\n")
        with GUARD.owner_receipt_guard(self.data):
            pass
        self.assertNotEqual(GUARD.GUARD_FILENAME, ".workstack.lock")
        self.assertEqual(lease.read_bytes(), b"real writer lease, owned by the store\n")

    def test_a_guard_that_is_not_a_regular_file_is_refused(self) -> None:
        GUARD.guard_path(self.data).mkdir()
        with self.assertRaises(GUARD.GuardUnavailable):
            with GUARD.owner_receipt_guard(self.data):
                self.fail("a directory must never be accepted as the guard")

    def test_acquisition_is_bounded_and_refuses_instead_of_spinning(self) -> None:
        slept: list[float] = []
        now = [0.0]

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        wait = GUARD.GuardWait(0.2, 0.05, monotonic, sleep)
        with GUARD.owner_receipt_guard(self.data):
            # A second hold in this process would be re-entrant, so the
            # contention is made by refusing every acquisition attempt.
            with mock.patch.object(GUARD, "_try_lock", return_value=False):
                with self.assertRaises(GUARD.GuardContended) as caught:
                    with GUARD.owner_receipt_guard(self.data, wait):
                        self.fail("a contended guard must not be entered")
        self.assertIn(GUARD.GUARD_HELD_DETAIL, str(caught.exception))
        self.assertEqual(slept, [0.05, 0.05, 0.05, 0.05])


class ReceiptGuardTwoProcessBarrierTest(unittest.TestCase):
    """A competing reclaim must not delete a receipt published while it decides."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.ready = self.root / "competitor.ready"
        self.release = self.root / "competitor.release"
        self.result = self.root / "competitor.result"
        source = self.root / "competitor.py"
        source.write_text(_COMPETITOR_SRC, encoding="utf-8")
        self.source = source
        self.old = _receipt_bytes(self.data, pid=OLD_PID, start=OLD_START)
        self.fresh = _receipt_bytes(self.data, pid=NEW_PID, start=NEW_START)

    def _start_competitor(self, *, guarded: bool) -> subprocess.Popen:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("PYTHONPATH", None)
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(self.source),
                str(SHELL),
                str(self.data),
                str(self.ready),
                str(self.release),
                str(self.result),
                "yes" if guarded else "no",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
        )
        self.addCleanup(_terminate, child)
        return child

    def _competitor_outcome(self, child: subprocess.Popen) -> dict[str, object]:
        self.assertEqual(child.wait(timeout=BARRIER_BUDGET_SECONDS), 0)
        return json.loads(self.result.read_text(encoding="utf-8"))

    def test_the_guard_stops_a_competitor_deleting_a_fresh_receipt(self) -> None:
        receipt = self.data / OWNER.OWNER_FILENAME
        receipt.write_bytes(self.old)
        with GUARD.owner_receipt_guard(self.data):
            child = self._start_competitor(guarded=True)
            self.assertTrue(_wait_for(self.ready), "the competitor never started")
            # This is the publication the review's probe deleted: a new owner
            # written while another worker is inside its reclaim decision.
            receipt.write_bytes(self.fresh)
        outcome = self._competitor_outcome(child)
        self.assertEqual(receipt.read_bytes(), self.fresh)
        self.assertIsNotNone(outcome["refused"], outcome)
        self.assertIn("REMOTE_LOCK_OWNED", str(outcome["refused"]))
        # It never reached the unlink, so it never had to be stalled there.
        self.assertEqual(self.ready.read_text(encoding="utf-8").strip(), "started")

    def test_without_the_guard_the_same_race_deletes_the_fresh_receipt(self) -> None:
        """The negative control: the byte comparison alone does not hold."""

        receipt = self.data / OWNER.OWNER_FILENAME
        receipt.write_bytes(self.old)
        child = self._start_competitor(guarded=False)
        self.assertTrue(_wait_for(self.ready), "the competitor never started")
        deadline = time.monotonic() + BARRIER_BUDGET_SECONDS
        while time.monotonic() < deadline:
            if self.ready.read_text(encoding="utf-8").strip() == "at-unlink":
                break
            time.sleep(0.01)
        self.assertEqual(self.ready.read_text(encoding="utf-8").strip(), "at-unlink")
        # The comparison against the old bytes has already passed.
        receipt.write_bytes(self.fresh)
        self.release.write_text("go\n", encoding="utf-8")
        self._competitor_outcome(child)
        self.assertFalse(receipt.exists(), "this is the defect the guard removes")


def _terminate(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.kill()
    child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
