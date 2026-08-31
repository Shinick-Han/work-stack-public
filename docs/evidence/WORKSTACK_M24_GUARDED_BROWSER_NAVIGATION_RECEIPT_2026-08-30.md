# Work Stack M24 guarded browser navigation receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `582c19a56fbcbd7e3c2b58695381a5c7aea0177f`
- Product tree: `0a8f2a4738eb9e18482e4a881ca4cb6085ea1cc1`
- Push: not performed

## Delivered behavior

- The URL-state owner now accepts one synchronous navigation guard for programmatic updates and
  `popstate`.
- While a Task edit is dirty, saving, or failed, replacing or clearing that Task selection is
  rejected and the current canonical URL is restored.
- Task Drawer changes publish the lock immediately, including before a blur-triggered save begins.
- A native `beforeunload` handler requests browser confirmation for reload, tab close, and
  cross-document navigation while the same unsaved state exists.
- Confirmed save and explicit discard release the lock; ordinary view/filter changes that keep the
  same Task selected remain available.

## Verification

- RED-first URL-state tests demonstrated that both programmatic Task replacement and `popstate`
  changed the selected Task before the guard existed.
- Frontend unit/component gate: 31 files, 141 tests passed. This includes two guard-specific URL
  tests and Task Drawer lock/unload assertions.
- Playwright production gate: 15 scenarios passed. The unsaved-title scenario now traverses a real
  SPA history entry and proves browser Back leaves `task=T-0024` selected.
- Production build passed: initial JS 492.56 kB, CSS 88.59 kB; Task Drawer, Graph, and Treemap lazy
  chunks 33.92, 172.45, and 240.56 kB.
- Direct in-app production verification at port 8770 opened `T-0024` from Table, created the
  invalid unsaved state, invoked browser Back, and observed the exact Task URL and disabled close
  remain until explicit discard restored the confirmed title.
- Backend behavior was unchanged; most recent full gate remains 136 passed with one Windows symlink
  privilege skip.
- Source export audit: 228 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. Browser guarding changes no snapshot schema or canonical bytes
and adds no Conduit client, transport, watcher, relay, back-sync, bulk import, execution inference,
mutable link table, or Taskroom start.

## Nonclaims

- Browser vendors control the exact wording and presentation of native unload prompts.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No OS-signing certificate was added.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
