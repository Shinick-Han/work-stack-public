"""Remote owner receipt: identity, fencing, and bounded stop confirmation.

The receipt records which process on which host and which boot is serving one
data directory.  That directory can be a share, and a pid only means something
on the host and boot that produced it, so `/proc` is read only for a receipt
whose host and boot identity both match this process.  A foreign-host receipt
is refused untouched; an older boot of this host is provably dead without
reading `/proc`; a receipt written before fencing carries no host identity, so
it is refused too and the refusal names the one operator step that clears it.

Nothing here grants write authority.  The real writer lease stays the only
authority and is never opened, stolen, or unlinked from this module; the
receipt is lifetime bookkeeping that may refuse a start and may stop the one
process this session started, after proving that process is gone.

Two siblings carry the parts a pid check alone cannot do.  Every mutation runs
inside one ``remote_receipt_guard`` critical section, so a cooperating worker
cannot publish a new owner between another worker's comparison and its unlink,
and the stop signal goes through a ``remote_process_handle`` bound to the exact
process, so a reused pid is refused rather than signalled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal, Protocol

# Python isolated mode (-I) omits the script directory from sys.path. Admit
# only the resolved directory containing this checked-in file so the sibling
# contract loads from __file__, never cwd, PYTHONPATH, or user site.
_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_command_contract import token_hash
from remote_process_handle import (
    OwnedProcessHandle,
    ProcessHandleUnavailable,
    open_owned_process,
    read_bounded_ascii,
    read_start_identity,
)
from remote_receipt_guard import (
    GuardContended,
    GuardUnavailable,
    GuardWait,
    owner_receipt_guard,
)
from remote_receipt_io import (
    ReceiptAlreadyExists,
    ReceiptPublicationUnavailable,
    publish_bytes_exclusive,
)


OWNER_KEYS = (
    "workspace_id",
    "data_dir_digest",
    "app_dir_digest",
    "pid",
    "start_identity",
    "release_id",
    "token_hash",
)
# A receipt carries exactly OWNER_KEYS or exactly FENCED_OWNER_KEYS.
OWNER_FENCING_KEYS = ("host_identity", "boot_identity")
FENCED_OWNER_KEYS = OWNER_KEYS + OWNER_FENCING_KEYS
MAX_OWNER_BYTES = 4096
# /proc/stat puts a very long "intr" line before "btime", so it needs far more
# room than one process stat.
MAX_IDENTITY_SOURCE_BYTES = 262_144
MAX_IDENTITY_VALUE_LENGTH = 64
OWNER_FILENAME = ".workstack-remote-owner.json"
SHA256_HEX_LENGTH = 64

# Digests are domain separated so a host digest can never equal a boot digest,
# and source tagged so two different sources never compare equal by accident.
HOST_IDENTITY_DOMAIN = "workstack-remote-owner-host-v1"
BOOT_IDENTITY_DOMAIN = "workstack-remote-owner-boot-v1"
MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
PROC_STAT_PATH = "/proc/stat"
# A host without /proc has no observable boot. The constant keeps it
# self-consistent without claiming boot fencing it does not have.
ISOLATED_BOOT_SOURCE = "isolated-no-boot-identity"

UNFENCED_RECOVERY_DETAIL = (
    "legacy owner receipt has no host or boot identity; confirm no remote owner "
    f"is serving this data directory from any host, then remove {OWNER_FILENAME}"
)


@dataclass(frozen=True)
class StopWait:
    """Bounded confirmation budget for stop-owned. Tests inject the clock."""

    timeout_seconds: float = 10.0
    poll_seconds: float = 0.05
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


class EntryError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


# "replaced" is deliberately not folded into "exited": the recorded process is
# gone either way, but only "exited" is evidence that this session's own owner
# ended, and only "replaced" says its pid now names something unrelated.
ProcessObservation = Literal["live", "exited", "replaced", "unknown"]


class ProcessController(Protocol):
    def current_pid(self) -> int:
        ...

    def start_identity(self, pid: int) -> str | None:
        ...

    def observe(self, pid: int, start_identity: str) -> ProcessObservation:
        ...

    def open_owned_process(self, pid: int, start_identity: str) -> OwnedProcessHandle | None:
        ...

    def host_identity(self) -> str | None:
        ...

    def boot_identity(self) -> str | None:
        ...


class IsolatedProcessController:
    """Default controller outside Linux /proc, where nothing may be signalled."""

    def current_pid(self) -> int:
        return os.getpid()

    def start_identity(self, pid: int) -> str | None:
        if pid == os.getpid():
            return "isolated-self"
        return None

    def observe(self, pid: int, start_identity: str) -> ProcessObservation:
        if pid == os.getpid() and start_identity == "isolated-self":
            return "live"
        # Without /proc another process's liveness is not observable here, and
        # answering "exited" would be a claim this host cannot support.
        return "unknown"

    def open_owned_process(self, pid: int, start_identity: str) -> OwnedProcessHandle | None:
        raise EntryError(
            "REMOTE_PROTOCOL_INVALID",
            "this host cannot bind a stop to the owned process, so it is refused",
        )

    def host_identity(self) -> str | None:
        return local_host_identity()

    def boot_identity(self) -> str | None:
        return _identity_digest(BOOT_IDENTITY_DOMAIN, "isolated", ISOLATED_BOOT_SOURCE)


class LinuxProcController:
    def current_pid(self) -> int:
        return os.getpid()

    def start_identity(self, pid: int) -> str | None:
        return read_start_identity(pid)

    def observe(self, pid: int, start_identity: str) -> ProcessObservation:
        if not start_identity:
            return "unknown"
        actual = read_start_identity(pid)
        if actual is None:
            return "unknown" if Path(f"/proc/{pid}").exists() else "exited"
        return "live" if actual == start_identity else "replaced"

    def open_owned_process(self, pid: int, start_identity: str) -> OwnedProcessHandle | None:
        """Pin this exact process for the stop, or refuse the stop.

        There is no kill(pid) underneath: a pid that was checked and then
        signalled by number is the defect this replaces, so a host that cannot
        pin the process is told so and the receipt is kept as evidence.
        """

        try:
            return open_owned_process(pid, start_identity)
        except ProcessHandleUnavailable as error:
            raise EntryError("REMOTE_PROTOCOL_INVALID", str(error)) from error

    def host_identity(self) -> str | None:
        return local_host_identity()

    def boot_identity(self) -> str | None:
        return local_boot_identity()


_PROCESS_CONTROLLER: ProcessController | None = None


def default_process_controller() -> ProcessController:
    if Path("/proc/self/stat").is_file():
        return LinuxProcController()
    return IsolatedProcessController()


def get_process_controller() -> ProcessController:
    if _PROCESS_CONTROLLER is not None:
        return _PROCESS_CONTROLLER
    return default_process_controller()


def set_process_controller(controller: ProcessController | None) -> ProcessController | None:
    global _PROCESS_CONTROLLER
    previous = _PROCESS_CONTROLLER
    _PROCESS_CONTROLLER = controller
    return previous


def _identity_digest(domain: str, source: str, value: str) -> str:
    payload = "\x00".join((domain, source, value)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bounded_identity_source(path: str) -> str | None:
    value = read_bounded_ascii(path, MAX_IDENTITY_SOURCE_BYTES)
    return value if value and len(value) <= MAX_IDENTITY_VALUE_LENGTH else None


def local_host_identity() -> str | None:
    """Digest this host from a machine identity, or None when there is none.

    A nodename is configuration, not identity: two hosts can be given the same
    one and a host can change its own between boots.  Treating it as proof of
    "this host" is exactly what would let one machine claim another machine's
    receipt off a shared data directory, so there is no fallback.  Without a
    machine-id source this host is unknown, every receipt reads as unfenced,
    and both acquisition and stop refuse rather than guess.
    """

    for path in MACHINE_ID_PATHS:
        value = _bounded_identity_source(path)
        if value:
            return _identity_digest(HOST_IDENTITY_DOMAIN, "machine-id", value)
    return None


def _proc_btime() -> str | None:
    stat = read_bounded_ascii(PROC_STAT_PATH, MAX_IDENTITY_SOURCE_BYTES) or ""
    for line in stat.splitlines():
        value = line[len("btime ") :].strip() if line.startswith("btime ") else ""
        if value.isdigit():
            return value
    return None


def local_boot_identity() -> str | None:
    """Digest this boot, or None when this boot cannot be identified.

    boot_id is a fresh random value per boot and is preferred.  The btime
    fallback is a whole-second timestamp, so two boots landing in the same
    second share it; it is kept because it separates the reboots that actually
    happen, and it is not offered as collision-free proof.
    """

    value = _bounded_identity_source(BOOT_ID_PATH)
    if value:
        return _identity_digest(BOOT_IDENTITY_DOMAIN, "boot-id", value)
    btime = _proc_btime()
    return _identity_digest(BOOT_IDENTITY_DOMAIN, "btime", btime) if btime else None


def local_path_digest(path: Path) -> str:
    canonical = str(path)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OwnerReceipt:
    workspace_id: str
    data_dir_digest: str
    app_dir_digest: str
    pid: int
    start_identity: str
    release_id: str
    token_hash: str
    # Absent in a pre-fencing receipt, and absent means unknown host.
    host_identity: str | None = None
    boot_identity: str | None = None

    @property
    def is_fenced(self) -> bool:
        return self.host_identity is not None and self.boot_identity is not None


def owner_receipt_path(data_dir: Path) -> Path:
    return data_dir / OWNER_FILENAME


def _sha256_hex(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != SHA256_HEX_LENGTH:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{field} is invalid")
    if any(character not in "0123456789abcdef" for character in value):
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{field} is invalid")
    return value


def _bounded_label(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTITY_VALUE_LENGTH:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"owner {field} is invalid")
    if any(ord(character) < 32 for character in value):
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"owner {field} is invalid")
    return value


def _bounded_pid(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner pid is invalid")
    return value


def _owner_body(payload: bytes) -> dict[str, object]:
    if len(payload) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt is not JSON") from error
    if not isinstance(value, dict):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt shape")
    if set(value) not in ({*OWNER_KEYS}, {*FENCED_OWNER_KEYS}):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt shape")
    return value


def parse_owner_receipt(payload: bytes) -> OwnerReceipt:
    value = _owner_body(payload)
    fenced = "host_identity" in value
    return OwnerReceipt(
        workspace_id=_bounded_label(value["workspace_id"], "workspace identity"),
        data_dir_digest=_sha256_hex(value["data_dir_digest"], "data_dir_digest"),
        app_dir_digest=_sha256_hex(value["app_dir_digest"], "app_dir_digest"),
        pid=_bounded_pid(value["pid"]),
        start_identity=_bounded_label(value["start_identity"], "start identity"),
        release_id=_bounded_label(value["release_id"], "release identity"),
        token_hash=_sha256_hex(value["token_hash"], "token_hash"),
        host_identity=_sha256_hex(value["host_identity"], "host_identity") if fenced else None,
        boot_identity=_sha256_hex(value["boot_identity"], "boot_identity") if fenced else None,
    )


def _read_owner_bytes(data_dir: Path) -> bytes | None:
    path = owner_receipt_path(data_dir)
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_OWNER_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt is unreadable") from error
    if len(payload) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    return payload


def read_owner_receipt_bytes(data_dir: Path) -> tuple[OwnerReceipt, bytes] | None:
    """Read the receipt with the exact bytes every later removal is judged on."""

    payload = _read_owner_bytes(data_dir)
    if payload is None:
        return None
    return parse_owner_receipt(payload), payload


def read_owner_receipt(data_dir: Path) -> OwnerReceipt | None:
    found = read_owner_receipt_bytes(data_dir)
    return None if found is None else found[0]


def encode_owner_receipt(receipt: OwnerReceipt) -> bytes:
    if not receipt.is_fenced:
        raise EntryError(
            "REMOTE_PROTOCOL_INVALID", "owner receipt is missing host or boot identity"
        )
    encoded = json.dumps(
        {
            "workspace_id": receipt.workspace_id,
            "data_dir_digest": receipt.data_dir_digest,
            "app_dir_digest": receipt.app_dir_digest,
            "pid": receipt.pid,
            "start_identity": receipt.start_identity,
            "release_id": receipt.release_id,
            "token_hash": receipt.token_hash,
            "host_identity": receipt.host_identity,
            "boot_identity": receipt.boot_identity,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    return encoded


def unlink_owner_receipt(data_dir: Path) -> None:
    try:
        owner_receipt_path(data_dir).unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt could not be removed") from error


def _unlink_receipt_if_unchanged_locked(data_dir: Path, expected: bytes) -> bool:
    """Remove the receipt only while it is still the exact one that was judged.

    Call this with the receipt guard already held.  The comparison and the
    unlink are two operations and the filesystem has no compare-and-swap to
    fuse them, so it is the guard, not this function, that stops a cooperating
    worker publishing a new owner in between and having it deleted here.
    """

    current = _read_owner_bytes(data_dir)
    if current is None:
        return True
    if current != expected:
        return False
    unlink_owner_receipt(data_dir)
    return True


@contextmanager
def _receipt_guard(data_dir: Path, guard: GuardWait) -> Iterator[None]:
    """Hold the receipt guard, reporting a refusal in the remote error codes."""

    try:
        with owner_receipt_guard(data_dir, guard):
            yield
    except GuardContended as error:
        raise EntryError("REMOTE_LOCK_OWNED", str(error)) from error
    except GuardUnavailable as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", str(error)) from error


def write_owner_receipt_exclusive(data_dir: Path, receipt: OwnerReceipt) -> None:
    """Create the receipt, refusing an existing one. Callers hold the guard."""

    encoded = encode_owner_receipt(receipt)
    try:
        publish_bytes_exclusive(owner_receipt_path(data_dir), encoded)
    except ReceiptAlreadyExists as error:
        raise EntryError("REMOTE_LOCK_OWNED", "pid=unknown") from error
    except ReceiptPublicationUnavailable as error:
        raise EntryError(
            "REMOTE_PROTOCOL_INVALID",
            str(error) or "owner receipt could not be published",
        ) from error


OwnerFencing = Literal["unfenced", "foreign_host", "prior_boot", "current_boot"]
OwnerState = Literal[
    "absent", "dead", "replaced", "live", "ambiguous", "foreign", "unfenced"
]


def classify_owner_fencing(receipt: OwnerReceipt, controller: ProcessController) -> OwnerFencing:
    """Decide whether this receipt's pid may be interpreted here at all."""

    host = controller.host_identity()
    boot = controller.boot_identity()
    # A process that cannot say which host or boot it is cannot claim a receipt.
    if not receipt.is_fenced or not host or not boot:
        return "unfenced"
    if not hmac.compare_digest(str(receipt.host_identity), host):
        return "foreign_host"
    if not hmac.compare_digest(str(receipt.boot_identity), boot):
        return "prior_boot"
    return "current_boot"


