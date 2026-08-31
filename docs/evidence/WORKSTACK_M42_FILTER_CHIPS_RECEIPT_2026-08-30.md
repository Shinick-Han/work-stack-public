# Work Stack M42 removable filter chips receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `1c487accbe3e9b194737ef575b7d014097456ec6`
- Product tree: `88ec351c223cc80d62474ead0201fb60fa15dbf6`
- Push: not performed

## Delivered behavior

- Search, Objective, status, priority, and readiness summaries are independently removable controls.
- Clearing one chip updates only that canonical URL field and preserves every other active filter.
- The existing all-at-once `Clear filters` action remains available.
- Each chip has a complete accessible name and visible focus treatment.
- Table is now loaded as a separate view chunk, returning the initial bundle below the existing
  500 kB warning boundary after the new controls were added.

## Verification

- RED-first Workspace test failed before chips were actionable.
- Focused Workspace gate: 1 file / 3 tests passed.
- Full frontend gate: 33 files / 168 tests passed.
- Browser gate: all 21 Playwright scenarios passed, including independent readiness-filter removal
  and primary-surface serious/critical accessibility scans.
- Direct production-browser inspection on port 8770 cleared only `Blocked work`; the status and
  Objective chips remained and only `readiness=blocked` disappeared from the URL.
- Production build passed without a chunk-size warning: initial bundle 497.81 kB, Table 3.11 kB,
  Task Drawer 35.59 kB, Graph 172.51 kB, Treemap 241.16 kB, CSS 92.90 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 251 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Filter controls are local URL projections and do not alter planning facts,
canonical snapshot serialization, or disclosure behavior.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Local filters are not server-side shared views or authorization boundaries.
- Removing a filter does not edit or reveal data outside the already loaded Workspace projection.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
