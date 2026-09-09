"""Read-only observation of the remote owner's ORIGINAL listener and writer lease.

This file runs on the remote Linux host, invoked over the existing SSH seam by
``remote_update_owner_port``.  It also holds the bounded wire codec, so the
desktop side imports the same definition it decodes against rather than
restating it.

What it observes, and why each answer is separate
-------------------------------------------------

The 1.0.8 incident is exactly what happens when a stop request, a process
exit, a listening socket and a writer lease are treated as one fact.  They are
four facts, so this helper reports the three it can measure on the remote host
independently and never derives one from another:

``owner_state`` / ``process_exit``
    read from the published owner receipt through the existing
    ``remote_owner`` classification.  Nothing is signalled: the receipt is read
    and its pid classified, which is what ``stop-owned`` already does before it
    decides anything.  A receipt that is absent proves nothing about a
    process, so it stays ``unknown``.

``listener_release``
    a bounded loopback connect to the ORIGINAL served port, taken **on the
    host that owns that listener**.  This is not the desktop's forwarded port:
    closing the local SSH process frees the forward while the remote listener
    survives, which is the visible half of the incident, so a Windows-side
    forward observation can never populate this field.

``lease_release``
    proved through the same non-blocking exclusive file lease the store itself
    uses (:class:`workstack.file_lease._FileLease` over the data directory's
    lock file), never through the existence or removal of a receipt.  A lease
    that can be taken and immediately released was free; a lease another
    process holds answers ``failed``.

What it never does
------------------

It migrates nothing and writes nothing into the live SSOT.  It never
constructs a ``Store`` -- that constructor makes its root and runtime
directories -- and it takes the writer lease through
``_FileLease.acquire_existing``, the no-create acquisition on the same lock
protocol: no ``mkdir``, no ``O_CREAT``, no sentinel byte, no write and no
fsync.  A lock file that is absent, empty, replaced or unreadable therefore
leaves the pathname exactly as it was found, at every timing, and never reads
as a released lease.  It sends no signal, removes no receipt and opens no
channel of its own.  What remains unknowable rather than unwritten is stated
exactly in :func:`observe_writer_lease`.

Refusals stay refusals.  A workspace whose id does not match the selected one,
an owner receipt this session's token does not recognise, a foreign host and a
legacy unfenced receipt are four distinct ``binding`` values, none of which is
ever softened into a success.  The process exit status is always ``0``: the
refusal is the payload, and a status alone must never be readable as an
observation.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Sequence

# Python isolated mode (-I) omits the script directory from sys.path, exactly
# as it does for ``remote_entry``.  Admit only the resolved directory holding
# this checked-in file, and the application root above it that carries the
# ``workstack`` package, so both load from __file__ and never from cwd,
# PYTHONPATH or user site.  Import time on the desktop side is unaffected:
# both entries are already present there.
_SHELL_DIR = Path(__file__).resolve(strict=True).parent
_APP_ROOT = _SHELL_DIR.parent.parent
for _entry in (str(_APP_ROOT), str(_SHELL_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


SCHEMA_VERSION = "remote-owner-release/1"
OBSERVE_COMMAND = "observe-owner"

MAX_RELEASE_BYTES = 1024
MAX_DETAIL_LENGTH = 200
MAX_WORKSPACE_BYTES = 1_048_576

LOOPBACK_HOST = "127.0.0.1"
LISTENER_PROBE_TIMEOUT_SECONDS = 0.25

# workstack.store.LOCK_NAME.  Named here the way ``connection_registry_startup``
# already names it, so this module keeps one layer edge -- the lease primitive
# itself -- instead of two.  The Store never unlinks this file.
STORE_LEASE_FILE = ".workstack.lock"
WORKSPACE_FILE = "workspace.json"

Evidence = Literal["verified", "failed", "unknown"]
OwnerViewState = Literal["live", "stopping", "dead", "foreign", "unfenced", "unknown"]

EVIDENCE_VALUES: tuple[str, ...] = ("verified", "failed", "unknown")
# Deliberately the shared snapshot's owner states, so the desktop projection is
# a rename of nothing.
OWNER_STATE_VALUES: tuple[str, ...] = (
    "live",
    "stopping",
    "dead",
    "foreign",
    "unfenced",
    "unknown",
)

# How the observation is bound to the thing the caller selected.  Only
# ``bound`` means "these observations are about the owner this session
# started"; every other value names a different operator step.
BINDING_BOUND = "bound"
BINDING_WORKSPACE_MISMATCH = "workspace_mismatch"
BINDING_WORKSPACE_UNKNOWN = "workspace_unknown"
BINDING_RECEIPT_ABSENT = "receipt_absent"
BINDING_TOKEN_MISMATCH = "token_mismatch"
BINDING_FOREIGN_OWNER = "foreign_owner"
BINDING_UNFENCED_OWNER = "unfenced_owner"
BINDING_OWNER_UNREADABLE = "owner_unreadable"
BINDING_UNKNOWN = "unknown"

BINDING_VALUES: tuple[str, ...] = (
    BINDING_BOUND,
    BINDING_WORKSPACE_MISMATCH,
    BINDING_WORKSPACE_UNKNOWN,
    BINDING_RECEIPT_ABSENT,
    BINDING_TOKEN_MISMATCH,
    BINDING_FOREIGN_OWNER,
    BINDING_UNFENCED_OWNER,
    BINDING_OWNER_UNREADABLE,
    BINDING_UNKNOWN,
)

# Bindings under which the workspace gate passed, so the listener and the
# lease really are facts about the SELECTED store even where the receipt is
# absent, foreign, legacy or owned by another session.  Under the two
# workspace answers, and under an unknown binding, nothing was measured at all
# and no field may be attached anywhere.
WORKSPACE_BOUND_BINDINGS = frozenset(BINDING_VALUES) - {
    BINDING_WORKSPACE_MISMATCH,
    BINDING_WORKSPACE_UNKNOWN,
    BINDING_UNKNOWN,
}

# ``remote_owner`` classification -> (published owner state, exit evidence).
# ``replaced`` and ``ambiguous`` establish nothing about this session's owner:
# the pid may carry a different process, or may not be readable at all, and
# neither is an exit.  They are unknown rather than either extreme.
_OWNER_CLASSIFICATION: dict[str, tuple[str, str]] = {
    "live": ("live", "failed"),
    "dead": ("dead", "verified"),
    "replaced": ("unknown", "unknown"),
    "ambiguous": ("unknown", "unknown"),
    "foreign": ("foreign", "unknown"),
    "unfenced": ("unfenced", "unknown"),
}


def sanitize_detail(value: object) -> str:
    """Printable ASCII, bounded.  Remote text never reaches a caller raw."""

    if not isinstance(value, str):
        return ""
    kept = "".join(char for char in value if 32 <= ord(char) < 127)
    return kept[:MAX_DETAIL_LENGTH]


def _evidence(value: object) -> Evidence:
    return value if value in EVIDENCE_VALUES else "unknown"  # type: ignore[return-value]


def _owner_state(value: object) -> OwnerViewState:
    return value if value in OWNER_STATE_VALUES else "unknown"  # type: ignore[return-value]


def _binding(value: object) -> str:
    return value if value in BINDING_VALUES else BINDING_UNKNOWN


def _tristate(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class OwnerReleaseObservation:
    """One read-only look at the remote owner, its listener and its lease.

    Every field defaults to the answer that claims nothing, so an observation
    that could not be taken is indistinguishable from one that was never
    attempted -- which is the honest reading of both.
    """

    binding: str = BINDING_UNKNOWN
    owner_state: OwnerViewState = "unknown"
    token_available: bool | None = None
    process_exit: Evidence = "unknown"
    listener_release: Evidence = "unknown"
    lease_release: Evidence = "unknown"
    detail: str = ""

    @property
    def bound(self) -> bool:
        """True only when this is the owner the calling session started."""

        return self.binding == BINDING_BOUND


def normalized(observation: OwnerReleaseObservation) -> OwnerReleaseObservation:
    """Coerce every field onto its allowlist, however the value was built."""

    return OwnerReleaseObservation(
        binding=_binding(observation.binding),
        owner_state=_owner_state(observation.owner_state),
        token_available=_tristate(observation.token_available),
        process_exit=_evidence(observation.process_exit),
        listener_release=_evidence(observation.listener_release),
        lease_release=_evidence(observation.lease_release),
        detail=sanitize_detail(observation.detail),
    )


def encode_owner_release(observation: OwnerReleaseObservation) -> bytes:
    """One bounded single line -- the only thing this command prints."""

    checked = normalized(observation)
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "binding": checked.binding,
            "owner_state": checked.owner_state,
            "token_available": checked.token_available,
            "process_exit": checked.process_exit,
            "listener_release": checked.listener_release,
            "lease_release": checked.lease_release,
            "detail": checked.detail,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if len(payload) > MAX_RELEASE_BYTES:
        return encode_owner_release(replace(checked, detail=""))
    return payload


def decode_owner_release(payload: object) -> OwnerReleaseObservation | None:
    """Read one emitted observation, or report that there was none.

    An untrusted boundary: the bytes come off a remote host.  Size, schema and
    every value are checked, unrecognised values normalize to the answer that
    claims nothing, and ``None`` means nothing readable was emitted at all --
    which is different from an observation that said ``unknown``.
    """

    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    if not isinstance(payload, str):
        return None
    lines = [line for line in payload.splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[-1]
    if len(line.encode("utf-8", "replace")) > MAX_RELEASE_BYTES:
        return None
    try:
        body = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
        return None
    return normalized(
        OwnerReleaseObservation(
            binding=body.get("binding"),  # type: ignore[arg-type]
            owner_state=body.get("owner_state"),  # type: ignore[arg-type]
            token_available=body.get("token_available"),
            process_exit=body.get("process_exit"),  # type: ignore[arg-type]
            listener_release=body.get("listener_release"),  # type: ignore[arg-type]
            lease_release=body.get("lease_release"),  # type: ignore[arg-type]
            detail=body.get("detail"),  # type: ignore[arg-type]
        )
    )


def observe_original_listener(port: object) -> Evidence:
    """Is the ORIGINAL served port free again, as seen from its own host?

    A bounded connect, sending nothing: refused means the listener is gone,
    accepted means something is still serving there.  A connect that neither
    completes nor is refused has proved neither, so it is not read as a
    release; only then is it settled by one momentary exclusive bind of the
    same port, released immediately, which is how this repository already
    settles port availability.

    Scope is exactly the remote host's own loopback listener.  A desktop-side
    forwarded port is a different socket on a different machine and can never
    answer this question.
    """

    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return "unknown"
    try:
        with socket.create_connection(
            (LOOPBACK_HOST, port), timeout=LISTENER_PROBE_TIMEOUT_SECONDS
        ):
            return "failed"
    except ConnectionRefusedError:
        return "verified"
    except OSError:
        return _listener_by_bind(port)


def _listener_by_bind(port: int) -> Evidence:
    """Settle an unanswered port with one momentary bind, or stay unknown.

    Only ``EADDRINUSE`` says something is holding the port.  ``EACCES`` says
    this process may not bind here -- a privileged port, a reserved range, a
    local policy -- not that anything is listening, so it stays unknown.
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK_HOST, port))
        return "verified"
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return "failed"
        return "unknown"


