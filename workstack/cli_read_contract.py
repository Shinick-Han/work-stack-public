"""Pure request-shape contract shared by the owner CLI read client and server.

Identity and query grammar are the same on both sides of the wire, so the
client transport validates against this module instead of importing the server
route table. Nothing here touches HTTP, the Store or route registration: it is
value validation and one refusal type, and ``workstack.cli_read_http``
re-exports every name so existing imports keep their identities.
"""

from __future__ import annotations

import re
import uuid
from typing import Any


QUERY_LIMIT = 1024
ENTITY_ID_LIMIT = 128
_CONTROLS = re.compile(r"[\x00-\x1f]")


class CliReadHttpError(Exception):
    """Stable refusal for CLI owner reads. Domain errors keep their types."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def invalid_query() -> CliReadHttpError:
    return CliReadHttpError("invalid_query", "CLI read query is invalid", 400)


def canonical_workspace_uid(value: object) -> str:
    """Accept exactly one canonical RFC 4122 spelling of a workspace identity."""

    if type(value) is not str:
        raise invalid_query()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise invalid_query() from error
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise invalid_query()
    return value


def require_entity_id(value: str) -> str:
    """Refuse path navigation and control bytes; domain lookup stays unchanged."""

    if type(value) is not str or not value or len(value) > ENTITY_ID_LIMIT:
        raise CliReadHttpError("invalid_query", "CLI read identity is invalid", 400)
    if (
        _CONTROLS.search(value) is not None
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    ):
        raise CliReadHttpError("invalid_query", "CLI read identity is invalid", 400)
    return value


__all__ = (
    "CliReadHttpError",
    "ENTITY_ID_LIMIT",
    "QUERY_LIMIT",
    "canonical_workspace_uid",
    "invalid_query",
    "require_entity_id",
)
