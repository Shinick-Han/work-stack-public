# Work Stack M36 filtered result totals receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `6646be68e5eedb980cce5ff1315c9fde7012b5c6`
- Product tree: `ef60b8ca41f883c01bf7ffbc95484816b2686ebe`
- Push: not performed

## Delivered behavior

- The Workspace filter summary uses the exact shared model used by Graph, Board, Treemap, and Table.
- Any active search, status, priority, readiness, or Objective filter displays `matched of total`.
- With no active filter, the summary retains the concise complete Task count.
- Relationship totals remain the complete Workspace relationship count and are not misrepresented as
  a filtered graph-edge count.
- The calculation is memoized by Workspace task snapshot and filter coordinates.

## Verification

- RED-first component coverage observed the stale complete count before implementation.
- The isolated component gate proves one blocked result is reported as `1 of 2 tasks`.
- Full frontend gate: 32 files / 159 tests passed.
- Browser gate: all 19 Playwright scenarios passed; the readiness scenario accepts mutable fixture
  totals while requiring the canonical `matched of total` summary shape.
- Direct production-browser inspection on port 8770 observed
  `7 of 30 tasks · 104 relationships` at `?view=board&readiness=blocked`.
- Production build passed: initial bundle 497.84 kB, Task Drawer 35.59 kB, CSS 91.26 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 243 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. The total is a local read-only display projection. Snapshot schema, canonical
bytes, disclosure, and explicit file export are unchanged.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- The relationship count is intentionally the complete Workspace total, not a claim about only the
  currently rendered Task subset.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
