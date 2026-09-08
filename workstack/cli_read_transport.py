"""Owner-route transport for the six parity CLI reads.

One bounded owner advertisement, the existing session/storage/sync preflight,
then one GET. A failure never falls back to a local Store or JSON read. The
success payload is the exact domain object the exclusive-local CLI would emit.
"""

from __future__ import annotations

from typing import Callable, Mapping
from urllib.parse import quote, urlencode

from workstack import cli_reads
from workstack.cli_read_contract import CliReadHttpError, require_entity_id
from workstack.cli_writer import (
    AMBIGUOUS_TRANSPORT,
    OWNER_INVALID,
    OWNER_PRESENT,
    WriterTransportError,
    expected_workspace_uid,
    read_owner_binding,
    _origin,
    _preflight,
)
from workstack.service import NotFoundError


RequestJson = Callable[..., tuple[int, dict[str, object]]]
CoordinatesReader = Callable[..., "tuple[str, int] | None"]

_UNAVAILABLE = "the running Work Stack server could not be read"
_REFUSED = "the running Work Stack server refused the read (HTTP {})"
_CHANGED = "Work Stack server runtime metadata changed before the read was sent"
_MISMATCH = "the running Work Stack server owns a different workspace identity"
_NOT_IN_SYNC = (
    "the running Work Stack store is not in-sync; resolve synchronization first"
)


def _project_read(payload: Mapping[str, object]) -> object:
    if type(payload) is not dict or set(payload) != {"data"}:
        raise WriterTransportError("Work Stack server returned an invalid read response")
    return payload["data"]


def _query(workspace_uid: str, **fields: object) -> str:
    pairs: list[tuple[str, str]] = [("workspace_uid", workspace_uid)]
    for name, value in fields.items():
        if value is None:
            continue
        if type(value) is bool or not isinstance(value, (str, int)):
            raise WriterTransportError("CLI read arguments are invalid")
        pairs.append((name, value if type(value) is str else str(value)))
    return urlencode(pairs)


def _entity_id(value: object) -> str:
    try:
        return require_entity_id(str(value))
    except CliReadHttpError as error:
        raise ValueError(error.message) from error


def read_path(command_key: str, arguments: object, workspace_uid: str) -> str:
    """Build the frozen GET target for one admitted parity command."""

    if command_key == "backlog.list":
        return "/api/v1/cli/backlog?" + _query(
            workspace_uid, status=getattr(arguments, "status")
        )
    if command_key == "backlog.show":
        return "/api/v1/cli/backlog/{}?{}".format(
            quote(_entity_id(getattr(arguments, "id")), safe=""),
            _query(workspace_uid),
        )
    if command_key == "okr.list":
        return "/api/v1/cli/okr?" + _query(
            workspace_uid, status=getattr(arguments, "status")
        )
    if command_key == "okr.rollup":
        return "/api/v1/cli/okr/rollup?" + _query(workspace_uid)
    if command_key == "worklog.list":
        return "/api/v1/cli/worklog?" + _query(
            workspace_uid, date=getattr(arguments, "date")
        )
    if command_key == "weekly":
        return "/api/v1/cli/weekly?" + _query(
            workspace_uid,
            end=getattr(arguments, "end"),
            days=getattr(arguments, "days"),
        )
    raise cli_reads.owner_read_refusal()


def _raise_http_error(status: int, payload: Mapping[str, object]) -> None:
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, dict):
        raise WriterTransportError(_REFUSED.format(status))
    code = error.get("code")
    message = error.get("message")
    details = error.get("details")
    if status == 404 and code == "not_found" and type(message) is str:
        raise NotFoundError(message, details if isinstance(details, dict) else None)
    if status == 400 and type(message) is str and message:
        raise ValueError(message)
    if status == 409 and code == "workspace_mismatch":
        raise WriterTransportError(_MISMATCH)
    if status == 409 and code == "store_sync_required":
        raise WriterTransportError(_NOT_IN_SYNC)
    raise WriterTransportError(_REFUSED.format(status))


def forward_read(
    store: object,
    owner_state: str,
    command_key: str,
    arguments: object,
    *,
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
) -> object:
    """Return the exclusive-local stdout object from the owning server."""

    del coordinates_reader
    if command_key not in cli_reads.OWNER_HTTP_READ_PARITY:
        raise cli_reads.owner_read_refusal()
    if owner_state == OWNER_INVALID:
        raise WriterTransportError(
            "Work Stack server runtime metadata is not a readable regular file"
        )
    if owner_state != OWNER_PRESENT:
        raise WriterTransportError("Work Stack server runtime metadata is not available")

    host, port, binding = read_owner_binding(store)
    expected_uid = expected_workspace_uid(store)
    csrf = _preflight(request_json, host, port, expected_uid)
    revalidated_host, revalidated_port, revalidated_binding = read_owner_binding(store)
    if (revalidated_host, revalidated_port, revalidated_binding) != (host, port, binding):
        raise WriterTransportError(_CHANGED)

    path = read_path(command_key, arguments, expected_uid)
    headers = {"Origin": _origin(host, port), "X-WorkStack-CSRF": csrf}
    try:
        status, payload = request_json(host, port, "GET", path, headers=headers)
    except AMBIGUOUS_TRANSPORT as error:
        raise WriterTransportError(_UNAVAILABLE) from error
    if status == 200:
        return _project_read(payload)
    _raise_http_error(status, payload)
    raise WriterTransportError(_UNAVAILABLE)
