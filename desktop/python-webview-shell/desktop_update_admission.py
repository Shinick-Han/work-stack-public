"""Whether a verified PC download may actually be applied to this machine.

Finding and fetching a Work Stack desktop release is the PC's own business. A
remote server that is offline, that answers for another workspace, or that
speaks an older protocol cannot make a newer desktop invisible and cannot stop
the verified artifact from being downloaded and kept. What such a remote can
legitimately withhold is the *installation*, because the desktop that gets
installed is the one that will connect to it next.

That single decision lives here. It is settled while the remote authority can
still be asked - right after a download completes, when the user presses
install, and once more at the very start of window close, before the tunnel is
torn down - and every one of those three boundaries observes the remote that is
connected *now*. An earlier `admitted` verdict is never handed back unexamined:
an endpoint can change its workspace or drop below the release's protocol floor
between the download and the install, so each boundary asks again and records
what it was told. The applicator is started last, in `run`'s finally block,
after the owned server and the tunnel are already gone; at that point nothing
can be probed, so the launch path reads the recorded verdict and never the
network.

The verdict is bound to exactly what was judged: this download, this workspace
and endpoint, this session generation.

Three verdicts, and only one of them installs:

`admitted`   the remote answered for the selected workspace and meets the
             release's protocol floor, or there is no remote at all.
`refused`    the remote answered and the answer disqualifies it - another
             workspace, invalid identity, or a protocol below the floor.
`unknown`    nobody answered: offline, no settled session, a rebind or
             recovery in flight, or a binding that moved during the probe.

`refused` and `unknown` behave the same way on purpose: keep the downloaded
artifact, explain that the install is pending, do not close the window and do
not start the applicator. An unverified remote is never flattened into an
admitted one, and an unreachable remote never destroys work the PC already did.

Reuse is deliberately narrow. A verdict speaks only for the download it was
taken against, for the same workspace and endpoint, and for the same session.
Installation does happen after shutdown, so a session that is *gone* can still
be covered - but only when this host explicitly recorded that it was itself
tearing that exact session down. A session that merely stopped answering has
not been intentionally closed: an SSH tunnel that dies between the settlement
and the launch leaves no such record, and an unexpected death is never allowed
to stand in for the intentional one. A session that was replaced, rebound or is
under recovery is a different remote, so its predecessor's verdict is not
reused either.

"No session right now" is never read as "no successor was ever started",
because both a closed session and a replaced one that has since died report
exactly that. The attempt lineage is what separates them, and it is read from
the host's own remote startup state machine, whose attempt generation only ever
advances and is *retained* through failure and teardown. Every verdict and
every close record is therefore bound to the attempt generation that was the
newest one at the time it was taken; once a later attempt has been begun, that
number has moved on and no predecessor's record can authorize an install again.

The host is passed in rather than subclassed, matching the other bounded
desktop lifecycle modules. The host's identity-refusal exception type is passed
in as `refusal` so this module never imports the shell it serves. Nothing here
mutates SSOT, the remote, the session, the artifact or any preference; the only
host field written is the recorded verdict itself.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass


ADMITTED = "admitted"
REFUSED = "refused"
UNKNOWN = "unknown"

#: No remote profile, or a profile with no live settled session right now.
#: Reusable only against a captured `IntentionalClose` for that same session.
NO_SESSION = 0
#: An authority this host cannot currently speak for. Never reusable.
INDETERMINATE_SESSION = -1

#: No remote attempt has ever been begun on this host, so none can be replaced.
NO_ATTEMPT = 0
#: The attempt lineage could not be read, so no record may be trusted to still
#: be the newest one. Never equal to a retained generation, so never reusable.
UNKNOWN_ATTEMPT = -1

REMOTE_ADMISSION_PROBE_SECONDS = 3.0

HELD_HEADLINE = (
    "The verified Work Stack update is downloaded and kept, and installing it "
    "is still pending."
)
ADMITTED_DETAIL = "The connected remote Work Stack server was verified for this release."
LOCAL_ONLY_DETAIL = "No remote Work Stack server is connected, so only this PC is affected."
NOTHING_DOWNLOADED_DETAIL = "No verified Work Stack update is downloaded on this PC."
UNSETTLED_DETAIL = (
    "The connected remote Work Stack server has no settled session to answer "
    "for it, so its compatibility with this release is unknown."
)
UNREACHABLE_DETAIL = (
    "The connected remote Work Stack server could not be reached, so its "
    "compatibility with this release is unknown."
)
MOVED_DETAIL = (
    "The remote Work Stack session changed while this release was being "
    "checked against it, so the answer is unknown."
)
WORKSPACE_DETAIL = (
    "The connected remote Work Stack server reported a different workspace, so "
    "this release was not admitted for it."
)


@dataclass(frozen=True)
class AdmissionBinding:
    """Exactly what a verdict was about: one download, one remote, one session.

    The remote is identified by the workspace the selected profile names and by
    the endpoint the probe reaches; both are empty when no remote profile is
    selected, so connecting a remote after a local-only verdict is a visible
    change rather than a silent match. Other profile configuration - the SSH
    host, the remote app and data directories - is deliberately not part of
    this identity: activating a different connection profile is restart-bound,
    and the in-process workspace rebind moves the workspace UUID and settles a
    new session, so both of the ways it can change today are already caught by
    the three fields that are here. A future in-process profile swap that kept
    all three would need this record widened.

    `session` is the settled session that answered - or the reason there was
    none. `attempt` is the newest attempt generation this host had begun at
    that moment, read from the retained lifecycle counter. The two are captured
    as one observation under one lock hold, and only `attempt` still means
    something once the session is gone: it is what distinguishes a session this
    host closed from one that was replaced by a successor.
    """

    download: str
    workspace_id: str
    endpoint: str
    session: int
    attempt: int


@dataclass(frozen=True)
class UpdateAdmission:
    """One settled verdict and the bounded English reason behind it."""

    verdict: str
    binding: AdmissionBinding
    detail: str


@dataclass(frozen=True)
class IntentionalClose:
    """This host's own record that it is shutting the admitted remote down.

    Captured at the very start of window close, only for an `admitted` verdict,
    and only while that verdict's session was still settled and alive. It is
    what makes the post-shutdown install legitimate: the launch path may accept
    a session that is gone only when this record says the session it is missing
    is the admitted one and that this close path is the reason it is missing.

    A session that simply stopped answering never produces one - a tunnel that
    died, an attempt that failed and a remote that was never settled all leave
    this absent - so an unexpected death cannot borrow the exemption that
    belongs to an intentional shutdown.

    The binding carries the attempt generation that was the newest one when the
    close was captured, which is what keeps the record from outliving the
    session it names. A successor attempt advances the host's retained
    generation, so once one has been begun this record no longer describes the
    last thing that happened to the remote and stops authorizing an install -
    even after that successor has itself died and left no session behind.
    """

    binding: AdmissionBinding


def download_identity(downloaded: object) -> str:
    """Name the download a verdict is taken against, as the download reports it.

    This is the version, the two verified file paths and the protocol floor of
    the release that was fetched - what distinguishes one download this host is
    holding from another, not a digest of the bytes. Version alone is not
    enough: a re-download writes new files under a new versioned directory and
    carries its own floor, and a verdict must not survive into a different one.
    Whether those bytes are the ones that were verified stays the download and
    applicator contract's business, unchanged here.
    """

    if downloaded is None:
        return ""
    return "|".join(
        str(getattr(downloaded, field, ""))
        for field in ("version", "setup_path", "checksum_path", "minimum_remote_protocol")
    )


def retained_attempt_generation(host: object) -> int:
    """Read the newest remote attempt this host has ever begun.

    The host's remote startup state machine already counts attempts
    monotonically and keeps that count through failure and teardown, so its
    `last_attempt_generation` is the one fact that still distinguishes "this
    host closed the session it was admitted against" from "something newer took
    its place and then died". Callers must hold the lifecycle lock; the machine
    takes only its own innermost lock here and publishes nothing.

    A host whose attempt protocol is not published, or a machine too old to
    expose the retained count, cannot prove either way and reports
    UNKNOWN_ATTEMPT, which no record will ever match.
    """

    retained = getattr(getattr(host, "remote_startup", None), "last_attempt_generation", None)
    return int(retained) if isinstance(retained, int) else UNKNOWN_ATTEMPT


def _lifecycle_lock(host: object):
    """The host's own single lifecycle lock, or nothing to hold if unpublished."""

    lock = getattr(host, "remote_resource_lock", None)
    return contextlib.nullcontext() if lock is None else lock


