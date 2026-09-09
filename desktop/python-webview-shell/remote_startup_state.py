"""Deterministic Remote SSH startup attempt state machine.

Pure lifecycle only. Callers inject process, socket, registry, and UI effects
at the host boundary. This module does not import pywebview, start processes,
open sockets, read the registry, inspect live SSOT data, or consult the clock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum


class RemoteStartupState(str, Enum):
    IDLE = "IDLE"
    PROBING = "PROBING"
    STARTING_TUNNEL = "STARTING_TUNNEL"
    WAITING_REMOTE_READY = "WAITING_REMOTE_READY"
    VERIFYING_AUTHORITY = "VERIFYING_AUTHORITY"
    READY = "READY"
    MONITORING = "MONITORING"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


StartupObserver = Callable[[str, str], None]

LEGAL_TRANSITIONS: dict[RemoteStartupState, frozenset[RemoteStartupState]] = {
    RemoteStartupState.IDLE: frozenset({RemoteStartupState.PROBING}),
    RemoteStartupState.PROBING: frozenset({
        RemoteStartupState.STARTING_TUNNEL,
        RemoteStartupState.FAILED,
        RemoteStartupState.STOPPED,
    }),
    RemoteStartupState.STARTING_TUNNEL: frozenset({
        RemoteStartupState.WAITING_REMOTE_READY,
        RemoteStartupState.FAILED,
        RemoteStartupState.STOPPED,
    }),
    RemoteStartupState.WAITING_REMOTE_READY: frozenset({
        RemoteStartupState.VERIFYING_AUTHORITY,
        RemoteStartupState.FAILED,
        RemoteStartupState.STOPPED,
    }),
    RemoteStartupState.VERIFYING_AUTHORITY: frozenset({
        RemoteStartupState.READY,
        RemoteStartupState.FAILED,
        RemoteStartupState.STOPPED,
    }),
    RemoteStartupState.READY: frozenset({
        RemoteStartupState.MONITORING,
        RemoteStartupState.FAILED,
        RemoteStartupState.STOPPED,
    }),
    RemoteStartupState.MONITORING: frozenset({
        RemoteStartupState.FAILED,
        RemoteStartupState.STOPPED,
    }),
    RemoteStartupState.FAILED: frozenset(),
    RemoteStartupState.STOPPED: frozenset(),
}

READY_STATES = frozenset({RemoteStartupState.READY, RemoteStartupState.MONITORING})


def is_legal_transition(source: RemoteStartupState, target: RemoteStartupState) -> bool:
    return target in LEGAL_TRANSITIONS.get(source, frozenset())


class RemoteStartupStateMachine:
    """One monotonic attempt at a time; stale callbacks are no-ops."""

    def __init__(self, observer: StartupObserver | None = None) -> None:
        self._observer = observer
        self._lock = threading.Lock()
        self._generation = 0
        self._active_id: str | None = None
        self._state = RemoteStartupState.IDLE
        self._last_publication: tuple[str, str] | None = None
        self._monitor_started_for: str | None = None
        self._monitor_stopped_for: str | None = None

    @property
    def active_attempt_id(self) -> str | None:
        with self._lock:
            return self._active_id

    @property
    def state(self) -> RemoteStartupState:
        with self._lock:
            return self._state

    @property
    def last_attempt_generation(self) -> int:
        """The newest attempt id ever begun here, still readable after `stop`.

        `begin` is the only writer and only ever increments, so this is the
        machine's monotonic high-water mark. `fail` and `stop` clear the active
        attempt but never this count, so a caller that captured a generation
        can still tell, once that attempt is gone, whether a later attempt was
        begun in its place - which the active id alone cannot say. Zero means
        no attempt has ever been begun. Read-only: nothing is published and no
        transition is performed.
        """

        with self._lock:
            return self._generation

    def is_current(self, attempt_id: str) -> bool:
        with self._lock:
            return self._active_id == attempt_id

    def begin(self) -> str:
        """Invalidate every prior callback, then enter PROBING with a newer ID."""
        with self._lock:
            self._generation += 1
            attempt_id = str(self._generation)
            self._active_id = attempt_id
            self._state = RemoteStartupState.PROBING
            self._monitor_started_for = None
            self._monitor_stopped_for = None
            self._publish_locked(attempt_id, self._state)
            return attempt_id

    def advance(self, attempt_id: str, target: RemoteStartupState) -> bool:
        with self._lock:
            return self._commit_locked(attempt_id, target)

    def fail(self, attempt_id: str) -> bool:
        return self.advance(attempt_id, RemoteStartupState.FAILED)

    def observe(self, attempt_id: str, target: RemoteStartupState) -> bool:
        """Callback entry. Stale or illegal targets do not mutate the current attempt."""
        return self.advance(attempt_id, target)

    def stop(self) -> str | None:
        """Invalidate the active attempt before the host joins threads or closes processes."""
        with self._lock:
            previous = self._active_id
            if previous is None and self._state is RemoteStartupState.STOPPED:
                return None
            self._active_id = None
            self._state = RemoteStartupState.STOPPED
            if previous is not None:
                self._publish_locked(previous, self._state)
            return previous

    def can_start_monitor(self, attempt_id: str) -> bool:
        with self._lock:
            if self._active_id != attempt_id or self._state is not RemoteStartupState.READY:
                return False
            return self._monitor_started_for != attempt_id

    def mark_monitor_started(self, attempt_id: str) -> bool:
        with self._lock:
            if not self._commit_locked(attempt_id, RemoteStartupState.MONITORING):
                return False
            self._monitor_started_for = attempt_id
            return True

    def mark_monitor_stopped(self, attempt_id: str) -> bool:
        with self._lock:
            if self._monitor_started_for != attempt_id or self._monitor_stopped_for == attempt_id:
                return False
            self._monitor_stopped_for = attempt_id
            return True

    def _commit_locked(self, attempt_id: str, target: RemoteStartupState) -> bool:
        if self._active_id != attempt_id:
            return False
        if target in READY_STATES and self._state not in {
            RemoteStartupState.VERIFYING_AUTHORITY,
            RemoteStartupState.READY,
        }:
            return False
        if not is_legal_transition(self._state, target):
            return False
        self._state = target
        self._publish_locked(attempt_id, target)
        return True

    def _publish_locked(self, attempt_id: str, state: RemoteStartupState) -> None:
        publication = (attempt_id, state.value)
        if publication == self._last_publication:
            return
        self._last_publication = publication
        observer = self._observer
        if observer is None:
            return
        try:
            observer(attempt_id, state.value)
        except Exception:
            return
