# Knowledge attempt guard v1 contract

Status: **library only.** `workstack/knowledge_attempt_guard.py` is implemented and
tested; nothing in production constructs a `KnowledgeAttemptGuard` yet. The issuer that
will call `register_new`, the route that will call `consume`, and the bounded child
process that will execute the attempt are separate later slices. A build that contains
this module has *not* gained automatic execution, and must not be described as having a
completed adapter.

This document defines what the guard is, what its two operations mean, and — more
importantly — what a caller must already have done before it is allowed to consume.

## What it is

One owner incarnation's ready/attempted set, held in memory by the process that issued
the requests. It exists so that a KnowledgeRequest *this* owner incarnation issued can be
executed automatically at most once.

It is a **trusted internal helper, not an authentication boundary.** It authenticates
nobody, mints nothing, and proves nothing about the identity it is handed. It must never
be reached from an untrusted request body, and it must not be widened into a remote
surface: its refusals are not authorization answers.

## Frozen API

```
KnowledgeAttemptGuard()                                       -> instance
register_new(request_id: str, request_digest: str)            -> None
consume(request_id: str, request_digest: str)                 -> None
```

Refusals are `KnowledgeAttemptError`, carrying a closed `code` and nothing else. No
submitted identifier, digest, count or field value is ever echoed, in the code, the
message or the exception's attributes.

Grammar is the released one, reused rather than restated:
`workstack.knowledge_request.canonical_uuid` admits `request_id`, and
`workstack.capture.SHA256_RE` (`sha256:` plus 64 lowercase hex) admits `request_digest`
— the same spellings `workstack.knowledge_owner_requests.request_digest` produces and the
owner ledger records. Both arguments are admitted **before** the lock is taken and before
any state changes, so a malformed argument mutates nothing.

## Codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| `invalid_request_id` | both | not the canonical, non-nil UUID text |
| `invalid_request_digest` | both | not `sha256:` + 64 lowercase hex |
| `request_digest_mismatch` | both | this identity is registered under a different digest |
| `attempt_capacity` | `register_new` | a *new* identity beyond the ledger's `MAX_REQUESTS` (200) |
| `request_not_registered` | `consume` | this instance never registered this identity |
| `request_already_attempted` | `consume` | this identity's one attempt is already spent |

## register_new

Called **only** by the issuer path, as a callback, after a request has been *newly
committed* in this owner — that is, a real issue with `replayed` false. The guard cannot
discover authority and never tries: it holds no Store, opens no path, reads no clock and
reaches no ledger.

* Re-presenting a known identity with **its own** digest is a no-op. This deliberately
  includes the case where that identity has already been attempted: a replayed issue must
  never rearm a spent attempt.
* Re-presenting a known identity with a **different** digest names a different request and
  refuses `request_digest_mismatch`; the existing binding survives.
* Capacity applies only to a *new* identity. At 200 held identities a new one refuses
  `attempt_capacity`, while a replay of one already held is still the no-op above.
* There is no eviction, no reset, no release and no method to clear an instance.
  Consuming does not free a slot.

## consume

`consume` returns only once the identity has moved from ready to attempted **under the
lock**. A caller that reaches the next statement owns the single attempt, and no
concurrent caller can also own it.

**Caller obligations — all of them, before consuming.** The guard checks none of these:

1. The request is in the current stored ledger and its state is still `pending`.
2. Its policy revision is the one in force and its connection still grants its corpora
   (`request_authority_is_current`).
3. Its workspace and Task binding still match current Store state.
4. It has not expired.
5. The `request_digest` passed here is the one the ledger holds for that record.

A `request_digest_mismatch` refuses **without** spending the attempt: a digest that does
not match names a different request, so there is nothing of this one's to spend. Every
other consume outcome is terminal for the identity.

**After a successful consume there is no automatic retry, ever — including a failure the
caller can prove never reached the upstream.** The attempt is spent. An outcome this owner
cannot describe is a question for the operator, not a re-execution.

## Bounds

* At most 200 unique identities per instance, reusing
  `workstack.knowledge_ledger_document.MAX_REQUESTS` so the guard can never outgrow the
  document the caller validates against.
* State per identity is exactly `(request_digest, attempted)`. No query text, no
  timestamp, no ledger record, no process handle, no counter of failures.
* No module-level mutable state. Separate instances share nothing, so a new owner
  incarnation starts empty and every request from a previous incarnation is
  `request_not_registered`.
* No IO of any kind: no Store, no filesystem, no socket, no subprocess, no clock.
* One `threading.Lock` covers each `register_new` and each `consume` atomically.

## What this is not

Not exactly-once against a non-idempotent upstream. Not crash resume — a restart drops
the whole set by design, and every pre-existing request becomes ineligible for automatic
execution while manual import of it continues to work unchanged. Not deduplication of the
upstream itself: a second request issued through the manual tools is invisible here. Not
a completed adapter.

The honest guarantee is exactly one sentence: **at most one automatic attempt per
registered request per owner incarnation.**
