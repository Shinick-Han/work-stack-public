# Capture v1.1 retrieval extension contract

Status: validation primitive only. `workstack/capture_retrieval.py` implements this
document exactly. There is no endpoint, no route, no persistence, no connector, no
resolver and no provider allow-list change in this slice.

**Capture Packet v1.0 is unchanged.** `workstack/capture.py` is not edited,
`validate_capture_packet` projects exactly what it projected before, and importing this
module does not alter it. The frozen v1 fixtures still validate byte-identically.

This is the answering half of `knowledge-request-v1.md`.

## Wire shape

`schema` is the literal `workstack.capture-retrieval.v1.1`.

```json
{
  "schema": "workstack.capture-retrieval.v1.1",
  "capture_schema_version": "1.1",
  "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "query_id": "engine-q-00194f5a",
  "answer_scope": "single_source",
  "confidence": { "level": "medium", "score": 0.62 },
  "evidence": [
    {
      "source_type": "notion.page",
      "title": "Release quality gate",
      "document_ref": "od-page-7f3ba1d34f50c884600112ab",
      "chunk_ref": "chunk-0004abcd",
      "source_version": "od-version-14",
      "indexed_digest": "sha256:aaaa...",
      "web_url": null
    }
  ],
  "truncated": false
}
```

The example is illustrative of the shape only. The contract is the field table below and
the code that enforces it, not this document's sample values.

## Fields

The object is **closed at every level**: an unknown key anywhere, and a missing required
key anywhere, is a refusal.

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | string | exactly `workstack.capture-retrieval.v1.1` |
| `capture_schema_version` | string | exactly `1.1` |
| `request_id` | string | canonical non-nil UUID; must equal the `request_id` the caller passes in from its ledger |
| `query_id` | string | 1..128 characters matching `[A-Za-z0-9][A-Za-z0-9._~:-]*`; must **not** equal `request_id`; refused when it holds credential material |
| `answer_scope` | string | `single_source` or `synthesized` |
| `confidence.level` | string | `low`, `medium` or `high` |
| `confidence.score` | number | finite, `0.0 .. 1.0`. A JSON boolean is not a number here, and an integer is range-checked before it is converted to a float |
| `evidence` | array | 1..10 items, no two sharing the same `(document_ref, chunk_ref)` |
| `evidence[].source_type` | string | `notion.page`, `nas.file` or `knowledge.answer` |
| `evidence[].title` | string | 1..500 characters of safe display text that does not read as a resolved source location |
| `evidence[].document_ref` | string | opaque handle, 8..256 characters matching `[A-Za-z0-9][A-Za-z0-9._~-]*` |
| `evidence[].chunk_ref` | string \| null | same handle grammar, or absent/null |
| `evidence[].source_version` | string \| null | same handle grammar, or absent/null |
| `evidence[].indexed_digest` | string \| null | `sha256:` plus 64 lowercase hex, or absent/null |
| `evidence[].web_url` | null | may only be null |
| `truncated` | boolean | a real JSON boolean; `0`/`1`/`"false"` are refused |

## Reported claims versus verified provenance

Syntax validation can establish exactly one thing about provenance: that a claim is
*bounded*. It cannot attest that bytes came from OpenDocuments, that a named source is
the real one, or that a version string is the source's. The projection therefore keeps
the two apart by name, and a wire field can never cross the line:

- Everything the document says about its sources is projected under a `reported_` name.
  A reader cannot mistake `reported_source_type` for attested provenance.
- Trusted `origin` and any `verified_*` currentness state come only from
  `RetrievalVerification`, which the **caller** supplies from its own attested state,
  exactly as `RequestAuthority` is supplied on the asking half. Assembling it from the
  answering document would defeat the whole split.
- With no verification supplied — the default, and the honest state today — the
  projection carries reported claims, a `null` `origin`, and `capture_source_type`
  `knowledge.answer`. Nothing is lost: the later admission gate can supply verification,
  or record the answer as an answer.

