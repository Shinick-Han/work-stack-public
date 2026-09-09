"""One bounded vocabulary for what a remote stop request actually achieved.

Spawning a stop command is not a shutdown.  The 1.0.8 incident ended with an
owner that outlived the GUI while every layer above it had already reported
success, because the only thing anybody checked was that a process had been
launched.  This module is the type that makes the difference sayable: a
symbolic code for what happened, and -- kept deliberately apart from each
other -- one piece of evidence per independent observation.

Process exit, listener release and lease release are three separate questions.
A confirmed exit is not a confirmed port release, and neither is a confirmed
lease release; each carries ``verified``, ``failed`` or ``unknown`` on its own
and nothing here derives one from another.  ``unknown`` is a legitimate answer
and is what an absent fact normalizes to; it is never upgraded by inference.

Missing session token, a legacy unfenced receipt, a foreign host, an
unavailable pidfd and a stop that ran but could not be confirmed are five
different codes, because the operator step that clears each one is different.

The decoder at the bottom is an untrusted boundary.  A remote may emit
anything, so every field is length bounded and value allowlisted, every
unrecognised value normalizes to ``unknown``, and free text is stripped to
printable ASCII before it can reach a log line or a view.  Allowlisting each
field is not enough on its own: a payload whose fields contradict each other,
or whose claim disagrees with the exit status of the command that printed it,
is read as unreadable rather than as the one field a caller wanted, so neither
a broken nor a hostile remote can talk this desktop into a confirmed stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Literal


SCHEMA_VERSION = "remote-stop-result/1"
# The whole payload is one short line. Anything larger is not this contract.
MAX_RESULT_BYTES = 1024
MAX_DETAIL_LENGTH = 200

# Budgets the desktop spends on a stop. The first request may travel to a host
# that is refusing connections, and the confirmation pass runs on the window
# close path behind a bounded join, so the pair has to stay well inside it.
STOP_REQUEST_TIMEOUT_SECONDS = 8.0
STOP_CONFIRM_TIMEOUT_SECONDS = 3.0
# Loopback connect budget for observing whether the owned forward port is free.
LISTENER_PROBE_TIMEOUT_SECONDS = 0.25

Evidence = Literal["verified", "failed", "unknown"]
EVIDENCE_VALUES: tuple[str, ...] = ("verified", "failed", "unknown")

# The owner states the shared snapshot names. "stopping" is the honest label
# for an owner that was signalled and has not yet been seen to go.
OwnerState = Literal["live", "stopping", "dead", "foreign", "unfenced", "unknown"]
OWNER_STATE_VALUES: tuple[str, ...] = (
    "live",
    "stopping",
    "dead",
    "foreign",
    "unfenced",
    "unknown",
)

# --- codes ----------------------------------------------------------------
# Confirmed: the recorded owner process is provably gone.
STOP_CONFIRMED_EXIT = "stop_confirmed_exit"
STOP_CONFIRMED_ALREADY_EXITED = "stop_confirmed_already_exited"
STOP_CONFIRMED_WITHOUT_SIGNAL = "stop_confirmed_without_signal"
STOP_RECEIPT_ABSENT = "stop_receipt_absent"
# Refused: this caller may not act on that receipt, and nothing was touched.
STOP_REFUSED_TOKEN_MISMATCH = "stop_refused_token_mismatch"
STOP_REFUSED_WORKSPACE_MISMATCH = "stop_refused_workspace_mismatch"
STOP_REFUSED_FOREIGN_HOST = "stop_refused_foreign_host"
STOP_REFUSED_UNFENCED_RECEIPT = "stop_refused_unfenced_receipt"
STOP_REFUSED_REPLACED_PID = "stop_refused_replaced_pid"
STOP_REFUSED_RECEIPT_INVALID = "stop_refused_receipt_invalid"
STOP_REFUSED_GUARD = "stop_refused_guard"
# Requested but unconfirmed: something was attempted and did not prove an exit.
STOP_AMBIGUOUS_LIVENESS = "stop_ambiguous_liveness"
STOP_UNCONFIRMED_TIMEOUT = "stop_unconfirmed_timeout"
STOP_UNCONFIRMED_HANDLE = "stop_unconfirmed_handle"
STOP_UNCONFIRMED_NO_PIDFD = "stop_unconfirmed_no_pidfd"
# Never requested: the desktop could not even ask.
STOP_NOT_REQUESTED_NO_TOKEN = "stop_not_requested_no_token"
STOP_NOT_REQUESTED_NO_PROFILE = "stop_not_requested_no_profile"
# The request left this machine but its outcome did not come back readable.
STOP_REQUEST_FAILED = "stop_request_failed"
STOP_REQUEST_TIMED_OUT = "stop_request_timed_out"
STOP_RESULT_ABSENT = "stop_result_absent"
STOP_RESULT_UNREADABLE = "stop_result_unreadable"

STOP_CODES: tuple[str, ...] = (
    STOP_CONFIRMED_EXIT,
    STOP_CONFIRMED_ALREADY_EXITED,
    STOP_CONFIRMED_WITHOUT_SIGNAL,
    STOP_RECEIPT_ABSENT,
    STOP_REFUSED_TOKEN_MISMATCH,
    STOP_REFUSED_WORKSPACE_MISMATCH,
    STOP_REFUSED_FOREIGN_HOST,
    STOP_REFUSED_UNFENCED_RECEIPT,
    STOP_REFUSED_REPLACED_PID,
    STOP_REFUSED_RECEIPT_INVALID,
    STOP_REFUSED_GUARD,
    STOP_AMBIGUOUS_LIVENESS,
    STOP_UNCONFIRMED_TIMEOUT,
    STOP_UNCONFIRMED_HANDLE,
    STOP_UNCONFIRMED_NO_PIDFD,
    STOP_NOT_REQUESTED_NO_TOKEN,
    STOP_NOT_REQUESTED_NO_PROFILE,
    STOP_REQUEST_FAILED,
    STOP_REQUEST_TIMED_OUT,
    STOP_RESULT_ABSENT,
    STOP_RESULT_UNREADABLE,
)

# Codes whose meaning is "this caller has no standing to act on that receipt",
# as opposed to "the stop ran and could not be confirmed". The operator step
# differs, so the two families never share a code.
REFUSAL_CODES = frozenset(
    {
        STOP_REFUSED_TOKEN_MISMATCH,
        STOP_REFUSED_WORKSPACE_MISMATCH,
        STOP_REFUSED_FOREIGN_HOST,
        STOP_REFUSED_UNFENCED_RECEIPT,
        STOP_REFUSED_REPLACED_PID,
        STOP_REFUSED_RECEIPT_INVALID,
        STOP_REFUSED_GUARD,
    }
)
# Codes that mean no stop request was ever issued for this session's owner.
NOT_REQUESTED_CODES = frozenset(
    {STOP_NOT_REQUESTED_NO_TOKEN, STOP_NOT_REQUESTED_NO_PROFILE}
)
# Owner states that positively assert the owner has NOT been seen to go. Each
# one contradicts a proven exit; "unknown" does not, because it asserts
# nothing at all.
LIVING_STATES = frozenset({"live", "stopping", "foreign", "unfenced"})
# Codes that carry a proven exit of the recorded owner process. An absent
# receipt is deliberately NOT one of them: nothing claims this data directory,
# which is consistent with an owner that exited and cleaned up and equally
# consistent with a receipt that was never written or was removed by hand, and
# no process was observed either way. Absent is therefore an unconfirmed
# outcome and its exit status says so.
CONFIRMED_CODES = frozenset(
    {
        STOP_CONFIRMED_EXIT,
        STOP_CONFIRMED_ALREADY_EXITED,
        STOP_CONFIRMED_WITHOUT_SIGNAL,
    }
)
# Codes that can only be reached through a pidfd-bound handle, and codes that
# can only be reached where no pidfd exists. Every honest emitter of a
# confirmed exit says which of the two it was, and the two are exclusive:
#
#   stop_confirmed_exit          watched to exit through the handle that was
#                                signalled, so the host had pidfd signalling.
#   stop_confirmed_without_signal  the no-pidfd observation path, which sends
#                                nothing at all and hard-codes pidfd False.
#   stop_confirmed_already_exited  reached from either path -- the pid held no
#                                process, or the classification already read it
#                                as dead -- so it constrains no capability and
#                                is deliberately in neither set.
PIDFD_SIGNALLED_CODES = frozenset({STOP_CONFIRMED_EXIT})
NO_PIDFD_CODES = frozenset({STOP_CONFIRMED_WITHOUT_SIGNAL})

# Process exit codes the remote entry point may return per family. 0 and 2 are
# what 1.0.13 already returns, so an unchanged remote stays readable; 3 and 4
# split "refused" from "ran but unconfirmed", which 1.0.13 collapses into 2.
EXIT_CONFIRMED = 0
EXIT_LEGACY_ERROR = 2
EXIT_REFUSED = 3
EXIT_UNCONFIRMED = 4


def exit_code_for(code: str) -> int:
    """The process exit status a remote stop-owned run should return."""

    if code in REFUSAL_CODES:
        return EXIT_REFUSED
    if code in CONFIRMED_CODES:
        return EXIT_CONFIRMED
    return EXIT_UNCONFIRMED


def sanitize_detail(value: object) -> str:
    """Reduce any remote or exception text to short printable ASCII.

    Detail strings reach trace logs and, through the coordinator's snapshot,
    a view.  A remote is not trusted to stay inside this contract, so control
    characters, newlines and non-ASCII are replaced rather than escaped later,
    and the result is truncated.  Nothing here is ever a command.
    """

    if not isinstance(value, str) or not value:
        return ""
    kept = "".join(character if 32 <= ord(character) < 127 else "." for character in value)
    return kept[:MAX_DETAIL_LENGTH].strip()


def _evidence(value: object) -> Evidence:
    return value if value in EVIDENCE_VALUES else "unknown"  # type: ignore[return-value]


def _owner_state(value: object) -> OwnerState:
    return value if value in OWNER_STATE_VALUES else "unknown"  # type: ignore[return-value]


def _tristate(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class StopResult:
    """What one stop request achieved, with each observation kept separate.

    ``code`` says which condition was met.  The three evidence fields are
    independent: nothing in this module infers a listener or lease release
    from a confirmed process exit, because a released process and a released
    port are two facts and the incident is exactly what happens when they are
    treated as one.
    """

    code: str
    state: OwnerState = "unknown"
    token_available: bool | None = None
    pidfd_available: bool | None = None
    process_exit: Evidence = "unknown"
    listener_release: Evidence = "unknown"
    lease_release: Evidence = "unknown"
    # The desktop's own forward port, kept deliberately apart from
    # ``listener_release``. Closing the local SSH process frees the forward
    # while the remote owner and its original listener can both survive -- the
    # 1.0.8 incident exactly -- so this is a different fact about a different
    # listener on a different host, it never populates the contract field, and
    # it is never projected into the shared snapshot. It is also never encoded
    # or decoded: a remote cannot report the desktop's own port.
    forward_listener_release: Evidence = "unknown"
    detail: str = ""

    @property
    def requested(self) -> bool:
        """True when a stop was actually asked for, whatever came of it."""

        return self.code not in NOT_REQUESTED_CODES

    @property
    def confirmed(self) -> bool:
        """True only for a proven exit of the recorded owner process."""

        return self.process_exit == "verified"

    @property
    def refused(self) -> bool:
        return self.code in REFUSAL_CODES

    def owner_facts(self) -> dict[str, object]:
        """Project onto exactly the six owner keys the shared snapshot names.

        This is a projection of existing typed evidence, not a second UI
        contract: the caller that builds the snapshot owns the rest of it.
        """

        return {
            "state": self.state,
            "token_available": self.token_available,
            "pidfd_available": self.pidfd_available,
            "process_exit": self.process_exit,
            "listener_release": self.listener_release,
            "lease_release": self.lease_release,
        }

    def with_observations(
        self,
        *,
        listener_release: object = None,
        lease_release: object = None,
        forward_listener_release: object = None,
    ) -> "StopResult":
        """Attach an independently measured listener or lease observation.

        Only a real measurement may be attached.  Passing ``None`` leaves the
        field where it was, so a caller with no observer cannot accidentally
        overwrite a measured fact with a default.

        ``listener_release`` is the contract's original-listener evidence and
        only a caller that observed *that* listener may set it.  A desktop
        that measured its own forward port sets ``forward_listener_release``
        instead; the two are not interchangeable.
        """

        changes: dict[str, object] = {}
        if listener_release is not None:
            changes["listener_release"] = _evidence(listener_release)
        if lease_release is not None:
            changes["lease_release"] = _evidence(lease_release)
        if forward_listener_release is not None:
            changes["forward_listener_release"] = _evidence(forward_listener_release)
        return replace(self, **changes) if changes else self


def normalized(result: StopResult) -> StopResult:
    """Coerce every field onto the allowlist, however the value was built."""

    return StopResult(
        code=result.code if result.code in STOP_CODES else STOP_RESULT_UNREADABLE,
        state=_owner_state(result.state),
        token_available=_tristate(result.token_available),
        pidfd_available=_tristate(result.pidfd_available),
        process_exit=_evidence(result.process_exit),
        listener_release=_evidence(result.listener_release),
        lease_release=_evidence(result.lease_release),
        forward_listener_release=_evidence(result.forward_listener_release),
        detail=sanitize_detail(result.detail),
    )


def encode_stop_result(result: StopResult) -> bytes:
    """One bounded single line, the only thing a stop-owned run prints."""

    checked = normalized(result)
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "code": checked.code,
            "state": checked.state,
            "token_available": checked.token_available,
            "pidfd_available": checked.pidfd_available,
            "process_exit": checked.process_exit,
            "listener_release": checked.listener_release,
            "lease_release": checked.lease_release,
            "detail": checked.detail,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if len(payload) > MAX_RESULT_BYTES:
        return encode_stop_result(replace(checked, detail=""))
    return payload


def _unreadable(detail: str) -> StopResult:
    return StopResult(code=STOP_RESULT_UNREADABLE, detail=sanitize_detail(detail))


def decode_stop_result(payload: object) -> StopResult | None:
    """Read one emitted line, or None when this is not that contract at all.

    Returning None rather than a guess lets the caller keep whatever it knew
    from the exit status; a payload that is present but wrong is a different
    fact and comes back as ``stop_result_unreadable``.
    """

    if payload is None:
        return None
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "replace")
    if not isinstance(payload, (bytes, bytearray)):
        return None
    raw = bytes(payload).strip()
    if not raw:
        return None
    if len(raw) > MAX_RESULT_BYTES:
        return _unreadable("stop result payload is larger than this contract")
    # Only the last line matters: a remote may have written unrelated noise
    # first, and that noise is never parsed and never surfaced.
    line = raw.splitlines()[-1]
    try:
        body = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _unreadable("stop result payload is not this contract")
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
        return _unreadable("stop result payload is not this contract")
    code = body.get("code")
    if code not in STOP_CODES:
        return _unreadable("stop result code is not in this contract")
    return StopResult(
        code=str(code),
        state=_owner_state(body.get("state")),
        token_available=_tristate(body.get("token_available")),
        pidfd_available=_tristate(body.get("pidfd_available")),
        process_exit=_evidence(body.get("process_exit")),
        listener_release=_evidence(body.get("listener_release")),
        lease_release=_evidence(body.get("lease_release")),
        detail=sanitize_detail(body.get("detail")),
    )


def _confirmed_family_contradiction(result: StopResult) -> str:
    """Check one confirmed record against the evidence its emitter must carry.

    A confirmed code is the only claim strong enough to be read as a shutdown,
    so it is the one family checked against every field that distinguishes it
    rather than against ``process_exit`` alone.
    """

    if result.process_exit != "verified":
        return "a confirmed code was reported without a proven exit"
    if result.state in LIVING_STATES:
        return f"a proven exit was reported for an owner said to be {result.state}"
    if result.token_available is not True:
        # Every confirmed code is emitted downstream of the token admission:
        # the receipt guard, the session token and the host/boot fencing are
        # all behind the caller before any process is observed, so a confirmed
        # exit always carries an admitted token. A confirmed exit whose token
        # was refused, or that never reports one at all, is a claim about a
        # process this session was never shown it had standing over. A-R5
        # forbids reading liveness out of a token mismatch, and this is that
        # same reading in the other direction.
        return "a proven exit was reported without the token that authorized it"
    if result.code in NO_PIDFD_CODES and result.pidfd_available is True:
        return (
            "a stop confirmed without any signal was reported by a host that "
            "says it has pidfd signalling"
        )
    if result.code in PIDFD_SIGNALLED_CODES and result.pidfd_available is False:
        return (
            "an exit watched through a signalled handle was reported by a host "
            "that says it has no pidfd signalling"
        )
    return ""


def report_contradiction(result: StopResult) -> str:
    """Name the first internal contradiction in one reported outcome.

    The decoder allowlists every field on its own, which is not enough: a
    remote -- broken, older, or hostile -- can emit a combination no honest
    stop can produce, such as an unconfirmed code beside ``process_exit``
    ``verified``, or a confirmed code beside an owner said to be live.  A
    reader that trusts only the evidence field would read the first as a
    shutdown, which is the incident's mistake in a new place.

    Only positive assertions can contradict each other.  ``unknown`` claims
    nothing, so a proven exit beside an owner state nobody filled in is
    under-specified rather than inconsistent and is admitted with the state
    left unknown; a proven exit beside an owner said to be live is not.
    Returning the reason rather than a bool keeps the refusal reportable in a
    bounded detail line.  An empty string means the record is self-consistent;
    it does not mean the record is true.

    The confirmed family is checked against every piece of evidence that
    distinguishes it, not only against ``process_exit``, because those are the
    records that can talk a caller into reporting a shutdown.  A confirmed exit
    is emitted only downstream of the token admission, so it carries an
    admitted token; ``stop_confirmed_without_signal`` is emitted only where no
    pidfd exists, and ``stop_confirmed_exit`` only through a handle that was
    signalled, so each names the capability it required.  The token is the one
    place where the absence of an assertion is itself the contradiction: an
    honest confirmation always reports the token it was granted, and a
    confirmed exit is too strong a claim to accept from a record that will not
    say this session had standing to make it.  ``pidfd_available`` keeps the
    ordinary rule -- only a positive claim of the wrong capability contradicts
    -- because a remote that never measured it says nothing either way.
    """

    if result.code in CONFIRMED_CODES:
        return _confirmed_family_contradiction(result)
    if result.process_exit == "verified":
        return "a proven exit was reported under a code that confirms nothing"
    if result.code in REFUSAL_CODES and result.state == "dead":
        return "a refusal was reported for an owner said to be dead"
    if result.code in NOT_REQUESTED_CODES and result.state == "dead":
        return "an owner said to be dead was reported for a stop never asked for"
    return ""


def admitted_report(reported: StopResult) -> StopResult:
    """Admit one reported outcome only while it contradicts nothing it says.

    Used where an outcome arrives already decoded -- the host seam may hand
    back a ``StopResult`` directly -- so there is no exit status to corroborate
    it against.  A contradictory record becomes unreadable, which claims no
    exit, rather than being trusted for the one field a caller reads.
    """

    contradiction = report_contradiction(reported)
    if contradiction:
        return _unreadable(f"stop result is inconsistent: {contradiction}")
    return reported


def reconcile_reported_outcome(reported: StopResult, returncode: object) -> StopResult:
    """Read a remote-emitted outcome only where its own exit status agrees.

    The payload does not override the status.  A stop command that exited
    nonzero while its stdout reports a confirmed exit is contradicting itself,
    and this desktop cannot tell which half is true, so the disagreement is
    the fact: the outcome comes back unreadable and nothing is confirmed.
    Agreement is checked against :func:`exit_code_for`, the same mapping the
    remote entry point is to return, so every family is covered rather than
    only ``0``.

    A payload with no integer status behind it has nothing to corroborate it
    at all and is treated the same way.
    """

    checked = admitted_report(reported)
    if checked.code == STOP_RESULT_UNREADABLE:
        return checked
    if not isinstance(returncode, int):
        return _unreadable("stop result was reported with no exit status behind it")
    expected = exit_code_for(checked.code)
    if returncode != expected:
        return _unreadable(
            f"stop command exited {returncode} while reporting {checked.code}, "
            f"which exits {expected}"
        )
    return checked


def _first_text(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if not isinstance(value, str):
        return ""
    lines = [line for line in value.splitlines() if line.strip()]
    return lines[0] if lines else ""


def result_from_launch(
    *,
    returncode: object,
    stdout: object = None,
    stderr: object = None,
    timed_out: bool = False,
    launch_error: object = None,
) -> StopResult:
    """Turn one stop-command launch into an outcome, never into a success.

    A launch that returned is evidence about the launch.  It becomes evidence
    about the owner only when the remote emitted this contract and said so.
    A remote that emitted nothing leaves ``process_exit`` unknown even after
    exit status 0, because a status is a statement about the command that ran
    and this desktop has still not observed the owner either way.

    A remote that did emit the contract still does not get to overrule its own
    exit status: the payload is reconciled against it, and a disagreement or
    an internally contradictory record confirms nothing.
    """

    if launch_error is not None:
        return StopResult(code=STOP_REQUEST_FAILED, detail=sanitize_detail(str(launch_error)))
    if timed_out:
        return StopResult(
            code=STOP_REQUEST_TIMED_OUT,
            detail="the stop request did not return within its budget",
        )
    decoded = decode_stop_result(stdout)
    if decoded is not None:
        return reconcile_reported_outcome(decoded, returncode)
    if not isinstance(returncode, int):
        return StopResult(
            code=STOP_RESULT_ABSENT,
            detail="the stop request reported no exit status",
        )
    if returncode == EXIT_CONFIRMED:
        return StopResult(
            code=STOP_RESULT_ABSENT,
            detail="stop command exited 0 without reporting an owner observation",
        )
    reported = sanitize_detail(_first_text(stderr))
    return StopResult(
        code=STOP_RESULT_ABSENT,
        detail=(reported or f"stop command exited {returncode}"),
    )


def merge_confirmation(first: StopResult, confirmation: StopResult) -> StopResult:
    """Fold a later confirmation pass into the outcome of the first request.

    The second pass observes; it does not re-request.  A confirmation that
    proves the exit replaces an unconfirmed first outcome, keeping whatever
    the first pass had already established about the token, the pidfd and the
    two releases.  A confirmation that proves nothing never downgrades
    evidence the first pass had already won.
    """

    if first.confirmed or not confirmation.confirmed:
        return first
    merged = replace(
        confirmation,
        token_available=(
            confirmation.token_available
            if confirmation.token_available is not None
            else first.token_available
        ),
        pidfd_available=(
            confirmation.pidfd_available
            if confirmation.pidfd_available is not None
            else first.pidfd_available
        ),
        listener_release=(
            confirmation.listener_release
            if confirmation.listener_release != "unknown"
            else first.listener_release
        ),
        lease_release=(
            confirmation.lease_release
            if confirmation.lease_release != "unknown"
            else first.lease_release
        ),
        forward_listener_release=(
            confirmation.forward_listener_release
            if confirmation.forward_listener_release != "unknown"
            else first.forward_listener_release
        ),
    )
    # Both halves were admitted on their own, but carrying the first pass's
    # capability forward can still land a confirmed code beside evidence that
    # contradicts it -- a stop confirmed without any signal, wearing the pidfd
    # the first pass reported. A merge that contradicts itself has proved
    # nothing, so the honest unconfirmed outcome stands rather than a
    # confirmation assembled here.
    if report_contradiction(merged):
        return first
    return merged


def summarize(result: StopResult) -> str:
    """One bounded trace line. Never says "stopped" without a proven exit."""

    checked = normalized(result)
    parts = [
        f"remote stop {checked.code}",
        f"owner={checked.state}",
        f"exit={checked.process_exit}",
        f"listener={checked.listener_release}",
        f"lease={checked.lease_release}",
    ]
    if checked.forward_listener_release != "unknown":
        # Named as the desktop's own port so a log line can never be read as
        # the original remote listener the contract asks about.
        parts.append(f"forward-port={checked.forward_listener_release}")
    if checked.pidfd_available is False:
        parts.append("pidfd=unavailable")
    if checked.token_available is False:
        parts.append("token=not-owner")
    if checked.detail:
        parts.append(checked.detail)
    return "; ".join(parts)
