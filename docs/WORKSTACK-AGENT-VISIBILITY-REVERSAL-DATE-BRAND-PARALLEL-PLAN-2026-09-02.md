# Work Stack Agent Visibility, Reversal, Date, and Brand Parallel Plan

Status: implementation-ready draft

Date: 2026-09-02

Target branch: `codex/workstack-python-desktop-20260831`

Implementation baseline: `.worktrees/source-providers` at
`f6aca9723f2d5dbc1c1a67027ae4ea78a9d513ad`. The top-level `WORK-STACK`
worktree is not the installed desktop/Agent CLI baseline for this program.

## 1. Outcome

This program makes agent-originated Work Stack changes visible and reversible without
weakening the current local-first, revision-guarded, append-only boundaries. It also
removes OS-locale leakage from Work Stack date controls and makes every Windows and web
brand surface use the same large, rounded lime mark.

The completed user flow is:

1. an agent reads one explicitly selected Task through `workstack.cli.v1`;
2. the agent appends one idempotent checkpoint through the running Work Stack owner;
3. the open GUI refreshes from authoritative HTTP state and shows one small, content-safe
   notification describing the accepted change;
4. if the Task is still Open, the notification offers an explicit **Mark In Progress**
   action rather than silently mutating Task state;
5. the explicit status transition retains the existing revision-safe Undo behavior;
6. a checkpoint can be superseded by an append-only compensating record, never by
   deleting or rewriting its historical meaning;
7. every Work Stack date control renders the same English ISO-date experience regardless
   of Windows/WebView2 locale; and
8. taskbar, shortcuts, native window, startup surface, and web shell use one visually
   consistent lime brand mark.

## 2. Non-negotiable invariants

- `agent checkpoint` never changes Task status or Task revision implicitly.
- A Task transition requires an explicit user or agent command, current revision, target
  status, and idempotency key.
- A replayed checkpoint emits no duplicate Worklog entry and no duplicate GUI notice.
- SSE carries only allowlisted, content-safe metadata. Journal text, paths, prompts,
  credentials, and environment data never enter a notification event.
- `X-WorkStack-Client: agent-cli-v1` is a provenance hint for user-facing attribution,
  not authentication. The existing loopback session, Origin, and CSRF controls remain the
  security boundary.
- The GUI treats SSE as a hint and always refetches authoritative HTTP projections.
- Live notices cover changes accepted while the desktop owner is running. Recovering
  historical notices after a closed-GUI local write is outside this slice and would need
  a separate durable notification cursor.
- Revert of a checkpoint means append a compensating supersession record. The original
  checkpoint remains auditable.
- One checkpoint can have at most one effective supersession. Replaying the same
  supersession is idempotent; a different second supersession is rejected.
- Existing v3 authorities remain readable before activation of the new supersession
  projection. No startup, installer, or updater path auto-migrates user data.
- Work Stack product locale changes must not alter Outlook, Teams, or OneNote WebView
  language or `Accept-Language` behavior.
- The product logo uses a fixed product-brand accent, not a theme-dependent control
  accent. Light/dark themes may change surrounding surfaces but not the logo identity.
- Installer replacement preserves the selected SSOT directory, backup directory, port,
  connection profiles, and user data.

### Blocking defect B0 — workspace identity persistence

On 2026-09-02, the GUI exposed workspace UID
`014b10b4-53db-43c7-b744-dad41e19942b`, but `agent status` returned
`workspace_mismatch` both before and after an explicit workspace reconnect and desktop
restart. This is a release blocker for Agent/Skill mutations and a recurring UX defect,
not an operator error to be worked around repeatedly.

Forensic correction: **Save and activate after restart did persist correctly**. After the
restart, the active connection profile and canonical `workspace.json` both identify the
full workspace as `dafe29d2-7625-41d3-809d-49e67685ccdf`. The remaining block is a
different authority boundary: the preserved runtime sync manifest still identifies a
previous, sparse workspace baseline as
`014b10b4-53db-43c7-b744-dad41e19942b`. Content-safe backup evidence shows that the
authoritative SSOT directory was replaced wholesale before the installer ran; the
installer preserved both the SSOT bytes and runtime evidence and did not cause the
replacement. Because every authoritative file differs, changing only a profile UID
would be unsafe and could hide a real workspace replacement.

