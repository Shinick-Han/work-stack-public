from __future__ import annotations

from workstack.agent_cli_contract import (
    STATUS_COMMAND,
    AgentBackend,
    AgentOutcome,
    StatusRequest,
)


__all__ = ("handle_status",)


_STATUS_FIELDS = frozenset(
    {
        "actual_workspace_uid",
        "capability_reason",
        "capability_supported",
        "contract",
        "data_dir_available",
        "exclusive_local_available",
        "expected_workspace_uid",
        "ready",
        "running_server_available",
        "storage_format",
    }
)


def _internal_error() -> AgentOutcome:
    return AgentOutcome(
        command=f"agent.{STATUS_COMMAND}",
        commit_state=None,
        data=None,
        error_code="internal_error",
        error_details={},
        error_message="internal_error",
        intent_id=None,
        replayed=None,
        retryable=None,
        task_id=None,
        transport=None,
        workspace_uid=None,
    )


def handle_status(
    *,
    request: StatusRequest,
    backend: AgentBackend,
) -> AgentOutcome:
    try:
        result = backend.status(request=request)
        if type(result) is not dict or set(result) != _STATUS_FIELDS:
            return _internal_error()

        running_available = result["running_server_available"]
        exclusive_local_available = result["exclusive_local_available"]
        if type(running_available) is not bool or type(exclusive_local_available) is not bool:
            return _internal_error()
        if running_available:
            transport = "running-server"
        elif exclusive_local_available:
            transport = "exclusive-local"
        else:
            return _internal_error()

        workspace_uid = result["actual_workspace_uid"]
        if type(workspace_uid) is not str:
            return _internal_error()
        return AgentOutcome(
            command=f"agent.{STATUS_COMMAND}",
            commit_state=None,
            data=result,
            error_code=None,
            error_details={},
            error_message=None,
            intent_id=None,
            replayed=None,
            retryable=None,
            task_id=None,
            transport=transport,
            workspace_uid=workspace_uid,
        )
    except Exception:
        return _internal_error()
