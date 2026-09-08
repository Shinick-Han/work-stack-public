"""Immutable per-attempt remote resources and generation-gated host commits.

Each start captures generation, profile, token, process and log once. Stale
cleanup uses only that bundle. The connection monitor is published the same
way: one revocable lease per captured generation. The gate lock is for
ownership decisions and host-field commits; callers must not hold it during
blocking SSH, a process wait or a monitor start/join.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from remote_command_contract import generate_session_token, token_hash
from ssot_connection import build_ssh_tunnel_command, find_ssh_executable


WAIT_STALE = "stale"
WAIT_EXITED = "exited"
WAIT_READY = "ready"
WAIT_POLL = "poll"
WAIT_TIMEOUT = "timeout"
WAIT_VERIFY_FAILED = "verify-failed"

CLEANUP_STALE = "stale"
CLEANUP_FAILED = "failed"
CLEANUP_STOPPED = "stopped"

CLAIM_STOP_OWNED = "stop-owned"
CLAIM_PROCESS = "process"
CLAIM_LOG = "log"


class AttemptCleanupClaim:
    """One-shot tickets for the externally visible cleanups of one attempt.

    Close, the stale waiter, failure cleanup and pre-publication cancellation
    all race for the same claim, so each remote stop, each captured process
    reap and each captured log close happens exactly once. The ticket set is
    bounded by the three effects above and dies with its attempt; no history
    is retained anywhere.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._taken: set[str] = set()

    def take(self, effect: str) -> bool:
        with self._lock:
            if effect in self._taken:
                return False
            self._taken.add(effect)
            return True


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


class RemoteAttemptResourceGate:
    """Pointer to the bundle currently installed on the host fields.

    Every method requires the caller to hold the host's single lifecycle
    lock; the gate owns no second lock of its own.
    """

    def __init__(self) -> None:
        self._current: RemoteAttemptResources | None = None

    def current_locked(self) -> RemoteAttemptResources | None:
        return self._current

    def install_locked(self, resources: RemoteAttemptResources) -> None:
        self._current = resources

    def detach_if_current_locked(self, resources: RemoteAttemptResources) -> bool:
        current = self._current
        if current is None or not same_installed_bundle(current, resources):
            return False
        self._current = None
        return True

    def detach_current_locked(self) -> RemoteAttemptResources | None:
        resources = self._current
        self._current = None
        return resources


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


def observe_wait_tick(*, current: bool, process_exited: bool, ready: bool) -> str:
    if not current:
        return WAIT_STALE
    if process_exited:
        return WAIT_EXITED
    if ready:
        return WAIT_READY
    return WAIT_POLL


def captured_process_exited(resources: RemoteAttemptResources) -> bool:
    process = resources.process
    if process is None:
        return False
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return False
    return poll() is not None


def run_ready_wait(
    resources: RemoteAttemptResources,
    *,
    is_current: Callable[[], bool],
    after_generation_check: Callable[[RemoteAttemptResources], None],
    is_ready: Callable[[], bool],
    complete_ready: Callable[[], str],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout: float = 25.0,
    interval: float = 0.25,
) -> str:
    """Wait without holding a lifecycle lock. Uses only captured process state."""

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        current = is_current()
        after_generation_check(resources)
        exited = captured_process_exited(resources)
        ready = False if (not current or exited) else is_ready()
        tick = observe_wait_tick(current=current, process_exited=exited, ready=ready)
        if tick == WAIT_POLL:
            sleep(interval)
            continue
        if tick == WAIT_READY:
            return complete_ready()
        return tick
    if not is_current():
        return WAIT_STALE
    return WAIT_TIMEOUT


def initialize_attempt_resources(host: object) -> None:
    """Publish the one lifecycle lock, gate and monitor slot before any thread.

    Production calls this from `__init__`; a test host built with
    `object.__new__` must call it explicitly. There is deliberately no
    first-use fallback: two callers must never enter the protocol under two
    different locks.
    """

    host.remote_resource_lock = threading.RLock()
    host.remote_attempt_gate = RemoteAttemptResourceGate()
    host.remote_attempt_claim = None
    host.remote_monitor_lease = None