`RetrievalVerification` holds a tuple of `VerifiedSource(document_ref, source_type,
source_version=None)`. Its shape is checked before use — a malformed one is
`invalid_verification`, never a raw Python error — and a document that names a different
`source_type` than the caller attested for the same `document_ref` is refused as
`verification_conflict` rather than half-projected.

## Derived projection

The validator returns the accepted fields plus values it derives. A derived value is
never read from the wire, so a sender cannot assert it:

| Derived | Meaning |
| --- | --- |
| `evidence[].version_state` | `unreported` (no version claimed), `reported_unverified` (a version claimed, nothing attested), `verified_current` (the claim equals the attested version), `verified_stale` (the claim differs from the attested version) |
| `reported_origin` | `{document_ref, source_type}` the document claims for a `single_source` answer; `null` for a `synthesized` one. Still only a claim |
| `origin` | the same pair read from the caller's attestation, and `null` whenever the document is unattested |
| `origin_state` | `synthesized`, `reported_unverified`, or `verified` |
| `capture_source_type` | the **verified** origin's `source_type`, or `knowledge.answer` — which is what an unattested answer is |

A verified version that the answer never reported is `unreported`, not `verified_stale`:
an absent claim cannot be contradicted.

## What an answer may not claim

**A source it did not name.** `document_ref`, `chunk_ref` and `source_version` are opaque
handles. The grammar admits no separator, scheme, drive letter, `..` traversal, space,
control character or percent escape, so a filesystem path, a UNC share, a URL and their
percent-encoded forms are not representable at all. A credential-shaped value such as a
JWT is spelled in characters the grammar does allow, so it is refused separately by the
credential-material detector.

**A navigation target.** `web_url` may only be `null` for initial NAS and Notion evidence.
There is no trusted provider-URL policy for these sources and no resolver in this slice, so
any URL here would be an unverified target presented as if the host had vouched for it.
Widening this is a later, explicit policy decision.

**Currentness it cannot prove.** `source_version` may legitimately be absent — a NAS share
or an index may not expose one. When it is absent the projection says `unreported`; when it
is present but unattested it says `reported_unverified`. A syntactically valid version
string is always reportable and is never marked verified or current on its own. Nothing
invents a version, and nothing infers one.

**That an indexed digest is a source version.** `indexed_digest` is a digest of what the
*index* holds. It is kept in its own field, never promoted into a version, and never
changes `version_state` — not even alongside a caller's attestation: equal digests do not
prove equal sources, and a fresh index of a stale copy is still stale.

**A single original source for a multi-source answer.** Once the evidence spans more than
one `document_ref`, `answer_scope` must be `synthesized`; `origin` is then `null` and
`capture_source_type` is `knowledge.answer`. `single_source` additionally requires that
every evidence item share one `source_type`. Naming the first evidence item as "the"
source would be a fabrication. A caller that genuinely wants per-source attribution must
split the answer by action and submit each with its own evidence.

**Truth.** `confidence.score` is a bounded finite retrieval score. It is **not** a
probability that the answer is correct and must never be presented as one. `level` is the
coarse band a reviewer reads; the score is the engine's own ordering signal.

**Raw content.** Keys that would carry source text, a resolved location, a credential, the
raw engine query or an open-ended property bag are refused at any depth, with the code
`forbidden_field`. The Capture v1.0 forbidden set is reused verbatim
(`body`, `html`, `content`, `attachments`, `raw`, `transcript`, `recipients`) and widened
with the retrieval-specific leaks: `text`, `snippet`, `excerpt`, `passage`, `chunk_text`,
`path`, `file_path`, `filepath`, `source_path`, `share`, `unc_path`, `url`, `uri`, `href`,
`link`, `token`, `credential`, `credentials`, `secret`, `password`, `api_key`,
`query_raw`, `raw_query`, `metadata`, `properties`, `extra`, `fields`.

A `title` is held to the same raw-content gate Capture v1.0 applies to display text:
credential material, rendered HTML, an address, a quoted reply and the raw-content canary
are all refused, as is any control character.

**A location relabelled as a label.** The ref grammar cannot represent a path or a URL, so
a `title` is the remaining place one could be written down. After percent-decoding, a title
that reads as a resolved source location is refused with `source_location_suspected`. That
judgment is a bounded display-text heuristic, not a detector of every conceivable
filesystem name. It refuses:

* Windows drive forms, absolute and relative (`C:/secret/payroll.xlsx`,
  `C:\secret\payroll.xlsx`, `C:secret/payroll.xlsx`, `C:payroll.xlsx`), including
  percent-encoded equivalents of those forms
* UNC shares, POSIX or home-relative roots (`/mnt/nas/…`, `~/…`), protocol-relative
  hosts (`//fs01/…`), and `file:` URLs
* a generic RFC-style scheme with a non-whitespace payload (`https://…`,
  `urn:isbn:…`, `s3:bucket/object`, `custom+v1:opaque`). A colon followed by
  whitespace in ordinary prose (`File: Payroll review`, `SMB: deployment notes`) is
  not a URI. A one-letter prefix remains drive-like rather than a URI scheme. There
  is no separate scheme allow-list
* a file-looking slash token such as a one-level relative file (`finance/payroll.xlsx`,
  also backslash) and traversal (`./payroll.xlsx`, `../payroll.xlsx`)
* two or more unspaced slashes (`nas/finance/payroll.xlsx`)

A bare filename (`payroll.xlsx`, `2026-Q3 gate.xlsx`) is allowed. Ordinary human
titles such as `Q3/Q4 planning`, `Budget / Forecast`, `R&D / QA / Ops handover`,
and Korean display titles remain legal. This does not claim that every
slash-containing prose phrase is accepted or that every possible path spelling is
refused.

**A credential in the correlation ID.** `query_id` is projected and appears in
`evidence_summary`, and its grammar admits dots and dashes, so a JWT is spelled entirely in
characters it allows. It is therefore held to the same credential-material detector the refs
use, and a token-shaped value is refused as `credential_material_suspected`.

## The measured boundary and the body budget

`MAX_CAPTURE_BODY_BYTES` stays 65536, unchanged from `workstack/cli.py`. The extension is
budgeted **inside** that bound and is separately capped at 16 KiB. Retrieval evidence does
not buy a larger Capture; a caller that would exceed the bound sends less evidence, not a
bigger body.

**`validate_retrieval_payload(payload, request_id=…, capture_body_bytes=…, verification=…)`
is the boundary**, and is mandatory for anything arriving as bytes or text. It measures the
payload's own UTF-8 octets, decodes it under the strict decoder (which is where the 16 KiB
bound and the nesting refusal live), validates the document, and only then applies the
Capture body budget to the *measured* size. The budget is applied last, so a malformed
payload refuses as malformed rather than as oversized.

The split helpers are internal and are **not** the boundary:

- `validate_retrieval_extension(document, …)` takes an already-decoded object. It
  establishes no wire-size and no nesting compliance whatsoever — a valid object whose
  UTF-8 encoding exceeds 16 KiB is accepted here, and only refused on the payload path. It
  does bound its own key walk (`extension_too_deep`) so a hand-built nest refuses under the
  closed error model instead of raising `RecursionError`.
- `require_within_capture_body_budget(capture_body_bytes, extension_bytes)` checks
  arithmetic on two caller-supplied counts. Called directly it proves nothing about a
  payload; only the entry point above passes it real measured octets.
- `decode_retrieval_extension(payload)` decodes and bounds, but does not validate.

## Caller obligations

Validation proves the document is well formed and binds to the request identity the caller
passed in. It proves nothing else. It is **not** ledger, replay or transport-admission
enforcement. Before a real OpenDocuments answer is admitted, the host must:

1. **Admit the transport under exactly one of two modes.** The modes are different, and
   conflating them either blocks a legitimate manual import or waves an automated one
   through:

   - *Manual user import.* The user carries the answer out of band and imports it.
     Admission rests on the existing authenticated user session, its CSRF protection and
     the user's own import/Capture authority. It does **not** require an adapter
     credential merely because the bytes were carried by hand — there is no connection to
     authenticate, and demanding one would describe a flow that does not exist.
   - *Automated adapter.* A connector submits directly. Admission additionally requires an
     independently authenticated, approved connection carrying the retrieval authority and
     Capture submission authority.

   In both modes, a `provider`, tool or `opendocuments.ask` string inside a payload
   authorises nothing. This schema has nowhere to put such a claim, and a claim would not
   be authentication if it did.
