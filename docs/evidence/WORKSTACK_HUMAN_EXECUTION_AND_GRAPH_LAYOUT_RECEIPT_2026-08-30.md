# Work Stack Human Execution and Graph Layout Milestone Receipt

Date: 2026-08-30 (Asia/Seoul)

## Outcome

This milestone adds two bounded capabilities that remain useful before and after Conduit docking:

1. a human-only Work Session loop on the Focus surface; and
2. deterministic automatic planning-graph layout using `elkjs` 0.11.1.

Work Stack remains the sole authority for planning state. Starting, pausing, resuming, or stopping a Work Session does not change a Task status, Task revision, or docking snapshot. A stopped session becomes a persisted Worklog draft, and only an explicit user confirmation appends its Done, Next, and Blocker facts to the Worklog.

The graph layout changes positions only. Persisted relationship identity, source, target, kind, and selection semantics are unchanged.

## Repository coordinates

- Worktree: repository-local isolated worktree `.worktrees/human-execution-layout`
- Branch: `codex/workstack-human-execution-layout-20260830`
- Starting commit: `e1790659b617b2fa21a3b50c97235773527e9395`
- Starting tree: `a1d899d0545cc4330f281d7bde8db848fc9dfee6`
- Reviewed product commit: `8deaba52c65ffcc504b3a11a1b5eeda1653da678`
- Reviewed product tree: `cb3e1fc69077c674eda56eb3329c3485b1405be4`
- State at receipt creation: the product commit is local and unpushed; only this receipt remains outside that product tree until its handoff-only commit.

The separate user-owned questionnaire in the `ui-actions` worktree was not modified or copied into this branch.

## Work Session contract

- Persistence: optional `sessions` arrays inside existing dated records in `worklog.json`; no new store file and no planning-schema migration.
- Stable identity: repository-wide `WS-000001`-style monotonic identifiers.
- States: `running`, `paused`, and `stopped`.
- Worklog states: `not_ready`, `pending`, and `recorded`.
- Cardinality: at most one running or paused session across the workspace.
- Time accounting: persisted UTC whole-second segments; paused time is excluded.
- Restart behavior: active, paused, and pending sessions survive a process restart.
- Mutation safety: every POST uses the existing browser CSRF boundary and an idempotency key.
- Fail-closed behavior: malformed session identity, timestamps, segments, state combinations, or multiple active sessions make the projection unavailable instead of guessing.
- Worklog confirmation: writes the Task title snapshot, `session_id`, and `duration_seconds` together with the user-reviewed Done, Next, and Blocker arrays.
- Weekly review: aggregates focused duration per Task without inferring Task completion or progress.

Versioned API:

- `GET /api/v1/work-sessions`
- `POST /api/v1/work-sessions`
- `POST /api/v1/work-sessions/{session_id}/pause`
- `POST /api/v1/work-sessions/{session_id}/resume`
- `POST /api/v1/work-sessions/{session_id}/stop`
- `POST /api/v1/work-sessions/{session_id}/worklog`

## Graph layout contract

- Engine: `elkjs` 0.11.1, pinned exactly and licensed under EPL-2.0.
- Determinism: nodes and edges are sorted before layout; reversed input order produces the same output.
- Reading order: Objective → aligned Task → dependent Task → linked Note.
- Semantic preservation: Work Stack edges continue to render with their original stored direction. Reversal happens only in the private ELK layout input.
- Failure behavior: the prior deterministic column layout remains the immediate render and fallback if the asynchronous layout cannot complete.
- Loading behavior: the 1.44 MB uncompressed ELK engine is a separate dynamic chunk and is fetched only when graph layout is requested. It does not inflate the initial application or ordinary Workspace chunk.
- Scale behavior: existing React Flow virtualization policy remains active above 250 visible nodes.

## Changed product areas

- `workstack/service.py`: Work Session state machine, validation, persistence, idempotency, Worklog confirmation, and duration roll-up.
- `workstack/server.py`: strict versioned Work Session HTTP routes and explicit conflict mapping.
- `frontend/src/api/client.ts`, `frontend/src/domain/types.ts`, `frontend/src/domain/schemas.ts`: validated browser contract.
- `frontend/src/features/focus/FocusPage.tsx`, `WorkSessionPanel.tsx`: human execution controls and explicit Worklog confirmation.
- `frontend/src/features/review/DailyReviewPage.tsx`: reviewed session duration in day and seven-day views.
- `frontend/src/features/workspace/views/GraphView.tsx`, `graphLayout.ts`: lazy deterministic ELK integration.
- `frontend/package.json`, `frontend/package-lock.json`, `licenses/elkjs-0.11.1-LICENSE.md`: exact dependency and retained license.
- bounded unit, API, UI, and Chromium tests for both capabilities.

## Verification evidence

- Backend/domain/API: 148 tests passed; 1 Windows symlink-privilege scenario skipped explicitly.
- Frontend unit/component: 36 files, 181 tests passed.
- Chromium product and accessibility: 33 scenarios passed, including the complete human session → explicit Worklog → Daily Review loop.
- Production build: passed under Vite 7.3.6.
- Serious/critical axe findings: zero on Graph, Board, Treemap, Table, Focus, Inbox, Daily Review, and Objective Hub.
- Source privacy audit: 274 UTF-8 text files passed the repository source policy.
- Diff whitespace check: passed.

Known build note: Vite reports a chunk-size warning for the isolated ELK engine. The chunk is intentionally lazy and graph-only; removing the warning without hiding it is future performance work, not a correctness blocker.

## Click-by-click reviewed demo

### Human Work Session

1. Open **Focus** from the left navigation.
2. Choose an unblocked candidate and click **Focus** in its row.
3. Confirm the **Current work session** card appears and its timer advances.
4. Click **Pause session**; confirm the timer stops and the action changes to **Resume session**.
5. Click **Resume session**; confirm timing continues.
6. Click **Stop session**.
7. Confirm a **Worklog ready** card appears. The Task planning-status button must still show the same action as before the session began.
8. Enter one or more reviewed lines under **Done**, **Next**, or **Blockers**.
9. Click **Add to worklog**.
10. Open **Daily Review** and confirm the facts, focused duration, and `WS-…` identity appear in the day record. Confirm the seven-day Task card includes the aggregated focused duration.

### Automatic graph layout

1. Open **Workspace**.
2. Select **Graph**.
3. Confirm Objectives appear before their aligned Tasks, prerequisites before dependent Tasks, and linked Notes after their targets.
4. Select a Task and confirm unrelated nodes and edges are muted.
5. Select the same Task again and confirm the full graph returns.
6. Open an Objective node and confirm the existing Objective Hub navigation still works.

## Explicit nonclaims

- This is not an agent runtime, Taskroom, orchestration engine, or Conduit client.
- It does not contact Conduit, Microsoft, Teams, Outlook, SharePoint, or any cloud relay.
- It does not execute scripts, create provider jobs, watch directories, or synchronize an external SSOT.
- It does not auto-start, auto-complete, or otherwise infer planning status from elapsed time or Worklog text.
- It does not change the frozen Work Stack → Conduit export contract or docking snapshot bytes.
- It does not yet provide reports beyond per-Task focused duration, nor pruning/archival for long-lived Work Session history.
