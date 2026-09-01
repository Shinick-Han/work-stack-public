"""Explicit opt-in capture/reply commands for normalized v4 authorities.

The command layer deliberately works against the backend-neutral semantic read
model.  It validates and prepares one detached legacy-shaped proposal, then
uses the v4 record, stream, idempotency and write-session primitives for the
only filesystem mutation.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from ..capture import canonical_digest, parse_rfc3339, validate_capture_packet
from .canonical import canonical_json_bytes
from .command_backend_support import (
    V4CommandBackendSupportError,
    commit_command_proposal,
    load_verified_command_baseline,
)
from .idempotency import stage_idempotency_ledger
from .journal import JournalTarget
from .migration_conversion import convert_v3_documents
from .read_repository import V4WorkspaceRepository
from .reader import V4ReadResult
from .records import stage_record_put
from .runtime import RuntimeAuthority
from .streams import stage_stream_appends
from .write_session import recover_write_session


Clock = Callable[[], str]
FaultHook = Callable[[str], None]
_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPTURE_FIELDS = (
    "id", "schema_version", "source_key", "source", "normalized", "task_hints",
    "provenance", "status", "linked_task_ids", "converted_task_ids", "revision",
    "created_at", "updated_at",
)
_REPLY_FIELDS = (
    "id", "task_id", "capture_id", "capture_revision", "provider", "capability",
    "target", "body", "body_digest", "target_digest", "state", "approved_at",
    "receipt", "created_at", "updated_at",
)
_REPLY_CAPABILITIES = {
    "microsoft-outlook": "outlook.reply",
    "microsoft-teams": "teams.reply",
}
_REPLY_TARGET_FIELDS = (
    "resource_type", "connection_ref", "container_ref", "object_ref", "version_ref",
)


class CaptureReplyRepositoryError(ValueError):
    """Stable, content-free command refusal."""

    command_boundary = "capture-reply"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _next_id(items: list[dict[str, Any]], prefix: str, width: int) -> str:
    largest = 0
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.I)
    for item in items:
        match = pattern.fullmatch(str(item.get("id", "")))
        if match:
            largest = max(largest, int(match.group(1)))
    return f"{prefix.upper()}-{largest + 1:0{width}d}"


def _find(items: list[dict[str, Any]], display_id: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item.get("id") == display_id:
            return item
    raise CaptureReplyRepositoryError(f"{kind}_not_found")


def _project(value: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: copy.deepcopy(value[field]) for field in fields}


def _event(
    documents: dict[str, dict[str, Any]], event_type: str, now: str, **references: Any
) -> None:
    events = documents["activity.json"].setdefault("activity", [])
    value: dict[str, Any] = {
        "id": _next_id(events, "E", 6),
        "type": event_type,
        "created_at": now,
        "details": copy.deepcopy(references.pop("details", {}) or {}),
    }
    value.update({key: item for key, item in references.items() if item})
    events.append(value)


def _capture_review_digest(capture: Mapping[str, Any]) -> str:
    normalized = copy.deepcopy(capture.get("normalized", {}))
    for action in normalized.get("action_items", []):
        if isinstance(action, dict):
            action.pop("task_id", None)
    return canonical_digest(
        {"normalized": normalized, "task_hints": capture.get("task_hints", [])}
    )


def _validate_key(value: str) -> None:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise CaptureReplyRepositoryError("invalid_idempotency_key")


def _request_digest(value: str | None, body: Any) -> str:
    return value if value is not None else canonical_digest(body)


def _replay(
    documents: Mapping[str, Mapping[str, Any]], key: str, method: str, path: str,
    request_digest: str,
) -> dict[str, Any] | None:
    _validate_key(key)
    for record in documents["activity.json"].get("idempotency", []):
        if record.get("key") != key:
            continue
        if any(record.get(field) != expected for field, expected in (
            ("method", method), ("path", path), ("request_digest", request_digest)
        )):
            raise CaptureReplyRepositoryError("idempotency_conflict")
        reference = record.get("response_ref")
        if reference is None:
            body = copy.deepcopy(record["response_body"])
        else:
            reply = _find(
                documents["replies.json"]["replies"], reference["id"], "reply"
            )
            body = {"data": _project(reply, _REPLY_FIELDS)}
            if record.get("response_meta"):
                body["meta"] = copy.deepcopy(record["response_meta"])
        body.setdefault("meta", {})["replayed"] = True
        return {"status": 200, "body": body}
    return None


def _record_idempotency(
    documents: dict[str, dict[str, Any]], key: str, path: str,
    request_digest: str, status: int, now: str, *, body: dict[str, Any] | None = None,
    reply_id: str | None = None, response_meta: dict[str, Any] | None = None,
    method: str = "POST",
) -> None:
    record: dict[str, Any] = {
        "key": key, "method": method, "path": path,
        "request_digest": request_digest, "response_status": status,
        "created_at": now,
    }
    if reply_id is None:
        record["response_body"] = copy.deepcopy(body)
    else:
        record["response_ref"] = {"kind": "reply", "id": reply_id}
        if response_meta:
            record["response_meta"] = copy.deepcopy(response_meta)
    documents["activity.json"].setdefault("idempotency", []).append(record)


class V4CaptureReplyRepository:
    """Capture/reply writer activated only by an explicit v4 caller."""

    def __init__(
        self,
        authority_root: Path | str,
        runtime: RuntimeAuthority,
        *,
        task_note_source_indexes: Mapping[str, int] | None = None,
        clock: Clock | None = None,
        fault_hook: FaultHook | None = None,
        enable_v4_capture_reply_commands: bool = False,
    ) -> None:
        if enable_v4_capture_reply_commands is not True:
            raise CaptureReplyRepositoryError("v4_capture_reply_commands_not_enabled")
        self.authority_root = Path(authority_root).resolve(strict=False)
        self.runtime = runtime
        self.task_note_source_indexes = copy.deepcopy(task_note_source_indexes)
        self.clock = clock or _utc_now
        self.fault_hook = fault_hook

    def recover(self) -> None:
        recover_write_session(self.runtime, fault_hook=self.fault_hook)

    def _load(self) -> tuple[V4ReadResult, dict[str, Any], dict[str, dict[str, Any]], int]:
        self.recover()
        try:
            baseline = load_verified_command_baseline(self.authority_root, self.runtime)
        except V4CommandBackendSupportError as error:
            raise CaptureReplyRepositoryError(error.code) from error
        ledger = dict(baseline.ledger)
        result = V4WorkspaceRepository(
            self.authority_root,
            idempotency_ledger=ledger,
            task_note_source_indexes=self.task_note_source_indexes,
            generation=baseline.generation,
        ).read()
        return (
            baseline.physical,
            ledger,
            result.snapshot.to_v3_documents(),
            baseline.generation,
        )

    def state_documents(self) -> dict[str, dict[str, Any]]:
        return self._load()[2]

    def ingest_capture(
        self,
        packet: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        method: str = "POST",
        path: str = "/api/v1/captures",
    ) -> dict[str, Any]:
        physical, ledger, documents, generation = self._load()
        request_digest = _request_digest(request_digest, packet)
        replay = _replay(documents, idempotency_key, method, path, request_digest)
        if replay:
            return replay
        sanitized = validate_capture_packet(packet)
        now = self.clock()
        captures = documents["captures.json"]["captures"]
        capture = next(
            (item for item in captures if item.get("source_key") == sanitized["source_key"]),
            None,
        )
        duplicate = False
        status = 201
        if capture is None:
            capture = {
                **sanitized, "id": _next_id(captures, "C", 4), "status": "inbox",
                "linked_task_ids": [], "converted_task_ids": [], "revision": 0,
                "created_at": now, "updated_at": now, "recent_revisions": [],
            }
            captures.append(capture)
            _event(documents, "capture.ingested", now, capture_id=capture["id"])
        elif capture["source"].get("fingerprint") == sanitized["source"]["fingerprint"]:
            if _capture_review_digest(capture) != _capture_review_digest(sanitized):
                raise CaptureReplyRepositoryError("source_revision_conflict")
            duplicate, status = True, 200
        else:
            self._update_capture_revision(capture, sanitized, now)
            status = 200
            _event(
                documents, "capture.updated", now, capture_id=capture["id"],
                details={"revision": capture["revision"]},
            )
        body: dict[str, Any] = {"data": _project(capture, _CAPTURE_FIELDS)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        _record_idempotency(
            documents, idempotency_key, path, request_digest, status, now,
            body=body, method=method,
        )
        self._commit(physical, ledger, documents, generation, now, f"capture-ingest-{idempotency_key}")
        return {"status": status, "body": body}

    @staticmethod
    def _update_capture_revision(
        capture: dict[str, Any], sanitized: Mapping[str, Any], now: str
    ) -> None:
        old_time = parse_rfc3339(capture["source"]["retrieved_at"], "stored")
        new_time = parse_rfc3339(sanitized["source"]["retrieved_at"], "incoming")
        if new_time < old_time:
            raise CaptureReplyRepositoryError("stale_capture")
        if new_time == old_time:
            raise CaptureReplyRepositoryError("source_revision_conflict")
        linked_actions = {
            item["id"]: item.get("task_id")
            for item in capture["normalized"]["action_items"] if item.get("task_id")
        }
        incoming = copy.deepcopy(dict(sanitized))
        for action in incoming["normalized"]["action_items"]:
            if action["id"] in linked_actions:
                action["task_id"] = linked_actions[action["id"]]
        recent = list(capture.get("recent_revisions", []))
        recent.append({
            "fingerprint": capture["source"].get("fingerprint"),
            "version_ref": capture["source"].get("version_ref"),
            "retrieved_at": capture["source"].get("retrieved_at"),
            "provenance_digest": canonical_digest(capture.get("provenance", {})),
            "redaction_policy_version": capture.get("provenance", {}).get("redaction_policy_version"),
        })
        for field in ("schema_version", "source_key", "source", "normalized", "task_hints", "provenance"):
            capture[field] = incoming[field]
        capture["recent_revisions"] = recent[-10:]
        capture["revision"] += 1
        capture["updated_at"] = now

    def link_capture(
        self,
        capture_id: str,
        task_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        physical, ledger, documents, generation = self._load()
        path = path or f"/api/v1/captures/{capture_id}/link"
        request_digest = _request_digest(request_digest, {"task_id": task_id})
        replay = _replay(documents, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        _find(documents["backlog.json"]["tasks"], task_id, "task")
        capture = _find(documents["captures.json"]["captures"], capture_id, "capture")
        duplicate = task_id in capture["linked_task_ids"]
        now = self.clock()
        if not duplicate:
            capture["linked_task_ids"] = sorted((*capture["linked_task_ids"], task_id))
            if capture["status"] != "converted":
                capture["status"] = "linked"
            capture["revision"] += 1
            capture["updated_at"] = now
            _event(documents, "capture.linked", now, capture_id=capture_id, task_id=task_id)
        body: dict[str, Any] = {"data": _project(capture, _CAPTURE_FIELDS)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        _record_idempotency(documents, idempotency_key, path, request_digest, 200, now, body=body)
        self._commit(physical, ledger, documents, generation, now, f"capture-link-{idempotency_key}")
        return {"status": 200, "body": body}

    def approve_reply(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str = "/api/v1/replies",
    ) -> dict[str, Any]:
        if set(request) != {"task_id", "capture_id", "body", "approved"}:
            raise CaptureReplyRepositoryError("reply_request_invalid")
        if request.get("approved") is not True or not isinstance(request.get("body"), str):
            raise CaptureReplyRepositoryError("reply_request_invalid")
        body_text = request["body"]
        if not body_text.strip() or len(body_text) > 12_000 or "<" in body_text:
            raise CaptureReplyRepositoryError("reply_body_invalid")
        physical, ledger, documents, generation = self._load()
        request_digest = _request_digest(request_digest, request)
        replay = _replay(documents, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        task = _find(documents["backlog.json"]["tasks"], request["task_id"], "task")
        capture = _find(documents["captures.json"]["captures"], request["capture_id"], "capture")
        if task["id"] not in set(capture["linked_task_ids"] + capture["converted_task_ids"]):
            raise CaptureReplyRepositoryError("capture_not_linked")
        provider = capture["source"].get("provider")
        if provider not in _REPLY_CAPABILITIES:
            raise CaptureReplyRepositoryError("reply_provider_unsupported")
        target = {field: capture["source"].get(field) for field in _REPLY_TARGET_FIELDS}
        if any(not isinstance(value, str) or not value or len(value) > 512 for value in target.values()):
            raise CaptureReplyRepositoryError("reply_target_invalid")
        now = self.clock()
        replies = documents["replies.json"]["replies"]
        reply = {
            "id": _next_id(replies, "R", 4), "task_id": task["id"],
            "capture_id": capture["id"], "capture_revision": capture["revision"],
            "provider": provider, "capability": _REPLY_CAPABILITIES[provider],
            "target": target, "body": body_text, "body_digest": canonical_digest(body_text),
            "target_digest": canonical_digest(target), "state": "approved",
            "approved_at": now, "receipt": None, "created_at": now, "updated_at": now,
        }
        replies.append(reply)
        _event(
            documents, "reply.approved", now, capture_id=capture["id"],
            task_id=task["id"], reply_id=reply["id"],
            details={"provider": provider, "state": "approved"},
        )
        _record_idempotency(
            documents, idempotency_key, path, request_digest, 201, now,
            reply_id=reply["id"],
        )
        self._commit(physical, ledger, documents, generation, now, f"reply-approve-{idempotency_key}")
        return {"status": 201, "body": {"data": _project(reply, _REPLY_FIELDS)}}

    def apply_reply_receipt(
        self,
        reply_id: str,
        receipt: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        self._validate_receipt(receipt)
        physical, ledger, documents, generation = self._load()
        path = path or f"/api/v1/replies/{reply_id}/receipt"
        request_digest = _request_digest(request_digest, receipt)
        replay = _replay(documents, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        reply = _find(documents["replies.json"]["replies"], reply_id, "reply")
        mismatched = [
            field for field in ("reply_id", "provider", "body_digest", "target_digest")
            if receipt[field] != ({"reply_id": reply["id"]}.get(field, reply.get(field)))
        ]
        if mismatched:
            raise CaptureReplyRepositoryError("reply_receipt_conflict")
        duplicate = reply["receipt"] is not None
        if duplicate and reply["receipt"] != receipt:
            raise CaptureReplyRepositoryError("reply_receipt_conflict")
        now = self.clock()
        if not duplicate:
            reply["state"] = receipt["outcome"]
            reply["receipt"] = copy.deepcopy(receipt)
            reply["updated_at"] = now
            details = {"provider": reply["provider"], "state": reply["state"]}
            if "error_code" in receipt:
                details["error_code"] = receipt["error_code"]
            _event(
                documents, f"reply.{reply['state']}", now,
                capture_id=reply["capture_id"], task_id=reply["task_id"],
                reply_id=reply["id"], details=details,
            )
        response_body: dict[str, Any] = {"data": _project(reply, _REPLY_FIELDS)}
        if duplicate:
            response_body["meta"] = {"duplicate": True}
        _record_idempotency(
            documents, idempotency_key, path, request_digest, 200, now,
            reply_id=reply["id"], response_meta={"duplicate": True} if duplicate else None,
        )
        self._commit(physical, ledger, documents, generation, now, f"reply-receipt-{idempotency_key}")
        return {"status": 200, "body": response_body}

    @staticmethod
    def _validate_receipt(receipt: dict[str, Any]) -> None:
        required = {
            "schema_version", "reply_id", "provider", "outcome", "occurred_at",
            "body_digest", "target_digest",
        }
        optional = {"remote_message_ref", "web_url", "error_code"}
        if not isinstance(receipt, dict) or set(receipt) - required - optional or required - set(receipt):
            raise CaptureReplyRepositoryError("reply_receipt_invalid")
        if receipt["schema_version"] != "1.0" or receipt["provider"] not in _REPLY_CAPABILITIES:
            raise CaptureReplyRepositoryError("reply_receipt_invalid")
        if receipt["outcome"] not in {"sent", "failed", "unknown"}:
            raise CaptureReplyRepositoryError("reply_receipt_invalid")
        try:
            parse_rfc3339(receipt["occurred_at"], "occurred_at")
        except ValueError as error:
            raise CaptureReplyRepositoryError("reply_receipt_invalid") from error
        if any(_SHA256.fullmatch(str(receipt[field])) is None for field in ("body_digest", "target_digest")):
            raise CaptureReplyRepositoryError("reply_receipt_invalid")

    def _commit(
        self, current: V4ReadResult, current_ledger: Mapping[str, Any],
        documents: dict[str, dict[str, Any]], generation: int, now: str,
        operation_id: str,
    ) -> None:
        conversion = convert_v3_documents(documents, candidate_created_at=now)
        artifact_digests = {item.artifact: item.sha256 for item in current.artifacts}
        targets = self._record_targets(current, conversion, artifact_digests)
        targets.extend(self._activity_targets(current, conversion))
        current_ledger_body = canonical_json_bytes(dict(current_ledger))
        targets.append(stage_idempotency_ledger(
            conversion.idempotency_ledger, current_body=current_ledger_body
        ))
        commit_command_proposal(
            self.authority_root, self.runtime, targets, generation=generation + 1,
            operation_id=operation_id, created_at=now, fault_hook=self.fault_hook,
            proposal_prefix="capture-reply-proposal-",
        )

    @staticmethod
    def _record_targets(current, conversion, artifact_digests) -> list[JournalTarget]:
        targets: list[JournalTarget] = []
        current_records = {
            kind: {str(record["uid"]): record for record in values}
            for kind, values in current.records.items()
        }
        for kind in ("captures", "replies"):
            for proposed_source in conversion.records[kind]:
                proposed = copy.deepcopy(dict(proposed_source))
                prior = current_records[kind].get(str(proposed["uid"]))
                if prior is not None and dict(prior) == proposed:
                    continue
                if prior is not None and kind == "replies":
                    proposed["revision"] = int(prior["revision"]) + 1
                staged = stage_record_put(
                    kind, proposed, current=prior,
                    expected_revision=None if prior is None else int(prior["revision"]),
                    expected_digest=None if prior is None else artifact_digests[
                        f"records/{kind}/{proposed['uid'][:2]}/{proposed['uid']}.json"
                    ],
                )
                targets.append(JournalTarget.replace(
                    staged.artifact, staged.body or b"", expected_digest=staged.expected_digest
                ))
        return targets

    @staticmethod
    def _activity_targets(current, conversion) -> list[JournalTarget]:
        current_activity_count = len(current.streams["activity"])
        additions = []
        for event in conversion.streams["activity"][current_activity_count:]:
            value = {key: copy.deepcopy(item) for key, item in event.items()
                     if key not in {"sequence", "previous_event_digest", "event_digest"}}
            additions.append(("activity", value))
        stream_digests = {
            item.artifact: item.sha256 for item in current.artifacts
            if item.category == "stream"
        }
        targets: list[JournalTarget] = []
        for staged in stage_stream_appends(
            current.streams, additions, current_artifact_digests=stream_digests
        ):
            targets.append(JournalTarget.replace(
                staged.artifact, staged.body, expected_digest=staged.expected_digest
            ))
        return targets