def _file_identity(path: Path) -> tuple[object, object] | None:
    """Which file this path currently names, or ``None`` if it names none."""

    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def _handle_identity(handle: object) -> tuple[object, object] | None:
    """Which file an OPEN handle refers to, whatever its pathname now says.

    A pathname can be made to name a different file between a check and an
    open; an already-opened descriptor cannot.  This is the identity the lease
    was actually taken on.
    """

    descriptor = getattr(handle, "fileno", None)
    if descriptor is None:
        return None
    try:
        status = os.fstat(descriptor())
    except (OSError, ValueError):
        return None
    return (status.st_dev, status.st_ino)


def observe_writer_lease(data_dir: Path) -> Evidence:
    """Is the data directory's writer lease free, proved by taking it?

    The proof is the supported lock API itself -- the same non-blocking
    exclusive file lease ``Store`` takes for a transaction and holds for a
    server's lifetime -- not the presence or absence of any receipt.  A
    receipt says what somebody wrote down; the lease says whether a writer is
    actually holding the store right now.  No second lock protocol is invented
    here: a private one would not interlock with the writers this is about.

    A missing directory, a missing lock file, something that is not a regular
    file, and a lock file still empty of its sentinel byte all answer
    ``unknown``.  Every other case is decided by one ``acquire_existing`` --
    the no-create acquisition on the same lock protocol -- which opens an
    already-existing file without ``O_CREAT``, makes no directory, writes no
    sentinel and fsyncs nothing.  Nothing here can bring a lock file into
    being at any timing, so an observation that fails to answer leaves the
    live SSOT exactly as it was found.

    **What is actually compared.**  One initial ``stat`` of the pathname
    fixes the whole question: regular file, non-zero size, and the identity
    ``(st_dev, st_ino)``.  The answer then has to survive two checks against
    that single baseline -- ``os.fstat`` on the handle that was *actually
    opened and locked*, and a stat of the pathname after the lock was taken.
    The first is what rules out the race the pathname cannot show: if the file
    is unlinked and replaced between the initial stat and the open, the lock
    is taken on the free replacement while the real writer still holds the
    original inode, and only the opened handle's own identity reveals it.
    Either mismatch releases and answers ``unknown``.  Contention is the same
    question: ``StoreLockedError`` is ordinary ``failed`` only while the
    pathname still names that baseline.  If the file is unlinked and replaced
    after the open and before the platform lock, flock contends on the
    original inode and the handle is gone before those two post-lock checks
    run, so the current pathname is compared here instead -- absent, replaced
    or unreadable stays ``unknown``.

    **What cannot be inferred.**  A replacement that had already completed
    *before* this observation began is an external violation of the lock-file
    protocol -- the store itself never unlinks this file -- and it is not
    discoverable from the new pathname alone: there is no earlier identity to
    compare against, and a lease taken on a legitimately recreated file is
    indistinguishable from one taken on a usurped pathname.  This function
    closes its own observed interval and claims nothing outside it.
    """

    lock_path = data_dir / STORE_LEASE_FILE
    # One stat, one baseline: mode, size and identity all come from the same
    # look, so no two pre-checks can be talking about different files.
    try:
        status = os.stat(lock_path)
    except OSError:
        return "unknown"
    if not stat.S_ISREG(status.st_mode) or status.st_size == 0:
        return "unknown"
    identity = (status.st_dev, status.st_ino)

    from workstack.file_lease import StoreLockedError, _FileLease

    lease = _FileLease(lock_path)
    try:
        lease.acquire_existing()
    except StoreLockedError:
        named = _file_identity(lock_path)
        if named is None or named != identity:
            return "unknown"
        return "failed"
    except OSError:
        # Absent, vanished, unreadable or not openable here. Nothing was
        # created to find that out, and nothing is claimed from it.
        return "unknown"
    try:
        opened = _handle_identity(lease.file)
        named = _file_identity(lock_path)
    finally:
        try:
            lease.release()
        except OSError:
            # The lease was demonstrably free; a release that stumbled is this
            # process's problem, not evidence about the remote writer.
            pass
    if opened is None or opened != identity:
        # The lock that was actually taken is not on the file that was
        # checked. A writer may still be holding the original inode behind the
        # replaced pathname, so this is emphatically not a release.
        return "unknown"
    if named is None or named != identity:
        # The pathname stopped naming the locked file while it was locked.
        return "unknown"
    return "verified"


