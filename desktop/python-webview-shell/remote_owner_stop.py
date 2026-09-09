"""Confirm a stop through a handle bound to the process, or say what stopped it.

The half of stop-owned that decides nothing about authority.  By the time
anything here runs, the caller in ``remote_owner`` has already held the receipt
guard, admitted the session token, and proved the receipt belongs to this host
and this boot.  What is left is one question -- did the recorded process go? --
and the honest report of how well it was answered.

Where the kernel offers a pidfd, the pin, the liveness poll, the signal and the
wait all go through one handle bound to the recorded process, so a pid reused
at any point in that sequence cannot be signalled and cannot be mistaken for
the owner exiting.  Only one refusal is routed around: a host with no pidfd
mechanism at all.  A permission refusal, a pin failure or a start-identity
mismatch is a fact about this pid and stops here, because routing around those
is how a stop reaches the wrong process.

Where there is no pidfd, nothing is signalled at all.  The served process is
exec'd under the sshd session with PR_SET_PDEATHSIG, so closing the SSH channel
that started it is what ends it, and this module only observes the recorded pid
and bounds the confirmation.  There is no fallback to ``kill(pid)`` here or
anywhere below: that fallback is the exact defect the handle exists to remove.

Every outcome is a bounded pair -- the evidence to record and, for the caller
that still wants the historical raise-on-anything-but-success contract, the
refusal to raise.  A replaced pid and a timeout both keep the receipt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from remote_owner_receipt import EntryError, OwnerReceipt
from remote_process_handle import (
    PIDFD_UNAVAILABLE_DETAIL,
    REASON_PIDFD_UNSUPPORTED,
    OwnedProcessHandle,
    ProcessController,
    ProcessHandleUnavailable,
)
from remote_stop_result import (
    STOP_CONFIRMED_ALREADY_EXITED,
    STOP_CONFIRMED_EXIT,
    STOP_CONFIRMED_WITHOUT_SIGNAL,
    STOP_REFUSED_REPLACED_PID,
    STOP_UNCONFIRMED_HANDLE,
    STOP_UNCONFIRMED_NO_PIDFD,
    STOP_UNCONFIRMED_TIMEOUT,
    StopResult,
)


@dataclass(frozen=True)
class StopWait:
    """Bounded confirmation budget for stop-owned. Tests inject the clock."""

    # Both halves of a stop wait for the same thing, and neither of them can
    # make it happen.  The served process is exec'd under the sshd session
    # that the asking desktop is still holding open -- it has to be, because
    # that channel is what authenticates the request -- and it finishes its
    # shutdown by joining the request threads its own listener still has, one
    # per socket the desktop's forward is holding.  So an owner that has been
    # signalled and an owner that has not are in the same position: it goes
    # when the caller closes the channel, not while the caller is waiting.
    #
    # Measured on the supported remote (WSL2 6.18, packaged 3.12 runtime, this
    # source): with nothing connected to the served port, SIGTERM through the
    # pidfd handle is confirmed end to end in 0.19s.  With one socket open on
    # that port -- what the desktop's own forward leaves behind -- the exit
    # cannot complete at all, and the wait spends whatever it is given and
    # then reports the same honest "unconfirmed" either way.  A budget large
    # enough to outlast that is not a budget that can ever collect the answer;
    # it only delays the answer the caller needs in order to close the channel
    # and let the exit happen, and pushes the whole round trip past the
    # transport budget above it, which is how an honest verdict was lost.
    #
    # Two seconds is therefore ten times the measured unblocked confirmation
    # and no longer than that, for the same reason the unsignalled slice below
    # is.  The proven exit is earned on the caller's confirmation pass, after
    # the channel is gone, where it costs 0.14s.
    timeout_seconds: float = 2.0
    poll_seconds: float = 0.05
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


# The unsignalled observation below cannot make anything exit, so it is given a
# small slice of the stop budget rather than the whole of it: spending ten
# seconds watching a process nothing has asked to leave only delays the honest
# "unconfirmed" answer the caller needs in order to close its own SSH channel.
# A caller that injects a wider budget still gets only this slice here.
UNSIGNALLED_WAIT_SECONDS = 2.0

NO_PIDFD_LIMIT_DETAIL = (
    "this host has no pidfd signalling, so the owner was observed and not "
    "signalled; it exits with its own session through the exec/PDEATHSIG "
    "binding once the desktop closes the SSH channel that started it"
)


def controller_pidfd_available(controller: ProcessController) -> bool | None:
    """Ask a controller whether pidfd signalling exists here, or say unknown.

    The capability is reported, never assumed.  A controller that predates the
    question answers nothing and the capability stays unknown; an unknown here
    changes no decision, it only stops the report claiming a capability that
    was never measured.
    """

    probe = getattr(controller, "pidfd_available", None)
    if not callable(probe):
        return None
    try:
        value = probe()
    except (AttributeError, OSError):
        return None
    return value if isinstance(value, bool) else None


# One stop decision: the evidence the caller records, and the refusal a caller
# wanting the historical raise-on-anything-but-success contract should raise.
_Decision = tuple[StopResult, "EntryError | None"]


def _refusal(
    code: str,
    error: EntryError,
    *,
    state: str = "unknown",
    token_available: bool | None = None,
    pidfd_available: bool | None = None,
    process_exit: str = "unknown",
) -> _Decision:
    return (
        StopResult(
            code=code,
            state=state,  # type: ignore[arg-type]
            token_available=token_available,
            pidfd_available=pidfd_available,
            process_exit=process_exit,  # type: ignore[arg-type]
            detail=str(error),
        ),
        error,
    )


def _confirmed(code: str, *, pidfd_available: bool | None, detail: str = "") -> _Decision:
    return (
        StopResult(
            code=code,
            state="dead",
            token_available=True,
            pidfd_available=pidfd_available,
            process_exit="verified",
            detail=detail,
        ),
        None,
    )


def _unsignalled_budget(wait: StopWait) -> StopWait:
    """A short slice of the stop budget, on whatever clock the caller injected."""

    return StopWait(
        timeout_seconds=min(wait.timeout_seconds, UNSIGNALLED_WAIT_SECONDS),
        poll_seconds=wait.poll_seconds,
        monotonic=wait.monotonic,
        sleep=wait.sleep,
    )


def _await_handle_exit(
    handle: OwnedProcessHandle, receipt: OwnerReceipt, wait: StopWait
) -> _Decision:
    """Wait through the same handle that was signalled, or report why not.

    The handle names the process, so nothing that later takes its pid can make
    this loop report success. SIGTERM was sent once, before this call.
    """

    deadline = wait.monotonic() + wait.timeout_seconds
    while True:
        exited = handle.has_exited()
        if exited:
            return _confirmed(STOP_CONFIRMED_EXIT, pidfd_available=True)
        if exited is None:
            return _refusal(
                STOP_UNCONFIRMED_HANDLE,
                EntryError(
                    "REMOTE_PROTOCOL_INVALID",
                    f"owner exit could not be confirmed pid={receipt.pid}",
                ),
                state="stopping",
                token_available=True,
                pidfd_available=True,
            )
        if wait.monotonic() >= deadline:
            return _refusal(
                STOP_UNCONFIRMED_TIMEOUT,
                EntryError(
                    "REMOTE_LOCK_OWNED",
                    f"owner did not exit within {wait.timeout_seconds:g}s pid={receipt.pid}",
                ),
                state="stopping",
                token_available=True,
                pidfd_available=True,
                # Signalled, watched through its own handle, still there: this
                # is an observed failure to exit, not an absence of evidence.
                process_exit="failed",
            )
        wait.sleep(wait.poll_seconds)


def _await_exit_without_signal(
    receipt: OwnerReceipt, controller: ProcessController, wait: StopWait
) -> _Decision:
    """The no-pidfd path: observe the recorded process, never signal it.

    Where a pidfd cannot be opened there is no way to bind a signal to the
    process, and signalling the pid number instead is the exact defect the
    handle exists to remove, so this path sends nothing at all.  What it does
    is the other half of a normal shutdown that already exists: the served
    process is exec'd under the sshd session with PR_SET_PDEATHSIG, so closing
    the SSH channel that started it is what ends it, and this loop is the
    bounded confirmation that it went.

    The limit is stated rather than hidden: called while the owning channel is
    still open, this cannot make anything exit and will report an unconfirmed
    stop.  It earns its answer on the confirmation pass the caller runs after
    it has closed that channel, where the process is already gone.

    Reading ``/proc`` for this pid needs no pidfd and is already fenced to this
    host and this boot by the caller.  A pid that now carries a different start
    is reported as replaced, not as an exit, because the two are different
    facts even though the recorded process is gone in both.
    """

    deadline = wait.monotonic() + wait.timeout_seconds
    while True:
        observed = controller.observe(receipt.pid, receipt.start_identity)
        if observed == "exited":
            return _confirmed(
                STOP_CONFIRMED_WITHOUT_SIGNAL,
                pidfd_available=False,
                detail=NO_PIDFD_LIMIT_DETAIL,
            )
        if observed == "replaced":
            return _replaced_refusal(receipt, pidfd_available=False)
        if observed == "unknown":
            return _refusal(
                STOP_UNCONFIRMED_NO_PIDFD,
                EntryError(
                    "REMOTE_PROTOCOL_INVALID",
                    f"{PIDFD_UNAVAILABLE_DETAIL}; owner liveness is ambiguous "
                    f"pid={receipt.pid}",
                    reason=REASON_PIDFD_UNSUPPORTED,
                ),
                state="unknown",
                token_available=True,
                pidfd_available=False,
            )
        if wait.monotonic() >= deadline:
            return _refusal(
                STOP_UNCONFIRMED_NO_PIDFD,
                EntryError(
                    "REMOTE_LOCK_OWNED",
                    f"{PIDFD_UNAVAILABLE_DETAIL}; owner pid={receipt.pid} was still "
                    f"running after {wait.timeout_seconds:g}s of observation. "
                    f"{NO_PIDFD_LIMIT_DETAIL}",
                    reason=REASON_PIDFD_UNSUPPORTED,
                ),
                state="live",
                token_available=True,
                pidfd_available=False,
                # Watched and still running: an observed failure to exit.
                process_exit="failed",
            )
        wait.sleep(wait.poll_seconds)


def _replaced_refusal(receipt: OwnerReceipt, *, pidfd_available: bool | None) -> _Decision:
    return _refusal(
        STOP_REFUSED_REPLACED_PID,
        EntryError(
            "REMOTE_PROTOCOL_INVALID",
            f"owner pid {receipt.pid} now carries a different start identity, so "
            "this session cannot tell whether its owner exited; receipt kept",
        ),
        state="unknown",
        token_available=True,
        pidfd_available=pidfd_available,
    )


def _stop_through_owned_handle(
    receipt: OwnerReceipt,
    controller: ProcessController,
    wait: StopWait,
    unsignalled_wait: StopWait,
) -> _Decision:
    """Signal and confirm through one handle bound to the recorded process.

    The pin, the liveness poll, the signal and the wait are the same handle, so
    a pid reused at any point in that sequence cannot be signalled and cannot
    be mistaken for the owner exiting.  The handle is closed exactly once.

    Only one refusal is routed around: a host with no pidfd mechanism at all,
    which goes to the observation path above.  A permission refusal, a pin
    failure or a start-identity mismatch is a fact about this pid and stops
    here, because routing around those is how a stop reaches the wrong process.
    """

    pidfd = controller_pidfd_available(controller)
    try:
        handle = controller.open_owned_process(receipt.pid, receipt.start_identity)
    except EntryError as error:
        if getattr(error, "reason", "") == REASON_PIDFD_UNSUPPORTED:
            return _await_exit_without_signal(receipt, controller, unsignalled_wait)
        return _refusal(
            STOP_UNCONFIRMED_HANDLE,
            error,
            state="live",
            token_available=True,
            pidfd_available=pidfd,
        )
    if handle is None:
        # The pid holds no process at all, which is the end a stop asks for.
        return _confirmed(STOP_CONFIRMED_ALREADY_EXITED, pidfd_available=pidfd)
    try:
        exited = handle.has_exited()
        if exited is None:
            return _refusal(
                STOP_UNCONFIRMED_HANDLE,
                EntryError(
                    "REMOTE_PROTOCOL_INVALID",
                    f"owner liveness could not be read through its handle pid={receipt.pid}",
                ),
                state="unknown",
                token_available=True,
                pidfd_available=pidfd,
            )
        if exited:
            return _confirmed(STOP_CONFIRMED_ALREADY_EXITED, pidfd_available=pidfd)
        try:
            handle.send_terminate()
        except ProcessHandleUnavailable as error:
            return _refusal(
                STOP_UNCONFIRMED_HANDLE,
                EntryError("REMOTE_PROTOCOL_INVALID", str(error), reason=error.reason),
                state="live",
                token_available=True,
                pidfd_available=pidfd,
            )
        return _await_handle_exit(handle, receipt, wait)
    finally:
        handle.close()
