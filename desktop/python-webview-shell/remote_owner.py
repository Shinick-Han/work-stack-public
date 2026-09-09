"""Remote owner receipt: identity, fencing, and the authority a stop needs.

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

Siblings carry the parts a pid check alone cannot do.  Every mutation runs
inside one ``remote_receipt_guard`` critical section, so a cooperating worker
cannot publish a new owner between another worker's comparison and its unlink,
and the stop signal goes through a ``remote_process_handle`` bound to the exact
process, so a reused pid is refused rather than signalled.

What is left here is the composition of those parts, and it is the half that
needs this host.  ``remote_owner_receipt`` holds the receipt's bytes, its
bounds and its publication; ``remote_owner_stop`` holds the confirmation that
goes through a handle, or the honest observation where no pidfd exists.  The
controllers, the host and boot identity they report, the fencing
classification and the guarded acquire/reclaim/remove/stop decisions live in
this module, which reads those siblings and is read by none of them.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from pathlib import Path
from typing import Literal

# Python isolated mode (-I) omits the script directory from sys.path. Admit
# only the resolved directory containing this checked-in file so the sibling
# contract loads from __file__, never cwd, PYTHONPATH, or user site.
_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_command_contract import token_hash

# Every name a caller already reached through this module stays reachable from
# here, re-exported unchanged, so the split moved no import site and no
# monkeypatch target.
from remote_owner_receipt import (  # noqa: F401  partly re-exported
    FENCED_OWNER_KEYS,
    MAX_IDENTITY_VALUE_LENGTH,
    MAX_OWNER_BYTES,
    OWNER_FENCING_KEYS,
    OWNER_FILENAME,
    OWNER_KEYS,
    SHA256_HEX_LENGTH,
    UNFENCED_RECOVERY_DETAIL,
    EntryError,
    GuardWait,
    OwnerReceipt,
    encode_owner_receipt,
    owner_receipt_path,
    parse_owner_receipt,
    read_owner_receipt,
    read_owner_receipt_bytes,
    write_owner_receipt_exclusive,
    _read_owner_bytes,
    _receipt_guard,
)
from remote_owner_stop import (  # noqa: F401  partly re-exported
    NO_PIDFD_LIMIT_DETAIL,
    UNSIGNALLED_WAIT_SECONDS,
    StopWait,
    _Decision,
    _confirmed,
    _refusal,
    _replaced_refusal,
    _stop_through_owned_handle,
    _unsignalled_budget,
    controller_pidfd_available,
)
from remote_process_handle import (  # noqa: F401  partly re-exported
    OwnedProcessHandle,
    ProcessController,
    ProcessHandleUnavailable,
    ProcessObservation,
    open_owned_process,
    pidfd_signalling_available,
    read_bounded_ascii,
    read_start_identity,
)
from remote_stop_result import (
    STOP_AMBIGUOUS_LIVENESS,
    STOP_CONFIRMED_ALREADY_EXITED,
    STOP_RECEIPT_ABSENT,
    STOP_REFUSED_FOREIGN_HOST,
    STOP_REFUSED_GUARD,
    STOP_REFUSED_RECEIPT_INVALID,
    STOP_REFUSED_TOKEN_MISMATCH,
    STOP_REFUSED_UNFENCED_RECEIPT,
    STOP_REFUSED_WORKSPACE_MISMATCH,
    StopResult,
)


# /proc/stat puts a very long "intr" line before "btime", so it needs far more
# room than one process stat.
MAX_IDENTITY_SOURCE_BYTES = 262_144

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

    def pidfd_available(self) -> bool | None:
        # Nothing outside Linux /proc opens a pidfd, and saying so plainly is
        # better than reporting an unknown this host can already answer.
        return False

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
            raise EntryError(
                "REMOTE_PROTOCOL_INVALID", str(error), reason=error.reason
            ) from error

    def pidfd_available(self) -> bool | None:
        return pidfd_signalling_available()

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


# Removing a receipt is an authority decision, not a byte operation: only the
# module that judged the receipt may take it away, and only while it is still
# the exact one that was judged. So the removal stays here with the decisions
# that authorise it -- which is also what keeps it one seam a cooperating
# worker's reclaim can be stalled at in a two-process race test.
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




def _stop_owned_locked(
    data_dir: Path,
    *,
    expected_hash: str,
    expected_data: str,
    wait: StopWait,
    unsignalled_wait: StopWait,
) -> _Decision:
    """The whole stop decision, with the receipt guard already held."""

    try:
        found = read_owner_receipt_bytes(data_dir)
    except EntryError as error:
        return _refusal(STOP_REFUSED_RECEIPT_INVALID, error)
    if found is None:
        # Nothing claims this data directory, and that is all it says. No
        # process was observed either way, so both the exit evidence and the
        # owner state stay unknown: an owner that exited and cleaned up, a
        # receipt that was never written, and one removed by hand all look
        # like this, and calling it "dead" would pick one of the three
        # without having observed anything.
        return (
            StopResult(
                code=STOP_RECEIPT_ABSENT,
                state="unknown",
                detail="no owner receipt claims this data directory",
            ),
            None,
        )
    receipt, raw = found
    if not hmac.compare_digest(receipt.data_dir_digest, expected_data):
        return _refusal(
            STOP_REFUSED_WORKSPACE_MISMATCH,
            EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch"),
        )
    if not hmac.compare_digest(receipt.token_hash, expected_hash):
        # A different session owns this receipt, or this session's token was
        # lost and regenerated. Either way this caller is not the owner; that
        # is a distinct fact from a legacy receipt and from a foreign host.
        #
        # It is a fact about authority and not about liveness. The refusal
        # lands before any controller is consulted, so nothing has looked at
        # the recorded pid: a receipt whose token does not match may name a
        # process that is running, one that exited minutes ago, or a pid now
        # held by something unrelated. The state therefore stays unknown, and
        # the operator step is the one the token mismatch names.
        return _refusal(
            STOP_REFUSED_TOKEN_MISMATCH,
            EntryError("REMOTE_LOCK_OWNED", f"pid={receipt.pid}"),
            state="unknown",
            token_available=False,
        )
    controller = get_process_controller()
    pidfd = controller_pidfd_available(controller)
    state = classify_owner(receipt, controller)
    if state == "unfenced":
        return _refusal(
            STOP_REFUSED_UNFENCED_RECEIPT,
            EntryError("REMOTE_LOCK_OWNED", UNFENCED_RECOVERY_DETAIL),
            state="unfenced",
            token_available=True,
            pidfd_available=pidfd,
        )
    if state == "foreign":
        marker = (receipt.host_identity or "")[:12]
        return _refusal(
            STOP_REFUSED_FOREIGN_HOST,
            EntryError(
                "REMOTE_LOCK_OWNED",
                f"owner receipt belongs to another host host={marker}",
            ),
            state="foreign",
            token_available=True,
            pidfd_available=pidfd,
        )
    if state == "ambiguous":
        return _refusal(
            STOP_AMBIGUOUS_LIVENESS,
            EntryError("REMOTE_PROTOCOL_INVALID", "owner liveness is ambiguous"),
            state="unknown",
            token_available=True,
            pidfd_available=pidfd,
        )
    if state == "replaced":
        return _replaced_refusal(receipt, pidfd_available=pidfd)
    if state == "dead":
        _unlink_receipt_if_unchanged_locked(data_dir, raw)
        return _confirmed(STOP_CONFIRMED_ALREADY_EXITED, pidfd_available=pidfd)
    result, refusal = _stop_through_owned_handle(receipt, controller, wait, unsignalled_wait)
    if result.confirmed:
        _unlink_receipt_if_unchanged_locked(data_dir, raw)
    return result, refusal


def _stop_owned(
    data_dir: Path,
    session_token: str,
    wait: StopWait,
    guard: GuardWait,
    unsignalled_wait: StopWait | None,
) -> _Decision:
    expected_hash = token_hash(session_token)
    expected_data = local_path_digest(data_dir)
    try:
        with _receipt_guard(data_dir, guard):
            return _stop_owned_locked(
                data_dir,
                expected_hash=expected_hash,
                expected_data=expected_data,
                wait=wait,
                unsignalled_wait=unsignalled_wait or _unsignalled_budget(wait),
            )
    except EntryError as error:
        # Only the guard itself raises out of that block; everything inside it
        # returns its refusal instead, so this really is "the critical section
        # could not be entered" and nothing was read, signalled or removed.
        return _refusal(STOP_REFUSED_GUARD, error)


def stop_owned_result(
    data_dir: Path,
    session_token: str,
    wait: StopWait = StopWait(),
    guard: GuardWait = GuardWait(),
    *,
    unsignalled_wait: StopWait | None = None,
) -> StopResult:
    """Stop the owner this session started and report what was established.

    Same authority and same effects as :func:`run_stop_owned`; the difference
    is that every condition comes back as one bounded symbolic outcome instead
    of as a raise, so the caller can tell a refusal apart from an unconfirmed
    stop and record the exit evidence rather than the fact that a command ran.

    ``listener_release`` and ``lease_release`` are left unknown here on
    purpose.  This module never opens the writer lease and never binds a port,
    so it has measured neither; a caller that has really observed one attaches
    it with :meth:`StopResult.with_observations`.
    """

    result, _ = _stop_owned(data_dir, session_token, wait, guard, unsignalled_wait)
    return result


def run_stop_owned(
    data_dir: Path,
    session_token: str,
    wait: StopWait = StopWait(),
    guard: GuardWait = GuardWait(),
) -> None:
    """Stop the owner this session started, and only after proving it stopped.

    The raising form, kept for the remote entry point's existing exit-status
    contract: it returns for a confirmed stop and raises the refusal for every
    other condition.  :func:`stop_owned_result` is the same decision without
    the loss of detail that collapsing every condition into one raise causes.

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

    _, refusal = _stop_owned(data_dir, session_token, wait, guard, None)
    if refusal is not None:
        raise refusal
