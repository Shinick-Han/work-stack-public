"""Bounded running-server backend for the agent CLI."""

from __future__ import annotations

import json
import pathlib
import uuid

from workstack.agent_cli_contract import (
    AgentBackend,
    CheckpointRequest,
    ContextRequest,
    JsonRequester,
    StatusRequest,
)


__all__ = ["create_running_server_backend"]


_SERVER_INFO_MAX_BYTES = 4096
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SYNC_STATES = frozenset({"external-change-detected", "in-sync", "invalid"})
_SYNC_REQUIRED_REASON = "store_sync_required"

# Frozen sender provenance for agent-written checkpoints. Named here so the
# request site and its contract test agree on one spelling rather than two
# string literals that could drift apart. A hint only: it identifies the sender
# for attribution and carries no authority.
AGENT_CLIENT_HEADER = "X-WorkStack-Client"
AGENT_CLIENT_VALUE = "agent-cli-v1"


def _canonical_workspace_uid(value: object) -> str:
    if type(value) is not str:
        raise OSError("workspace identity is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise OSError("workspace identity is invalid") from error
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise OSError("workspace identity is invalid")
    return value


def _previous_day(year: int, month: int, day: int) -> tuple[int, int, int]:
    if day > 1:
        return year, month, day - 1
    if month == 1:
        return year - 1, 12, 31
    previous_month = month - 1
    lengths = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    length = lengths[previous_month - 1]
    if previous_month == 2 and (
        year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)
    ):
        length = 29
    return year, previous_month, length


def _review_dates(today: object) -> list[str]:
    year = getattr(today, "year", None)
    month = getattr(today, "month", None)
    day = getattr(today, "day", None)
    if any(type(value) is not int for value in (year, month, day)):
        raise OSError("review date is invalid")
    result: list[str] = []
    for _ in range(31):
        result.append("{:04d}-{:02d}-{:02d}".format(year, month, day))
        year, month, day = _previous_day(year, month, day)
    return result


def _storage_format(value: object) -> str:
    if value == 3:
        return "v3"
    if value == 4:
        return "v4"
    return "unknown"


def _origin(host: str, port: int) -> str:
    rendered_host = "[{}]".format(host) if ":" in host else host
    return "http://{}:{}".format(rendered_host, port)


