"""Ordinary CLI reads: exclusive-local WorkStack API, or owner HTTP parity.

GET /api/v1/workspace, /api/v1/tasks/{id}, /api/v1/review and
/api/v1/objectives/{id} are GUI projections. They add fields such as
context_count and drop others such as status_fact_id, so they are not a
parity source. Owner-held workspaces use the frozen CLI GET routes instead
of a second local lease or a filtered GUI GET.
"""

from __future__ import annotations

from workstack.cli_capabilities import CliCapability


PARITY_READ_KEYS = frozenset(
    {
        "backlog.list",
        "backlog.show",
        "okr.list",
        "okr.rollup",
        "worklog.list",
        "weekly",
    }
)

OWNER_HTTP_READ_PARITY = PARITY_READ_KEYS
OWNER_READ_REFUSAL = (
    "this command cannot run while a Work Stack owner holds the workspace"
)


def is_parity_read(command_key: str) -> bool:
    return command_key in PARITY_READ_KEYS


def owner_read_is_supported(capability: CliCapability) -> bool:
    return capability.command_key in OWNER_HTTP_READ_PARITY


def owner_read_refusal() -> OSError:
    return OSError(OWNER_READ_REFUSAL)