def remote_lifecycle_identity(host: object) -> tuple[int, int]:
    """Identify the settled session and the attempt lineage as one observation.

    A rebind coordination or a required recovery is not "no session": it is an
    authority this host cannot speak for, so it reports INDETERMINATE_SESSION,
    which never satisfies a later reuse check, and it declines to vouch for the
    lineage either. The rebind window is read, never expired, so a read-only
    admission probe cannot clear live coordination state.

    The two facts are read together under one hold of the lifecycle lock, in the
    established direction - the authority guard outside it, the state machine's
    own lock innermost - because they are only useful as a pair: sampling them
    separately would let an attempt begin in between and produce a session and a
    lineage that were never true at the same moment. The lock is reentrant and
    is the same one `_settled_remote_session` takes, and no lock is held across
    the network.
    """

    if getattr(host, "remote_profile", None) is None:
        return NO_SESSION, NO_ATTEMPT
    with host._remote_authority_guard():
        target = str(getattr(host, "remote_rebind_target", "") or "")
        deadline = float(getattr(host, "remote_rebind_deadline", 0.0) or 0.0)
        if target and time.monotonic() <= deadline:
            return INDETERMINATE_SESSION, UNKNOWN_ATTEMPT
        if host._remote_recovery_is_required():
            return INDETERMINATE_SESSION, UNKNOWN_ATTEMPT
        with _lifecycle_lock(host):
            session = host._settled_remote_session()
            attempt = retained_attempt_generation(host)
    return (NO_SESSION if session is None else int(session.generation)), attempt


