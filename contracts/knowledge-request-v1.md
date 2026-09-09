# KnowledgeRequest v1 contract

Status: the wire contract. `workstack/knowledge_request.py` implements this document
exactly, and it remains a pure validation primitive: it opens no socket, reads no clock,
persists nothing and resolves no connector or provider. This document owns the shape and
the refusals, not who may ask for one.

The surfaces that construct, store and serve a KnowledgeRequest have since landed and are
contracted separately: `workstack/knowledge_owner_requests.py` records and retires the
identifier (`knowledge-owner-ledger-v1.md`), `workstack/knowledge_requests_http.py` with
`workstack/knowledge_request_issuer.py` issue one over the authenticated loopback owner
routes (`knowledge-request-http-v1.md`), and the answer is imported manually through
`workstack/knowledge_captures_http.py` (`knowledge-capture-import-v1.md`). None of them
edits this module or widens a provider allow-list, and **nothing in Work Stack submits a
KnowledgeRequest to a provider**: there is still no adapter, no connector credential and
no network call. The earlier statement that nothing constructed, stored or served one
described this document's own slice and is no longer the state of the code.

A KnowledgeRequest is the asking half of one evidence exchange. The answering half is
`capture-retrieval-v1.1.md`.

This is **not** the desktop shell's `workstack-knowledge-request` host-bridge message.
That is a local IPC envelope between the webview and `knowledge_host`. This is a wire
contract for an out-of-band retrieval exchange, and it carries its own schema string.

## Wire shape

`schema` is the literal `workstack.knowledge-request.v1`.

```json
{
  "schema": "workstack.knowledge-request.v1",
  "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "binding": {
    "workspace_uid": "66666666-6666-4666-8666-666666666666",
    "task_uid": "77777777-7777-4777-8777-777777777777",
    "task_id": "T-0033",
    "task_revision": 2
  },
  "purpose": "find_context",
  "query": "rollback verification owner",
  "corpus_refs": ["nas-team-share", "notion-product"],
  "result_limit": 5,
  "requested_at": "2026-09-08T09:00:00Z",
  "expires_at": "2026-09-08T09:05:00Z"
}
```

The example is illustrative of the shape only. The contract is the field table below and
the code that enforces it, not this document's sample values.

## Fields

The object is **closed at every level**: an unknown key anywhere, and a missing required
key anywhere, is a refusal. Nothing is defaulted and nothing is coerced.

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | string | exactly `workstack.knowledge-request.v1` |
| `request_id` | string | canonical lowercase textual UUID, non-nil. Braces, a `urn:uuid:` prefix, uppercase and the 32-character form are all refused, so one request has exactly one identity on the wire and in a ledger |
| `binding` | object | either `{workspace_uid}` alone, or the full `{workspace_uid, task_uid, task_id, task_revision}`. No other key set |
| `binding.workspace_uid` | string | canonical non-nil UUID; must equal the caller's active workspace |
| `binding.task_uid` | string | canonical non-nil UUID |
| `binding.task_id` | string | the existing display ID grammar `T-\d{4,}`, matched case-insensitively and projected uppercase |
| `binding.task_revision` | integer | `0 .. 2^53-1`. A JSON boolean is not an integer here |
| `purpose` | string | one of `find_context`, `extract_actions`, `refresh_capture` |
| `query` | string | trimmed, 1..1000 characters, no C0/DEL/C1 control character, no credential material |
| `corpus_refs` | array of string | 1..8 unique aliases matching `[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?`, each one already granted to the caller |
| `result_limit` | integer | `1 .. 10`. A JSON boolean is not an integer here |
| `requested_at` | string | strict RFC3339, the same grammar Capture Packet v1 already accepts |
| `expires_at` | string | strict RFC3339, strictly after `requested_at` and at most 300 seconds after it |

The Task trio is all-or-nothing. A `task_uid` without its `task_revision` would bind
evidence to a Task state nobody read, so a partial binding is refused rather than
completed from the caller's own view.

The display ID is carried because the binding shape already in this repository
(`workstack.knowledge_context.task_binding`) carries it. Dropping it here would make the
two bindings different objects for no gain.

## What a request may not do

**Grant itself scope.** Active workspace, active Task, granted corpus aliases and the
clock all come from the caller as `RequestAuthority`. A body naming a corpus the caller
does not hold is refused with `corpus_not_granted`; it is never treated as a grant.

**Claim a provider or a tool.** There is no `provider`, `tools`, `connection` or
`authority` field, so a payload asserting `opendocuments.ask` is refused as an unknown
field. Claiming a connector in a payload is not authentication.

**Execute.** The query is bounded, control-free text the user can read before submitting.
Nothing in this module interpolates it into a command, a path or a URL, and nothing here
resolves it. It is data handed to a retrieval engine, and the engine's answer is likewise
untrusted input to `capture-retrieval-v1.1.md`.

**Carry Task detail.** The binding is identity only. No Task title, detail, note or body
is attached, and the closed key set means none can be added by a sender. Attaching Task
detail automatically would hand the retrieval side content it was never granted.

**Outlive five minutes.** `MAX_ACTIVE_SECONDS` is 300. That single window bounds both an
active request and an automatic submission. A request whose window is longer is refused
outright rather than truncated.

**Renew itself.** There is deliberately no renew, extend or refresh entry point.
`is_expired(request, now)` reads the expiry; it never moves it. An expired manual result
requires a *fresh* request that the user explicitly reviews. Silence is not consent, and
a lapsed window must not quietly become a live one.

