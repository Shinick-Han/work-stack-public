# Prototype release checklist

## Release label

Choose exactly one:

- `manual sanitized capture prototype`: the manual fixture/import flow passed; no claim
  is made about Outlook lookup, upstream prompt-injection isolation, or upstream log
  retention.
- `Microsoft 365 read-only dogfood (<providers>)`: the named Outlook and/or Teams read
  gates passed, but every unverified reply control is disabled.
- `single-provider bidirectional dogfood (<provider>)`: the named provider passed read
  and reply gates; the other provider is visibly disabled and this is not the full-plan
  completion label.
- `Outlook + Teams bidirectional dogfood`: both providers passed read and reply gates
  plus every common safety gate below.

Current selection: **manual sanitized capture prototype**. The OOB implementation and
synthetic tests are complete, but they are not Gate 0 evidence. No Outlook or Teams
read/reply capability is currently verified or approved for dogfood.

## Current provider gate matrix

| Provider | Read status | Read build flag | Reply status | Reply build flag | Default UI |
| --- | --- | --- | --- | --- | --- |
| Outlook | **Unverified** | `VITE_WORKSTACK_OUTLOOK_READ_VERIFIED` | **Unverified** | `VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED` | Read and reply disabled |
| Teams | **Unverified** | `VITE_WORKSTACK_TEAMS_READ_VERIFIED` | **Unverified** | `VITE_WORKSTACK_TEAMS_REPLY_VERIFIED` | Read and reply disabled |

All four flags default false. Only a trimmed, case-insensitive build-time value exactly
equal to `true` enables a flag; unset, empty, `1`, `yes`, and all other values are false.
Effective reply enablement requires both the provider's read flag and reply flag. These
are build gates, not live connection or health signals, and changing them requires a new
frontend build. Generic manual Capture import remains available with every Microsoft
flag disabled.

To enable a lane after real Gate 0:

1. Complete and retain the non-sensitive evidence for that provider's read gate below.
2. Set only its read flag to `true`, rebuild, and label the release for the passing
   provider; keep every unverified flag unset or false.
3. Only after that provider's read gate and reply gate both pass, set its reply flag to
   `true` and rebuild. A reply flag by itself must never expose reply controls.
4. Re-run this checklist, the source/runtime audit, tests, and production build for the
   exact flagged artifact. Do not transfer evidence between Outlook and Teams.

## Data and recovery

- All backend/frontend tests and the production frontend build pass.
- The exact Windows setup artifact installs and first-launches on its declared Python 3.12
  target; upgrade writes a verified backup and uninstall preserves data by default.
- Backup verification checks the exact member allowlist, sizes, digests, workspace identity,
  and complete store semantics before restore writes any destination.
- The server holds the data-directory lease for its lifetime.
- A direct-write CLI mutation is rejected while the server lease is held.
- Interrupted multi-file link/convert replays the journal without duplicate task,
  context, or activity records.
- Malformed JSON and malformed journal input fail closed and preserve evidence.
- Exact duplicate, idempotency replay, idempotency conflict, stale packet, and equal-time
  source conflict tests all pass.
- Generic Task-from-Capture commits Task, Capture linkage, Activity, and idempotency in
  one recovery operation.
- Approved ReplyCommand and terminal ReplyReceipt updates recover without duplicate
  records or duplicate Task activity.

## Capture boundary

- Positive OOB and manual fixtures validate.
- Recursive forbidden-key and every value-negative case are rejected.
- Runtime JSON, logs, exports, and browser storage contain no raw canary.
- Microsoft deep links use allowed HTTPS hosts and `noopener noreferrer`.
- Manual captures display manual provenance and never fabricate model/tool evidence.
- OobRequest, ReplyCommand, and ReplyReceipt fixtures parse and reject unknown/raw/token/
  recipient-list fields.

## Browser boundary

- All state-changing legacy and v1 browser routes reject missing or wrong Host, Origin,
  JSON content type, and CSRF nonce.
- With a valid browser boundary, every recognized unversioned writer returns HTTP 410 and
  leaves planning state unchanged; all product mutations use versioned `/api/v1` routes.
- Agent ingest rejects missing/wrong bearer token and bodies larger than 64 KiB.
- Generic Task-from-Capture, reply approval, and receipt import reject missing/wrong
  Origin, CSRF, JSON content type, and Idempotency-Key.
- No CORS response header is present.
- The release artifact records all four Microsoft build-flag values; every unverified
  lane is disabled, and reply remains disabled unless the same provider's read lane is
  enabled.
- The documented threat model excludes malicious processes under the same OS account
  and other local users.

## Product path

- Thirty fixture planning tasks and objectives appear in Graph, Board, Treemap, and Table.
- Selecting the same Task twice in Graph, Board, or Treemap clears the shared drawer and
  selection emphasis without changing planning state.
- Graph notes, Task notes, and subtasks are available from explicit production UI actions.
- Objective, Graph-note, Task-note, and subtask creation retain one Idempotency-Key
  through transport loss and an explicit unchanged retry.
- Board status action updates all three projections and rolls back visibly on failure.
- Quick Add uses idempotent `POST /api/v1/tasks`, opens the committed Task, and preserves
  the current surface where the navigation contract permits it.
- The command palette opens Tasks, changes surfaces, starts Quick Add, and starts manual
  Capture import; numeric and J/K shortcuts are suppressed inside editable controls.
- Unified search returns only bounded privacy-minimized Task, Objective, note, sanitized
  Capture, and minimal activity projections; Table reuses shared filters, selection,
  drawer, and revision-guarded status mutation.
