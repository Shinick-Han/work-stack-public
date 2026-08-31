# Work Stack M14 Graph viewport receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `0da585e3baa2138cb15f8ca796846686296affb3`
Product tree: `a63679297f120f64e425002f428310e8ae4e2f8d`

## Outcome

Graph keeps its complete in-memory node and edge projection, but graphs above 250 nodes now use
React Flow's built-in `onlyRenderVisibleElements` viewport renderer. Small graphs retain the prior
render path to avoid unnecessary visibility-calculation overhead. Fit view, pan/zoom, minimap,
relationship styling, selection, re-click clearing, and the shared Task Drawer remain unchanged.

The 1,000-Task scale fixture produces 1,020 unique nodes and at least 1,999 edges deterministically
inside the existing one-second model budget, and explicitly selects the virtualized path.

## Complete local gate

- Backend: 126 passed, one explicit Windows symlink privilege skip (unchanged product backend).
- Frontend: 30 files, 131 tests passed.
- Production build: passed; initial JS 490.74 kB and lazy Graph chunk 172.45 kB.
- Browser and accessibility: 11 Playwright scenarios passed, including Graph selection clearing and
  the primary-surface accessibility sweep.
- Source privacy audit: 212 UTF-8 source-policy files passed after this receipt was added.
- Diff audit: passed.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained outside
  every commit.

## Frozen boundaries and nonclaims

- Virtualization changes only DOM mounting; the authoritative planning projection and every edge
  remain complete and Work Stack-owned.
- Frozen contract, safety-policy, and conformance-kit bytes were not changed.
- No database, persisted FTS, export mutation, Conduit contact, client, watcher, relay, sync,
  back-sync, bulk import, agent start, or taskroom start was added.
- No Microsoft provider capability, external side effect, release, merge, or push is claimed.
