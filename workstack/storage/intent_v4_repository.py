"""Default-off v4 repository backend for the bounded intent mutation slice."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .canonical import canonical_json_bytes
from .idempotency import (
    append_idempotency_record,
    new_idempotency_ledger,
    parse_idempotency_ledger,
    stage_idempotency_ledger,
)
from .intent_contract import (
    IntentContractError,
    NormalizedIntent,
    normalize_checkin_intent,
    normalize_note_intent,
    normalize_worklog_intent,
    replay_response,
    success_response,
    validate_idempotency_key,
)
from .journal import JournalTarget
from .manifest import build_v4_manifest
from .manifest_store import read_runtime_manifest
from .reader import V4ReadResult, read_v4
from .records import stage_record_put
from .runtime import RuntimeAuthority
from .streams import stage_stream_appends
from .write_session import execute_write_session


class V4IntentRepositoryError(RuntimeError):
    """A content-free refusal at the inactive v4 intent boundary."""

    command_boundary = "intent"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class V4IntentRepository:
    """Commit notes/check-ins/worklog entries through existing v4 CAS primitives."""

    format_version = 4

    def __init__(
        self,
        runtime: RuntimeAuthority,
        *,
        enable_v4_intents: bool = False,
        now: Callable[[], str],
        uid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        checkpoint_facts: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        if enable_v4_intents is not True:
            raise V4IntentRepositoryError("V4_INTENTS_DISABLED")
        self._runtime = runtime
        self._now = now
        self._uid_factory = uid_factory
        # The pure checkpoint facts builder lives above this layer
        # (workstack/checkpoint_change.py); the composition root injects it so
        # the storage layer never imports upward. Without it a worklog entry
        # refuses instead of silently diverging from the released v3 record.
        self._checkpoint_facts = checkpoint_facts

    def create_note(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/notes",
    ) -> dict[str, Any]:
        physical, ledger, ledger_body, generation = self._baseline()
        intent = normalize_note_intent(
            body,
            [str(item["display_id"]) for item in physical.records["notes"]],
            created_date=self._timestamp()[:10],
            path=path,
        )
        replay = self._replay(ledger, idempotency_key, intent)
        if replay is not None:
            return replay
        note = self._note_record(physical.workspace_uid, intent)
        staged = stage_record_put(
            "notes", note, current=None, expected_revision=None, expected_digest=None
        )
        target = JournalTarget.replace(
            staged.artifact, staged.body or b"", expected_digest=staged.expected_digest
        )
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            (target,),
            operation_id=f"graph-note-create-{idempotency_key}",
        )

    def checkin(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/checkin",
    ) -> dict[str, Any]:
        physical, ledger, ledger_body, generation = self._baseline()
        intent = normalize_checkin_intent(body, path=path)
        replay = self._replay(ledger, idempotency_key, intent)
        if replay is not None:
            return replay
        event = self._worklog_event(physical.workspace_uid, intent, kind="check-in")
        targets = self._stream_targets(physical, (("worklog", event),))
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            targets,
            operation_id=f"review-checkin-{idempotency_key}",
        )

    def add_worklog(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/entries",
    ) -> dict[str, Any]:
        physical, ledger, ledger_body, generation = self._baseline()
        task = self._task(physical, body.get("task_id"))
        legacy_task = {"id": task["display_id"], "title": task["title"]}
        intent = normalize_worklog_intent(body, legacy_task, path=path)
        replay = self._replay(ledger, idempotency_key, intent)
        if replay is not None:
            return replay
        event = self._worklog_event(
            physical.workspace_uid, intent, kind="entry", task=task
        )
        recorded = self._recorded_fact(physical, intent, idempotency_key, task=task)
        targets = self._stream_targets(
            physical, (("worklog", event), ("activity", recorded))
        )
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            targets,
            operation_id=f"review-entry-{idempotency_key}",
        )

    def _timestamp(self) -> str:
        value = self._now()
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise V4IntentRepositoryError("CLOCK_TIMESTAMP_INVALID") from error
        if not value.endswith("Z") or parsed.utcoffset() != dt.timedelta(0):
            raise V4IntentRepositoryError("CLOCK_TIMESTAMP_INVALID")
        return value

    def _baseline(self) -> tuple[V4ReadResult, dict[str, Any], bytes | None, int]:
        physical = read_v4(self._runtime.authority_root)
        state = read_runtime_manifest(self._runtime.manifest_path)
        if state is None:
            raise V4IntentRepositoryError("RUNTIME_MANIFEST_MISSING")
        actual = build_v4_manifest(physical, generation=state.generation)
        if actual.digest != state.manifest.digest:
            raise V4IntentRepositoryError("RUNTIME_MANIFEST_STALE")
        try:
            body = self._runtime.idempotency_path.read_bytes()
        except FileNotFoundError:
            body = None
            ledger = new_idempotency_ledger(
                physical.workspace_uid, updated_at=self._timestamp()
            )
        else:
            ledger = parse_idempotency_ledger(
                body, expected_workspace_uid=physical.workspace_uid
            )
        return physical, ledger, body, state.generation

    @staticmethod
    def _replay(
        ledger: Mapping[str, Any], key: str, intent: NormalizedIntent
    ) -> dict[str, Any] | None:
        return replay_response(
            ledger["records"],
            validate_idempotency_key(key),
            method="POST",
            path=intent.path,
            request_digest=intent.request_digest,
        )

    def _note_record(
        self, workspace_uid: str, intent: NormalizedIntent
    ) -> dict[str, Any]:
        note = intent.authority_value
        created = str(note["created"])
        return {
            "format": "workstack.note",
            "schema_version": 1,
            "workspace_uid": workspace_uid,
            "uid": self._uid(),
            "revision": 0,
            "created_at": created,
            "updated_at": created,
            "display_id": note["id"],
            "note_kind": "standalone",
            "task_uid": None,
            "text": note["text"],
            "links": copy.deepcopy(note["links"]),
            "created_by": "local.user",
        }

    def _worklog_event(
        self,
        workspace_uid: str,
        intent: NormalizedIntent,
        *,
        kind: str,
        task: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = intent.authority_value
        event: dict[str, Any] = {
            "format": "workstack.worklog-event",
            "schema_version": 1,
            "workspace_uid": workspace_uid,
            "event_uid": self._uid(),
            "record_uid": None if task is None else task["uid"],
            "created_at": self._timestamp(),
            "actor": "local.user",
            "provenance": "workstack.intent-v4",
            "kind": kind,
            "work_date": value["date"],
        }
        if kind == "check-in":
            event["start_time"] = value["time"]
        else:
            if task is None:
                raise V4IntentRepositoryError("TASK_REQUIRED")
            event.update(
                {
                    "task_uid": task["uid"],
                    "task_display_id": task["display_id"],
                    "task_title": task["title"],
                    "done": copy.deepcopy(value["done"]),
                    "next": copy.deepcopy(value["next"]),
                    "blockers": copy.deepcopy(value["blockers"]),
                }
            )
        return event

    def _recorded_fact(
        self,
        physical: V4ReadResult,
        intent: NormalizedIntent,
        idempotency_key: str,
        *,
        task: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Mirror the released-v3 ``worklog.recorded`` Activity fact.

        The released v3 writer records one fact per NEW idempotent checkpoint
        beside the idempotency receipt (service.add_worklog_v1); the admitted
        pure builder decides the CP identity, locator and physical-first
        semantics from the same inputs v3 supplies: the date-local ordinal
        captured before the append and every previously accepted entry
        flattened across all dates.
        """

        value = intent.authority_value
        entry = {
            field: copy.deepcopy(value[field])
            for field in ("task_id", "task", "done", "next", "blockers")
        }
        ordinal = 0
        prior_entries: list[dict[str, Any]] = []
        for stored in sorted(
            physical.streams["worklog"],
            key=lambda item: (item["sequence"], item["event_uid"]),
        ):
            if stored.get("kind") != "entry":
                continue
            if stored["work_date"] == value["date"]:
                ordinal += 1
            prior_entries.append({"task_id": stored["task_display_id"]})
        if self._checkpoint_facts is None:
            raise V4IntentRepositoryError("CHECKPOINT_FACTS_UNAVAILABLE")
        try:
            facts = self._checkpoint_facts(
                workspace_uid=physical.workspace_uid,
                idempotency_key=idempotency_key,
                date=value["date"],
                entry=entry,
                ordinal=ordinal,
                prior_entries=prior_entries,
                origin=None,
            )
        except ValueError as error:
            # CheckpointChangeError is a ValueError; the code stays content-free.
            raise V4IntentRepositoryError("CHECKPOINT_FACTS_INVALID") from error
        return {
            "format": "workstack.activity-event",
            "schema_version": 1,
            "workspace_uid": physical.workspace_uid,
            "event_uid": self._uid(),
            "record_uid": task["uid"],
            "created_at": self._timestamp(),
            "actor": "local.user",
            "provenance": "workstack.intent-v4",
            "legacy_event_id": _next_event_id(physical),
            "event_type": facts["recorded"]["type"],
            "details": copy.deepcopy(facts["recorded"]),
            "capture_uid": None,
            "task_uid": task["uid"],
            "reply_uid": None,
        }

    @staticmethod
    def _task(physical: V4ReadResult, value: object) -> Mapping[str, Any]:
        wanted = value.strip().upper() if isinstance(value, str) else ""
        matches = [item for item in physical.records["tasks"] if item["display_id"] == wanted]
        if len(matches) != 1:
            raise IntentContractError("TASK_NOT_FOUND")
        return matches[0]

    @staticmethod
    def _stream_targets(
        physical: V4ReadResult,
        additions: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> tuple[JournalTarget, ...]:
        digests = {
            artifact.artifact: artifact.sha256
            for artifact in physical.artifacts
            if artifact.category == "stream"
        }
        writes = stage_stream_appends(
            physical.streams, additions, current_artifact_digests=digests
        )
        return tuple(
            JournalTarget.replace(
                item.artifact, item.body, expected_digest=item.expected_digest
            )
            for item in writes
        )

    def _commit(
        self,
        physical: V4ReadResult,
        ledger: Mapping[str, Any],
        ledger_body: bytes | None,
        generation: int,
        intent: NormalizedIntent,
        key: str,
        authority_targets: Sequence[JournalTarget],
        *,
        operation_id: str,
        response_status: int = 201,
    ) -> dict[str, Any]:
        now = self._timestamp()
        response = success_response(intent.response_data)
        response["status"] = response_status
        record = self._idempotency_record(
            key, intent, response, now, response_status=response_status
        )
        proposed_ledger, duplicate = append_idempotency_record(ledger, record, now=now)
        if duplicate:
            raise V4IntentRepositoryError("UNEXPECTED_IDEMPOTENCY_DUPLICATE")
        runtime_target = stage_idempotency_ledger(
            proposed_ledger, current_body=ledger_body
        )
        manifest = self._proposed_manifest(authority_targets, generation + 1)
        execute_write_session(
            self._runtime,
            (*authority_targets, runtime_target),
            manifest,
            operation_id=operation_id,
            created_at=now,
        )
        return response

    @staticmethod
    def _idempotency_record(
        key: str,
        intent: NormalizedIntent,
        response: Mapping[str, Any],
        created_at: str,
        *,
        response_status: int,
    ) -> dict[str, Any]:
        instant = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        expiry = (instant + dt.timedelta(days=30)).isoformat().replace("+00:00", "Z")
        return {
            "key": key,
            "method": "POST",
            "path": intent.path,
            "request_digest": intent.request_digest,
            "response_status": response_status,
            "created_at": created_at,
            "expires_at": expiry,
            "response_body": copy.deepcopy(response["body"]),
        }

    def _proposed_manifest(
        self, targets: Sequence[JournalTarget], generation: int
    ):
        parent = self._runtime.runtime_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="intent-proposal-", dir=parent) as directory:
            proposed = Path(directory) / "authority"
            shutil.copytree(self._runtime.authority_root, proposed)
            for target in targets:
                path = proposed.joinpath(*target.artifact.split("/"))
                if target.proposed_bytes is None:
                    raise V4IntentRepositoryError("PROPOSAL_CONTENT_MISSING")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(target.proposed_bytes)
            return build_v4_manifest(read_v4(proposed), generation=generation)

    def _uid(self) -> str:
        value = self._uid_factory()
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as error:
            raise V4IntentRepositoryError("UID_FACTORY_INVALID") from error
        if parsed.int == 0 or str(parsed) != value:
            raise V4IntentRepositoryError("UID_FACTORY_INVALID")
        return value


def _next_event_id(physical: V4ReadResult) -> str:
    """The next v3-shaped Activity id, mirroring the released ``_next_id``."""

    largest = max(
        (int(str(event["legacy_event_id"])[2:]) for event in physical.streams["activity"]),
        default=0,
    )
    return f"E-{largest + 1:06d}"
