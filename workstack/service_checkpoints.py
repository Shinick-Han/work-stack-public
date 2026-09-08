"""The shared ACTIVE Worklog view and the checkpoint transition command.

Every active reader in the application goes through ``_active_worklog`` so
supersession is applied once, in one place. The transition command itself is
ordering-critical: validation, then the receipt lookup, and only then anything
mutable, so an old exact replay returns its original event and neither saves
nor publishes.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .checkpoint_projection import (
    active_worklog_document,
    build_audit,
    physical_locator_for,
)
from .checkpoint_transition import (
    CheckpointTransitionError,
    build_transition_event,
    build_transition_notice,
    next_transition,
    normalize_transition_request,
    verify_locator,
)
from .service_composition import (
    _released_v3_attributed_composition_owner,
    _transactional,
)
from .service_domain import _next_id
from .service_errors import CheckpointTransitionConflictError, DomainError
from .storage.document_repository import WorkspaceDocument

_CHECKPOINT_ID = re.compile(r"CP-[0-9a-f]{64}")


class CheckpointServiceMixin:
    """The active Worklog view, the audit read and the transition command."""

    # ---- D5 shared projection and transitions ---------------------------
    #
    # Every ACTIVE reader below goes through one whole-history-validated view.
    # Physical writers deliberately do NOT: append, check-in and work-session
    # writers keep loading the raw document, so future ordinals and
    # first-for-task still count superseded rows.

    # The five closed pure history codes. "duplicate_revision" was never one
    # of them; "history_invalid" is, and an invalid known history must reach
    # the 409 envelope rather than being reported as a malformed request.
    _TRANSITION_CONFLICT_CODES = frozenset(
        {"stale_revision", "same_state", "exhausted", "locator_mismatch", "history_invalid"}
    )

    def _workspace_uid(self) -> str:
        readiness = self.store.readiness
        if readiness is None:
            raise DomainError("the workspace is not ready")
        return readiness.workspace_uid

    def _refuse_transition(self, error: CheckpointTransitionError) -> DomainError:
        """Map one closed pure code to its frozen public envelope."""

        code = getattr(error, "code", "malformed")
        if code in self._TRANSITION_CONFLICT_CODES:
            return CheckpointTransitionConflictError(code)
        return DomainError("the checkpoint transition request is invalid")

    def _active_worklog(self) -> dict[str, Any]:
        """The Worklog every active reader shares, superseded rows removed."""

        try:
            return active_worklog_document(
                workspace_uid=self._workspace_uid(),
                worklog=self.documents.load(WorkspaceDocument.WORKLOG),
                activity=self.documents.load(WorkspaceDocument.ACTIVITY),
            )
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error

    def active_worklog_view(self) -> dict[str, Any]:
        """The shared active Worklog view, for readers outside this class.

        The local Agent backend and the running-owner Agent context read through
        this so every active reader sees one whole-history-validated projection.
        The caller must already hold the transaction, exactly as the readers in
        this class do.
        """

        return self._active_worklog()

    @_transactional
    def list_checkpoint_audit(self) -> dict[str, Any]:
        """The exact frozen complete audit view over the whole history."""

        try:
            return build_audit(
                workspace_uid=self._workspace_uid(),
                worklog=self.documents.load(WorkspaceDocument.WORKLOG),
                activity=self.documents.load(WorkspaceDocument.ACTIVITY),
            )
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error

    def _admitted_transition_request(
        self, checkpoint_id: str, body: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """Everything decided before the digest, in the frozen order.

        The key, the identifier syntax, the storage composition and the body
        shape are all settled here, so a body the domain rejects never reaches
        the serializer, where a lone surrogate or a cycle would escape as
        UnicodeEncodeError or ValueError and leak position and value detail out
        of the public boundary.
        """

        # This NEW entrypoint requires an exact built-in str key. JSON and HTTP
        # cannot carry a str subclass, so nothing on the wire is affected;
        # legacy writers keep their own long-standing semantics untouched.
        if type(idempotency_key) is not str:
            raise DomainError("Idempotency-Key must be a string", {"field": "idempotency_key"})
        self._validate_idempotency_key(idempotency_key)
        if type(checkpoint_id) is not str or _CHECKPOINT_ID.fullmatch(checkpoint_id) is None:
            # Malformed identifier SYNTAX is a bad request. An absent but
            # canonical identifier is a different thing and stays a
            # locator_mismatch conflict below.
            raise DomainError("the checkpoint identifier is invalid")
        if not _released_v3_attributed_composition_owner(self):
            raise DomainError(
                "checkpoint transitions are not supported by this storage composition"
            )
        try:
            return normalize_transition_request(body)
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error

    def _transition_event(
        self,
        checkpoint_id: str,
        request: dict[str, Any],
        activity: dict[str, Any],
        origin: str | None,
    ) -> dict[str, Any]:
        """Locate, verify, advance and build - all before any fresh mutation."""

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        workspace_uid = self._workspace_uid()
        try:
            locator, row = physical_locator_for(
                workspace_uid=workspace_uid,
                checkpoint_id=checkpoint_id,
                worklog=worklog,
                activity=activity,
            )
            verify_locator({
                "workspace_uid": workspace_uid,
                "checkpoint_id": checkpoint_id,
                "recorded": row["recorded"],
                "actual_locator": locator,
            })
            transition = next_transition({
                "current": {"state": row["state"], "revision": row["revision"]},
                "request": request,
            })
            event = build_transition_event({
                "workspace_uid": workspace_uid,
                "checkpoint_id": checkpoint_id,
                "locator": locator,
                "transition": transition,
                "origin": origin,
            })
            if origin is not None:
                # Full notice shape and event capacity are proven BEFORE any
                # fresh mutation, at the ceiling publication could reach.
                build_transition_notice(
                    event, self.store.projected_change_event_id()
                )
        except CheckpointTransitionError as error:
            raise self._refuse_transition(error) from error
        return event

    @_transactional
    def apply_checkpoint_transition_v1(
        self,
        checkpoint_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
        request_digest: str | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """Supersede or restore one checkpoint, durably and idempotently.

        Ordering is the frozen one. Ordinary validation runs first, then the
        receipt lookup, and only then anything mutable: no locator, history,
        revision or capacity decision may precede a matching replay, so an old
        exact replay after later cycles returns its original event and saves and
        publishes nothing.

        The digest is supplied by the transport, which computes it from the RAW
        parsed body before any normalization: a direct caller cannot forge one
        that would match a differently-worded request.
        """

        request = self._admitted_transition_request(checkpoint_id, body, idempotency_key)
        # The digest is taken only after admission, and always of the ORIGINAL raw parsed body
        # rather than the normalized replacement, so replay identity keeps
        # distinguishing bodies the domain would normalize together. A supplied
        # digest is verified, never trusted.
        request_digest = self._raw_request_digest(body, request_digest)

        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        event = self._transition_event(checkpoint_id, request, activity, origin)

        response_body = {"data": copy.deepcopy(event), "meta": {"replayed": False}}
        feed = activity.setdefault("activity", [])
        feed.append({
            "id": _next_id(feed, "E", 6),
            "type": event["type"],
            "created_at": self._utc_now(),
            "task_id": event["task_id"],
            "details": copy.deepcopy(event),
        })
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        # ONE Activity-only save commits the transition and its receipt
        # atomically. Worklog and Task bytes are never touched by a transition.
        self.documents.save_many(
            {WorkspaceDocument.ACTIVITY: activity},
            operation_id="checkpoint-transition-{}".format(idempotency_key),
        )
        if origin is not None:
            # The existing Store publisher is reused unchanged: store.py is not
            # an owned path in this packet, and it needs no change. The record
            # carries the notice, and the encoder tells the two variants apart
            # by their disjoint frozen field sets.
            self.store.publish_change_notice(
                lambda event_id: build_transition_notice(event, event_id)
            )
        return {"status": 201, "body": response_body}
