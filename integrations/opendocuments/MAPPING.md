# OpenDocuments retrieval mapper

Production mapper from a frozen OpenDocuments `/api/v1/chat` body onto the
existing closed wire schema `workstack.capture-retrieval.v1.1`. It is not a
new RAG engine, not an HTTP client, not source verification, and not the A0
fixture helper.

A0 `contracts/fixtures/opendocuments-adapter/mapping.py` is a **reference
only**. Its excerpt, heading-path and locator fields are **not** the
production output. This module emits the Capture retrieval extension, then
hands that document to `workstack.capture_retrieval.validate_retrieval_payload`.

Owned files: `retrieval_mapper.py`, `retrieval_mapper_fields.py`, this
`MAPPING.md`. There is no `__init__.py` here.

## Settled call

```python
from integrations.opendocuments.retrieval_mapper import MappingError, map_retrieval

wire = map_retrieval(
    chat,
    request_id=ledger_request_id,
    result_limit=limit,
    source_catalog=catalog,
)
```

`map_retrieval` returns the closed **wire** object, or raises `MappingError`
whose `code` / `str` / `repr` / `details` carry **only that code**. Submitted
values never appear in the error.

The return value is the extension a caller feeds to
`validate_retrieval_payload`. It does **not** include derived projection
fields (`origin`, `origin_state`, `reported_origin`, `capture_source_type`,
`version_state`). Those exist only after the existing validator runs, and
only from caller-supplied `RetrievalVerification` — which this lane never
assembles from chat.

## Trusted catalog (narrow, caller-supplied)

The catalog is an **admitted index-identity allowlist**. It is not built from
chat, `sourcePath`, titles, hashes or connector source types. It does not
prove current origin.

Shape, required and closed:

| Part | Rule |
| --- | --- |
| Container | nonempty `Mapping`, at most 64 entries |
| Key | upstream OpenDocuments `documentId` string (indexed workspace id) |
| Value keys | exactly `document_ref`, `source_type`, `display_title` |
| `document_ref` | nonempty string; must satisfy the existing opaque-ref grammar when sealed |
| `source_type` | `notion.page` or `nas.file` only |
| `display_title` | nonempty string; must pass the **existing** title rules when sealed |

Any other value key (`metadata`, `path`, `source_version`, hashes, URLs) is
`invalid_catalog`. An empty catalog is `invalid_catalog`. Catalog titles are
not rewritten here; a title the existing validator would refuse fails closed
as that validator's code, still without echoing the title.

`document_ref` is the Work Stack handle the host already admitted. It is not
the upstream UUID, not a Notion page id, and not a filesystem path.

## What is copied, derived, or ignored

| Input | Fate |
| --- | --- |
| Caller `request_id` | Canonical UUID on the wire. Chat cannot assert request authority. |
| Chat `queryId` | Becomes `query_id`. Must be distinct from `request_id`. Grammar/credential checks are the existing validator's. |
| Chat `confidence.level` / `.score` | Copied when already a bounded finite level/score. Malformed values fail `invalid_confidence`; nothing invents low/high/0/1. `reason` is ignored. |
| Chat `sources[]` | Walked in order, at most 100 items. |
| Catalog entry | `source_type`, `title` (`display_title`), `document_ref`. |
| Valid `{documentId}_chunk_{n}` whose UUID prefix equals that `documentId` | Lowercased as `chunk_ref` (indexed chunk identity, not a Notion block id). |
| Known web/external identity (`documentId` `web-search` / `web_search` / `websearch`, or `chunkId` `web_{n}`) | Omitted; `truncated=true`. |
| `documentId` absent from the catalog | Omitted; `truncated=true`. |
| Chunk that belongs to another document, missing/malformed chunk id | Omitted; `truncated=true`. |
| Repeated `(document_ref, chunk_ref)` | First kept; later dropped; `truncated=true`. |
| Count above `min(result_limit, 10)` | Overflow dropped in order; `truncated=true`. |
| UTF-8 encoding above 16 KiB | Later evidence dropped until the extension fits; `truncated=true`. One item that cannot fit is `extension_too_large`. |
| Pruning that leaves a mixed admitted set with one document | `mixed_evidence_not_representable`. |
| `sourcePath`, `content`, `answer`, `headingHierarchy`, `reason`, `password`, hashes, `source_version` from chat or index metadata | Ignored. Never become `source_version`, `indexed_digest`, summary, origin, or Task actions. |
| `source_version` / `web_url` on the wire | Always `null`. No origin attestation in this lane. |
| `indexed_digest` | Not emitted. |

If every source is omitted, the mapper raises `no_admitted_evidence` rather
than emitting empty evidence.

`answer_scope` is `single_source` when remaining evidence names one
`document_ref`, and `synthesized` when it names more than one. Mixed
documents are never attributed to the first source.

## Mixed evidence and the caps

Whether the answer drew on more than one approved document is read from
**every admitted draft, before any pruning** — including drafts the result
cap dropped. Only admitted drafts count: an omitted web, out-of-catalog,
forged-chunk or malformed hit never contributes a second document identity,
so excluded evidence cannot manufacture mixed-source authority.

The caps and the selection are unchanged: 16 KiB, `min(result_limit, 10)`,
stable source order, first occurrence wins, byte overflow drops from the
tail. There is no alternative multi-document selection here.

What changes is the outcome when the surviving selection can no longer carry
what admission saw. If admission was mixed but the set left after the result
cap — or after any byte-budget drop — names a single `document_ref`, the
mapper raises `mixed_evidence_not_representable`. `single_source` would
attribute the answer to whichever document happened to sort first, and
`synthesized` would claim evidence the wire no longer carries; a second
source is never fabricated to escape the refusal. Both caps are reachable
this way on their own: `result_limit=1` over a two-document chat, and a
ten-hit chat of eight-then-two documents whose 500-code-point titles push the
compact UTF-8 wire past 16 KiB.

Unchanged single-document evidence is unaffected: it still maps and still
reports `single_source`, byte pruning included.

## Refusal codes (mapper)

`invalid_catalog`, `invalid_result_limit`, `invalid_uuid`, `invalid_chat`,
`invalid_query_id`, `invalid_confidence`, `query_id_not_distinct`,
`no_admitted_evidence`, `mixed_evidence_not_representable`,
`extension_too_large`, plus whatever closed code the
existing payload validator returns when a catalog title or ref fails its
rules.

This mapper is not a 100% injection detector. It keeps untrusted text off the
wire by not copying it, and relies on the existing validator for display-text
and handle grammar.

## Remaining wiring (not this lane)

- HTTP client / `POST /api/v1/chat`
- `source_access` / NAS path verification / origin attestation
- Central quality registration of `integrations/opendocuments/**/*.py`
- Store, API, UI, request ledger, packaging/dependency distribution
- Any future verifier that could fill `source_version`
