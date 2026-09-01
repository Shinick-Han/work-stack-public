# SSOT storage Wave 2 semantic parity receipt

Date: 2026-09-01  
Scope: inactive, read-only v4 candidate projection  
Activation/write status: disabled

This slice receipt is incorporated into the consolidated
`SSOT-STORAGE-WAVE-2-REGRESSION-RECEIPT-2026-09-01.md`.

## Outcome

`workstack.storage.semantic` now projects both the frozen nine-document v3
fixture and normalized v4 record collections into one `WorkspaceSnapshot`.
The populated projections are structurally equal and both retain the frozen
semantic digest:

```text
sha256:cca698f3d4137f0f4220eaa22102c6625a1e0de439cea364e9c0bcca0f15b36f
```

The equality covers workspace identity, Tasks, subtasks, Task annotations,
current and historical planning status, objectives/key results, review days,
complete work sessions/segments, standalone notes and links, captures including
actions and source revisions, replies/receipts, activity, and the deferred
idempotency ledger. Deterministic relation edges and search-source documents
also match between formats.

## Refusal boundary

The projector does not infer the original order of v3 embedded Task notes from
generated UUIDs. `task_note_source_indexes` must be supplied from the migration
receipt's generated-ID roster. Omitting that evidence fails closed with
`TASK_NOTE_SOURCE_INDEX_REQUIRED`. This makes the already-documented migration
receipt a concrete activation dependency rather than silently reordering or
deduplicating annotations.

The idempotency ledger remains an explicit input because its bounded v4 runtime
schema is also an existing activation blocker. It is preserved byte-for-value
in the semantic projection; it is not treated as a domain event stream.

## Verification

```text
python -m unittest tests.test_storage_semantic_parity -v
Ran 5 tests ... OK

python scripts/quality_gate.py report
PASS: 129 production files

python scripts/audit_export.py .
EXPORT AUDIT PASSED: 508 UTF-8 text files (source policy)
```

The v4 fixture used by the parity test is assembled in memory from the frozen
v3 fixture, and every schema-covered v4 record/event is validated against the
bundled Draft 2020-12 contracts before comparison. No candidate directory is
written, activated, or substituted for the existing Store.
