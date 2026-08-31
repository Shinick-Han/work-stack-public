# Work Stack M41 due timing receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `d998bcb77d7ae65b06be3072c6a8d4eb0d16a02f`
- Product tree: `9746a695699dde88f4b13f654411c9019eca6c82`
- Push: not performed

## Delivered behavior

- Board and Table translate active Task due dates into `N days overdue`, `Due today`, or
  `Due in N days` within the existing seven-day window.
- Focus and Workspace views reuse one DST-independent civil-calendar due projection.
- Dates outside the urgency window retain their canonical date; absent dates remain explicit.
- Done and dropped Tasks retain historical dates without being mislabeled as currently overdue.
- Accessible labels include the Task ID and the full due meaning; the exact date remains in the
  tooltip.

## Verification

- RED-first Board and Table tests failed before due timing existed.
- Focused Focus/Board/Table gate: 3 files / 37 tests passed.
- Full frontend gate: 33 files / 167 tests passed.
- Browser gate: all 21 Playwright scenarios passed, including the primary-surface
  serious/critical accessibility scans.
- Direct production-browser inspection on port 8770 observed `Due in 2 days` for `T-0001` in both
  Board and Table from the same local-calendar projection.
- Production build passed: initial bundle 499.98 kB, Task Drawer 35.59 kB, Graph 172.51 kB,
  Treemap 241.16 kB, CSS 92.49 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 250 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Due timing is a local read-only presentation of an existing planning date and
does not alter canonical snapshot serialization or disclosure behavior.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Due timing does not reschedule, reprioritize, notify, or complete a Task.
- Relative labels use the browser's local calendar, not an external calendar or provider clock.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