- Saved filters and Quick Add drafts use bounded strict local schemas; malformed values
  fail closed, successful creation clears its draft, and no Capture/reply/docking data is included.
- A committed browser mutation refreshes another tab through a content-free hint; status Undo
  appends a new revision-guarded fact rather than deleting history.
- Focus separates actionable candidates from those waiting on unfinished, dropped, or missing
  dependencies; blocked rows explain the prerequisite and expose no Start/Done mutation.
- A known Focus blocker opens its exact Task Drawer through explicit navigation while preserving
  the Focus surface and without changing either Task.
- Graph/Treemap/Task Drawer load as separate chunks, the 1,000-Task graph-model gate passes,
  and the content-free readiness endpoint returns a request correlation ID.
- Daily Review records idempotent check-in and Task-scoped Done/Next/Blocker evidence,
  then derives a deterministic seven-day roll-up without changing Task planning status.
- Objective Hub reads the existing planning SSOT, adds restart-replay-safe Key Results,
  rejects stale Objective/KR writes by revision, and records append-only change activity.
- Objective Hub separates linked Tasks into actionable, blocked, done, and dropped counts and
  explains unfinished dependencies without changing Objective or Task state.
- Board and Table calculate dependency readiness from the complete Workspace, show unfinished
  prerequisite IDs, and open a resolvable blocker without disabling explicit status controls.
- Workspace can filter active Tasks to `Ready to act` or `Blocked work`; the choice is canonical in
  the URL, persists in bounded local saved views, and defaults older saved-view v1 records to all.
- Board, Focus, Objective Hub, and readiness filters reuse one per-snapshot dependency index; the
  10,000-Task filter gate passes without a persisted reverse store or SSOT change.
- An active Workspace filter reports `matched of total` from the exact shared filter projection;
  clearing all filters returns the concise complete Task count.
- The Workspace summary reports all active blocked Tasks as its attention headline and retains the
  active P0 count as supporting context from the same planning snapshot.
- Board cards and the Table Steps column expose accessible completed/total subtask progress without
  changing Task or subtask planning status.
- Board and Table Objective controls, Graph Objective nodes, and the Treemap Objective navigator open
  the same canonical Objective Hub route without selecting or mutating a Task.
- Board and Table use Focus's local civil-calendar rules for accessible overdue/today/due-soon
  labels without changing the canonical due date or treating completed work as overdue.
- Every active Workspace filter has an accessible single-remove control that preserves the other
  URL fields; the all-clear action remains available and Table loads as a separate view chunk.
- Due timing filters use the same local civil-calendar projection as visible labels, round-trip in
  URL/local saved views, default legacy views safely, and exclude done/dropped work.
- Enter/Space on nested Board/Table controls never activates the parent Task selection; the parent
  keyboard shortcut remains available only when the card or row itself owns focus.
- Graph Task and Objective nodes expose one focus target and activate their existing navigation with
  Enter/Space; informational Note nodes do not expose a false action.
- A Task snapshot export requires explicit disclosure confirmation, emits the exact
  canonical bytes for one committed revision, and does not mutate Work Stack or contact
  Conduit.
- Inbox can link a capture, convert one stable action ID into a task, and dismiss a
  capture without duplication.
- Inbox can copy a bounded Outlook/Teams request, import an agent result without manual
  JSON editing, and create a Task directly from a selected Capture.
- Task detail can prepare a reply only from linked Outlook/Teams context, shows the fixed
  source target and body before approval, and exposes no arbitrary recipient control.
- Task parent and dependency updates reject direct or transitive cycles without advancing the
  Task revision or persisting a partial relationship.
- Task detail excludes known cyclic parent/dependency candidates and edits dependencies through
  existing Task choices and removable relationship chips rather than free-form IDs.
- Approved commands can be copied and strict receipts imported; `unknown` never triggers
  automatic resend.
- UI copy says that the agent handoff is manual and never claims Connected, Healthy,
  background sync, or provider polling.
- Loading, empty, error, and keyboard-focus states are visible and usable.
- Local evidence is not substituted for remote evidence: M52 splits axe surfaces and adds bounded
  Firefox/WebKit compatibility, forced-colors, and 200%-reflow-equivalent checks. Strict Windows
  CI run `33304485683` passed every independent install, unit, build, audit, and browser step.

## Per-provider read gate

- The connected agent exposes the provider's exact search/read surface.
- Outlook records stable message/thread/version references and the mailbox-move
  limitation; Teams records stable chat/channel/thread/message paths.
- Tool execution was limited to the named provider's read/search tools and local packet
  output.
- Adapter/model/prompt/redaction-policy versions and the redacted tool-trace digest are
  present.
- A source canary did not enter the Capture Packet or Work Stack data/log/export.
- The Capture has a stable opaque locator and allowlisted Microsoft deep link and remains
  linked to its Task after restart.
- Upstream runtime retention was inspected separately; Work Stack does not claim to
  control it.

## Per-provider reply gate

- The read gate passed first and the connector exposes a canonical reply action:
  Outlook plain-text thread reply or Teams chat/channel thread reply.
- The approved ReplyCommand snapshots the exact target locator from the linked Capture;
  the browser cannot supply or replace the target or recipient.
- The agent recomputes body and target digests before sending and performs exactly one
  explicitly approved tool call during the validation run.
- No write occurs while composing, previewing, copying a read request, or importing a
  Capture.
- A matching receipt reaches one terminal local state and appears once in Task activity.
- Mismatched reply ID, provider, body digest, or target digest is rejected.
- A simulated or real unconfirmed result becomes `unknown` and is not automatically
  retried or reconciled.
- The release evidence states that local idempotency does not prove externally
  exactly-once Microsoft delivery.