def remote_session_identity(host: object) -> int:
    """The settled remote session alone, for callers that need only that."""

    return remote_lifecycle_identity(host)[0]


def admission_binding(host: object) -> AdmissionBinding:
    """Capture what a verdict taken right now would be about."""

    profile = getattr(host, "remote_profile", None)
    session, attempt = remote_lifecycle_identity(host)
    return AdmissionBinding(
        download_identity(getattr(host, "downloaded_update", None)),
        "" if profile is None else str(profile.workspace_id),
        "" if profile is None else str(getattr(host, "workstack_url", "")),
        session,
        attempt,
    )


def admission_permits_install(admission: object, host: object) -> bool:
    """True only when a settled `admitted` verdict still speaks for the install.

    Called from the launch path, which runs after shutdown and may not read the
    network, so this consults the recorded verdict and local lifecycle state
    only. The download, the workspace and the endpoint must still be the ones
    that were judged; a different download, a different workspace or endpoint,
    a newer session generation, a rebind in flight or a recovery in progress
    all mean the admitted remote is not the remote this install would meet.

    The session is the one fact that is allowed to be missing, because the
    install happens after the tunnel is torn down - but only against this
    host's own `IntentionalClose` record for that same admitted session, and
    only while that record is still the last thing that happened to the remote.
    Absence of a session is not evidence of a shutdown, and it is not evidence
    that nothing came afterwards either: a tunnel that died between the
    settlement and the launch reports no session, and so does a successor
    session that was installed after the close and has since exited. The
    retained attempt generation is what tells those apart, so it must still be
    the one the record was taken against; anything newer means the remote this
    install would meet is not the one that was judged, which stays unknown and
    holds the artifact rather than installing.

    With no remote profile bound there is no remote authority being spoken for
    and nothing that could refuse, so the answer is the same one
    `observe_update_admission` gives for that case and does not depend on a
    record having been taken. A bound profile always does.
    """

    current = admission_binding(host)
    if not current.download:
        return False
    if not current.workspace_id:
        return True
    if not isinstance(admission, UpdateAdmission) or admission.verdict != ADMITTED:
        return False
    settled = admission.binding
    if (settled.download, settled.workspace_id, settled.endpoint) != (
        current.download,
        current.workspace_id,
        current.endpoint,
    ):
        return False
    if (current.session, current.attempt) == (settled.session, settled.attempt):
        return True
    if current.session != NO_SESSION:
        return False
    closed = getattr(host, "update_admission_close", None)
    if not isinstance(closed, IntentionalClose) or closed.binding != settled:
        return False
    return current.attempt == closed.binding.attempt


def capture_intentional_close(host: object, admission: object) -> None:
    """Record that this host is itself shutting the admitted session down.

    Written once, at the start of the host's own close, from the verdict that
    close just settled against a live session. Anything else - a refused or
    unknown verdict, or an `admitted` one whose session had already gone by the
    time close ran - leaves no record, so the launch path keeps holding the
    artifact instead of installing against a remote nobody spoke to.

    The lifecycle is read once more here, under the same locks the settlement
    used, and the record is written only if the admitted session is still the
    settled one *and* its attempt generation is still the newest this host has
    begun. That closes the window between the probe and this write: a
    replacement attempt started in it would leave a record naming a generation
    that had already been superseded, which is exactly the record that must not
    exist.
    """

    if not isinstance(admission, UpdateAdmission) or admission.verdict != ADMITTED:
        return
    settled = admission.binding
    if settled.session <= NO_SESSION:
        return
    if remote_lifecycle_identity(host) != (settled.session, settled.attempt):
        return
    host.update_admission_close = IntentionalClose(settled)


