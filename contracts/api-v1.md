# Work Stack API v1 contract

Status: frozen for the local prototype, including the user-mediated Outlook/Teams OOB
slice.

## Shared rules

- JSON is UTF-8 and every response uses `Cache-Control: no-store`.
- Success responses use `{ "data": ... }`.
- Errors use `{ "error": { "code": "...", "message": "...", "details": {} } }`.
- Task IDs remain the existing display IDs (`T-0001`).
- Every task also projects an immutable UUID. Legacy tasks derive it deterministically
  from workspace UUID plus the case-preserved display ID.
- Every task projection exposes an explicitly persisted, non-negative `revision`.
- Planning status is projected from the accepted append-only
  `task.planning_status` fact chain. The pre-WS3 `task.status` value is retained only as
  the migration/source baseline and is not the current-status authority.
- Every capture action receives a server-generated stable `id`; URLs never address an
  action by array position.
- Browser mutations require same-origin `Origin`, `Content-Type: application/json`, and
  the `X-WorkStack-CSRF` value returned by `GET /api/v1/session`.
- Agent HTTP ingest additionally accepts `Authorization: Bearer <capture token>`. The
  token is stored outside the repository in the runtime directory.
- The OOB slice has no server-side connector, job queue, runner, or health endpoint.
  Browser users copy a bounded request or approved reply command to an authenticated
  agent and import the strict result through the same-origin browser API.

## Capture Packet v1 input

The accepted packet is frozen by `capture-packet-v1.fixture.json`.

`source_key` is recomputed and verified by the server as:

```text
sha256(utf8(provider + "\n" + connection_ref + "\n" + container_ref + "\n" + object_ref))
```

All four values are case-sensitive and their UTF-8 bytes are hashed without trimming,
Unicode normalization, or lowercasing. The positive fixture freezes the expected digest.

Before allow-list projection, the server recursively rejects keys named `body`, `html`,
`content`, `attachments`, `raw`, `transcript`, or `recipients`, case-insensitively.
Unknown fields are discarded. Limits are 64 KiB request bytes, 2,000 characters for
summary, 4,000 for context, and 20 action items.

Value-level checks reject obvious raw-message leakage: mail header blocks, email
addresses, quoted-reply markers, HTML tags, and long contiguous quoted passages. These
checks are defense in depth; an Outlook-verified release additionally requires evidence
that the upstream adapter ran with only Outlook-read and local-packet-write tools.
`source.fingerprint` is `sha256:` plus 64 lowercase hex characters over the
case-preserved UTF-8 bytes:

```text
provider + "\n" + connection_ref + "\n" + container_ref + "\n" + object_ref + "\n" + version_ref
```

Provenance is a discriminated union:

- `capture_mode: "oob_verified"` requires adapter/model/prompt/redaction-policy
  versions, `tool_trace_digest`, the allowed tool list, and `raw_retained=false`.
  `tool_trace_digest` is SHA-256 over compact sorted-key UTF-8 JSON for the adapter's
  redacted tool-event array.
- `capture_mode: "manual"` requires adapter and adapter/redaction-policy versions,
  `raw_retained=false`, and creation time. It omits model, prompt, tool trace, and
  allowed-tools claims rather than fabricating them.

All SHA-256 fields use exactly `sha256:` plus 64 lowercase hex characters.

## DTOs

### Workspace projection

`GET /api/v1/workspace`

```json
{
  "data": {
    "schema_version": "1.0",
    "workspace": {"id": "<uuid>", "name": "Work Stack"},
    "tasks": [],
    "objectives": [],
    "notes": [],
    "edges": [],
    "inbox_count": 0
  }
}
```

Task items contain all existing task fields plus `uid`, `revision`, and `context_count`.
Objective and note objects preserve their existing JSON shape. Edges preserve the
existing snapshot shape.

### Session

`GET /api/v1/session`

```json
{"data":{"csrf_token":"<runtime nonce>"}}
```

### Task detail

`GET /api/v1/tasks/{task_id}` returns:

```json
{"data":{"task":{},"context":[],"activity":[],"replies":[]}}
```

