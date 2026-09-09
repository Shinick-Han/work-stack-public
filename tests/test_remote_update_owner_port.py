"""D's owner stop port: the original listener and the writer lease, separately.

The 1.0.8 incident is what happens when "a stop command was sent", "the owner
process exited", "the port it served is free" and "the writer lease is free"
are treated as one fact.  They are four, and three of them are observable on
the remote host.  These oracles hold that separation:

* the ORIGINAL listener is observed on the host that owns it, never through
  the desktop's forwarded port and never through a general reachability probe;
* the writer lease is proved by taking it through the supported lock API,
  never by the presence or absence of an owner receipt;
* that acquisition creates nothing, at any timing: a lock file that is absent,
  empty, replaced or unreadable leaves the pathname exactly as it was found,
  and a lease taken on a file that is not the one that was checked -- proved
  against the handle actually opened, not against the pathname -- is never a
  release;
* neither observation may create, migrate or mutate anything in the live SSOT;
* a missing token, a foreign host, a legacy unfenced receipt and a receipt
  that names another workspace stay four distinct, actionable answers, and
  none of them is ever softened into a success;
* an absent or refused stop never reports a zero exit.

Fixtures are real: real loopback listeners, real exclusive file leases taken
through ``workstack.file_lease``, real ``subprocess.CompletedProcess`` values,
and real subprocess runs of the checked-in observation helper.  No live SSH, no
company host, no NFS or csh claim.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
for _entry in (str(ROOT), str(SHELL)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import remote_entry as ENTRY  # noqa: E402
import remote_owner as OWNER  # noqa: E402
import remote_stop_result as STOP  # noqa: E402
import remote_update_flow_outcomes as OUTCOMES  # noqa: E402
import remote_update_owner_observation as OBS  # noqa: E402
import remote_update_owner_port as PORT  # noqa: E402
from remote_command_contract import RemoteCommandError, token_hash  # noqa: E402
from remote_update_flow_contract import (  # noqa: E402
    OBSERVATIONS,
    OWNER_FIELDS,
    OWNER_STATES,
    LostResponse,
    OwnerStopFacts,
    RemoteUpdateStage,
)
from ssot_connection import RemoteConnectionProfile  # noqa: E402
from workstack import file_lease as FILE_LEASE  # noqa: E402
from workstack.file_lease import StoreLockedError, _FileLease  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
OWN_TOKEN = "owner-observation-token-not-enforced-01"
OTHER_TOKEN = "owner-observation-token-not-enforced-02"
HOST_IDENTITY = "b" * 64
BOOT_IDENTITY = "c" * 64
OTHER_HOST_IDENTITY = "d" * 64
OWNED_PID = 5151
OWNED_START = "start-observation-1"
REMOTE_PYTHON = "/srv/workstack/venv/bin/python"
REMOTE_APP_DIR = "/srv/workstack/app"
# The observation helper runs from a location prepared for THIS release,
# never from the app being stopped -- which in the incident is 1.0.8.
OBSERVATION_APP_DIR = "/srv/workstack/observer"
REMOTE_DATA_DIR = "/srv/workstack/ssot"
SSH = "/usr/bin/ssh"


# --------------------------------------------------------------------------
# Fixtures


def _write_workspace(data: Path, workspace_id: str = WORKSPACE_ID) -> None:
    (data / OBS.WORKSPACE_FILE).write_text(
        json.dumps({"id": workspace_id, "name": "fixture"}), encoding="utf-8"
    )


def _write_receipt(data: Path, *, legacy: bool = False, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "data_dir_digest": OWNER.local_path_digest(data),
        "app_dir_digest": "a" * 64,
        "pid": OWNED_PID,
        "start_identity": OWNED_START,
        "release_id": "1.0.13",
        "token_hash": token_hash(OWN_TOKEN),
    }
    if not legacy:
        payload["host_identity"] = HOST_IDENTITY
        payload["boot_identity"] = BOOT_IDENTITY
    payload.update(overrides)
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    (data / OWNER.OWNER_FILENAME).write_bytes(encoded)
    return encoded


def _make_lock(data: Path) -> Path:
    """A real, released lock file, made exactly the way the store makes one."""

    lock = data / OBS.STORE_LEASE_FILE
    lease = _FileLease(lock)
    lease.acquire()
    lease.release()
    return lock


class HeldLease:
    """A real exclusive writer lease held for the body of a test."""

    def __init__(self, lock_path: Path) -> None:
        self._lease = _FileLease(lock_path)

    def __enter__(self) -> "HeldLease":
        self._lease.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lease.release()


@contextlib.contextmanager
def during_the_observed_interval(action, *, when="before_open"):
    """Force one REAL event into the observer's own acquisition window.

    Only the timing is arranged.  The lease, the platform lock, the unlink,
    the recreation and the inodes are all real; this fixture just guarantees
    the event lands inside the interval the observer is responsible for,
    instead of relying on a sleep to lose the race.

    ``before_open`` fires between the observer's one initial stat and its
    no-create open -- the window in which a pathname can be made to name a
    different file than the one that was checked.  ``before_lock`` fires after
    that open, before the platform lock: flock then contends on the original
    inode while the pathname already names a replacement.  ``after_lock``
    fires once the handle is open and locked, before the pathname is
    re-checked.
    """

    fired: list[bool] = []
    before_lock_fired: list[bool] = []

    class Raced(_FileLease):
        def acquire_existing(self) -> None:
            first = not fired
            if first:
                fired.append(True)
                if when == "before_open":
                    action()
            super().acquire_existing()
            if first and when == "after_lock":
                action()

        def _take_platform_lock(self, handle: object) -> None:
            if when == "before_lock" and not before_lock_fired:
                before_lock_fired.append(True)
                action()
            super()._take_platform_lock(handle)

    with mock.patch.object(FILE_LEASE, "_FileLease", Raced):
        yield


def _replace_lock_file(lock: Path) -> None:
    """A real unlink and a real, freshly made released lock file in its place."""

    lock.unlink()
    fresh = _FileLease(lock)
    fresh.acquire()
    fresh.release()


class Listener:
    """A real loopback listener, so the port answers for real."""

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((OBS.LOOPBACK_HOST, 0))
        self.socket.listen(1)
        self.port = int(self.socket.getsockname()[1])

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> "Listener":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _free_port() -> int:
    listener = Listener()
    port = listener.port
    listener.close()
    return port


class FakeController:
    """A scripted process controller: no real pid is ever touched."""

    def __init__(
        self,
        *,
        observation: str = "live",
        pidfd: bool | None = True,
        host: str | None = HOST_IDENTITY,
        boot: str | None = BOOT_IDENTITY,
    ) -> None:
        self.observation = observation
        self.pidfd = pidfd
        self.host = host
        self.boot = boot
        self.observe_calls: list[tuple[int, str]] = []
        self.opened: list[tuple[int, str]] = []

    def current_pid(self) -> int:
        return OWNED_PID

    def start_identity(self, pid: int) -> str | None:
        return OWNED_START if pid == OWNED_PID else None

    def observe(self, pid: int, start_identity: str) -> str:
        self.observe_calls.append((pid, start_identity))
        return self.observation

    def open_owned_process(self, pid: int, start_identity: str):
        self.opened.append((pid, start_identity))
        raise AssertionError("a read-only observation must never open a process")

    def pidfd_available(self) -> bool | None:
        return self.pidfd

    def host_identity(self) -> str | None:
        return self.host

    def boot_identity(self) -> str | None:
        return self.boot


def _refusing_open(pid: int, start_identity: str):
    """Exactly what a kernel that will not let this session signal that pid does."""

    raise OWNER.EntryError(
        "REMOTE_PROTOCOL_INVALID", "this session cannot signal the owned process"
    )


def _profile(**overrides: object) -> RemoteConnectionProfile:
    values: dict[str, object] = {
        "ssh_host_alias": "workstack-remote",
        "remote_app_dir": REMOTE_APP_DIR,
        "remote_data_dir": REMOTE_DATA_DIR,
        "local_forward_port": 18765,
        "workspace_id": WORKSPACE_ID,
        "remote_port": 8765,
        "remote_python": REMOTE_PYTHON,
    }
    values.update(overrides)
    return RemoteConnectionProfile(**values)  # type: ignore[arg-type]


def _completed(argv: list[str], returncode: int, stdout: bytes = b"") -> subprocess.CompletedProcess:
    """A real CompletedProcess, the shape the host seam actually hands back."""

    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")


class ScriptedRunner:
    """One injected command seam for both fixed argv shapes, as the host has.

    It records every argv it was given, so a test can prove which commands ran
    -- and, more importantly, which did not.
    """

    def __init__(
        self,
        *,
        stop: subprocess.CompletedProcess | None = None,
        observe: subprocess.CompletedProcess | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.stop = stop
        self.observe = observe
        self.stop_error = stop_error
        self.commands: list[list[str]] = []

    @property
    def stop_commands(self) -> list[list[str]]:
        return [argv for argv in self.commands if "stop-owned" in argv[-1]]

    @property
    def observe_commands(self) -> list[list[str]]:
        return [argv for argv in self.commands if OBS.OBSERVE_COMMAND in argv[-1]]

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(argv))
        if OBS.OBSERVE_COMMAND in argv[-1]:
            if self.observe is None:
                raise AssertionError("no observation was scripted for this test")
            return self.observe
        if self.stop_error is not None:
            raise self.stop_error
        if self.stop is None:
            raise AssertionError("no stop was scripted for this test")
        return self.stop


def _observation_stdout(**fields: object) -> bytes:
    return OBS.encode_owner_release(OBS.OwnerReleaseObservation(**fields))  # type: ignore[arg-type]


def _stop_stdout(**fields: object) -> bytes:
    return STOP.encode_stop_result(STOP.StopResult(**fields))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The wire contract


class ObservationContractTest(unittest.TestCase):
    """The bounded line the remote emits, read as an untrusted boundary."""

    def test_a_full_observation_survives_one_round_trip(self) -> None:
        original = OBS.OwnerReleaseObservation(
            binding=OBS.BINDING_BOUND,
            owner_state="dead",
            token_available=True,
            process_exit="verified",
            listener_release="verified",
            lease_release="failed",
            detail="fixture",
        )
        self.assertEqual(
            OBS.decode_owner_release(OBS.encode_owner_release(original)), original
        )

    def test_the_emitted_line_is_one_bounded_ascii_line(self) -> None:
        encoded = OBS.encode_owner_release(
            OBS.OwnerReleaseObservation(detail="x" * 500)
        )
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertLessEqual(len(encoded), OBS.MAX_RELEASE_BYTES)
        encoded.decode("ascii")

    def test_control_characters_never_survive_into_a_detail(self) -> None:
        decoded = OBS.decode_owner_release(
            OBS.encode_owner_release(
                OBS.OwnerReleaseObservation(detail="a\r\nb\x00<script>")
            )
        )
        assert decoded is not None
        self.assertNotIn("\r", decoded.detail)
        self.assertNotIn("\n", decoded.detail)
        self.assertNotIn("\x00", decoded.detail)

    def test_a_forged_value_normalizes_to_the_answer_that_claims_nothing(self) -> None:
        forged = json.dumps(
            {
                "schema_version": OBS.SCHEMA_VERSION,
                "binding": "definitely_fine",
                "owner_state": "gone_for_good",
                "token_available": "yes",
                "process_exit": "totally_verified",
                "listener_release": "released!",
                "lease_release": 1,
                "detail": "x",
            }
        ).encode("ascii") + b"\n"
        decoded = OBS.decode_owner_release(forged)
        assert decoded is not None
        self.assertEqual(decoded.binding, OBS.BINDING_UNKNOWN)
        self.assertEqual(decoded.owner_state, "unknown")
        self.assertIsNone(decoded.token_available)
        self.assertEqual(decoded.process_exit, "unknown")
        self.assertEqual(decoded.listener_release, "unknown")
        self.assertEqual(decoded.lease_release, "unknown")

    def test_nothing_readable_is_not_an_observation_that_said_unknown(self) -> None:
        for payload in (
            b"",
            b"not json\n",
            b'{"schema_version":"other/1"}\n',
            b'["a"]\n',
            None,
            object(),
            b"x" * (OBS.MAX_RELEASE_BYTES + 1) + b"\n",
        ):
            with self.subTest(payload=repr(payload)[:40]):
                self.assertIsNone(OBS.decode_owner_release(payload))

    def test_the_published_vocabulary_is_the_shared_snapshot_vocabulary(self) -> None:
        self.assertEqual(set(OBS.OWNER_STATE_VALUES), set(OWNER_STATES))
        self.assertEqual(set(OBS.EVIDENCE_VALUES), set(OBSERVATIONS))

    def test_the_two_workspace_answers_never_permit_an_attached_fact(self) -> None:
        self.assertNotIn(OBS.BINDING_WORKSPACE_MISMATCH, OBS.WORKSPACE_BOUND_BINDINGS)
        self.assertNotIn(OBS.BINDING_WORKSPACE_UNKNOWN, OBS.WORKSPACE_BOUND_BINDINGS)
        self.assertNotIn(OBS.BINDING_UNKNOWN, OBS.WORKSPACE_BOUND_BINDINGS)
        self.assertIn(OBS.BINDING_BOUND, OBS.WORKSPACE_BOUND_BINDINGS)


# --------------------------------------------------------------------------
# The writer lease, proved through the supported lock API


class WriterLeaseObservationTest(unittest.TestCase):
    """The lease is taken to be proved, and nothing is ever created to do it."""

    def test_a_free_lease_is_verified_by_actually_taking_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _make_lock(data)
            self.assertEqual(OBS.observe_writer_lease(data), "verified")

    def test_a_lease_another_writer_holds_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            with HeldLease(lock):
                self.assertEqual(OBS.observe_writer_lease(data), "failed")
            self.assertEqual(OBS.observe_writer_lease(data), "verified")

    def test_the_answer_comes_from_the_lock_and_never_from_a_receipt(self) -> None:
        """A held lease reads failed with no receipt; a free one reads verified with one."""

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            with HeldLease(lock):
                self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
                self.assertEqual(OBS.observe_writer_lease(data), "failed")
            _write_receipt(data)
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())
            self.assertEqual(OBS.observe_writer_lease(data), "verified")

    def test_a_missing_data_directory_creates_nothing_and_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "never-made"
            self.assertEqual(OBS.observe_writer_lease(data), "unknown")
            self.assertFalse(data.exists())

    def test_a_missing_lock_file_is_unknown_and_is_not_brought_into_being(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            lock = data / OBS.STORE_LEASE_FILE
            self.assertEqual(OBS.observe_writer_lease(data), "unknown")
            self.assertFalse(lock.exists())
            self.assertEqual(
                sorted(entry.name for entry in data.iterdir()), [OBS.WORKSPACE_FILE]
            )

    def test_an_empty_lock_file_is_unknown_and_its_sentinel_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = data / OBS.STORE_LEASE_FILE
            lock.write_bytes(b"")
            self.assertEqual(OBS.observe_writer_lease(data), "unknown")
            self.assertEqual(lock.stat().st_size, 0)

    def test_a_lock_file_that_vanishes_before_the_open_is_never_recreated(self) -> None:
        """A real removal inside the observed interval, answered without a write.

        This is the oracle the no-create acquisition exists for. The lock file
        is removed by a real ``unlink`` after the observer's initial stat and
        before its open -- an operator clearing a store by hand, mid-run. The
        answer must be unknown AND the pathname must still be absent
        afterwards: the observation is not allowed to make the thing it failed
        to find.
        """

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            with during_the_observed_interval(lock.unlink):
                self.assertEqual(OBS.observe_writer_lease(data), "unknown")
            self.assertFalse(lock.exists())
            self.assertEqual(list(data.iterdir()), [])

    def test_a_pathname_that_names_another_file_after_the_lock_is_never_verified(
        self,
    ) -> None:
        """The post-acquisition pathname check is live, not decorative.

        Only the second identity is fabricated here, so the assertion is about
        one thing: a pathname that stops naming the locked file while it is
        locked cannot answer verified. The real replacement is exercised
        separately below, where the platform allows it.
        """

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _make_lock(data)
            with mock.patch.object(
                OBS, "_file_identity", lambda path: ("a-different-device", "a-different-file")
            ):
                self.assertEqual(OBS.observe_writer_lease(data), "unknown")

    @unittest.skipIf(os.name == "nt", "needs POSIX unlink-while-open semantics")
    def test_a_replacement_while_a_writer_holds_the_original_is_never_verified(
        self,
    ) -> None:
        """The counterexample the pathname alone cannot catch.

        A real writer holds the lease on the original inode. The pathname is
        then really unlinked and really recreated inside the observed interval,
        so the observer's own acquisition succeeds -- on the free replacement --
        while the writer is still holding the store. Comparing only the
        pathname before and after would report a released lease here, because
        both stats name the replacement. Only the identity of the handle that
        was actually opened and locked reveals it, and it must answer unknown.
        """

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            writer = _FileLease(lock)
            writer.acquire()
            try:
                original = os.fstat(writer.file.fileno()).st_ino
                with during_the_observed_interval(lambda: _replace_lock_file(lock)):
                    self.assertEqual(OBS.observe_writer_lease(data), "unknown")
                # The replacement really happened, and the real writer really
                # never let go of the original inode.
                self.assertNotEqual(lock.stat().st_ino, original)
                self.assertFalse(writer.file.closed)
            finally:
                writer.release()

    @unittest.skipIf(os.name == "nt", "needs POSIX unlink-while-open semantics")
    def test_a_replacement_after_open_before_lock_with_a_held_writer_is_unknown(
        self,
    ) -> None:
        """Contention on the original inode is not failed if the pathname moved.

        A real writer holds the original lock. The observer opens that same
        inode; then, after open and before flock, the pathname is really
        unlinked and an ordinary writer creates a distinct, released
        replacement. flock contends with the held original, so
        ``acquire_existing`` raises ``StoreLockedError`` after closing its
        handle. The observation must still answer unknown: the pathname no
        longer names the inode that was checked. Only the timing is injected,
        by firing on the existing ``_take_platform_lock`` seam; the writer,
        the observer descriptors, the unlink/recreate and the inodes are real.
        """

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            writer = _FileLease(lock)
            writer.acquire()
            try:
                original = os.fstat(writer.file.fileno())
                with during_the_observed_interval(
                    lambda: _replace_lock_file(lock), when="before_lock"
                ):
                    self.assertEqual(OBS.observe_writer_lease(data), "unknown")
                replacement = lock.stat()
                self.assertNotEqual(replacement.st_ino, original.st_ino)
                self.assertEqual(lock.read_bytes(), b"\0")
                self.assertFalse(writer.file.closed)
            finally:
                writer.release()

    @unittest.skipIf(os.name == "nt", "needs POSIX unlink-while-open semantics")
    def test_a_real_replacement_after_the_lock_is_taken_is_never_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            with during_the_observed_interval(
                lambda: _replace_lock_file(lock), when="after_lock"
            ):
                self.assertEqual(OBS.observe_writer_lease(data), "unknown")

    def test_something_that_is_not_a_regular_file_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / OBS.STORE_LEASE_FILE).mkdir()
            self.assertEqual(OBS.observe_writer_lease(data), "unknown")
            self.assertTrue((data / OBS.STORE_LEASE_FILE).is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores the mode")
    def test_an_unreadable_lock_file_is_unknown_and_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            before = lock.stat()
            lock.chmod(0o000)
            try:
                self.assertEqual(OBS.observe_writer_lease(data), "unknown")
                after = lock.stat()
                self.assertEqual(after.st_ino, before.st_ino)
                self.assertEqual(after.st_size, before.st_size)
            finally:
                lock.chmod(0o600)

    def test_taking_the_lease_leaves_the_lock_file_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock = _make_lock(data)
            before = lock.read_bytes()
            self.assertEqual(OBS.observe_writer_lease(data), "verified")
            self.assertEqual(lock.read_bytes(), before)


# --------------------------------------------------------------------------
# The no-create acquisition on the shared lock primitive


class NoCreateLeaseAcquisitionTest(unittest.TestCase):
    """``_FileLease.acquire_existing``: the same lock, and nothing made.

    These are focused tests of the seam this observation depends on. They also
    hold the original writer acquisition to its unchanged behaviour, so the
    no-create variant cannot be paid for by weakening the one the store uses.
    """

    def test_it_takes_the_same_lock_and_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = _make_lock(Path(directory))
            lease = _FileLease(lock)
            lease.acquire_existing()
            self.assertIsNotNone(lease.file)
            lease.release()
            self.assertIsNone(lease.file)

    def test_it_is_refused_while_a_writer_holds_the_ordinary_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = _make_lock(Path(directory))
            with HeldLease(lock):
                with self.assertRaises(StoreLockedError):
                    _FileLease(lock).acquire_existing()

    def test_it_refuses_the_ordinary_lease_while_it_holds(self) -> None:
        """The interlock runs both ways: one lock, not two protocols."""

        with tempfile.TemporaryDirectory() as directory:
            lock = _make_lock(Path(directory))
            observer = _FileLease(lock)
            observer.acquire_existing()
            try:
                with self.assertRaises(StoreLockedError):
                    _FileLease(lock).acquire()
            finally:
                observer.release()
            writer = _FileLease(lock)
            writer.acquire()
            writer.release()

    def test_an_absent_lock_file_raises_and_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / OBS.STORE_LEASE_FILE
            with self.assertRaises(FileNotFoundError):
                _FileLease(lock).acquire_existing()
            self.assertFalse(lock.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_an_absent_parent_directory_is_not_made(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "never-made"
            with self.assertRaises(OSError):
                _FileLease(parent / OBS.STORE_LEASE_FILE).acquire_existing()
            self.assertFalse(parent.exists())

    def test_it_writes_no_sentinel_into_an_empty_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / OBS.STORE_LEASE_FILE
            lock.write_bytes(b"")
            lease = _FileLease(lock)
            lease.acquire_existing()
            lease.release()
            self.assertEqual(lock.stat().st_size, 0)

    def test_it_leaves_an_existing_lock_file_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = _make_lock(Path(directory))
            before = lock.read_bytes()
            lease = _FileLease(lock)
            lease.acquire_existing()
            lease.release()
            self.assertEqual(lock.read_bytes(), before)

    def test_the_ordinary_acquisition_still_creates_and_initialises(self) -> None:
        """Regression on the writer path the store actually uses."""

        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "made" / OBS.STORE_LEASE_FILE
            lease = _FileLease(lock)
            lease.acquire()
            try:
                self.assertTrue(lock.parent.is_dir())
                self.assertTrue(lock.is_file())
                self.assertEqual(lock.stat().st_size, 1)
            finally:
                lease.release()
            self.assertEqual(lock.read_bytes(), b"\0")


# --------------------------------------------------------------------------
# The ORIGINAL listener, observed on the host that owns it


class OriginalListenerObservationTest(unittest.TestCase):
    def test_a_real_listener_still_serving_reads_failed(self) -> None:
        with Listener() as listener:
            self.assertEqual(OBS.observe_original_listener(listener.port), "failed")

    def test_the_same_port_after_the_listener_goes_reads_verified(self) -> None:
        listener = Listener()
        port = listener.port
        self.assertEqual(OBS.observe_original_listener(port), "failed")
        listener.close()
        self.assertEqual(OBS.observe_original_listener(port), "verified")

    def test_a_port_that_is_not_a_port_claims_nothing(self) -> None:
        for value in (None, 0, -1, 70000, True, "8765", 8765.0):
            with self.subTest(value=value):
                self.assertEqual(OBS.observe_original_listener(value), "unknown")


# --------------------------------------------------------------------------
# Binding: what an observation is allowed to be about


class ObservationBindingTest(unittest.TestCase):
    """Selected host, canonical SSOT workspace and session token, in that order."""

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeController:
        controller = FakeController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def _observe(
        self, data: Path, port: object, *, token: str = OWN_TOKEN, workspace: str = WORKSPACE_ID
    ) -> OBS.OwnerReleaseObservation:
        return OBS.observe_owner_release(
            data_dir=data,
            listener_port=port,
            workspace_id=workspace,
            session_token=token,
        )

    def test_an_unreadable_workspace_measures_nothing_at_all(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _make_lock(data)
            observed = self._observe(data, listener.port)
        self.assertEqual(observed.binding, OBS.BINDING_WORKSPACE_UNKNOWN)
        # The listener was really answering and the lease was really free; the
        # gate is what kept both out of the report.
        self.assertEqual(observed.listener_release, "unknown")
        self.assertEqual(observed.lease_release, "unknown")

    def test_another_workspace_in_the_same_directory_measures_nothing(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data, OTHER_WORKSPACE_ID)
            _make_lock(data)
            observed = self._observe(data, listener.port)
        self.assertEqual(observed.binding, OBS.BINDING_WORKSPACE_MISMATCH)
        self.assertEqual(observed.listener_release, "unknown")
        self.assertEqual(observed.lease_release, "unknown")

    def test_a_receipt_that_names_another_workspace_measures_nothing(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data, workspace_id=OTHER_WORKSPACE_ID)
            _make_lock(data)
            observed = self._observe(data, listener.port)
        self.assertEqual(observed.binding, OBS.BINDING_WORKSPACE_MISMATCH)
        self.assertEqual(observed.process_exit, "unknown")
        self.assertEqual(observed.listener_release, "unknown")

    def test_a_receipt_that_names_another_data_directory_measures_nothing(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data, data_dir_digest="f" * 64)
            _make_lock(data)
            observed = self._observe(data, listener.port)
        self.assertEqual(observed.binding, OBS.BINDING_WORKSPACE_MISMATCH)
        self.assertEqual(observed.lease_release, "unknown")

    def test_an_absent_receipt_still_reports_the_listener_and_the_lease(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data)
            lock = _make_lock(data)
            with HeldLease(lock):
                observed = self._observe(data, listener.port)
        self.assertEqual(observed.binding, OBS.BINDING_RECEIPT_ABSENT)
        # An absent receipt proves nothing about a process...
        self.assertEqual(observed.owner_state, "unknown")
        self.assertEqual(observed.process_exit, "unknown")
        self.assertIsNone(observed.token_available)
        # ...while the port and the lock are still measured, and still say the
        # old writer is very much there.
        self.assertEqual(observed.listener_release, "failed")
        self.assertEqual(observed.lease_release, "failed")

    def test_the_owner_this_session_started_and_left_is_a_proven_exit(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data)
            _make_lock(data)
            observed = self._observe(data, _free_port())
        self.assertEqual(observed.binding, OBS.BINDING_BOUND)
        self.assertTrue(observed.bound)
        self.assertEqual(observed.owner_state, "dead")
        self.assertIs(observed.token_available, True)
        self.assertEqual(observed.process_exit, "verified")
        self.assertEqual(observed.listener_release, "verified")
        self.assertEqual(observed.lease_release, "verified")

    def test_a_live_owner_is_a_failed_release_not_an_unknown_one(self) -> None:
        self._controller(observation="live")
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data)
            _make_lock(data)
            observed = self._observe(data, listener.port)
        self.assertEqual(observed.owner_state, "live")
        self.assertEqual(observed.process_exit, "failed")

    def test_a_replaced_pid_is_never_read_as_an_exit(self) -> None:
        self._controller(observation="replaced")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data)
            _make_lock(data)
            observed = self._observe(data, _free_port())
        self.assertEqual(observed.owner_state, "unknown")
        self.assertEqual(observed.process_exit, "unknown")
        # Still this session's own receipt: the refusal is about the pid, not
        # about standing.
        self.assertEqual(observed.binding, OBS.BINDING_BOUND)

    def test_an_ambiguous_liveness_is_never_read_as_an_exit(self) -> None:
        self._controller(observation="unknown")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data)
            _make_lock(data)
            observed = self._observe(data, _free_port())
        self.assertEqual(observed.process_exit, "unknown")

    def test_a_foreign_token_is_its_own_refusal_and_claims_no_exit(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data)
            _make_lock(data)
            observed = self._observe(data, _free_port(), token=OTHER_TOKEN)
        self.assertEqual(observed.binding, OBS.BINDING_TOKEN_MISMATCH)
        self.assertIs(observed.token_available, False)
        self.assertEqual(observed.process_exit, "unknown")
        self.assertEqual(observed.owner_state, "unknown")
        # The port and the lock are facts about the selected store either way.
        self.assertEqual(observed.listener_release, "verified")
        self.assertEqual(observed.lease_release, "verified")

    def test_a_foreign_host_is_never_softened_into_a_token_question(self) -> None:
        self._controller(observation="exited", host=OTHER_HOST_IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data)
            _make_lock(data)
            observed = self._observe(data, _free_port())
        self.assertEqual(observed.binding, OBS.BINDING_FOREIGN_OWNER)
        self.assertEqual(observed.owner_state, "foreign")
        self.assertIsNone(observed.token_available)
        self.assertEqual(observed.process_exit, "unknown")

    def test_a_legacy_unfenced_receipt_is_its_own_condition(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _write_receipt(data, legacy=True)
            _make_lock(data)
            observed = self._observe(data, _free_port())
        self.assertEqual(observed.binding, OBS.BINDING_UNFENCED_OWNER)
        self.assertEqual(observed.owner_state, "unfenced")
        self.assertEqual(observed.process_exit, "unknown")

    def test_an_unreadable_receipt_is_reported_and_never_removed(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            receipt = data / OWNER.OWNER_FILENAME
            receipt.write_bytes(b"{not json")
            _make_lock(data)
            observed = self._observe(data, _free_port())
            self.assertTrue(receipt.exists())
            self.assertEqual(receipt.read_bytes(), b"{not json")
        self.assertEqual(observed.binding, OBS.BINDING_OWNER_UNREADABLE)
        self.assertEqual(observed.process_exit, "unknown")

    def test_the_observation_never_opens_a_process_handle(self) -> None:
        controller = self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            before = _write_receipt(data)
            _make_lock(data)
            self._observe(data, _free_port())
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), before)
        self.assertEqual(controller.opened, [])


# --------------------------------------------------------------------------
# The helper as the remote actually runs it


class ObservationSubprocessTest(unittest.TestCase):
    """One real subprocess run of the checked-in helper, decoded for real."""

    HELPER = SHELL / "remote_update_owner_observation.py"

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(self.HELPER), *argv],
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )

    def _observe_argv(self, data: Path, port: int, workspace: str = WORKSPACE_ID) -> list[str]:
        return [
            OBS.OBSERVE_COMMAND,
            "--data-dir",
            str(data),
            "--listener-port",
            str(port),
            "--workspace-id",
            workspace,
            "--session-token",
            OWN_TOKEN,
        ]

    def test_a_real_run_reports_a_held_lease_and_a_live_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data)
            lock = _make_lock(data)
            with HeldLease(lock):
                completed = self._run(self._observe_argv(data, listener.port))
        self.assertEqual(completed.returncode, 0)
        observed = OBS.decode_owner_release(completed.stdout)
        assert observed is not None
        self.assertEqual(observed.binding, OBS.BINDING_RECEIPT_ABSENT)
        self.assertEqual(observed.listener_release, "failed")
        self.assertEqual(observed.lease_release, "failed")

    def test_a_real_run_reports_a_free_lease_and_a_released_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _make_lock(data)
            completed = self._run(self._observe_argv(data, _free_port()))
        observed = OBS.decode_owner_release(completed.stdout)
        assert observed is not None
        self.assertEqual(observed.listener_release, "verified")
        self.assertEqual(observed.lease_release, "verified")

    def test_a_real_run_against_another_workspace_measures_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Listener() as listener:
            data = Path(directory)
            _write_workspace(data, OTHER_WORKSPACE_ID)
            _make_lock(data)
            completed = self._run(self._observe_argv(data, listener.port))
        observed = OBS.decode_owner_release(completed.stdout)
        assert observed is not None
        self.assertEqual(observed.binding, OBS.BINDING_WORKSPACE_MISMATCH)
        self.assertEqual(observed.listener_release, "unknown")

    def test_a_real_run_prints_exactly_one_bounded_line_and_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _make_lock(data)
            completed = self._run(self._observe_argv(data, _free_port()))
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        self.assertLessEqual(len(completed.stdout), OBS.MAX_RELEASE_BYTES)
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertNotIn(b"usage:", completed.stderr.lower())

    def test_malformed_argv_answers_with_an_observation_not_a_status(self) -> None:
        completed = self._run([OBS.OBSERVE_COMMAND, "--data-dir", "only"])
        self.assertEqual(completed.returncode, 0)
        observed = OBS.decode_owner_release(completed.stdout)
        assert observed is not None
        self.assertEqual(observed.binding, OBS.BINDING_UNKNOWN)
        self.assertEqual(observed.process_exit, "unknown")

    def test_an_all_zero_session_token_answers_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_workspace(data)
            _make_lock(data)
            argv = self._observe_argv(data, _free_port())
            argv[-1] = "0" * 32
            completed = self._run(argv)
        observed = OBS.decode_owner_release(completed.stdout)
        assert observed is not None
        self.assertEqual(observed.binding, OBS.BINDING_UNKNOWN)
        self.assertEqual(observed.lease_release, "unknown")


# --------------------------------------------------------------------------
# The observation command this port builds


class ObserveCommandTest(unittest.TestCase):
    def test_the_argv_is_the_one_fixed_shape(self) -> None:
        tokens = PORT.observe_owner_tokens(
            remote_python=REMOTE_PYTHON,
            observation_app_dir=OBSERVATION_APP_DIR,
            remote_data_dir=REMOTE_DATA_DIR,
            listener_port=8765,
            workspace_id=WORKSPACE_ID,
            session_token=OWN_TOKEN,
        )
        self.assertEqual(
            tokens,
            [
                REMOTE_PYTHON,
                "-I",
                "-B",
                f"{OBSERVATION_APP_DIR}/{PORT.OBSERVATION_RELATIVE}",
                OBS.OBSERVE_COMMAND,
                "--data-dir",
                REMOTE_DATA_DIR,
                "--listener-port",
                "8765",
                "--workspace-id",
                WORKSPACE_ID,
                "--session-token",
                OWN_TOKEN,
            ],
        )

    def test_the_command_refuses_every_unvalidated_input(self) -> None:
        base: dict[str, object] = {
            "remote_python": REMOTE_PYTHON,
            "observation_app_dir": OBSERVATION_APP_DIR,
            "remote_data_dir": REMOTE_DATA_DIR,
            "listener_port": 8765,
            "workspace_id": WORKSPACE_ID,
            "session_token": OWN_TOKEN,
        }
        for field, value, error in (
            ("remote_python", None, RemoteCommandError),
            ("remote_python", "python3", RemoteCommandError),
            ("observation_app_dir", "relative/app", RemoteCommandError),
            ("observation_app_dir", None, RemoteCommandError),
            ("remote_data_dir", "C:\\data", RemoteCommandError),
            ("workspace_id", "not-a-uuid", RemoteCommandError),
            ("workspace_id", "00000000-0000-0000-0000-000000000000", RemoteCommandError),
            ("session_token", "", RemoteCommandError),
            ("session_token", "0" * 32, RemoteCommandError),
            ("session_token", "token with spaces", RemoteCommandError),
            ("listener_port", 0, ValueError),
            ("listener_port", True, ValueError),
            ("listener_port", "8765", ValueError),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(error):
                    PORT.observe_owner_tokens(**{**base, field: value})  # type: ignore[arg-type]

    def test_the_ssh_command_is_batch_strict_and_forwards_nothing(self) -> None:
        command = PORT.build_ssh_observe_owner_command(
            _profile(), SSH, OWN_TOKEN, observation_app_dir=OBSERVATION_APP_DIR
        )
        self.assertEqual(command[0], SSH)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertNotIn("-L", command)
        self.assertEqual(command[-2], "workstack-remote")

    def test_the_observed_port_is_the_remote_listener_never_the_local_forward(self) -> None:
        profile = _profile(remote_port=9001, local_forward_port=19001)
        command = PORT.build_ssh_observe_owner_command(
            profile, SSH, OWN_TOKEN, observation_app_dir=OBSERVATION_APP_DIR
        )
        self.assertIn("--listener-port 9001", command[-1])
        self.assertNotIn("19001", command[-1])

    def test_the_helper_runs_from_the_prepared_location_not_the_stopped_app(self) -> None:
        command = PORT.build_ssh_observe_owner_command(
            _profile(), SSH, OWN_TOKEN, observation_app_dir=OBSERVATION_APP_DIR
        )
        remote = command[-1]
        self.assertIn(f"{OBSERVATION_APP_DIR}/{PORT.OBSERVATION_RELATIVE}", remote)
        # The app under observation still supplies the data directory, and
        # never the helper.
        self.assertIn(REMOTE_DATA_DIR, remote)
        self.assertNotIn(f"{REMOTE_APP_DIR}/{PORT.OBSERVATION_RELATIVE}", remote)

    def test_without_a_prepared_location_nothing_runs_and_nothing_is_claimed(self) -> None:
        runner = ScriptedRunner()
        observed = PORT.run_remote_observe_owner(
            _profile(), SSH, OWN_TOKEN, observation_app_dir=None, runner=runner
        )
        self.assertIsNone(observed)
        self.assertEqual(runner.commands, [])

    def test_a_command_that_cannot_be_built_was_never_run(self) -> None:
        runner = ScriptedRunner()
        observed = PORT.run_remote_observe_owner(
            _profile(remote_python=None),
            SSH,
            OWN_TOKEN,
            observation_app_dir=OBSERVATION_APP_DIR,
            runner=runner,
        )
        self.assertIsNone(observed)
        self.assertEqual(runner.commands, [])

    def test_only_stdout_is_read_and_stderr_is_never_a_diagnostic(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"], 255, stdout=b"", stderr=b"ssh: connect to host: no route\n"
        )
        observed = PORT.run_remote_observe_owner(
            _profile(),
            SSH,
            OWN_TOKEN,
            observation_app_dir=OBSERVATION_APP_DIR,
            runner=lambda argv: completed,
        )
        self.assertIsNone(observed)


# --------------------------------------------------------------------------
# The port itself


class OwnerStopPortTest(unittest.TestCase):
    """stop() asks the authenticated question; observe() only ever looks."""

    def _port(self, runner: ScriptedRunner, **kwargs: object) -> PORT.RemoteOwnerStopPort:
        options: dict[str, object] = {
            "observation_app_dir": OBSERVATION_APP_DIR,
            "ssh_executable": SSH,
            "runner": runner,
        }
        options.update(kwargs)
        return PORT.RemoteOwnerStopPort(_profile(), OWN_TOKEN, **options)  # type: ignore[arg-type]

    def test_it_satisfies_the_flow_port_and_publishes_only_the_owner_fields(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_CONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_RECEIPT_ABSENT,
                    listener_release="verified",
                    lease_release="verified",
                ),
            ),
        )
        port = self._port(runner)
        facts = port.stop("op-1")
        self.assertIsInstance(facts, OwnerStopFacts)
        # The projection publishes exactly the shared snapshot's owner keys.
        assert port.last_result is not None
        self.assertEqual(set(port.last_result.owner_facts()), set(OWNER_FIELDS))
        self.assertEqual(set(facts.__dict__), set(OWNER_FIELDS))
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.STOP, "stop_verified"),
        )

    def test_a_remote_that_reported_nothing_never_yields_a_confirmed_stop(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(["ssh"], STOP.EXIT_CONFIRMED, b""),
            observe=_completed(["ssh"], 0, _observation_stdout()),
        )
        port = self._port(runner)
        facts = port.stop("op-1")
        self.assertEqual(facts.process_exit, "unknown")
        assert port.last_result is not None
        self.assertEqual(port.last_result.code, STOP.STOP_RESULT_ABSENT)
        # Exit status zero with nothing said about the owner does not even
        # establish that this session held the token, so the flow stays
        # unsettled rather than reading the launch as a shutdown.
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.UNKNOWN, "stop_token_unknown"),
        )

    def test_a_refused_stop_stays_refused_and_names_its_own_operator_step(self) -> None:
        for code, expected in (
            (
                STOP.STOP_REFUSED_TOKEN_MISMATCH,
                (RemoteUpdateStage.FAILED, "stop_refused_missing_token"),
            ),
            (
                STOP.STOP_REFUSED_FOREIGN_HOST,
                (RemoteUpdateStage.FAILED, "stop_refused_foreign_host"),
            ),
            (
                STOP.STOP_REFUSED_UNFENCED_RECEIPT,
                (RemoteUpdateStage.FAILED, "stop_refused_unfenced_receipt"),
            ),
        ):
            with self.subTest(code=code):
                state = {
                    STOP.STOP_REFUSED_TOKEN_MISMATCH: ("unknown", False),
                    STOP.STOP_REFUSED_FOREIGN_HOST: ("foreign", True),
                    STOP.STOP_REFUSED_UNFENCED_RECEIPT: ("unfenced", True),
                }[code]
                runner = ScriptedRunner(
                    stop=_completed(
                        ["ssh"],
                        STOP.EXIT_REFUSED,
                        _stop_stdout(
                            code=code, state=state[0], token_available=state[1]
                        ),
                    ),
                    observe=_completed(
                        ["ssh"],
                        0,
                        _observation_stdout(
                            binding=OBS.BINDING_RECEIPT_ABSENT,
                            listener_release="verified",
                            lease_release="verified",
                        ),
                    ),
                )
                facts = self._port(runner).stop("op-1")
                self.assertEqual(facts.process_exit, "unknown")
                self.assertEqual(OUTCOMES.classify_owner_stop(facts), expected)

    def test_a_refusal_is_never_lifted_by_a_later_observation(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_REFUSED,
                _stop_stdout(
                    code=STOP.STOP_REFUSED_TOKEN_MISMATCH, token_available=False
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_BOUND,
                    owner_state="dead",
                    token_available=True,
                    process_exit="verified",
                    listener_release="verified",
                    lease_release="verified",
                ),
            ),
        )
        port = self._port(runner)
        facts = port.stop("op-1")
        self.assertEqual(facts.process_exit, "unknown")
        assert port.last_result is not None
        self.assertEqual(port.last_result.code, STOP.STOP_REFUSED_TOKEN_MISMATCH)
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.FAILED, "stop_refused_missing_token"),
        )

    def test_an_observation_completes_a_stop_the_request_could_not_confirm(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_UNCONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                    state="unknown",
                    token_available=True,
                    pidfd_available=False,
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_BOUND,
                    owner_state="dead",
                    token_available=True,
                    process_exit="verified",
                    listener_release="verified",
                    lease_release="verified",
                ),
            ),
        )
        port = self._port(runner)
        facts = port.stop("op-1")
        self.assertEqual(facts.process_exit, "verified")
        self.assertEqual(facts.state, "dead")
        assert port.last_result is not None
        self.assertEqual(port.last_result.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        # The upgraded record must still be internally consistent.
        self.assertEqual(STOP.report_contradiction(port.last_result), "")
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.STOP, "stop_verified_without_pidfd"),
        )

    def test_an_unbound_observation_never_completes_a_stop(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_UNCONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                    token_available=True,
                    pidfd_available=False,
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_TOKEN_MISMATCH,
                    owner_state="dead",
                    token_available=False,
                    process_exit="verified",
                    listener_release="verified",
                    lease_release="verified",
                ),
            ),
        )
        facts = self._port(runner).stop("op-1")
        self.assertEqual(facts.process_exit, "unknown")
        # The two releases were still measured against the selected store.
        self.assertEqual(facts.listener_release, "verified")
        self.assertEqual(facts.lease_release, "verified")

    def test_an_observation_from_another_workspace_attaches_nothing(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_UNCONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_UNCONFIRMED_TIMEOUT, token_available=True
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_WORKSPACE_MISMATCH,
                    listener_release="verified",
                    lease_release="verified",
                ),
            ),
        )
        facts = self._port(runner).stop("op-1")
        self.assertEqual(facts.listener_release, "unknown")
        self.assertEqual(facts.lease_release, "unknown")

    def test_a_retained_listener_is_carried_all_the_way_to_the_flow(self) -> None:
        """The 1.0.8 shape: the process went, the original listener did not."""

        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_CONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_RECEIPT_ABSENT,
                    listener_release="failed",
                    lease_release="failed",
                ),
            ),
        )
        facts = self._port(runner).stop("op-1")
        self.assertEqual(facts.process_exit, "verified")
        self.assertEqual(facts.listener_release, "failed")
        self.assertEqual(facts.lease_release, "failed")
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.FAILED, "stop_failed"),
        )

    def test_a_lost_stop_response_is_reconciled_and_never_reissued(self) -> None:
        runner = ScriptedRunner(stop_error=subprocess.TimeoutExpired(["ssh"], 8.0))
        port = self._port(runner)
        with self.assertRaises(LostResponse) as raised:
            port.stop("op-lost")
        self.assertEqual(raised.exception.operation_id, "op-lost")
        assert port.last_result is not None
        self.assertEqual(port.last_result.code, STOP.STOP_REQUEST_TIMED_OUT)
        self.assertEqual(port.last_result.process_exit, "unknown")
        # A timed-out request was not followed by an observation attempt; the
        # flow decides when to reconcile.
        self.assertEqual(runner.observe_commands, [])

    def test_observe_issues_no_stop_at_all(self) -> None:
        runner = ScriptedRunner(
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_BOUND,
                    owner_state="dead",
                    token_available=True,
                    process_exit="verified",
                    listener_release="verified",
                    lease_release="verified",
                ),
            )
        )
        port = self._port(runner)
        facts = port.observe("op-lost")
        self.assertEqual(runner.stop_commands, [])
        self.assertEqual(len(runner.observe_commands), 1)
        self.assertEqual(facts.process_exit, "verified")
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.STOP, "stop_verified"),
        )

    def test_observe_carries_the_pidfd_capability_the_stop_measured(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_UNCONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                    token_available=True,
                    pidfd_available=False,
                ),
            ),
            observe=_completed(
                ["ssh"], 0, _observation_stdout(binding=OBS.BINDING_RECEIPT_ABSENT)
            ),
        )
        port = self._port(runner)
        port.stop("op-1")
        self.assertIs(port.observe("op-1").pidfd_available, False)

    def test_an_observation_that_never_answered_claims_nothing(self) -> None:
        runner = ScriptedRunner(observe=_completed(["ssh"], 0, b"nonsense\n"))
        port = self._port(runner)
        facts = port.observe("op-1")
        self.assertIsNone(port.last_observation)
        self.assertEqual(facts.state, "unknown")
        self.assertEqual(facts.process_exit, "unknown")
        self.assertEqual(facts.listener_release, "unknown")
        self.assertEqual(facts.lease_release, "unknown")

    def test_the_stop_outcome_and_the_observation_stay_two_records(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_CONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
            ),
            observe=_completed(
                ["ssh"],
                0,
                _observation_stdout(
                    binding=OBS.BINDING_RECEIPT_ABSENT, listener_release="verified"
                ),
            ),
        )
        port = self._port(runner)
        port.stop("op-1")
        confirmed = port.last_result
        assert confirmed is not None
        runner.observe = _completed(
            ["ssh"], 0, _observation_stdout(binding=OBS.BINDING_RECEIPT_ABSENT)
        )
        port.observe("op-1")
        # A later read never rewrites what the request established.
        self.assertIs(port.last_result, confirmed)
        self.assertEqual(port.last_result.process_exit, "verified")

    def test_the_session_token_never_reaches_a_reported_detail(self) -> None:
        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_UNCONFIRMED,
                f"stop failed for {OWN_TOKEN}\n".encode("utf-8"),
            ),
            observe=_completed(
                ["ssh"], 0, _observation_stdout(binding=OBS.BINDING_RECEIPT_ABSENT)
            ),
        )
        port = self._port(runner)
        port.stop("op-1")
        assert port.last_result is not None
        self.assertNotIn(OWN_TOKEN, port.last_result.detail)

    def test_a_port_with_no_prepared_observer_reports_unknown_releases(self) -> None:
        """No verified helper location means no observation, not a fallback."""

        runner = ScriptedRunner(
            stop=_completed(
                ["ssh"],
                STOP.EXIT_CONFIRMED,
                _stop_stdout(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
            )
        )
        port = self._port(runner, observation_app_dir=None)
        facts = port.stop("op-1")
        self.assertEqual(facts.process_exit, "verified")
        self.assertEqual(facts.listener_release, "unknown")
        self.assertEqual(facts.lease_release, "unknown")
        self.assertIsNone(port.last_observation)
        self.assertEqual(runner.observe_commands, [])
        self.assertEqual(
            OUTCOMES.classify_owner_stop(facts),
            (RemoteUpdateStage.UNKNOWN, "stop_incomplete_unknown"),
        )

    def test_nothing_in_the_port_ever_falls_back_to_a_pid(self) -> None:
        source = (SHELL / "remote_update_owner_port.py").read_text(encoding="utf-8")
        for forbidden in ("os.kill", "pkill", "killpg", "SIGKILL", "taskkill"):
            self.assertNotIn(forbidden, source)


# --------------------------------------------------------------------------
# The remote entry point's stop-owned serialization


class RemoteEntryStopSerializationTest(unittest.TestCase):
    """stop-owned emits what it established; a status alone never confirms."""

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeController:
        controller = FakeController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def _main(self, data: Path, token: str) -> tuple[int, bytes]:
        """One stop-owned run, with both streams captured rather than leaked."""

        stdout = BytesIO()
        self.stderr = StringIO()
        with mock.patch.object(ENTRY.sys, "stdout", mock.Mock(buffer=stdout)):
            with mock.patch.object(ENTRY.sys, "stderr", self.stderr):
                code = ENTRY.main(
                    ["stop-owned", "--data-dir", str(data), "--session-token", token]
                )
        return code, stdout.getvalue()

    def test_an_absent_receipt_is_unconfirmed_and_never_exits_zero(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory:
            code, emitted = self._main(Path(directory), OWN_TOKEN)
        self.assertEqual(code, STOP.EXIT_UNCONFIRMED)
        self.assertNotEqual(code, STOP.EXIT_CONFIRMED)
        decoded = STOP.decode_stop_result(emitted)
        assert decoded is not None
        self.assertEqual(decoded.code, STOP.STOP_RECEIPT_ABSENT)
        self.assertEqual(decoded.process_exit, "unknown")

    def test_a_foreign_token_is_a_refusal_status_and_touches_nothing(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            before = _write_receipt(data)
            code, emitted = self._main(data, OTHER_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), before)
        self.assertEqual(code, STOP.EXIT_REFUSED)
        decoded = STOP.decode_stop_result(emitted)
        assert decoded is not None
        self.assertEqual(decoded.code, STOP.STOP_REFUSED_TOKEN_MISMATCH)
        self.assertIs(decoded.token_available, False)
        self.assertEqual(decoded.process_exit, "unknown")

    def test_a_legacy_receipt_is_its_own_refusal_and_is_kept(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            before = _write_receipt(data, legacy=True)
            code, emitted = self._main(data, OWN_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), before)
        self.assertEqual(code, STOP.EXIT_REFUSED)
        decoded = STOP.decode_stop_result(emitted)
        assert decoded is not None
        self.assertEqual(decoded.code, STOP.STOP_REFUSED_UNFENCED_RECEIPT)

    def test_a_confirmed_exit_is_the_only_thing_that_exits_zero(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            code, emitted = self._main(data, OWN_TOKEN)
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(code, STOP.EXIT_CONFIRMED)
        decoded = STOP.decode_stop_result(emitted)
        assert decoded is not None
        self.assertTrue(decoded.confirmed)
        self.assertEqual(decoded.process_exit, "verified")
        # The remote never opened a lease or a port, so it claims neither.
        self.assertEqual(decoded.listener_release, "unknown")
        self.assertEqual(decoded.lease_release, "unknown")

    def test_the_emitted_line_reconciles_against_its_own_exit_status(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            code, emitted = self._main(data, OWN_TOKEN)
        read_back = STOP.result_from_launch(returncode=code, stdout=emitted)
        self.assertTrue(read_back.confirmed)
        # The same payload under a disagreeing status confirms nothing.
        self.assertFalse(
            STOP.result_from_launch(returncode=code + 1, stdout=emitted).confirmed
        )

    def test_stdout_is_one_bounded_line_and_carries_no_token(self) -> None:
        self._controller(observation="exited")
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            _, emitted = self._main(data, OWN_TOKEN)
        self.assertEqual(emitted.count(b"\n"), 1)
        self.assertLessEqual(len(emitted), STOP.MAX_RESULT_BYTES)
        self.assertNotIn(OWN_TOKEN.encode("ascii"), emitted)

    def test_a_handle_that_could_not_signal_is_unconfirmed_not_a_flat_error(self) -> None:
        """The family split, on the exact case the superseded oracle pinned.

        ``tests/test_remote_ownership`` asserts this run exits ``2``, which is
        1.0.13's single error status for every condition. Splitting refused
        from unconfirmed is the whole point of the serialization, and the
        payload is only readable when the status agrees with it, so that
        assertion is superseded by this one. The operator diagnostic on stderr
        is unchanged, which is what the rest of that oracle checks.
        """

        controller = self._controller(observation="live")
        controller.open_owned_process = _refusing_open  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            before = _write_receipt(data)
            code, emitted = self._main(data, OWN_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), before)
        self.assertEqual(code, STOP.EXIT_UNCONFIRMED)
        self.assertNotEqual(code, STOP.EXIT_CONFIRMED)
        self.assertNotEqual(code, STOP.EXIT_REFUSED)
        decoded = STOP.decode_stop_result(emitted)
        assert decoded is not None
        self.assertEqual(decoded.code, STOP.STOP_UNCONFIRMED_HANDLE)
        self.assertEqual(decoded.process_exit, "unknown")
        message = self.stderr.getvalue()
        self.assertIn("REMOTE_PROTOCOL_INVALID", message)
        self.assertNotIn("Traceback", message)

    def test_the_historical_raising_contract_is_still_exported(self) -> None:
        self.assertIs(ENTRY.run_stop_owned, OWNER.run_stop_owned)


if __name__ == "__main__":
    unittest.main()
