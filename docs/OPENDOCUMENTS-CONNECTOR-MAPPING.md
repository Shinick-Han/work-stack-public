# OpenDocuments connector mapping (A0, proposed)

Status: **proposed mapping**. This document is not C1, not a production schema,
not customer acceptance, and not permission to call a live OpenDocuments
service. C1 is drafted concurrently; do not import unreviewed C1 files here,
and do not treat this format as the host contract.

Revision: this replaces the A0 candidate `ebe0d0c47dc3c81f659d24161ee3d562de9a4cbd`,
which received CHANGES REQUESTED in an independent review. Findings A0-1 through
A0-6 are addressed here and in `contracts/fixtures/opendocuments-adapter/`. The
schema version stays `0` because the shape is still a proposal, not a freeze;
the field names below are **not** compatible with the previous candidate.

North star: T-0042. Authority docs live outside this checkout:
`C:/ws-orca/workstack-pilot/docs/WORKSTACK-CONNECTOR-EXTENSIBILITY-REVIEW-2026-09-08.ko.md`
and
`C:/ws-orca/workstack-pilot/docs/WORKSTACK-NORTH-STAR-IMPLEMENTATION-PLAN-2026-09-08.ko.md`.

Product copy below is English. Fixture text is synthetic and sanitized.

## 1. Verified revisions

| Tree | SHA | Notes |
|---|---|---|
| Work Stack checkout | `0fb998ebf4b5fb4fd17cfddf21837c22f2c94aae` | Packet source base; `git rev-parse HEAD` matched. |
| OpenDocuments upstream snapshot | `f3aba15f0161f1730e746de47a2ffd1e53cbea44` | `C:/ws-orca/workstack-pilot/.artifacts/opendocuments-review-20260908/upstream`. Matches the first-customer review. |

No OpenDocuments process was started. No customer, cloud, or original-document API was called. No original corpus was indexed.

## 2. Roles (observed vs proposed)

OpenDocuments is a **retriever** over an already indexed workspace. Notion and
local/NAS files are **sources** that OpenDocuments ingested. Work Stack remains
the Task/reference host. Agent execution is out of scope.

Proposed adapter ID: `opendocuments` (retriever). A Work Stack **connection
instance** binds one OpenDocuments workspace. The retriever name must not replace
source document identity.

The existing Markdown search contract (`docs/KNOWLEDGE-SEARCH.md`) still requires
vault-relative `.md` paths, line spans, and SHA-256. OpenDocuments hits cannot
be stuffed into `provider: markdown-vault` without lying about locators. C1 must
own the host shape; this packet only shows how an OpenDocuments `/chat` body
could feed a later host contract.

Capture (`workstack/capture.py`) and Microsoft URL allowlists are unchanged.
This packet does not add `opendocuments` to any allowlist.

## 3. Observed OpenDocuments fields

Paths are relative to the frozen upstream tree.

### 3.1 HTTP search is `/chat`, not a collection-filtered search API

`packages/server/src/http/routes/chat.ts` `POST /api/v1/chat` (`requireScope('ask')`)
accepts `{ query, profile?, conversationId?, workspaceId? }` and returns the
RAG `QueryResult`. There is **no collection id on the chat body**. Collections
exist as a separate CRUD API (`packages/server/src/http/routes/collections.ts`)
and do not constrain retrieval.

There is no document-search HTTP route. CLI `packages/cli/src/commands/search.ts`
searches chunks locally (no LLM) and is not the adapter transport. Scope name
`search` exists on API keys but is unused by `/chat`.

Non-stream `/chat` returns `ragEngine.query(...)` as JSON. Stream
`POST /api/v1/chat/stream` is a different persistence path (creates a
conversation even when `conversationId` is omitted). **Proposed first adapter:
non-stream `/chat`, omit `conversationId`.**

### 3.2 Workspace auth beats body.workspaceId

`packages/server/src/http/workspace.ts` `resolveRequestWorkspaceId`:

1. If the API key record has `workspaceId`, that workspace is used.
2. Else `body.workspaceId` (id or name) if it exists.
3. Else configured default workspace.
4. Else `ensureDefault()`.

`packages/server/src/http/middleware/auth.ts`:

