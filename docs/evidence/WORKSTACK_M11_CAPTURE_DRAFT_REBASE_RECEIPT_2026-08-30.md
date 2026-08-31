# Work Stack M11 Capture draft rebase receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `3ed1bc993eb651cf4742a44e0f59bfb83aff9a6d`
Product tree: `83a01c0180ec873a9fc672e60faad3cae0c29fa1`

## Outcome

An open source-based Task draft now has a deterministic Capture refresh policy:

- a higher workflow revision with the same sanitized source fingerprint is acknowledged without a
  false source conflict;
- a newer fingerprint automatically refreshes untouched title, context, and tags;
- a newer fingerprint never overwrites edited source-derived fields and instead offers explicit
  `Keep my draft` and `Refresh source fields` actions;
- refreshing source fields preserves user-selected priority, due date, and Objective alignment.

The policy uses the server-validated sanitized source fingerprint as the source-change discriminator.
No raw source content is introduced or retained by the browser.

## Complete local gate

- Backend: 126 passed, one explicit Windows symlink privilege skip (unchanged product backend).
- Frontend: 30 files, 128 tests passed, including pristine rebase, workflow-only revision, dirty
  preservation, explicit resolution, and planning-field preservation.
- Production build: passed; initial JS 490.25 kB.
- Browser and accessibility: 11 Playwright scenarios passed against the production build.
- Source privacy audit: 209 UTF-8 source-policy files passed after this receipt was added.
- Diff audit: passed.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained outside
  every commit.

## Frozen boundaries and nonclaims

- Work Stack remains the sole planning-state authority.
- Frozen contract, safety-policy, and conformance-kit bytes were not changed.
- No Capture refresh exports a snapshot or contacts Conduit.
- No raw Microsoft content, provider capability, external side effect, Conduit client, watcher,
  relay, sync, back-sync, bulk import, agent start, or taskroom start was added.
- No release, merge, or push is claimed.
