# Work Stack M15 Capture trust gate receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `87e567059dab75b8e683c6ab1adb27c3202b1351`
Product tree: `de26e6dc5fc22077f30a51e2e14d6caef049256b`

## Outcome

Capture provenance and provider capability are now represented as separate facts:

- generic `Import packet` accepts only `capture_mode: manual` and directs OOB-shaped packets to the
  dedicated Microsoft 365 agent-result importer;
- Inbox cards and the Capture Drawer display `Supplied provenance · Gate 0 unverified` for an OOB
  shape while that exact provider's read Gate is false;
- `OOB verified` appears only when the provider-specific build gate is true;
- manual imports retain their existing neutral label and strict raw-content rejection.

This closes the risk that a self-asserted packet could be mistaken for retained real-tenant Gate 0
evidence. It does not activate either Microsoft provider.

## Complete local gate

- Backend: 126 passed, one explicit Windows symlink privilege skip (unchanged product backend).
- Frontend: 30 files, 133 tests passed, including generic OOB refusal and false/true Gate label
  behavior.
- Production build: passed; initial JS 490.90 kB.
- Browser and accessibility: 11 Playwright scenarios passed against the production build.
- Source privacy audit: 214 UTF-8 source-policy files passed after this receipt was added.
- Diff audit: passed.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained outside
  every commit.

## Frozen boundaries and nonclaims

- Work Stack remains the sole planning-state authority.
- Frozen contract, safety-policy, and conformance-kit bytes were not changed.
- Gate flags remain false absent retained real-tenant evidence; no Outlook/Teams read or reply
  capability is claimed.
- No OAuth token, raw Microsoft content, external side effect, Conduit client, watcher, relay, sync,
  back-sync, bulk import, agent start, or taskroom start was added.
- No release, merge, or push is claimed.