- Personal mode: no key, `hasScope` always true.
- Team mode: `X-API-Key` or `opendocuments_session` cookie. Invalid/expired → 401.
- `requireScope('ask')` for chat; `requireScope('document:read')` for
  `GET /api/v1/documents` and `GET /api/v1/documents/:id`.

Default key scopes (`packages/core/src/auth/api-key.ts`): viewer gets
`ask`, `search`, `document:read`. An ask-only key is possible if created with
`scopes: ['ask']`. **Ask-only cannot read `source_version` / `content_hash`.**

Readiness errors from chat: `503 MODEL_UNAVAILABLE`, `409 CORPUS_EMPTY`.
Empty query → `400`. Missing conversation → `404`.

### 3.3 Response fields (`QueryResult` / `SearchResult`)

Core (`packages/core/src/rag/engine.ts`, `packages/core/src/ingest/document-store.ts`):

```text
QueryResult: queryId, answer, sources[], confidence{score,level,reason}, route, profile
SearchResult: chunkId, content, score, documentId, chunkType, headingHierarchy[],
              sourcePath, sourceType, contextualPrefix?, parentSection?
```

Client typings (`packages/client/src/index.ts:6-15`) mark `chunkId` optional even
though the core `SearchResult` requires it
(`packages/core/src/ingest/document-store.ts:42-50`). The mapper therefore
treats an absent chunk id as a compatibility hazard and omits the hit. Chat does
**not** return `content_hash`, `source_version`, `indexed_at`, or origin
reachability.

`documentId` is `randomUUID()` on ingest. `chunkId` is
`{documentId}_chunk_{position}` (see `storeChunks`). Using a chunk id as the
Work Stack document id would collapse many excerpts onto fake documents and
break later reads.

**These UUID/`{uuid}_chunk_{n}` forms describe indexed OpenDocuments workspace
hits only.** The RAG engine can merge web-search results into the same
`sources[]` array with a synthetic identity of `documentId: "web-search"` and
`chunkId: "web_{n}"` (`packages/core/src/rag/engine.ts:409-430`). The `balanced`
profile uses web fallback and `precise` enables web search when a provider is
configured (`packages/core/src/rag/profiles.ts:44-71`). Those hits are not
workspace documents, carry no index identity, and are **omitted** by this
mapping with the reason `non_workspace_result`; the same omission covers a
web-shaped `chunkId` presented alongside a workspace-shaped `documentId`. A
later fake-HTTP adapter (A1/M1) must pin or constrain the profile so this
behaviour is bound rather than merely filtered after the fact.

`parentSection` may replace `content` when parent-doc retrieval is on
(`packages/core/src/rag/parent-doc.ts`). That is still excerpt text, not a
version token.

Confidence (`packages/core/src/rag/confidence.ts`) is a weighted mix of
retrieval, rerank, source count, and keyword coverage. It is **not** accuracy
and must not be shown as a percent-correct.

Generated `answer` is model prose. It can contain sensitive text and must not
become a Reference identity, locator, or Capture payload.

### 3.4 Document vs chunk vs source

| ID | What it is | Where it appears on `/chat` |
|---|---|---|
| `documents.id` | Index document UUID | `sources[].documentId` |
| `chunkId` | Retrieval unit `{uuid}_chunk_{n}` | `sources[].chunkId` |
| `source_path` | Ingest locator | `sources[].sourcePath` |
| Notion page id | Source identity inside `notion://{pageId}` | path only; not a native block id |
| Local/NAS file | Absolute path from `opendocuments index` | `sourcePath` is a filesystem path |

Notion connector (`plugins/connector-notion/src/index.ts`): `sourcePath` is
`notion://{page.id}`. `rootPageId` is declared on the config interface and UI
copy, but `setup` only reads `token` and `discover` searches all accessible
pages with **no root filter**. Flattened block text is parsed as Markdown-like
chunks; **Notion block ids are not preserved**.

Local index (`packages/cli/src/commands/index-cmd.ts`) sets `sourceType: 'local'`
and `sourcePath` to the resolved filesystem path (drive letter / UNC included).
That path must not be stored in Work Stack SSOT or UI as a live opener.

