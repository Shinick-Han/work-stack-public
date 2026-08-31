# Work Stack M47 Table preference receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `ca92a3daba444d13c096e5211394c3530d5194c5`
- Product tree: `80c11d962a0f0fc498835f05d65801aad7eb7cab`
- Push: not yet performed for this product commit when the receipt was written.

## Delivered behavior

- Table users can choose Comfortable or Compact rows through an accessible pressed-state control.
- Sort field, ascending/descending direction, and density survive reload as a strict browser-local
  preference record.
- Unknown fields, invalid enum values, and malformed JSON are removed instead of partially trusted.
- Compact mode reduces row height and hides tag subtitles without hiding the Task title.
- At 720px and below, ID, Steps, Context, and Revision technical columns collapse. Task, Status,
  Priority, Due, Readiness, and Objectives stay visible in the scrollable table.
- A caption exposes the current sort field and direction to assistive technology.

## Verification

- RED-first tests initially failed because the preference module and density controls did not exist.
- Full frontend gate: 35 files / 176 tests passed.
- Full Chromium Playwright gate: 23/23 passed, including reload persistence and 390×844 column
  priority evidence plus the existing serious/critical axe scan.
- Production build passed without a chunk-size warning: initial JS 438.79 kB, WorkspacePage
  62.45 kB, Table 5.15 kB, Task Drawer 35.59 kB, Graph 173.04 kB, Treemap 241.16 kB.
- Source export audit before this receipt: 258 UTF-8 text files. Expected count after adding this
  receipt: 259.
- `git diff --check` passed.
- Backend behavior was unchanged; its most recent complete gate remains 138 tests passed with one
  explicit Windows symlink privilege skip.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Table preferences are local presentation state and do not affect Task identity,
revision, planning-status facts, canonical snapshot serialization, or disclosure confirmation.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Table preferences do not synchronize between browser profiles or devices.
- Narrow-screen evidence covers Chromium at 390×844, not every browser or assistive-technology pair.
- OS publisher signing and real Microsoft provider Gate 0 evidence remain separate lanes.
- No Conduit consumer implementation was performed.
