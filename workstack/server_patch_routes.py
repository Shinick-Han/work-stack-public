"""PATCH field-update routes and the DELETE commit route.

The four PATCH targets are tried in the order they shipped — subtask, key
result, objective, then task — because the objective pattern would otherwise
swallow the key-result path. Body admission stays per target: each one names
the fields it accepts and refuses anything else before the service is reached.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from .server_errors import RequestError


SUBTASK_PATH = re.compile(r"/api/v1/tasks/([^/]+)/subtasks/([^/]+)")
KEY_RESULT_PATH = re.compile(r"/api/v1/objectives/([^/]+)/key-results/([^/]+)")
OBJECTIVE_PATH = re.compile(r"/api/v1/objectives/([^/]+)")
TASK_PATH = re.compile(r"/api/v1/tasks/([^/]+)")
LEGACY_TASK_PATH = re.compile(r"/api/tasks/([^/]+)")

KEY_RESULT_FIELDS = {"text", "target", "progress", "status", "revision"}
OBJECTIVE_FIELDS = {"objective", "quarter", "status", "revision"}


def _require_subtask_patch_body(body: dict[str, Any]) -> None:
    if set(body) != {"status", "revision"}:
        raise RequestError(
            "invalid_body",
            "subtask update requires only status and revision",
            400,
        )


def _require_key_result_patch_body(body: dict[str, Any]) -> None:
    mutable = KEY_RESULT_FIELDS - {"revision"}
    if (
        "revision" not in body
        or not (set(body) & mutable)
        or not set(body) <= KEY_RESULT_FIELDS
        or any(
            field in body and not isinstance(body[field], str)
            for field in ("text", "target", "status")
        )
        or (
            "progress" in body
            and type(body["progress"]) is not int
        )
    ):
        raise RequestError(
            "invalid_body",
            "key result update requires revision and at least one supported field",
            400,
        )


def _require_objective_patch_body(body: dict[str, Any]) -> None:
    mutable = OBJECTIVE_FIELDS - {"revision"}
    if (
        "revision" not in body
        or not (set(body) & mutable)
        or not set(body) <= OBJECTIVE_FIELDS
        or any(
            field in body and not isinstance(body[field], str)
            for field in ("objective", "quarter", "status")
        )
    ):
        raise RequestError(
            "invalid_body",
            "objective update requires revision and at least one supported field",
            400,
        )


class PatchRouteMixin:
    """PATCH target selection, per-target body admission, and DELETE commit."""

    def _handle_v1_patch(self, path: str) -> None:
        body, request_digest = self.read_json()
        self._require_browser_mutation()
        subtask_match = SUBTASK_PATH.fullmatch(path)
        if subtask_match:
            self._patch_subtask(subtask_match, body)
            return
        key_result_match = KEY_RESULT_PATH.fullmatch(path)
        if key_result_match:
            self._patch_key_result(key_result_match, body)
            return
        objective_match = OBJECTIVE_PATH.fullmatch(path)
        if objective_match:
            self._patch_objective(objective_match, body)
            return
        match = TASK_PATH.fullmatch(path)
        if not match:
            self.send_api_error("not_found", "API endpoint not found", 404)
            return
        self._patch_task(unquote(match.group(1)), body, request_digest)

    def _handle_legacy_patch(self, path: str) -> None:
        self.read_json()
        self._require_browser_mutation()
        if LEGACY_TASK_PATH.fullmatch(path):
            self.send_api_error(
                "legacy_writer_disabled",
                "Use PATCH /api/v1/tasks/{id}",
                410,
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _patch_subtask(self, match: re.Match[str], body: dict[str, Any]) -> None:
        _require_subtask_patch_body(body)
        task_id = unquote(match.group(1))
        self.stack.set_subtask_status(
            task_id,
            unquote(match.group(2)),
            body["status"],
            body["revision"],
        )
        self.send_json({"data": self.stack.get_task(task_id)})

    def _patch_key_result(self, match: re.Match[str], body: dict[str, Any]) -> None:
        _require_key_result_patch_body(body)
        self.send_json({
            "data": self.stack.patch_key_result_v1(
                unquote(match.group(1)),
                unquote(match.group(2)),
                body,
            )
        })

    def _patch_objective(self, match: re.Match[str], body: dict[str, Any]) -> None:
        _require_objective_patch_body(body)
        self.send_json({
            "data": self.stack.patch_objective_v1(
                unquote(match.group(1)), body
            )
        })

    def _patch_task(
        self, task_id: str, body: dict[str, Any], request_digest: str
    ) -> None:
        """One PATCH route, two branches, selected by the key's PRESENCE.

        A duplicate Idempotency-Key header refuses here through the existing
        once-only rule, so a caller can never smuggle two keys past this point.
        An absent key leaves the ordinary general PATCH exactly as it was,
        including its experimental-v4 behaviour: no existing caller gains a
        header or a receipt by accident.
        """

        if self._header_once("Idempotency-Key") is None:
            self.send_json({"data": self.stack.patch_task(task_id, body)})
            return
        self._send_service_result(
            self.stack.set_task_status_v1(
                task_id,
                body,
                self._idempotency_key(),
                path="/api/v1/tasks/{}".format(task_id),
                request_digest=request_digest,
            )
        )

    def _handle_delete(self, path: str) -> None:
        body, request_digest = self.read_json()
        self._require_browser_mutation()
        if not path.startswith("/api/v1/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        match = TASK_PATH.fullmatch(path)
        if match is None:
            self.send_api_error("not_found", "API endpoint not found", 404)
            return
        self._delete_task(unquote(match.group(1)), body, request_digest, path)

    def _delete_task(
        self,
        task_id: str,
        body: dict[str, Any],
        request_digest: str,
        path: str,
    ) -> None:
        data = self.stack.commit_task_deletion(
            task_id=task_id,
            body=body,
            request_digest=request_digest,
            path=path,
            idempotency_key=self._idempotency_key(),
            if_match=self._header_once("If-Match"),
            force_backup_failure=self._header_once("X-WorkStack-Force-Backup-Failure") == "1",
        )
        self.send_json({"data": data})


__all__ = ("PatchRouteMixin",)
