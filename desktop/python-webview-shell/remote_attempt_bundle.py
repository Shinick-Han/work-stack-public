"""One attempt's captured resource bundle and its one-shot cleanup tickets.

A start captures generation, profile, token, process and log once, and every
later decision about that attempt is made from that frozen bundle rather than
from whatever the host fields hold at the time.  This module is only the value
layer: the bundle itself, the tickets that keep each externally visible cleanup
to exactly one occurrence, and the pure comparisons over bundles.

Nothing here reads or writes a host field, takes the lifecycle lock, opens an
SSH connection or waits on a process.  The gate and the host-field commits live
in ``remote_attempt_resources``; the release of a captured bundle lives in
``remote_attempt_shutdown``.  Keeping the value layer underneath both is what
lets the release path name the same claims the gate hands out without either
side importing the other.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


CLAIM_STOP_OWNED = "stop-owned"
CLAIM_PROCESS = "process"
CLAIM_LOG = "log"
# The confirmation pass is a fourth externally visible effect of one attempt:
# it opens its own bounded SSH connection, so it gets its own one-shot ticket
# and can never run twice for the same captured bundle.
CLAIM_STOP_CONFIRM = "stop-confirm"


def proves_exit(outcome: object) -> bool:
    """Does this record carry a proven exit of the recorded owner process?

    Asked of the value rather than of its type: this is the value layer and it
    reads no contract module.  Anything that does not say yes is treated as
    proving nothing, which is the safe direction for a comparison whose whole
    job is to refuse to lose evidence.
    """

    return getattr(outcome, "confirmed", False) is True


class AttemptCleanupClaim:
    """One-shot tickets for the externally visible cleanups of one attempt.

    Close, the stale waiter, failure cleanup and pre-publication cancellation
    all race for the same claim, so each remote stop, each captured process
    reap and each captured log close happens exactly once. The ticket set is
    bounded by the four effects above and dies with its attempt; no history
    is retained anywhere.

    Taking a ticket and finishing the effect it names are two different facts,
    and one caller can need the other's.  A release that lost the process
    ticket has not thereby been told the channel is closed -- the caller that
    won it may not have terminated anything yet -- so ``complete`` records that
    an effect actually finished and ``completed`` answers that question without
    blocking.  Nothing here waits: a caller that needs an effect it does not
    own decides what to do with "not yet", and this module never holds a lock
    across anything but a set membership test.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._taken: set[str] = set()
        self._done: set[str] = set()
        # One entry per captured bundle sharing this claim, never merged.
        self._stop_outcomes: list[tuple[object, object]] = []

    def take(self, effect: str) -> bool:
        with self._lock:
            if effect in self._taken:
                return False
            self._taken.add(effect)
            return True

    def complete(self, effect: str) -> None:
        """Record that the caller holding this ticket finished its effect."""

        with self._lock:
            self._done.add(effect)

    def completed(self, effect: str) -> bool:
        """Has this effect actually finished? Never blocks, never claims."""

        with self._lock:
            return effect in self._done

    def settle_stop_outcome(self, bundle: object, outcome: object) -> object:
        """Keep this bundle's strongest stop evidence, and hand back what stands.

        The record is shared and not private because the two halves of one
        release can land in different callers: one wins the stop request,
        another wins the process close, and the confirmation belongs to
        whichever of them actually closed the channel.  Without this, the
        closer would be holding an outcome that observed nothing and could not
        confirm, while the caller that did observe could not confirm either
        because it had not closed anything.

        Sharing it makes the reverse order matter too.  Those two callers can
        finish in either order, and the one that finishes last may be the one
        holding the weaker record, so a proven exit this bundle has already
        earned is never replaced by a record that proves less.  Keeping and
        returning are one operation under this lock, so a caller cannot read
        "nothing proven yet", be overtaken, and then write its own answer over
        the proof that arrived in between.

        The scope is exactly one captured bundle, and it has to be: an
        in-flight bundle deliberately shares the running attempt's claim so
        that the two cannot stop the same owner twice, and those two are
        different captured owners with different tokens and different
        processes.  One of them proving an exit says nothing about the other,
        so each is recorded against the bundle it belongs to and a bundle only
        ever reads back its own.
        """

        with self._lock:
            for index, (owner, stored) in enumerate(self._stop_outcomes):
                if same_installed_bundle(owner, bundle):  # type: ignore[arg-type]
                    if proves_exit(stored) and not proves_exit(outcome):
                        return stored
                    self._stop_outcomes[index] = (owner, outcome)
                    return outcome
            self._stop_outcomes.append((bundle, outcome))
            return outcome

    def stop_outcome(self, bundle: object) -> object:
        """What THIS bundle's stop request established, or None if not yet."""

        with self._lock:
            for owner, stored in self._stop_outcomes:
                if same_installed_bundle(owner, bundle):  # type: ignore[arg-type]
                    return stored
        return None


@dataclass(frozen=True)
class RemoteAttemptResources:
    generation: int
    profile: object
    token: str
    process: object | None
    log: object | None
    claim: AttemptCleanupClaim = field(
        default_factory=AttemptCleanupClaim, compare=False, repr=False
    )


@dataclass(frozen=True)
class AttemptStart:
    """What one `begin` published: the generation, its token and its claim."""

    generation: int
    token: str
    claim: AttemptCleanupClaim


def same_installed_bundle(
    current: RemoteAttemptResources, resources: RemoteAttemptResources
) -> bool:
    return (
        current.generation == resources.generation
        and current.token == resources.token
        and current.process is resources.process
        and current.log is resources.log
    )


def host_fields_after_detach(
    resources: RemoteAttemptResources,
    *,
    token: object,
    process: object,
    log: object,
) -> tuple[object, object, object]:
    """Keep newer host fields; clear only the captured bundle's identities."""

    next_token = None if token == resources.token else token
    next_process = None if process is resources.process else process
    next_log = None if log is resources.log else log
    return next_token, next_process, next_log


def close_resource_sets(
    installed: RemoteAttemptResources | None,
    *,
    profile: object,
    token: object,
    process: object,
    log: object,
    claim: AttemptCleanupClaim | None = None,
) -> tuple[RemoteAttemptResources, ...]:
    """Intentional close: installed bundle plus any in-flight host fields.

    The in-flight bundle reuses the running attempt's claim, so a close that
    lands before publication and the startup owner that later cancels cannot
    both stop the same owned token.
    """

    bundles: list[RemoteAttemptResources] = []
    if installed is not None:
        bundles.append(installed)
    extra_token = _extra_token(installed, token)
    extra_process = process if process is not None and (
        installed is None or process is not installed.process
    ) else None
    extra_log = log if log is not None and (installed is None or log is not installed.log) else None
    if extra_token or extra_process is not None or extra_log is not None:
        bundles.append(
            RemoteAttemptResources(
                generation=0,
                profile=profile,
                token=extra_token or "",
                process=extra_process,
                log=extra_log,
                claim=claim if claim is not None else AttemptCleanupClaim(),
            )
        )
    return tuple(bundles)


def _extra_token(installed: RemoteAttemptResources | None, token: object) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    if installed is not None and token == installed.token:
        return None
    return token
