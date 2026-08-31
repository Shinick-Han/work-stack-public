# Work Stack M6A unified search and Table View receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `604ed5843f4b021c81364a746e1963372cc281cd`
Product tree: `a551f8d7deab3c2346b2404c4f30b536dd815bef`

## Delivered

- strict bounded `GET /api/v1/search` across Task, Objective, note, sanitized Capture,
  and minimal activity projections;
- privacy-minimized search response with no reply body, reply target, raw locator,
  recipient, or credential field;
- debounced unified command-palette retrieval and stable navigation targets;
- sortable Table View sharing Workspace filters, Task selection, drawer, and
  revision-guarded status mutation;
- shortcut `8` and explicit browser coverage for Table selection and Objective search.

## Evidence

- backend: 122 passed, 1 Windows symlink privilege skip;
- frontend: 25 files, 114 tests passed;
- Playwright: 6 passed, including eight planning surfaces in the axe sweep;
- production build: passed, 913 modules;
- source export audit: 189 UTF-8 text files passed;
- `git diff --check`: passed;
- disposable browser QA: Table rendered 30 Tasks; the same Task row opened and cleared
  selection; `Release quality` returned a Task and Objective and navigated to O-1.

## Docking boundary and nonclaims

Frozen docking coordinates remain unchanged:

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`;
- safety-policy root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`;
- conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`.

This milestone adds no Conduit client, transport, watcher, relay, back-sync, bulk import,
mutable link table, agent start, or taskroom start. Search and Table operate only on Work
Stack planning projections. It does not yet claim full-text indexing, saved filters,
cross-tab refresh, durable drafts, bounded undo, bundle splitting, or graph scale proof.

No push was performed.
