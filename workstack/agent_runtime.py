"""Compose admitted authority, one selected backend, and frozen command I/O."""

from __future__ import annotations

import argparse
import http.client
import json
import pathlib
import typing

from workstack.agent_authority import admit_authority
from workstack.agent_cli_contract import (
    CHECKPOINT_COMMAND,
    CONTEXT_COMMAND,
    STATUS_COMMAND,
    AgentOutcome,
    AuthorityAdmission,
    ContextRequest,
    RuntimeDependencies,
    StatusRequest,
    parse_checkpoint_packet,
    render_outcome,
)
from workstack.agent_command_checkpoint import handle_checkpoint
from workstack.agent_command_context import datetime as _context_datetime
from workstack.agent_command_context import handle_context
from workstack.agent_command_status import handle_status
from workstack.agent_context_brief import (
    BriefTooLarge,
    MARKDOWN_FORMAT,
    render_context_brief,
)
from workstack.agent_local_backend import create_local_backend
from workstack.agent_transport import create_running_server_backend
from workstack.owner_authority import EXCLUSIVE_LOCAL_HELD, OwnerAuthority
from workstack.store import JOURNAL_NAME, Store


__all__ = ["run_agent_command"]


_HTTP_TIMEOUT_SECONDS = 10
_COMMANDS = frozenset({STATUS_COMMAND, CONTEXT_COMMAND, CHECKPOINT_COMMAND})
_DEFAULT_CONTEXT_VIEW = "core-v1"
_ADMISSION_ERRORS = frozenset(
    {"invalid_authority", "capability_not_enabled", "workspace_mismatch"}
)


