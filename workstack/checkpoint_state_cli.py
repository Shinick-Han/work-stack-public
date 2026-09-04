"""The public ``worklog checkpoint-state`` command.

One explicit invocation sends exactly one POST to the running owner's
``/api/v1/review/checkpoints/{id}/transitions`` route and prints the durable
event envelope the owner returns. The command owns no network pipeline of its
own: it reuses the admitted owner metadata classification, the session, storage
and sync preflight and the final advertisement revalidation in
:mod:`workstack.cli_writer`.

The body is the caller's own parsed JSON. It is never trimmed, reordered or
normalized before the send, because the owner computes its idempotency digest
from the raw parsed body: rewriting it here would change the identity of an
otherwise identical reinvocation.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping

from workstack import checkpoint_transition, cli_writer

CHECKPOINT_TRANSITIONS = "/api/v1/review/checkpoints/{}/transitions"
# The frozen attribution for this client, matching the owner's exact value.
AGENT_CLIENT_HEADER = "X-WorkStack-Client"
AGENT_CLIENT_VALUE = "agent-cli-v1"
STDIN_LIMIT = 32 * 1024
REASON_FIELDS = ("code", "explanation")
BODY_FIELDS = ("state", "revision", "reason")
# Same spellings the admitted policy and the owner enforce. They are restated
# here so a malformed request is refused before the network, not duplicated as
# a second domain decision: every value question is answered by
# checkpoint_transition.normalize_transition_request below.
CHECKPOINT_ID = re.compile(r"\ACP-[0-9a-f]{64}\Z", re.ASCII)
IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9._:-]{8,128}\Z", re.ASCII)
# The eleven durable fields of a transition event.
EVENT_FIELDS = (
    "type", "workspace_uid", "task_id", "checkpoint_id", "date", "ordinal",
    "entry_digest", "state", "revision", "reason", "origin",
)


def parse_body(raw: bytes) -> dict[str, Any]:
    """The caller's JSON object, validated for shape only and never rewritten.

    Only the field set and the coarse types are checked here so a malformed
    request is refused before it reaches the owner. Every domain decision -
    which states are legal, which reason codes exist - belongs to the admitted
    policy on the owner side and is not duplicated.
    """

    if len(raw) > STDIN_LIMIT:
        raise ValueError("checkpoint state request exceeds 32 KiB")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stdin must contain one UTF-8 JSON object") from error
    if not isinstance(body, dict) or set(body) != set(BODY_FIELDS):
        raise ValueError("stdin must contain exactly state, revision and reason")
    if not isinstance(body.get("state"), str):
        raise ValueError("state must be a string")
    revision = body.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    reason = body.get("reason")
    if not isinstance(reason, dict) or set(reason) != set(REASON_FIELDS):
        raise ValueError("reason must carry exactly code and explanation")
    if not isinstance(reason.get("code"), str) or not isinstance(
        reason.get("explanation"), str
    ):
        raise ValueError("reason code and explanation must be strings")
    # The admitted policy answers every domain question - which states exist,
    # which reason codes belong to which state, the revision range - on a
    # SEPARATE deep copy. The caller's own object is returned untouched, so the
    # bytes the owner digests are exactly the bytes that were parsed, padded
    # strings and NFD included.
    try:
        checkpoint_transition.normalize_transition_request(copy.deepcopy(body))
    except checkpoint_transition.CheckpointTransitionError as error:
        raise ValueError("the checkpoint transition request is invalid") from error
    return body


def normalized_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical view used only for validating the owner's answer."""

    return checkpoint_transition.normalize_transition_request(copy.deepcopy(dict(body)))


def validate_target(checkpoint_id: object, idempotency_key: object) -> None:
    """Refuse a malformed identifier or key BEFORE anything reaches the owner.

    The key is checked here because the shared writer falls back to a generated
    key when it is given an empty one: an explicit empty key must be refused,
    never silently replaced by one the caller never chose.
    """

    if type(checkpoint_id) is not str or CHECKPOINT_ID.fullmatch(checkpoint_id) is None:
        raise ValueError("the checkpoint identifier is invalid")
    if type(idempotency_key) is not str or IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise ValueError("the idempotency key is invalid")


def _valid_envelope(status: int, payload: Mapping[str, Any]) -> tuple[dict, dict]:
    """The exact data/meta envelope, with booleans kept distinct from numbers."""

    if not isinstance(payload, Mapping) or set(payload) != {"data", "meta"}:
        raise _unknown()
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, dict) or not isinstance(meta, dict):
        raise _unknown()
    if set(meta) != {"replayed"}:
        raise _unknown()
    replayed = meta["replayed"]
    # `type(...) is bool` so an integer 0 or 1 cannot pass as the flag.
    if type(replayed) is not bool:
        raise _unknown()
    if (status, replayed) not in ((201, False), (200, True)):
        raise _unknown()
    return data, meta


