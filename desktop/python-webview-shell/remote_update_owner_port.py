"""The remote update flow's ``OwnerStopPort``, over the existing SSH seam.

``RemoteOwnerStopPort`` is the production port the flow's stop stage calls.  It
does exactly two things, and it keeps them apart:

``stop(operation_id)``
    runs the EXISTING authenticated ``stop-owned`` command through
    ``ssot_connection.run_remote_stop_owned`` and reads back what that
    established.  There is no pid fallback and no second shutdown mechanism
    here; a stop this port cannot authenticate is refused, and a refusal is
    reported as a refusal.

``observe(operation_id)``
    runs ONLY the read-only observation helper.  Reconciliation is observation
    of the same retained operation, so this path never re-issues a stop, never
    signals and never removes a receipt.

Both then attach the two release observations the stop itself cannot make.
``remote_owner`` opens no port and no lease, so a stop result alone leaves
``listener_release`` and ``lease_release`` unknown; this port fills them from
``remote_update_owner_observation`` running on the remote host, and only where
that observation was taken against the selected workspace.

The desktop's own forwarded port is never used for any of this.  Closing the
local SSH process frees the Windows-side forward while the remote listener and
the remote writer lease can both survive -- that is the 1.0.8 incident -- so a
forward observation and a general reachability probe are both incapable of
answering ``listener_release`` and neither is consulted.

The observation helper runs from an explicitly supplied, separately prepared
location -- never from the app being stopped, which may be the old release
whose behaviour is exactly what is in question.  Without such a location no
observation is taken and both releases stay unknown.

Everything that reaches the network is one of two fixed argv shapes built from
the profile through the existing ``remote_command_contract`` validators, run
with a deadline, with stdin closed and output captured.  No raw command
diagnostic is ever surfaced: the only text that leaves this module is the
bounded, sanitized ``detail`` of a decoded contract.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace

from remote_command_contract import (
    join_remote_tokens,
    require_session_token,
    require_remote_python,
    require_workspace_uid,
    scan_tokens_for_live_ssh,
    validated_posix_path,
)
from remote_stop_result import (
    STOP_CONFIRMED_ALREADY_EXITED,
    result_from_launch,
    STOP_REQUEST_TIMED_OUT,
    STOP_REQUEST_TIMEOUT_SECONDS,
    StopResult,
)
from remote_update_flow_contract import LostResponse, OwnerStopFacts
from remote_update_owner_observation import (
    OBSERVE_COMMAND,
    WORKSPACE_BOUND_BINDINGS,
    OwnerReleaseObservation,
    decode_owner_release,
)
from ssot_connection import (
    RemoteConnectionProfile,
    find_ssh_executable,
    run_remote_stop_owned,
)


OBSERVATION_RELATIVE = "desktop/python-webview-shell/remote_update_owner_observation.py"

#: The observation is read-only and answers from local state; it never waits on
#: a process to go.  Kept well inside the window-close budget the stop request
#: already lives in.
OBSERVE_TIMEOUT_SECONDS = 4.0

#: Nothing this command can legitimately print approaches this, and the codec
#: bounds the line it reads anyway.  The cap is here so a remote that floods
#: stdout cannot make the desktop hold it.
MAX_OBSERVATION_BYTES = 64 * 1024


def observation_path(observation_app_dir: str) -> str:
    """Where the checked-in observation helper runs from.

    Deliberately NOT ``profile.remote_app_dir``.  The app being stopped may be
    the old release -- 1.0.8 is exactly the case this observation exists for --
    and a helper loaded out of it would be whatever that release shipped, which
    is not this contract and may not be there at all.  The execution location
    is therefore an explicit input the caller supplies, verified and prepared
    separately from the app under observation.
    """

    root = validated_posix_path(observation_app_dir, "observation_app_dir")
    return validated_posix_path(f"{root}/{OBSERVATION_RELATIVE}", "remote_observation")


def observe_owner_tokens(
    *,
    remote_python: object,
    observation_app_dir: str,
    remote_data_dir: str,
    listener_port: int,
    workspace_id: object,
    session_token: object,
) -> list[str]:
    """The one fixed argv this port ever runs on the remote host."""

    python = require_remote_python(remote_python)
    helper = observation_path(observation_app_dir)
    data = validated_posix_path(remote_data_dir, "remote_data_dir")
    workspace = require_workspace_uid(workspace_id)
    token = require_session_token(session_token)
    if isinstance(listener_port, bool) or not isinstance(listener_port, int):
        raise ValueError("listener port must be an integer from 1 to 65535")
    if not 0 < listener_port < 65536:
        raise ValueError("listener port must be an integer from 1 to 65535")
    return [
        python,
        "-I",
        "-B",
        helper,
        OBSERVE_COMMAND,
        "--data-dir",
        data,
        "--listener-port",
        str(listener_port),
        "--workspace-id",
        workspace,
        "--session-token",
        token,
    ]


def join_observe_owner_command(
    *,
    remote_python: object,
    observation_app_dir: str,
    remote_data_dir: str,
    listener_port: int,
    workspace_id: object,
    session_token: object,
) -> str:
    tokens = observe_owner_tokens(
        remote_python=remote_python,
        observation_app_dir=observation_app_dir,
        remote_data_dir=remote_data_dir,
        listener_port=listener_port,
        workspace_id=workspace_id,
        session_token=session_token,
    )
    scan_tokens_for_live_ssh(tokens)
    return join_remote_tokens(tokens)


def build_ssh_observe_owner_command(
    profile: RemoteConnectionProfile,
    ssh_executable: str,
    session_token: object,
    *,
    observation_app_dir: str,
) -> list[str]:
    """The same transport shape the stop request already uses.

    ``BatchMode`` so it can never prompt, strict host key checking so it can
    never be answered by an unknown host, and a connect timeout so a dead
    route settles instead of hanging.  It forwards nothing and allocates no
    tty: this is a read, not a session.

    The profile supplies what is being observed -- the data directory, the
    served listener port and the workspace -- while ``observation_app_dir``
    supplies where the observing helper itself runs from.  The two are
    separate on purpose and never default into each other.
    """

    remote_observe = join_observe_owner_command(
        remote_python=profile.remote_python,
        observation_app_dir=observation_app_dir,
        remote_data_dir=profile.remote_data_dir,
        listener_port=profile.remote_port,
        workspace_id=profile.workspace_id,
        session_token=session_token,
    )
    return [
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        profile.ssh_host_alias,
        remote_observe,
    ]


def _bounded_stdout(value: object) -> str:
    """Bounded text from the observation command, and nothing else.

    Only stdout is ever read.  stderr is deliberately dropped: it is raw
    remote diagnostic text, this port has no bounded vocabulary for it, and
    the contract carries everything a caller is allowed to learn.
    """

    if isinstance(value, (bytes, bytearray)):
        value = bytes(value[:MAX_OBSERVATION_BYTES]).decode("utf-8", "replace")
    if not isinstance(value, str):
        return ""
    return value[:MAX_OBSERVATION_BYTES]


def run_remote_observe_owner(
    profile: RemoteConnectionProfile,
    ssh_executable: str,
    session_token: object,
    *,
    observation_app_dir: str | None,
    timeout: float = OBSERVE_TIMEOUT_SECONDS,
    runner: object = None,
) -> OwnerReleaseObservation | None:
    """Ask the remote what it can see, and return only what it actually said.

    ``None`` means no readable observation came back at all -- the command
    could not be built, could not be run, did not answer in time, or answered
    with something that is not this contract.  That is different from an
    observation whose fields say ``unknown``, and the caller keeps the
    distinction.

    ``observation_app_dir`` of ``None`` means no verified execution location
    for the helper has been prepared on that host.  Nothing is then run and
    nothing is claimed -- the releases stay unknown -- rather than falling back
    to the app under observation, which may be the very release whose
    behaviour is in question.

    The exit status is not consulted.  The helper always exits ``0`` precisely
    so that a status can never be mistaken for evidence; only an emitted,
    decodable contract says anything.
    """

    if observation_app_dir is None:
        return None
    try:
        command = build_ssh_observe_owner_command(
            profile,
            ssh_executable,
            session_token,
            observation_app_dir=observation_app_dir,
        )
    except Exception:  # noqa: BLE001  a command that cannot be built was never run
        return None
    if runner is not None:
        try:
            completed = runner(command)
        except Exception:  # noqa: BLE001  an injected seam that failed observed nothing
            return None
        return decode_owner_release(_bounded_stdout(getattr(completed, "stdout", None)))
    try:
        completed = subprocess.run(
            command,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        # Deliberately not the exception text: it carries the argv, and the
        # argv carries the raw session token.
        return None
    return decode_owner_release(_bounded_stdout(completed.stdout))


def facts_from_stop_result(result: StopResult) -> OwnerStopFacts:
    """Project the stop outcome onto exactly the flow's six owner fields."""

    return OwnerStopFacts(**result.owner_facts())  # type: ignore[arg-type]


