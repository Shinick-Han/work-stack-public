# Work Stack M8 usability and local continuity receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`

## Product coordinates

- Focus inline Start/Done and bounded Undo: commit
  `41a028f1908b85b5f0a1ca1291d349ad54cc34c4`, tree
  `5c131910a60e4060ea9d115daad73db3d3172224`.
- Objective/KR title, quarter, description, and target editing: commit
  `44ff7eb743b070bfe1e53877e50a164dca44ec70`, tree
  `f91488c8a4b8bed855adeac5242f26efd00bfdb0`.
- Verified live backup download and Local continuity UI: commit
  `f26d73c9244136d9027ba268dd365473e8547e0e`, tree
  `59f7489029a0d645fcb324af85ecefa2ad7ff3c4`.

## Machine evidence

- Backend: 125 passed, one explicit Windows symlink privilege skip.
- Frontend: 30 files, 124 tests passed.
- Production build: passed; initial JS 486.83 kB. Task Drawer, Graph, and Treemap remain
  separate lazy chunks.
- Browser: 10 Playwright scenarios passed, including explicit full-backup disclosure,
  verified download filename, Focus transition/Undo, Objective/KR edit, cross-tab refresh,
  draft recovery, and axe serious/critical checks.
- Source privacy audit: 205 UTF-8 source-policy files passed after this receipt was added.
- Diff whitespace audit: passed.
- Backup round-trip: the running-store owner produced a ZIP that the existing strict offline
  verifier accepted. All nine JSON store files were byte-identical before and after creation.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained
  outside these commits.

## Docking invariants retained

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`.
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`.
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`.
- No contract, safety-policy, or conformance-kit bytes changed.
- Work Stack remains the sole planning-state authority. The full local backup is a private
  continuity artifact, not a Conduit snapshot or transport.
- Task snapshot export still selects one immutable revision, requires disclosure, never mutates
  planning state, and never contacts Conduit.
- No Conduit client, watcher, relay, sync/back-sync, bulk import, taskroom start, or execution
  inference was added.

## Nonclaims and remaining debt

- No push or release was performed.
- No real Microsoft tenant read or reply capability was enabled.
- No live browser restore was added; restore remains offline and fail-closed by design.
- No OS signing/notarization or bundled Python runtime was added.
- No SQLite/Postgres/FTS migration, viewport virtualization, or server-shared saved filters were
  added. Those remain dogfood-driven scale decisions.