def classify_owner(receipt: OwnerReceipt, controller: ProcessController) -> OwnerState:
    """Classify a receipt, reading /proc only for this host and this boot."""

    fencing = classify_owner_fencing(receipt, controller)
    if fencing == "foreign_host":
        # That pid names an unrelated local process, so it is never inspected.
        return "foreign"
    if fencing == "unfenced":
        return "unfenced"
    if fencing == "prior_boot":
        # That pid space is gone, which is proof without touching any pid.
        return "dead"
    observed = controller.observe(receipt.pid, receipt.start_identity)
    if observed == "unknown":
        return "ambiguous"
    if observed == "exited":
        return "dead"
    if observed == "replaced":
        return "replaced"
    return "live"


def _refuse_unowned_state(receipt: OwnerReceipt, state: OwnerState) -> None:
    """Raise for every state that forbids touching the receipt or its pid."""

    if state == "unfenced":
        raise EntryError("REMOTE_LOCK_OWNED", UNFENCED_RECOVERY_DETAIL)
    if state == "foreign":
        marker = (receipt.host_identity or "")[:12]
        raise EntryError(
            "REMOTE_LOCK_OWNED",
            f"owner receipt belongs to another host host={marker}",
        )
    if state == "ambiguous":
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner liveness is ambiguous")