**`sourceType` is upstream-controlled metadata and is never trusted to admit a
`sourcePath`.** The mapper classifies the locator from the path alone. A declared
type that disagrees with the parsed path (for example a Notion `sourceType` with
a `Z:\…` path, or `sourceType: 'local'` with a `notion://` path) redacts the
locator entirely and records
`source.type_locator_agreement: "mismatch"`. A `notion://` path is accepted only
when the remainder is a valid page-id form (32 hex or dashed UUID).

**URI schemes are case-insensitive, so the scheme is parsed exactly once and
compared lowercased.** `HTTPS://…`, `HtTpS://…`, and `https://…` take the same
branch, and so do `GDRIVE://` and `gdrive://`. No spelling of a recognised
scheme can fall through to the display-label branch, which is what would
otherwise let a credential or an `api_key` query survive inside `locator.value`
and the title fallback. Only the schemes the frozen upstream actually emits are
recognised; no further protocol is inferred, and an unrecognised scheme that
embeds `userinfo@` is redacted rather than reduced to a basename.

**Every `sourceType` value the frozen upstream can produce is reconciled**
(`DECLARED_SOURCE_TYPES` in `mapping.py`), each entry carrying the file and line
that emits it:

| Declared `sourceType` | Emitted by | `sourcePath` class it may carry |
|---|---|---|
| `local` | `packages/cli/src/commands/index-cmd.ts:78,106`, `packages/server/src/mcp/server.ts:289` | filesystem path or bare label |
| `upload` | `packages/server/src/http/routes/documents.ts:84` | `upload:{hash}:{name}` |
| `web` | `packages/core/src/rag/engine.ts:427` | `http(s)` (already omitted as `non_workspace_result`) |
| `@opendocuments/connector-notion` | `manager.ts:196-204` + `plugins/connector-notion/src/index.ts:72` | `notion://` |
| `@opendocuments/connector-gdrive` | `plugins/connector-gdrive/src/index.ts:108` | `gdrive://` |
| `@opendocuments/connector-confluence` | `plugins/connector-confluence/src/index.ts:76` | `confluence://` |
| `@opendocuments/connector-github` | `plugins/connector-github/src/index.ts:59` | `github://` |
| `@opendocuments/connector-s3` | `plugins/connector-s3/src/index.ts:99,133` | `s3://` **or** `gcs://` — one plugin, two providers |
| `@opendocuments/connector-swagger` | `plugins/connector-swagger/src/index.ts:49` | `swagger://` |
| `@opendocuments/connector-web-crawler` | `plugins/connector-web-crawler/src/index.ts:115` | `http(s)` |
| `@opendocuments/connector-web-search` | `plugins/connector-web-search/src/index.ts:23` | `http(s)` |

A nonempty declaration outside that table cannot be reconciled with anything, so
the locator is redacted with `reason: unreconcilable_declared_source_type` and
`kind: unknown` rather than retained on a guess. An **absent** declaration is
different from an unreconcilable one: it records `type_locator_agreement:
"unverified"` and keeps the parsed locator, so partial-but-valid evidence stays
usable. `sourceType` can still only lose information; it never admits a path.

### 3.5 Source version and content hash

Pipeline (`packages/core/src/ingest/pipeline.ts`): SHA-256 of ingested bytes →
`updateContentHash` (full 64-hex). Connector `discovered.contentHash` is stored
as `source_version` (`packages/core/src/connector/manager.ts`). For Notion that
discover field is `last_edited_time`, not a content hash. Either column may be
null. Restore clears both.

`GET /api/v1/documents/:id` returns the store row (`SELECT *`), including
`content_hash` and `source_version` when present, and requires `document:read`.
Chat hits do not. An excerpt digest of `sources[].content` is **not** the
document version.

Index snapshot ≠ origin current. Proposed UI copy:
**Indexed snapshot · Source not verified**.

### 3.6 Query logging

`persistQueryLog` in `chat.ts:109-121` is **called on every successful non-stream
`/chat`** with the raw, untrimmed `body.query`, even when `conversationId` is
omitted. Stream logging also records the query and may create a conversation.
There is no request switch that suppresses it, so the adapter cannot turn it off.

