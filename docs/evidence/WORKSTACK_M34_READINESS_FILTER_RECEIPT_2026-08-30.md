# Work Stack M34 readiness filter receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `f6165991456bde8cc2ac172a6de6fe7aa552802b`
- Product tree: `64c1d67aee30c7214f33c0db6eac8903b2d9c8d2`
- Push: not performed

## Delivered behavior

- Workspace offers `All readiness`, `Ready to act`, and `Blocked work` filters across Graph,
  Board, Treemap, and Table.
- Ready and blocked filters include only active (`open` or `started`) planning Tasks.
- A Task is blocked whenever any dependency is not `done`, including a dropped or missing target.
- The calculation uses the complete Workspace task set before other display filters are applied.
- Readiness is encoded as a canonical `readiness=ready|blocked` URL parameter and included in
  bounded local saved views.
- Existing strict saved-view v1 records without the field load with `All readiness`; malformed or
  unknown values still fail closed.

## Verification

- RED-first view-model, URL, and saved-view tests failed before readiness was modeled.
- Focused readiness gate: 3 files / 19 tests passed.
- Full frontend gate: 32 files / 157 tests passed.
- Browser gate: all 19 Playwright scenarios passed, including blocked/ready URL filtering and the
  serious/critical accessibility scan for every primary planning surface.
- Direct production-browser inspection on port 8770 selected `Blocked work`, observed
  `?view=board&readiness=blocked`, displayed T-0002, and excluded ready T-0001.
- Production build passed: initial bundle 497.50 kB, Task Drawer 35.59 kB, CSS 91.26 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 241 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Filtering is local display state, not a new planning or execution fact. Snapshot
schema, canonical bytes, disclosure, and explicit file export are unchanged.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Ready means only that Work Stack dependency planning facts are satisfied; it does not prove
  external capacity, scheduling, or Conduit execution state.
- No automatic priority, status, due-date, or dependency change is performed.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