def _selected_workspace_id(data_dir: Path) -> str | None:
    """The canonical SSOT workspace id, read without touching anything else."""

    path = data_dir / WORKSPACE_FILE
    try:
        if path.stat().st_size > MAX_WORKSPACE_BYTES:
            return None
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(body, dict):
        return None
    identifier = body.get("id")
    return identifier if isinstance(identifier, str) and identifier else None


def _classify_published_owner(
    data_dir: Path, workspace_id: str, session_token: str
) -> tuple[str, OwnerViewState, bool | None, Evidence, str]:
    """Bind the published receipt to this session, without signalling anything.

    Fencing is decided before the token: a foreign host and a legacy unfenced
    receipt are facts about whether that pid may be interpreted here at all,
    and neither becomes a token question.  Only once the receipt is one this
    host may read is it asked whether this session's token owns it.
    """

    from remote_owner import (
        classify_owner,
        get_process_controller,
        local_path_digest,
        owner_recognizes_caller,
        read_owner_receipt,
    )

    try:
        receipt = read_owner_receipt(data_dir)
    except Exception as error:  # noqa: BLE001  bounded: any unreadable receipt
        return (
            BINDING_OWNER_UNREADABLE,
            "unknown",
            None,
            "unknown",
            sanitize_detail(f"owner receipt is unreadable: {type(error).__name__}"),
        )
    if receipt is None:
        return (
            BINDING_RECEIPT_ABSENT,
            "unknown",
            None,
            "unknown",
            "no owner receipt is published for this data directory",
        )
    if receipt.workspace_id != workspace_id or receipt.data_dir_digest != local_path_digest(
        data_dir
    ):
        # The receipt itself disagrees about which store it owns. Nothing read
        # under it describes the selected workspace, so this is the workspace
        # answer rather than a token or fencing answer.
        return (
            BINDING_WORKSPACE_MISMATCH,
            "unknown",
            None,
            "unknown",
            "the owner receipt names a different workspace or data directory",
        )
    state, exit_evidence = _OWNER_CLASSIFICATION.get(
        classify_owner(receipt, get_process_controller()), ("unknown", "unknown")
    )
    if state == "foreign":
        return (BINDING_FOREIGN_OWNER, "foreign", None, "unknown",
                "the owner receipt belongs to another host")
    if state == "unfenced":
        return (BINDING_UNFENCED_OWNER, "unfenced", None, "unknown",
                "the owner receipt is legacy and carries no host or boot fencing")
    if not owner_recognizes_caller(receipt, session_token):
        return (BINDING_TOKEN_MISMATCH, "unknown", False, "unknown",
                "the published owner does not recognise this session's token")
    return (BINDING_BOUND, state, True, exit_evidence, "")  # type: ignore[return-value]