What the code proves is *attempted* logging, not persistence: `persistQueryLog`
(`chat.ts:20-39`) catches database errors and returns normally, so an insert can
fail silently. The mapping therefore reports two separate flags —
`query_logging_attempted_by_route: true` and
`query_log_persistence_confirmed: false` — instead of the previous absolute
`query_logged_upstream: true`.

User-facing disclosure stays conservative and unchanged in effect: **the query
will be sent to an upstream route that attempts to store it, and the adapter
cannot suppress that.** Do not promise “query not stored”, and do not claim
proof of storage either. Fake HTTP can prove neither persistence nor absence.

## 4. Proposed mapping (not frozen)

Fixture format: `workstack.opendocuments-adapter.proposed` / `schema_version: 0`.
Executable rules: `contracts/fixtures/opendocuments-adapter/mapping.py`.

| OpenDocuments | Proposed Work Stack evidence | Status |
|---|---|---|
| Adapter / retriever | `retriever.id = opendocuments`, role `retriever` | **PROPOSED** |
| Work Stack connection | `connection_id` (host-issued, ≤128 chars). OD workspace id stays in adapter config, not in the Reference row | **PROPOSED** (D1) |
| `sources[].documentId` | `indexed_identity.indexed_document_id` (UUID only) | **PROPOSED** |
| `sources[].chunkId` | `indexed_identity.indexed_chunk_id`; **required**, must match the document, never a document identity | **PROPOSED** |
| missing / malformed / cross-document `chunkId` | candidate **omitted** (`missing_chunk_id`, `invalid_chunk_id`, `mismatched_chunk_id`) | **PROPOSED** (D2) |
| `headingHierarchy` | `index_heading_path.segments`, `addressable: false`; display/title only | **PROPOSED** (D2) |
| chunk position | the only address: locator `type: chunk_index`, `source_locator: false` | **PROPOSED** (D2) |
| line range | **unsupported** (not present) | observed |
| Notion block id | **unsupported** (flattened away) | observed |
| `notion://{valid page id}` | source locator `type: uri` | **PROPOSED** |
| `notion://{anything else}` | `type: redacted`, `reason: malformed_notion_page_id` | **PROPOSED** |
| `sourceType` disagreeing with `sourcePath` | `type: redacted`, `reason: source_type_locator_mismatch`, `kind: unknown` | **PROPOSED** |
| `sourceType` outside the frozen upstream's producers | `type: redacted`, `reason: unreconcilable_declared_source_type`, `kind: unknown` | **PROPOSED** |
| absent / non-string `sourceType` | parsed locator kept, `type_locator_agreement: "unverified"` | **PROPOSED** |
| `http(s)://` with userinfo | `type: redacted`, `reason: credentialed_url` (never retained) | **PROPOSED** |
| `http(s)://` otherwise | `type: uri`, scheme+host+port+path only; query and fragment dropped | **PROPOSED** |
| `gdrive://`, `confluence://`, `github://`, `s3://`, `gcs://`, `swagger://` | `type: opaque`, value is the scheme name only; the identifier is dropped | **PROPOSED** |
| local/NAS `sourcePath` | `type: display_label` (safe basename) or `document` when hostile | **PROPOSED** |
| source-qualified document identity | `source.identity.status = unknown` — `/chat` cannot prove it | **PROPOSED** (D1) |
| web-search / non-workspace hits | **omitted** (`non_workspace_result`) | **PROPOSED** (D7) |
| `content` | bounded excerpt (1200 chars), control chars stripped, `trust: external_reference` | **PROPOSED** |
| `answer` | discarded; not a Reference | **PROPOSED** (D6) |
| `confidence` | `engine.confidence.kind = engine_relevance` | **PROPOSED** |
| chat-only version | `source_version.status = unavailable`, `basis: chat_response` | **PROPOSED** (D3) |
| `GET /documents/:id` version | `index_revision` / `index_hash` or still `unavailable` if null/malformed, `basis: document_read` | **PROPOSED** (D3) |
| origin verify / open / source identity | unsupported capabilities | observed |
| `collection_id` on request | refuse `collection_filter_unsupported` | **PROPOSED** (D4) |
| `conversationId` | refuse `conversation_persistence_out_of_scope` | **PROPOSED** |
| body `workspaceId` from Work Stack | refuse; bind via connection/API key | **PROPOSED** (D4) |
| query log | `query_logging_attempted_by_route: true` + `query_log_persistence_confirmed: false` | observed (D5) |
| verification | `indexed_snapshot_unverified`, `origin_current: false` | **PROPOSED** |

