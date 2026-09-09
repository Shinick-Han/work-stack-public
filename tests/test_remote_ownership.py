"""Wave 2 R2: owner receipt, stop-owned, and dead-owner recovery. No live SSH.

The receipt is fenced by host and boot identity because the data directory can
be a share.  These tests hold the line that a pid is only ever interpreted for
this host and this boot, that stop-owned proves its own process is gone before
removing anything, and that none of it ever touches the real writer lease.

The csh-family parent-loss regression and the pidfd proof at the bottom run
real processes.  Both are skipped off Linux, the first when no csh-family shell
is available and the second when the kernel has no pidfd signalling.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_entry as ENTRY  # noqa: E402
import remote_owner as OWNER  # noqa: E402
import remote_process_handle as HANDLE  # noqa: E402
import remote_receipt_guard as GUARD  # noqa: E402
from remote_command_contract import join_serve_command, token_hash  # noqa: E402
from remote_stop_result import EXIT_UNCONFIRMED  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWN_TOKEN = "r5pending-token-not-enforced-01"
FOREIGN_TOKEN = "foreign-owner-token-not-enforced-02"
HOST_IDENTITY = "b" * 64
BOOT_IDENTITY = "c" * 64
OTHER_HOST_IDENTITY = "d" * 64
OTHER_BOOT_IDENTITY = "e" * 64

# The one csh-family runtime the root explicitly authorised for this task, kept
# outside the system tree. WS_OWNER_CSH overrides it; the system shells are only
# consulted when a host already has one.
AUTHORISED_CSH = os.path.expanduser("~/ws-owner-csh-20260907/payload/usr/bin/tcsh")
CSH_CANDIDATES = (
    AUTHORISED_CSH,
    "/tmp/ws-owner-csh-20260907/payload/usr/bin/tcsh",
    "/bin/tcsh",
    "/usr/bin/tcsh",
    "/bin/csh",
    "/usr/bin/csh",
)
LIFETIME_FUNCTION = "_bind_linux_process_lifetime_to_parent"
_RECEIPT_MODULES = (
    "remote_entry.py",
    "remote_owner.py",
    "remote_process_handle.py",
    "remote_receipt_guard.py",
    "remote_receipt_io.py",
)


class FakeOwnedProcess:
    """A handle bound at open time, the way a pidfd is bound to a process.

    ``bound_start`` is fixed when the handle opens, so a test that reuses the
    pid afterwards can show the signal still went to the pinned process and
    never to whatever now answers to that number.
    """

    def __init__(self, controller: "FakeProcessController", pid: int, start: str) -> None:
        self.controller = controller
        self.pid = pid
        self.bound_start = start
        self.closes = 0

    def has_exited(self) -> bool | None:
        return self.controller.poll_handle(self)

    def send_terminate(self) -> None:
        self.controller.signal_handle(self)

    def close(self) -> None:
        self.closes += 1


class FakeProcessController:
    """Records every liveness question so a test can prove one never happened."""

    def __init__(
        self,
        pid: int = 4242,
        start: str = "start-1",
        observed: str = "live",
        host: str | None = HOST_IDENTITY,
        boot: str | None = BOOT_IDENTITY,
        exit_after_polls: int = 0,
        on_liveness=None,
        on_open=None,
        unreadable_after_signal: bool = False,
        pre_signal_exit: bool | None = False,
        terminate_error: str | None = None,
    ) -> None:
        self.pid = pid
        self.start = start
        self.observed = observed
        self.host = host
        self.boot = boot
        self.exit_after_polls = exit_after_polls
        self.on_liveness = on_liveness
        self.on_open = on_open
        self.unreadable_after_signal = unreadable_after_signal
        self.pre_signal_exit = pre_signal_exit
        self.terminate_error = terminate_error
        self.terminated: list[int] = []
        self.signalled: list[tuple[int, str]] = []
        self.liveness_queries: list[tuple[int, str]] = []
        self.opened: list[tuple[int, str]] = []
        self.handles: list[FakeOwnedProcess] = []
        self._polls_after_terminate = 0

    def current_pid(self) -> int:
        return self.pid

    def start_identity(self, pid: int) -> str | None:
        if pid == self.pid:
            return self.start
        return None

    def observe(self, pid: int, start_identity: str) -> str:
        self.liveness_queries.append((pid, start_identity))
        if pid != self.pid:
            result = "exited"
        elif start_identity != self.start:
            result = "replaced"
        else:
            result = self.observed
        # The hook runs after the answer is fixed, so a test can move the world
        # in exactly the window between this answer and whatever acts on it.
        if self.on_liveness is not None and len(self.liveness_queries) == 1:
            self.on_liveness()
        return result

    def open_owned_process(self, pid: int, start_identity: str):
        """Pin the process, or refuse when this pid no longer names it."""

        self.opened.append((pid, start_identity))
        if pid != self.pid:
            return None
        if start_identity != self.start:
            raise OWNER.EntryError(
                "REMOTE_PROTOCOL_INVALID",
                f"owned pid {pid} no longer carries its recorded start identity",
            )
        handle = FakeOwnedProcess(self, pid, self.start)
        self.handles.append(handle)
        if self.on_open is not None:
            self.on_open()
        return handle

    def poll_handle(self, handle: FakeOwnedProcess) -> bool | None:
        self.liveness_queries.append((handle.pid, handle.bound_start))
        if not self.terminated:
            return self.pre_signal_exit
        if self.unreadable_after_signal:
            return None
        self._polls_after_terminate += 1
        return self._polls_after_terminate > self.exit_after_polls

    def signal_handle(self, handle: FakeOwnedProcess) -> None:
        if self.terminate_error is not None:
            raise HANDLE.ProcessHandleUnavailable(self.terminate_error)
        self.terminated.append(handle.pid)
        # What the handle names, not what its pid names by the time of this call.
        self.signalled.append((handle.pid, handle.bound_start))

    def host_identity(self) -> str | None:
        return self.host

    def boot_identity(self) -> str | None:
        return self.boot


class FakeClock:
    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


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


def _write_receipt(data: Path, *, legacy: bool = False, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "data_dir_digest": OWNER.local_path_digest(data),
        "app_dir_digest": "a" * 64,
        "pid": 4242,
        "start_identity": "start-1",
        "release_id": "1.0.7",
        "token_hash": token_hash(OWN_TOKEN),
    }
    if not legacy:
        payload["host_identity"] = HOST_IDENTITY
        payload["boot_identity"] = BOOT_IDENTITY
    payload.update(overrides)
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    (data / OWNER.OWNER_FILENAME).write_bytes(encoded)
    return encoded


class RemoteOwnershipTest(unittest.TestCase):
    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeProcessController:
        controller = FakeProcessController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def test_own_owner_stop_terminates_only_matching_pid(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual(controller.terminated, [4242])
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())

    def test_foreign_owner_stop_refuses_and_does_not_kill(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                OWNER.run_stop_owned(data, FOREIGN_TOKEN)
            self.assertEqual(controller.terminated, [])
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())

    def test_dead_owner_is_reclaimed_without_kill(self) -> None:
        controller = self._controller(observed="exited")
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            payload = ENTRY.run_probe(_app, data)
            self.assertEqual(controller.terminated, [])
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(decoded["workspace_id"], WORKSPACE_ID)

    def test_malformed_owner_receipt_blocks_without_kill(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data, extra="nope")
            with self.assertRaisesRegex(OWNER.EntryError, "owner receipt shape"):
                OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual(controller.terminated, [])
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())

    def test_ambiguous_liveness_blocks_without_kill(self) -> None:
        controller = self._controller(observed="unknown")
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(OWNER.EntryError, "ambiguous"):
                OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual(controller.terminated, [])
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())

    def test_live_owner_blocks_probe_and_serve(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                ENTRY.run_probe(app, data)
            parsed = _serve_argv(app, data, FOREIGN_TOKEN)
            with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                ENTRY.run_serve(parsed)
            self.assertEqual(controller.terminated, [])

    def test_live_owner_admits_only_its_own_probe_token(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            payload = ENTRY.run_probe(app, data, OWN_TOKEN)
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())
            self.assertEqual(controller.terminated, [])
            raw = (data / OWNER.OWNER_FILENAME).read_text(encoding="utf-8")
            for label, token in (("foreign", FOREIGN_TOKEN), ("missing", None)):
                with self.subTest(caller=label):
                    with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                        ENTRY.run_probe(app, data, token)
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(set(decoded), set(ENTRY.PROBE_KEYS))
        self.assertEqual(decoded["workspace_id"], WORKSPACE_ID)
        self.assertNotIn(OWN_TOKEN, payload.decode("utf-8"))
        self.assertNotIn(OWN_TOKEN, raw)
        self.assertEqual(controller.terminated, [])

    def test_recognized_probe_token_does_not_start_a_second_server(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            parsed = _serve_argv(app, data, OWN_TOKEN)
            with mock.patch.object(ENTRY.os, "execv") as execv:
                with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                    ENTRY.run_serve(parsed)
            execv.assert_not_called()
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.terminated, [])

    def test_probe_argv_admits_an_optional_token_and_rejects_a_sentinel(self) -> None:
        base = ["probe", "--app-dir", "/srv/workstack/app", "--data-dir", "/srv/workstack/ssot"]
        self.assertIsNone(ENTRY.parse_remote_entry_argv(base).session_token)
        parsed = ENTRY.parse_remote_entry_argv(base + ["--session-token", OWN_TOKEN])
        self.assertEqual(parsed.session_token, OWN_TOKEN)
        with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_SESSION_TOKEN_INVALID"):
            ENTRY.parse_remote_entry_argv(base + ["--session-token", "0" * 32])
        with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_PROTOCOL_INVALID"):
            ENTRY.parse_remote_entry_argv(base + ["--session-token", "token with space"])

    def test_dead_owner_is_still_reclaimed_for_a_recognized_caller(self) -> None:
        controller = self._controller(observed="exited")
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            ENTRY.run_probe(app, data, OWN_TOKEN)
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.terminated, [])

    def test_owner_receipt_stores_hash_not_raw_token(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            parsed = _serve_argv(app, data, OWN_TOKEN, exit_with_parent=True)
            with mock.patch.object(ENTRY.os, "execv"):
                ENTRY.run_serve(parsed)
            raw = (data / OWNER.OWNER_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(OWN_TOKEN, raw)
        receipt = json.loads(raw)
        self.assertEqual(set(receipt), set(OWNER.FENCED_OWNER_KEYS))
        self.assertEqual(receipt["token_hash"], token_hash(OWN_TOKEN))
        self.assertEqual(receipt["pid"], 4242)
        self.assertEqual(controller.terminated, [])


class RemoteOwnerFencingTest(unittest.TestCase):
    """Host and boot identity decide whether a pid may be interpreted at all."""

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeProcessController:
        controller = FakeProcessController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def test_serve_records_this_host_and_boot(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            with mock.patch.object(ENTRY.os, "execv"):
                ENTRY.run_serve(_serve_argv(app, data, OWN_TOKEN))
            receipt = OWNER.read_owner_receipt(data)
        assert receipt is not None
        self.assertEqual(receipt.host_identity, HOST_IDENTITY)
        self.assertEqual(receipt.boot_identity, BOOT_IDENTITY)
        self.assertTrue(receipt.is_fenced)

    def test_serve_refuses_when_this_host_cannot_be_identified(self) -> None:
        self._controller(host=None)
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            with mock.patch.object(ENTRY.os, "execv") as execv:
                with self.assertRaisesRegex(OWNER.EntryError, "host or boot identity"):
                    ENTRY.run_serve(_serve_argv(app, data, OWN_TOKEN))
            execv.assert_not_called()
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())

    def test_foreign_host_receipt_is_never_probed_killed_or_removed(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data, host_identity=OTHER_HOST_IDENTITY)
            for label, call in (
                ("stop", lambda: OWNER.run_stop_owned(data, OWN_TOKEN)),
                ("probe", lambda: ENTRY.run_probe(app, data, OWN_TOKEN)),
                ("serve", lambda: ENTRY.run_serve(_serve_argv(app, data, OWN_TOKEN))),
            ):
                with self.subTest(command=label):
                    with self.assertRaisesRegex(OWNER.EntryError, "another host"):
                        call()
            self.assertEqual(controller.liveness_queries, [])
            self.assertEqual(controller.terminated, [])
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)

    def test_prior_boot_receipt_is_reclaimed_without_reading_proc(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data, boot_identity=OTHER_BOOT_IDENTITY)
            OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.liveness_queries, [])
        self.assertEqual(controller.terminated, [])

    def test_legacy_receipt_refuses_with_named_operator_recovery(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data, legacy=True)
            for label, call in (
                ("stop", lambda: OWNER.run_stop_owned(data, OWN_TOKEN)),
                ("probe", lambda: ENTRY.run_probe(app, data, OWN_TOKEN)),
                ("serve", lambda: ENTRY.run_serve(_serve_argv(app, data, OWN_TOKEN))),
            ):
                with self.subTest(command=label):
                    with self.assertRaises(OWNER.EntryError) as caught:
                        call()
                    message = str(caught.exception)
                    self.assertIn("REMOTE_LOCK_OWNED", message)
                    self.assertIn(OWNER.OWNER_FILENAME, message)
                    self.assertIn("confirm no remote owner", message)
            self.assertEqual(controller.liveness_queries, [])
            self.assertEqual(controller.terminated, [])
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)

    def test_legacy_receipt_is_readable_and_reports_itself_unfenced(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data, legacy=True)
            receipt = OWNER.read_owner_receipt(data)
        assert receipt is not None
        self.assertEqual(set(OWNER.OWNER_KEYS) | {"host_identity", "boot_identity"},
                         set(OWNER.FENCED_OWNER_KEYS))
        self.assertIsNone(receipt.host_identity)
        self.assertIsNone(receipt.boot_identity)
        self.assertFalse(receipt.is_fenced)
        self.assertEqual(receipt.pid, 4242)
        self.assertEqual(OWNER.classify_owner_fencing(receipt, controller), "unfenced")
        self.assertEqual(OWNER.classify_owner(receipt, controller), "unfenced")

    def test_a_host_that_cannot_identify_itself_interprets_nothing(self) -> None:
        controller = self._controller(host=None)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.liveness_queries, [])
        self.assertEqual(controller.terminated, [])

    def test_an_unfenced_receipt_can_never_be_written(self) -> None:
        unfenced = OWNER.OwnerReceipt(
            workspace_id=WORKSPACE_ID,
            data_dir_digest="f" * 64,
            app_dir_digest="a" * 64,
            pid=7,
            start_identity="start-1",
            release_id="1.0.7",
            token_hash=token_hash(OWN_TOKEN),
        )
        with self.assertRaisesRegex(OWNER.EntryError, "missing host or boot identity"):
            OWNER.encode_owner_receipt(unfenced)

    def test_identity_digests_are_domain_and_source_separated(self) -> None:
        host = OWNER._identity_digest(OWNER.HOST_IDENTITY_DOMAIN, "machine-id", "abc")
        boot = OWNER._identity_digest(OWNER.BOOT_IDENTITY_DOMAIN, "machine-id", "abc")
        btime = OWNER._identity_digest(OWNER.BOOT_IDENTITY_DOMAIN, "btime", "abc")
        self.assertNotEqual(host, boot)
        self.assertNotEqual(boot, btime)
        for digest in (host, boot, btime):
            self.assertEqual(len(digest), OWNER.SHA256_HEX_LENGTH)

    def test_this_host_identity_is_a_stable_digest_or_absent(self) -> None:
        first = OWNER.local_host_identity()
        self.assertEqual(first, OWNER.local_host_identity())
        if first is not None:
            self.assertEqual(len(first), OWNER.SHA256_HEX_LENGTH)
            self.assertTrue(all(character in "0123456789abcdef" for character in first))
        boot = OWNER.local_boot_identity()
        self.assertEqual(boot, OWNER.local_boot_identity())
        if sys.platform.startswith("linux"):
            self.assertIsNotNone(boot)

    def test_a_host_without_a_machine_id_refuses_to_name_itself(self) -> None:
        """A nodename is configuration two hosts can share, so it proves nothing."""

        with tempfile.TemporaryDirectory() as directory:
            absent = (str(Path(directory) / "no-machine-id"),)
            with mock.patch.object(OWNER, "MACHINE_ID_PATHS", absent):
                self.assertIsNone(OWNER.local_host_identity())
        self.assertEqual([], _code_literals_naming(SHELL / "remote_owner.py", "nodename"))
        self.assertEqual([], _attribute_calls_naming(SHELL / "remote_owner.py", "platform", "node"))

    def test_a_host_that_cannot_name_itself_neither_acquires_nor_stops(self) -> None:
        """The prior-boot reclaim is the strongest move, so it needs the strongest proof."""

        controller = self._controller(host=None)
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data, boot_identity=OTHER_BOOT_IDENTITY)
            for label, call in (
                ("stop", lambda: OWNER.run_stop_owned(data, OWN_TOKEN)),
                ("probe", lambda: ENTRY.run_probe(app, data, OWN_TOKEN)),
                ("serve", lambda: ENTRY.run_serve(_serve_argv(app, data, OWN_TOKEN))),
            ):
                with self.subTest(command=label):
                    with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                        call()
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.liveness_queries, [])


class RemoteOwnerStopConfirmationTest(unittest.TestCase):
    """stop-owned proves the exact owned process is gone before it removes."""

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeProcessController:
        controller = FakeProcessController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def test_removal_waits_for_the_exact_process_to_disappear(self) -> None:
        controller = self._controller(exit_after_polls=3)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            OWNER.run_stop_owned(
                data,
                OWN_TOKEN,
                OWNER.StopWait(5.0, 0.25, clock.monotonic, clock.sleep),
            )
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.terminated, [4242])
        self.assertEqual(clock.slept, [0.25, 0.25, 0.25])

    def test_timeout_retains_the_receipt_and_never_widens_the_signal(self) -> None:
        controller = self._controller(exit_after_polls=10**6)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data)
            with self.assertRaises(OWNER.EntryError) as caught:
                OWNER.run_stop_owned(
                    data,
                    OWN_TOKEN,
                    OWNER.StopWait(1.0, 0.25, clock.monotonic, clock.sleep),
                )
            message = str(caught.exception)
            self.assertIn("REMOTE_LOCK_OWNED", message)
            self.assertIn("did not exit", message)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        # One SIGTERM to one pid. No escalation, no second signal, no sweep.
        self.assertEqual(controller.terminated, [4242])

    def test_unreadable_liveness_during_the_wait_retains_the_receipt(self) -> None:
        controller = self._controller(unreadable_after_signal=True)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data)
            with self.assertRaisesRegex(OWNER.EntryError, "could not be confirmed"):
                OWNER.run_stop_owned(
                    data,
                    OWN_TOKEN,
                    OWNER.StopWait(5.0, 0.25, clock.monotonic, clock.sleep),
                )
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [4242])

    def test_a_receipt_replaced_during_the_wait_is_left_alone(self) -> None:
        # A writer that ignores the guard is outside what an advisory lock can
        # fence, so the byte comparison is what keeps its receipt here.
        replacement: dict[str, bytes] = {}

        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)

            def hand_over() -> None:
                replacement["bytes"] = _write_receipt(
                    data, pid=5151, start_identity="start-9", token_hash=token_hash(FOREIGN_TOKEN)
                )

            controller = self._controller(exit_after_polls=1)
            clock = FakeClock(on_sleep=hand_over)
            OWNER.run_stop_owned(
                data,
                OWN_TOKEN,
                OWNER.StopWait(5.0, 0.25, clock.monotonic, clock.sleep),
            )
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), replacement["bytes"])
        self.assertEqual(controller.terminated, [4242])

    def test_a_raw_replacement_written_mid_stop_survives_the_removal(self) -> None:
        # This session still holds the owner's token and its handle still names
        # the process it pinned, so stopping that process is right. What must
        # not happen is deleting the bytes somebody else published.
        replacement: dict[str, bytes] = {}

        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)

            def hand_over() -> None:
                replacement["bytes"] = _write_receipt(
                    data, pid=5151, start_identity="start-9", token_hash=token_hash(FOREIGN_TOKEN)
                )

            controller = self._controller(on_liveness=hand_over)
            OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), replacement["bytes"])
        self.assertEqual(controller.signalled, [(4242, "start-1")])

    def test_a_pid_reused_before_the_pin_is_refused_with_the_receipt_kept(self) -> None:
        # The receipt's pid now names an unrelated process. This session cannot
        # say whether its own owner exited or its pid was handed on, so it says
        # so and keeps the evidence instead of signalling or deleting.
        controller = self._controller(start="start-2")
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data, start_identity="start-1")
            with self.assertRaises(OWNER.EntryError) as caught:
                OWNER.run_stop_owned(data, OWN_TOKEN)
            message = str(caught.exception)
            self.assertIn("REMOTE_PROTOCOL_INVALID", message)
            self.assertIn("different start identity", message)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.signalled, [])
        self.assertEqual(controller.opened, [])

    def test_a_pid_reused_between_the_check_and_the_pin_is_never_signalled(self) -> None:
        # The exact window the review probed: the recorded process is still
        # there when it is classified and gone by the time a handle is asked
        # for. The pin refuses; nothing is signalled and nothing is removed.
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data)
            controller = self._controller()
            controller.on_liveness = lambda: setattr(controller, "start", "start-2")
            with self.assertRaisesRegex(OWNER.EntryError, "recorded start identity"):
                OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.signalled, [])
        self.assertEqual(controller.opened, [(4242, "start-1")])

    def test_a_pid_reused_after_the_pin_cannot_redirect_the_signal(self) -> None:
        # Once the handle is bound, the pid number stops mattering: the signal
        # and the wait both follow the handle, so a reuse afterwards can
        # neither receive the signal nor be mistaken for the owner exiting.
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)
            controller = self._controller(exit_after_polls=1)
            controller.on_open = lambda: setattr(controller, "start", "start-2")
            clock = FakeClock()
            OWNER.run_stop_owned(
                data,
                OWN_TOKEN,
                OWNER.StopWait(5.0, 0.25, clock.monotonic, clock.sleep),
            )
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.signalled, [(4242, "start-1")])
        self.assertEqual([handle.closes for handle in controller.handles], [1])

    def test_a_wrong_token_never_removes_even_a_finished_owner(self) -> None:
        # Clearing a finished owner so a new session can start is acquisition's
        # job. A token-scoped stop that cannot prove it owns the receipt
        # deletes nothing, whatever state that receipt's process is in.
        for label, observed in (("exited", "exited"), ("reused pid", "live")):
            with self.subTest(owner=label):
                controller = self._controller(observed=observed, start="start-2")
                with tempfile.TemporaryDirectory() as directory:
                    _app, data = _write_probe_fixture(Path(directory))
                    original = _write_receipt(data, start_identity="start-1")
                    with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
                        OWNER.run_stop_owned(data, FOREIGN_TOKEN)
                    self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
                self.assertEqual(controller.signalled, [])
                # A refused token learns nothing about the owner's process.
                self.assertEqual(controller.liveness_queries, [])

    def test_acquisition_still_reclaims_the_receipt_a_stop_refuses(self) -> None:
        # The two policies are deliberately different, and both are exercised
        # on the same receipt so the split cannot rot into an accident.
        controller = self._controller(start="start-2")
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data, start_identity="start-1")
            with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_PROTOCOL_INVALID"):
                OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())
            ENTRY.run_probe(app, data)
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.signalled, [])

    def test_a_dead_receipt_replaced_before_removal_is_left_alone(self) -> None:
        replacement: dict[str, bytes] = {}

        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            _write_receipt(data)

            def hand_over() -> None:
                replacement["bytes"] = _write_receipt(
                    data, pid=5151, start_identity="start-9", token_hash=token_hash(FOREIGN_TOKEN)
                )

            controller = self._controller(observed="exited", on_liveness=hand_over)
            OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), replacement["bytes"])
        self.assertEqual(controller.terminated, [])

    def test_the_receipt_never_touches_the_real_writer_lease(self) -> None:
        lock_bytes = b"real writer lease, owned by the store\n"
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            lock = data / ".workstack.lock"
            lock.write_bytes(lock_bytes)
            _write_receipt(data)
            OWNER.run_stop_owned(data, OWN_TOKEN)
            with mock.patch.object(ENTRY.os, "execv"):
                ENTRY.run_serve(_serve_argv(app, data, OWN_TOKEN))
            ENTRY.run_probe(app, data, OWN_TOKEN)
            self.assertEqual(lock.read_bytes(), lock_bytes)
        # Nothing in the module's code names the lease either, so the receipt
        # can neither steal it nor unlink it; prose about it is allowed.
        for module in _RECEIPT_MODULES:
            self.assertEqual([], _code_literals_naming(SHELL / module, ".workstack.lock"))
        # The guard is its own inode and says so in its own name.
        self.assertNotEqual(GUARD.GUARD_FILENAME, ".workstack.lock")
        self.assertEqual(controller.terminated, [4242])

    def test_no_signal_is_ever_addressed_to_a_bare_pid(self) -> None:
        """os.kill(pid) after a checked pid is the defect; it may not come back."""

        for module in _RECEIPT_MODULES:
            with self.subTest(module=module):
                self.assertEqual([], _attribute_calls_naming(SHELL / module, "os", "kill"))
        self.assertTrue(hasattr(HANDLE, "open_owned_process"))

    def test_poll_error_before_signal_is_unknown_and_keeps_the_receipt(self) -> None:
        controller = self._controller(pre_signal_exit=None)
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data)
            with self.assertRaises(OWNER.EntryError) as caught:
                OWNER.run_stop_owned(data, OWN_TOKEN)
            self.assertIn("REMOTE_PROTOCOL_INVALID", str(caught.exception))
            self.assertIn("could not be read through its handle", str(caught.exception))
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])
        self.assertEqual([handle.closes for handle in controller.handles], [1])

    def test_a_permission_denied_signal_is_a_protocol_error_and_keeps_the_receipt(self) -> None:
        controller = self._controller(
            terminate_error="this session cannot signal the owned process"
        )
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _write_probe_fixture(Path(directory))
            original = _write_receipt(data)
            stderr = StringIO()
            with mock.patch.object(ENTRY.sys, "stderr", stderr):
                code = ENTRY.main(
                    ["stop-owned", "--data-dir", str(data), "--session-token", OWN_TOKEN]
                )
            # stop-owned now emits its structured outcome and returns the
            # status of that outcome's family. A handle that could not be
            # signalled is unconfirmed, not the flat error 1.0.13 returned for
            # every condition alike; the payload is only readable when the
            # status agrees with it. The operator diagnostic below is
            # unchanged, and nothing about the refusal is weakened.
            self.assertEqual(code, EXIT_UNCONFIRMED)
            message = stderr.getvalue()
            self.assertIn("REMOTE_PROTOCOL_INVALID", message)
            self.assertIn("cannot signal the owned process", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn("ProcessHandleUnavailable", message)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])
        self.assertEqual([handle.closes for handle in controller.handles], [1])


def _code_literals_naming(path: Path, needle: str) -> list[str]:
    """String literals in real code, ignoring module/class/function docstrings."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    documented = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in documented
        and needle in node.value
    ]


