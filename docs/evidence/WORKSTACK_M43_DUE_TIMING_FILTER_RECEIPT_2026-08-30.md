# Work Stack M43 due timing filter receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `e5a063dcb48da27678b3056ffe40dd764bd765c2`
- Product tree: `6a1dbc9cef1cfcd186e21ef379e599596fd5be4a`
- Push: not performed

## Delivered behavior

- Workspace can filter active Tasks by `Overdue`, `Due today`, `Due soon`, or `No due date`.
- Timing is canonical URL state and round-trips through bounded local saved views.
- Pre-timing saved-view v1 records safely default to `All due timing` in memory.
- Timing chips can be removed independently and the all-clear action resets timing with the other
  filters.
- Filter membership and Board/Table labels use the same browser-local civil-calendar date.
- Done and dropped Tasks do not appear as currently overdue or unscheduled execution work.

## Verification

- RED-first URL, saved-view, and model tests failed before timing was implemented.
- Focused URL/saved-view/model/Workspace gate: 4 files / 25 tests passed.
- Full frontend gate: 33 files / 170 tests passed.
- Browser gate: all 22 Playwright scenarios passed, including an idempotently created active Task
  with no due date, deep-link filtering, chip removal, and accessibility scans.
- Direct production-browser inspection on port 8770 observed 12 `Due soon` rows and 12 matching
  due-soon labels, then removed only `timing=soon` through its chip.
- Production build passed without a chunk-size warning: initial bundle 499.36 kB, Table 3.12 kB,
  Task Drawer 35.59 kB, Graph 172.51 kB, Treemap 241.16 kB, CSS 92.90 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 252 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Timing is local URL/saved-view projection state and does not alter planning facts,
canonical snapshot serialization, or disclosure behavior.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Timing filters do not schedule, notify, reprioritize, or mutate a Task.
- Saved views remain local browser preferences, not shared server objects.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