Unknown adapter-request fields fail closed. Extra upstream source fields
(`password`, `parentSection`, …) are dropped, not forwarded.

### 4.1 Two identities, kept apart

`indexed_identity` = retriever `opendocuments` + Work Stack connection id +
`documents.id` UUID + `{uuid}_chunk_{n}`, tagged
`identity_scope: "opendocuments_index"`. This is an **indexed / retrieval**
identity: `documents.id` is a `randomUUID()` created inside the OpenDocuments
workspace (`document-store.ts:91-100`) and `chunkId` is derived from it
(`document-store.ts:144-159,179-186`). It is **not** the Notion page identity and
**not** the NAS file identity.

`source.identity` is the separate, source-qualified document identity. It is
always `status: "unknown"` in A0, because `/api/v1/chat` cannot prove it: a
`sourcePath` is upstream-asserted metadata, not an authenticated origin
reference. Proving it would require trusted connection configuration (which
adapter instance points at which origin tenant) **plus** a strictly validated
source locator. A0 models no connection configuration, so it claims nothing.

C1 must keep four things distinct — adapter definition, connection instance,
source-qualified document identity, and request/retrieval provenance. The A0
shape above is a proposal about the last two only and cannot settle that
contract. The existing display precedent
`sourceQualifiedIdentity(connectorId, sourceId, documentKey)`
(`frontend/src/features/knowledge/knowledgeSourceView.ts`) is *not* satisfied by
`indexed_identity` alone.

### 4.2 Bounds are enforced, not just declared

`proposed-normalized.schema.json` is closed evidence: `additionalProperties:
false` everywhere, `if/then/else` on `ok` so a success body cannot carry an
error (and vice versa), a closed per-candidate `locator`, and length/range caps
on every string and number. The mapper enforces the same caps before emitting —
request id (UUID), `connection_id` (≤128, trimmed, control-free), `query`
(≤1000), route/profile (≤64), confidence reason (≤256), title (≤240), excerpt
(≤1200), heading segments (≤8 × ≤120), source type (≤120), source URI (≤512),
version (≤128), content hash (exactly 64 lowercase hex), and at most 100
`sources[]` per response. Every catalog output is validated against the schema
by the test suite, including the hostile overlong/control-character case.

The bound the mapper enforces and the bound the schema declares are the same
bound, checked **before** the candidate is constructed:

* **Chunk position.** The schema caps `locator.position` at `999999999` and the
  chunk-id suffix at nine digits. `CHUNK_ID_RE` now accepts at most nine ASCII
  digits and `_map_source` rejects anything above `MAX_CHUNK_POSITION`, so a
  matching `{uuid}_chunk_1000000000` is omitted as `invalid_chunk_id` instead of
  becoming a candidate the closed schema rejects. A document id that merely
  *looks* like an out-of-range chunk id is still reported as
  `chunk_id_used_as_document_id`.
* **Whole-string identity.** In Python `$` also matches immediately before a
  trailing line feed, so `"{uuid}\n"` used to satisfy a `^…$` UUID check and
  then fail the schema's `format: uuid`. Every identity pattern is now anchored
  `\A…\Z`, and `[0-9]` replaces `\d` so non-ASCII digits cannot reach `int()`.
  `connection_id` is additionally rejected for any C0 control, which is what its
  error message already claimed.
* **Exception-safe numbers.** JSON has no integer ceiling, so `float()` on a
  decoded score could raise `OverflowError`. Integers are now clamped without
  any float conversion, and non-finite floats and booleans normalise to `0.0`.
  A score is advisory metadata; an unusable one must not abort the mapping.

Schema validity is asserted for a bounded corpus of accepted boundary and
adversarial projections (`BOUNDARY_PROBES`), not only for the frozen catalog, so
"every mapper output is valid" is tested rather than asserted.

## 5. Coordinator decisions requested

These are not silently frozen. Alternatives are listed only where the fixture
had to pick one shape.

