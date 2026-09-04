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
from workstack.agent_local_backend import create_local_backend
from workstack.agent_transport import create_running_server_backend
from workstack.store import Store


__all__ = ["run_agent_command"]


_HTTP_TIMEOUT_SECONDS = 10
_COMMANDS = frozenset({STATUS_COMMAND, CONTEXT_COMMAND, CHECKPOINT_COMMAND})
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
):
    store = dependencies.store_factory(root=admission.data_dir)
    if _owner_metadata_present(store.server_info_path):
        return _RunningBackendFailureRecorder(
            dependencies.create_running_server_backend(
                server_info_path=store.server_info_path,
                expected_workspace_uid=expected_workspace_uid,
                request_json=dependencies.request_json,
            )
        )
    return dependencies.create_local_backend(
        admission=admission,
        store_factory=_AdmittedStoreFactory(admission=admission, store=store),
    )


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
            request=ContextRequest(task_id=getattr(args, "task", None)),
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

    try:
        backend = _select_backend(
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
    except Exception:
        return _failure(command=command, code="internal_error")


def _emit_rendered_bytes(*, stdout: typing.TextIO, rendered: bytes) -> None:
    binary = getattr(stdout, "buffer", None)
    if binary is not None:
        binary.write(rendered)
        return
    stdout.write(rendered.decode("utf-8"))


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
    _emit_rendered_bytes(stdout=stdout, rendered=rendered)
    return 0 if outcome.error_code is None else 1
