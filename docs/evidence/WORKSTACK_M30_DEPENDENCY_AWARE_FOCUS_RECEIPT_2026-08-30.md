# Work Stack M30 dependency-aware Focus receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `1a94bb8d2ec27f87933eec43fe5ef8a37df962b7`
- Product tree: `8542f9d05f589b8577d4566e40b3790f96f0f799`
- Push: not performed

## Delivered behavior

- Focus candidates now carry a deterministic blocker projection derived from their exact dependency
  IDs and the loaded Workspace Task statuses.
- Only a `done` dependency is satisfied. Open, started, dropped, and missing dependencies keep the
  candidate blocked.
- Actionable candidates sort before blocked candidates without losing the existing urgency, due,
  priority, status, and ID ordering within each group.
- Blocked rows show `Blocked by <Task ID>`, the prerequisite title or an unavailable marker, and a
  disabled `Blocked` action instead of Start/Done.
- Opening the blocked Task remains available for inspection and relationship traversal.

## Verification

- RED-first model/UI coverage failed before blocker projection and disabled actions existed.
- Focus model and page gate: 2 files / 26 tests passed.
- Full frontend gate: 32 files / 148 tests passed.
- Browser gate: all 16 Playwright scenarios passed. The Focus scenario verified that T-0002 is
  blocked by unfinished T-0001 while actionable T-0005 retains Mark done and append-only Undo.
- Direct production-browser inspection on port 8770 showed the blocked summary and badge, a disabled
  T-0002 action, and an enabled T-0005 action.
- Production build passed: initial bundle 493.46 kB, Task Drawer 35.88 kB, CSS 89.75 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one
  Windows symlink privilege case skipped.
- Source export audit: 237 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. Focus does not infer or write Conduit execution state and changes
no snapshot schema or canonical bytes. No Conduit client, transport, watcher, relay, back-sync, bulk
import, mutable link table, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Dependency readiness is a local planning projection, not proof that external work or a Conduit
  run completed.
- Focus does not automatically change Task status, dependency status, priority, or due date.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
