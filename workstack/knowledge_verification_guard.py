"""At most one source verification at a time, in one owner incarnation.

The R21 wiring contract gives the owner process a single, dedicated, in-memory
gate for read-time source verification, and this module is that gate and
nothing else. It is deliberately *not* the execution attempt guard: a search
attempt is spent once and never returned, while a verification is a read-only
observation a user may repeat, so the two must not share a registry, a bound or
a vocabulary. Registering a verification here would consume nothing of the
search surface's, and consuming a search attempt would be a lie about what this
check did.

**Three states, and the third one is terminal.**

*Idle* is the ordinary state: the next explicit owner check may proceed.
*Active* means one check already holds the gate, and a second one is refused
:data:`VERIFICATION_BUSY` rather than being queued -- an owner asking twice at
once is asking the same question twice, not asking for two children.
*Unsettled* is what this owner says after a bounded exchange whose cleanup
could not be confirmed: a child may still be running, holding handles this
process can no longer account for, and piling a second child onto an unresolved
one is exactly what must not happen. That state is latched for the life of the
instance -- there is no timeout, no reset and no administrative clear -- so
every later check is refused :data:`VERIFICATION_CLEANUP_UNSETTLED` until the
owner restarts. A new server incarnation has a new guard, which is the only way
back, and saying so is more honest than guessing that the child has since died.

**A settled failure releases.** Every outcome the owner *can* describe -- a
refused result, a driver that never started, an authority that moved while the
child ran -- returns the gate to idle, because the user may reasonably run a
new read-only check after seeing one. Nothing here retries on its own.

**The gate carries no content.** No capture identifier, revision, digest,
verification nonce, evidence handle, command, environment, timestamp or count
is stored, and the two refusals carry a closed code and nothing else. What this
object knows is whether a check is in flight and whether a child was left
unaccounted for, which is the whole of what it is asked.

This is a trusted internal helper, not an authentication boundary. It proves no
authority whatsoever: the caller must have admitted the capture, the ledger and
the operator's configuration against current Store state before it acquires,
and must acquire before it starts a child.

**Release belongs to the caller that acquired.** There is no ownership token
here, because the one caller is in this process and this is not a boundary that
must defend itself against itself. What that buys is simplicity, and what it
costs is one rule the caller keeps: only the invocation whose :meth:`acquire`
returned may call :meth:`settle`. See that method for how the runtime keeps it.
"""

from __future__ import annotations

import threading

__all__ = (
    "VERIFICATION_BUSY",
    "VERIFICATION_CLEANUP_UNSETTLED",
    "KnowledgeVerificationGuard",
    "KnowledgeVerificationGuardError",
)


#: One owner check is already in flight. The answer is "not now", and it says
#: nothing about whether the other check will succeed.
VERIFICATION_BUSY = "verification_busy"

#: A previous exchange's cleanup could not be confirmed, so this owner cannot
#: say that no child is still running. Latched until the owner restarts.
VERIFICATION_CLEANUP_UNSETTLED = "verification_cleanup_unsettled"


class KnowledgeVerificationGuardError(ValueError):
    """Closed refusal: one code, and no submitted value of any kind."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KnowledgeVerificationGuard:
    """One owner incarnation's verification gate. Instances share nothing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._unsettled = False

    @property
    def active(self) -> bool:
        """Whether a check currently holds the gate."""

        with self._lock:
            return self._active

    @property
    def unsettled(self) -> bool:
        """Whether this instance has latched an unconfirmed child cleanup."""

        with self._lock:
            return self._unsettled

    def acquire(self) -> None:
        """Take the gate for one check, or refuse with a closed code.

        Returns only once the idle-to-active transition has been made under the
        lock, so a caller that reaches the next line holds the only gate and no
        concurrent caller can also hold it. The latched state is checked first:
        an owner that cannot account for an earlier child does not get to start
        another one merely because that child's slot looks free.
        """

        with self._lock:
            if self._unsettled:
                raise KnowledgeVerificationGuardError(VERIFICATION_CLEANUP_UNSETTLED)
            if self._active:
                raise KnowledgeVerificationGuardError(VERIFICATION_BUSY)
            self._active = True

    def latch_unsettled(self) -> None:
        """Record that a child's cleanup could not be confirmed. One way.

        Called by the caller that ran the exchange, on the one branch where the
        bounded transport reports anything other than a settled cleanup. After
        this, every :meth:`acquire` on this instance refuses, for the life of
        the instance: there is no expiry and no way to clear it, because this
        process has no evidence on which it could honestly decide the child is
        gone. It is idempotent, so a caller may latch and then settle without
        reopening the gate.
        """

        with self._lock:
            self._unsettled = True
            self._active = False

    def settle(self) -> None:
        """Release the gate **this caller acquired**, after any settled outcome.

        Call this only from the invocation whose :meth:`acquire` returned. It
        is not a general "make the gate idle" operation and it is not safe to
        call from a caller that was refused: this object holds no ownership
        token -- deliberately, because it is a trusted internal helper and not
        a boundary that has to defend itself against its own process -- so a
        non-owner calling it would clear the *holder's* flag and let a second
        child start beside a running one.

        The discipline that makes that unreachable belongs to the caller, and
        :func:`workstack.knowledge_verification_runtime.verify_capture_source`
        keeps it by acquiring **outside** the ``try`` whose ``finally`` settles:
        a refused acquire never enters that block, so a busy or latched check
        cannot release the check that is actually running.

        A latched instance stays latched.
        """

        with self._lock:
            if not self._unsettled:
                self._active = False