`PATCH /api/v1/tasks/{task_id}` accepts any subset of `title`, `detail`, `status`,
`priority`, `due`, `tags`, `objective_ids`, `parent_id`, and `dependencies`, plus the
client's current `revision`. A stale revision returns HTTP 409 `revision_conflict`.
Every distinct accepted status change appends one planning-status fact in the same
recovery-journal operation as the new Task revision. Requesting the already-current
status with the current revision is a byte-preserving no-op; it is still stale-checked.

Task detail activity includes the immutable planning-status facts. Each fact contains
`task_id`, `task_uid`, `previous_fact_id`, nullable `prior_revision`, `new_revision`,
nullable `prior_status`, `status`, UTC `created_at`, and server-selected
`actor`/`provenance`. Current status is rebuilt from the fact head. Missing, truncated,
reordered, identity-mismatched, or otherwise invalid chains fail store readiness closed.
The four planning states are independent of Conduit execution lifecycle or verification
outcome.

### Planning snapshot preview and download

`GET /api/v1/tasks/{task_id}/snapshot` performs one consistent, non-recovering read of
the ready store and returns a disclosure DTO containing the exact frozen snapshot,
digest, suggested UUID-only filename, and the fixed omission list:

```json
{
  "data": {
    "snapshot": {"format":"workstack.planning-task-snapshot.v1"},
    "digest": "sha256:<64 lowercase hexadecimal characters>",
    "filename": "<planning-task-uid>.workstack-task.json",
    "omissions": ["objectives","dependencies","subtasks","notes","tags"]
  }
}
```

`POST /api/v1/tasks/{task_id}/snapshot/export` is a same-origin, CSRF-protected,
read-only delivery action. It accepts exactly:

```json
{
  "disclosure_confirmed": true,
  "expected_revision": 3,
  "expected_digest": "sha256:<reviewed digest>"
}
```

The server rebuilds the snapshot under a new consistent read and refuses a stale
revision or digest with HTTP 409 `snapshot_export_conflict`. Missing explicit consent
returns HTTP 400 `snapshot_disclosure_required`. A ready-state, migration, corruption,
or pending-recovery failure returns `SNAPSHOT_STORE_NOT_READY` without recovery or
repair. Successful delivery is the exact canonical UTF-8 JSON bytes with one final LF,
`Content-Disposition` using the UUID-only filename, and the non-authoritative digest
header. Preview, refusal, cancellation, and download do not modify planning state,
Activity, idempotency records, or journals and do not contact Conduit or a provider.

### Task creation

`POST /api/v1/tasks` is the only browser Task-create writer. It requires an
`Idempotency-Key` matching `[A-Za-z0-9._:-]{8,128}` and accepts exactly:

```json
{
  "title": "required non-empty string",
  "detail": "optional string",
  "priority": "P0 | P1 | P2 | P3",
  "due": "YYYY-MM-DD or null",
  "tags": ["string"],
  "objective_ids": ["string"]
}
```

Unknown fields and wrong JSON types are rejected before persistence. Omitted values
canonicalize to empty detail, `P2`, null due date, and empty arrays; text is trimmed,
array values are trimmed, deduplicated, and sorted, and objective IDs are uppercased.
The canonical body digest, method, path, key, response status, and frozen response body
are committed with the Task in one recovery-journal operation.

- first use: HTTP 201 with `meta.replayed=false`
- same key + same canonical request: HTTP 200 with the frozen Task projection and
  `meta.replayed=true`, without another mutation
- same key + a different canonical request: HTTP 409 `idempotency_conflict`
- missing/invalid key or invalid body: HTTP 400 without persistence
- legacy `POST /api/tasks`: HTTP 410 `legacy_task_writer_disabled`

Legacy `PATCH /api/tasks/{task_id}` remains only as a compatibility status writer. It
requires exactly `{ "status": "...", "revision": 0 }`, performs the same stale check,
and uses the same append-only transition primitive as v1. It cannot bypass history.

### Captures