class MonitorLease:
    """The right to run one connection monitor for one captured generation.

    Publication installs the lease under the host lifecycle lock; the start
    happens outside it. The lease's own lock guards only the three flags below
    and is never held across `start` or `stop`, so a slow monitor can never
    block a caller that holds the host lifecycle lock. A close therefore always
    wins: it either revokes before the start, which then never happens, or it
    revokes during the start window and the starting thread performs the
    bounded stop itself, outside every lock.
    """

    def __init__(self, monitor: object, attempt_id: str) -> None:
        self.monitor = monitor
        self.attempt_id = attempt_id
        self._lock = threading.Lock()
        self._starting = False
        self._started = False
        self._revoked = False

    def start_if_live(self) -> bool:
        """Start this monitor unless a close already revoked the lease."""

        with self._lock:
            if self._revoked:
                return False
            self._starting = True
        try:
            self.monitor.start()
        finally:
            with self._lock:
                self._starting = False
                self._started = True
                revoked = self._revoked
        if not revoked:
            return True
        # The close could not join a monitor that had not started yet, so the
        # thread that did start it stops it here, holding no lock.
        self.monitor.stop(timeout=5)
        return False

    def revoke(self) -> object | None:
        """Forbid any later start; return the monitor a caller must still join.

        `None` means no caller has to join: either the lease never started a
        thread, or a start is still in flight and `start_if_live` will stop it.
        """

        with self._lock:
            revoked, self._revoked = self._revoked, True
            if revoked or self._starting or not self._started:
                return None
            return self.monitor

    def occupies_slot(self) -> bool:
        """A pending or running monitor still owns the published slot.

        Read without the lease lock, so asking under the host lifecycle lock
        can never wait on a monitor that is slow to start.
        """

        if self._revoked:
            return False
        if not self._started:
            return True
        return bool(getattr(self.monitor, "is_running", False))


def _monitor_slot_occupied(host: object) -> bool:
    lease = getattr(host, "remote_monitor_lease", None)
    if isinstance(lease, MonitorLease):
        return lease.occupies_slot()
    monitor = getattr(host, "remote_monitor", None)
    return monitor is not None and bool(getattr(monitor, "is_running", False))


def monitor_slot_occupied(host: object) -> bool:
    with resource_lock_for(host):
        return _monitor_slot_occupied(host)


def publish_monitor_lease(
    host: object, lease: MonitorLease, *, still_admitted: Callable[[], bool]
) -> bool:
    """Install one monitor for one generation, or publish nothing at all.

    The captured generation is revalidated under the same lifecycle lock the
    ready commit uses, so a callback whose attempt was closed can never
    overwrite a newer generation's monitor, and the slot admits one lease at a
    time. Starting the thread stays outside this lock.
    """

    with resource_lock_for(host):
        if not still_admitted() or _monitor_slot_occupied(host):
            return False
        host.remote_monitor_lease = lease
        host.remote_monitor = lease.monitor
        host.remote_monitor_attempt_id = lease.attempt_id
        return True


def take_published_monitor(host: object) -> tuple[object | None, str]:
    """Detach the published monitor under the lock; join it outside.

    Returns the monitor that still needs a bounded join, or ``None`` when the
    lease was revoked before it could start a thread.
    """

    with resource_lock_for(host):
        lease = getattr(host, "remote_monitor_lease", None)
        monitor = getattr(host, "remote_monitor", None)
        attempt_id = str(getattr(host, "remote_monitor_attempt_id", "") or "")
        host.remote_monitor_lease = None
        host.remote_monitor = None
        host.remote_monitor_attempt_id = ""
        if isinstance(lease, MonitorLease):
            return lease.revoke(), attempt_id
        return monitor, attempt_id


