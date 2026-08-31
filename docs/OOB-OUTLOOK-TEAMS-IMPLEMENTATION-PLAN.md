# Outlook + Teams OOB implementation plan

Status: fixture implementation complete behind disabled build-time gates; Gate 0 remains unverified
Date: 2026-08-29
Target: enable only provider capabilities that later pass the real Gate 0 matrix; the current release remains manual Capture only

## 1. Decision summary

Work Stack will reuse the user's already authenticated Outlook Email and Microsoft Teams
OOB connector sessions. It will not automate desktop applications, store Microsoft
credentials, or pretend that the current Python server can call an agent-only connector
as an application API.

The first slice deliberately uses a user-mediated, one-shot bridge:

```text
Read path
Work Stack creates an OobRequest
  -> user copies it to the connected agent
  -> agent performs one allowlisted Outlook/Teams OOB read
  -> agent returns a sanitized Capture Packet v1
  -> user imports it in Work Stack
  -> existing Capture -> Task link/conversion persists the source as Task context

Write path
Task + linked Capture
  -> user writes and previews a reply in Work Stack
  -> explicit approval creates a ReplyCommand
  -> user copies it to the connected agent
  -> agent performs one reply in the original Microsoft thread
  -> agent returns a minimal ReplyReceipt
  -> user imports the receipt and Work Stack records Task activity
```

This handoff is an honest product boundary, not an implementation detail hidden behind
fake health or queue UI. The first slice remains user-mediated even if Gate 0 discovers
a supported application-callable agent path. Such a path is recorded as a maturation
candidate; it is not implemented under this plan.

Work Stack remains the system of record for PlanningTasks, sanitized Captures, approved
reply intent, minimal receipts, and Task activity. Outlook and Teams remain the system
of record for messages, threads, recipients, and delivery state.

The OOB request, Capture import, Task-from-Capture, approval, command, receipt, and audit
implementation is complete against synthetic fixtures. That implementation is not
evidence of connector capability or approval. All Microsoft controls are build-disabled
until provider-specific Gate 0 evidence exists; generic manual Capture import remains
available.

## 2. Why the previous plan was cut

The earlier 36-48 hour plan assumed a durable agent job platform before proving that an
application-callable agent worker existed. The adversarial review found that this added
infrastructure without solving the only hard boundary: OOB tools are currently callable
inside an authenticated agent session, not directly from the Work Stack server.

The first slice therefore does **not** add:

- `evidence.json`, `oob_jobs.json`, or `integrations.json`;
- a generic `TaskEvidenceLink` model or evidence roles;
- a general `ExternalAction` platform or nine-state action machine;
- a background runner, durable queue, leasing, heartbeat, attempt counts, or provider
  health dashboard;
- automatic reconciliation after an ambiguous send;
- `workstack/evidence.py`, `workstack/oob.py`,
  `workstack/external_actions.py`, or `scripts/run_oob_bridge.py`;
- runner heartbeat/lease/complete/fail APIs.

Those components remain maturation options only after real dogfood shows that the
manual agent handoff is the main friction and a supported callable worker is available.

## 3. Known environment and repository facts

- Capture Packet v1 already accepts Outlook and Teams providers and validates source
  locator, source version/fingerprint, Microsoft deep link, sanitized provenance,
  idempotent ingest, and stale revisions.
- Existing Work Stack behavior already supports Capture review, linking a Capture to an
  existing Task, converting a Capture action item into a Task, and displaying linked
  Capture context in the Task drawer.
- The existing JSON single-writer store and recovery journal already make Capture/Task/
  Activity mutations recoverable. This slice keeps that storage boundary.
- Official Outlook Email and Teams OOB connector packages exist locally and advertise
  interactive and write capabilities. Their exact tool surface and stable identifiers
  still require a real capability spike against the user's connected tenant.
- The connectors expose an agent surface, not a local executable or HTTP API known to
  the Work Stack server. A queue would have no consumer unless Gate 0 discovers a
  supported callable path.
- Outlook for Windows and Microsoft Teams AppX packages are already present, but neither
  is required. Desktop UI automation is outside the design.

## 4. Scope

### First dogfood release includes

- Generate a plain-language, machine-identifiable OobRequest for Outlook mail search or
  Teams chat/channel-message search.
- Copy the request to an already connected agent without exposing credentials.
- Import the agent's sanitized Capture Packet through a guided UI; the user never edits
  JSON manually.
- Create a Task from the selected Capture even when no extracted action item exists.
- Link the Capture to an existing Task as its external context.
- Display provider, source title, retrieval time, sanitized summary/context, and the
  Microsoft deep link in the existing Task context UI.