def owner_recognizes_caller(receipt: OwnerReceipt, caller_token: str | None) -> bool:
    """Recognize the caller that already owns this receipt, in constant time."""

    if not caller_token:
        return False
    return hmac.compare_digest(receipt.token_hash, token_hash(caller_token))


def reclaim_or_refuse_owner(
    data_dir: Path,
    *,
    expected_workspace_id: str | None = None,
    expected_data_digest: str | None = None,
    caller_token: str | None = None,
    guard: GuardWait = GuardWait(),
) -> None:
    """Take the guard and decide whether this caller may serve here."""

    with _receipt_guard(data_dir, guard):
        _reclaim_or_refuse_owner_locked(
            data_dir,
            expected_workspace_id=expected_workspace_id,
            expected_data_digest=expected_data_digest,
            caller_token=caller_token,
        )


def _reclaim_or_refuse_owner_locked(
    data_dir: Path,
    *,
    expected_workspace_id: str | None = None,
    expected_data_digest: str | None = None,
    caller_token: str | None = None,
) -> None:
    """Acquisition-side reclaim, with the guard already held.

    This is the acquisition policy, and it is deliberately wider than the
    token-scoped stop: an owner whose process has exited, whose boot is gone,
    or whose pid now names an unrelated process is no longer serving, so any
    caller starting here may clear its receipt.  A stop does not inherit that
    authority, because a stop claims to act for the session that wrote the
    receipt and must prove it holds that session's token first.
    """

    found = read_owner_receipt_bytes(data_dir)
    if found is None:
        return
    receipt, raw = found
    if expected_data_digest is not None and receipt.data_dir_digest != expected_data_digest:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch")
    if expected_workspace_id is not None and receipt.workspace_id != expected_workspace_id:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch")
    state = classify_owner(receipt, get_process_controller())
    _refuse_unowned_state(receipt, state)
    if state == "live":
        if owner_recognizes_caller(receipt, caller_token):
            return
        raise EntryError("REMOTE_LOCK_OWNED", f"pid={receipt.pid}")
    _unlink_receipt_if_unchanged_locked(data_dir, raw)