def _attribute_calls_naming(path: Path, module: str, attribute: str) -> list[str]:
    """Every ``module.attribute`` reference in one source file."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{module}.{attribute}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == module
    ]


def _serve_argv(app: Path, data: Path, token: str, *, exit_with_parent: bool = False):
    argv = [
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
        token,
    ]
    if exit_with_parent:
        argv.append("--exit-with-parent")
    return ENTRY.parse_remote_entry_argv(argv)


def _csh_family_shell() -> str | None:
    override = os.environ.get("WS_OWNER_CSH")
    candidates = (override, *CSH_CANDIDATES) if override else CSH_CANDIDATES
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK) and Path(candidate).is_file():
            return candidate
    return None


# The stand-in for the per-session sshd child: it starts the login shell exactly
# as sshd does and then waits to be killed abruptly, as a dying tunnel kills it.
_SESSION_SRC = """
import subprocess, sys, time
child = subprocess.Popen([sys.argv[1], "-c", sys.argv[2]])
time.sleep(120)
"""

# Stands in for run_work_stack.py.  It runs the product's own lifetime binding
# verbatim rather than reimplementing PR_SET_PDEATHSIG, so the regression
# exercises the real function; only the HTTP serve loop after it is a sleep.
_RUNNER_TEMPLATE = """
import os, sys, time
from pathlib import Path

