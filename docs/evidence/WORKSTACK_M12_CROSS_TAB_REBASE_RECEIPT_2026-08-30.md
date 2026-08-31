# Work Stack M12 bounded cross-tab rebase receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `54204d7fe54e17e0967580e8c293a2dfdcd8b1b2`
Product tree: `8bdd48a339a987711e193e7d855cf17ac7d4a92a`

## Outcome

Task editing now resolves a narrow class of safe cross-tab races without discarding either user's
work. After a revision conflict, the drawer reloads the authoritative Task and compares every local
pending field with the exact Task revision used for the failed request. If none of those fields
changed remotely, it retries once on the newer revision. If any pending field changed remotely, or
the bounded automatic retry races again, it preserves the draft and requires explicit review.

Cross-tab signaling was corrected at the transport boundary:

- successful reads no longer publish a planning-change signal;
- a confirmed mutation publishes one content-free signal;
- the subscriber de-duplicates the same nonce received through BroadcastChannel and localStorage;
- the bounded 64-nonce memory contains no Task or Capture content.

## Complete local gate

- Backend: 126 passed, one explicit Windows symlink privilege skip (unchanged product backend).
- Frontend: 30 files, 130 tests passed, including disjoint auto-rebase, same-field blocking,
  mutation-only publication, and nonce de-duplication.
- Production build: passed; initial JS 490.34 kB and lazy Task Drawer 31.78 kB.
- Browser and accessibility: 11 Playwright scenarios passed. The isolated two-tab mutation scenario
  also passed three fresh-server reruns after the feedback-loop fix.
- Source privacy audit: 210 UTF-8 source-policy files passed after this receipt was added.
- Diff audit: passed.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained outside
  every commit.

## Frozen boundaries and nonclaims

- Work Stack remains the sole planning-state authority and all writes still require the server's
  monotonic revision guard.
- Frozen contract, safety-policy, and conformance-kit bytes were not changed.
- This is bounded conflict handling, not real-time coauthoring, CRDT storage, or last-writer-wins.
- No export contacts Conduit; no Conduit client, watcher, relay, sync, back-sync, bulk import, agent
  start, or taskroom start was added.
- No Microsoft provider capability, external side effect, release, merge, or push is claimed.
