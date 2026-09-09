# Knowledge owner ledger v1 contract

Status: **storage and owner-operation foundation.** `knowledge.json` is the eleventh
collection-store document and the whole of schema 6.
`workstack/knowledge_ledger_document.py` owns its shape,
`workstack/knowledge_owner_requests.py` owns the owner operations over it, and
`workstack/store_knowledge_migration.py` owns the v5-to-v6 upgrade. This document
defines what the ledger *is* and what may be done to it from inside the process; it does
not define who may reach it over a socket.

**Every operation this contract names is a trusted internal function, and that has not
changed.** Each takes owner inputs from inside the process; none of them is itself a
remote boundary, and none may be handed an authority object built from an untrusted
request body.

**The authenticated HTTP boundaries above this ledger have since landed, and are
contracted separately.** `workstack/knowledge_requests_http.py` with
`workstack/knowledge_request_issuer.py` serve the owner issuer routes
(`contracts/knowledge-request-http-v1.md`), and `workstack/knowledge_captures_http.py`
with `workstack/knowledge_capture_import.py` serve the manual Capture import route
(`contracts/knowledge-capture-import-v1.md`). The two issuer POST routes and the
import POST prove a loopback `Host`, same-origin `Origin` and the session CSRF token
before a handler runs. The policy GET requires only the loopback `Host`. The owner
operations derive grants and workspace/Task authority from stored policy and actual
Store state; a request may select a registered connection alias, but cannot supply
its own authority object. The earlier statement that no HTTP issuer and no
Capture importer existed described this document's own foundation slice; it is no longer
the state of the code, and nothing here should be read as saying that surface is missing
from production.

The asking half of the exchange this ledger records is `knowledge-request-v1.md`; the
answering half is `capture-retrieval-v1.1.md`. This document adds the third thing those
two say a host must have and deliberately do not provide: *a host-issued ledger that
issues, records and retires a `request_id` and refuses a replay*
(`knowledge-request-v1.md`, caller obligation 6).

## Where it lives

`knowledge.json` is a roster member of collection schema 6, so it is created, admitted,
journalled, backed up and recovered by the same `Store` machinery as every other
document. It is not a separate database, not a settings file and not an activity detail:

- a fresh store writes the empty ledger under `store-initialize-v6`;
- a v5 (or v1/v2/v3) store is upgraded through one journalled `save_many` after a
  verified pre-upgrade rollback archive of the *detected* bytes;
- `store-meta.json` gains a fourth evidence record, `knowledge`, naming the version the
  upgrade actually detected;
- an interrupted upgrade recovers to one coherent old or new generation, never a mix.

Historical rosters are untouched. `V1`/`V2`/`V3`/`V5_DOCUMENT_NAMES` still describe their
own stores, and a genuine v5 directory is still admitted as v5 by
`validate_document_values(values, schema_version=5)`.

## Document shape

The object is **closed at every level**. An unknown key anywhere, and a missing required
key anywhere, is a refusal. Nothing is defaulted and nothing is coerced.

```json
{
  "version": 1,
  "policy_revision": 3,
  "connections": [
    {
      "alias": "team-nas",
      "upstream_workspace_uid": "66666666-6666-4666-8666-666666666666",
      "corpus_refs": ["nas-team-share", "notion-product"],
      "scope": "workspace"
    }
  ],
  "requests": [
    {
      "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      "request_digest": "sha256:…",
      "connection_alias": "team-nas",
      "binding": {"workspace_uid": "…", "task_uid": "…", "task_id": "T-0033", "task_revision": 2},
      "corpus_refs": ["nas-team-share"],
      "policy_revision": 3,
      "result_limit": 5,
      "requested_at": "2026-09-08T09:00:00Z",
      "expires_at": "2026-09-08T09:05:00Z",
      "state": "pending",
      "capture_ids": [],
      "completion_digest": null,
      "completed_at": null
    }
  ]
}
```

The example is illustrative of the shape only. The contract is the rules below and the
code that enforces them, not this document's sample values.

### Top level

| Field | Type | Rule |
| --- | --- | --- |
| `version` | integer | exactly `1` |
| `policy_revision` | integer | `0 .. 2^53-1`; incremented by exactly one on every policy change |
| `connections` | array | `0 .. 8` connection policies, aliases unique |
| `requests` | array | `0 .. 200` request records, `request_id` unique |

