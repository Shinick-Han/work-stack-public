"""At most one automatic attempt per request, per owner incarnation.

This is the bounded pilot the R14 execution contract chose: a guard object that
one owner process holds in memory, so that a KnowledgeRequest this incarnation
issued can be executed automatically at most once, and never again -- not after
a failure, not after a restart, not after a retry someone adds later.

**Trusted internal helper, not an authentication boundary.** Nothing here proves
anything. It does not know whether ``request_id`` names an owner-issued request,
whether that request is still open, whether its policy revision is still in
force, whether its Task binding still matches, or whether it has expired. Those
questions belong to the owner ledger, and the caller must have answered them --
against current Store state -- *before* it consumes. A guard that says "not yet
attempted" is saying only that: this instance has not spent this identity yet.

**Registration is a callback, not a discovery.** :meth:`register_new` is called
by the issuer path after a request has been newly committed in this owner, with
``replayed`` false. The guard cannot reach the ledger and never tries: a request
it was not told about is simply unknown, which is exactly what makes every
request from a previous owner incarnation ineligible. A replayed issue is not a
new registration, so an existing identity re-presented with its own digest is a
no-op that leaves an already-spent attempt spent.

**Attempted is one-way.** :meth:`consume` moves an identity from ready to
attempted under the lock and returns; there is no release, no reset, no eviction
and no way to clear an instance. If the caller then fails -- including before it
sends anything upstream -- the attempt stays spent. That is the point: an
outcome this owner cannot describe is a question for the operator, never an
automatic re-execution.

**What this is not.** It is not exactly-once against a non-idempotent upstream,
not crash resume, not deduplication of the upstream itself, and not a finished
adapter. A second upstream request issued through the manual tools is invisible
here, and the owner's stored ledger is untouched by anything in this module.

Refusals are :class:`KnowledgeAttemptError` carrying a closed code and nothing
else. No submitted identifier, digest or count is ever echoed. The grammar is
the released one: :func:`workstack.knowledge_request.canonical_uuid` for the
identity and :data:`workstack.capture.SHA256_RE` for the digest, so an argument
this guard accepts is spelled exactly the way the ledger spells it.
"""

from __future__ import annotations

import secrets
import threading
from typing import Any, Final

from .capture import SHA256_RE
from .knowledge_ledger_document import MAX_REQUESTS
from .knowledge_request import KnowledgeRequestError, canonical_uuid

__all__ = ("KnowledgeAttemptError", "KnowledgeAttemptGuard")

# One instance may hold at most as many identities as the owner ledger itself
# may hold records. The bound is the ledger's, reused rather than restated, so
# the guard can never outgrow the document the caller validates against. There
# is no eviction: reaching it is a refusal, not a reason to forget an attempt.
_MAX_REGISTRATIONS: Final[int] = MAX_REQUESTS


class KnowledgeAttemptError(ValueError):
    """Closed refusal: one code, and no submitted value of any kind."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _admitted_request_id(value: Any) -> str:
    """The released canonical UUID grammar, reported as this guard's own code."""

    try:
        return canonical_uuid(value, "request_id")
    except KnowledgeRequestError as error:
        raise KnowledgeAttemptError("invalid_request_id") from error


def _admitted_request_digest(value: Any) -> str:
    """The released ``sha256:<64 hex>`` digest grammar the ledger records."""

    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise KnowledgeAttemptError("invalid_request_digest")
    return value


class KnowledgeAttemptGuard:
    """One owner incarnation's ready/attempted set. Instances share nothing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # request_id -> (request_digest, attempted). Identities and digests
        # only: no query, no timestamp, no ledger record and no handle.
        self._entries: dict[str, tuple[str, bool]] = {}

    def register_new(self, request_id: str, request_digest: str) -> None:
        """Admit one identity this owner incarnation has just newly issued.

        Called by the issuer path after a *committed, not replayed* issue. Both
        arguments are admitted before the lock is taken, so a malformed one
        changes nothing. Re-presenting a known identity with its own digest is
        a no-op -- deliberately including the case where that identity has
        already been attempted, which a replayed issue must never rearm. A
        known identity presented with a different digest is a different request
        and is refused. The capacity bound applies only to a *new* identity.
        """

        identifier = _admitted_request_id(request_id)
        digest = _admitted_request_digest(request_digest)
        with self._lock:
            recorded = self._entries.get(identifier)
            if recorded is not None:
                if not secrets.compare_digest(recorded[0], digest):
                    raise KnowledgeAttemptError("request_digest_mismatch")
                return
            if len(self._entries) >= _MAX_REGISTRATIONS:
                raise KnowledgeAttemptError("attempt_capacity")
            self._entries[identifier] = (digest, False)

    def consume(self, request_id: str, request_digest: str) -> None:
        """Spend this identity's one automatic attempt, or refuse.

        Returns only when the transition from ready to attempted has already
        been made under the lock, so a caller that reaches the next line owns
        the single attempt and no concurrent caller can also own it. The caller
        must already have verified the request's current ledger authority,
        state, expiry, Task binding and digest against the Store; this guard
        re-checks none of that.

        A digest that does not match the registered one refuses *without*
        spending the attempt: it names a different request, so there is nothing
        of this one's to spend. Everything else is terminal for the identity --
        there is no release and no retry, and a failure after this call,
        including one that provably never reached the upstream, leaves the
        attempt spent.
        """

        identifier = _admitted_request_id(request_id)
        digest = _admitted_request_digest(request_digest)
        with self._lock:
            recorded = self._entries.get(identifier)
            if recorded is None:
                # Never registered here, or registered by a previous owner
                # incarnation, which is the same thing to this instance.
                raise KnowledgeAttemptError("request_not_registered")
            stored, attempted = recorded
            if not secrets.compare_digest(stored, digest):
                raise KnowledgeAttemptError("request_digest_mismatch")
            if attempted:
                raise KnowledgeAttemptError("request_already_attempted")
            self._entries[identifier] = (stored, True)