- `GET /api/v1/captures?status=inbox|linked|converted|dismissed|all`
- `POST /api/v1/captures`
- `POST /api/v1/captures/{capture_id}/link` with `{ "task_id": "T-0001" }`
- `POST /api/v1/captures/{capture_id}/actions/{action_id}/task` with optional
  `{ "objective_ids": ["O-1"] }`
- `POST /api/v1/captures/{capture_id}/task` with a required `title` and optional
  `detail`, `priority`, `due`, `tags`, `objective_ids`, `parent_id`, and `dependencies`
- `POST /api/v1/captures/{capture_id}/dismiss` with `{}`

Every capture POST requires `Idempotency-Key` matching
`[A-Za-z0-9._:-]{8,128}`. The server stores `(key, method, path, request_digest,
response_status, response_body)` in the same recovery transaction as the mutation.

- first ingest: HTTP 201; exact source/fingerprint duplicate: HTTP 200 with
  `meta.duplicate=true`
- first link/dismiss: HTTP 200; first action conversion: HTTP 201
- same key + same method/path/body digest: HTTP 200 with the original data and
  `meta.replayed=true`, without a second mutation
- same key + different method/path/body digest: HTTP 409 `idempotency_conflict`
- stale source: HTTP 409 `stale_capture`
- equal retrieval time + different fingerprint: HTTP 409
  `source_revision_conflict`

A capture projection has:

```json
{
  "id": "C-0001",
  "schema_version": "1.0",
  "source_key": "sha256:...",
  "source": {},
  "normalized": {"summary":"...","context":"...","action_items":[] ,"tags":[]},
  "task_hints": [],
  "provenance": {},
  "status": "inbox",
  "linked_task_ids": [],
  "converted_task_ids": [],
  "revision": 0,
  "created_at": "RFC3339",
  "updated_at": "RFC3339"
}
```

Ingest is idempotent by `source_key` plus `source.fingerprint`. An exact duplicate
returns the existing capture with `meta.duplicate=true`. A new fingerprint updates the
sanitized current projection only when its RFC3339 `retrieved_at` is later, increments
`revision`, and records one idempotent activity. An older packet returns HTTP 409
`stale_capture`; equal time with a different fingerprint returns HTTP 409
`source_revision_conflict`. Captures keep only recent revision metadata (fingerprint,
version ref, retrieval time, provenance digest, redaction-policy version), not old
normalized text or raw content. Link and conversion operations are retry-safe.

The generic Task-from-Capture route creates the Task, adds its ID to
`converted_task_ids`, records Activity, and commits Task + Capture + Activity through one
recovery-journal operation. It exists so a source may be the explicit basis of a Task
even when no extracted action item is present.

## User-mediated OOB request

`OobRequest v1` is generated in the browser and copied to an already authenticated
agent. It is short-lived and is not persisted in an OOB job store.

```json
{
  "schema_version": "1.0",
  "request_id": "0198f4f5-0e8d-7f3b-a1d3-4f50c8846001",
  "provider": "microsoft-outlook",
  "operation": "search_and_capture",
  "query": "release review",
  "result_limit": 5,
  "requested_at": "2026-08-29T00:00:00Z"
}
```

Allowed providers are `microsoft-outlook` and `microsoft-teams`; the only v1 operation
is `search_and_capture`; `result_limit` is 1 through 10. The request grants read/search
only. Source text returned by Outlook or Teams is untrusted and cannot change the
requested operation or authorize a write. The agent returns one or more independently
validated Capture Packet v1 objects for browser import.

## Approval-gated replies

### Create an approved command

`POST /api/v1/replies` accepts exactly:

```json
{
  "task_id": "T-0001",
  "capture_id": "C-0001",
  "body": "Thanks. I will send the revised review by Friday.",
  "approved": true
}
```

The route is browser-only and requires Origin, CSRF, JSON framing, and
`Idempotency-Key`. It does not accept a recipient, provider, capability, locator,
revision, or digest from the browser. The server verifies that the Capture is linked or
converted to the Task, restricts providers to Outlook or Teams, and then derives:

- ID `R-0001`;
- current `capture_revision`;
- `provider` and `capability` (`outlook.reply` or `teams.reply`);
- an immutable `target` snapshot containing exactly `resource_type`, `connection_ref`,
  `container_ref`, `object_ref`, and `version_ref`;