- Compose a reply only for a linked Outlook or Teams source.
- Require target/body preview and explicit approval before a ReplyCommand exists.
- Import a strict ReplyReceipt and record `sent`, `failed`, or `unknown` in Task activity.
- Preserve all existing manual Capture import and normal Work Stack behavior when OOB is
  unavailable.

### Explicitly excluded

- A general Outlook inbox or Teams client clone.
- New mail to arbitrary recipients, reply-all, forwarding, attachments, arbitrary
  channel broadcast, edit/delete/move, chat membership, or Planner writes.
- Automatic or unattended sending.
- Durable job queues, worker processes, connector health polling, leases, heartbeats,
  retries, or automated ambiguous-send reconciliation.
- Generic evidence roles, frozen citations, many-to-many evidence synchronization, or a
  separate evidence store.
- OAuth/token persistence in Work Stack.
- Desktop automation, Activepieces, Conduit integration, SQLite migration, or tenant-wide
  subscriptions in the same change.

## 5. Minimal architecture and contracts

### 5.1 One-shot agent handoff

The bridge is schema-driven but user-mediated:

```text
Work Stack UI
  -> Copy OobRequest
  -> authenticated Outlook/Teams OOB agent
  -> Copy sanitized Capture Packet or ReplyReceipt
  -> Work Stack Import UI
  -> existing loopback API and store transaction
```

The UI must label this handoff clearly. It must not display `Connected`, `Healthy`,
`Syncing`, or similar automation claims that are not backed by an application-callable
transport.

`OobRequest v1` is a short-lived envelope and is not stored in a new server-side queue:

```text
request_id
schema_version
provider: microsoft-outlook | microsoft-teams
operation: search_and_capture
query
result_limit
requested_at
```

The accompanying agent instruction permits only search/read, treats all source content
as untrusted data, and requires one or more strict Capture Packet v1 results. The request
contains no OAuth token, raw source content, or external write authority.

### 5.2 Capture as Task creation basis

The existing Capture record is the first-release evidence object. Its existing
`linked_task_ids` and `converted_task_ids`, source revision/fingerprint, sanitized
content, provenance, and deep link are sufficient to show why a Task exists.

One small backend addition is required:

```text
POST /api/v1/captures/{capture_id}/task
```

It accepts the normal Task creation fields, creates the Task, adds the Task ID to the
Capture's `converted_task_ids`, records Activity, and saves Task + Capture + Activity in
one existing recovery-journal transaction. The current action-specific conversion route
remains unchanged and is reused when an extracted action item already exists.

Linking to an existing Task continues to use:

```text
POST /api/v1/captures/{capture_id}/link
```

No `TaskEvidenceLink`, role taxonomy, migration, or duplicate compatibility projection
is introduced in this slice.

### 5.3 Approval-gated reply

The reply model is intentionally source-bound and small. An unapproved editor draft
lives only in UI state. Approval creates the durable command:

```text
ReplyCommand
  id
  task_id
  capture_id
  capture_revision
  provider: microsoft-outlook | microsoft-teams
  capability: outlook.reply | teams.reply
  target:
    resource_type
    connection_ref
    container_ref
    object_ref
    version_ref
  body
  body_digest
  target_digest
  state: approved | sent | failed | unknown
  approved_at
  receipt
  created_at / updated_at
```

At approval time, the server copies the allowlisted locator fields from the linked
Capture into the immutable `target` snapshot and computes `target_digest` from their
canonical representation. The snapshot contains only `resource_type`, `connection_ref`,
`container_ref`, `object_ref`, and `version_ref`; it contains no recipient list. The UI
does not accept an arbitrary recipient or target. Approval binds the Capture revision,
target digest, and body digest. Before sending, the agent recomputes both digests. The
receipt importer accepts a result only when `reply_id`, provider, body digest, and target
digest all match the stored approved command.

`ReplyReceipt` is minimal:

```text
schema_version
reply_id
provider
outcome: sent | failed | unknown
remote_message_ref?   # opaque, optional
web_url?              # allowlisted Microsoft URL, optional
occurred_at
body_digest
target_digest
error_code?           # bounded symbolic code, no connector dump
```

The receipt never contains OAuth material, raw source bodies, HTML, attachments,
recipient lists, or arbitrary connector output. `unknown` is terminal in the first
release: Work Stack warns the user and never resends automatically.

