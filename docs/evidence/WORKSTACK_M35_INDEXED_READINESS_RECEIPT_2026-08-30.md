# Work Stack M35 indexed dependency readiness receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `07b5a2de0536666ef4d781335d79f81d0a33aad0`
- Product tree: `7b04f788a4008009fd4b2647facb80135126bdcb`
- Push: not performed

## Delivered behavior

- Dependency lookup now builds one Task-ID index for a Workspace snapshot and derives blockers from
  that index in O(number of dependency edges inspected).
- Board, Focus, Objective Hub, and Workspace readiness filtering reuse the indexed path.
- The existing convenience helper remains available for isolated single-Task callers and delegates
  to the same indexed implementation.
- Blocker semantics are unchanged: only `done` satisfies; open, started, dropped, unknown status,
  and missing dependency targets remain blocking evidence.
- The index is process-local derived state and is never persisted or exported.

## Verification

- RED-first 10,000-Task readiness filtering took approximately 5,020.8 ms and failed the 500 ms gate.
- After indexing, the focused relationship/Focus/Board/Objective/model gate passed 5 files / 42 tests;
  the complete six-test workspace model file finished in 18 ms in that run.
- Full frontend gate: 32 files / 158 tests passed; the model file finished in 29 ms.
- Browser gate: all 19 Playwright scenarios passed, including readiness deep links, blocker
  navigation, and primary-surface serious/critical accessibility scans.
- Production build passed: initial bundle 497.60 kB, Task Drawer 35.59 kB, CSS 91.26 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 242 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. The optimization is a read-only in-memory projection over Work Stack planning
facts. Snapshot schema, canonical bytes, disclosure, and explicit file export are unchanged.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- The index does not infer execution readiness beyond stored Work Stack dependency statuses.
- No server-side persisted index, database migration, or FTS dependency was introduced.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
