"""The one explicit write that activates the version 2 captures container.

Every other collection path leaves ``captures.json`` exactly as it found it: a
fresh store initializes container 1, ``open``/``load``/``consistent_read``
never convert, and the v5-to-v6 planner is not involved. This module owns the
single deliberate exception -- :meth:`record_capture_observation` -- and the
in-generation format change it performs the first time an owner actually saves
a historical source-check observation.

Trust boundary, stated once here and again in the method's own docstring: this
is a *trusted internal storage primitive*, not an authority verifier and not a
new public security boundary. It takes a CLOSED record the runtime already
admitted; no HTTP or CLI route reaches it in this slice. It does not re-prove
``capture_digest``, does not re-admit the connection policy, and does not run a
verifier. The caller that will eventually reach it must re-admit authority and
compare the retained binding while holding the SAME outer ``Store`` transaction
this method nests inside.

What it does enforce is admission against the store's own state: the record
must name the workspace these documents declare, and the Capture id and the
exact current Capture revision its binding names must exist on disk right now.
Those two refusals are bounded refusals about the submitted record, not claims
that the workspace is corrupt; malformed *stored* data keeps the store's own
corruption error.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from . import capture_observations
from .capture_observations import CaptureObservationError
from .store_document_validation import (
    CAPTURES_DOCUMENT_NAME,
    validate_document_values,
)
from .store_layout import STORE_SCHEMA_VERSION
from .store_rosters import V6_DOCUMENT_ORDER

__all__ = ["OPERATION_ID_PREFIX", "StoreCaptureObservationMixin"]

# A content-free operation id: this prefix plus the observation's own request
# UUID. It carries no query, title or path, and a retry after a crash lands on
# the same id rather than inventing a second operation for the same record.
OPERATION_ID_PREFIX = "workstack.capture-observation."


def _require_current_capture(
    document: Mapping[str, Any], binding: Mapping[str, Any]
) -> None:
    """Refuse unless the bound Capture exists at exactly the bound revision.

    A caller whose binding no longer matches the stored Capture is a bounded
    semantic refusal about that record: the workspace is intact, the record is
    simply about a Capture id this store does not hold, or about a revision it
    has moved past. The two codes are the ones the released read-only
    verification admission already publishes for the same two conditions.
    """

    captures = document.get("captures")
    if not isinstance(captures, list):
        raise CaptureObservationError("unknown_capture")
    wanted_id = binding["capture_id"]
    wanted_revision = binding["capture_revision"]
    for entry in captures:
        if not isinstance(entry, dict) or entry.get("id") != wanted_id:
            continue
        revision = entry.get("revision")
        if type(revision) is not int or revision != wanted_revision:
            raise CaptureObservationError("capture_revision_mismatch")
        return
    raise CaptureObservationError("unknown_capture")


def _stored_observations(document: Mapping[str, Any]) -> list[Any]:
    """The observation list this container already holds; empty for version 1."""

    if document["version"] == 2:
        return list(document["observations"])
    return []


def _planned_document(
    document: Mapping[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """The complete captures document to commit, with every Capture preserved.

    A version 2 container keeps its own captures list untouched and only its
    bounded observation list is replaced. A version 1 container is converted by
    the pure model's planner, which copies the captures list as a JSON value
    and writes no file; this module owns the backup and the commit around it.
    """

    if document["version"] == 2:
        planned = copy.deepcopy(dict(document))
        planned["observations"] = observations
        return planned
    planned = capture_observations.plan_capture_observations_upgrade(document)
    planned["observations"] = observations
    return planned


class StoreCaptureObservationMixin:
    """The explicit observation write for the composed ``Store``.

    Collaborators the composed Store owns: ``transaction``,
    ``_read_documents_locked``, ``_decoded_documents``, ``save_many``,
    ``_validate_ready_state_locked``, ``_readiness``, and the schema-upgrade
    collaborators ``_assert_upgrade_source_owned_locked``,
    ``_persist_prebackup_locked`` and ``_rebind_prebackup_locked``. Every
    private method it reaches for stays inside this mixin; no route or service
    obtains a generic raw-write surface through it.
    """

    def record_capture_observation(self, record: Any) -> dict[str, Any]:
        """Persist one closed historical observation and return it normalized.

        NOT an authority verifier. The ``capture_digest`` carried by ``record``
        is the runtime's; this method neither recomputes nor re-proves it, and
        admitting a record here is not evidence that the source is still
        accessible or that the connection policy still holds. The caller owns
        re-admission and comparison inside the same outer transaction.

        The transaction is reentrant, so an outer holder keeps its one lease
        and no second writer is taken. Refusals about the submitted record are
        :class:`~workstack.capture_observations.CaptureObservationError`;
        malformed stored documents raise ``StoreCorruptError`` as everywhere
        else. An identical retained record is a no-op that writes nothing,
        backs up nothing and journals nothing.
        """

        with self.transaction():
            bodies = self._read_documents_locked(V6_DOCUMENT_ORDER)
            values = self._decoded_documents(bodies)
            readiness = validate_document_values(
                values, schema_version=STORE_SCHEMA_VERSION
            )
            normalized = capture_observations.validate_observation(
                record, workspace_uid=readiness.workspace_uid
            )
            document = values[CAPTURES_DOCUMENT_NAME]
            _require_current_capture(document, normalized["request"]["binding"])
            planned = _planned_document(
                document,
                capture_observations.plan_observation_upsert(
                    _stored_observations(document),
                    normalized,
                    workspace_uid=readiness.workspace_uid,
                ),
            )
            if planned == document:
                return normalized
            self._commit_observation_locked(
                planned, bodies, values, readiness, normalized
            )
            return normalized

    def _commit_observation_locked(
        self,
        planned: dict[str, Any],
        bodies: Mapping[str, bytes],
        values: Mapping[str, dict[str, Any]],
        readiness: Any,
        normalized: Mapping[str, Any],
    ) -> None:
        """Commit the new captures document, backing up a format change first.

        An already-version-2 container is a bounded list update: no format
        backup is owed, and the ordinary journalled commit is the whole write.

        The first actual 1 -> 2 write is a format change inside this
        generation, so it earns the gate the schema upgrade earns before its
        own: the held bytes must still be the generation the runtime manifest
        recorded, and a rollback archive of the exact eleven original documents
        must be produced, read back, verified and re-bound at its final path
        before any journal exists. Every refusal there therefore leaves all
        eleven authoritative documents exactly as they were found. The upgraded
        document and the new record are then committed together through one
        ``save_many``; no empty version 2 document is published first, and the
        v5-to-v6 planner is never involved in this in-generation change.
        """

        operation_id = OPERATION_ID_PREFIX + str(
            normalized["request"]["verification_id"]
        )
        if values[CAPTURES_DOCUMENT_NAME]["version"] != 1:
            self.save_many({CAPTURES_DOCUMENT_NAME: planned}, operation_id=operation_id)
            self._readiness = self._validate_ready_state_locked()
            return
        self._assert_upgrade_source_owned_locked(bodies, values, readiness)
        target, verified_digest = self._persist_prebackup_locked(
            readiness.schema_version, bodies, readiness
        )
        self._rebind_prebackup_locked(target, verified_digest)
        self.save_many({CAPTURES_DOCUMENT_NAME: planned}, operation_id=operation_id)
        self._readiness = self._validate_ready_state_locked()