The only new store is `replies.json`. It holds approved user-authored reply text, its
digest, state, and minimal receipt. Microsoft source content remains in sanitized
`captures.json` only.

## 6. Minimal API and file changes

### Reused browser APIs

- `POST /api/v1/captures`
- `POST /api/v1/captures/{capture_id}/link`
- `POST /api/v1/captures/{capture_id}/actions/{action_id}/task`
- existing Task detail and Activity reads

### New browser APIs

- `POST /api/v1/captures/{capture_id}/task`
- `POST /api/v1/replies` — validates an explicit approval and creates an `approved`
  ReplyCommand
- `POST /api/v1/replies/{reply_id}/receipt` — idempotently applies a strict receipt

All mutations reuse the current loopback Host, same-origin Origin, CSRF, JSON framing,
body-size, recovery-journal, and `Idempotency-Key` protections. There is no runner bearer
or runner API in this slice because the result returns through the user's browser.

### Backend

- `workstack/capture.py`
  - Preserve Capture Packet v1 and existing raw-content rejection.
  - Confirm Outlook and Teams OOB fixtures use exact provider/tool claims.
- `workstack/store.py`
  - Add only `replies.json`.
  - Reuse the current journal; add no runner token, lease, or integration store.
- `workstack/service.py`
  - Add generic atomic Task-from-Capture creation.
  - Add approved ReplyCommand creation and idempotent receipt application.
  - Project reply state into Task activity/detail without changing Capture linkage.
- `workstack/server.py`
  - Add only the three browser routes listed above.
- `SKILL.md`
  - Define OobRequest execution, strict result envelopes, untrusted-source handling,
    approval verification, and the prohibition on retrying `unknown` writes.

No new backend worker or evidence/action framework module is planned. A tiny reply
validator may be extracted only if keeping it in the existing domain modules makes the
code harder to test; it must not grow into a generic ExternalAction abstraction.

### Frontend

- `frontend/src/domain/types.ts` and `schemas.ts`
  - Add only OobRequest, ReplyCommand, and ReplyReceipt DTOs.
- `frontend/src/api/client.ts`
  - Add generic Task-from-Capture, approved reply, and receipt-import calls.
- `frontend/src/features/inbox/InboxPage.tsx`
  - Add explicit `Copy Microsoft 365 request` and `Import agent result` actions; retain
    `Import packet` fallback.
- `frontend/src/features/inbox/CaptureDrawer.tsx`
  - Add `Create task from this source` using the new atomic endpoint.
- `frontend/src/features/tasks/TaskDrawer.tsx`
  - Add source-bound `Prepare Outlook/Teams reply`, preview/approval, approved-command
    copy, receipt import, and status display.
- Add at most two focused components:
  - `frontend/src/features/integrations/MicrosoftOobDialog.tsx`
  - `frontend/src/features/tasks/ReplyComposer.tsx`

The Microsoft dialog generates/copies a request and imports returned Capture Packets.
The reply composer creates the approved command, copies it, and imports a receipt. There
is no polling, integration-health model, EvidenceList, or generic action timeline.

### Contracts, security, and tests

- Extend `contracts/api-v1.md` with OobRequest, generic Task-from-Capture,
  ReplyCommand, and ReplyReceipt contracts. Do not create OOB job or ExternalAction
  contract documents.
- Add Outlook and Teams OOB Capture fixtures and sent/failed/unknown reply receipt
  fixtures.
- Update `SECURITY.md` with the agent handoff trust boundary, approved body digest,
  terminal `unknown` rule, and explicit statement that approved user-authored reply text
  is stored while raw Microsoft content and tokens are not.
- Update `README.md` and `docs/RELEASE-CHECKLIST.md` with the user-mediated flow and
  separate Outlook-read, Teams-read, Outlook-write, and Teams-write gates.
- Extend the current backend/API/audit tests and focused Inbox/Task drawer/client tests.
  Do not build a fake durable runner test suite.

## 7. Execution plan

The contract freeze, fixture-backed Wave 1 implementation, and automated hardening are
complete. Wave 2 and Wave 3 real-provider validation remain pending and their UI lanes
stay disabled by the Gate 0 build flags below.

### Gate 0 - capability spike (0-3 hours)

Current status: Gate 0 has not been completed for either provider. No row below is an
approval or verification claim.

| Provider | Read surface and stable Capture locator | Read flag | Reply surface, stable target, and approval behavior | Reply flag | Current UI |
| --- | --- | --- | --- | --- | --- |
| Outlook | **Unverified** | `VITE_WORKSTACK_OUTLOOK_READ_VERIFIED` | **Unverified** | `VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED` | Read and reply disabled |
| Teams | **Unverified** | `VITE_WORKSTACK_TEAMS_READ_VERIFIED` | **Unverified** | `VITE_WORKSTACK_TEAMS_REPLY_VERIFIED` | Read and reply disabled |