def observe_owner_release(
    *,
    data_dir: Path,
    listener_port: object,
    workspace_id: str,
    session_token: str,
) -> OwnerReleaseObservation:
    """Take the three independent observations, bound to what was selected.

    The workspace binding is checked first and it is a gate: an observation
    taken against a data directory that is not the selected workspace -- by
    its own ``workspace.json``, or by the published receipt's own account of
    which store and which directory it owns -- says nothing about the selected
    one, so the listener and the lease are not even measured there.  Everything after that gate is measured and reported
    separately, including under a binding that says this is not the caller's
    own owner -- the port and the lock are facts about the selected workspace
    either way, and suppressing them would only hide the incident's shape.
    """

    observed_workspace = _selected_workspace_id(data_dir)
    if observed_workspace is None:
        return OwnerReleaseObservation(
            binding=BINDING_WORKSPACE_UNKNOWN,
            detail="the selected data directory publishes no readable workspace id",
        )
    if observed_workspace != workspace_id:
        return OwnerReleaseObservation(
            binding=BINDING_WORKSPACE_MISMATCH,
            detail="the data directory holds a different workspace than the one selected",
        )
    binding, state, token_available, exit_evidence, detail = _classify_published_owner(
        data_dir, workspace_id, session_token
    )
    if binding not in WORKSPACE_BOUND_BINDINGS:
        # The gate did not pass after all: the published receipt names another
        # store. Nothing further is measured, because nothing measured here
        # would be about the workspace the caller selected.
        return OwnerReleaseObservation(binding=binding, detail=detail)
    return OwnerReleaseObservation(
        binding=binding,
        owner_state=state,
        token_available=token_available,
        process_exit=exit_evidence,
        listener_release=observe_original_listener(listener_port),
        lease_release=observe_writer_lease(data_dir),
        detail=detail,
    )


