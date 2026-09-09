"""Release one captured attempt and report what the stop actually proved.

Closing a remote connection is several independent effects and several
independent observations, and this module keeps them apart.  Asking the owner
to stop is one effect; the owner's exit, the release of the local forward
listener and the release of the writer lease are three separate questions, and
a return code from the stop command answers none of them.  So the request's
outcome is kept rather than dropped, an unconfirmed stop earns one bounded
second look after this desktop has closed the channel that authenticated it --
after that close has actually finished, not merely been claimed by somebody --
and the listener is observed on its own or left unknown.

The order inside a release is deliberate and unchanged: ask while the
authenticating channel is still up, then close that channel and the log.  Each
of those effects is spent through the captured bundle's one-shot claim from
``remote_attempt_bundle``, so a close, a stale waiter and a failure cleanup
racing for the same attempt cannot stop the same owner twice.

Two invariants keep an answer from being read wider than it is.  One close can
release several captured bundles, and the close has stopped the connection only
when every one of them proved an exit, so any unresolved bundle decides the
aggregate rather than being masked by a bundle that confirmed.  And every
publication carries the attempt generation that earned it, so a stale cleanup
finishing after a newer attempt has started cannot overwrite the newer
attempt's result with a dead owner's.

The port this module observes is the desktop's own forward, recorded as
``forward_listener_release``.  It is never the contract's original-listener
evidence: closing the local SSH process frees the forward while the remote
owner and its listener can both survive, and that is the 1.0.8 incident, not a
release.

Everything here works from the frozen bundle it is handed.  It never touches
the gate and never detaches a host field: ``remote_attempt_resources`` owns
those and calls in.  The one host commit it does make is the stop outcome, and
that commit takes the host's single lifecycle lock -- the same one every start
and reset takes -- because admitting a generation and writing the result under
it is one decision and has to be one critical section.  The accessor for that
lock lives here, below its other caller, so both halves of the protocol reach
one lock and the import graph stays acyclic.  Nothing here renders a result;
the outcome published on the host is typed internal evidence for whoever builds
the shared update snapshot, and the trace never says the owner stopped without
a proven exit.
"""

from __future__ import annotations

import errno
import socket
import subprocess
from typing import Sequence

from remote_attempt_bundle import (
    CLAIM_LOG,
    CLAIM_PROCESS,
    CLAIM_STOP_CONFIRM,
    CLAIM_STOP_OWNED,
    RemoteAttemptResources,
)
from remote_command_contract import LOOPBACK_HOST
from remote_stop_result import (
    LISTENER_PROBE_TIMEOUT_SECONDS,
    STOP_NOT_REQUESTED_NO_PROFILE,
    STOP_NOT_REQUESTED_NO_TOKEN,
    STOP_REQUEST_FAILED,
    STOP_REQUEST_TIMED_OUT,
    STOP_RESULT_ABSENT,
    STOP_UNCONFIRMED_NO_PIDFD,
    STOP_UNCONFIRMED_TIMEOUT,
    StopResult,
    admitted_report,
    merge_confirmation,
    result_from_launch,
    summarize,
)
from ssot_connection import stop_result_from_completed


def uninitialized_protocol(name: str) -> RuntimeError:
    return RuntimeError(
        f"remote attempt protocol used before {name} was published; "
        "call initialize_attempt_resources(host) in the constructor"
    )


def resource_lock_for(host: object):
    """The host's one lifecycle lock, the only lock this protocol has.

    Published once by ``initialize_attempt_resources`` before any thread can
    reach the protocol.  There is deliberately no first-use fallback: two
    callers must never enter the protocol under two different locks, which is
    exactly what a stale publication and a fresh start racing for the outcome
    field would then be doing.
    """

    lock = getattr(host, "remote_resource_lock", None)
    if lock is None:
        raise uninitialized_protocol("remote_resource_lock")
    return lock


def close_captured_log(log: object) -> None:
    closer = getattr(log, "close", None) if log is not None else None
    if callable(closer):
        closer()


