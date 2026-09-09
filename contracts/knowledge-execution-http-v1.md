# Knowledge execution HTTP v1 contract

Status: **owner execution caller, pilot.** One canonical loopback route that
executes a KnowledgeRequest this owner already issued, through a driver the
operator pinned in this process's configuration, and returns the child's answer
as a **proposal for the user to review**. `workstack/knowledge_execution_http.py`
owns the boundary; `workstack/knowledge_execution_runtime.py` owns the sequence
and the operator registry.

Every step below it was built and reviewed separately and is called here as it
stands: the read-only admission
(`contracts/knowledge-execution-admission-v1.md`), the per-incarnation attempt
guard (`contracts/knowledge-attempt-guard-v1.md`), the bounded child transport
(`contracts/knowledge-driver-exchange-v1.md`) and the untrusted-output
projection (`workstack/knowledge_execution_proposal.py`). This slice adds the
order they run in and the HTTP surface that starts them.

**This route saves nothing.** A success writes no Capture, changes no Task,
touches no ledger record and caches no response. The user confirms the proposal
through the existing manual import route
(`POST /api/v1/knowledge/captures/import`,
`contracts/knowledge-capture-import-v1.md`), which is still the only writer, and
still under `capture-retrieval-v1.1.md` manual semantics: an imported item is
`knowledge.answer` with no verifier, reported unverified.

## Route

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/knowledge/requests/execute` | run one already-issued request once and return a proposal |

That exact spelling is the route, byte for byte. This route takes no parameter
of any kind, so a request-target that only *resolves* to it — a trailing
`;params` run, a query string, a bare `?`, a fragment — is `404 not_found`,
refused before any Store transaction, guard consume or child, and with the
released Host, same-origin and CSRF admission still proven ahead of it. The v1
POST table matches on the stripped path, so the comparison is made in this
route's own handler, on its own target; no shared route parsing and no other
endpoint changes.

## Request

The body **is** the KnowledgeRequest v1 document the issue route returned
(`contracts/knowledge-request-v1.md`), and nothing else. There is no field for a
command, a driver alias, an executable path, an endpoint, an environment, a
timeout, a corpus grant or a credential: everything the child runs with comes
from the operator's server configuration and from the owner's own ledger. A
browser reaching this route therefore cannot widen what it reaches.

The route shares the released 16 KiB knowledge body budget
(`KNOWLEDGE_BODY_LIMIT`). It does not touch the 64 KiB Capture budget.

## Authentication

The released owner browser admission, unchanged and in the position the v1 POST
surface already ran it: loopback `Host`, same-origin `Origin`, and the session
CSRF token. **No new bearer is introduced.** The Agent/Capture ingestion token
authorises nothing here; a request carrying only that token is refused before
any handler, transaction, guard consume or child.

The route is absent from `IDEMPOTENT_POST_ROUTES` and rejects an
`Idempotency-Key` header through the existing helper, with the existing
`unsupported_idempotency_key` refusal. That mechanism stores a whole response
body in `activity.json`, and both this request and this response describe a
query.

The knowledge ledger is a collection-store document; on the experimental v4
store this route answers `knowledge_backend_unsupported` (409), the same way the
issue and import routes do.

## Operator configuration

`WorkStackHTTPServer`, `create_server` and `serve` take one optional keyword,
`knowledge_drivers`, mapping a connection alias to a `KnowledgeDriverBinding`:

| Field | Meaning |
| --- | --- |
| `upstream_workspace_uid` | the canonical UUID this alias must resolve to in the live ledger |
| `command` | the operator-pinned argv; `argv[0]` absolute, 1–16 non-empty parts of ≤512 characters |
| `environment` | the explicit string-to-string environment the child gets |

`command` and `environment` are excluded from `repr`.

This is **trusted configuration supplied by the embedding process, not a parsed
request.** `admit_drivers` copies and admits it once, in the server constructor,
*before* the store lease is taken and before the socket exists, so an invalid
binding refuses to start the server rather than becoming a refusal some later
request discovers. It reads no environment variable, no configuration file, no
SSOT document and no secret loader. At most `MAX_CONNECTIONS` (8) aliases are
accepted, under the released connection-alias grammar and canonical UUID
grammar; `command` is copied to a tuple and `environment` into an immutable
`MappingProxyType`, so a later mutation of the operator's own mapping cannot
change what a running server passes a child. There is **no route that changes a
binding**: a running server's registry is fixed for its lifetime.

The default is `None`, meaning the empty registry. That is the explicit
"no driver is configured" state, not a silent no-op.

There is no CLI or desktop configuration UI for this yet. Nothing here should be
described as a customer-installed automatic adapter.

## The attempt guard

The server holds exactly one `KnowledgeAttemptGuard` per instance. Two servers
share nothing.

The issue route registers a request with it **only** when
`issue_knowledge_request` reports a fresh, non-replayed commit, registering the
request id and the canonical `request_digest`. It never registers a replay,
never registers anything for an already-attempted identity, and never scans the
existing ledger. The issue response — its `data` document and its `meta` —
is unchanged by any of this.

Registration happens after the commit. Its only reachable failure is this
instance's capacity bound, which *is* the ledger's own 200-record bound
(`MAX_REQUESTS`), so a request the ledger accepted has room here unless records
left the ledger without this process learning of it. If registration did fail,
the issue is **not** rolled back and the response is **not** changed: pretending
to undo a committed authorization would be a lie. The request is simply never
eligible for automatic execution. That is conservative in the safe direction —
it withholds an attempt and grants no authority that was not already granted.

Every request issued before this process started is likewise ineligible. **This
is not crash resume and is not recovery**; it is the deliberate consequence of a
per-incarnation guard, and the operator is told so rather than shown a feature.

## Sequence

1. Under one outer Store transaction: `admit_execution_request` against the live
   ledger with the server's clock; select the pinned binding for the admitted
   alias; `guard.consume` once; build the child payload.
2. **Release the transaction.** No Store transaction is held while a child runs,
   so an unrelated write proceeds the whole time.
3. `run_knowledge_driver(command, payload, environment=…, timeout_seconds=75)`,
   **at most once**, on any branch. No retry, no reset, no second factory call.
4. On success, project the untrusted stdout through
   `validate_execution_proposal(payload, request_id=…, connection_alias=…,
   result_limit=…, now=…)`.
5. Admit **again**, in a new transaction, before returning anything. A policy,
   Task, expiry, state or digest change during the child blocks the result.

The child's whole stdin is one compact canonical UTF-8 JSON object:

```json
{"schema": "workstack.knowledge-execute.v1",
 "request": <the admitted KnowledgeRequest v1 document>,
 "connection": {"alias": "...", "upstream_workspace_uid": "...", "policy_revision": 3}}
