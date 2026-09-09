# Explicit source verification v1

An owner may ask an operator-configured external verifier to check the sources
of one stored Capture. This is a read-only observation, not a search, content
refresh, persisted freshness claim, or Task mutation.

The wire protocol is implemented in `workstack/knowledge_verification_protocol.py`.
The owner/HTTP composition is specified here for the R21 integration; deployments
without that composition cannot execute a check merely by accepting this wire.

## Authority and configuration

The browser supplies only workspace UID, Capture ID and Capture revision. The
owner derives connection, corpora, evidence and expected versions from the
stored v1.1 Capture and its completed knowledge-request ledger record. That
record must list the Capture, and its connection policy must still be current.
The original search request's expiry does not expire an imported Capture.

An existing knowledge-driver registry entry may include an optional
`verification` object with exactly `command` and `environment`. Omission keeps
verification disabled; explicit null is invalid. The operator's pinned command
and environment remain outside the browser and SSOT. Search does not imply
permission to verify, and the owner never falls back to the search command.

The request is a closed JSON object, limited to 16 KiB:

| Field | Meaning |
| --- | --- |
| `schema` | `workstack.knowledge-verify.v1` |
| `verification_id` | Owner-generated UUID |
| `binding` | Exact `workspace_uid`, `capture_id`, `capture_revision` |
| `connection` | Exact `alias`, `upstream_workspace_uid`, `policy_revision` |
| `corpus_refs` | 1–8 unique owner-authorized aliases |
| `evidence` | 1–10 ordered entries: `document_ref`, `source_type`, `expected_source_version` |
| `requested_at`, `expires_at` | RFC3339 timestamps; positive window of at most 60 seconds |

Evidence types are `nas.file`, `notion.page`, and `knowledge.answer`. Repeated
document references are allowed; each result must preserve its exact position
and original expected version. Supporting a type in the wire does not mean an
installed verifier can verify it.

The closed result has `schema: workstack.knowledge-verification.v1`, the same
`verification_id`, `checked_at`, and ordered `evidence`. Each entry repeats the
three request evidence fields and adds `observed_source_version`, `status`, and
`code`. No titles, paths, query text, document bodies, credentials, free-form
reason, or indexed digest belong in either message.

## Observations

| Status | Code | Version condition |
| --- | --- | --- |
| `current` | `hash_matched` | Expected and observed versions are non-null and equal |
| `stale` | `hash_differs` | Expected and observed versions are non-null and different |
| `missing` | `file_absent` | Observed version is null |
| `unavailable` | `root_unavailable` | Observed version is null |
| `denied` | `access_denied` | Observed version is null |
| `refused` | `source_refused` | Observed version is null |
| `revoked` | `mapping_revoked` | Observed version is null |
| `unverifiable` | `no_expected_version`, `no_origin_verifier`, `unsupported_source_type`, or `verification_unavailable` | Observed version is null |

These are the complete permitted status/code pairs. A missing file is not proof
of deletion. A disconnected root is not revocation. Unknown authority is not
silently converted into a source observation.

The NAS adapter uses wire versions `sha256-` followed by 64 lowercase hex
characters, translating only that exact form to its internal `sha256:` form.
Other opaque expected versions cannot be compared to a file hash. A missing
expected source version stays missing; an indexed digest or a hash computed
only at check time cannot establish the version of an earlier Capture.

## Owner execution and browser presentation

R21's route is `POST /api/v1/knowledge/captures/verify`, with the exact body
`{workspace_uid, capture_id, capture_revision}`. It uses existing browser
same-origin/CSRF protection, not Capture ingestion authority. No arbitrary
command, URL, corpus, source path, or expected version is accepted from the UI.

The owner admits authority under the Store transaction, releases the transaction
before child I/O, and uses a dedicated per-owner verification guard. One active
check is allowed. A busy caller must not release another caller's guard.
Unconfirmed child cleanup latches verification until owner restart.

The existing bounded child transport has a 45-second budget. After I/O the owner
validates the result against the original request and actual current time:
`requested_at <= checked_at <= now < expires_at`. A backdated response arriving
after expiry is refused. Re-admission must find the same Capture revision,
completed-record binding, evidence and current policy. Its fresh timestamps do
not extend the original acceptance window.

Success returns `{data: {binding, result}, meta: {outcome: "verification_ready"}}`.
No Capture, Task, ledger record, activity content, or SSOT schema is changed by
the operation. A settled failure can be followed by an explicit new read-only
check; there is no automatic retry or background check on opening a drawer.

The UI shows a timestamped observation beside existing reported evidence. It
does not replace the saved Capture's version claims or imply that a summary is
correct. Changing, closing, or reopening the Capture invalidates the transient
view; a late response must not attach to a different view or revision.