def _unknown() -> cli_writer.CommitUnknownError:
    """A malformed 2xx followed a request that DID reach the owner.

    The owner may already have recorded the transition, so this is reported as
    unknown rather than as a determinate refusal, and nothing here claims the
    write did not happen or attempts a rollback.
    """

    return cli_writer.CommitUnknownError(
        "checkpoint state commit is unknown; inspect the checkpoint before retrying"
    )


def _event_from(
    status: int,
    payload: Mapping[str, Any],
    checkpoint_id: str,
    workspace_uid: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the durable event against the frozen request and workspace."""

    data, meta = _valid_envelope(status, payload)
    # The field SET, not its order: the owner's idempotency ledger returns a
    # replayed body with its keys sorted, so requiring the original order would
    # reject the legitimate historical receipt.
    if set(data) != set(EVENT_FIELDS):
        raise _unknown()
    if data["checkpoint_id"] != checkpoint_id or data["workspace_uid"] != workspace_uid:
        raise _unknown()
    if data["origin"] != AGENT_CLIENT_VALUE:
        raise _unknown()
    if data["state"] != request["state"] or data["reason"] != request["reason"]:
        raise _unknown()
    revision = data["revision"]
    # The event carries the NEXT revision, and the policy's parity rule ties
    # that number to the state it records.
    if type(revision) is not int or revision != request["revision"] + 1:
        raise _unknown()
    if ("active" if revision % 2 == 0 else "superseded") != data["state"]:
        raise _unknown()
    _matches_policy(data)
    return {"data": data, "meta": dict(meta)}


def _matches_policy(data: Mapping[str, Any]) -> None:
    """Rebuild the event from its OWN received fields and require equality.

    The admitted policy validates every locator and transition domain and
    constructs the event type, so composing it here proves the received
    syntax and its internal binding without a second validator and without
    substituting an expected value for a bad one. It proves syntax only, not
    that a physical fact exists.
    """

    try:
        rebuilt = checkpoint_transition.build_transition_event({
            "workspace_uid": data["workspace_uid"],
            "checkpoint_id": data["checkpoint_id"],
            "locator": {
                "workspace_uid": data["workspace_uid"],
                "task_id": data["task_id"],
                "date": data["date"],
                "ordinal": data["ordinal"],
                "entry_digest": data["entry_digest"],
            },
            "transition": {
                "state": data["state"],
                "revision": data["revision"],
                "reason": data["reason"],
            },
            "origin": data["origin"],
        })
    except checkpoint_transition.CheckpointTransitionError as error:
        raise _unknown() from error
    if rebuilt != dict(data):
        raise _unknown()


def forward_checkpoint_state(
    store: object,
    owner_state: str,
    checkpoint_id: str,
    body: Mapping[str, Any],
    idempotency_key: str,
    *,
    coordinates_reader: cli_writer.CoordinatesReader,
    request_json: cli_writer.RequestJson,
) -> dict[str, Any]:
    """Send one transition and return the owner's data/meta envelope.

    The caller supplies the checkpoint identifier and the key as the original
    parsed strings. There is no generated key, no rebase, no automatic audit or
    Task read and no replay: one explicit invocation is one attempt, and an
    ambiguous outcome is reported as unknown rather than guessed.
    """

    validate_target(checkpoint_id, idempotency_key)
    request = normalized_request(body)
    workspace_uid = cli_writer.expected_workspace_uid(store)
    path = CHECKPOINT_TRANSITIONS.format(
        cli_writer.quote(checkpoint_id, safe="")
    )
    return cli_writer._forward_write(
        store,
        owner_state,
        path=path,
        body=dict(body),
        coordinates_reader=coordinates_reader,
        request_json=request_json,
        idempotency_key=idempotency_key,
        extra_headers={AGENT_CLIENT_HEADER: AGENT_CLIENT_VALUE},
        replay=False,
        project=lambda payload: {},
        project_result=lambda status, payload: _event_from(
            status, payload, checkpoint_id, workspace_uid, request
        ),
        changed_message=(
            "Work Stack server runtime metadata changed before the checkpoint"
            " state was sent"
        ),
        unknown_message=(
            "checkpoint state commit is unknown; inspect the checkpoint before"
            " retrying"
        ),
        refused_message=(
            "the running Work Stack server refused the checkpoint state (HTTP {})"
        ),
    )
