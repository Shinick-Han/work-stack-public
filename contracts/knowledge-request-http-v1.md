# Knowledge request HTTP v1 contract

Status: **owner issuer surface.** Three canonical loopback routes that read and
replace the owner connection policy and issue a KnowledgeRequest against it.
`workstack/knowledge_requests_http.py` owns the boundary,
`workstack/knowledge_request_issuer.py` owns the composition. Everything below
them — the ledger (`contracts/knowledge-owner-ledger-v1.md`) and the wire
(`contracts/knowledge-request-v1.md`) — was already released when this slice
landed, and this surface was composed by calling those modules as they stood
rather than by editing them. That records how the slice was built; it is not a
standing promise that those modules never change. What this contract requires of
them is the *behaviour* named below, which a later slice must keep.

**Nothing on this surface submits a request or authenticates an adapter.** No
route here contacts a provider, resolves a connection alias to an endpoint,
imports an answer or authorises a connector token; this surface issues the
*asking* half only.

**The answering half has since landed, as a manual import.** The retrieval
answer is carried out of band by the user and imported through
`POST /api/v1/knowledge/captures/import`
(`contracts/knowledge-capture-import-v1.md`,
`workstack/knowledge_captures_http.py` over
`workstack/knowledge_capture_import.py`), under the same owner browser session
this surface requires. That is still the manual mode of
`capture-retrieval-v1.1.md` only: there remains no automated adapter, no adapter
credential and no network call anywhere in the pair. The earlier statement that
no Capture importer existed described this document's own slice and is no longer
the state of the code.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/knowledge/connections` | the stored nonsecret owner policy and its revision |
| `POST` | `/api/v1/knowledge/connections` | replace that policy, under a compare-and-set on the revision |
| `POST` | `/api/v1/knowledge/requests` | issue one scoped KnowledgeRequest |

These exact spellings are the routes. A target that only *resolves* to one of
them — `.../connections;x`, which `urlparse` strips a trailing `;params` run
from — is answered `not_found` before any Store transaction is opened.

## Admission

Unchanged from the released POST surface, in the released order: bounded body
drained, then browser authorization, then route resolution, then the handler.

- **Both writes require the browser owner session.** `Host` must be the
  loopback server, `Origin` must be same-origin, and `X-WorkStack-CSRF` must
  equal the session token. A cross-origin page reaches neither route.
- **A Capture/Agent bearer token alone authorises neither.** That token
  authorises Capture ingestion. It is not an owner session, so a request
  carrying only `Authorization: Bearer …` is refused `origin_required` (403)
  and performs no policy or ledger write.
- **The read follows the released read model**: loopback `Host` only, as every
  other `GET /api/v1/…` projection does. It exposes no secret — see below.
- **No `Idempotency-Key`.** Both routes refuse one with
  `unsupported_idempotency_key`, and neither is a member of
  `IDEMPOTENT_POST_ROUTES`. That mechanism stores the whole response body in
  `activity.json`, and an issued response contains the user's query.
  Idempotence comes from `intent_id` instead.
- **Body bound**: 16 KiB of UTF-8 for any `/api/v1/knowledge/` POST, replacing
  the 1 MiB default for these routes. The Capture 64 KiB budget is neither
  widened nor borrowed.
- **Same-OS-user isolation is out of scope**, exactly as it is for every other
  loopback route: a process running as this user with the runtime files is
  inside the existing trust model, not outside it.

## Backends

The ledger is `knowledge.json`, a collection-store document at schema 6. The
experimental v4 application (`workstack.storage`) composes a different store
adapter that has no such document. All three routes refuse it explicitly with
`knowledge_backend_unsupported` (409) rather than improvising a second writer.

## `GET /api/v1/knowledge/connections`

No query string is accepted (`invalid_query`).

```json
{
  "data": {
    "policy_revision": 1,
    "connections": [
      {
        "alias": "team-nas",
        "upstream_workspace_uid": "66666666-6666-4666-8666-666666666666",
        "corpus_refs": ["nas-team-share", "notion-product"],
        "scope": "workspace"
      }
    ]
  },
  "meta": {
    "occupancy": {
      "request_count": 0,
      "request_bound": 200,
      "encoded_bytes": 218,
      "byte_bound": 262144
    }
  }
}
```

The four connection fields are projected **by name**, so the read cannot widen
if the stored schema ever gains a field. There is no endpoint, credential,
token, query, path, digest or request record in this answer, and the alias
grammar `[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?` cannot express a scheme, a host,
a share, a drive letter, a path separator or a `user:secret@` pair.

`data` stays the closed two-field policy object. Successful reads add
`meta.occupancy` projected from **that same loaded document**, with no second
Store load:

| Field | Source |
| --- | --- |
| `request_count` | `len(document["requests"])` — pending, completed and expired-unused records still held |
| `request_bound` | `MAX_REQUESTS` (200) |
| `encoded_bytes` | `len(compact_bytes(document))`, including the connection roster |
| `byte_bound` | `MAX_KNOWLEDGE_BYTES` (262144) |

All four values are finite JSON-safe integers: counts and bytes `>= 0`, bounds
`> 0`, and each observed value is `<=` its bound. Occupancy is not the
on-disk pretty JSON size, not a stored field, and not a new route. A client
that ignores unknown envelope keys continues to read `data` exactly as before.

The `encoded_bytes` figure in the example is the compact encoding of the
admitted ledger that produced this `data` (version, revision, the one
connection, empty `requests`). It is illustrative of the formula, not a second
authority.

## `POST /api/v1/knowledge/connections`

```json
{
  "expected_policy_revision": 1,
  "connections": [
    {
      "alias": "team-nas",
      "upstream_workspace_uid": "66666666-6666-4666-8666-666666666666",
      "corpus_refs": ["nas-team-share", "notion-product"]
    }
  ]
}
```

The body is **closed**: an unknown key anywhere, and a missing required key
anywhere, is a refusal. Occupancy is never a caller field and is never sent
in this body.

- `expected_policy_revision` is a compare-and-set guard. The read, the
  comparison and the released `set_owner_connection_policy` write all happen
  inside one Store transaction, so two owners replacing the policy at once
  cannot both commit: the loser refuses `policy_revision_changed` (409) and
  writes nothing.
- `scope` is **not** a caller field. The ledger has exactly one legal scope, so
  the server supplies `workspace` and a body cannot spell a project-level
  grant.
- The response is the same projection `GET` returns, at the new revision,
  including `meta.occupancy` taken from the **committed planned document**
  rather than a second Store load. A successful POST and the GET that follows
  it therefore carry the same `data` and the same occupancy.

Replacing the roster advances `policy_revision` by one. Outstanding requests
keep the revision they were issued under, which is how their authority is
invalidated — that rule belongs to the ledger contract and is unchanged here.

Internal Python `read_connection_policy` and `replace_connection_policy` still
return only the two-field policy object. Occupancy of the same snapshot is
available from the optional receipt helpers
`read_connection_policy_receipt` / `replace_connection_policy_receipt` and the
pure projector `project_knowledge_occupancy`. Refusals, admission, status
mapping and the single policy mutation are unchanged. There is no occupancy
endpoint.

## `POST /api/v1/knowledge/requests`

```json
{
  "intent_id": "11111111-1111-4111-8111-111111111111",
  "connection_alias": "team-nas",
  "binding": {
    "workspace_uid": "210aefb5-edf0-4841-af90-ffb5f778a255",
    "task_uid": "e0269290-a622-5ced-ab57-6d2f716a72bd",
    "task_id": "T-0001",
    "task_revision": 0
  },
  "query": "rollback verification owner",
  "corpus_refs": ["nas-team-share"],
  "purpose": "find_context",
  "result_limit": 3
}
```

```json
{
  "data": {
    "schema": "workstack.knowledge-request.v1",
    "request_id": "2cce8f7c-7961-5614-bc6f-ca4a093661e2",
    "binding": {
      "workspace_uid": "210aefb5-edf0-4841-af90-ffb5f778a255",
      "task_uid": "e0269290-a622-5ced-ab57-6d2f716a72bd",
      "task_id": "T-0001",
      "task_revision": 0
    },
    "purpose": "find_context",
    "query": "rollback verification owner",
    "corpus_refs": ["nas-team-share"],
    "result_limit": 3,
    "requested_at": "2026-09-08T09:32:07Z",
    "expires_at": "2026-09-08T09:37:07Z"
  },
  "meta": {
    "replayed": false,
    "state": "pending",
    "connection_alias": "team-nas",
    "policy_revision": 1
  }
}
```

`data` **is** the KnowledgeRequest v1 wire document. The ledger hashes the
canonical validated projection using sorted-key compact UTF-8, not the raw
response bytes. A client should copy this response for the out-of-band exchange
rather than rebuild it: a change that survives validation changes request identity,
while JSON spelling or values that normalize to the same projection do not.

### What the server derives, and what the body may only assert

| Field | Source |
| --- | --- |
| `schema` | server constant |
| `request_id` | derived (below); **never** taken from the body |
| `requested_at` / `expires_at` | the server clock and exactly 300 s; **never** taken from the body |
| `binding.workspace_uid` | the Store's own workspace; the body's value is an *expected-state guard* compared against it |
| `binding.task_uid` / `task_revision` | the Task the Store actually holds, looked up by the body's `task_id`; the body's values are guards |
| `corpus_refs` | the body's, admitted only against the **stored connection policy**'s granted aliases |
| `purpose`, `query`, `result_limit` | the body's, judged by the released wire validator |

A stale or hostile guard refuses (`workspace_mismatch`,
`task_binding_mismatch`, `task_binding_required`, `corpus_not_granted`); it
never steers the scope. The body has no `request_id`, `requested_at`,
`expires_at`, `schema`, `provider`, `tools`, `connection` or `authority` field,
so none of those can be asserted at all.

The Task binding carries identity only. **No Task title, body, note or detail
is attached** — the retrieval side receives the caller's query and nothing
else.

### Identity: `request_id`

```
request_id = uuidv5(REQUEST_INTENT_NAMESPACE, "<actual workspace_uid>:<intent_id>")
REQUEST_INTENT_NAMESPACE = 92a31ef4-d870-5657-a390-cbd452f0205b
```

The namespace is the fixed application constant
`uuid5(NAMESPACE_URL, "https://work-stack.invalid/knowledge-request/v1/intent")`,
written out in `workstack/knowledge_request_issuer.py` so the value is
auditable on its own. The *actual* workspace identity is one half of the name,
so the same intent replayed against a different workspace derives a different
identity instead of colliding with an existing record.

### Idempotence, without the query ever being stored

The ledger stores a digest, never a query, so a retry cannot be recognised by
re-reading what was asked. Instead:

1. the same `intent_id` reconstructs the same `request_id`;
2. if that identity already has a ledger record, the **original stored**
   `requested_at`/`expires_at` are read back out of it and the request is
   rebuilt on them — *before* any digest is compared;
3. the released `plan_request_issue` then compares digests.

So a retry that rebuilds the same canonical validated request matches and returns
the original receipt with `meta.replayed = true` and the ledger unchanged.
A body change that survives validation changes that digest and refuses
`request_digest_mismatch` (409). JSON key order, normalized query whitespace or
normalized Task-ID case alone do not change it. The recorded connection alias is
also checked separately; another alias refuses even if the request digest matches.
One identifier names exactly one authorization. A restart changes nothing — the document on disk is the only
state, so the same intent still reconstructs the original receipt.

**Expiry is terminal and is not a new identity.** Retrying an intent whose
window has closed rebuilds the request on its *original* window, so the
released validator refuses `request_expired` (409). Nothing renews the window,
and no second `request_id` is minted for that intent — a lapsed request must be
raised again under a *new* intent the user explicitly reviews.

**A completed request is recognised, not reissued.** Its record replays with
`meta.state = "completed"`, so a caller is told it is holding a receipt for
work the ledger has already accounted for. No ledger write occurs.

### Concurrency

The whole issue decision — read the ledger, derive the identity, recover the
original window, rebuild, validate, plan, commit — happens inside one outer
Store transaction. Two concurrent issues of the same intent therefore
serialise: the second observes the first's record and replays it. Exactly one
authorization exists, and exactly one record is written.

## Refusals

The envelope is the released one:
`{"error": {"code": …, "message": …, "details": {"field": …}}}`.

The `code` is a closed `KnowledgeRequestError` or `KnowledgeLedgerError` code
from the two contracts below, or one of `not_found`, `invalid_query`,
`unsupported_idempotency_key`, `knowledge_backend_unsupported` and the released
admission codes (`invalid_host`, `origin_required`, `invalid_origin`,
`invalid_csrf`, `invalid_authorization`, `unsupported_media_type`,
`length_required`, `body_too_large`, `invalid_json`, `invalid_body`).

`message` is a **constant** per surface — `"the knowledge request was refused"`
or `"the knowledge connection policy request was refused"` — and `details`
carries at most `field`, the *name* of a field in a closed schema. No query
text, alias, identifier, timestamp, token, path or digest is ever echoed into a
refusal.

Status mapping: these codes are conflicts (409) because the body was well
formed and the state had moved —

`ledger_full`, `policy_revision_changed`, `request_digest_mismatch`,
`request_expired`, `task_binding_mismatch`, `task_binding_required`,
`unknown_task`, `workspace_mismatch`.

Every other closed code is 400.

## What this surface does not do

- **No Capture import on this surface.** `service_captures.py` and
  `capture_retrieval.py` are untouched *by these routes*; none of them stages,
  imports or completes evidence. The ledger's stage-completion operation is
  reachable only from the separate import route
  (`contracts/knowledge-capture-import-v1.md`), never from the three routes
  declared here.
- **No adapter authentication.** No connector credential is issued, accepted,
  stored or verified. A `provider` or tool name has nowhere to be written and
  would authorise nothing if it did.
- **No provider contact and no alias resolution.** An alias stays a label.
- **No Task change.** Tasks are read for their identity and revision only; no
  route here writes a Task, and no Task text is added to a request.
- **No user SSOT upgrade.** These routes upgrade nothing; the server's existing
  startup owns store schema upgrades.
- **No audit event yet — explicitly pending.** A content-free
  `knowledge.requested` / `knowledge.rejected` activity event is *not* written.
  The existing safe event path appends to `activity.json`, and composing that
  atomically with `knowledge.json` requires a single `save_many` over both
  documents — which the released `issue_owner_knowledge_request` does not
  offer, and forking it into a private re-implementation is exactly what this
  lane must not do. Writing the event in a second `save_many` would be a
  separate commit, so a crash between them would record an issue that did not
  happen or hide one that did. The ledger's digest record is therefore the only
  durable evidence in this slice, and the audit event is carried as declared
  pending work rather than added as a hidden document.

## Reused, not re-implemented

These are the released seams this surface calls rather than re-implements. Each
was reused as released, without an edit, when the slice landed; what the surface
depends on is the behaviour stated beside it.

- `workstack.knowledge_request.validate_knowledge_request` and its
  `RequestAuthority` — the wire rules, whose codes propagate unchanged.
- `workstack.knowledge_owner_requests.issue_owner_knowledge_request`,
  `set_owner_connection_policy`, `request_digest`, `ConnectionPolicy` — the
  owner ledger operations, which decide the record and its replay.
- The released admission chain (`Host`, `Origin`, CSRF, content type,
  `Content-Length`, body bound) and the released error envelope.
- No new dependency, no new schema framework, no new store document and no
  change to any threshold or baseline.