def stop_result_from_request(value: object, session_token: object = None) -> StopResult:
    """Read what the host's stop request reported, or record that it did not.

    The host method is the seam the coordinator wires and it may hand back
    either shape: an already-decoded outcome, or the raw completed process the
    stop command produced, which is normalized here.  A host that returns
    nothing at all -- the 1.0.13 shape -- reports no success and no failure:
    it is one stop request whose outcome this desktop never learned, and it is
    recorded as such so nothing downstream can read a spawned command as a
    shutdown.

    An exit status of zero is not promoted to anything on its own.  It says
    the command ran; the owner's exit, its port and its lease are three other
    questions and none of them is answered by a return code.
    """

    if isinstance(value, StopResult):
        # Already decoded, so there is no exit status to reconcile it against,
        # but a record that contradicts itself is still not readable as a stop.
        return admitted_report(value)
    if isinstance(getattr(value, "returncode", None), int):
        return stop_result_from_completed(value, session_token)
    return result_from_launch(returncode=None)


def _settle_bundle_outcome(
    resources: RemoteAttemptResources, outcome: StopResult
) -> StopResult:
    """Record this outcome for the bundle and take back whatever now stands.

    One captured bundle can be released by two callers, and a proven exit one
    of them earned must survive the other finishing afterwards with less. The
    claim keeps and returns in one operation, so there is no window between
    reading what stands and writing this answer over it, and it is keyed by
    the captured bundle: an in-flight bundle shares the running attempt's
    claim, and those two are different owners whose evidence never merges.
    """

    standing = resources.claim.settle_stop_outcome(resources, outcome)
    return standing if isinstance(standing, StopResult) else outcome


def _release_stop_owned(
    host: object, resources: RemoteAttemptResources, trace: object
) -> StopResult:
    """Ask the remote to stop this attempt's owner and keep what came back.

    A missing token and a missing profile are separate facts and neither is a
    stop: the first is the 1.0.8 shape where the owner outlives the session
    that could have proved authority over it, and saying so is what lets the
    layer above offer the one operator step that clears it.
    """

    if not resources.token:
        return StopResult(
            code=STOP_NOT_REQUESTED_NO_TOKEN,
            token_available=False,
            detail="no session token for this attempt, so no stop could be authorized",
        )
    if resources.profile is None:
        return StopResult(
            code=STOP_NOT_REQUESTED_NO_PROFILE,
            token_available=True,
            detail="no remote profile for this attempt, so no stop could be addressed",
        )
    if not resources.claim.take(CLAIM_STOP_OWNED):
        # Another release already spent this attempt's one stop request. What
        # it established belongs to this bundle rather than to that caller:
        # the confirmation pass is earned by whichever release actually closes
        # the channel, and that may not be the one that asked. If the asking
        # release has not published yet, this one really has observed nothing.
        shared = resources.claim.stop_outcome(resources)
        return shared if isinstance(shared, StopResult) else result_from_launch(returncode=None)
    try:
        reported = host._request_remote_stop_owned(resources.profile, resources.token)
    except subprocess.TimeoutExpired:
        # Clarification 1: the host seam propagates TimeoutExpired rather than
        # swallowing it, and a request that never came back inside its budget
        # is not a request that could not be run. The command did leave this
        # machine and may still be doing something on the remote, so it gets
        # the timeout code and not the launch-failure one: the operator step
        # for an unreachable host differs from the one for a missing ssh.
        # TimeoutExpired is a SubprocessError, so this must be caught first.
        if callable(trace):
            trace("stop-owned request timed out")
        outcome = StopResult(
            code=STOP_REQUEST_TIMED_OUT,
            token_available=True,
            detail="the stop request did not return within its budget",
        )
        return _settle_bundle_outcome(resources, outcome)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        if callable(trace):
            trace("stop-owned request failed")
        outcome = StopResult(
            code=STOP_REQUEST_FAILED,
            token_available=True,
            detail="the stop request could not be run",
        )
        return _settle_bundle_outcome(resources, outcome)
    return _settle_bundle_outcome(resources, stop_result_from_request(reported, resources.token))


