"""Canonical, immutable evidence for one remote provisioning inspection.

A dataclass being frozen freezes only the field slots.  A ``facts`` or
``plan`` dictionary handed out through one stays fully editable, so a caller
holding a refused inspection can rewrite the decision it recorded and hand the
same object back.  This module removes that: what an inspection carries is a
deep read-only view of what was read, plus one digest over all of it.

The digest is integrity *inside this API*: it says that the facts and plan a
caller returned are the ones the inspection was issued under, and that this
process issued them at all.  It is not authentication.  Python running as the
same user can reach anything this process can reach, and no secret, signature,
or distributed proof is introduced here or implied by it.  The digest exists
so that an ordinary caller mistake -- a retained document edited, replayed, or
built by hand -- refuses instead of quietly authorising work.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from types import MappingProxyType

DIGEST_PREFIX = "sha256:"

#: Bound on one canonical evidence document.
MAX_EVIDENCE_BYTES = 65536

#: Evidence digests this process actually issued.  Bounded on purpose: an
#: evicted inspection refuses, and refusing is always the safe direction.
ISSUED_EVIDENCE_LIMIT = 32
_ISSUED_EVIDENCE: deque[str] = deque(maxlen=ISSUED_EVIDENCE_LIMIT)


INVALID = "DRIVER_EVIDENCE_INVALID"
MISMATCH = "DRIVER_EVIDENCE_MISMATCH"
UNISSUED = "DRIVER_EVIDENCE_UNISSUED"


class EvidenceError(RuntimeError):
    """Evidence that is malformed, edited since issue, or never issued here."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def freeze(value: object) -> object:
    """Deep-copy one JSON value into a structure nothing can edit in place."""

    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise EvidenceError(INVALID, "evidence keys must be strings")
            frozen[key] = freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise EvidenceError(INVALID, "evidence holds a value that is not JSON")


def plain(value: object) -> object:
    """Rebuild an ordinary JSON structure from a frozen one, for output only."""

    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


def freeze_document(value: object) -> Mapping[str, object]:
    """Freeze one JSON object, or refuse it as evidence."""

    if not isinstance(value, Mapping):
        raise EvidenceError(INVALID, "inspection evidence must be a JSON object")
    try:
        frozen = freeze(value)
    except RecursionError:
        raise EvidenceError(INVALID, "inspection evidence is not a bounded document") from None
    if not isinstance(frozen, Mapping):
        raise EvidenceError(INVALID, "inspection evidence must be a JSON object")
    return frozen


def compute_evidence(binding: str, facts: object, plan: object) -> str:
    """Digest a binding together with every fact and plan field it was read from.

    A binding digest covers what an operator chose -- the target, the owner,
    the artifact.  This covers what the decision was actually read from, so a
    retained document that is edited afterwards no longer matches the
    inspection it came from.
    """

    try:
        canonical = json.dumps(
            {"binding": binding, "facts": plain(facts), "plan": plain(plan)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise EvidenceError(INVALID, "inspection evidence is not a canonical document") from None
    if len(canonical) > MAX_EVIDENCE_BYTES:
        raise EvidenceError(INVALID, "inspection evidence exceeds the bounded size")
    return DIGEST_PREFIX + hashlib.sha256(canonical).hexdigest()


def issue(
    binding: str, facts: object, plan: object
) -> tuple[Mapping[str, object], Mapping[str, object], str]:
    """Freeze one live reading and record that this process issued exactly it."""

    frozen_facts = freeze_document(facts)
    frozen_plan = freeze_document(plan)
    evidence = compute_evidence(binding, frozen_facts, frozen_plan)
    if evidence not in _ISSUED_EVIDENCE:
        _ISSUED_EVIDENCE.append(evidence)
    return frozen_facts, frozen_plan, evidence


def require_issued(binding: str, facts: object, plan: object, evidence: object) -> None:
    """Refuse evidence this process did not issue, or that was edited since.

    Order matters: a document that no longer digests to the evidence it claims
    is a mismatch, not merely something unknown, and either way it is refused
    before it can stand in for a reading of the target.
    """

    if not isinstance(evidence, str) or not evidence:
        raise EvidenceError(UNISSUED, "this inspection carries no evidence")
    if compute_evidence(binding, facts, plan) != evidence:
        raise EvidenceError(
            MISMATCH,
            "the retained facts or plan are not the ones this inspection was issued under",
        )
    if evidence not in _ISSUED_EVIDENCE:
        raise EvidenceError(
            UNISSUED, "this inspection was not issued by inspect_remote_target"
        )


__all__ = [
    "DIGEST_PREFIX", "EvidenceError", "INVALID", "ISSUED_EVIDENCE_LIMIT",
    "MAX_EVIDENCE_BYTES", "MISMATCH", "UNISSUED", "compute_evidence", "freeze",
    "freeze_document", "issue", "plain", "require_issued",
]