The actual UX defect is that Connection Center, Store synchronization, and Agent
readiness expose three individually valid facts without making their required order
clear. A profile test can identify the files on disk, while the Store correctly keeps
writes blocked until an explicit rebind establishes a new runtime baseline. Meanwhile
`agent status` currently checks only server/storage identity and can incorrectly report
`ready: true` even while `/api/v1/sync/status` blocks all mutations.

Before Wave 1 mutation dogfooding, Work Stack must:

- preserve the selected connection profile and its expected identity across clean
  restart without auto-adopting an unreviewed Store candidate;
- make the GUI projection, server-owner metadata, and Agent preflight report one UID;
- show which identity source disagrees when reconciliation is required;
- route Connection Center identity mismatches to the existing read-only Store
  synchronization/rebind review instead of offering profile-only repair;
- make `agent status` report not-ready whenever Store synchronization blocks writes;
- provide a deterministic explicit Store rebind that succeeds once and survives the next
  restart, then require a fresh profile test if profile metadata still disagrees;
- prove that a mismatch blocks writes without hiding readable recovery guidance; and
- add a restart/upgrade regression covering external full-store replacement, preserved
  manifest evidence, explicit rebind, owner startup, GUI load, and `agent status`.

## 3. Contract freeze before implementation

One contract owner lands executable RED fixtures before feature lanes begin. No lane may
invent a different shape locally.

### 3.1 Agent-originated change event

Freeze a content-safe `workstack.change.v1` event with exactly:

- monotonic process-local `event_id`;
- `kind`: initially `agent.checkpoint.committed` or
  `agent.checkpoint.superseded`;
- `workspace_uid`;
- `task_id`;
- checkpoint date;
- `done_count`, `next_count`, and `blocker_count` for a committed checkpoint;
- `first_for_task`, defined as the first physically accepted checkpoint for the Task and
  unaffected by later supersession;
- stable opaque checkpoint reference for View/Supersede actions;
- `origin: agent-cli-v1`;
- `replayed: false` only; replayed mutations never publish an event.

The agent transport sends one exact provenance header. The loopback server validates the
header as a closed enum and does not accept arbitrary actor text. This distinguishes the
display origin but grants no additional authority. Browser-originated mutations do not
masquerade as Agent CLI mutations.

The existing `/api/v1/events` cursor must emit every event after the requested cursor in
order. It must not collapse several store events into one generation frame and advance
past an undisplayed Agent change. Idle heartbeats remain content-free.

### 3.2 GUI notification behavior

- A valid change event invalidates `workspace`, selected `task`, and `review` queries.
- The notice text is derived from allowlisted event metadata, not server-supplied prose.
- An Open Task receives **Mark In Progress**; a non-Open Task receives **View**.
- Marking In Progress uses the current authoritative Task revision.
- A conflict refetches authoritative state and reports that no transition was applied.
- A successful transition exposes the already-established revision-safe Undo action.
- EventSource reconnect with `Last-Event-ID` does not show the same notice twice.

This slice does not claim a pre-existing Undo facility for every Task Drawer field.
Checkpoint supersession and the explicit status-transition Undo are the reversal surfaces
in scope; a composite Undo for arbitrary queued property edits would be a separate lane.

### 3.3 Checkpoint supersession

Freeze a stable checkpoint identity and a compensating record containing:

- supersession UID;
- workspace UID;
- target checkpoint UID;
- Task ID;
- superseding intent ID;
- canonical reason code plus a short user-reviewed explanation;
- actor/provenance;
- created timestamp; and
- an optional replacement checkpoint UID only when a separately committed replacement
  exists.

Do not add IDs to, rewrite, or backfill the released v3 `worklog.json` inventory. Use the
already-atomic `activity.json` ledger instead:

- a new idempotent checkpoint appends `worklog.recorded` in the same
  `save_many(WORKLOG, ACTIVITY)` transaction;