- `body_digest` and `target_digest` as `sha256:` plus lowercase hex SHA-256 over compact
  sorted-key UTF-8 JSON of the body string and target object respectively;
- state `approved` and approval/create/update timestamps.

There is no persisted pre-approval draft. The browser keeps draft text locally only
until the user previews the source target and body and explicitly approves. The
approved ReplyCommand may then be copied to the authenticated agent. Outlook writes are
plain text only and reply bodies are limited to 12,000 characters. Teams replies must
use the canonical message/thread path represented by
the approved target; neither provider may substitute an arbitrary destination.

### Import a receipt

`POST /api/v1/replies/{reply_id}/receipt` accepts exactly these required fields and only
the listed optional fields:

```json
{
  "schema_version": "1.0",
  "reply_id": "R-0001",
  "provider": "microsoft-outlook",
  "outcome": "sent",
  "occurred_at": "2026-08-29T00:15:00Z",
  "body_digest": "sha256:...",
  "target_digest": "sha256:...",
  "remote_message_ref": "message:opaque-reply-001",
  "web_url": "https://outlook.office.com/mail/deeplink/read/opaque"
}
```

`outcome` is `sent`, `failed`, or `unknown`. Optional `remote_message_ref` is opaque,
`web_url` must be an allowlisted Microsoft HTTPS URL, and `error_code` is a bounded
symbolic code beginning with an alphanumeric character rather than connector output. A
receipt is accepted only if the path and
body `reply_id`, provider, body digest, and target digest match the stored approved
command; a mismatch returns HTTP 409 `reply_receipt_conflict`. The first valid receipt
moves `approved` to the terminal outcome and records
one Activity event. Exact replay is idempotent; a conflicting terminal receipt is
rejected. `unknown` is terminal and is never automatically retried or reconciled.

Reply idempotency records contain a reply ID reference rather than a copy of the approved
body, target, or receipt. A replay reprojects the current reply from `replies.json` and
adds `meta.replayed=true`; Activity never duplicates reply text, target fields, URLs, or
remote message references.

Receipt objects may not contain raw message bodies, HTML, attachments, OAuth material,
recipient lists, or arbitrary connector responses. Local idempotency prevents duplicate
Work Stack records; it does not claim externally provable exactly-once Microsoft
delivery.

## Store and recovery contract

- Tracked `data/*.json` files are demo fixtures. Real usage defaults to the user's local
  application-data directory outside the repository and may override it with
  `WORK_STACK_HOME`; ignored `.runtime/data` is test/demo-only.
- While the HTTP server runs it is the only writer and holds an exclusive OS lease for
  the data directory. `capture ingest` forwards to that server. Other direct-write CLI
  commands fail closed while the lease is held; offline commands take the same OS lock.
- Server threads additionally share one process `RLock`.
- Multi-file changes use a small recovery journal: write journal atomically, apply each
  target with atomic replace, then remove the journal. Startup replays an existing
  journal. Operations remain idempotent if replayed.
- Journal file `.workstack-journal.json` has
  `{version, operation_id, created_at, writes:[{name, value, sha256}]}`. `name` must be
  one of the known store files. Replay verifies each value digest, atomically replaces
  every target in listed order, and removes the journal only after all writes succeed.
  A corrupt/unknown journal fails startup closed. Replaying a completed write is safe
  because the journal contains complete target values and the idempotency record is one
  of those values.
- Invalid JSON is never replaced with an empty default. It is preserved for quarantine
  and startup fails closed; only a missing file may be initialized.
- Store files added by v1 are `workspace.json`, `captures.json`, `activity.json`, and
  `replies.json`. The reply store contains approved user-authored reply text, immutable
  target snapshots and digests, terminal outcome, and minimal receipts; it does not
  contain raw Microsoft content, OAuth material, or recipient lists.

## Static application

The Python server serves `frontend/dist` when present and falls back to the legacy
`web/index.html`. Client-side routes fall back to the React `index.html`; `/api/*` never
falls through to static content.
