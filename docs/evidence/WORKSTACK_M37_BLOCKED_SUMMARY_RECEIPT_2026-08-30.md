# Work Stack M37 blocked-work summary receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `801bdced8f6d163fd5bd1f4e09878620e34ffe38`
- Product tree: `599777ef019a996d9ede9c72dcba6caae37fd383`
- Push: not performed

## Delivered behavior

- The fourth Workspace summary metric is now `Blocked` instead of an ambiguous P0-only
  `Needs focus` count.
- The headline counts every active Task with at least one unfinished, dropped, or missing dependency.
- The existing number of active P0 Tasks remains visible as supporting text.
- The calculation uses the indexed shared readiness filter over the complete Workspace snapshot.

## Verification

- RED-first component coverage failed before the `Blocked` metric existed.
- The isolated two-Task fixture reports one blocker and two active P0 Tasks.
- Full frontend gate: 32 files / 159 tests passed.
- Browser gate: all 19 Playwright scenarios passed, including serious/critical accessibility scans.
- Direct production-browser inspection on port 8770 observed `Blocked 7` and `2 P0 active tasks`
  alongside Active, Completion, and Aligned metrics.
- Production build passed: initial bundle 497.90 kB, Task Drawer 35.59 kB, CSS 91.26 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 244 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. The metric is a local read-only planning projection. Snapshot schema, canonical
bytes, disclosure, and explicit file export are unchanged.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Blocked reflects Work Stack dependency statuses only; it is not external execution telemetry.
- P0 count remains descriptive and does not automatically change priority or schedule.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