def build_owner_receipt(
    *,
    app_dir: Path,
    data_dir: Path,
    workspace_id: str,
    release_id: str,
    session_token: str,
) -> OwnerReceipt:
    controller = get_process_controller()
    pid = controller.current_pid()
    start = controller.start_identity(pid)
    if not start:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "process start identity is unavailable")
    host = controller.host_identity()
    boot = controller.boot_identity()
    if not host or not boot:
        # Never write a receipt no later caller could fence on.
        raise EntryError("REMOTE_PROTOCOL_INVALID", "host or boot identity is unavailable")
    return OwnerReceipt(
        workspace_id=workspace_id,
        data_dir_digest=local_path_digest(data_dir),
        app_dir_digest=local_path_digest(app_dir),
        pid=pid,
        start_identity=start,
        release_id=release_id,
        token_hash=token_hash(session_token),
        host_identity=host,
        boot_identity=boot,
    )


def acquire_owner_receipt(
    *,
    app_dir: Path,
    data_dir: Path,
    workspace_id: str,
    release_id: str,
    session_token: str,
    guard: GuardWait = GuardWait(),
) -> OwnerReceipt:
    """Clear any finished owner and publish this one, without letting go.

    Reclaiming and creating are one decision.  Split across two guard sections,
    a competing worker could reclaim in the gap and delete the receipt this
    process had just published, so both happen inside a single hold.
    """

    with _receipt_guard(data_dir, guard):
        _reclaim_or_refuse_owner_locked(
            data_dir,
            expected_workspace_id=workspace_id,
            expected_data_digest=local_path_digest(data_dir),
        )
        receipt = build_owner_receipt(
            app_dir=app_dir,
            data_dir=data_dir,
            workspace_id=workspace_id,
            release_id=release_id,
            session_token=session_token,
        )
        write_owner_receipt_exclusive(data_dir, receipt)
    return receipt