def observe_forward_listener_release(profile: object) -> str:
    """Is the port this attempt forwarded free again? An independent fact.

    This is the DESKTOP's own forward port and nothing else.  It is not the
    contract's original-listener evidence and must never populate it: closing
    the local SSH process frees the forward while the remote owner and the
    original listener it published can both survive, which is precisely the
    1.0.8 incident.  The caller records it as ``forward_listener_release``, a
    separate desktop-side fact, and leaves ``listener_release`` unknown until
    the original listener is observed on the host that owns it.

    Process exit and port release are two observations and this one is taken
    on its own, never derived from the other.  It is a bounded loopback
    connect to the exact port this attempt forwarded: accepted means something
    is still serving there, which is the visible half of the 1.0.8 incident,
    and refused means the listener is gone.  It sends nothing and never
    touches a port this attempt did not forward.

    A connect that neither completes nor is refused has proved nothing --
    Windows lets a connect to a free loopback port sit in SYN retry well past
    any budget this close path can afford -- so a timeout is not read as a
    release.  Only then, and only then, is the question settled the way this
    module already settles port availability elsewhere: one momentary
    exclusive bind of that same port, released immediately.  The bind race
    that leaves behind is the one the tunnel already handles with
    ExitOnForwardFailure, and it is entered only when the port was not
    answering in the first place.

    The scope is exactly the local forward listener.  The remote listener is a
    different observation on a different host and is not claimed here.
    """

    port = getattr(profile, "local_forward_port", None)
    if not isinstance(port, int) or not 0 < port < 65536:
        return "unknown"
    try:
        with socket.create_connection(
            (LOOPBACK_HOST, port), timeout=LISTENER_PROBE_TIMEOUT_SECONDS
        ):
            return "failed"
    except ConnectionRefusedError:
        return "verified"
    except OSError:
        return _observe_forward_port_by_bind(port)