def initialize_remote_attempt_state(host: object, startup: object) -> None:
    """Publish the whole remote slice of host construction in one place.

    The lifecycle lock and gate, the attempt identity fields, the monitor slot
    and the remote coordination primitives all exist before the startup state
    machine is reachable, before any callback and before any thread.
    """

    initialize_attempt_resources(host)
    host.remote_startup = startup
    host.remote_lifecycle_state = "IDLE"
    host.remote_attempt_id = 0
    host.remote_ready_attempt_id = 0
    host.remote_session_token = None
    host.remote_session_token_hash = None
    host.remote_ssh_process = None
    host.remote_ssh_log = None
    host.remote_monitor = None
    host.remote_monitor_lease = None
    host.remote_monitor_attempt_id = ""
    host.remote_reconnect_lock = threading.Lock()
    host.remote_authority_lock = threading.RLock()
    host.remote_shutdown_requested = threading.Event()
    host.remote_recovery_required = threading.Event()
    host.remote_recovery_message = ""
    host.remote_rebind_target = ""
    host.remote_rebind_deadline = 0.0
    host.remote_product_version = ""
    host.remote_protocol_version = None
    host._stop_owned_runner = None


def _uninitialized(name: str) -> RuntimeError:
    return RuntimeError(
        f"remote attempt protocol used before {name} was published; "
        "call initialize_attempt_resources(host) in the constructor"
    )


def gate_for(host: object) -> RemoteAttemptResourceGate:
    gate = getattr(host, "remote_attempt_gate", None)
    if not isinstance(gate, RemoteAttemptResourceGate):
        raise _uninitialized("remote_attempt_gate")
    return gate


def resource_lock_for(host: object):
    lock = getattr(host, "remote_resource_lock", None)
    if lock is None:
        raise _uninitialized("remote_resource_lock")
    return lock


def attempt_is_current(host: object, generation: int) -> bool:
    machine = getattr(host, "remote_startup", None)
    if machine is not None:
        return bool(machine.is_current(str(generation)))
    return getattr(host, "remote_attempt_id", 0) == generation


def commit_resources(
    host: object,
    resources: RemoteAttemptResources,
    next_state: str | None = None,
) -> bool:
    """Install the frozen bundle onto host fields if this generation is current."""

    with resource_lock_for(host):
        if not attempt_is_current(host, resources.generation):
            return False
        gate_for(host).install_locked(resources)
        host.remote_session_token = resources.token
        host.remote_session_token_hash = token_hash(resources.token)
        host.remote_ssh_process = resources.process
        host.remote_ssh_log = resources.log
        if next_state is None:
            return True
        advance = getattr(host, "_advance_remote_startup")
        return bool(advance(resources.generation, next_state))


def detach_matching_host_fields(host: object, resources: RemoteAttemptResources) -> bool:
    gate = gate_for(host)
    if not gate.detach_if_current_locked(resources):
        return False
    token, process, log = host_fields_after_detach(
        resources,
        token=getattr(host, "remote_session_token", None),
        process=getattr(host, "remote_ssh_process", None),
        log=getattr(host, "remote_ssh_log", None),
    )
    host.remote_session_token = token
    if token is None:
        host.remote_session_token_hash = None
    host.remote_ssh_process = process
    host.remote_ssh_log = log
    return True


def snapshot_for_intentional_close(host: object) -> Sequence[RemoteAttemptResources]:
    installed = gate_for(host).detach_current_locked()
    bundles = close_resource_sets(
        installed,
        profile=getattr(host, "remote_profile", None),
        token=getattr(host, "remote_session_token", None),
        process=getattr(host, "remote_ssh_process", None),
        log=getattr(host, "remote_ssh_log", None),
        claim=getattr(host, "remote_attempt_claim", None),
    )
    host.remote_attempt_claim = None
    host.remote_session_token = None
    host.remote_session_token_hash = None
    host.remote_ready_attempt_id = 0
    host.remote_ssh_process = None
    host.remote_ssh_log = None
    return bundles


def finish_ready_wait(host: object, resources: RemoteAttemptResources) -> str:
    if not host._advance_remote_startup(resources.generation, "VERIFYING_AUTHORITY"):
        return WAIT_STALE
    try:
        host._verify_remote_workspace()
    except RuntimeError as error:
        host.remote_wait_error = error
        return WAIT_VERIFY_FAILED
    if not host._apply_remote_ready_if_current(resources.generation):
        return WAIT_STALE
    return WAIT_READY