- its opaque `CP-...` identity is derived from workspace UID plus the original
  idempotency key, so a lost-response replay resolves to the same identity without
  exposing the key;
- `worklog.superseded` and `worklog.restored` append state transitions to
  `activity.json` only; and
- each recorded event binds the date, zero-based ordinal, and canonical entry digest.
  Projection verifies that locator before applying state and fails closed on mismatch.

Existing unique checkpoints may be linked from their stored idempotency response to one
unmatched worklog occurrence. Ambiguous or non-idempotent legacy entries remain visible
but expose `revertible: false`; Work Stack never guesses. The active Daily Review,
weekly roll-up, graph, and Agent context exclude superseded facts, while Activity/audit
shows the original plus every compensating event. Restore includes the same original fact
again without changing its historical bytes.

Freeze one idempotent state endpoint:

`POST /api/v1/review/entries/{checkpoint_id}/state`

Its exact body is `state`, `revision`, and a bounded `reason`. Replay lookup precedes the
current-state guard. Same key and same body returns the original response; same key with a
different body conflicts; two fresh keys at one revision permit exactly one commit. The
CLI mirrors this as an explicit `agent checkpoint-state` command and never infers a target
from checkpoint text.

### 3.4 Date control

Freeze a shared `IsoDateField` contract:

- external value is `null`/empty or canonical `YYYY-MM-DD`;
- displayed empty placeholder is `YYYY-MM-DD`;
- month and weekday labels are English and owned by Work Stack;
- parsing and leap-year behavior are timezone independent;
- text entry, clear, Today, pointer selection, Escape, Enter, and basic arrow-key
  navigation are covered;
- no native `input[type=date]` remains on product-owned Work Stack surfaces; and
- Microsoft source WebViews are untouched.

### 3.5 Brand mark

Freeze one geometry document containing the fixed logo colors, 256 px canvas, outer
inset, corner radius, and the three rounded bar rectangles. The product accent is
`#B8F24B`; the ink is the established dark brand ink. Both light and dark web themes use
the same logo colors.

The Windows ICO is a deterministic PNG-backed multi-frame ICO rather than a GDI `HICON`
serialization.
The generated asset has a versioned filename so Windows does not reuse the earlier yellow
icon cache entry.

The source color request is already lime; the current GDI `GetHicon`/`Icon.Save` path is
what serializes it as yellow. The implementation therefore replaces the encoding path,
not merely the color constant.

## 4. Parallel ownership lanes

Central composition files are reserved for the integrator. Workers return tested modules
and patch proposals; they do not independently edit `workstack/server.py`,
`workstack/service.py`, `workstack/agent_cli_contract.py`,
`frontend/src/app/App.tsx`, installer version metadata, or release manifests.

| Lane | Exclusive ownership | Deliverable | Depends on |
| --- | --- | --- | --- |
| C0 contract/oracle | contract fixtures, new focused oracle tests | frozen event, supersession, date, and brand contracts | baseline only |
| A1 transport provenance | `workstack/agent_transport.py` tests plus a small header helper if needed | exact Agent CLI provenance header; replay semantics unchanged | C0 event contract |
| A2 checkpoint event metadata | new strict event/model module and focused service tests; `service.py` patch proposal only | first-for-Task calculation, replay-stable metadata, no Task status/revision mutation | C0 event contract |
| A3 server publication | store/SSE tests and server adapter; thin `server.py`/`service.py` patches reserved for integrator | ordered cursor delivery and one accepted non-replay Agent change through existing `/api/v1/events` | A1, A2 |
| A4 frontend decoder | new `frontend/src/app/workspaceChangeEvents.ts` and tests | strict event decoder, cursor dedupe, authoritative invalidation signal | C0 event contract |
| A5 notification actions | new notice-model module/component and tests | content-safe notice; View/Mark In Progress decision | A4 |
| B1 supersession domain | new domain/projector module and service tests; `service.py` patch proposal only | activity-ledger IDs, append-only state fold, locator verification, legacy ambiguity refusal | C0 supersession contract |
| B2 supersession API | route adapter and API tests; thin route-table patch reserved for integrator | preview/commit/readback with CSRF, revision, idempotency | B1 |
| B3 CLI and Skill | CLI command modules, contract tests, canonical Skill tree | explicit supersede command; updated commands/journal policy | B1, B2 contract |
| B4 supersession GUI | API client, focused Daily Review/Activity components and tests | Review/Supersede flow; original remains in audit view | B1, B2 contract |
| D1 date component | new shared component, date model, CSS, unit/a11y tests | locale-independent ISO date field | C0 date contract |
| D2 date adoption | the five current consumer files and their focused tests | replace all seven native date inputs in Task, Quick Task, Review, and Capture | D1 |
| E1 brand generator | geometry document, deterministic generator, asset tests | SVG plus PNG-backed 16–256 px ICO outputs with exact colors and geometry | C0 brand contract |
| E2 native/installer brand | native fallback and installer tests; thin installer patch reserved for integrator | versioned icon deployed to shortcut and window | E1 |
| E3 web/startup brand | web mark/startup surface CSS or asset bindings and tests | same geometry/colors across web and splash | E1 |
| I integration | only central composition files and conflict resolution | wire accepted modules; no new behavior invented during merge | accepted lanes |
| R release verification | build scripts and external receipts only | full regression, installer, shortcut, taskbar/date/live CLI smoke | integrated SHA |

