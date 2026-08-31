# Work Stack M29 relationship filter scale receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `1a18336621d390541af1e4745770a26c7155fa52`
- Product tree: `a8550d75d7991686957c879b0ad0dd4f57447752`
- Push: not performed

## Delivered behavior

- Parent and dependency cycle candidates are now derived through one reverse-edge traversal for
  each relationship kind.
- The Task Drawer filters each choice by set membership instead of rebuilding and traversing the
  complete Task map once per option.
- Visited IDs bound traversal even if an unrelated pre-existing cycle is present.
- The set is process-local React projection state only; no reverse index or new planning record is
  persisted.

## Verification

- Relationship model gate: 4 tests passed, including complete transitive candidate sets, unrelated
  cyclic input, and a synthetic 10,000-Task chain inside the 500 ms bound.
- During the complete suite the relationship-model test file, including that scale case, finished
  in 29 ms.
- Full frontend gate: 32 files / 145 tests passed.
- Browser gate: all 16 Playwright scenarios passed.
- Production build passed: initial bundle 492.56 kB, Task Drawer 35.88 kB, CSS 89.54 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one
  Windows symlink privilege case skipped.
- Source export audit: 236 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. Relationship filtering changes no snapshot schema or canonical
bytes and adds no Conduit client, transport, watcher, relay, back-sync, bulk import, execution
inference, mutable link table, or Taskroom start.

## Nonclaims and remaining dependencies

- The native select still renders the eligible current-Workspace choices; this milestone bounds
  graph computation, not browser DOM option virtualization.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