def wait_failure_error(
    resources: RemoteAttemptResources,
    log_path: object,
    outcome: str,
    pending: object,
) -> RuntimeError:
    if outcome == WAIT_VERIFY_FAILED and isinstance(pending, RuntimeError):
        return pending
    if outcome == WAIT_EXITED:
        returncode = getattr(resources.process, "returncode", None)
        return RuntimeError(
            f"SSH remote Work Stack exited before becoming ready (exit {returncode}). "
            f"Review {log_path} and run --check-remote-connection."
        )
    return RuntimeError(
        f"SSH remote Work Stack did not become ready within 25 seconds. "
        f"Review {log_path} and run --check-remote-connection."
    )


def fire_barrier(host: object, name: str, resources: RemoteAttemptResources) -> None:
    hook = getattr(host, "_remote_barrier_" + name, None)
    if callable(hook):
        hook(resources)


def fire_generation_barrier(host: object, name: str, generation: int) -> None:
    hook = getattr(host, "_remote_barrier_" + name, None)
    if callable(hook):
        hook(generation)


def generation_current(host: object, generation: int) -> bool:
    with resource_lock_for(host):
        return attempt_is_current(host, generation)


def start_remote_attempt(host: object) -> AttemptStart:
    """Begin one attempt and publish its generation, token and cleanup claim."""

    with resource_lock_for(host):
        machine = getattr(host, "remote_startup", None)
        if machine is not None:
            attempt = machine.begin()
            host.remote_attempt_id = int(attempt)
            host.remote_lifecycle_state = machine.state.value
        else:
            host.remote_attempt_id = int(getattr(host, "remote_attempt_id", 0)) + 1
            host.remote_lifecycle_state = "PROBING"
        host.remote_ready_attempt_id = 0
        token = generate_session_token()
        host.remote_session_token = token
        host.remote_session_token_hash = token_hash(token)
        claim = AttemptCleanupClaim()
        host.remote_attempt_claim = claim
        return AttemptStart(int(host.remote_attempt_id), token, claim)


def begin_remote_attempt(host: object) -> int:
    return start_remote_attempt(host).generation


def commit_remote_ready(host: object, generation: int) -> bool:
    """Validate the generation, enter READY and publish the ready id at once.

    One commit under the single lifecycle lock: a close can only land wholly
    before it (the generation is gone, so nothing is published) or wholly
    after it (the close clears the published field). Blocking monitor work
    stays outside this lock.
    """

    with resource_lock_for(host):
        if not attempt_is_current(host, generation):
            return False
        advance = getattr(host, "_advance_remote_startup")
        if not advance(generation, "READY"):
            return False
        host.remote_ready_attempt_id = generation
        fire_generation_barrier(host, "ready_commit", generation)
        return True


def stop_remote_machine(host: object) -> None:
    machine = getattr(host, "remote_startup", None)
    if machine is not None:
        machine.stop()
        host.remote_lifecycle_state = machine.state.value
        host.remote_attempt_id = 0
        return
    if getattr(host, "remote_lifecycle_state", None) is not None:
        host.remote_lifecycle_state = "IDLE"


def fail_remote_attempt(host: object, attempt_id: int, error: object = None) -> None:
    if error is not None:
        host.remote_lifecycle_error = str(error)
    machine = getattr(host, "remote_startup", None)
    if machine is not None:
        machine.fail(str(attempt_id))
        host.remote_lifecycle_state = machine.state.value
    elif getattr(host, "remote_attempt_id", 0) == attempt_id:
        host.remote_lifecycle_state = "FAILED"


def clear_matching_token(host: object, attempt_id: int, token: object) -> None:
    with resource_lock_for(host):
        if not attempt_is_current(host, attempt_id):
            return
        if getattr(host, "remote_session_token", None) == token:
            host.remote_session_token = None
            host.remote_session_token_hash = None


def close_captured_log(log: object) -> None:
    closer = getattr(log, "close", None) if log is not None else None
    if callable(closer):
        closer()


