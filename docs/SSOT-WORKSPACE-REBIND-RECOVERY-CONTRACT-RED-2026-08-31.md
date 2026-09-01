# SSOT workspace rebind/recovery contract (RED)

Status: RED test contract. Product implementation is intentionally absent in this commit.

## Boundary

The normal sync-adoption path remains limited to validated changes within the
same workspace identity. A mismatch between the committed runtime manifest
workspace ID and the currently configured authoritative store is invalid and
write-blocking. It must never be accepted automatically, including on restart.

Rebinding is a separate, explicit recovery action. It is executed by the Work
Stack server that owns the authoritative store. In local mode that server and
its recovery artifacts are local. In `ssh-remote` mode the same operation runs
on the remote server through the existing loopback SSH tunnel; the Windows
desktop must not copy, rewrite, or locally re-baseline the remote SSOT.

## Review coordinate

`GET /api/v1/sync/rebind-preview` (and the corresponding store method
`workspace_rebind_preview`) is available only when all authoritative files form
a valid store and the sole invalidity is a workspace identity mismatch. It
returns content-free coordinates:

- `state = workspace-identity-mismatch`
- `manifest_workspace_id`: identity in the committed runtime manifest
- `candidate_workspace_id`: identity in the configured authoritative store
- `manifest_digest`: digest of the exact committed manifest under review
- `candidate_digest`: digest binding the candidate workspace ID, complete
  authoritative file roster, file sizes, and SHA-256 values
- `changed_files`: authoritative file names only

It does not return planning content and does not change any file.

## Explicit action

`POST /api/v1/sync/rebind-workspace` requires same-origin CSRF protection, an
idempotency key, and this exact intent:

```json
{
  "confirmed": true,
  "expected_manifest_workspace_id": "<uuid>",
  "expected_candidate_workspace_id": "<uuid>",
  "expected_manifest_digest": "sha256:<64 lowercase hex>",
  "expected_candidate_digest": "sha256:<64 lowercase hex>"
}
```

Every coordinate is re-read and compared immediately before commit. A missing
confirmation, wrong identity/digest, non-mismatch state, invalid candidate, or
candidate change after review fails closed without changing the manifest or
creating a completed receipt.

## Durable recovery evidence and atomic boundary

Before replacing the active manifest, the authority-local runtime directory
must durably contain:

1. a verified ZIP backup of the exact candidate authoritative bytes;
2. a content-free JSON receipt binding every authoritative filename, byte size,
   and SHA-256 value to the reviewed candidate digest; and
3. an exact-byte quarantine copy of the previous manifest and its SHA-256.

Only then may one atomic manifest replacement establish the candidate workspace
as the new baseline. There is no interval in which the active manifest is
deleted. A crash before replacement leaves the old mismatch fail-closed; a
crash after replacement restarts in-sync. Recovery artifacts live under
`Store.runtime_root`, never in the authoritative data directory.

The completed receipt records schema version, operation, previous and candidate
workspace identities, both reviewed digests, artifact filenames/digests, the
authoritative file hash roster, idempotency key, UTC timestamp, and
`planning_mutated: false`. It contains no task titles, notes, message text, or
other planning content.

## Non-mutation invariant

Rebinding changes only runtime recovery evidence and the runtime manifest.
Every authoritative byte present at the reviewed candidate coordinate is
identical before and after success. Task revisions, planning facts, and activity
are not advanced. Normal writes resume only after the new manifest is committed.

## Restart and idempotency

An exact retry with the same idempotency key and review coordinate returns the
same completed result after restart. Reuse of the key for another coordinate is
a conflict. Restart alone never converts a mismatch into an accepted baseline.
