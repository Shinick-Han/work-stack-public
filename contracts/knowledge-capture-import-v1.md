# Knowledge Capture import v1 contract

Status: **manual owner import.** One canonical loopback route imports a
manually carried retrieval answer as Captures, against a request the owner's
own ledger issued. `workstack/knowledge_captures_http.py` owns the boundary,
`workstack/knowledge_capture_import.py` owns the transaction, and
`workstack/knowledge_capture_packets.py` owns the closed envelope and the
Capture records it builds. Everything below them — the ledger
(`knowledge-owner-ledger-v1.md`), the issuer (`knowledge-request-http-v1.md`),
the retrieval wire (`capture-retrieval-v1.1.md`) and Capture Packet v1.0 — was
already released when this slice landed, and the import path was composed by
calling those modules as they stood rather than by editing them. That is a
record of how this slice was built, not a standing promise that those modules
never change: what this contract requires of them is the *behaviour* named
below, and a later slice that changes one of them must keep that behaviour, not
avoid touching the file.

**This is the manual mode of `capture-retrieval-v1.1.md`'s caller obligation 1,
and only that.** The user carries the answer out of band and imports it under
the authentication they already have. There is still **no automated adapter, no
adapter credential, no connector, no alias resolution, no provider contact and
no network call of any kind.** A `provider`, tool or `opendocuments.ask` string
authorises nothing and has nowhere in this schema to be written. Automated
submission remains later, separate work.