2. Match `request_id` against an **open entry in a host-issued request ledger**, refuse a
   replay of a settled entry, and retire the entry on admission.
3. Confirm the answering exchange is still inside the request's five-minute window; see
   `knowledge-request-v1.md`. An expired window requires a fresh, user-reviewed request.
4. Supply `RetrievalVerification` if — and only if — it actually holds attested provenance.
   Passing verification the host has not itself established is the one way to make this
   projection lie. Supplying nothing is always safe and is the default.
5. Decide the Capture projection. This slice deliberately does not write one, and
   `capture_source_type` on an unattested answer is `knowledge.answer` precisely so that a
   later gate is not handed a fabricated origin.

`evidence_summary(projection)` gives a reviewer or a ledger entry a counts-only view. It
carries no title, ref, digest or query — only the evidence count, the `origin_state`, the
confidence band, the truncation flag, and a separate count for each version state
(`unreported`, `reported_unverified`, `verified_current`, `verified_stale`). Each state is
counted on its own so that a reported claim is never summed together with a checked one.

## Refusal codes

A refusal is `CaptureRetrievalError` carrying `code` and, at most, `details.field` — the
*name* of a field or an index path in this closed schema. No submitted value, title, ref,
digest or query is ever echoed into a diagnostic.

`forbidden_field`, `invalid_extension`, `extension_too_deep`, `unknown_field`,
`missing_field`, `unsupported_schema`, `invalid_uuid`, `request_id_mismatch`,
`query_id_not_distinct`, `invalid_text`, `invalid_ref`, `credential_material_suspected`,
`raw_content_suspected`, `source_location_suspected`, `invalid_digest`,
`web_url_not_allowed`, `invalid_source_type`, `invalid_evidence`, `duplicate_evidence`,
`invalid_answer_scope`, `answer_scope_mismatch`, `invalid_confidence`, `invalid_truncated`,
`invalid_number`, `invalid_verification`, `verification_conflict`, `extension_too_large`,
`capture_body_too_large`.

Decoding shares the strict decoder in `workstack/knowledge_request.py` and therefore its
refusal type: `decode_retrieval_extension` — and `validate_retrieval_payload`, which uses
it — raises `KnowledgeRequestError` for `duplicate_json_key`, `non_finite_number`,
`request_too_large`, `request_too_deep`, `invalid_encoding`, `invalid_json` and
`invalid_request`, under this module's own 16 KiB bound. `non_finite_number` now covers a
standard JSON number that overflows to infinity (`1e9999`), not only the nonstandard
literals.

## Versioning and migration

- **v1.0 compatibility is real, not asserted.** `workstack/capture.py` is untouched and
  `tests/test_capture_retrieval.py` re-validates the frozen v1 fixtures before and after
  importing this module, including the negative fixture.
- The extension travels **alongside** a Capture, not inside one. A v1.0 reader projects
  an allow-list, so an extension attached to a v1.0 packet is silently dropped rather
  than stored — which is correct, and which is also why attaching it buys nothing until
  the projection gate below.
- `capture_schema_version: "1.1"` names the Capture version this extension is *for*. No
  Capture is written at 1.1 yet. Introducing 1.1 storage is a separate gate that must
  decide where evidence lands in a Capture projection, how `capture_source_type` relates
  to the existing `source.provider` allow-list, and what a v1.0 reader does with a stored
  1.1 record. None of that is decided here, and no provider allow-list is widened here.
- A future v1.2 or v2 takes a new `schema` string. This validator refuses any other
  value, so an old reader cannot silently accept a newer document.
- Reused from existing code, not re-implemented: the forbidden-key set, the SHA-256
  grammar, the percent-decoding bound, the credential-material detector and the
  raw-content regexes from `workstack/capture.py`; the canonical-UUID rule and the strict
  JSON decoder from `workstack/knowledge_request.py`.
- No new dependency and no general schema framework is introduced.
