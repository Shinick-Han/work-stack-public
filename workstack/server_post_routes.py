"""POST route admission and the v1 write handlers it dispatches to.

The admission order is load bearing and unchanged: the bounded body is drained
first so a Windows socket is never closed with unread bytes, then agent bearer
or browser Origin/CSRF authorization is proven, then an idempotency key is
demanded for the routes that require one, and only then is a route resolved.
An unknown or non-canonical route is refused in exactly that position.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from .http_route_types import IDEMPOTENT_POST_ROUTES, PostRoute, V1_POST_ROUTES
from .report_documents_http import is_report_route_alias
from .server_admission import CAPTURE_BODY_LIMIT, DEFAULT_BODY_LIMIT
from .server_errors import RequestError


_CHECKPOINT_TRANSITION_PATH = re.compile(
    r"/api/v1/review/checkpoints/([^/]+)/transitions"
)

_CAPTURE_PATH_PREFIX = "/api/v1/captures"

_LEGACY_VERSIONED_PATHS = {"/api/objectives", "/api/worklog", "/api/notes"}

_WORKLOG_FIELDS = ("done", "next", "blockers")


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _require_exact_body(body: dict[str, Any], expected: set[str], message: str) -> None:
    if set(body) != expected:
        raise RequestError("invalid_body", message, 400)


class PostRouteMixin:
    """The v1 POST surface: admission, route selection and write handlers."""

    @staticmethod
    def _match_v1_post_route(path: str) -> tuple[PostRoute | None, re.Match[str] | None]:
        for route in V1_POST_ROUTES:
            match = route.match(path)
            if match is not None:
                return route, match
        return None, None

    @staticmethod
    def _post_needs_idempotency(path: str, route: PostRoute | None) -> bool:
        return path.startswith(_CAPTURE_PATH_PREFIX) or (
            route is not None and route.name in IDEMPOTENT_POST_ROUTES
        )

    def _read_post_body(self, path: str) -> tuple[dict[str, Any], str] | None:
        """The bounded body, or None once the surrogate refusal was answered."""

        maximum = (
            CAPTURE_BODY_LIMIT
            if path.startswith(_CAPTURE_PATH_PREFIX)
            else DEFAULT_BODY_LIMIT
        )
        try:
            return self.read_json(maximum)
        except UnicodeEncodeError:
            # A JSON-escaped lone surrogate decodes fine but cannot be encoded
            # canonically, so the digest raises here, BEFORE any handler runs.
            # Only the new D5 route is given this narrow refusal: every other
            # route keeps its existing parsing policy exactly, and the constant
            # message echoes none of the input.
            if _CHECKPOINT_TRANSITION_PATH.fullmatch(path) is None:
                raise
            # The approved malformed taxonomy for this route is
            # invalid_request; the message stays constant and echoes no input.
            self.send_api_error(
                "invalid_request", "the request body is invalid", 400
            )
            return None

    def _handle_v1_post(self, path: str) -> None:
        route, match = self._match_v1_post_route(path)
        # Consume the bounded JSON body before returning an authorization
        # error. Closing a Windows socket with unread request bytes can reset
        # the connection before the client receives the JSON error.
        parsed_body = self._read_post_body(path)
        if parsed_body is None:
            return
        body, request_digest = parsed_body
        is_agent_ingest = (
            route is not None
            and route.name == "capture_ingest"
            and self._has_agent_bearer()
        )
        if not is_agent_ingest:
            self._require_browser_mutation()
        idempotency_key = (
            self._idempotency_key()
            if self._post_needs_idempotency(path, route)
            else ""
        )
        # A report route reached by a non-canonical spelling is refused with
        # the unknown-route answer, in the position the unknown route already
        # held: after the body is drained and the mutation is authorized, and
        # before any handler, service, transaction or receipt is reached.
        if (
            route is None
            or match is None
            or is_report_route_alias(route.handler, self.path)
        ):
            self.send_api_error("not_found", "API endpoint not found", 404)
            return
        handler = getattr(self, route.handler)
        handler(path, match, body, request_digest, idempotency_key)

    def _handle_legacy_post(self, path: str) -> None:
        self.read_json()
        self._require_browser_mutation()
        if path == "/api/tasks":
            self.send_api_error(
                "legacy_task_writer_disabled",
                "Use POST /api/v1/tasks with Idempotency-Key",
                410,
            )
            return
        if path in _LEGACY_VERSIONED_PATHS:
            self.send_api_error(
                "legacy_writer_disabled",
                "Use the corresponding versioned /api/v1 endpoint",
                410,
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _post_sync_adopt(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        _require_exact_body(
            body,
            {"expected_generation", "expected_manifest_digest"},
            "sync adoption requires expected_generation and expected_manifest_digest",
        )
        result = self.stack.store.adopt_external_change(
            body["expected_generation"],
            body["expected_manifest_digest"],
            idempotency_key,
        )
        self.send_json({"data": result})

    def _post_sync_rebind(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        _require_exact_body(
            body,
            {
                "confirmed",
                "expected_manifest_workspace_id",
                "expected_candidate_workspace_id",
                "expected_manifest_digest",
                "expected_candidate_digest",
            },
            "sync rebind body is invalid",
        )
        result = self.stack.store.rebind_workspace_identity(
            confirmed=body["confirmed"],
            expected_manifest_workspace_id=body["expected_manifest_workspace_id"],
            expected_candidate_workspace_id=body["expected_candidate_workspace_id"],
            expected_manifest_digest=body["expected_manifest_digest"],
            expected_candidate_digest=body["expected_candidate_digest"],
            idempotency_key=idempotency_key,
        )
        self.send_json({
            "data": {
                "state": result["state"],
                "workspace_id": result["workspace_id"],
                "generation": result["generation"],
                "recovery_receipt_digest": result["recovery"]["receipt_digest"],
                "planning_mutated": result["recovery"]["planning_mutated"],
            }
        })

    def _post_task_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.create_task_v1(body, idempotency_key, path=path)
        )

    def _post_work_session_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"task_id"} or not isinstance(body["task_id"], str):
            raise RequestError(
                "invalid_body", "work session creation requires only task_id", 400
            )
        self._send_service_result(
            self.stack.start_work_session_v1(body, idempotency_key, path=path)
        )

    def _post_work_session_action(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        session_id = unquote(match.group(1))
        action = match.group(2)
        if action == "worklog":
            self._post_work_session_log(
                path, session_id, body, idempotency_key
            )
            return
        if body:
            raise RequestError(
                "invalid_body", "work session transitions require an empty body", 400
            )
        self._send_service_result(
            self.stack.transition_work_session_v1(
                session_id, action, body, idempotency_key, path=path
            )
        )

    def _post_work_session_log(
        self, path: str, session_id: str, body: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        if set(body) != set(_WORKLOG_FIELDS) or any(
            not _string_list(body[field]) for field in _WORKLOG_FIELDS
        ):
            raise RequestError(
                "invalid_body",
                "work session worklog requires string arrays for done, next, and blockers",
                400,
            )
        self._send_service_result(
            self.stack.record_work_session_v1(
                session_id, body, idempotency_key, path=path
            )
        )

    def _post_backup(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"confirmed"} or body["confirmed"] is not True:
            raise RequestError(
                "invalid_body", "backup download requires explicit confirmation", 400
            )
        download = self.stack.create_backup_download()
        self.send_backup(
            download.body, download.filename, download.digest, download.workspace_id
        )

    def _post_snapshot_export(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        _require_exact_body(
            body,
            {"disclosure_confirmed", "expected_revision", "expected_digest"},
            "snapshot export confirmation has unknown or missing fields",
        )
        artifact = self.stack.confirmed_snapshot_export(
            unquote(match.group(1)),
            body["expected_revision"],
            body["expected_digest"],
            body["disclosure_confirmed"],
        )
        self.send_snapshot(
            artifact.canonical_bytes, artifact.filename, artifact.digest
        )

    def _post_deletion_preview(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        del path, request_digest, idempotency_key
        data = self.stack.preview_task_deletion(unquote(match.group(1)), body)
        self.send_json({"data": data})

    def _post_task_note(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        _require_exact_body(
            body, {"text", "revision"}, "task note requires only text and revision"
        )
        self._send_service_result(
            self.stack.add_task_note_v1(
                unquote(match.group(1)), body, idempotency_key, path=path
            )
        )

    def _post_task_subtask(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        _require_exact_body(
            body,
            {"title", "priority", "revision"},
            "subtask creation requires only title, priority, and revision",
        )
        self._send_service_result(
            self.stack.add_subtask_v1(
                unquote(match.group(1)), body, idempotency_key, path=path
            )
        )

    def _post_objective_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"objective", "quarter"}
            or not isinstance(body["objective"], str)
            or not isinstance(body["quarter"], str)
        ):
            raise RequestError(
                "invalid_body",
                "objective creation requires only string objective and quarter fields",
                400,
            )
        self._send_service_result(
            self.stack.create_objective_v1(body, idempotency_key, path=path)
        )

    def _post_key_result_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"text", "target", "revision"}
            or not isinstance(body["text"], str)
            or not isinstance(body["target"], str)
            or type(body["revision"]) is not int
        ):
            raise RequestError(
                "invalid_body",
                "key result creation requires only text, target, and revision",
                400,
            )
        self._send_service_result(
            self.stack.add_key_result_v1(
                unquote(match.group(1)), body, idempotency_key, path=path
            )
        )

    def _post_note_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"text", "links"}
            or not isinstance(body["text"], str)
            or not _string_list(body["links"])
        ):
            raise RequestError(
                "invalid_body",
                "note creation requires only string text and a string links array",
                400,
            )
        self._send_service_result(
            self.stack.create_note_v1(body, idempotency_key, path=path)
        )

    def _post_cli_backlog_add(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._refuse_unsupported_idempotency_key(
            "CLI backlog add does not accept Idempotency-Key"
        )
        self.send_json({"data": self.stack.add_task_cli(body)})

    def _post_cli_okr_link(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._refuse_unsupported_idempotency_key(
            "CLI OKR link does not accept Idempotency-Key"
        )
        self.send_json({"data": self.stack.link_task_cli(body)})

    def _post_cli_okr_progress(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._refuse_unsupported_idempotency_key(
            "CLI OKR progress does not accept Idempotency-Key"
        )
        self.send_json({"data": self.stack.set_key_result_progress_cli(body)})

    def _post_cli_checkin(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._refuse_unsupported_idempotency_key(
            "CLI checkin does not accept Idempotency-Key"
        )
        self.send_json({"data": self.stack.checkin_cli(body)})

    def _post_cli_worklog_entry(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._refuse_unsupported_idempotency_key(
            "CLI worklog add does not accept Idempotency-Key"
        )
        self.send_json({"data": self.stack.add_worklog_cli(body)})

    def _post_review_checkin(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"date", "time"}
            or not isinstance(body["date"], str)
            or not isinstance(body["time"], str)
        ):
            raise RequestError(
                "invalid_body",
                "review check-in requires only string date and time fields",
                400,
            )
        self._send_service_result(
            self.stack.checkin_v1(body, idempotency_key, path=path)
        )

    def _post_review_entry(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if (
            set(body) != {"date", "task_id", *_WORKLOG_FIELDS}
            or not isinstance(body["date"], str)
            or not isinstance(body["task_id"], str)
            or any(not _string_list(body[field]) for field in _WORKLOG_FIELDS)
        ):
            raise RequestError(
                "invalid_body",
                "review entry requires date, task_id, and string arrays for done, next, and blockers",
                400,
            )
        # Parsed only on this route, and refused BEFORE any mutation. Empty,
        # unknown, padded and repeated values all refuse, including repeated
        # identical values. Attribution is provenance, not authentication.
        origin = self._agent_client_origin()
        self._send_service_result(
            self.stack.add_worklog_v1(body, idempotency_key, path=path, origin=origin)
        )

    def _post_checkpoint_transition(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        # The server derives the physical locator; the caller supplies only
        # state, revision and reason. The digest is the RAW parsed-body digest
        # read_json already computed, so a caller cannot supply its own.
        self._send_service_result(
            self.stack.apply_checkpoint_transition_v1(
                match.group(1),
                body,
                idempotency_key,
                path=path,
                request_digest=request_digest,
                origin=self._agent_client_origin(),
            )
        )

    def _post_capture_ingest(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.ingest_capture(
                body, idempotency_key, request_digest, path=path
            )
        )

    def _post_capture_link(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) != {"task_id"} or not isinstance(body["task_id"], str):
            raise RequestError("invalid_body", "link requires only task_id", 400)
        self._send_service_result(
            self.stack.link_capture(
                unquote(match.group(1)), body["task_id"], idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_capture_action_task(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if set(body) - {"objective_ids"} or not isinstance(
            body.get("objective_ids", []), list
        ):
            raise RequestError(
                "invalid_body", "conversion accepts only objective_ids", 400
            )
        self._send_service_result(
            self.stack.convert_capture_action(
                unquote(match.group(1)), unquote(match.group(2)),
                body.get("objective_ids", []), idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_capture_task(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.create_task_from_capture(
                unquote(match.group(1)), body, idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_capture_dismiss(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        if body:
            raise RequestError(
                "invalid_body", "dismiss requires an empty object", 400
            )
        self._send_service_result(
            self.stack.dismiss_capture(
                unquote(match.group(1)), idempotency_key,
                request_digest, path=path,
            )
        )

    def _post_reply_create(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.approve_reply(
                body, idempotency_key, request_digest, path=path
            )
        )

    def _post_reply_receipt(
        self, path: str, match: re.Match[str], body: dict[str, Any],
        request_digest: str, idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.apply_reply_receipt(
                unquote(match.group(1)), body, idempotency_key,
                request_digest, path=path,
            )
        )


__all__ = ("PostRouteMixin",)
