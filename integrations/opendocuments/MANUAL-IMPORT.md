# Manual import composer

A pure bridge from one mapped OpenDocuments retrieval onto the released
`workstack.knowledge-import.v1` envelope of
`contracts/knowledge-capture-import-v1.md`. It is not an automated Adapter, not
an HTTP client, not a submitter, not source verification and not a summarizer.

Owned files: `manual_import.py`, this `MANUAL-IMPORT.md`. There is no
`__init__.py` here.

## Settled call

```python
from integrations.opendocuments.manual_import import (
    ManualImportError,
    build_manual_import,
)

envelope = build_manual_import(
    chat,
    request_id=ledger_request_id,
    item_id=caller_item_id,
    result_limit=limit,
    source_catalog=catalog,
)
```

The return value is a `dict`: the closed import envelope, ready for the owner
to carry into `POST /api/v1/knowledge/captures/import` under the browser
session they already have. Nothing is sent, stored, logged or allocated here.

The function is **pure and deterministic**. It opens no path and no network,
reads no clock, mints no identifier, and returns the same envelope for the same
admitted inputs. It mutates neither `chat` nor `source_catalog`.

`map_retrieval` from `MAPPING.md` is called unedited and owns every admission
decision about the evidence: the trusted catalog, the caps, the 16 KiB wire
bound and the `answer_scope`. This module adds no detector, copies no schema
and re-derives nothing the mapper already decided.

## What the envelope carries

Exactly **one** item, holding the **entire** admitted retrieval wire. The
evidence set is never split across items and mixed evidence is never relabelled
to one source — the mapper's `mixed_evidence_not_representable` refusal is
propagated rather than worked around.

| Envelope field | Value |
| --- | --- |
| `schema` | the constant `workstack.knowledge-import.v1` |
| `request_id` | the mapper's admitted canonical request UUID |
| `items[0].item_id` | the **caller's** canonical UUID, admitted by the existing `canonical_uuid` |
| `items[0].title` | `single_source`: the first admitted evidence title, already catalog-derived. `synthesized`: the constant `Search evidence` |
| `items[0].normalized.summary` | the same string as `title` |
| `items[0].normalized.context` | the constant `Review the listed evidence before linking it to a task.` |
| `items[0].normalized.action_items` | `[]` |
| `items[0].normalized.tags` | `[]` |
| `items[0].retrieval` | the mapper's wire, unchanged |

**Nothing is synthesized and nothing is extracted.** The generated `answer`,
chunk `content`, confidence `reason`, `sourcePath`, URLs, query text, index
keys and hashes are copied nowhere: not into the summary, not into the context,
not into an action item, not into a tag. `action_items` is empty because
this bounded composer does not extract action candidates. The released manual
import contract can carry user-reviewed `normalized.action_items`; it still
creates no Task automatically and derives empty `task_hints`.

The evidence stays **unverified**. No `source_version` and no origin is added:
the mapper emits `source_version` / `web_url` as `null`, and the released
validator therefore projects `origin = null`, `capture_source_type =
knowledge.answer` and `version_state = unreported`. This lane wires no
verifier, so nothing here asserts currentness.

## Replay is the caller's `item_id`

The released importer's logical key is `request_id` plus the digest of the
whole admitted completion, and that digest covers each item's identity. So:

- the **same** `item_id` over the same admitted inputs composes the same
  envelope and therefore replays the same completion;
- a **different** `item_id` is a **different** completion identity, which the
  released importer answers with `completion_digest_mismatch` for an already
  completed request.

Choosing the value is the caller's, deliberately: this module allocates no
identity and reads no clock, so it cannot decide which attempt a retry is.

## Authority

**Nothing here proves the request was issued, is still open, or may be
completed.** The envelope has nowhere to write an issuance, expiry, policy
revision, provider, credential, connection, path or verification claim, and
this module asserts none. The released importer reads the ledger inside its own
transaction and stays the **sole authority on submission**; a composed envelope
is only a document.

This is a bounded functional bridge. Saying so is documentation of that bound,
not a claim that the customer's end-to-end job is finished.

## Bounds

- The composed body is measured as compact UTF-8 against the released 64 KiB
  whole-body bound. The mapper's 16 KiB wire plus one bounded title, its repeat
  as the summary and two constants cannot currently reach that bound, so
  `import_too_large` is a **guard against a later bound change**, not a
  reachable refusal today.
- The retrieval wire keeps every mapper bound and cap unchanged.

## Refusals

`ManualImportError` is a subclass of the mapper's `MappingError`, so one
`except MappingError` catches both. Its `code`, `str`, `repr` and `details`
carry **only the code**: no submitted value, title, identifier, path or
traceback text.

- `invalid_uuid` — `item_id` is not a canonical lowercase non-nil UUID. It is
  decided before any evidence is composed.
- `import_too_large` — the guard above.
- Every mapper code (`invalid_catalog`, `invalid_result_limit`, `invalid_uuid`,
  `invalid_chat`, `invalid_query_id`, `invalid_confidence`,
  `query_id_not_distinct`, `no_admitted_evidence`,
  `mixed_evidence_not_representable`, `extension_too_large`, plus whatever
  closed code the existing payload validator returns) propagates unchanged.

## Not this lane

- The HTTP client, `POST /api/v1/chat`, and any automated submission
- Request issuance, expiry, authority, ledger or Store writes
- `source_access` / NAS path verification / origin attestation
- Any verifier that could fill `source_version`
- UI, packaging, quality-config or schema changes