def apply_observation(
    result: StopResult, observation: OwnerReleaseObservation | None
) -> StopResult:
    """Fold a read-only observation into a stop outcome, upgrades only.

    Two rules, and they are the whole of it:

    * The listener and the lease are attached whenever the observation passed
      its workspace gate.  A stop can never establish them -- ``remote_owner``
      opens neither a port nor a lease -- so there is nothing to overwrite.
    * A proven exit may be *added* by an observation that was bound to this
      session's own owner and found it gone.  Nothing else about the exit
      moves: an observation never downgrades a confirmed stop, and it can
      never lift a refusal, because a caller with no standing to stop that
      receipt has no standing to declare its owner stopped either.

    An added exit takes ``stop_confirmed_already_exited`` with it.  That code
    is the one confirmed member which constrains no capability -- it is what
    an emitter says when the classification already read the recorded owner as
    dead, which is exactly what happened here -- so the record stays
    self-consistent under ``report_contradiction`` instead of carrying a
    proven exit under a code that confirms nothing.
    """

    if observation is None or observation.binding not in WORKSPACE_BOUND_BINDINGS:
        return result
    merged = result.with_observations(
        listener_release=observation.listener_release,
        lease_release=observation.lease_release,
    )
    if not _exit_upgrade_allowed(merged, observation):
        return merged
    return replace(
        merged,
        code=STOP_CONFIRMED_ALREADY_EXITED,
        state="dead",
        token_available=True,
        process_exit="verified",
        detail="the recorded owner was observed gone after the stop request",
    )