def _observe_forward_port_by_bind(port: int) -> str:
    """Settle an unanswered port with one momentary bind, or stay unknown.

    Only ``EADDRINUSE`` says something is holding the port.  ``EACCES`` is a
    permission or reservation refusal -- a privileged port, an excluded range,
    a local policy -- and it says this process may not bind here, not that
    anything is listening, so it stays unknown rather than being counted as a
    retained listener.
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK_HOST, port))
        return "verified"
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return "failed"
        return "unknown"


# The first outcomes a second look can actually improve on, and only those.
#
# Each one is a first pass that reached the owner -- signalled it through its
# own handle, or observed it where no pidfd exists -- and could not prove the
# exit for the one reason closing the authenticating channel removes: the owner
# cannot finish leaving while that channel is still holding sockets open on the
# port it serves.  A transport timeout belongs here for the same reason and not
# as a retry: the remote had already begun that decision, and this is the pass
# that reads what became of it.
#
# Nothing else is asked twice, and the exclusions are about authority, not
# about tidiness.  A refusal is a fact about this caller that a second later
# refuses identically.  A launch failure never reached the remote.  An absent
# or unreadable result is a remote that did not answer in this contract, and
# reading a differently shaped second answer as an upgrade over a
# self-contradicting first one is what this contract refuses elsewhere.  And
# an unreadable handle or an ambiguous liveness is a pre-signal refusal about
# this exact pid -- a permission refusal, a failed pin, a /proc this host could
# not read -- which ``remote_owner_stop`` deliberately does not route around;
# asking the same command again could classify that pid live and signal it, so
# those two are never re-asked here either.
CONFIRMABLE_CODES = frozenset(
    {
        STOP_UNCONFIRMED_TIMEOUT,
        STOP_UNCONFIRMED_NO_PIDFD,
        STOP_REQUEST_TIMED_OUT,
    }
)


def _adopt_shared_outcome(
    resources: RemoteAttemptResources, result: StopResult
) -> StopResult:
    """After the close, read what this bundle's own stop request established.

    A release that lost the stop ticket may have looked before the winner had
    published anything, and would then be holding a record that observed
    nothing -- while the release that did observe cannot confirm, because it
    is not the one that closed the channel.  The confirmation is decided after
    the close, so the question is asked again here, at the point where the
    answer is needed.

    Only a record this bundle's own stop request wrote is adopted.  Nothing is
    invented, no other bundle is read, and a release that already has an
    observation of its own keeps it.
    """

    shared = resources.claim.stop_outcome(resources)
    if not isinstance(shared, StopResult):
        return result
    if shared.confirmed and not result.confirmed:
        # The other release already earned this bundle's proven exit. Adopting
        # it here is what keeps this release from asking a question that has
        # been answered -- and from finishing with a record that proves less
        # than the bundle already knows.
        return shared
    return shared if result.code == STOP_RESULT_ABSENT else result


def _confirm_stop_outcome(
    host: object,
    resources: RemoteAttemptResources,
    first: StopResult,
    trace: object,
    *,
    channel_closed: bool,
) -> StopResult:
    """Re-observe the owner once, after this desktop closed its own channel.

    This is the second half of a normal shutdown, and it is not specific to a
    host without pidfd.  Either way the first request is made while the
    channel that authenticates it is still up, and the served process cannot
    finish exiting until that channel is gone: where there is no pidfd nothing
    was signalled at all and the exec/PDEATHSIG binding to the sshd session is
    what ends it, and where there is one the signal has been delivered but the
    shutdown it started still has to close a listener whose request threads
    this desktop's own forward is holding open.  By the time this runs the
    channel has been closed, so the same authenticated question can now be
    answered with a proven exit.

    It never escalates and never widens: same token, same command, same single
    pid bound through its own handle, and it runs at most once per attempt.
    It is not, however, guaranteed to be observation-only -- an owner the
    remote still classifies as live is signalled again by the same authorized
    stop, exactly as the first request signalled it, with the same authority
    and no wider reach.  It is asked only where a second look can change the
    answer (:data:`CONFIRMABLE_CODES`); without the host seam it is skipped
    and the unconfirmed outcome stands, which is the honest report.
    """

    if first.confirmed or not first.requested or not resources.token:
        return first
    if first.code not in CONFIRMABLE_CODES:
        return first
    if not channel_closed:
        # Another release holds this bundle's one process claim and has not
        # finished terminating it, so the channel is still up and this pass
        # would ask the same unanswerable question the first request already
        # asked -- and would spend the one confirmation ticket doing it. The
        # honest unconfirmed outcome stands instead.
        if callable(trace):
            trace("stop-owned confirmation skipped; the owned channel is still open")
        return first
    if resources.profile is None:
        return first
    confirm = getattr(host, "_confirm_remote_stop_owned", None)
    if not callable(confirm) or not resources.claim.take(CLAIM_STOP_CONFIRM):
        return first
    try:
        reported = confirm(resources.profile, resources.token)
    except subprocess.TimeoutExpired:
        # The observation pass proves nothing either way, so the first
        # outcome stands; the trace still separates a budget from a fault.
        if callable(trace):
            trace("stop-owned confirmation pass timed out")
        return first
    except (OSError, subprocess.SubprocessError, RuntimeError):
        if callable(trace):
            trace("stop-owned confirmation pass failed")
        return first
    return merge_confirmation(
        first, stop_result_from_request(reported, resources.token)
    )


def _fire_publish_barrier(host: object, generation: int) -> None:
    """The interleaving point between admitting a generation and writing it.

    Nothing in production installs this hook.  A regression test does, to try
    to start a new attempt in exactly the window where a stale publication
    could once overwrite a newer one.  It fires while the lifecycle lock is
    held, which is what makes that window unreachable rather than merely
    unlikely.
    """

    hook = getattr(host, "_remote_barrier_stop_publish", None)
    if callable(hook):
        hook(generation)


def publish_stop_outcome(
    host: object,
    generation: object,
    result: StopResult,
    trace: object,
    *,
    preserve_proven_exit: bool = False,
) -> StopResult:
    """Publish an outcome on the host under the attempt identity that earned it.

    The field is typed internal evidence for whoever builds the shared update
    snapshot; nothing here renders it and nothing here prints a raw remote
    payload.  The trace never says the owner stopped without a proven exit.

    The publication is stamped with the attempt generation it belongs to and
    only ever moves forward.  A stale cleanup can finish long after a newer
    attempt has started -- the wait that triggers it is deliberately outside
    every lock -- and without the stamp it would overwrite the newer attempt's
    result with a dead owner's, which is how a reopened connection ends up
    projecting an owner that is already gone.  A generation behind the one on
    the field publishes nothing and says so in the trace; the outcome is still
    returned, because it is the truth about the attempt that asked.

    Reading the stamp and writing the pair is ONE decision, so it is one
    critical section under the host's single lifecycle lock -- the same lock
    ``start_remote_attempt`` takes to reset the field and stamp the new
    generation.  Split apart they are not enough: a start landing between the
    two republishes the older attempt's result under the older stamp, and a
    reopened connection is once again told its predecessor is dead with a
    proven exit.  Whichever side wins the lock, the newer generation ends up
    owning the field, because the loser either finds the newer stamp and
    withholds, or is overwritten by the reset that follows it.

    Only the check and the write are held; the trace is emitted outside, so
    this lock is never held across a log sink, and the lock ordering the rest
    of the protocol relies on -- lifecycle lock outermost, nothing blocking
    beneath it -- is unchanged.  The lock is reentrant, so a caller that
    already holds it is not a deadlock; today no caller does, because
    ``cleanup_captured`` releases it before the release path begins.
    """

    current = int(generation) if isinstance(generation, int) else 0
    with resource_lock_for(host):
        published = getattr(host, "remote_stop_outcome_generation", None)
        stale = isinstance(published, int) and current < published
        # A proven exit already published for one captured bundle is evidence
        # that bundle earned, and a second publication FOR THAT SAME BUNDLE
        # which proves less must not erase it: one close can publish twice for
        # one bundle -- the release that asked and the release that closed the
        # channel -- and they finish in either order.
        #
        # Only a per-bundle publication asks for this. The aggregate speaks
        # for every bundle the close released and is deliberately the more
        # conservative statement, so it lowers the claim at this same
        # generation on purpose: one bundle that confirmed must never speak
        # for one that did not, which is the 1.0.8 aggregation itself.
        #
        # It cannot mask a newer owner either: an older generation is already
        # withheld above, and a newer one resets this field under this same
        # lock before it stamps itself, so no previous attempt's exit is ever
        # standing in a newer attempt's way.
        standing = getattr(host, "remote_stop_outcome", None)
        superseded = (
            preserve_proven_exit
            and not stale
            and published == current
            and getattr(standing, "confirmed", False) is True
            and not result.confirmed
        )
        _fire_publish_barrier(host, current)
        if not stale and not superseded:
            host.remote_stop_outcome = result
            host.remote_stop_outcome_generation = current
    if stale:
        if callable(trace):
            trace(
                f"remote stop outcome for attempt {current} not published; "
                f"attempt {published} owns that field"
            )
        return result
    if superseded:
        if callable(trace):
            trace(
                f"remote stop outcome for attempt {current} not published; "
                "a proven exit for that bundle already stands"
            )
        # The earned evidence is what this attempt established, so it is what
        # goes back to the caller as well as what stays on the field.
        return standing  # type: ignore[return-value]
    if callable(trace):
        trace(summarize(result))
    return result


def record_stop_outcome(
    host: object, resources: RemoteAttemptResources, result: StopResult, trace: object
) -> StopResult:
    """Publish one released bundle's outcome under that bundle's generation.

    This is the per-bundle publication, so a proven exit this bundle already
    earned is preserved against a second, weaker release of the same bundle
    finishing afterwards.  The aggregate publishes without that, because it is
    the wider and more conservative statement about every bundle released.
    """

    return publish_stop_outcome(
        host, resources.generation, result, trace, preserve_proven_exit=True
    )


def captured_process_is_reaped(process: object) -> bool:
    """Did this captured process actually go? Observed, never assumed.

    A terminate that returned is not an exit: the bounded terminate and kill
    can both time out and be reported rather than raised, and a process that
    is still running still owns the channel.  So the question is asked of the
    process itself and an unreadable answer is not a yes.
    """

    poll = getattr(process, "poll", None)
    if not callable(poll):
        return False
    try:
        return poll() is not None
    except OSError:
        return False


def _release_captured_process(host: object, process: object, trace: object) -> bool:
    """Close this bundle's captured SSH process, and say whether it went."""

    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is None and callable(trace):
        trace(f"stopping SSH connection owned by desktop host (PID {process.pid})")
    try:
        host._terminate_owned_process(process)
    except subprocess.TimeoutExpired:
        # Bounded terminate and kill both timed out. Report the unconfirmed
        # reap instead of widening the kill or claiming a clean stop.
        if callable(trace):
            pid = getattr(process, "pid", "?")
            trace(f"SSH process reap unconfirmed after terminate and kill (PID {pid})")
        return False
    return captured_process_is_reaped(process)


