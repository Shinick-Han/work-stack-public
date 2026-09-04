"""Frozen static command registry for the P0 agent CLI."""

from workstack.agent_cli_contract import (
    CHECKPOINT_COMMAND as _CHECKPOINT_COMMAND,
    CONTEXT_COMMAND as _CONTEXT_COMMAND,
    STATUS_COMMAND as _STATUS_COMMAND,
)


__all__ = ["COMMANDS"]


COMMANDS: tuple[str, str, str] = (
    _STATUS_COMMAND,
    _CONTEXT_COMMAND,
    _CHECKPOINT_COMMAND,
)
