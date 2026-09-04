"""Pure checkpoint-command mapping over the frozen agent CLI contract."""

from __future__ import annotations

from workstack.agent_cli_contract import (
    CHECKPOINT_COMMAND,
    AgentBackend,
    AgentOutcome,
    CheckpointRequest,
    render_outcome,
)


__all__ = ("handle_checkpoint",)


_COMMAND = "agent.{}".format(CHECKPOINT_COMMAND)


def _outcome(
    *,
    commit_state: str | None,
    data: dict[str, object] | None,
    error_code: str | None,
    intent_id: str | None,
    replayed: bool | None,
    retryable: bool | None,
    task_id: str | None,
    transport: str | None,
    workspace_uid: str | None,
) -> AgentOutcome:
    return AgentOutcome(
        command=_COMMAND,
        commit_state=commit_state,
        data=data,
        error_code=error_code,
        error_details={},
        error_message="" if error_code is not None else None,
        intent_id=intent_id,
        replayed=replayed,
        retryable=retryable,
        task_id=task_id,
        transport=transport,
        workspace_uid=workspace_uid,
    )


def _internal_error() -> AgentOutcome:
    return _outcome(
        commit_state=None,
        data=None,
        error_code="internal_error",
        intent_id=None,
        replayed=None,
        retryable=None,
        task_id=None,
        transport=None,
        workspace_uid=None,
    )


def _map_result(
    *, request: CheckpointRequest, result: dict[str, object]
) -> AgentOutcome:
    commit_state = result.get("commit_state")
    transport = result.get("transport")
    workspace_uid = result.get("workspace_uid")

    if commit_state == "committed":
        outcome = _outcome(
            commit_state="committed",
            data=result.get("entry"),
            error_code=None,
            intent_id=request.intent_id,
            replayed=result.get("replayed"),
            retryable=None,
            task_id=request.task_id,
            transport=transport,
            workspace_uid=workspace_uid,
        )
    elif commit_state == "unknown":
        if result.get("entry") is not None:
            raise ValueError("an uncertain checkpoint cannot contain success data")
        outcome = _outcome(
            commit_state="unknown",
            data=None,
            error_code="commit_unknown",
            intent_id=request.intent_id,
            replayed=None,
            retryable=None,
            task_id=request.task_id,
            transport=transport,
            workspace_uid=workspace_uid,
        )
    else:
        error_code = result.get("error_code")
        if type(error_code) is not str:
            raise ValueError("backend returned neither success nor a classified failure")
        outcome = _outcome(
            commit_state=None,
            data=None,
            error_code=error_code,
            intent_id=None,
            replayed=None,
            retryable=result.get("retryable"),
            task_id=None,
            transport=None,
            workspace_uid=None,
        )

    # The frozen renderer is the single authority for metadata, response data,
    # safe error classifications and the final UTF-8 envelope bound. Validate
    # here without writing the returned bytes; the runtime owns all I/O.
    render_outcome(outcome=outcome)
    return outcome


def handle_checkpoint(
    *,
    request: CheckpointRequest,
    backend: AgentBackend,
) -> AgentOutcome:
    """Execute one already-validated checkpoint request through one backend."""

    try:
        result = backend.checkpoint(request=request)
        if type(result) is not dict:
            raise ValueError("checkpoint backend result must be a mapping")
        return _map_result(request=request, result=result)
    except Exception:
        return _internal_error()