**D1 — Document identity.** Recommendation: index UUID as
`indexed_identity.indexed_document_id`, explicitly scoped to the OpenDocuments
index, with source-qualified identity kept separate and `unknown` until trusted
connection configuration plus a validated source locator can supply it.
Alternative A: `sourcePath` as the only id (leaks NAS paths; unstable for
uploads; upstream-controlled). Alternative B: retriever-qualified chunk id
(wrong grain). Alternative C: assert source identity from `sourcePath` alone
(rejected — that is exactly the trust A0-1 removed).

**D2 — Excerpt locator.** Recommendation: require a well-formed,
document-matching `chunkId` and address only by opaque `chunk_index` marked
`source_locator: false`; omit any hit that lacks one. Heading hierarchy is
descriptive index metadata (`index_heading_path`, `addressable: false`) and never
makes a hit attachable or pinnable. Do not invent Markdown line ranges.
Alternative A: keep such hits as explicitly search-only/unattachable rows with an
`unsupported` locator and no evidence-attachment capability — viable, but it
needs a host-side "unattachable candidate" concept that C1 does not yet define,
so A0 omits instead. Alternative B: treat all OD locators as `unsupported` until
C1 lands.

**D3 — Version on M1.** Recommendation: ask-only `/chat` with version
`unavailable`. Optional `document:read` metadata is a later capability, still
not origin-current. Alternative: require `document:read` from day one.

**D4 — Corpus isolation.** Recommendation: one OpenDocuments workspace per Work
Stack connection. Collection filter is unsupported. Alternative: client-side
filter after `/chat` (not isolation; rejected here).

**D5 — Query logs.** Recommendation: disclose that the route always *attempts* to
store the query and that the adapter cannot suppress it; do not claim no-log and
do not claim proven storage. Alternative: wait for upstream/customer policy
before any chat calls (M1 fake HTTP can still run).

**D6 — Generated answer.** Recommendation: discard for Reference mapping; do not
map into Capture. Alternative: keep answer as a separate non-evidence field in
the adapter only (still not C1).

**D7 — Non-workspace results.** Recommendation: omit merged web-search hits
(`documentId: "web-search"`, `chunkId: "web_{n}"`) with reason
`non_workspace_result`, and have A1/M1 pin the retrieval profile so web fallback
is bound rather than filtered after retrieval. Alternative: surface them as a
separate untrusted, non-indexed result class — rejected for A0 because they have
no index identity and no version basis, so nothing here could describe them
honestly.

## 6. Fixtures

Under `contracts/fixtures/opendocuments-adapter/`:

| Case | Intent |
|---|---|
| `happy-sanitized-chat` | Notion URI + local file; extra fields dropped; answer discarded; mixed version metadata |
| `hostile-filesystem-path` | `Z:\…\..\Secrets\token.txt` → no drive, no `..`, no target filename |
| `hostile-chunk-as-document` | chunk id / missing document id omitted |
| `hostile-source-type-mismatch` | Notion type + `Z:\…` path, local type + `notion://` path, and a malformed Notion page id → all redacted |
| `hostile-credential-url` | `https://user:pass@host/?api_key=…` redacted; token query/fragment dropped; `gdrive://` reduced to the scheme — all three under the connector names the frozen upstream actually writes |
| `hostile-chunk-identity` | missing, malformed, non-string, and cross-document chunk ids omitted; only the well-formed hit survives |
| `hostile-web-search-result` | `documentId: "web-search"` and `chunkId: "web_n"` omitted as `non_workspace_result` |
| `hostile-overlong-metadata` | 5 000-char content with control bytes, 40 × 500-char headings, 900-char type/title/version/reason, out-of-range score, non-hex hash, boundary-length connection id and query; its 900-char `sourceType` is also an unreconcilable declaration, so its otherwise valid `notion://` locator is redacted |
| `missing-version` | document metadata present but null version/hash |
| `out-of-scope-collection-filter` | refuse |
| `out-of-scope-conversation` | refuse |
| `out-of-scope-workspace-override` | refuse |
| `out-of-scope-overlong-connection-id` | 129-char connection id refused instead of emitted |
| `out-of-scope-overlong-query` | 1 001-char query refused |