{lifetime}

assert "--exit-with-parent" in sys.argv, sys.argv
marker = Path({marker!r})
{call}()
marker.with_suffix(".pid").write_text("%d %d\\n" % (os.getpid(), os.getppid()))
try:
    for _ in range(1200):
        time.sleep(0.1)
except KeyboardInterrupt:
    raise SystemExit(0)
marker.write_text("survived\\n")
"""


def _product_lifetime_binding() -> str:
    """The product's PR_SET_PDEATHSIG function, taken verbatim from cli.py.

    It is lifted by source rather than imported because importing
    ``workstack.cli`` pulls the whole product's pinned runtime dependencies,
    which are not installed here.  Lifting keeps the code under test the real
    code, and a rename or removal fails this test loudly.
    """

    source = (ROOT / "workstack" / "cli.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == LIFETIME_FUNCTION:
            segment = ast.get_source_segment(source, node)
            assert segment
            return segment
    raise AssertionError(f"{LIFETIME_FUNCTION} is gone from workstack/cli.py")


@unittest.skipUnless(sys.platform.startswith("linux"), "process ancestry needs Linux")
class RemoteOwnerCshParentLossTest(unittest.TestCase):
    """Regression for the reported /bin/csh login shell, on real processes.

    Every process here is started and reaped by this test under a synthetic
    HOME.  No sshd, no network, no company host: the session parent is a local
    stand-in that is killed abruptly, which is what a dying tunnel looks like
    to the remote side.

    Boundaries: the product's serve token grammar, `remote_entry.py serve`, and
    `workstack.cli._bind_linux_process_lifetime_to_parent` are the real code.
    The HTTP serve loop after the binding is replaced by a sleep, because parent
    loss is decided before the socket exists.
    """

    def setUp(self) -> None:
        shell = _csh_family_shell()
        if shell is None:
            self.skipTest("no csh-family shell available; see WS_OWNER_CSH")
        self.shell = shell
        self.root = Path(tempfile.mkdtemp(prefix="ws-owner-csh-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()

    def _fixture(self, marker: Path) -> tuple[Path, Path]:
        app = self.root / "app"
        data = self.root / "data"
        shell_dir = app / "desktop" / "python-webview-shell"
        shell_dir.mkdir(parents=True)
        (app / "workstack").mkdir(parents=True)
        data.mkdir()
        for name in _RECEIPT_MODULES + ("remote_command_contract.py",):
            shutil.copyfile(SHELL / name, shell_dir / name)
        (app / "workstack" / "__init__.py").write_text(
            '__version__ = "1.0.7"\nREMOTE_PROTOCOL_VERSION = 1\n', encoding="utf-8"
        )
        (app / "run_work_stack.py").write_text(
            _RUNNER_TEMPLATE.format(
                lifetime=_product_lifetime_binding(), marker=str(marker), call=LIFETIME_FUNCTION
            ),
            encoding="utf-8",
        )
        (data / "workspace.json").write_text(
            json.dumps({"id": WORKSPACE_ID, "name": "csh", "version": 2}), encoding="utf-8"
        )
        (data / "store-meta.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        return app, data

    def _serve_command(self, app: Path, data: Path) -> str:
        return join_serve_command(
            remote_python=sys.executable,
            remote_app_dir=str(app),
            remote_data_dir=str(data),
            remote_port=8765,
            local_forward_port=28765,
            session_token=OWN_TOKEN,
        )

    def _run_case(self, command: str) -> dict[str, object]:
        marker = self.root / "server.marker"
        pid_file = marker.with_suffix(".pid")
        for path in (marker, pid_file):
            if path.exists():
                path.unlink()
        session_src = self.root / "session.py"
        session_src.write_text(_SESSION_SRC, encoding="utf-8")
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment.pop("PYTHONPATH", None)
        session = subprocess.Popen(
            [sys.executable, str(session_src), self.shell, command],
            env=environment,
            cwd=str(self.root),
            stdin=subprocess.DEVNULL,
        )
        self.addCleanup(_reap, session.pid)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "the server never reported its pid")
        server_pid, server_ppid = (int(value) for value in pid_file.read_text().split())
        self.addCleanup(_reap, server_pid)
        os.kill(session.pid, signal.SIGKILL)
        session.wait(timeout=10)
        survived = _alive_after(server_pid, seconds=5.0)
        return {
            "session_pid": session.pid,
            "server_pid": server_pid,
            "shell_stayed_between": server_ppid != session.pid,
            "server_survived_parent_loss": survived,
        }

    def test_exec_prefixed_serve_dies_with_its_session_under_csh(self) -> None:
        marker = self.root / "server.marker"
        app, data = self._fixture(marker)
        command = self._serve_command(app, data)
        self.assertTrue(command.startswith("exec /"), command[:40])
        result = self._run_case(command)
        self.assertFalse(result["shell_stayed_between"], result)
        self.assertFalse(result["server_survived_parent_loss"], result)
        self.assertFalse(marker.exists(), "the server outlived its session")
        # The receipt the real serve wrote on this real host carries this
        # host's and this boot's identity, not a placeholder.
        receipt = OWNER.read_owner_receipt(data)
        assert receipt is not None
        self.assertTrue(receipt.is_fenced)
        self.assertEqual(receipt.host_identity, OWNER.local_host_identity())
        self.assertEqual(receipt.boot_identity, OWNER.local_boot_identity())
        self.assertEqual(receipt.pid, result["server_pid"])

    def test_without_exec_the_csh_fork_strands_the_server(self) -> None:
        marker = self.root / "server.marker"
        app, data = self._fixture(marker)
        command = self._serve_command(app, data)
        self.assertTrue(command.startswith("exec "))
        # The pre-fix shape: csh keeps a fork between the session and the server,
        # so PR_SET_PDEATHSIG watches the shell instead of the session.
        forked = command[len("exec ") :] + " ; :"
        result = self._run_case(forked)
        self.assertTrue(result["shell_stayed_between"], result)
        self.assertTrue(result["server_survived_parent_loss"], result)


class PidfdPollMaskTest(unittest.TestCase):
    """Invalid poll masks are unknown; only POLLIN is a confirmed pidfd exit."""

    def test_only_pollin_is_confirmed_exit(self) -> None:
        fd = 7
        self.assertFalse(HANDLE.interpret_pidfd_poll(fd, []))
        self.assertTrue(HANDLE.interpret_pidfd_poll(fd, [(fd, HANDLE.POLLIN)]))
        self.assertTrue(
            HANDLE.interpret_pidfd_poll(fd, [(fd, HANDLE.POLLIN | HANDLE.POLLHUP)])
        )
        for mask in (
            HANDLE.POLLERR,
            HANDLE.POLLNVAL,
            HANDLE.POLLHUP,
            HANDLE.POLLIN | HANDLE.POLLERR,
            HANDLE.POLLIN | HANDLE.POLLNVAL,
            # POLLIN beside a bit that is not an error bit: the old interpreter
            # tested for POLLIN and accepted every one of these as an exit.
            HANDLE.POLLIN | HANDLE.POLLPRI,
            HANDLE.POLLIN | HANDLE.POLLPRI | HANDLE.POLLHUP,
            HANDLE.POLLIN | 0x0400,
            HANDLE.POLLIN | HANDLE.POLLHUP | 0x2000,
        ):
            with self.subTest(mask=mask):
                self.assertIsNone(HANDLE.interpret_pidfd_poll(fd, [(fd, mask)]))
        self.assertIsNone(HANDLE.interpret_pidfd_poll(fd, [(8, HANDLE.POLLIN)]))
        self.assertIsNone(
            HANDLE.interpret_pidfd_poll(
                fd, [(fd, HANDLE.POLLIN), (fd, HANDLE.POLLHUP)]
            )
        )

    def test_the_accepted_masks_are_exactly_the_two_documented_ones(self) -> None:
        self.assertEqual(HANDLE.PIDFD_EXIT_MASKS, (HANDLE.POLLIN, HANDLE.POLLIN | HANDLE.POLLHUP))

    def test_poller_creation_and_register_failure_are_unknown(self) -> None:
        handle = HANDLE.PidfdOwnedProcess(7, 4242)

        class BrokenRegister:
            def register(self, *_args: object) -> None:
                raise OSError("register failed")

        class ErrorPoller:
            def register(self, *_args: object) -> None:
                return None

            def poll(self, _timeout: object) -> list[tuple[int, int]]:
                return [(7, HANDLE.POLLERR)]

        try:
            with mock.patch.object(
                HANDLE.select, "poll", side_effect=OSError("no poller"), create=True
            ):
                self.assertIsNone(handle.has_exited())
            with mock.patch.object(
                HANDLE.select, "poll", return_value=BrokenRegister(), create=True
            ):
                self.assertIsNone(handle.has_exited())
            with mock.patch.object(
                HANDLE.select, "poll", return_value=ErrorPoller(), create=True
            ):
                self.assertIsNone(handle.has_exited())
        finally:
            handle._closed = True


class _MaskPoller:
    """One poller that reports exactly the mask a test wants interpreted."""

    def __init__(self, fd: int, mask: int) -> None:
        self._fd = fd
        self._mask = mask

    def register(self, *_args: object) -> None:
        return None

    def poll(self, _timeout: object) -> list[tuple[int, int]]:
        return [(self._fd, self._mask)]


class _RealHandleController(FakeProcessController):
    """Hands the stop the product's own pidfd handle, not a fake one."""

    def __init__(self, fd: int, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.fd = fd

    def open_owned_process(self, pid: int, start_identity: str):
        self.opened.append((pid, start_identity))
        return HANDLE.PidfdOwnedProcess(self.fd, pid)


class PidfdPollMaskStopPathTest(unittest.TestCase):
    """The public stop, over the real handle, decided only by the poll mask.

    The old interpreter accepted any mask carrying POLLIN, so an unexpected bit
    beside it made ``run_stop_owned`` report success and delete the ownership
    receipt for a process it had neither signalled nor seen exit.
    """

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _stop_under_mask(self, mask: int):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        _app, data = _write_probe_fixture(Path(directory.name))
        encoded = _write_receipt(data)
        # A real descriptor, so the handle's single close is a real close.
        fd = os.open(os.devnull, os.O_RDONLY)
        controller = _RealHandleController(fd)
        OWNER.set_process_controller(controller)
        signals: list[tuple[object, ...]] = []

        def record_signal(*args: object) -> None:
            signals.append(args)

        error: OWNER.EntryError | None = None
        with mock.patch.object(
            HANDLE.select, "poll", return_value=_MaskPoller(fd, mask), create=True
        ), mock.patch.object(
            HANDLE.signal, "pidfd_send_signal", new=record_signal, create=True
        ):
            try:
                OWNER.run_stop_owned(data, OWN_TOKEN)
            except OWNER.EntryError as caught:
                error = caught
        return {
            "error": error,
            "signals": signals,
            "receipt": (data / OWNER.OWNER_FILENAME),
            "encoded": encoded,
        }

    def test_an_unknown_bit_beside_pollin_keeps_the_receipt_and_fails(self) -> None:
        for mask in (
            HANDLE.POLLIN | HANDLE.POLLPRI,
            HANDLE.POLLIN | HANDLE.POLLHUP | HANDLE.POLLPRI,
            HANDLE.POLLIN | 0x0400,
        ):
            with self.subTest(mask=mask):
                result = self._stop_under_mask(mask)
                error = result["error"]
                self.assertIsNotNone(error, "the stop reported success on an unknown mask")
                self.assertIn("REMOTE_PROTOCOL_INVALID", str(error))
                self.assertIn("liveness could not be read", str(error))
                self.assertEqual(result["signals"], [])
                self.assertEqual(result["receipt"].read_bytes(), result["encoded"])

    def test_an_exact_exit_mask_still_completes_the_stop(self) -> None:
        for mask in (HANDLE.POLLIN, HANDLE.POLLIN | HANDLE.POLLHUP):
            with self.subTest(mask=mask):
                result = self._stop_under_mask(mask)
                self.assertIsNone(result["error"])
                # An already-exited owner is never signalled, only cleared.
                self.assertEqual(result["signals"], [])
                self.assertFalse(result["receipt"].exists())


@unittest.skipUnless(sys.platform.startswith("linux"), "pidfd signalling needs Linux")
class RemoteOwnerPidfdStopTest(unittest.TestCase):
    """The real stop path against a real owned process, with no fake anywhere.

    Every process here is started and reaped by this test.  Nothing else on the
    host is signalled: the only pid ever handed to the product is one this test
    forked, and the receipt is written into a temporary directory this test owns.
    """

    def setUp(self) -> None:
        if not HANDLE.pidfd_signalling_available():
            self.skipTest("this interpreter has no pidfd signalling")
        if OWNER.local_host_identity() is None:
            self.skipTest("no machine-id on this host, so no receipt can be fenced")
        self.root = Path(tempfile.mkdtemp(prefix="ws-owner-pidfd-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = self.root / "data"
        self.data.mkdir()

    def _owned_child(self) -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
        )
        self.addCleanup(_reap_child, child)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if HANDLE.read_start_identity(child.pid) is not None:
                return child
            time.sleep(0.02)
        self.fail("the owned child never appeared in /proc")

    def _receipt_for(self, child: subprocess.Popen, start: str) -> bytes:
        receipt = OWNER.OwnerReceipt(
            workspace_id=WORKSPACE_ID,
            data_dir_digest=OWNER.local_path_digest(self.data),
            app_dir_digest="a" * 64,
            pid=child.pid,
            start_identity=start,
            release_id="1.0.7",
            token_hash=token_hash(OWN_TOKEN),
            host_identity=OWNER.local_host_identity(),
            boot_identity=OWNER.local_boot_identity(),
        )
        encoded = OWNER.encode_owner_receipt(receipt)
        (self.data / OWNER.OWNER_FILENAME).write_bytes(encoded)
        return encoded

    def test_the_owned_child_is_stopped_through_its_own_pidfd(self) -> None:
        child = self._owned_child()
        start = HANDLE.read_start_identity(child.pid)
        assert start is not None
        self._receipt_for(child, start)
        OWNER.run_stop_owned(self.data, OWN_TOKEN, OWNER.StopWait(20.0, 0.05))
        self.assertFalse((self.data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)

    def test_a_wrong_start_identity_leaves_the_real_child_running(self) -> None:
        """The shape a reused pid takes: same pid, a start this session never saw."""

        child = self._owned_child()
        original = self._receipt_for(child, "1")
        with self.assertRaises(OWNER.EntryError) as caught:
            OWNER.run_stop_owned(self.data, OWN_TOKEN, OWNER.StopWait(5.0, 0.05))
        self.assertIn("REMOTE_PROTOCOL_INVALID", str(caught.exception))
        self.assertEqual((self.data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertIsNone(child.poll())

    def test_a_pidfd_handle_reports_its_own_process_exiting(self) -> None:
        child = self._owned_child()
        start = HANDLE.read_start_identity(child.pid)
        assert start is not None
        handle = HANDLE.open_owned_process(child.pid, start)
        assert handle is not None
        try:
            self.assertFalse(handle.has_exited())
            handle.send_terminate()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not handle.has_exited():
                time.sleep(0.02)
            self.assertTrue(handle.has_exited())
        finally:
            handle.close()
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        # Closed once, and a second close is a no-op rather than a bad fd.
        handle.close()

    def test_an_unpinnable_pid_is_refused_rather_than_signalled(self) -> None:
        child = self._owned_child()
        start = HANDLE.read_start_identity(child.pid)
        assert start is not None
        with self.assertRaises(HANDLE.ProcessHandleUnavailable):
            HANDLE.open_owned_process(os.getpid(), start)
        child.kill()
        child.wait(timeout=10)
        # A reaped pid comes back as "nothing to signal" or as a refusal. It
        # never comes back as a handle to whatever now holds that number.
        try:
            self.assertIsNone(HANDLE.open_owned_process(child.pid, start))
        except HANDLE.ProcessHandleUnavailable:
            pass

    def test_an_invalid_pidfd_does_not_report_exit(self) -> None:
        child = self._owned_child()
        start = HANDLE.read_start_identity(child.pid)
        assert start is not None
        handle = HANDLE.open_owned_process(child.pid, start)
        assert handle is not None
        os.close(handle._fd)
        try:
            self.assertIsNone(handle.has_exited())
        finally:
            handle._closed = True
        self.assertIsNone(child.poll())


def _alive_after(pid: int, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.05)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _reap_child(child: subprocess.Popen) -> None:
    """Reap a child this test owns through its own handle, leaving nothing."""

    if child.poll() is not None:
        return
    child.kill()
    child.wait(timeout=10)


def _reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        return


if __name__ == "__main__":
    unittest.main()
