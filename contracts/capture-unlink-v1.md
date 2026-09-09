# Capture unlink v1

Status: implemented. Inverse of explicit Capture→Task linking. Frozen R22
contract: `ART/R22-CAPTURE-UNLINK-CONTRACT.md`. Capture Packet v1.0,
retrieval v1.1, `/link`, dismiss, conversion, notes, knowledge unpin, Task
PATCH and mutation-notice Undo are unchanged.

This document is the backend wire and domain surface. It does not add a CLI
leaf, a schema migration, or a frontend.

## HTTP

`POST /api/v1/captures/{capture_id}/unlink`

Exact JSON body:

```json
{ "task_id": "T-0001", "revision": 1 }
```

| Field | Rule |
| --- | --- |
| `task_id` | string; a known Task display id |
| `revision` | displayed Capture revision; nonnegative safe integer. `true`/`false` and non-integers are refused |

Unknown keys, a missing key, a non-string `task_id`, or an inadmissible
`revision` are HTTP 400 `invalid_body` before any Capture write.

The route is a Capture POST: existing browser same-origin/CSRF and a mandatory
`Idempotency-Key` matching `[A-Za-z0-9._:-]{8,128}` apply. Capture ingestion
bearer tokens grant no access. Prefix idempotency uses method, the request
path, and the canonical digest of both body fields.

There is no argparse leaf. The GUI-only exclusion is
`POST /api/v1/captures/{id}/unlink`.

## Service and V4 repository

```
unlink_capture(capture_id, task_id, revision, idempotency_key,
               request_digest=None, *, path=None)
```

The method exists on the legacy `CaptureServiceMixin` and, when injected, on
`V4CaptureReplyRepository`. Result and error conventions match `/link`:
HTTP 200 with a Capture projection, 404 unknown Task or Capture, 409
`revision_conflict` or `idempotency_conflict`, 400 invalid revision.

Order:

1. Admit `revision`.
2. Replay a stored matching Idempotency-Key **before** the current-revision
   check. Same key and different body or path is `idempotency_conflict`.
3. Require a known Task and a known Capture.
4. Enforce CAS against the stored Capture revision even when the explicit
   link is already absent. A mismatch writes nothing.
5. Remove `task_id` only from `linked_task_ids`. Preserve
   `converted_task_ids`, action `task_id` references, source content, notes,
   backlog Tasks and workspace schema.

When the actual link changes: increment Capture `revision` once, set
`updated_at`, emit `capture.unlinked` with opaque `capture_id` and `task_id`
only. If status is `linked` and both `linked_task_ids` and
`converted_task_ids` would be empty, status becomes `inbox`. Otherwise
preserve status, including `dismissed` and `converted`.

Success envelope:

```json
{ "data": { "...normal Capture projection..." }, "meta": { "duplicate": false } }
```

An absent link at the correct revision is `200` with `meta.duplicate=true`,
no revision or event change. Persisting the idempotency receipt is allowed.
Writes use the existing atomic Capture+activity transaction, not raw JSON
files.

A Task connected by both `capture-link` and `capture-conversion` keeps the
conversion card after the explicit link is removed.

## Additive unlink Undo (R33)

A successful **mutating** unlink may add `meta.undo_receipt_id` (canonical
UUID). Duplicate or unmutating unlink responses omit it. Existing readers
that ignore unknown `meta` keys remain valid.

The receipt itself, the Undo HTTP body `{receipt_id, revision}`, and
`POST /api/v1/captures/{capture_id}/undo-unlink` are specified in
`contracts/capture-unlink-receipt-v1.md`. There is still no argparse leaf;
the GUI-only exclusion is `POST /api/v1/captures/{id}/undo-unlink`.