The suite (46 tests, stdlib `unittest` plus the already-declared
`jsonschema==4.26.0`) checks the frozen goldens, validates **every** mapped
output and every golden against the schema with a `FormatChecker`, asserts the
schema rejects an `ok:true` body carrying an error, and asserts the
redaction/omission properties directly so the goldens are not the only oracle.
It also carries the exact counterexamples raised by the independent final
review — mixed-case credential and opaque URIs, real connector declarations
disagreeing with a parsed locator, a ten-digit chunk position, trailing-line-feed
identities, and a 4 000-digit integer score — and validates every accepted
boundary projection against the closed schema.

`mapping.py` is not installed as a Work Stack module.

## 7. What M1 can test with fake HTTP

Fake the OpenDocuments HTTP app (in-process or recorded JSON). Do not hit a
customer host. Concrete paths:

| Call | Fake | Assert |
|---|---|---|
| `POST /api/v1/chat` | 200 `QueryResult` from `input/happy-chat.json` | mapper matches `expected/happy-normalized.json` when documents omitted or supplied |
| `POST /api/v1/chat` | 400 empty query | adapter surfaces invalid query; no partial candidates |
| `POST /api/v1/chat` | 503 `MODEL_UNAVAILABLE` | classified unavailable, not empty corpus |
| `POST /api/v1/chat` | 409 `CORPUS_EMPTY` | classified empty corpus |
| `POST /api/v1/chat` team mode, no key | 401 | auth failure |
| `POST /api/v1/chat` ask-only key | 200 chat; `GET /documents/:id` 403 | version stays `unavailable` / `chat_response` |
| `POST /api/v1/chat` with `collection_id` | do not send; refuse locally | `collection_filter_unsupported` |
| `POST /api/v1/chat` with `conversationId` | do not send; refuse locally | no conversation persistence |
| `GET /api/v1/documents/:id` | 200 row with null `source_version` | `missing-version` shape |
| `GET /api/v1/collections` | 200 | unused by search; must not imply a filter |
| `POST /api/v1/chat` `balanced`/`precise` with a web provider | 200 with a merged `documentId: "web-search"` source | omitted `non_workspace_result`; request pins/constrains the profile |
| Stream `/chat/stream` | out of scope | adapter does not call it |

Query-log behaviour cannot be settled with fake HTTP against this SHA. The route
always *calls* `persistQueryLog`, and that function swallows database errors, so
fake HTTP can prove neither persistence nor absence. Tests can only prove the
adapter does not add a second log, that it sends no suppression flag (none
exists), and that the UI states attempted upstream logging.

## 8. Work Stack code this mapping must not pretend to satisfy

Read-only on `0fb998e`:

- `docs/KNOWLEDGE-SEARCH.md` / `desktop/python-webview-shell/knowledge_search.py` — path/line/SHA provider protocol.
- `frontend/src/features/knowledge/knowledgeTypes.ts` — `provider: markdown-vault` only.
- `frontend/src/features/knowledge/knowledgeSourceView.ts` — `sourceQualifiedIdentity(connectorId, sourceId, documentKey)` is the display precedent.
- `workstack/capture.py` — Microsoft source allowlist; do not expand.

M1 adapter work is a later packet (A1). This A0 packet does not implement HTTP.

## 9. Limitations

- Customer install commit, Docker vs native, RaiDrive `Z:` visibility, and real
  Notion ACL were not executed.
- `rootPageId` isolation is not implemented in the reviewed Notion connector.
- Deleted/unshared origin documents are not proven to leave the index.
- Actual `query_logs` persistence is neither proven nor disproven; only the
  route's attempt is visible in code.
- Source-qualified document identity is unproven by design here. Nothing in this
  packet establishes that a `notion://` locator names a page the caller may read.
- Web-search merging is filtered, not bound. Profile/provider constraint is A1
  work.
- No HTTP search-without-LLM exists; `/chat` always involves the RAG engine
  (including `route: direct` for some queries).
- Proposed schema v0 will be rewritten if C1 uses different names. Workers must
  not fork a second host contract.

## 10. Public interface added by this packet

None in product runtime. Reviewable artifacts:

- `docs/OPENDOCUMENTS-CONNECTOR-MAPPING.md` (this file)
- `contracts/fixtures/opendocuments-adapter/**` including `mapping.py`
  (`map_opendocuments_chat_bound`) as a fixture helper only
