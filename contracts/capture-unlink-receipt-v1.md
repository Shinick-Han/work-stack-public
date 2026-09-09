# Capture unlink receipt and Undo v1

Status: implemented. Bounded inverse of one owner unlink. Frozen R33
contract: `R33-CAPTURE-UNDO-CONTRACT.md`. This is not generic undo, content
refresh, a schema migration, a CLI leaf, or a frontend.

## Receipt

Durable sibling activity event `capture.unlink_receipt`, written in the same
transaction as a mutating unlink. Existing `capture.unlinked` keeps empty
details. No receipt on duplicate/unmutating unlink. Same unlink Idempotency-Key
replay returns the original response and receipt id; it never remints.

Closed receipt fields, exact set:

| Field | Rule |
| --- | --- |
| `format` | `workstack.capture-unlink-receipt` |
| `schema_version` | integer `1` |
| `receipt_id` | canonical UUID5 from workspace UUID + unlink idempotency key |
| `workspace_uid` | canonical RFC 4122 UUID |
| `capture_id` | canonical Capture display id `C-` + four or more digits |
| `task_id` | canonical Task display id `T-` + four or more digits |
| `task_uid` | canonical RFC 4122 UUID of that Task |
| `status_before` | `inbox`, `linked`, `converted`, or `dismissed` |
| `before_revision` | displayed Capture revision before the unlink increment |
| `after_revision` | `before_revision + 1` |
| `after_digest` | `sha256:` + 64 lowercase hex; digest of the raw post-unlink Capture row |
| `idempotency_key` | `[A-Za-z0-9._:-]{8,128}` |
| `commit_state` | `committed` |

Unknown fields, bool/out-of-bound revisions, noncanonical ids/digests/status,
and a receipt id that does not match the workspace+key derivation are
malformed. Activity `details` is exactly `{ "receipt": <canonical JSON string> }`.
The envelope `capture_id` and `task_id` must match the blob. A malformed,
pretty-printed, foreign, or mismatched envelope cannot authorize Undo.
Same receipt id with conflicting content is `idempotency_conflict`.

The digest covers the stored Capture row after one unlink increment. R27
observations live on the captures container, not on that row: they do not
enter the digest, are not erased by Undo, and do not by themselves bump
Capture.revision.

## HTTP

`POST /api/v1/captures/{capture_id}/undo-unlink`

Exact JSON body:

```json
{ "receipt_id": "<uuid>", "revision": 2 }
```

Unknown keys (including `status_before`, `task_id`, source or snapshot), a
missing key, a non-string `receipt_id`, or an inadmissible `revision` are
HTTP 400 `invalid_body` before any Capture write. The client does not send
prior status.

The route is a Capture POST: existing browser same-origin/CSRF and a
mandatory `Idempotency-Key` apply. Capture ingestion bearer tokens grant no
access. The route is appended after existing POST entries so earlier ordinals
stay put. It is not listed in `IDEMPOTENT_POST_ROUTES`; the `/api/v1/captures`
prefix already demands the key.

## Service and V4 repository

```
undo_capture_unlink(capture_id, receipt_id, revision, idempotency_key,
                    request_digest=None, *, path=None)
```

The method exists on `CaptureServiceMixin` and, when injected, on
`V4CaptureReplyRepository`.

Order:

1. Admit `receipt_id` and displayed `revision`.
2. Replay a stored matching Idempotency-Key **before** receipt lookup.
3. Load the receipt for this workspace and Capture. Absent or foreign is
   `not_found`. Malformed/ambiguous is a closed refusal.
4. Require the named Task; missing is `not_found`. Display id present with a
   replaced uid is `capture_unlink_undo_conflict`.
5. Require stored Capture revision equals `after_revision` and the supplied
   displayed revision; otherwise existing `revision_conflict`.
6. Require the raw Capture digest equals `after_digest` and the recorded
   Task id absent from `linked_task_ids`. Otherwise
   `capture_unlink_undo_conflict` with no source/body in diagnostics.
7. Restore `linked_task_ids = sorted(existing + receipt.task_id)` and
   `status = status_before`. Increment Capture revision once, set
   `updated_at`, emit `capture.unlink_undone` with empty details. Preserve
   converted ids, action references, source/normalized data and container
   observations. Backlog is unchanged.
8. `MAX_REVISION` refusal stays atomic: no half-written Capture+activity.

Success envelope:

```json
{ "data": { "...normal Capture projection..." }, "meta": { "duplicate": false } }
```

Same-key replay follows current unlink semantics (`meta.replayed`). A second
distinct Undo of the same receipt after revision advance is
`revision_conflict`.