## Caller obligations

`validate_knowledge_request(document, authority)` proves the document is well formed and
inside the authority handed to it. It proves nothing else. The caller must:

1. Build `RequestAuthority` from its own trusted state — never from the request body. A
   `RequestAuthority` assembled from the document defeats every scope check here. The
   function refuses anything that is not the declared frozen dataclass, which stops the
   accidental `dict`, not a determined caller. The *shape* of that object is checked
   before it is used, including the nested `ActiveTask` and every field type, so corrupt
   host state leaves as `invalid_authority` naming a field rather than as a raw
   `AttributeError` or `TypeError`. Trusted input is still checked input; "trusted" is a
   statement about where a value came from, not about whether it is well formed.
2. Supply `workspace_uid` as the **active** workspace at the moment of the check.
3. Supply `active_task` when a Task is open, at the revision it was actually read under,
   and `None` when none is. Both directions are enforced: a request omitting the binding
   while a Task is open is refused (`task_binding_required`), and a request carrying one
   while none is open is refused (`task_binding_mismatch`).
4. Supply `granted_corpus_refs` from a **nonsecret corpus registry**. An alias names a
   grant; it must never encode a filesystem root, a share, a host, a credential or a
   connection string. The manual owner flow now supplies those grants from the stored
   connection policy in `knowledge.json`, through `owner_request_authority` in
   `workstack/knowledge_owner_requests.py`; see `knowledge-owner-ledger-v1.md` and
   `knowledge-request-http-v1.md`. Resolving these aliases to real upstream endpoints
   and admitted document catalogs remains separate Adapter work.
5. Supply `now` as a strict RFC3339 instant. This module never reads a wall clock, so
   expiry is decided by the host and is reproducible in a test and in a ledger.
6. Issue, record and retire `request_id` in a host-issued ledger, and refuse a replay.
   Validation is not a ledger, and passing validation twice is two valid documents, not
   one authorized exchange.
7. Admit the answering transport under one of the two modes in
   `capture-retrieval-v1.1.md`, and never on the strength of a string in a payload:

   - **Manual user import.** The user carries the bytes out of band and imports them.
     Admission rests on the existing authenticated user session, its CSRF protection and
     the user's own import/Capture authority. No adapter credential is required merely
     because the bytes were carried by hand.
   - **Automated adapter.** A connector submits directly. Admission additionally requires
     an independently authenticated, approved connection carrying the retrieval and
     Capture submission authority.

   In both modes a `provider` or tool name inside a document authorises nothing, and this
   schema has nowhere to put such a claim.

## Refusal codes

A refusal is `KnowledgeRequestError` carrying `code` and, at most, `details.field` — the
*name* of a field in this closed schema. No submitted value, query text, corpus alias,
identifier or timestamp is ever echoed into a diagnostic.

`duplicate_json_key`, `non_finite_number`, `request_too_large`, `request_too_deep`,
`invalid_encoding`, `invalid_json`, `invalid_request`, `invalid_authority`,
`unsupported_schema`, `unknown_field`, `missing_field`, `invalid_uuid`, `invalid_number`,
`out_of_range`, `invalid_purpose`, `invalid_query`, `credential_material_suspected`,
`invalid_corpus_refs`, `duplicate_corpus_ref`, `corpus_not_granted`,
`invalid_task_binding`, `task_binding_required`, `task_binding_mismatch`,
`workspace_mismatch`, `invalid_timestamp`, `invalid_request_window`,
`request_not_yet_active`, `request_expired`.

## Decoding

`decode_knowledge_request` bounds the payload at 8 KiB of UTF-8 and refuses duplicate
JSON keys, the non-finite literals `NaN`/`Infinity`/`-Infinity`, undecodable bytes and
nesting past the decoder's depth. A duplicate key has no single meaning: last-wins would
let a sender show the user one value and the validator another.

It also refuses a *standard* JSON number that overflows to infinity. `1e9999` is legal
JSON syntax and Python's decoder answers `inf` for it, so `parse_constant` never sees it;
the decoder checks each parsed float itself and raises `non_finite_number`. The promise
that this decoder yields only finite numbers is therefore true for every consumer, not
only for the ones that happen to range-check afterwards. A large JSON *integer* stays
exact — it is bounded by the field rules that need a bound, not by the decoder.

`payload_bytes` exposes the same encoding step the decoder uses, so a caller that needs
the measured wire size gets the octets the bound was applied to rather than a separate
count of its own.

## Versioning and migration

- This is a new schema string on a new wire. Capture Packet v1.0, `OobRequest v1` and the
  desktop host bridge are untouched, and no existing fixture changes.
- `OobRequest v1` (`api-v1.md`) stays the Outlook/Teams request. It is not renamed,
  widened or replaced here. The two coexist; a later slice may unify them, and that is a
  contract change, not an implementation detail.
- A future v2 takes a new `schema` string. This validator refuses any other value, so an
  old reader cannot silently accept a newer document.
- Reused from existing code, not re-implemented: the strict RFC3339 grammar and parser,
  the Task display-ID grammar, the percent-decoding bound and the credential-material
  detector, all from `workstack/capture.py`; the binding field names from
  `workstack/knowledge_context.py`; the 1000-character query bound already enforced by
  the desktop host's `knowledge_host_search`. The bound is restated rather than imported
  because `workstack` must not depend on the desktop shell package.
- No new dependency and no general schema framework is introduced.