All four flags default to false. A flag is true only when its trimmed,
case-insensitive build-time string is exactly `true`; missing, empty, `1`, `yes`, and
every other value are false. Effective reply enablement is
`provider read flag AND provider reply flag`. The flags do not represent runtime health
or a live connection.

1. Use the user's existing personal-tenant OOB connection in an agent session.
2. With one non-sensitive Outlook message and one non-sensitive Teams message, verify:
   - exact search/read/reply tool surface;
   - Outlook message/thread/version references and Microsoft deep link;
   - Teams chat/channel/thread/message references and deep link;
   - a stable, allowlisted reply target for each provider;
   - whether a later agent turn can reuse the authenticated connection;
   - the connector's write-approval behavior and upstream logging/retention.
3. Validate sanitized Capture Packet generation for both providers.
4. Do not send during this gate.

Gate 0 produces a provider matrix with separate results for `read surface`, `stable
Capture locator`, `reply surface`, `stable reply target`, `approval behavior`, and
`deep link`. A provider enters the read lane only when its read and Capture checks pass;
it enters the reply lane only when all reply-target and approval checks also pass. If one
provider passes, enable only that provider without fabricating support for the other. If
neither provider passes read, leave every OOB lane disabled and preserve the existing
manual Capture path.

After a real Gate 0 run, retain the non-sensitive provider evidence, set only the flags
for capabilities whose complete matrix columns passed, and rebuild the frontend. Enable
read first. Enable reply only after the same provider's read gate and reply checks both
pass. Leave a failed or untested provider flag unset or false; never infer one provider's
result from the other.

The 24-32 hour estimate starts when the connected agent session and the user's Gate 0
and planned write-validation windows are available. Waiting for tenant reauthorization
or external connector approval is reported as an external blocker rather than hidden in
implementation time; the 36-hour stop-loss still forces a partial or read-only decision.

### Contract freeze - four envelopes and UX (3-6 hours)

- Freeze OobRequest, existing Capture Packet v1 usage, ReplyCommand, and ReplyReceipt.
- Freeze the exact Copy request, Import result, preview, approve, and Import receipt UX.
- Generate provider fixtures from Gate 0's real identifier shapes without storing raw
  message content.

Gate: backend, frontend, safety, and agent-instruction lanes can implement the same
envelopes without adding a queue or provider-health abstraction.

### Wave 1 - minimal UI/backend in parallel (6-14 hours)

- Lane A: generic Task-from-Capture endpoint, `replies.json`, reply/receipt service and
  API methods.
- Lane B: Microsoft OOB request/capture UI and source-bound reply approval/receipt UI
  against fixtures.
- Lane C: OobRequest/Reply schemas, agent instructions, fixtures, security and audit
  tests.

Gate: the full request-copy/result-import flow works with fixtures; no queue, polling,
token, raw body, recipient list, or unapproved send path exists.

### Wave 2 - real read path (14-22 hours)

- Run real Outlook and Teams search/read through the connected agent.
- Import sanitized Capture Packets without manual JSON editing.
- Create a Task from each source and link a source to an existing Task.
- Confirm the source basis and Microsoft deep link persist after restart.

Gate: Outlook and Teams each complete
`request -> OOB read -> Capture import -> Task source context` in the real UI.

This read-only slice is independently releasable. If reply capability is delayed, it
must not block dogfooding of search/read and Task creation.

### Wave 3 - approval-gated replies (22-28 hours)

- Compose and preview a reply from a Task's linked Outlook/Teams Capture.
- Create a ReplyCommand only after explicit approval.
- Execute one approved Outlook reply and one approved Teams reply through the agent.
- Import minimal receipts and display Task activity.
- Exercise `failed` and `unknown`; confirm neither triggers automatic resend.

Gate: one approved command per provider is sent to the original thread and its receipt
is recorded once. Work Stack guarantees idempotent local records and no automatic
resend; it does not claim externally provable exactly-once Microsoft delivery. Drafting
or copying a read request cannot produce an external write.

### Wave 4 - dogfood hardening (28-32 hours; 36-hour stop-loss)

- Restart and duplicate-import checks for Captures, Task creation, ReplyCommands, and
  receipts.
