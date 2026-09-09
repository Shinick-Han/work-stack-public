"""What the flow knows about the optional agent-Skill offer, and nothing else.

``RemoteUpdateFlow`` owns the update's own lifecycle.  The Skill step is a side
action on a finished one: it issues no journalled mutation, moves no stage and
changes no measured version, so its whole state is the small immutable value
here plus the two rules that decide what may be clicked next.  Keeping it out
of the lifecycle module is what stops an optional read-and-write of the
operator's home directory from growing a second state machine inside the one
that owns backups, activation and rollback.

Nothing here performs an effect and nothing here reads the clock.  The flow
holds one :class:`SkillState`, asks :func:`admit_skill_call` whether a call is
legal, and replaces the state with what the answer settled to.

Four rules are this module's own responsibility.

*   A write is reachable only from the read that earned it.  ``offer`` is set
    by an inspect that found something to install or update and is cleared by
    everything else, so an unknown answer, a refusal and a completed write all
    lead back to the read.
*   A write whose answer never arrived is never re-sent.  ``in_flight`` is set
    before the call and cleared only by an answer, so a call that never
    returned leaves the offer withdrawn rather than re-armed.
*   An answer is evidence for the page that asked for it and no other.  It
    describes a home directory this process does not own, so a genuinely new
    page withdraws it and asks again.  A call already in flight is not
    cancelled or re-issued, but its later reply cannot offer a write or claim
    success on the page that was mounted after it left.
*   Settling an answer is not publishing it.  A reply that settled for the
    page it left is still carried back to the flow by the calling thread, and
    a reopen can land in between.  What may be published is therefore derived
    at the publication boundary from the epoch the reply settled for, never
    from the code the caller is holding.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from remote_update_flow_contract import RemoteUpdateRefused, RemoteUpdateStage
from remote_update_skill_port import SkillOutcome, SkillPort, skill_flow_code


ACTION_INSPECT = "run_skill_inspect"
ACTION_INSTALL = "run_skill_install"
ACTION_UPDATE = "run_skill_update"

#: The D actions this offer can publish, in no particular order.  The flow
#: publishes exactly one of them at a time, because the page renders one.
SKILL_ACTIONS: frozenset[str] = frozenset({ACTION_INSPECT, ACTION_INSTALL, ACTION_UPDATE})

#: The write action each admitted inspect status earns.  Any status not named
#: here earns none, which is what makes the read the only way back.
OFFER_BY_STATUS: dict[str, str] = {
    "absent": ACTION_INSTALL,
    "outdated": ACTION_UPDATE,
}

REFUSED_UNAVAILABLE = "skill_refused_unavailable"
REFUSED_NOT_READY = "skill_refused_not_ready"
REFUSED_IN_FLIGHT = "skill_refused_in_flight"
REFUSED_INSPECT_REQUIRED = "skill_refused_inspect_required"


@dataclass(frozen=True)
class SkillState:
    """The published code of the last Skill call, and what it left offered.

    ``code`` is empty before any Skill call has run, which is the condition in
    which the update publishes its own ``update_ready`` and the offer is simply
    the read.
    """

    code: str = ""
    offer: str = ""
    in_flight: bool = False

    def actions(self, *, available: bool) -> tuple[str, ...]:
        """The single Skill action offered now, or none at all."""

        if not available or self.in_flight:
            return ()
        return (self.offer or ACTION_INSPECT,)

    def opened(self) -> SkillState:
        """Arm the call: the offer is withdrawn until an answer settles it."""

        return replace(self, offer="", in_flight=True)

    def withdrawn(self) -> SkillState:
        """The state a genuinely new page starts from.

        A settled answer is ephemeral: it says what one earlier read or write
        found in a directory outside this process, and anything may have
        changed it since.  Dropping it is what makes the read the only thing a
        reopened page can offer, and what stops a finished write from being
        headlined again without being re-measured.

        A call in flight is kept exactly as it is at this layer.  The offer
        that owns the epoch decides whether that call's later reply still
        belongs to the page on screen.
        """

        return self if self.in_flight else SkillState()

    def settled(self, outcome: object) -> SkillState:
        """Take one answer, publishing its code and whatever it re-offers."""

        answer = outcome if isinstance(outcome, SkillOutcome) else SkillOutcome()
        offer = OFFER_BY_STATUS.get(answer.status, "") if answer.offers_install else ""
        return SkillState(code=skill_flow_code(answer), offer=offer)


@dataclass(frozen=True)
class SkillReply:
    """One finished Skill call: what it settled to, and for which page.

    ``epoch`` is the page the code describes.  The call still has to carry it
    back to the flow, and a page can be retired and reopened while it does, so
    publication asks for this reply's code again rather than trusting the one
    already in hand.  An empty ``code`` is a call that never ran.
    """

    code: str = ""
    epoch: int = -1


def admit_skill_call(
    state: SkillState, port: object, *, ready: bool, mutating: bool
) -> str:
    """The refusal code this call earns, or an empty string when it is legal.

    ``ready`` is the flow's own answer to "the update is finished and nothing
    is unsettled", asked rather than recomputed here so this module never has
    to know the stage vocabulary.
    """

    if port is None:
        return REFUSED_UNAVAILABLE
    if not ready:
        return REFUSED_NOT_READY
    if state.in_flight:
        return REFUSED_IN_FLIGHT
    if mutating and not state.offer:
        return REFUSED_INSPECT_REQUIRED
    return ""


class SkillOffer:
    """One flow's Skill port and the state of its offer, under a short guard.

    ``_guard`` covers the gate, the arm, the epoch and the settle.  It is never
    held across the port call, and the flow's own lock is not held across that
    call either: a page being mounted can snapshot without waiting on SSH.
    ``in_flight`` is still the one-call gate, so a second click cannot arm a
    duplicate mutation while the first call is out.  A click may also carry the
    page epoch captured when the controller accepted it; that comparison lives
    in the same guard as the arm, so a queued call cannot adopt a newer page.
    The same guard answers :meth:`publishable`, so the epoch a reply settled
    for is re-read at the moment of publication rather than assumed to have
    held while the caller carried the code back.
    """

    def __init__(self, port: SkillPort | None) -> None:
        self._port = port
        self._state = SkillState()
        self._published = ""
        self._epoch = 0
        self._guard = threading.Lock()

    @property
    def state(self) -> SkillState:
        with self._guard:
            return self._state

    def actions(self) -> tuple[str, ...]:
        """The single Skill action offered now, or none at all."""

        with self._guard:
            return self._state.actions(available=self._port is not None)

    @property
    def epoch(self) -> int:
        with self._guard:
            return self._epoch

    def run(
        self,
        *,
        mutating: bool,
        ready: bool,
        published: str = "",
        origin_epoch: int | None = None,
    ) -> SkillReply:
        """Run the one legal call and return what it settled, and for whom.

        Raises :class:`RemoteUpdateRefused` and changes nothing when the call
        is not legal, so a refused click never disturbs the published state.

        ``published`` is what the update itself had published just before this
        call.  It is remembered rather than named here, so :meth:`reopened` can
        hand the update's own code back without this module having to know a
        single word of the lifecycle's vocabulary.

        ``origin_epoch`` is the page that accepted the click.  When it no
        longer matches, this returns without arming or touching the port: the
        originating view is gone and the call had not yet started.

        The reply names the epoch its code was settled for.  Settling is not
        publishing: the caller still has to reach the flow, and a reopen may
        land first, so the code is not publishable on its own.
        """

        with self._guard:
            started = self._start_locked(
                mutating=mutating,
                ready=ready,
                published=published,
                origin_epoch=origin_epoch,
            )
            if started is None:
                return SkillReply()
            port, epoch = started
        answer = port.install_skill() if mutating else port.inspect_skill()
        with self._guard:
            return self._accept_locked(answer, epoch, mutating=mutating)

    def _start_locked(
        self,
        *,
        mutating: bool,
        ready: bool,
        published: str,
        origin_epoch: int | None,
    ) -> tuple[SkillPort, int] | None:
        """Arm one call under the guard, or drop a queued click for a gone page."""

        if origin_epoch is not None and origin_epoch != self._epoch:
            return None
        refusal = admit_skill_call(self._state, self._port, ready=ready, mutating=mutating)
        if refusal:
            raise RemoteUpdateRefused(refusal)
        assert self._port is not None
        if not self._state.code:
            self._published = published
        self._state = self._state.opened()
        return self._port, self._epoch if origin_epoch is None else origin_epoch

    def reopened(self) -> str:
        """Withdraw a settled answer, and detach any reply still in flight.

        Returns the update's own published code when the new page should show
        that instead of a Skill sentence: a settled answer was dropped, or a
        call is still out and must not keep headlining the closed page.  The
        empty string means there was nothing to restore.

        The caller holds the flow lock across this and the restore it
        publishes, so advancing the epoch and putting the update's own code
        back are one step: a reply that settled a moment earlier cannot slip
        its code in between them.
        """

        with self._guard:
            self._epoch += 1
            if self._state.in_flight:
                return self._published
            if not self._state.code:
                return ""
            self._state = self._state.withdrawn()
            return self._published

    def _accept_locked(self, answer: object, epoch: int, *, mutating: bool) -> SkillReply:
        """Settle one reply, or discard it when the page it left has closed."""

        if self._epoch == epoch:
            self._state = self._state.settled(answer)
            return SkillReply(self._state.code, self._epoch)
        if not self._state.in_flight:
            return SkillReply(self._state.code or self._published, self._epoch)
        self._state, code = _stale_reply(answer, mutating=mutating, published=self._published)
        return SkillReply(code, self._epoch)

    def publishable(self, reply: SkillReply) -> str:
        """The part of a finished call's answer the page on screen may show.

        A reply is evidence for one page.  Between settling it and reaching
        the flow, that page can be retired and a new one mounted, and the
        reopen that did so has already put the update's own code back.  Asking
        here -- under the guard that owns the epoch, and inside the flow lock
        that owns the publication -- is what stops the older code from landing
        on top of it.  An empty string means this call publishes nothing.
        """

        with self._guard:
            if not reply.code or reply.epoch != self._epoch:
                return ""
            return reply.code


class SkillOfferMixin:
    """The two public Skill calls, mixed into the flow that owns the lock.

    They live here rather than in the lifecycle module because they are not
    part of the lifecycle: they run only once it has finished, they issue no
    journalled mutation and they move no stage.  ``advance`` refuses a terminal
    stage, which is precisely the condition these are offered in, so they are
    their own named calls and that refusal is left exactly as it is.

    The mixin uses five things from the flow it is mixed into and nothing else:
    ``_lock``, ``_ports``, the current ``_stage``, ``_pending`` and ``_code``,
    and ``_publish_locked``.  It publishes only at ``READY``, so the stage and
    the measured versions of the finished update are never disturbed.
    """

    #: Replaced on the instance the first time the offer is consulted.  A flow
    #: that never reaches ``READY`` never builds one.
    _skill: SkillOffer | None = None

    def inspect_skill(self, *, origin_epoch: int | None = None) -> object:
        """Measure the operator's agent Skill.  Read-only on both sides.

        This is also the reconciliation: after a write whose answer never
        arrived it is the one call that is allowed, because it reads the
        helper's receipt and the bytes actually in place rather than assuming
        what the lost write did.
        """

        return self._skill_call(mutating=False, origin_epoch=origin_epoch)

    def install_skill(self, *, origin_epoch: int | None = None) -> object:
        """Write the agent Skill, once, from the offer one inspect earned.

        This is the second, separately labelled click.  It needs no owner stop
        and no session token, and touches only the operator's own home
        directory on the host this update has just verified.
        """

        return self._skill_call(mutating=True, origin_epoch=origin_epoch)

    def reopen_skill_view(self) -> object:
        """Withdraw the closed page's Skill answer before a new one is mounted.

        The host keeps one composed flow for an unchanged binding, so without
        this a page opened after a close inherits the last inspect's offer and
        the last write's headline.  Both are claims about a home directory the
        desktop stopped watching when the page went away, so the new page is
        given the update's own ``READY`` code back: it offers the read first,
        and no write offer or success sentence exists again until a fresh
        inspect earns one.

        This is not the repaint path.  Rendering the page that is on screen
        keeps everything it has already published, including an answer that
        has only just arrived.  A call still in flight is left running -- it
        is not cancelled and not re-issued -- but the epoch advanced here
        stops its later reply from offering a write or claiming success.

        Withdrawing the answer and putting the update's own code back is one
        step under the flow lock, because that lock is also where a Skill call
        publishes: a reply that settled for the closed page cannot land
        between the epoch advancing and the restore.  Neither lock is held
        across the Skill port call, so this never waits on SSH for one.
        """

        offer = self._skill
        if offer is None:
            return None
        with self._lock:
            restore = offer.reopened()
            if not restore or self._stage is not RemoteUpdateStage.READY:
                return None
            return self._publish_locked(RemoteUpdateStage.READY, restore)

    def _skill_locked(self) -> SkillOffer:
        """The offer this flow speaks for, built once from its own port."""

        if self._skill is None:
            self._skill = SkillOffer(getattr(self._ports, "skill", None))
        return self._skill

    def _skill_call(self, *, mutating: bool, origin_epoch: int | None = None) -> object:
        """Run one Skill call and publish its code, leaving the stage alone.

        The flow lock covers the ready check and the publish.  The offer's
        own guard is the one-call arm.  Neither lock is held across the port
        call, so a snapshot or a reopen on the UI thread is not waiting on SSH.

        What the call settled is not what gets published: the code is asked
        for again inside the publishing critical section, so a page retired
        and reopened while the answer was being carried back keeps the
        update's own sentence instead of the finished call's.
        """

        with self._lock:
            ready = self._stage is RemoteUpdateStage.READY and self._pending is None
            published = self._code
            offer = self._skill_locked()
        reply = offer.run(
            mutating=mutating, ready=ready, published=published, origin_epoch=origin_epoch
        )
        with self._lock:
            code = offer.publishable(reply)
            if self._stage is not RemoteUpdateStage.READY or not code:
                return self._snapshot_locked()
            return self._publish_locked(RemoteUpdateStage.READY, code)


def _stale_reply(
    answer: object, *, mutating: bool, published: str
) -> tuple[SkillState, str]:
    """What a previous page's reply may still leave on a reopened view.

    An unknown write keeps its evidence and stays read-only.  Every other
    stale answer is dropped so the new page can Check again; the update's own
    ready code is what gets published, so overall success is untouched.
    """

    outcome = answer if isinstance(answer, SkillOutcome) else SkillOutcome()
    code = skill_flow_code(outcome)
    if mutating and outcome.status == "unknown":
        return SkillState(code=code), code
    return SkillState(), published


__all__ = [
    "ACTION_INSPECT",
    "ACTION_INSTALL",
    "ACTION_UPDATE",
    "OFFER_BY_STATUS",
    "REFUSED_INSPECT_REQUIRED",
    "REFUSED_IN_FLIGHT",
    "REFUSED_NOT_READY",
    "REFUSED_UNAVAILABLE",
    "SKILL_ACTIONS",
    "SkillOffer",
    "SkillOfferMixin",
    "SkillReply",
    "SkillState",
    "admit_skill_call",
]
