"""Backend-neutral command slices the service facade may delegate to.

These Protocols are the ONLY description of an optional command backend. They
carry no implementation and import nothing from the domain, so a backend can be
supplied without the facade learning anything about its storage.
"""

from __future__ import annotations

from typing import Any, Protocol


class CaptureReplyCommands(Protocol):
    """Backend-neutral completed capture/reply command slice."""

    def ingest_capture(
        self,
        packet: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        method: str = "POST",
        path: str = "/api/v1/captures",
    ) -> dict[str, Any]: ...

    def link_capture(
        self,
        capture_id: str,
        task_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...

    def approve_reply(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str = "/api/v1/replies",
    ) -> dict[str, Any]: ...

    def apply_reply_receipt(
        self,
        reply_id: str,
        receipt: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...


class IntentCommands(Protocol):
    def create_note(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def checkin(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def add_worklog(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...


class ObjectiveCommands(Protocol):
    def create_objective(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def add_key_result(
        self,
        objective_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]: ...


class TaskCommands(Protocol):
    def create_task_v1(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def patch_task(
        self, task_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]: ...


class RelationshipCommands(Protocol):
    def patch_relationships(
        self, task_id: str, request: dict[str, Any]
    ) -> Any: ...

    def delete_task(self, task_id: str, expected_revision: int) -> Any: ...


class PlanningCommands(Protocol):
    def set_task_status(
        self,
        task_id: str,
        status: str,
        expected_revision: int | None = None,
        *,
        provenance: str = "cli",
    ) -> dict[str, Any]: ...

    def patch_status(
        self, task_id: str, status: str, expected_revision: int
    ) -> dict[str, Any]: ...


class WorkSessionCommands(Protocol):
    def projection(self) -> dict[str, Any]: ...

    def start(
        self, body: dict[str, Any], idempotency_key: str, *, path: str
    ) -> dict[str, Any]: ...

    def transition(
        self,
        session_id: str,
        action: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...

    def record_worklog(
        self,
        session_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]: ...


class QueryCommands(Protocol):
    def search(self, query: str, *, limit: int = 50) -> Any: ...

    def graph(self) -> Any: ...