def release_captured(host: object, resources: RemoteAttemptResources) -> StopResult:
    """Release exactly the captured snapshot, each effect at most once.

    The order is unchanged and deliberate: ask the owner to stop while the
    channel that authenticates the request is still up, then close the channel
    and the log.  What is new is that the answer is kept rather than dropped,
    and that a stop nobody could confirm gets one bounded second look once the
    channel is gone, which is when the owner can finally be seen to have gone.

    The two halves can land in different callers -- one release wins the stop
    request, another wins the process close -- so the second look belongs to
    whichever of them actually closed the channel, reading what this bundle's
    one stop request established.  Neither the close nor the answer is assumed
    from a ticket having been taken.
    """

    trace = getattr(host, "_trace", None)
    result = _release_stop_owned(host, resources, trace)
    process = resources.process
    # Whether the channel that authenticated the request above is actually
    # gone by the time the confirmation runs. Taking the process ticket and
    # finishing the close are two different facts: a release that loses that
    # ticket has been told nothing about whether the winner has terminated
    # anything yet, and confirming on a channel that is still up is the one
    # thing the confirmation pass must not do.
    channel_closed = process is None
    try:
        if process is not None:
            if resources.claim.take(CLAIM_PROCESS):
                channel_closed = _release_captured_process(host, process, trace)
                if channel_closed:
                    resources.claim.complete(CLAIM_PROCESS)
            else:
                channel_closed = resources.claim.completed(CLAIM_PROCESS)
    finally:
        if resources.log is not None and resources.claim.take(CLAIM_LOG):
            close_captured_log(resources.log)
    result = _confirm_stop_outcome(
        host, resources, _adopt_shared_outcome(resources, result), trace,
        channel_closed=channel_closed,
    )
    # The forward-port observation is independent of whether a stop could be
    # asked for. A bundle with a lost token still had its captured SSH process
    # closed here, so its forward port can be observed exactly as any other
    # bundle's -- and that missing-token close is the 1.0.8 shape, the one case
    # where the observation matters most. It is recorded as the desktop's own
    # port; the contract original-listener field stays unknown because this
    # lane never observes the listener on the host that publishes it.
    result = result.with_observations(
        forward_listener_release=observe_forward_listener_release(resources.profile)
    )
    return record_stop_outcome(
        host, resources, _settle_bundle_outcome(resources, result), trace
    )


