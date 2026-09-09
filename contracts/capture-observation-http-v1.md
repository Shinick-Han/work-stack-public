# Saved source observation HTTP v1

Two loopback routes let an owner record one Capture's source check and read
back what was retained. A retained observation is a statement about a **past**
check. It is never a claim that the sources are currently in that state, and it
never becomes part of the Capture.

The released read-only check, `POST /api/v1/knowledge/captures/verify`
(`contracts/knowledge-verification-v1.md`), is unchanged: same request, same
`{binding, result}` response, same `verification_ready` outcome, and it still
saves nothing. These routes are additional, not a replacement.

The composition is implemented in `workstack/capture_observation_http.py`. The
derivation and the record itself belong to the owner runtime
(`record_capture_source_check`, `get_capture_source_observation`) and the
storage layer; this document specifies only the wire.

## Record a check

```
POST /api/v1/knowledge/captures/record-check
{"workspace_uid": <UUID>, "capture_id": <ID>, "capture_revision": <integer>}
```

The target is that exact path. A query string, a bare `?`, a fragment, a
trailing `;params` run, a trailing slash and a percent-encoded alias are all
targets this route never published and receive the closed unknown-route answer
(`not_found`, 404) before any Store transaction, verification gate or child.

The body is the released three-field verification body, admitted by the same
parser: no document reference, expected version, corpus, connection alias,
verifier alias, command, environment or configuration path exists here, and a
body carrying a fourth field is refused rather than having that field ignored.

Admission is the released v1 POST admission, unchanged and in the same
position: the bounded body is drained, then a same-origin `Origin` and the
session CSRF token are proven. No new bearer exists; the Capture ingestion
token authorises nothing here. The route shares the 16 KiB knowledge body
budget. `Idempotency-Key` is not supported and is refused
(`unsupported_idempotency_key`, 400) before any child. The route is absent from
`IDEMPOTENT_POST_ROUTES`: no response is cached, nothing is written to
`activity.json`, and a repeat is a new check the user asked for rather than a
replayed receipt.

Success is exactly:

```json
{"data": {"binding": ..., "observation": ...},
 "meta": {"outcome": "observation_recorded"}}
```

A 200 is sent only for a **confirmed** save whose observation is non-null with
`binding_state == "unchanged"`. Anything else is reported as an unconfirmed
save (below) rather than announced as recorded.

## Read the saved observation

```
GET /api/v1/knowledge/captures/observation
    ?workspace_uid=<UUID>&capture_id=<ID>&capture_revision=<integer>
```

Same exact-path rule as above, applied to the raw request-target. Exactly three
single-valued, nonempty query keys; ordinary query encoding and any key order
are accepted; a duplicate, unknown or missing key is refused
(`invalid_query`, 400) before any Store read. The revision is a canonical
base-10 unsigned integer — `0`, or digits with no leading zero — within the
released revision range; `007`, `+1`, `1.0`, `0x1` and a sign or space are
refused as shapes. The admitted identity is then passed through the same
three-field parser the POST body uses, so the two routes cannot drift.

The read runs under the loopback `Host` admission the whole GET surface already
runs under. It introduces no token and no header, and no other GET gains any
permission from it existing. It never starts a child, never reads or consults
the operator's verifier configuration, never saves, and never activates a
storage format. Ordinary Store recovery semantics still apply: this is not a
promise of zero physical I/O while a journal is being recovered.

Success is exactly:

```json
{"data": {"binding": ..., "observation": ...},
 "meta": {"outcome": "observation_ready"}}
```

Having kept nothing is a **200 with `observation: null`**, not a 404: the
Capture was found, and "no saved check" is the answer.

## The data shape

```
binding:     {workspace_uid, capture_id, capture_revision}
observation: null | {accepted_at, checked_at, binding_state, result}
```

`binding_state` is `unchanged` or `changed`. `unchanged` carries the original
validated closed verification result, and `checked_at` equals that result's
`checked_at`. `changed` carries `result: null` and the two historical times
only — it never shows old evidence over a binding that moved.

Both times are the original calendar-valid values and are never clamped to the
reading clock. No stored record, request envelope, capture digest, verifier
command, environment or connection alias appears on the wire. Nothing is added
to the Capture list projection or to the released verification response, so an
untouched old client sees exactly what it saw before.

## Refusals

The released verification status mapping is reused unchanged:

| Condition | Status | Code |
| --- | --- | --- |
| Unknown Capture | 404 | `unknown_capture` |
| Binding, revision, authority or gate conflict | 409 | released code |
| Missing verifier, unsettled cleanup, child not started | 503 | released code |
| Child outcome undescribable, result refused | 502 | released code |
| Malformed body | 400 | `invalid_body` / `invalid_request` |
| Malformed target | 404 | `not_found` |
| Malformed query (read route) | 400 | `invalid_query` |
| Unsupported backend | 409 | `knowledge_backend_unsupported` |

Two refusals are new to this surface:

- **`observation_save_unknown` (503)** — the check ran and its record could not
  be *confirmed* saved. The constant message says so explicitly. It does **not**
  claim that nothing was written: after a fault at an unknown commit point no
  such claim is available, and journal recovery may still complete the write.
  No automatic second child, retry or invented receipt follows it. The client's
  recourse is an explicit read, and a `null` read result must not be presented
  as proof that the failed save never committed.
- **`observation_read_unavailable` (503)** — an ordinary storage read failure,
  or a persisted record this owner will not project. Corrupt history is refused
  rather than flattened into "no observation", and is never reported as a
  changed binding. Every internal spelling collapses into this one closed code.

  The read route's storage catch covers the whole runtime call, so a refusal
  raised while the outer transaction is entered, while a later re-admission
  loads a document, or while that transaction is exited is answered here rather
  than escaping as a 500. It names actual classes — `StoreCorruptError`,
  `StoreLockedError`, `StoreExternalChangeError`, `StoreAdoptionConflictError`,
  the observation model's own refusal and `OSError`.

  The two synchronisation refusals are folded in deliberately: the runtime
  already answers some direct loads of an external change as an unreadable
  history, so letting the same condition surface as a 409 from a different
  phase of the same read would make the outcome depend on where the store
  happened to notice it. This is this route's own boundary, not a change to
  shared dispatch — every released route keeps `store_sync_required` and its
  409 exactly as it shipped, and no other endpoint gains any handling from
  this catch existing.

`unknown_capture` (404) is deliberately distinct from `not_found` (404). Only
`not_found` means "this server predates the route"; a Capture that is unknown is
a different answer and must not be read as an old-server diagnosis.

Every refusal is closed: one code, one constant message, and at most the *name*
of one of the three published fields. No raw exception text, stack, path,
digest, identifier, command, environment value or child byte travels, and the
handler writes no log line for any request target or body.

## Compatibility

A server built before this feature answers both targets with the existing
`not_found` 404, which is what a client uses to detect it. Adding these routes
changes no existing route's admission, parsing, response or ordering; the two
new tables entries are appended, never inserted, and each matches only its own
exact path.