- Raw/token/recipient leak audit across JSON stores, logs, exports, and browser storage.
- Backend/frontend suites, production build, and focused browser E2E.
- Independent adversarial check of prompt injection, altered reply digest, approval
  bypass, duplicate receipt, and ambiguous-send behavior.

At hour 32, remaining non-safety polish is deferred. At hour 36, the branch either meets
the acceptance gates or ships read-only; it does not expand into a runner platform to
rescue the schedule.

## 8. Parallel ownership and dependencies

| Lane | Ownership | Starts | Blocks on |
|---|---|---:|---|
| Capability | real Outlook/Teams OOB spike | 0h | existing tenant session |
| Contract | four envelopes and UX boundary | 3h | Gate 0 identifier shapes |
| Backend | Task-from-Capture and replies | 6h | frozen contracts |
| Frontend | copy/import and approval UX | 6h | frozen contracts |
| Safety | schemas, agent instruction, tests, audit | 6h | frozen contracts |
| Integration | real read/write and browser E2E | 14h | relevant Gate 0 capability and user validation window |

Contracts are frozen by hour 6. Lanes own disjoint files where possible and integrate at
the contract boundary. Outlook and Teams adapters remain independent so one provider's
failure does not block the other or the existing Work Stack.

## 9. Acceptance gates and release labels

### Common safety and usability gates

- The user completes the flow through Copy/Import controls without manually editing
  JSON.
- A selected Capture creates a new Task or links to an existing Task, and that basis is
  still visible after restart.
- No external write can occur before explicit target/body preview and approval.
- Replaying the same Capture, Task-creation request, ReplyCommand, or receipt creates no
  duplicate local record or Task activity.
- A receipt is rejected if its `reply_id`, provider, body digest, or target digest does
  not match the stored approved command.
- `unknown` never triggers automatic resend.
- Work Stack stores and logs contain no OAuth token, raw mail/chat body, HTML,
  attachments, or recipient list.
- OOB unavailability does not block normal planning, manual Capture import, or existing
  Task operations.
- The UI describes the agent handoff truthfully and does not claim background sync or
  provider health.
- The existing test suite and production frontend build remain green.

### Full bidirectional dogfood — target completion

- Outlook and Teams each complete real search/read and return a sanitized Capture with
  a stable source locator and Microsoft deep link.
- Outlook and Teams each complete one reply to the original linked thread through an
  approved command and matching imported receipt.
- All common gates pass for both providers.

### Single-provider partial dogfood — acceptable fallback, not plan completion

- At least one provider passes both read and reply gates end to end.
- The non-passing provider is visibly disabled and never represented as connected or
  supported.
- The release label names the passing provider and the failed Gate 0 capability.

### Read-only dogfood — safety fallback, not bidirectional completion

- At least one provider passes real search/read, Capture import, and Task context.
- Reply controls are disabled for every provider that failed the reply Gate 0 checks.
- The release is explicitly labeled read-only; no placeholder write path is shipped.

## 10. Deferred maturation triggers

The following are reconsidered only after this slice is used repeatedly:

- Build an application-callable bridge only when all three conditions hold: a supported
  authenticated invocation API is proven, the dogfood threshold below shows the manual
  handoff is the main friction, and a separate automation plan is explicitly approved.
- Add a durable queue, lease, heartbeat, retry, and provider health only when there is a
  real long-running consumer and user-mediated handoff is measurably the main friction.
- Add generic EvidenceLink roles only when users need multiple source roles, immutable
  citation snapshots, or independent evidence lifecycle beyond current Capture context.
- Add automatic ambiguous-send reconciliation only when the connector exposes a safe,
  stable lookup key and real sends show that manual review is inadequate.
- Add Activepieces or subscriptions only when unattended capture becomes the next
  validated product goal.

Suggested evidence threshold before these additions: at least 20 successful OOB
round-trips or five dogfood days, with observed handoff time, failure rate, duplicate
risk, and missing-context cases recorded.

## 11. Installation and user action

The current default build exposes the generic manual Capture path, not the unverified
Microsoft controls. The following OOB handoff instructions apply only to a provider lane
enabled after its Gate 0 evidence is retained and the frontend is rebuilt.

No Outlook or Teams desktop installation is required. Additional setup may require
re-authorizing the official OOB connector if the existing personal-tenant session is not
available to the agent used for Gate 0. Normal first-release use intentionally requires
a handoff for every operation: copy the read request or approved reply command, run it in
the connected agent, and import the Capture Packet or ReplyReceipt. Reply operations also
require the Work Stack preview and approval step. Secrets and tokens are never copied
into Work Stack.