def _exit_upgrade_allowed(
    result: StopResult, observation: OwnerReleaseObservation
) -> bool:
    """May this observation add the proven exit the stop did not establish?

    Only when it observed THIS session's own owner -- the receipt recognised
    the token -- and found it gone, and only when the stop was neither already
    confirmed nor refused.  A refused stop keeps its refusal: the operator
    step it names is what has to happen next.
    """

    return (
        observation.bound
        and observation.process_exit == "verified"
        and observation.owner_state == "dead"
        and result.process_exit != "verified"
        and not result.refused
        and result.token_available is not False
    )


def facts_from_observation(
    observation: OwnerReleaseObservation | None, *, pidfd_available: bool | None
) -> OwnerStopFacts:
    """Read-only facts, with no stop request behind them at all.

    ``pidfd_available`` is carried in from whatever the last real stop
    established, because that capability describes the host rather than this
    observation, and re-reporting it unknown would lose a measured answer.
    """

    if observation is None:
        return OwnerStopFacts(pidfd_available=pidfd_available)
    return OwnerStopFacts(
        state=observation.owner_state,
        token_available=observation.token_available,
        pidfd_available=pidfd_available,
        process_exit=observation.process_exit,
        listener_release=observation.listener_release,
        lease_release=observation.lease_release,
    )