```

The query travels there and nowhere else: never on the argv, never in the
environment, never in a log line. The owner's CSRF token, the Capture bearer
token, the ambient server environment and any path to the store are absent by
construction.

## Refusals

Closed codes with constant messages. At most the *name* of a field in a closed
schema travels in `details`. No query text, alias, identifier, digest, path,
command, environment value, child byte, stderr or exception text is ever echoed
or logged.

| Code | Status | Meaning |
| --- | --- | --- |
| `unknown_request`, `request_not_pending`, `policy_revision_changed`, `request_expired`, `task_binding_mismatch`, `task_binding_required`, `unknown_task`, `workspace_mismatch`, `request_digest_mismatch` | 409 | the ledger, the policy, the Task or the window is not what the caller's document assumed |
| `request_not_registered`, `request_already_attempted` | 409 | this incarnation never issued this request, or already spent its one attempt |
| `knowledge_driver_binding_mismatch` | 409 | the operator's pinned upstream workspace is not the one the ledger's roster names for this alias — nothing is spawned |
| `knowledge_driver_unavailable` | 503 | no driver is configured for this connection |
| `driver_not_started` | 503 | the child never started and never received stdin |
| `driver_outcome_unknown` | 502 | the child ran and its outcome cannot be described (non-zero exit, expired budget, unconfirmed cleanup) |
| projection refusals (`request_id_mismatch`, `result_limit_exceeded`, `invalid_import`, decoder codes, …) | 502 | the child answered, and the released validators refused what it said |
| `knowledge_driver_input_refused` | 500 | this server's own configuration and document cannot form an admissible child payload |
| structural refusals of the submitted document | 400 | the body is not a well-formed KnowledgeRequest v1 |

The 500 is a fallback, and it is kept as one. What keeps it unreached is the
issued document's own closed fields -- a query of at most 1000 characters, at
most eight corpus refs of at most 64 characters, and bounded identities and
timestamps -- which leave an ordinary issued document far below the transport's
16 KiB payload bound even after this wrapper's bytes are added. The raw 16 KiB
HTTP body budget alone does not prove that, because the wrapper is added after
it; the runtime measures the actual encoded payload instead of relying on
either bound.

A refusal from step 3, 4 or 5 does **not** give the attempt back. An outcome
this owner cannot describe is a question for the operator, never an automatic
re-execution.

## Success

```json
{"data": <canonical manual-import envelope>, "meta": {"outcome": "proposal_ready"}}
```

`data` is exactly the validator's projection — the `workstack.knowledge-import.v1`
envelope the user may then submit to the import route. Raw stdout, stderr, the
pinned command, the driver path, the environment and any token are absent by
construction, because the response body *is* the projection and nothing is added
to it.

## What this is not

Not exactly-once against a non-idempotent upstream. Not crash resume. Not an OS
sandbox: the R14 contract's decision stands, credentials live in the external
adapter's own operating configuration, and this pilot does not isolate the child
from the owner's user account. Not a scheduler — every execution is one explicit
user action. Not a general daemon: without an owner there is no execution.

The 200-record ledger bound is unchanged and is not raised by this slice.
Retention remains a separate question.
