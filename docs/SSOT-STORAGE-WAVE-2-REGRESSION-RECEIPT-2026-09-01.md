# SSOT Storage Wave 2 Regression Receipt

Date: 2026-09-01  
Scope: inactive v4 layout, reader, manifest, repository admission, and semantic projection  
Authority status: released builds remain v3-only; v4 writes and activation are disabled

## Delivered boundary

Wave 2 can inspect a hand-built normalized candidate without making it authoritative:

- `V4Layout` resolves exact record buckets and stream segments with deterministic rosters;
- `read_v4` performs bounded, schema-checked, canonical JSON/NDJSON reads and records exact raw digests;
- `construct_v4_manifest` builds content-free candidate or generation-bound manifests after re-reading stale inputs;
- released repository admission refuses v4, while an explicit test-only API exposes a read-only handle;
- the physical reader now feeds the format-neutral `WorkspaceSnapshot` adapter;
- frozen v3 and schema-valid v4 projections retain the same semantic digest:
  `sha256:cca698f3d4137f0f4220eaa22102c6625a1e0de439cea364e9c0bcca0f15b36f`.

The focused release matrix now includes reader, manifest, admission, and semantic
parity suites in addition to every Wave 0/1 contract suite.

## Fail-closed evidence

Tests cover traversal, Windows device and UNC paths, reparse/symlink rejection,
case collisions, wrong UID buckets, unknown kinds, duplicate UIDs and stream
sequences, malformed or truncated NDJSON, record/line/segment/count limits,
read-time file replacement, stale reader results, broken stream chains, ambiguous
v3/v4 markers, and missing migration evidence. Diagnostics expose stable codes and
artifact-relative locations, never Task or source content.

Task annotation source order is not inferred. A migration receipt must provide
`task_note_source_indexes`, otherwise semantic projection refuses with
`TASK_NOTE_SOURCE_INDEX_REQUIRED`. The bounded idempotency-ledger schema and the
migration-receipt schema remain explicit Wave 3 activation blockers.

## 2026-09-01 verification

- focused storage release matrix: 93 passed, 2 Windows symlink-privilege tests skipped;
- full backend: 659 passed, 3 OS-privilege symlink tests skipped;
- full frontend: 343 passed;
- storage package coverage: 91% statements with branch collection enabled;
- new reader/layout coverage: 86%; manifest 98%; repository 100%; semantic 96%;
- proportional frontend coverage gate: passed with one existing noncritical `main.tsx` warning;
- structural quality: 129 production files passed, storage maximum CCN 12;
- production frontend build: passed;
- export audit: 508 UTF-8 source-policy files passed;
- `git diff --check`: passed with checkout-policy line-ending warnings only.

No Store/service writer, startup format switch, migration executor, connection
activation, installer authority selection, or v3 source byte was changed by Wave 2.
