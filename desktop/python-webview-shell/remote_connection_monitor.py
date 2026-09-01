"""Bounded in-process monitoring for an owned SSH connection.

This module deliberately performs no networking or process management.  The
desktop host supplies those operations as callbacks, which keeps monitoring
policy deterministic and independently testable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence


ConnectionCheck = Callable[[], bool]
Reconnect = Callable[[], bool]
StatePublisher = Callable[[str], None]
Action = Callable[[], None]

_MAX_RECONNECT_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 30.0


class RemoteConnectionMonitor:
    """Watch one desktop-owned connection until stopped or exhausted."""

    def __init__(
        self,
        *,
        is_healthy: ConnectionCheck,
        is_process_alive: ConnectionCheck,
        reconnect_once: Reconnect,
        publish_state: StatePublisher,
        reload_view: Action,
        is_recovery_required: ConnectionCheck = lambda: False,
        on_recovery_required: Action = lambda: None,
        initial_grace: float = 2.0,
        poll_interval: float = 5.0,
        failure_threshold: int = 2,
        reconnect_backoff: Sequence[float] = (1.0, 2.0),
        reconnect_grace: float = 1.0,
        max_reconnect_attempts: int = _MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        _validate_options(
            initial_grace=initial_grace,
            poll_interval=poll_interval,
            failure_threshold=failure_threshold,
            reconnect_grace=reconnect_grace,
            max_reconnect_attempts=max_reconnect_attempts,
        )
        self._is_healthy = is_healthy
        self._is_process_alive = is_process_alive
        self._reconnect_once = reconnect_once
        self._publish_state = publish_state
        self._reload_view = reload_view
        self._is_recovery_required = is_recovery_required
        self._on_recovery_required = on_recovery_required
        self._initial_grace = initial_grace
        self._poll_interval = poll_interval
        self._failure_threshold = failure_threshold
        self._backoff = _bounded_backoff(reconnect_backoff)
        self._reconnect_grace = reconnect_grace
        self._max_reconnect_attempts = max_reconnect_attempts
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_state: str | None = None

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start exactly one monitor thread; return False if already running."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._last_state = None
            thread = threading.Thread(
                target=self._run_and_release,
                name="workstack-remote-connection-monitor",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return True

    def stop(self, timeout: float | None = None) -> None:
        """Wake all waits and prevent any later reconnect attempt."""
        self._stop_event.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run_and_release(self) -> None:
        try:
            self._run()
        finally:
            with self._lifecycle_lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _run(self) -> None:
        if self._wait(self._initial_grace):
            return
        failures = 0
        while not self._stop_event.is_set():
            if self._finish_if_recovery_required():
                return
            observation = self._observe()
            if self._finish_if_recovery_required():
                return
            failures = 0 if observation == "ready" else failures + 1
            if observation == "dead" or failures >= self._failure_threshold:
                if self._recover() != "ready":
                    return
                failures = 0
            if self._wait(self._poll_interval):
                return

    def _finish_if_recovery_required(self) -> bool:
        if not _safe_check(self._is_recovery_required):
            return False
        self._publish("disconnected")
        _safe_action(self._on_recovery_required)
        return True

    def _observe(self) -> str:
        if not _safe_check(self._is_process_alive):
            return "dead"
        if not _safe_check(self._is_healthy):
            return "unhealthy"
        self._publish("ready")
        return "ready"

    def _recover(self) -> str:
        self._publish("reconnecting")
        for attempt in range(self._max_reconnect_attempts):
            if self._stop_event.is_set():
                return "stopped"
            if self._attempt_reconnect():
                return "ready"
            if self._wait_before_retry(attempt):
                return "stopped"
        self._publish("disconnected")
        return "disconnected"

    def _attempt_reconnect(self) -> bool:
        if not _safe_check(self._reconnect_once):
            return False
        if self._wait(self._reconnect_grace):
            return False
        verified = _safe_check(self._is_process_alive) and _safe_check(self._is_healthy)
        if verified and not self._stop_event.is_set():
            self._publish("ready")
            _safe_action(self._reload_view)
            return True
        return False

    def _wait_before_retry(self, attempt: int) -> bool:
        if attempt + 1 >= self._max_reconnect_attempts:
            return False
        return self._wait(_backoff_for(self._backoff, attempt))

    def _wait(self, seconds: float) -> bool:
        return self._stop_event.wait(seconds)

    def _publish(self, state: str) -> None:
        if state == self._last_state or self._stop_event.is_set():
            return
        self._last_state = state
        _safe_publish(self._publish_state, state)


def _validate_options(
    *,
    initial_grace: float,
    poll_interval: float,
    failure_threshold: int,
    reconnect_grace: float,
    max_reconnect_attempts: int,
) -> None:
    if initial_grace < 0 or reconnect_grace < 0 or poll_interval <= 0:
        raise ValueError("monitor timing values must be non-negative and poll_interval positive")
    if failure_threshold < 1:
        raise ValueError("failure_threshold must be positive")
    if not 1 <= max_reconnect_attempts <= _MAX_RECONNECT_ATTEMPTS:
        raise ValueError("max_reconnect_attempts must be between 1 and 3")


def _bounded_backoff(values: Sequence[float]) -> tuple[float, ...]:
    return tuple(min(_MAX_BACKOFF_SECONDS, max(0.0, float(value))) for value in values)


def _backoff_for(values: tuple[float, ...], attempt: int) -> float:
    if not values:
        return 0.0
    return values[min(attempt, len(values) - 1)]


def _safe_check(callback: ConnectionCheck) -> bool:
    try:
        return bool(callback())
    except Exception:
        return False


def _safe_action(callback: Action) -> None:
    try:
        callback()
    except Exception:
        return


def _safe_publish(callback: StatePublisher, state: str) -> None:
    try:
        callback(state)
    except Exception:
        return