The whole document is bounded at 256 KiB of compact UTF-8.

### Connection policy

| Field | Type | Rule |
| --- | --- | --- |
| `alias` | string | the corpus-alias grammar `[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?`, at most 64 characters |
| `upstream_workspace_uid` | string | canonical lowercase non-nil UUID: the workspace the corpora are expected to live in |
| `corpus_refs` | array of string | `1 .. 8` unique aliases, same grammar |
| `scope` | string | exactly `workspace` |

**No endpoint, no credential, no query, no path, no arbitrary collection authority.**
The alias grammar cannot express a scheme, a host, a share, a drive letter, a path
separator or a `user:secret@` pair, and the closed key set leaves nowhere to put one.
Resolving an alias to an actual endpoint is a later connector's job and is not stored
here.

**Workspace-wide, user-owned corpora only.** `scope` has exactly one legal value. This
slice explicitly has no project-level ACL: a project grant would need its own subject and
its own schema, so it is a schema change rather than a value.

### Request record

| Field | Type | Rule |
| --- | --- | --- |
| `request_id` | string | canonical lowercase non-nil UUID, unique within the document |
| `request_digest` | string | `sha256:<64 hex>` over the validator's own closed projection of the issued KnowledgeRequest |
| `connection_alias` | string | alias grammar |
| `binding` | object | `{workspace_uid}` or the full `{workspace_uid, task_uid, task_id, task_revision}`; `workspace_uid` must equal the store's own workspace |
| `corpus_refs` | array of string | `1 .. 8` unique aliases |
| `policy_revision` | integer | `0 .. policy_revision` of the document |
| `result_limit` | integer | `1 .. 10` |
| `requested_at` / `expires_at` | string | strict RFC3339; `expires_at` strictly after `requested_at` and at most 300 seconds later |
| `state` | string | `pending` or `completed` |
| `capture_ids` | array of string | `0 .. result_limit` unique `C-\d{4,}` identifiers; empty while `pending` |
| `completion_digest` | string \| null | `sha256:<64 hex>` when `completed`, `null` while `pending` |
| `completed_at` | string \| null | RFC3339 when `completed`, `null` while `pending` |

**The query is never stored.** The closed key set has no place for it, and the digest is
what binds a record to the exact request that was issued. A stored document carrying a
`query` key is refused as `unknown_field`.

`task_id` is stored in the projected uppercase form the wire validator produces, so one
request has exactly one spelling in the ledger and a digest cannot fork on case.

Referential integrity is required of exactly the records that claim it. A request whose
stored `policy_revision` **equals** the document's must name a connection the current
`connections` roster still holds, and its `corpus_refs` must be a subset of that
connection's grants. A stored document violating either is refused as
`connection_not_found` or `corpus_not_granted`, so a record that no current policy
authorises is never admitted — and never survives a restart — as current authority.

A request issued under an **older** revision is admitted without that check, on purpose.
A policy change may retire a connection while a request issued under the old policy is
still on record; that request is already void as authority because its stored
`policy_revision` no longer matches, and deleting the record instead would erase the
evidence that it was ever issued. Such a record stays readable history, including a
completed record and its replay, and can never newly complete.

`request_authority_is_current(document, record) -> bool` is the single predicate for
this. Both `validate_knowledge_document` and the new-completion path in
`plan_ledger_stage_completion` ask it, and `PendingRequest.authority_current` reports it,
so "admitted as current" and "may still be completed" cannot drift apart.

## Owner operations

All of these are internal. Signatures are exact.

```python
plan_policy_revision(document, connections, *, workspace_uid) -> dict
owner_request_authority(document, *, workspace_uid, connection_alias, now,
                        active_task=None) -> RequestAuthority
plan_request_issue(document, request_document, *, workspace_uid, connection_alias, now,
                   active_task=None) -> IssuedRequest
plan_ledger_stage_completion(document, *, request_id, completion_digest, capture_ids,
                             workspace_uid, now, active_task=None) -> StageCompletion
pending_requests(document, *, workspace_uid, now=None) -> tuple[PendingRequest, ...]
request_digest(request) -> str

set_owner_connection_policy(store, connections) -> dict
issue_owner_knowledge_request(store, request_document, *, connection_alias, now,
                              task_id=None) -> IssuedRequest
owner_pending_requests(store, *, now=None) -> tuple[PendingRequest, ...]
```