Concrete date consumers owned by D2 are:

- `frontend/src/features/inbox/SourceCaptureDialog.tsx`;
- `frontend/src/features/inbox/CaptureDrawer.tsx`;
- `frontend/src/features/tasks/QuickTaskDialog.tsx` for planned and due dates;
- `frontend/src/features/tasks/TaskOverviewEditor.tsx` for planned and due dates; and
- `frontend/src/features/review/DailyReviewPage.tsx`.

Concrete brand surfaces are the web `BrandMark`, `NativeStartupSplash`, `STARTUP_HTML`,
`startup_recovery_host.py`, native form/taskbar icon, Desktop/Start/Maintenance shortcuts,
and the installed asset. E1 generates `WorkStack-Mark-Lime-v2.ico` with PNG frames at
16, 20, 24, 32, 40, 48, 64, 128, and 256 px. E2 preserves the existing App User Model ID,
rewrites shortcut `IconLocation`, and sends a shell change notification without deleting
pins or purging Explorer caches.

## 5. Implementation waves

### Wave 0 — clean baseline and executable RED contracts

1. Resolve Blocking defect B0 and prove identity agreement across reconnect plus restart.
2. Require a clean candidate SHA and record the current full Python/frontend/quality
   baseline.
3. Land only contract fixtures, schemas/types, and failing focused tests.
4. Run an overlap scanner proving exclusive file ownership.
5. C0 publishes a machine-readable contract digest consumed by every lane.

No production behavior starts before Wave 0 is green as a contract package and red only
for the intended missing implementations.

### Wave 1 — maximum independent construction

Run A1, A2, A4, B1, D1, E1, and E3 concurrently. These lanes share no production file;
where A2/B1 require `service.py`, they return integrator-owned patch proposals against
their new modules rather than touching that central file.
Each lane must return:

- clean commit SHA;
- exact changed-file roster;
- focused test command and output;
- contract digest used;
- `git diff --check` result; and
- explicit statement that protected central files were not changed.

### Wave 2 — adapters and consumers

After their upstream lane gates pass, run A3, A5, B2, B3, B4, D2, and E2 concurrently.
Thin edits to protected composition files are supplied as patch proposals to I rather than
committed by lane workers.

### Wave 3 — deterministic composition

I applies patches in this order:

1. event feed and server provenance;
2. frontend event decoder and notice action;
3. supersession domain/API/CLI/Skill/UI;
4. date component consumers; and
5. brand assets, native shell, and installer.

After every group, I runs its focused gate. A failure reverts only that group and returns
the evidence to its owner; I does not patch around failed lane assumptions.

### Wave 4 — cross-feature behavior

- First Agent checkpoint on an Open Task produces one notice but leaves Task revision and
  status unchanged.
- Mark In Progress produces one revisioned transition and offers Undo.
- Undo restores the prior status only if the expected revision remains current.
- Identical checkpoint replay changes no authoritative byte and emits no new notice.
- Superseding a checkpoint removes it from active Daily Review totals but leaves both
  records in Activity/audit.
