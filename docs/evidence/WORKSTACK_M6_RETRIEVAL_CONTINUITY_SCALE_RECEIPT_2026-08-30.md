# Work Stack M6 retrieval, continuity, and scale receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Final product commit: `1dad3bc63e97acc0281444a96533af87f2cb6220`
Final product tree: `9d1782ca172f4fd0cbd942effe56e78afd3fba4b`

## Product coordinates

- unified search and Table: `604ed5843f4b021c81364a746e1963372cc281cd`;
- cross-tab refresh, Quick Add draft recovery, and bounded status Undo:
  `168189b5462fafbc2f7cc5a330d8b80442900205` plus whitespace-only
  `893f22a`;
- saved filters: `3a1c9fc8ba1d72d72a4a12cb0ccc995025238c97`;
- lazy visualizations, graph scale evidence, readiness and request correlation:
  `1dad3bc63e97acc0281444a96533af87f2cb6220`.

## Machine evidence

- backend: 123 passed, 1 explicit Windows symlink privilege skip;
- frontend: 29 files, 123 tests passed;
- Playwright: 8 passed, including real two-tab refresh, page-reload draft recovery,
  saved-filter restoration, status Undo, and eight-surface axe coverage;
- source export audit: 198 UTF-8 text files passed;
- production build: 917 modules, no oversize-chunk warning;
- initial JS: 480.88 kB / 143.94 kB gzip, down from 936.37 kB / 287.63 kB gzip;
- lazy chunks: Task Drawer 31.17 kB, Graph 172.34 kB, Treemap 240.56 kB;
- deterministic graph model: 1,000 Tasks + 20 Objectives + at least 1,999 relations,
  unique node/edge IDs and repeated-byte-equivalent model, measured at 91 ms in the full run;
- readiness probe: HTTP 200, content-free `{status: ready, api_version: v1}` and unique
  16-hex request correlation ID;
- `git diff --check`: passed.

## Safety and docking nonclaims

Cross-tab signaling contains only version, source, nonce, and timestamp. Saved filters and
Quick Add drafts are bounded, strict local schemas. Undo appends a new revision-guarded
planning transition. No feature changes the frozen docking contract or snapshot bytes.

Frozen coordinates remain:

- contract SHA-256 `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`;
- safety root `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`;
- kit root `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`.

This milestone adds no Conduit client, transport, watcher, relay, background sync,
back-sync, bulk import, mutable link table, agent start, or taskroom start. Readiness is
local server readiness and never a Microsoft-provider health claim. No push was performed.