def remove_published_owner_receipt_if_still_ours(
    data_dir: Path,
    receipt: OwnerReceipt,
    guard: GuardWait = GuardWait(),
) -> str | None:
    """Remove only the exact bytes this process published, or say it is uncertain.

    Takes one new hold of the receipt guard.  It never signals, never opens the
    writer lease, and never unlinks a replacement.  Returns None when those
    exact bytes are gone.  Returns a short detail when this process cannot
    confirm that, including when the guard cannot be taken.
    """

    expected = encode_owner_receipt(receipt)
    try:
        with _receipt_guard(data_dir, guard):
            current = _read_owner_bytes(data_dir)
            if current is None:
                return None
            if current != expected:
                return "published owner receipt was replaced before cleanup"
            unlink_owner_receipt(data_dir)
            remaining = _read_owner_bytes(data_dir)
            if remaining is None:
                return None
            if remaining == expected:
                return "published owner receipt cleanup is uncertain"
            return "published owner receipt was replaced before cleanup"
    except EntryError:
        return "published owner receipt cleanup is uncertain"


def _await_handle_exit(
    handle: OwnedProcessHandle, receipt: OwnerReceipt, wait: StopWait
) -> None:
    """Wait through the same handle that was signalled, or refuse.

    The handle names the process, so nothing that later takes its pid can make
    this loop report success. SIGTERM was sent once, before this call.
    """

    deadline = wait.monotonic() + wait.timeout_seconds
    while True:
        exited = handle.has_exited()
        if exited:
            return
        if exited is None:
            raise EntryError(
                "REMOTE_PROTOCOL_INVALID", f"owner exit could not be confirmed pid={receipt.pid}"
            )
        if wait.monotonic() >= deadline:
            raise EntryError(
                "REMOTE_LOCK_OWNED",
                f"owner did not exit within {wait.timeout_seconds:g}s pid={receipt.pid}",
            )
        wait.sleep(wait.poll_seconds)