- Date values round-trip identically in every consumer under Korean Windows locale.
- All brand surfaces use the same geometry and lime pixel samples.

### Wave 5 — rebuild, install, and live dogfood

1. Run the full Python, frontend, structural-quality, export, Windows installer, update,
   and package gates selected by the existing release policy.
2. Build the React application and self-contained Windows installer from one clean SHA.
3. Verify installer sidecar and update-manifest hashes.
4. Upgrade the existing installation while explicitly preserving its current SSOT and
   backup paths.
5. Verify the OneDrive Desktop and Start-menu shortcuts' target, arguments, working
   directory, and versioned icon path.
6. Launch by the real Desktop shortcut and prove health, Agent status/context/checkpoint,
   one GUI notice, one replay with no notice, Mark In Progress, Undo, and supersession.
7. Capture taskbar icon at 100%, 125%, and 150% scaling and inspect lime color, rounded
   geometry, visual footprint, and absence of the old yellow asset.
8. Open every Work Stack date control on Korean Windows and prove English labels and ISO
   display without changing Microsoft source-app language.

## 6. Oracle gates

| Gate | Pass condition |
| --- | --- |
| G0 identity/baseline | reconnect survives restart; GUI/server/Agent UID agree; `agent status` ready; clean SHA; existing selected regression green |
| G1 contract | one canonical contract digest; protected-file ownership map has no overlap |
| G2 event transport | first commit emits exactly one allowlisted event; replay/browser mutation emits none |
| G3 notice/status | authoritative refetch occurs; exact notice/action selected; revision conflict is fail-closed; Undo is revision-safe |
| G4 supersession | original retained; one compensating record; active projection excludes it; audit includes both; replay byte-identical |
| G5 date | no product native date inputs; ISO/leap-year/keyboard/a11y matrix passes; Microsoft WebViews untouched |
| G6 brand | every ICO frame is PNG-backed; deterministic digest; exact lime/ink and no `#FFFF00`; transparent corners; >=94% occupied bounding box; versioned cache path; native/web parity |
| G7 integration | full backend/frontend/quality regression green; no undocumented contract drift |
| G8 installed dogfood | real shortcut launch, live Agent notice/replay/status/Undo/supersede, English dates, correct taskbar icon |

Every gate writes a canonical JSON receipt outside the candidate tree. Receipts bind the
candidate SHA, contract digest, command list, test counts, artifact hashes, and pass/fail
checks without recording Task or checkpoint text.

## 7. Review and merge discipline

- Workers never push, release, install, or modify user SSOT data.
- Workers do not repair another lane's failed contract. The owner receives the exact
  failing fixture and returns a new commit.
- New modules should keep public functions small and typed; structural quality cannot
  regress merely to preserve lane independence.
- C0 contract changes after Wave 1 require invalidating and rerunning every dependent lane.
- I independently reviews all untrusted worker output and reruns tests; worker reports are
  evidence hints, not authority.
- Release artifacts are created only from the final clean integrated SHA.

## 8. Estimated effort and critical path

Estimated implementation effort is 9–14 engineer-days, dominated by checkpoint
supersession compatibility and installed Windows verification. With seven or more bounded
workers plus one trusted integrator, expected wall-clock time is 3–5 focused days:

- Wave 0: 0.5–1 day;
- Waves 1–2: 1–2 days in parallel;
- Waves 3–4: 1–1.5 days; and
- Wave 5: 0.5–1 day.

The critical path is C0 supersession contract → B1 domain → B2 API → I integration → G4/G8.
Event notifications, dates, and branding do not wait for supersession implementation and
can reach an independently testable green state earlier.

## 9. Rollout slices

If supersession requires more compatibility work than forecast, ship neither a destructive
delete nor a misleading Undo. The safe rollout order is:

1. event notification plus explicit Mark In Progress/Task Undo;
2. locale-independent date controls and unified brand assets;
3. checkpoint supersession only after G4 is fully green.

This preserves immediate user value without weakening append-only history.