def _release_stop_owned(host: object, resources: RemoteAttemptResources, trace: object) -> None:
    if not resources.token or resources.profile is None:
        return
    if not resources.claim.take(CLAIM_STOP_OWNED):
        return
    try:
        host._request_remote_stop_owned(resources.profile, resources.token)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        if callable(trace):
            trace("stop-owned request failed")


def _release_captured_process(host: object, process: object, trace: object) -> None:
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


def release_captured(host: object, resources: RemoteAttemptResources) -> None:
    """Release exactly the captured snapshot, each effect at most once."""

    trace = getattr(host, "_trace", None)
    _release_stop_owned(host, resources, trace)
    process = resources.process
    try:
        if process is not None and resources.claim.take(CLAIM_PROCESS):
            _release_captured_process(host, process, trace)
    finally:
        if resources.log is not None and resources.claim.take(CLAIM_LOG):
            close_captured_log(resources.log)


def cleanup_captured(host: object, resources: RemoteAttemptResources, terminal: str) -> None:
    with resource_lock_for(host):
        detached = detach_matching_host_fields(host, resources)
        if detached and terminal == CLEANUP_FAILED:
            fail_remote_attempt(host, resources.generation)
    release_captured(host, resources)


def spawn_remote_ssh(host: object, command: list[str]):
    launch_root = Path(host.state_root) / "desktop-launch"
    launch_root.mkdir(parents=True, exist_ok=True)
    log_path = launch_root / "remote-ssh.log"
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        log_handle.close()
        raise RuntimeError(f"Could not start OpenSSH: {error}") from error
    return process, log_handle, log_path


def wait_until_ready(host: object, resources: RemoteAttemptResources, log_path: object) -> None:
    outcome = run_ready_wait(
        resources,
        is_current=lambda: generation_current(host, resources.generation),
        after_generation_check=lambda captured: fire_barrier(host, "generation_check", captured),
        is_ready=host._is_ready,
        complete_ready=lambda: finish_ready_wait(host, resources),
        sleep=time.sleep,
        monotonic=time.monotonic,
    )
    if outcome == WAIT_READY:
        port = host.remote_profile.local_forward_port
        trace = getattr(host, "_trace", None)
        if callable(trace):
            trace(f"SSH remote Work Stack is ready through local port {port}")
        return
    if outcome == WAIT_EXITED and not generation_current(host, resources.generation):
        outcome = WAIT_STALE
    if outcome == WAIT_STALE:
        cleanup_captured(host, resources, CLEANUP_STALE)
        return
    error = wait_failure_error(
        resources, log_path, outcome, getattr(host, "remote_wait_error", None)
    )
    cleanup_captured(host, resources, CLEANUP_FAILED)
    raise error


def ensure_remote_server(host: object) -> None:
    profile = getattr(host, "remote_profile", None)
    if profile is None:
        raise RuntimeError("Remote server startup requested without an SSH profile")
    if host._is_ready():
        raise RuntimeError(
            f"Local forward port {profile.local_forward_port} is already serving Work Stack; "
            "close that process or choose another local_forward_port"
        )
    start = start_remote_attempt(host)
    attempt_id = start.generation
    session_token = start.token
    if not host._advance_remote_startup(attempt_id, "STARTING_TUNNEL"):
        return
    command = build_ssh_tunnel_command(
        profile, find_ssh_executable(), session_token=session_token
    )
    try:
        process, log_handle, log_path = spawn_remote_ssh(host, command)
    except RuntimeError:
        fail_remote_attempt(host, attempt_id)
        clear_matching_token(host, attempt_id, session_token)
        raise
    resources = RemoteAttemptResources(
        int(attempt_id), profile, str(session_token), process, log_handle, start.claim
    )
    fire_barrier(host, "process_started", resources)
    if not commit_resources(host, resources, "WAITING_REMOTE_READY"):
        cleanup_captured(host, resources, CLEANUP_STALE)
        return
    fire_barrier(host, "waiting_commit", resources)
    wait_until_ready(host, resources, log_path)


def stop_owned_connection(host: object) -> None:
    with resource_lock_for(host):
        stop_remote_machine(host)
        bundles = snapshot_for_intentional_close(host)
    host._stop_remote_monitor()
    for resources in bundles:
        release_captured(host, resources)