def observe_update_admission(host: object, binding: AdmissionBinding, *, refusal: type) -> UpdateAdmission:
    """Ask the connected remote once whether this release may be installed.

    Reads the same storage identity endpoint the rest of the host already
    trusts, and reuses the host's own protocol floor check against the floor
    the downloaded release itself declares. An answer that belongs to another
    workspace, or an identity the endpoint could not state validly, is a
    refusal; an endpoint that could not be reached is unknown. The binding is
    re-read afterwards so an answer collected while the session was being
    replaced - or while a replacement attempt was merely being started, which
    moves the retained attempt generation before any new session settles - is
    discarded rather than credited to the new one.
    """

    if getattr(host, "remote_profile", None) is None:
        return UpdateAdmission(ADMITTED, binding, LOCAL_ONLY_DETAIL)
    if binding.session <= NO_SESSION:
        return UpdateAdmission(UNKNOWN, binding, UNSETTLED_DETAIL)
    downloaded = host.downloaded_update
    try:
        metadata = host._read_remote_storage_metadata(timeout=REMOTE_ADMISSION_PROBE_SECONDS)
        if str(metadata["workspace_id"]) != binding.workspace_id:
            return UpdateAdmission(REFUSED, binding, WORKSPACE_DETAIL)
        if admission_binding(host) != binding:
            return UpdateAdmission(UNKNOWN, binding, MOVED_DETAIL)
        host._require_supported_remote_protocol(
            metadata,
            minimum=int(downloaded.minimum_remote_protocol),
            purpose=f"update to Work Stack {downloaded.version}",
        )
    except refusal as error:
        return UpdateAdmission(REFUSED, binding, str(error))
    except (OSError, RuntimeError, ValueError):
        return UpdateAdmission(UNKNOWN, binding, UNREACHABLE_DETAIL)
    host._remember_remote_metadata(metadata)
    return UpdateAdmission(ADMITTED, binding, ADMITTED_DETAIL)


def settle_update_admission(host: object, *, refusal: type) -> UpdateAdmission:
    """Record one verdict for the download this host is currently holding.

    Every settlement boundary observes the remote again, and the answer it gets
    replaces whatever was recorded before. An earlier `admitted` verdict is not
    a shortcut past the probe: the endpoint that answered when the download
    finished can be serving another workspace, or answering below the release's
    protocol floor, by the time install is pressed or the window closes, and
    the host that gets installed is the one that will connect to it next. An
    earlier `refused` or `unknown` verdict is re-observed for the same reason
    in the other direction - a remote that was offline when the download
    finished may well answer by the time the window closes, and holding a stale
    unknown would strand the install.

    There are exactly three of these boundaries, all of them user or lifecycle
    events with a live authority - a completed download, the install button and
    the first statement of window close - so asking again costs one bounded
    read, never a poll.
    """

    binding = admission_binding(host)
    if binding.download:
        admission = observe_update_admission(host, binding, refusal=refusal)
    else:
        admission = UpdateAdmission(UNKNOWN, binding, NOTHING_DOWNLOADED_DETAIL)
    host.update_admission = admission
    return admission


def settle_update_admission_for_exit(host: object, *, refusal: type) -> None:
    """Settle admission at the start of close, before the tunnel is torn down.

    This is the only point on the install-on-exit path where the remote can
    still be asked: the applicator is started in `run`'s finally block, after
    the owned server has been stopped and the session released. It is therefore
    also the only honest place to record that the session about to disappear is
    disappearing because this host is closing, which is what later permits the
    post-shutdown launch.

    An applicator that is already running was launched from an explicit install
    that settled its own verdict moments ago, so there is nothing left to
    settle and no reason to probe again. Nothing here may prevent the window
    from closing, so a probe that fails is traced and leaves whatever verdict
    was already recorded - without a close record, which holds the install.
    """

    if not getattr(host, "install_update_on_exit", False):
        return
    if getattr(host, "downloaded_update", None) is None:
        return
    if getattr(host, "update_process", None) is not None:
        return
    try:
        capture_intentional_close(host, settle_update_admission(host, refusal=refusal))
    except (AttributeError, OSError, RuntimeError, ValueError) as error:
        host._trace(f"update admission could not be settled at close: {type(error).__name__}")


def held_install_message(admission: object) -> str:
    """Say the artifact is kept and why the install has not happened yet."""

    detail = admission.detail if isinstance(admission, UpdateAdmission) and admission.detail else UNSETTLED_DETAIL
    return f"{HELD_HEADLINE} {detail}"


def ready_install_message(admission: object, *, install_on_exit: bool) -> str:
    """Word the post-download card for the verdict that was actually settled."""

    if not isinstance(admission, UpdateAdmission) or admission.verdict != ADMITTED:
        return held_install_message(admission)
    if install_on_exit:
        return "Verified update will install when Work Stack closes"
    return "Verified update is ready to install"