class RemoteOwnerStopPort:
    """The flow's owner stop port, bound to one selected remote profile.

    Narrow on purpose: a profile, the session token that authenticates against
    that profile's owner receipt, the verified location the observation helper
    runs from, and the injected seams the tests and the host already use.  The
    workspace binding is the profile's own ``workspace_id``; this port never
    accepts one from anywhere else, so an observation can only ever be taken
    against the store the desktop selected.

    ``observation_app_dir`` is required and has no default.  It is where the
    checked-in observation helper executes, which is not the app being
    stopped: that app may be the old release, and a helper loaded out of it
    would be whatever that release shipped.  ``None`` states plainly that no
    such location has been prepared on this host, and then no observation is
    taken and both releases stay unknown -- never a silent fallback to the app
    under observation.
    """

    def __init__(
        self,
        profile: RemoteConnectionProfile,
        session_token: object,
        *,
        observation_app_dir: str | None,
        ssh_executable: str | None = None,
        stop_timeout: float = STOP_REQUEST_TIMEOUT_SECONDS,
        observe_timeout: float = OBSERVE_TIMEOUT_SECONDS,
        runner: object = None,
    ) -> None:
        self._profile = profile
        self._session_token = session_token
        self._observation_app_dir = observation_app_dir
        self._ssh_executable = ssh_executable
        self._stop_timeout = stop_timeout
        self._observe_timeout = observe_timeout
        self._runner = runner
        self._last_result: StopResult | None = None
        self._last_observation: OwnerReleaseObservation | None = None
        self._pidfd_available: bool | None = None

    @property
    def last_result(self) -> StopResult | None:
        """The last stop outcome, for the snapshot and diagnostics glue.

        ``detail`` on it is sanitized but remote-influenced; it is a bounded
        symbolic record for a reader, never markup and never a command.
        """

        return self._last_result

    @property
    def last_observation(self) -> OwnerReleaseObservation | None:
        """The last read-only observation, kept apart from the stop outcome.

        Two records because they are two things.  Folding an observation back
        into the stop's own outcome would let a later read rewrite what a
        request established, which is the collapse this whole module exists to
        prevent.
        """

        return self._last_observation

    def _ssh(self) -> str:
        return self._ssh_executable or find_ssh_executable()

    def stop(self, operation_id: str) -> OwnerStopFacts:
        """Request the authenticated stop, then observe what it released.

        A request that timed out may have signalled: the far side's answer is
        lost, not negative, so this raises :class:`LostResponse` for the flow
        to reconcile the same identity through :meth:`observe`.  Every other
        condition -- a refusal, a launch that never happened, a remote that
        reported nothing -- comes back as facts that say so.
        """

        result = self._request_stop()
        if result.pidfd_available is not None:
            self._pidfd_available = result.pidfd_available
        if result.code == STOP_REQUEST_TIMED_OUT:
            self._last_result = result
            raise LostResponse(operation_id)
        observation = self._observe_release()
        self._last_observation = observation
        result = apply_observation(result, observation)
        self._last_result = result
        return facts_from_stop_result(result)

    def observe(self, operation_id: str) -> OwnerStopFacts:
        """Re-read the same owner, requesting nothing.

        This is the reconciliation path.  It issues no stop, so it can neither
        commit nor repeat the mutation whose answer was lost; it can only
        report what the remote host can currently see about the owner, its
        original listener and its writer lease.
        """

        observation = self._observe_release()
        self._last_observation = observation
        return facts_from_observation(
            observation, pidfd_available=self._pidfd_available
        )

    def _request_stop(self) -> StopResult:
        """Run the authenticated stop, and settle its launch here as well.

        ``run_remote_stop_owned`` settles a timeout and a launch failure for
        the calls it makes itself, but an INJECTED runner -- the existing host
        seam -- raises through it.  A port that let that escape would hand the
        flow an exception it does not catch, and the flow would lose a
        mutation whose answer is exactly the one that has to be reconciled.
        So both shapes settle to the same bounded outcome here.
        """

        try:
            return run_remote_stop_owned(
                self._profile,
                self._ssh(),
                self._session_token,
                timeout=self._stop_timeout,
                runner=self._runner,
            )
        except subprocess.TimeoutExpired:
            return result_from_launch(returncode=None, timed_out=True)
        except (OSError, subprocess.SubprocessError):
            # Deliberately not the exception text: it carries the argv, and
            # the argv carries the raw session token.
            return result_from_launch(
                returncode=None, launch_error="the stop command could not be run"
            )

    def _observe_release(self) -> OwnerReleaseObservation | None:
        return run_remote_observe_owner(
            self._profile,
            self._ssh(),
            self._session_token,
            observation_app_dir=self._observation_app_dir,
            timeout=self._observe_timeout,
            runner=self._runner,
        )