def _stop_through_owned_handle(
    receipt: OwnerReceipt, controller: ProcessController, wait: StopWait
) -> None:
    """Signal and confirm through one handle bound to the recorded process.

    The pin, the liveness poll, the signal and the wait are the same handle, so
    a pid reused at any point in that sequence cannot be signalled and cannot
    be mistaken for the owner exiting.  The handle is closed exactly once.
    """

    handle = controller.open_owned_process(receipt.pid, receipt.start_identity)
    if handle is None:
        # The pid holds no process at all, which is the end a stop asks for.
        return
    try:
        exited = handle.has_exited()
        if exited is None:
            raise EntryError(
                "REMOTE_PROTOCOL_INVALID",
                f"owner liveness could not be read through its handle pid={receipt.pid}",
            )
        if exited:
            return
        try:
            handle.send_terminate()
        except ProcessHandleUnavailable as error:
            raise EntryError("REMOTE_PROTOCOL_INVALID", str(error)) from error
        _await_handle_exit(handle, receipt, wait)
    finally:
        handle.close()


def run_stop_owned(
    data_dir: Path,
    session_token: str,
    wait: StopWait = StopWait(),
    guard: GuardWait = GuardWait(),
) -> None:
    """Stop the owner this session started, and only after proving it stopped.

    The session token is admitted before anything is removed, so a caller that
    does not hold the owner's token deletes nothing here, not even a receipt
    whose process has already exited; clearing a finished owner for a new start
    is the acquisition path's job and needs no token.

    Everything after that runs inside one hold of the receipt guard, from the
    first read through the signal, the bounded wait and the removal, so no
    cooperating worker can publish a new owner into the middle of a stop and
    have it deleted by a decision made about the old one.

    A pid that no longer carries the recorded start identity is refused with
    the receipt kept: this session cannot then say whether its own owner exited
    or its pid was handed to something else, and neither answer is worth
    signalling on.  A timeout or an unreadable handle also keeps the receipt.
    """

    expected_hash = token_hash(session_token)
    expected_data = local_path_digest(data_dir)
    with _receipt_guard(data_dir, guard):
        found = read_owner_receipt_bytes(data_dir)
        if found is None:
            return
        receipt, raw = found
        if not hmac.compare_digest(receipt.data_dir_digest, expected_data):
            raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch")
        if not hmac.compare_digest(receipt.token_hash, expected_hash):
            raise EntryError("REMOTE_LOCK_OWNED", f"pid={receipt.pid}")
        controller = get_process_controller()
        state = classify_owner(receipt, controller)
        _refuse_unowned_state(receipt, state)
        if state == "replaced":
            raise EntryError(
                "REMOTE_PROTOCOL_INVALID",
                f"owner pid {receipt.pid} now carries a different start identity, so "
                "this session cannot tell whether its owner exited; receipt kept",
            )
        if state != "dead":
            _stop_through_owned_handle(receipt, controller, wait)
        _unlink_receipt_if_unchanged_locked(data_dir, raw)