class _HttpJsonRequester:
    def request(
        self,
        *,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(
            host,
            port,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
                raise OSError("server response is invalid") from error
            if type(payload) is not dict:
                raise OSError("server response is invalid")
            return response.status, payload
        finally:
            connection.close()


def _store_factory(*, root: pathlib.Path) -> Store:
    return Store(root)


def _default_runtime_dependencies() -> RuntimeDependencies:
    return RuntimeDependencies(
        admit_authority=admit_authority,
        create_local_backend=create_local_backend,
        create_running_server_backend=create_running_server_backend,
        request_json=_HttpJsonRequester(),
        store_factory=_store_factory,
        today=_context_datetime.date.today,
    )


class _AdmittedStoreFactory:
    """Return the already-created admitted Store to the local backend."""

    def __init__(self, *, admission: AuthorityAdmission, store: Store) -> None:
        self._admission = admission
        self._store = store

    def __call__(self, *, root: pathlib.Path) -> Store:
        if root != self._admission.data_dir:
            raise ValueError("invalid_authority")
        return self._store


class _RunningBackendFailureRecorder:
    """Preserve the frozen online failure class after pure handlers redact exceptions."""

    def __init__(self, backend) -> None:
        self._backend = backend
        self.error_code: str | None = None

    def _call(self, method: str, **kwargs):
        try:
            return getattr(self._backend, method)(**kwargs)
        except (OSError, TimeoutError):
            self.error_code = "owner_unavailable"
            raise
        except ValueError:
            self.error_code = "workspace_mismatch"
            raise

    def status(self, *, request):
        return self._call("status", request=request)

    def context(self, *, request, today):
        return self._call("context", request=request, today=today)

    def checkpoint(self, *, request):
        return self._call("checkpoint", request=request)


def _command_name(action: object) -> str:
    if type(action) is str and action in _COMMANDS:
        return "agent.{}".format(action)
    return "agent.{}".format(STATUS_COMMAND)


def _failure(*, command: str, code: str) -> AgentOutcome:
    return AgentOutcome(
        command=command,
        commit_state=None,
        data=None,
        error_code=code,
        error_details={},
        error_message=code,
        intent_id=None,
        replayed=None,
        retryable=None,
        task_id=None,
        transport=None,
        workspace_uid=None,
    )


def _data_dir(value: object) -> pathlib.Path:
    if isinstance(value, pathlib.Path):
        return value
    if type(value) is str and value:
        return pathlib.Path(value)
    raise ValueError("invalid_authority")


def _admission_error(error: BaseException) -> str:
    if isinstance(error, ValueError) and len(error.args) == 1:
        code = error.args[0]
        if type(code) is str and code in _ADMISSION_ERRORS:
            return code
    return "internal_error"


class _OwnerUnavailable(Exception):
    """No compliant owner answers: the lease is held and no route is advertised."""


class _RecoveryBlocked(Exception):
    """A pending recovery journal is present: no agent command may consume it."""


def _pending_journal_present(path: pathlib.Path) -> bool:
    """Report a pending recovery journal fail-closed, without following links.

    ``lstat`` answers about the journal name itself, so a dangling symlink and
    an entry of any other type are still evidence and never read as absence.
    Only a definite "this name does not exist" is absence: every other
    inspection failure (permission, name-resolution, an unreadable parent)
    leaves the presence of authoritative recovery evidence unknown, and unknown
    is treated as pending. Refusing a command costs nothing; silently replaying
    a journal Work Stack could not even inspect would consume the evidence.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _owner_metadata_present(path: pathlib.Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise OSError("server ownership metadata cannot be inspected") from error
    return True


def _select_backend(
    *,
    admission: AuthorityAdmission,
    expected_workspace_uid: str,
    dependencies: RuntimeDependencies,
) -> tuple[object, OwnerAuthority | None]:
    """Select one backend from the real writer lease, never from metadata.

    The canonical ``.workstack.lock`` lease is attempted exactly once. A
    retained handle is the only positive evidence that no compliant owner
    exists, so it is kept on this exact Store for the whole command: the local
    backend joins it instead of taking a second lease. A refused attempt means
    a compliant writer already owns the data directory; the only lawful route
    is then the advertised loopback owner, and a missing advertisement refuses
    rather than probing, reclaiming, or retrying the acquisition.

    Selecting local backend construction is not read-only: it initializes the
    Store, and a depth-zero transaction replays a pending recovery journal.
    A journal may therefore appear in the window between the caller's preflight
    and this acquisition, so presence is rechecked once the lease is actually
    retained, while the writer is this process and the answer cannot go stale.
    The retained handle is released and the command refuses; the lease is never
    released and reacquired to look again, because that would hand the data
    directory to another writer mid-decision.
    """

    store = dependencies.store_factory(root=admission.data_dir)
    lease = store.try_acquire_writer_lease()
    if lease is None:
        if not _owner_metadata_present(store.server_info_path):
            raise _OwnerUnavailable()
        return (
            _RunningBackendFailureRecorder(
                dependencies.create_running_server_backend(
                    server_info_path=store.server_info_path,
                    expected_workspace_uid=expected_workspace_uid,
                    request_json=dependencies.request_json,
                )
            ),
            None,
        )
    authority = OwnerAuthority(
        state=EXCLUSIVE_LOCAL_HELD,
        store=store,
        lease=lease,
        workspace_uid=admission.workspace_uid,
        data_dir=admission.data_dir,
    )
    try:
        if _pending_journal_present(store.journal_path):
            raise _RecoveryBlocked()
        backend = dependencies.create_local_backend(
            admission=admission,
            store_factory=_AdmittedStoreFactory(admission=admission, store=store),
        )
    except BaseException:
        _release_authority(authority)
        raise
    return backend, authority


def _release_authority(authority: OwnerAuthority | None) -> None:
    """Release the retained lease exactly once, without masking the outcome."""

    if authority is None:
        return
    try:
        authority.release()
    except Exception:
        pass


def _handle_command(
    *,
    args: argparse.Namespace,
    admission: AuthorityAdmission,
    backend,
    dependencies: RuntimeDependencies,
) -> AgentOutcome:
    action = getattr(args, "action", None)
    if action == STATUS_COMMAND:
        return handle_status(
            request=StatusRequest(
                data_dir=admission.data_dir,
                expected_workspace_uid=getattr(args, "workspace_uid", None),
            ),
            backend=backend,
        )
    if action == CONTEXT_COMMAND:
        return handle_context(
            request=ContextRequest(
                task_id=getattr(args, "task", None),
                view=getattr(args, "view", None) or _DEFAULT_CONTEXT_VIEW,
            ),
            backend=backend,
            today=dependencies.today(),
        )
    if action == CHECKPOINT_COMMAND:
        try:
            request = parse_checkpoint_packet(
                raw=getattr(args, "checkpoint_raw", None),
                intent_id=getattr(args, "intent_id", None),
            )
        except ValueError:
            return _failure(command="agent.checkpoint", code="invalid_body")
        return handle_checkpoint(request=request, backend=backend)
    return _failure(command=_command_name(action), code="internal_error")


def _dispatch(
    *,
    args: argparse.Namespace,
    dependencies: RuntimeDependencies,
) -> AgentOutcome:
    action = getattr(args, "action", None)
    command = _command_name(action)
    expected_workspace_uid = getattr(args, "workspace_uid", None)
    try:
        admission = dependencies.admit_authority(
            data_dir=_data_dir(getattr(args, "data_dir", None)),
            expected_workspace_uid=expected_workspace_uid,
        )
    except Exception as error:
        return _failure(command=command, code=_admission_error(error))

    # A pending recovery journal is authoritative evidence of an interrupted
    # commit, and the ordinary owner-authority contract refuses every idle
    # mutation while one exists. Status, context and checkpoint are refused
    # here for the same reason and one step earlier: before any Store is
    # constructed, before the writer lease is attempted, and before any owner
    # route is chosen. Storage-level replay is a lawful storage behaviour, but
    # it is not an authorization for an agent command -- least of all for a
    # nominally read-only `status` -- to rewrite authoritative documents and
    # delete the recovery marker on the way to answering.
    if _pending_journal_present(admission.data_dir / JOURNAL_NAME):
        return _failure(command=command, code="internal_error")

    authority: OwnerAuthority | None = None
    try:
        backend, authority = _select_backend(
            admission=admission,
            expected_workspace_uid=expected_workspace_uid,
            dependencies=dependencies,
        )
        outcome = _handle_command(
            args=args,
            admission=admission,
            backend=backend,
            dependencies=dependencies,
        )
        classified = getattr(backend, "error_code", None)
        if outcome.error_code == "internal_error" and classified in {
            "owner_unavailable",
            "workspace_mismatch",
        }:
            return _failure(command=command, code=classified)
        return outcome
    except _OwnerUnavailable:
        return _failure(command=command, code="owner_unavailable")
    except _RecoveryBlocked:
        return _failure(command=command, code="internal_error")
    except Exception:
        return _failure(command=command, code="internal_error")
    finally:
        _release_authority(authority)


def _emit_rendered_bytes(*, stdout: typing.TextIO, rendered: bytes) -> None:
    binary = getattr(stdout, "buffer", None)
    if binary is not None:
        binary.write(rendered)
        return
    stdout.write(rendered.decode("utf-8"))


def _maybe_markdown(
    *,
    args: argparse.Namespace,
    outcome: AgentOutcome,
    rendered: bytes,
) -> tuple[AgentOutcome, bytes]:
    """Replace a validated context JSON envelope with Markdown when requested.

    Failures, including formatting failure, stay the existing JSON error
    envelope. The JSON bytes are produced first so raw backend data cannot
    skip contract validation.
    """

    if getattr(args, "action", None) != CONTEXT_COMMAND:
        return outcome, rendered
    if getattr(args, "context_format", None) != MARKDOWN_FORMAT:
        return outcome, rendered
    if outcome.error_code is not None:
        return outcome, rendered
    try:
        return outcome, render_context_brief(data=outcome.data)
    except BriefTooLarge:
        failed = _failure(command="agent.context", code="context_too_large")
        return failed, render_outcome(outcome=failed)
    except Exception:
        failed = _failure(command="agent.context", code="internal_error")
        return failed, render_outcome(outcome=failed)


def run_agent_command(
    *,
    args: argparse.Namespace,
    stdout: typing.TextIO,
    stderr: typing.TextIO,
    dependencies: RuntimeDependencies,
) -> int:
    """Run one already-parsed new agent command and emit one canonical envelope."""

    del stderr
    outcome = _dispatch(args=args, dependencies=dependencies)
    try:
        rendered = render_outcome(outcome=outcome)
    except Exception:
        outcome = _failure(command=_command_name(getattr(args, "action", None)), code="internal_error")
        rendered = render_outcome(outcome=outcome)
    outcome, rendered = _maybe_markdown(args=args, outcome=outcome, rendered=rendered)
    _emit_rendered_bytes(stdout=stdout, rendered=rendered)
    return 0 if outcome.error_code is None else 1