Every `plan_*` function is pure: it takes the document the caller read and returns the
document the caller should write. None opens a path, reads a clock, allocates an identity
or saves anything.

### Policy

`plan_policy_revision` replaces the connection roster and advances `policy_revision` by
one. Outstanding requests keep their records and keep the revision they were issued
under, which is exactly how their authority is invalidated: a *new* completion planned
afterwards refuses with `policy_revision_changed`.

### Issue

`owner_request_authority` builds the wire `RequestAuthority` from the **stored policy**
(the granted corpus aliases of the named connection) plus the **real Store state** (the
workspace identity, and the Task the Store actually holds at the revision it actually
carries). The request body supplies the connection alias selector; the selected
connection supplies the grants. Workspace and Task authority come from the Store,
not caller-provided authority fields. The landed public issuer
(`workstack/knowledge_request_issuer.py`, `contracts/knowledge-request-http-v1.md`)
derives its authority the same way; it never accepts one.

`plan_request_issue` then hands the document to the released
`workstack.knowledge_request.validate_knowledge_request`, which this module calls rather
than re-implements. Scope expansion, a foreign
workspace, a stale Task revision, a five-minute-plus window and an expired request all
refuse with that contract's own `KnowledgeRequestError` codes, which propagate unchanged.

Only the digest of the resulting projection is persisted, alongside the binding, the
corpus refs, the policy revision, the timestamps, the requested limit and the state.

**Replay.** The identity is the digest, and the digest is *semantic canonical identity,
not byte equality of the submitted JSON*. `request_digest(request)` is `sha256` over
`compact_bytes` — sorted keys, compact separators, UTF-8 — of the closed projection
`validate_knowledge_request` returns, never over the caller's raw bytes. Reissuing the
same `request_id` with input that canonicalizes to the same projection returns the
original record's result with `replayed=True` and leaves the document alone; the stored
digest is compared with `secrets.compare_digest`. Two submissions differing only in JSON
key order, in insignificant whitespace, in `query`'s leading or trailing whitespace (the
validator strips it) or in `binding.task_id`'s letter case (the validator uppercases it)
are therefore the *same* issue, not a mismatch.

What does fork the digest is anything the projection carries differently: a different
`query` after stripping, `purpose`, `result_limit`, `request_id` or `binding`; a
different *order* of `corpus_refs`, which the projection preserves as submitted; or a
different *spelling* of `requested_at` / `expires_at`, which are projected as the
submitted strings — so `2026-09-08T09:00:00Z` and `2026-09-08T09:00:00+00:00` are
different issues even though they name the same instant. Each of these refuses with
`request_digest_mismatch`.

**A different connection is caught by its own check, not by the digest.**
`connection_alias` is not part of the projection and so is not in the digest; it reaches
the digest only indirectly, by gating which `corpus_refs` the validator will admit.
Reissuing the same `request_id` under a *second* connection that grants the same aliases
therefore produces a **matching** digest — and `plan_request_issue` still refuses it,
with `request_digest_mismatch` reporting `details.field` as `connection_alias` rather
than `request_id`. One identifier names exactly one authorization, under exactly one
connection.

**Restart.** The document on disk is the only state, so `owner_pending_requests` after a
process restart reconstructs every outstanding request, its policy revision, its limit,
whether the policy has moved since (`authority_current`) and, when the caller supplies a
clock, whether it has lapsed (`expired`).

**Clocks.** Every entry point takes `now` explicitly, so expiry is reproducible in a test.
Production callers pass a UTC RFC3339 instant; this layer never reads a wall clock.

### Stage completion

`plan_ledger_stage_completion` is the **internal** operation the landed Capture importer
(`workstack/knowledge_capture_import.py`, `contracts/knowledge-capture-import-v1.md`)
composes into its own Store transaction, after every Capture in the batch has been
staged, in the same `save_many` that persists them. It is called **once per request,
after the whole batch** — never once per Capture — and it never writes.

There is deliberately **no standalone externally callable consume**, and there must not
be one: a completion that commits separately from the evidence it completes is exactly
the split this design prevents. `workstack.knowledge_owner_requests.__all__` contains no
`consume` entry point.

