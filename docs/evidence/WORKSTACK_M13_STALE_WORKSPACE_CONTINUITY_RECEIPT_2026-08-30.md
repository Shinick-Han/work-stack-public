# Work Stack M13 stale Workspace continuity receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `6e4900f79d894f7964659b92b245487e44cacac7`
Product tree: `d6bd2b9feb03ea92a854c6f059b895a303fdc6e6`

## Outcome

A background Workspace refresh failure no longer replaces usable planning state with a blocking
error page. When a confirmed projection exists, Work Stack keeps every surface usable and displays
a clearly labeled warning with the exact read error and an explicit retry action. An initial load
with no usable projection still fails closed through the existing full error state.

The existing Quick Add response-loss and authoritative Task Drawer behavior is unchanged. A newly
confirmed Task can remain open even if the broader Workspace refresh fails; the stale main surface
is never labeled as freshly confirmed.

## Complete local gate

- Backend: 126 passed, one explicit Windows symlink privilege skip (unchanged product backend).
- Frontend: 30 files, 130 tests passed, including a failed post-create Workspace refresh with the
  created Task Drawer, stale main surface, exact warning, and retry action all present.
- Production build: passed; initial JS 490.74 kB.
- Browser and accessibility: 11 Playwright scenarios passed against the production build.
- Source privacy audit: 211 UTF-8 source-policy files passed after this receipt was added.
- Diff audit: passed.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained outside
  every commit.

## Frozen boundaries and nonclaims

- Work Stack remains the sole planning-state authority; the warning does not mutate or fabricate
  planning state.
- Frozen contract, safety-policy, and conformance-kit bytes were not changed.
- No export contacts Conduit; no Conduit client, watcher, relay, sync, back-sync, bulk import, agent
  start, or taskroom start was added.
- No Microsoft provider capability, external side effect, release, merge, or push is claimed.
