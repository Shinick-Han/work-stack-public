# Work Stack M0 Mutation Safety Receipt

Date: 2026-08-30
Result: PASS

## Product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Worktree: dedicated local `ui-actions` task worktree
- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `a37cbf2806080f65878de51caab937a20cccb0be`
- Product tree: `1438ed4bc719345801cb45b45a8321ce912dbce4`
- Push: not performed

## Changed behavior

- Objective, Graph note, Task note, and subtask creation require an Idempotency-Key at
  the versioned browser API boundary.
- One logical UI intent retains the same key through the automatic transport retry and
  an explicit unchanged user retry.
- First commits and replay responses are stored atomically with the created planning
  record. A reused key with a different method, path, or body digest fails closed.
- Task-note and subtask responses are frozen with the resulting Task revision, so a
  replay does not project a later edit as the original response.
- A lost subtask-status response triggers an authoritative Task read. The UI reports
  success only when the requested status is present; otherwise it preserves the error.
- A failed background Workspace refresh no longer converts a successfully committed
  Objective or Graph note into a visible mutation failure.

## Machine evidence

- Backend: 112 tests passed; one Windows symlink-privilege test skipped explicitly.
- Frontend: 22 files, 109 tests passed.
- Production TypeScript/Vite build: passed; 910 modules; main JS 910.07 kB with the
  existing code-splitting advisory.
- Source export audit: 163 UTF-8 files passed.
- Disposable runtime audit: 10 UTF-8 files passed.
- Python compile audit and `git diff --check`: passed.
- Restart replay: all four creation kinds replayed their exact stored response without
  duplicate records; four intents produced four idempotency records.
- Concurrency: two concurrent Objective requests with one key produced one Objective
  and one first/replay pair.

## Production-build browser evidence

Disposable runtime: `.runtime/ui-actions-demo-20260830`; server:
`http://127.0.0.1:8770/`.

- Created Objective `O-6`.
- Created Graph note `N-0007` linked to `O-6`.
- Added one subtask and one Task note to synthetic Task `T-0001`.
- Browser console errors: 0.

## Docking invariants and nonclaims

- Frozen Contract Revision 4, Safety Policy Revision 5, and conformance-kit bytes were
  not changed.
- Work Stack remains the sole planning-state authority.
- Snapshot export remains explicit, read-only, one revision at a time, and does not
  contact Conduit.
- No Conduit client, watcher, transport, relay, back-sync, bulk import, Taskroom start,
  or agent start was added.
- No Microsoft provider was contacted and no provider capability was enabled.
- This receipt does not claim externally exactly-once Microsoft delivery, CI coverage,
  accessibility conformance, installer readiness, or a pushed branch.