class _RunningServerBackend:
    def __init__(
        self,
        *,
        server_info_path: pathlib.Path,
        expected_workspace_uid: str,
        request_json: JsonRequester,
    ) -> None:
        self._server_info_path = server_info_path
        self._expected_workspace_uid = _canonical_workspace_uid(expected_workspace_uid)
        self._request_json = request_json

    def _coordinates(self) -> tuple[str, int]:
        try:
            with self._server_info_path.open("rb") as source:
                raw = source.read(_SERVER_INFO_MAX_BYTES + 1)
        except OSError as error:
            raise OSError("server ownership metadata is unavailable") from error
        if len(raw) > _SERVER_INFO_MAX_BYTES:
            raise OSError("server ownership metadata is invalid")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OSError("server ownership metadata is invalid") from error
        if (
            type(value) is not dict
            or set(value) != {"host", "port", "version"}
            or value.get("version") != 1
            or value.get("host") not in _LOOPBACK_HOSTS
            or type(value.get("port")) is not int
            or not 1 <= value["port"] <= 65535
        ):
            raise OSError("server ownership metadata is invalid")
        return value["host"], value["port"]

    def _request(
        self,
        *,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        status, payload = self._request_json.request(
            host=host,
            port=port,
            method=method,
            path=path,
            body=body,
            headers=headers,
        )
        if type(status) is not int or type(payload) is not dict:
            raise OSError("server response is invalid")
        return status, payload

    @staticmethod
    def _data(status: int, payload: dict[str, object], label: str) -> dict[str, object]:
        data = payload.get("data")
        if status != 200 or type(data) is not dict:
            raise OSError("{} response is invalid".format(label))
        return data

    def _preflight(self) -> tuple[str, int, str, dict[str, object]]:
        host, port = self._coordinates()
        status, payload = self._request(
            host=host, port=port, method="GET", path="/api/v1/session"
        )
        session = self._data(status, payload, "session")
        csrf = session.get("csrf_token")
        if type(csrf) is not str or not csrf:
            raise OSError("session response is invalid")
        status, payload = self._request(
            host=host, port=port, method="GET", path="/api/v1/storage"
        )
        storage = self._data(status, payload, "storage")
        actual_uid = _canonical_workspace_uid(storage.get("workspace_id"))
        if actual_uid != self._expected_workspace_uid:
            raise ValueError("workspace identity does not match")
        return host, port, csrf, storage

    def _sync_state(self, *, host: str, port: int) -> str:
        status, payload = self._request(
            host=host,
            port=port,
            method="GET",
            path="/api/v1/sync/status",
        )
        sync = self._data(status, payload, "sync status")
        state = sync.get("state")
        if type(state) is not str or state not in _SYNC_STATES:
            raise OSError("sync status response is invalid")
        return state

    def status(self, *, request: StatusRequest) -> dict[str, object]:
        if request.expected_workspace_uid != self._expected_workspace_uid:
            raise ValueError("workspace identity does not match")
        host, port, _csrf, storage = self._preflight()
        actual_uid = _canonical_workspace_uid(storage["workspace_id"])
        storage_format = _storage_format(storage.get("store_schema_version"))
        supported = storage_format == "v3"
        in_sync = self._sync_state(host=host, port=port) == "in-sync"
        if not supported:
            capability_reason = "unsupported storage format"
        elif not in_sync:
            capability_reason = _SYNC_REQUIRED_REASON
        else:
            capability_reason = None
        return {
            "actual_workspace_uid": actual_uid,
            "capability_reason": capability_reason,
            "capability_supported": supported,
            "contract": "workstack.cli.v1",
            "data_dir_available": self._server_info_path.parent.is_dir(),
            "exclusive_local_available": False,
            "expected_workspace_uid": self._expected_workspace_uid,
            "ready": supported and in_sync,
            "running_server_available": True,
            "storage_format": storage_format,
        }

    def context(self, *, request: ContextRequest, today: object) -> dict[str, object]:
        host, port, _csrf, storage = self._preflight()
        status, payload = self._request(
            host=host,
            port=port,
            method="GET",
            path="/api/v1/tasks/{}".format(request.task_id),
        )
        detail = self._data(status, payload, "Task")
        task = detail.get("task")
        if type(task) is not dict:
            raise OSError("Task response is invalid")
        entries: list[dict[str, object]] = []
        for date in _review_dates(today):
            status, payload = self._request(
                host=host,
                port=port,
                method="GET",
                path="/api/v1/review?date={}&days=1".format(date),
            )
            projection = self._data(status, payload, "review")
            day = projection.get("day")
            if type(day) is not dict or type(day.get("entries")) is not list:
                raise OSError("review response is invalid")
            for entry in day["entries"]:
                if type(entry) is not dict:
                    raise OSError("review response is invalid")
                raw = dict(entry)
                raw["date"] = date
                entries.append(raw)
        return {
            "entries": entries,
            "task": task,
            "transport": "running-server",
            "workspace_uid": storage["workspace_id"],
        }

    def checkpoint(self, *, request: CheckpointRequest) -> dict[str, object]:
        host, port, csrf, storage = self._preflight()
        body = json.dumps(
            {
                "blockers": request.blockers,
                "date": request.date,
                "done": request.done,
                "next": request.next,
                "task_id": request.task_id,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": request.intent_id,
            "Origin": _origin(host, port),
            # Sender provenance hint, so a checkpoint written by the agent CLI is
            # distinguishable from one written by the GUI. It is attribution, not
            # authentication: nothing signs it and the server must not treat it
            # as a credential. The one ambiguous replay below reuses this same
            # dict, so the replayed request carries the identical header.
            AGENT_CLIENT_HEADER: AGENT_CLIENT_VALUE,
            "X-WorkStack-CSRF": csrf,
        }
        try:
            status, payload = self._request(
                host=host,
                port=port,
                method="POST",
                path="/api/v1/review/entries",
                body=body,
                headers=headers,
            )
        except (OSError, TimeoutError):
            try:
                status, payload = self._request(
                    host=host,
                    port=port,
                    method="POST",
                    path="/api/v1/review/entries",
                    body=body,
                    headers=headers,
                )
            except (OSError, TimeoutError):
                return {
                    "commit_state": "unknown",
                    "entry": None,
                    "replayed": False,
                    "transport": "running-server",
                    "workspace_uid": storage["workspace_id"],
                }
        if not 200 <= status < 300:
            raise OSError("checkpoint response is invalid")
        data = payload.get("data")
        meta = payload.get("meta")
        if type(data) is not dict or type(meta) is not dict or type(meta.get("replayed")) is not bool:
            raise OSError("checkpoint response is invalid")
        return {
            "commit_state": "committed",
            "entry": data,
            "replayed": meta["replayed"],
            "transport": "running-server",
            "workspace_uid": storage["workspace_id"],
        }


def create_running_server_backend(
    *,
    server_info_path: pathlib.Path,
    expected_workspace_uid: str,
    request_json: JsonRequester,
) -> AgentBackend:
    return _RunningServerBackend(
        server_info_path=server_info_path,
        expected_workspace_uid=expected_workspace_uid,
        request_json=request_json,
    )
