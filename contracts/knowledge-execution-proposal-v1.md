# Knowledge execution proposal v1

Status: **trusted internal projection.** `workstack/knowledge_execution_proposal.py`
takes one untrusted driver stdout payload plus already-admitted request
metadata and returns a closed `workstack.knowledge-import.v1` envelope. It is
not an HTTP route, not a ledger write, not an instance guard, not a child
transport and not an executor. No existing issuer, ledger, store, schema,
server, route, driver or frontend module is edited by this slice.

**Caller / trust boundary.** The caller is an in-process owner composition that
has already admitted the request. It supplies `request_id`, `connection_alias`,
`result_limit` and `now` as trusted structural facts; this module does not look
up a ledger, does not check expiry, policy, Task binding or attempt spend, and
does not mint a token. `payload` is the stdout of one driver exchange and is
untrusted. A passing projection never proves the driver was honest, that the
request is still open, or that the owner may complete it. The released importer
remains the sole authority on submission.

## Frozen API

```
validate_execution_proposal(
    payload: bytes,
    *,
    request_id: str,
    connection_alias: str,
    result_limit: int,
    now: str,
) -> dict
```

| Argument | Trust | Bound |
| --- | --- | --- |
| `payload` | untrusted | exact `bytes`; decoded by `decode_strict_json` with `maximum_bytes=64*1024` |
| `request_id` | trusted | `canonical_uuid` |
| `connection_alias` | trusted | released alias grammar (`CORPUS_REF_RE`, the same pattern `CONNECTION_ALIAS_RE` aliases), `MAX_CORPUS_REF_CHARS` |
| `result_limit` | trusted | existing `1 .. 10`; a JSON boolean is not an integer |
| `now` | trusted | strict RFC3339 via `parse_rfc3339`, at most `MAX_TIMESTAMP_CHARS` |

`now` is the caller's clock. Nothing here reads a wall clock, opens a network,
writes a log, or claims authority.

## Sequence

1. Admit the four trusted arguments with the released primitives above.
2. Require `type(payload) is bytes`.
3. `decode_strict_json(payload, maximum_bytes=64*1024)` — duplicate keys, non-finite
   numbers, over-deep JSON, oversize input and undecodable UTF-8 refuse with the
   decoder's closed `KnowledgeRequestError` codes.
4. `parse_import_envelope` on the decoded object. Unknown fields, missing fields,
   title / normalized / retrieval shape and evidence rules are the released
   import and retrieval validators; this module does not restate them.
5. Envelope `request_id` must equal the trusted identity, or
   `KnowledgeImportError request_id_mismatch`.
6. `len(items)` may not exceed the issued `result_limit`
   (`result_limit_exceeded`). The envelope's own 1..10 item bound still applies.
7. `stage_import_item` for each item, with the trusted `request_id`, alias and
   `now`, **and no `RetrievalVerification`**. Manual `knowledge.answer` and
   `reported_unverified` / `unreported` semantics are unchanged.
8. Rebuild `{schema, request_id, items}` from `IMPORT_SCHEMA`, the trusted
   `request_id`, and each staged item projected onto the **public import
   wire**: item `item_id` / `title` / `normalized` / `retrieval`, normalized
   `summary` / `context` / `action_items` / `tags`, and each action
   `title` / `detail` / `priority` / `due`. Staging's `digest_material()` is
   the internal material the completion digest covers, and its normalized
   projection additionally carries the Capture model's generated action `id`
   (`A-<16 hex>`); that identifier is a stored-record field, not a field of
   `workstack.knowledge-import.v1`, so it is dropped here. The released
   importer mints the identical `id` again from the same admitted action at
   explicit import, so nothing is lost by dropping it.
9. Compact-encode the whole rebuilt envelope (`sort_keys`, UTF-8) **after**
   that public projection. If it exceeds 64 KiB, refuse `request_too_large`
   even when the input fitted.

## Return

```json
{
  "schema": "workstack.knowledge-import.v1",
  "request_id": "<trusted request_id>",
  "items": [
    {"item_id": "…", "title": "…", "normalized": {"…": "…"}, "retrieval": {"…": "…"}}
  ]
}
```

The items are the public import wire, not the driver's object, not a Capture
packet and not staging's internal digest material. Packet internals are not
returned: no `source`, `source_key`, `provenance`, `verification`, `origin`,
path, raw payload bytes, or generated action `id`. A claimed `origin` or
verified version in the payload cannot become trusted metadata: there is no
verifier, and those fields are not among the public keys rebuilt above.

Because the returned envelope is exactly the shape the released importer and
the browser's closed parser admit, a proposal this module produced can be
re-parsed by either without widening their closed field sets. An internal
field the Capture model gains later is dropped by that rebuild rather than
reaching a caller as an unknown field.

## Refusals

Trusted-argument and import/staging refusals are `KnowledgeImportError`.
Decoder refusals from `decode_strict_json` remain `KnowledgeRequestError`.
Known closed decoder and import exceptions propagate; they are not rewritten
into a new family. `details` carries at most a closed field *name*. No
submitted value, path, key, title, identifier or traceback text is echoed.

| Code | When |
| --- | --- |
| `invalid_uuid` | trusted `request_id` is not the canonical non-nil UUID |
| `invalid_alias` | trusted `connection_alias` fails the released alias grammar |
| `invalid_number` / `out_of_range` | trusted `result_limit` is not an int in `1 .. 10` |
| `invalid_timestamp` | trusted `now` is not strict RFC3339 |
| `invalid_import` | payload is not `bytes` |
| `request_id_mismatch` | envelope `request_id` ≠ trusted identity (also a retrieval that names another request, via staging) |
| `result_limit_exceeded` | more items than the issued `result_limit` |
| `request_too_large` | input > 64 KiB, or the canonical rebuilt envelope > 64 KiB |
| decoder / import / retrieval / Capture codes | duplicate keys, non-finite, depth, unknown/forbidden fields, title/HTML/credential/path gates, … |

This slice does not execute a provider, persist a claim, spend an attempt,
open a child, or change CSRF, thresholds, schema 6 or `knowledge.json` v1.