Order of decision:

1. **Replay first.** A record already `completed` with the same `completion_digest` and
   the same `capture_ids` returns the recorded result with `replayed=True` — including
   after the request's window has closed, so an importer retried late still recognises
   its own completed work. A different digest refuses with `completion_digest_mismatch`;
   the same digest with different captures refuses with `completion_replay_mismatch`.
2. **A new completion is held to the current state.** The batch may not exceed the issued
   `result_limit` (`result_limit_exceeded`); the record's `policy_revision` must still be
   the document's (`policy_revision_changed`); the binding's workspace must be this store
   (`workspace_mismatch`); the Task binding must match the Task the caller actually holds,
   in both directions (`task_binding_mismatch`); and the request must not have expired
   (`request_expired`).
3. Only then does the record become `completed`, storing the bounded capture identifiers,
   the completion digest and `completed_at`.

Because planning is pure, an importer that fails anywhere — before, during or after the
plan — simply never saves, and the request is still pending on the next attempt. Two
competing importers settle on exactly one completion: the second one re-reads the
document inside its own transaction, and identical work replays while different work is
refused.

## Refusals

`KnowledgeLedgerError` carries a closed `code` and at most `details.field` — the *name* of
a field in this closed schema. **No submitted value, query text, request body, alias,
identifier, digest, timestamp, path or credential is ever echoed into a diagnostic**, and
the same holds for the `StoreCorruptError` the store raises, which is
`knowledge.json schema is invalid: <code>`.

`invalid_document`, `unknown_field`, `missing_field`, `unsupported_version`,
`document_too_large`, `invalid_number`, `out_of_range`, `invalid_uuid`, `invalid_alias`,
`invalid_corpus_refs`, `duplicate_corpus_ref`, `invalid_digest`, `invalid_timestamp`,
`invalid_task_binding`, `workspace_mismatch`, `invalid_scope`, `duplicate_connection`,
`duplicate_request`, `invalid_request_window`, `invalid_state`, `invalid_capture_ids`,
`duplicate_capture_id`, `connection_not_found`, `corpus_not_granted`,
`request_digest_mismatch`, `ledger_full`,
`unknown_request`, `unknown_task`, `completion_digest_mismatch`,
`completion_replay_mismatch`, `result_limit_exceeded`, `policy_revision_changed`,
`task_binding_mismatch`, `request_expired`.

## What this slice does not do

- **No HTTP in this layer.** Nothing in `knowledge_owner_requests.py` constructs a
  route, reads a header or accepts a public authority object; every function here is
  called with owner inputs from inside the process. The routes that do reach this ledger
  belong to the landed owner issuer (`contracts/knowledge-request-http-v1.md`) and the
  landed manual Capture import (`contracts/knowledge-capture-import-v1.md`), each of
  which derives its authority from this stored policy plus the actual Store workspace
  and Task.
- **No Capture import in this layer.** `service_captures.py` and the retrieval
  validators are untouched *by this module*, and `plan_ledger_stage_completion` stays
  pure and still never writes. The importer that composes it is
  `workstack/knowledge_capture_import.py`, which commits the ledger transition together
  with its captures and activity in one `save_many`.
- **No connector, client or resolver.** An alias is a label. Nothing here resolves it to
  an endpoint, opens a connection or presents a credential. A `provider` or tool name is
  not authority and has nowhere to be written.
- **No v4 composition.** `workstack.ssot` (schema 4) is a different backend and remains
  explicitly unsupported here. No new backend is introduced.

## Versioning

- `knowledge.json` carries its own `version`, currently `1`. A newer document version is
  refused as `unsupported_version` rather than partially read.
- Collection schema 6 is the v5 roster plus this one document. A directory holding the
  eleven v6 names is judged as v6; one holding the ten v5 names is still judged as v5; a
  store whose metadata claims a version newer than this build refuses with the released
  "store schema is newer than this Work Stack build".
- `workstack.store_report_migration.plan_upgrade` is unchanged and still produces exactly
  the v5 roster: it is the historical record of the v3-to-v5 step.
  `workstack.store_knowledge_migration.plan_upgrade` owns the step after it and composes
  the two for a v1, v2 or v3 source.