class _BoundedArgumentParser(argparse.ArgumentParser):
    """Argv errors are data too: no usage text, no stack, no exit status."""

    def error(self, message: str) -> None:
        raise ValueError("malformed argv")

    def print_usage(self, file=None) -> None:
        return

    def print_help(self, file=None) -> None:
        return


def parse_observe_argv(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _BoundedArgumentParser(
        prog="remote_update_owner_observation.py", add_help=False
    )
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser(OBSERVE_COMMAND, add_help=False)
    observe.add_argument("--data-dir", required=True)
    observe.add_argument("--listener-port", required=True, type=int)
    observe.add_argument("--workspace-id", required=True)
    observe.add_argument("--session-token", required=True)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """Emit one bounded observation line and exit ``0``, always.

    The status is deliberately uninformative.  A caller must read the emitted
    contract to learn anything; nothing about this command's exit is ever a
    statement about the owner, the listener or the lease.
    """

    from remote_command_contract import RemoteCommandError, require_session_token

    try:
        args = parse_observe_argv(argv)
        token = require_session_token(args.session_token)
    except (ValueError, RemoteCommandError):
        sys.stdout.buffer.write(
            encode_owner_release(
                OwnerReleaseObservation(detail="the observation request was malformed")
            )
        )
        return 0
    sys.stdout.buffer.write(
        encode_owner_release(
            observe_owner_release(
                data_dir=Path(args.data_dir),
                listener_port=args.listener_port,
                workspace_id=str(args.workspace_id),
                session_token=token,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
