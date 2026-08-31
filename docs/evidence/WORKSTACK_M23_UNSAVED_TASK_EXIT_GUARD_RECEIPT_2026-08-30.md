# Work Stack M23 unsaved Task exit guard receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `43701f140191bcc071a43be9ed7076760c0633e3`
- Product tree: `d67eff93244920e408ba00dc390b4f01cdac8361`
- Push: not performed

## Delivered behavior

- A failed Task PATCH retains the local draft and prevents closing the Drawer, following an
  Objective or related Task, exporting the committed snapshot, or opening secondary Task actions.
- The error surface offers explicit `Retry save` when a queued patch exists and always offers
  `Discard unsaved changes`.
- Discard restores the last confirmed Task, clears only the local failed intent, and re-enables
  navigation. It does not send a compensating planning mutation.
- An empty title becomes a visible invalid unsaved state instead of silently skipping its blur save.
- A same-click blur-and-navigation race checks the save run synchronously before leaving.

## Verification

- RED-first component coverage demonstrated that the Drawer close control remained enabled after a
  failed save before implementation.
- Frontend unit/component gate: 31 files, 139 tests passed.
- Playwright production gate: 15 scenarios passed, including empty-title close refusal, explicit
  discard and restore, and the serious/critical accessibility sweep.
- Production build passed: initial JS 492.12 kB, CSS 88.59 kB; Task Drawer, Graph, and Treemap lazy
  chunks 33.49, 172.45, and 240.56 kB.
- Direct in-app production verification at port 8770 cleared `T-0024`'s title, observed the Drawer
  remain open with close disabled and `Discard unsaved changes`, then restored `Publish insight
  review` with close re-enabled.
- Backend behavior was unchanged; most recent full gate remains 136 passed with one Windows symlink
  privilege skip.
- Source export audit: 227 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. The guard changes no snapshot schema or canonical bytes and adds
no Conduit client, transport, watcher, relay, back-sync, bulk import, execution inference, mutable
link table, or Taskroom start.

## Nonclaims

- This is not multi-user real-time collaborative editing; same-field conflicts still require
  explicit review.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No OS-signing certificate was added.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