def unresolved_rank(result: StopResult) -> int:
    """How much one unresolved release established, for reporting the worst.

    Ordered by how actionable the answer is rather than by how bad it sounds.
    An owner that was watched and did not go is the strongest observation
    available short of an exit.  A stop that could not be asked for at all is
    the 1.0.8 shape and names one operator step, so it outranks a refusal,
    which names a different one; a request whose answer established nothing is
    the least informative of the four.
    """

    if result.process_exit == "failed":
        return 4
    if not result.requested:
        return 3
    if result.refused:
        return 2
    return 1


def aggregate_stop_outcomes(results: Sequence[StopResult]) -> StopResult | None:
    """Reduce every captured bundle outcome to one, conservatively.

    One close can release more than one bundle: the installed one and an
    in-flight one that was captured before publication.  Each is a real owner
    with its own token, its own SSH process and its own port, so the close as a
    whole has only stopped the connection when EVERY one of them proved an
    exit.  Picking the most successful outcome is exactly the incident: an old
    owner that confirms would mask a new owner that timed out, and the layer
    above would read one proven exit as a shutdown of everything captured.

    So any bundle that did not prove an exit decides the answer, and the
    reported record is one of the unresolved ones -- never a confirmed record
    with its evidence quietly stripped, which would be internally
    contradictory.  Only when nothing is unresolved does a confirmed outcome
    come back.  ``None`` means no bundle was released at all.
    """

    if not results:
        return None
    unresolved = [result for result in results if not result.confirmed]
    if not unresolved:
        return results[0]
    worst = unresolved[0]
    for result in unresolved[1:]:
        if unresolved_rank(result) > unresolved_rank(worst):
            worst = result
    return worst