## The route

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/knowledge/captures/import` | import one issued request's answers |

These exact spellings are the route. A target that only *resolves* to it —
`.../import;x`, which `urlparse` strips a trailing `;params` run from — is
answered `not_found` before any Store transaction is opened.

## Admission

The released POST admission, in the released order: bounded body drained, then
browser authorization, then route resolution, then the handler.

- **The owner browser session is the whole of the authentication.** `Host` must
  be the loopback server, `Origin` must be same-origin, and `X-WorkStack-CSRF`
  must equal the session token. A cross-origin page reaches nothing.
- **A Capture/Agent bearer token alone authorises nothing here.** That token
  authorises Capture *ingestion*. It is not an owner session, so a request
  carrying only `Authorization: Bearer …` is refused and performs no ledger,
  capture or activity write. The route is deliberately not part of the
  `capture_ingest` bearer exception.
- **This is sufficient, and demanding more would describe a flow that does not
  exist.** There is no connection to authenticate: the bytes were carried by
  hand under the user's own import/Capture authority. An automated adapter
  would additionally require an independently authenticated, approved
  connection — which this slice does not have and does not pretend to.
- **No `Idempotency-Key`.** The route refuses one with
  `unsupported_idempotency_key` and is absent from `IDEMPOTENT_POST_ROUTES`,
  exactly as the two released knowledge writes are. That mechanism stores the
  whole response body in `activity.json`. Retrying is done by sending the
  **same** envelope for the **same** request; see *Replay* below.
- **Body bound: 64 KiB of UTF-8, measured on the WHOLE raw request.** The route
  declares it as `IMPORT_BODY_LIMIT` in `workstack/knowledge_captures_http.py`,
  the same 64 KiB as the released `CAPTURE_BODY_LIMIT`
  (`workstack/server_admission.py`), and `workstack/server_post_routes.py`
  applies it to this exact path only. It is *not* a per-item allowance: ten
  items share it. Every other `/api/v1/knowledge/` POST keeps its 16 KiB bound
  (`KNOWLEDGE_BODY_LIMIT`), which is neither widened nor borrowed.
- **Same-OS-user isolation is out of scope**, exactly as for every other
  loopback route.

## Backends

The ledger is `knowledge.json` at collection schema 6. The experimental v4
application (`workstack.storage`) composes a different store adapter with no
such document, and is refused with `knowledge_backend_unsupported` (409) rather
than served by a second improvised writer.

## The envelope

The object is **closed at every level**. An unknown key anywhere, and a missing
required key anywhere, is a refusal.

```json
{
  "schema": "workstack.knowledge-import.v1",
  "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "items": [
    {
      "item_id": "11111111-1111-4111-8111-111111111111",
      "title": "Rollback verification owner",
      "normalized": {
        "summary": "…", "context": "…", "action_items": [], "tags": []
      },
      "retrieval": { "schema": "workstack.capture-retrieval.v1.1", "…": "…" }
    }
  ]
}
```

| Field | Rule |
| --- | --- |
| `schema` | exactly `workstack.knowledge-import.v1` |
| `request_id` | canonical lowercase non-nil UUID; must name a record the ledger issued |
| `items` | 1..10 entries, `item_id` unique, never more than the record's stored `result_limit` |
| `items[].item_id` | canonical lowercase non-nil UUID |
| `items[].title` | 1..500 characters of safe display text, held to the released Capture display-text gate |
| `items[].normalized` | the existing Capture `normalized` shape: `summary`, `context`, `action_items` required, `tags` optional |
| `items[].retrieval` | the released `workstack.capture-retrieval.v1.1` wire |

**There is no `provider`, `tools`, `verification`, `source`, `source_key`,
`connection`, `path`, `web_url`, `query`, `capture_id`, `task_id` or credential
field, at any depth.** None of those can be asserted, and a body carrying one
is refused `unknown_field`. `task_hints` is not a caller field either: an
import never proposes Task work.

Each item's retrieval is serialized and validated through the released
`validate_retrieval_payload`, so the strict decoder, the closed retrieval
schema and its own **16 KiB** bound all apply to real octets. The resulting
Capture record is then held to the released Capture body budget alongside that
extension.

## What the server derives

Nothing the envelope says about *where the bytes came from* is stored, because
the envelope cannot say it. The Capture source is built entirely from the
server's own facts:

| Capture field | Source |
| --- | --- |
| `source.provider` | the constant `manual` — these bytes were carried by hand |
| `source.resource_type` | the constant `knowledge.answer` |
| `source.connection_ref` | the **recorded** `connection_alias` of the ledger request |
| `source.container_ref` | the request's canonical UUID |
| `source.object_ref` | the item's canonical UUID |
| `source.version_ref` | a canonical digest of the item's own *admitted* content |
| `source.fingerprint` / `source_key` | the released `fingerprint_for` / `source_key_for` helpers |
| `source.display_title` | the bounded safe `items[].title` |
| `source.web_url` | `null` |
| `source.retrieved_at`, `provenance.created_at`, `created_at`/`updated_at` | the owner clock the caller supplies |
| `provenance.capture_mode` | the constant `manual` |
| `schema_version` | `1.1` |
| `retrieval` | the sanitized wire rebuilt from the released validator's projection |

**No OpenDocuments origin is invented.** The stored record is an honest
unattested manual answer: `origin` is `null`, `origin_state` is
`reported_unverified` (or `synthesized`), `capture_source_type` is
`knowledge.answer`, and every reported version stays `reported_unverified`. No
`RetrievalVerification` is accepted from anywhere, and there is no field for
one. No provider allow-list is widened, and the released generic-ingest route
still writes 1.0 only.

## One transaction, one `save_many`

The whole decision happens inside one `Store.transaction()`:

1. read `workspace.json`, `knowledge.json`, `captures.json`, `activity.json`
   and, for a Task-bound request, `backlog.json`;
2. validate the **whole** envelope and stage every Capture in memory — nothing
   is written per item, and the released `ingest_capture` (which writes on
   every call) is deliberately not reused;
3. call the released `plan_ledger_stage_completion` **once**, after the whole
   batch;
4. save `knowledge.json`, `captures.json` and `activity.json` in **one**
   `save_many`.

Because planning is pure and the commit is one journalled write, a failure
anywhere before the commit leaves all three documents untouched and the request
still pending, and an interrupted commit recovers through the released journal
to one coherent old or new generation — never a mix.

**Nothing else is touched.** No Task, backlog, status, link, objective,
workspace or report write happens, `task_hints` is empty, every imported
Capture lands in `inbox`, and no automatic link is made even when the request
was bound to a Task. Linking and conversion stay explicit user actions through
the released capture routes.

**No import lands on an existing Capture.** A staged record whose `source_key`
is already held by any stored capture refuses the whole batch with
`source_key_conflict`; no unrelated capture is ever overwritten.

## Replay

The logical key is the `request_id` **plus** the canonical digest of the entire
admitted completion. The digest covers, in order, each item's identity, its
admitted display title, its admitted normalized projection and its sanitized
retrieval wire. It deliberately does **not** cover the allocated Capture
identifiers, and the identifiers of a completed record are recovered from the
ledger *before* the planner is asked anything — so a legitimate retry is
recognised rather than refused for having allocated a second set.

- **The same envelope for the same request** returns the **original** Capture
  identifiers and their **existing stored contents**, with
  `meta.replayed = true`, writing no record and no audit event. This holds
  after the request's window has closed and after the connection policy that
  authorised it has been retired, because replay is decided before any
  authority check — exactly as the ledger contract specifies.
- **A changed batch** under the same request refuses `completion_digest_`
  `mismatch` (409). **A reordered batch is a changed batch**: this guesses at no
  equivalence.
- **A completed record naming a Capture the store no longer holds** refuses
  `capture_record_missing` (409) rather than inventing the evidence.
- **Two competing imports of one envelope settle once.** The outer transaction
  serialises them; the second re-reads the document inside its own transaction,
  observes the completion and replays it. Exactly one set of records exists and
  exactly one audit event is written.
- **A response lost in transit is retried with the same envelope.** There is no
  cached plaintext receipt in `activity.json` for this route, by design.

A **new** completion is additionally held to the current state by the released
planner: the batch may not exceed the issued `result_limit`, the record's
policy revision must still be in force, the workspace must be this store, the
Task binding must match the Task the Store actually holds in both directions,
and the request must not have expired.

## The response

```json
{
  "data": {
    "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "capture_ids": ["C-0001", "C-0002"],
    "completion_digest": "sha256:…",
    "completed_at": "2026-09-08T09:02:00Z"
  },
  "meta": { "replayed": false, "imported_count": 2 }
}
```

The imported Capture bodies are not echoed. A client reads them back through
the released capture projection, which re-derives the retrieval state on every
read rather than trusting anything this response said.

## Reading a stored 1.1 record

A stored record keeps only the sanitized retrieval **wire**. Every trusted part
of the projection — `origin`, `origin_state`, each `version_state` and
`capture_source_type` — is **re-derived on every read** through the released
validator with **no** `RetrievalVerification`, so nothing a writer stored can
promote it. `GET /api/v1/captures` and every service read carry that re-derived
projection under `retrieval`; a stored 1.0 record has no such key and is
projected exactly as it was before.

A stored 1.1 record whose retrieval no longer validates is a **corrupt store**,
not a capture to display: `validate_document_values` refuses it on a v6 load,
and the read path refuses it as `StoreCorruptError`. It is never shown as
trusted metadata. Historical 1.0 records are not judged by this rule at all.

**Generic ingestion stays 1.0-only and cannot erase an import.** `source_key`
and `fingerprint` are both derived from values a v1.0 packet may spell for
itself, so a packet colliding with an imported record is constructible;
`POST /api/v1/captures` refuses that collision rather than replacing evidence
the ledger has already accounted for. Existing 1.0 re-ingest, stale, conflict,
link, convert and dismiss behaviour is unchanged.

## The audit trail

One content-free `knowledge.capture_ingested` event is appended in the same
`save_many`, carrying `request_id`, `capture_ids` and `imported_count` — and
nothing else. **No envelope, normalized summary, query, source title, evidence
reference, digest of content, returned body or exception text is logged**, and
no extra durable document is created.

## Refusals

The envelope is the released one:
`{"error": {"code": …, "message": …, "details": {"field": …}}}`.

`message` is the constant `"the knowledge capture import was refused"`, and
`details` carries at most `field`, the *name* of a field in a closed schema. No
title, summary, evidence reference, identifier, digest, timestamp, path or
credential is ever echoed into a refusal.

Conflicts (409), because the body was well formed and the state had moved:
`capture_record_missing`, `completion_digest_mismatch`,
`completion_replay_mismatch`, `ledger_full`, `policy_revision_changed`,
`request_expired`, `result_limit_exceeded`, `source_key_conflict`,
`task_binding_mismatch`, `task_binding_required`, `unknown_request`,
`unknown_task`, `workspace_mismatch`.

Every other closed code is 400. This surface's own codes are
`invalid_import`, `unknown_field`, `missing_field`, `unsupported_schema`,
`invalid_uuid`, `invalid_text`, `invalid_items`, `duplicate_item_id` and
`invalid_retrieval`; the released `CaptureRetrievalError`,
`CaptureValidationError`, `KnowledgeRequestError` and `KnowledgeLedgerError`
codes propagate unchanged, alongside the released admission codes.

**Injection and secret refusal is the released sanitizers plus a closed
envelope, and nothing more is claimed.** Credential material, rendered HTML, an
address, a quoted reply, a raw-content canary, a control character, a resolved
source location in a title and every forbidden raw-content key are refused by
the released Capture v1.0 and retrieval detectors, at any depth. That is a
bounded, honest defence: it is **not** a claim of perfect semantic
injection detection, and the closed envelope — not a detector — is what makes a
provider, credential or path unrepresentable.

## What this slice does not do

- **No automated adapter and no credential.** No connector token is issued,
  accepted, stored or verified, and no runtime submits on a user's behalf.
- **No provider contact and no alias resolution.** An alias stays a label.
- **No RAG, model, external source or network call.** Nothing here retrieves;
  the answer arrives already produced, carried by the user.
- **No request minting.** Only a request the ledger already issued may be
  imported against; `unknown_request` is the answer otherwise.
- **No Task change, no auto-link and no implicit conversion.**
- **No v4 composition.** Schema 4 is refused by code.
- **No store migration, no ledger change, no issuer change, no frontend, no
  Adapter, no quality-config change and no new dependency.**

## Reused, not re-implemented

These are the released seams this slice calls rather than re-implements. Each
was reused as released, without an edit, when the slice landed; what the import
path depends on is the behaviour stated beside it.

- `workstack.capture.validate_capture_packet`, `source_key_for`,
  `fingerprint_for`, `canonical_digest` — Capture v1.0's own rules decide the
  packet, the source key and the fingerprint.
- `workstack.capture_retrieval.validate_retrieval_payload`,
  `validate_retrieval_extension` and
  `require_within_capture_body_budget` — the retrieval wire, its 16 KiB bound
  and its codes, which propagate unchanged.
- `workstack.knowledge_owner_requests.plan_ledger_stage_completion` — called
  once per request after the whole batch, and committed in the same
  `save_many` as the evidence it retires.
- The released admission chain, the released error envelope, the released
  `Store.save_many` journal, and the released capture link/convert/dismiss
  routes.
