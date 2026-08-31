# Work Stack M28 guided Task relationships receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `1be5f1ce27dc90fa7851e802b42ef51aebe6565e`
- Product tree: `40314121e0e3c61808b63f5ac5a37ca9098ab1f9`
- Push: not performed

## Delivered behavior

- The Parent picker omits descendants that would create a direct or transitive parent cycle.
- The dependency picker omits the current Task, existing dependencies, and Tasks whose dependency
  chain would return to the current Task.
- Dependencies are added from known Task ID/title choices and shown as removable relationship chips.
- Add and remove operations reuse the existing serialized revision-guarded PATCH pipeline,
  cross-tab refresh, conflict handling, and unsaved-navigation guard.
- The M27 backend validator remains authoritative if the Workspace projection is stale or a caller
  bypasses the browser.

## Verification

- RED-first relationship-model coverage failed before the cycle projection module existed.
- Relationship model and Task Drawer gate: 2 files / 16 tests passed.
- Full frontend gate: 32 files / 143 tests passed.
- Browser gate: all 16 Playwright scenarios passed, including exclusion of T-0023 as a parent and
  T-0024 as a dependency for T-0019 plus a complete add/remove dependency round trip.
- Direct production-browser inspection on port 8770 measured zero blocked options, showed the new
  T-0001 relationship chip after selection, and removed it back to the original relationship set.
- Production build passed: initial bundle 492.56 kB, Task Drawer 35.73 kB, CSS 89.54 kB.
- Backend behavior was unchanged after M27; its most recent complete gate is 138 tests passed with
  one Windows symlink privilege case skipped.
- Source export audit: 235 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. Guided relationship editing changes no snapshot schema or
canonical bytes and adds no Conduit client, transport, watcher, relay, back-sync, bulk import,
execution inference, mutable link table, or Taskroom start.

## Nonclaims and remaining dependencies

- Candidate filtering uses the loaded Workspace projection; the server still decides whether the
  submitted relationship is valid at its authoritative revision.
- The picker is bounded to the current Workspace and is not a cross-workspace relationship tool.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
