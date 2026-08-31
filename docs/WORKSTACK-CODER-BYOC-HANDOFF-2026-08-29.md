# Work Stack Coder BYOC Successor Handoff

Date: 2026-08-29

Producer disposition: **READY_FOR_REVIEW after commit B is pushed; independent review is still required.**

Evidence cutoff for the pre-checkpoint leaf inventory: **2026-08-29T13:32:47+09:00**

## 1. Non-authority statement and evidence precedence

This document is a recovery and continuation aid. It grants no Work Stack, Conduit,
Microsoft, provider, repository, or execution authority. It does not supersede the
accepted boundary ADR, frozen product contracts, repository code, or an independent
review decision.

When statements disagree, use this precedence:

1. the immutable Git commit and tree plus freshly reproduced verification evidence;
2. code and tests at that tree;
3. `contracts/api-v1.md` and `docs/WORKSTACK-CONDUIT-TASK-BOUNDARY-ADR.md`;
4. this handoff;
5. `docs/CURRENT-IMPLEMENTATION-STATUS.md`, plans, chat history, and running processes.

The already-running service on port 8765 is not durable checkpoint evidence.

## 2. Checkpoint identity and finality

| Field | Value |
| --- | --- |
| Repository | `https://github.com/Shinick-Han/work-stack.git` |
| Branch | `codex/workstack-cloud-checkpoint-20260829` |
| Base commit | `344fd21ad2e20f1f5b9a3e4497196cbced49eed5` |
| Base tree | `967ba17ef078f4ad1045c4c3968a604ef6ed80bd` |
| Product commit A | `2fd15b4698de2fc1dad1efa2d748fef803f0cc9c` |
| Product tree A | `43ddc39c706edc8ec5415aa1125f366b9b607acc` |
| Commit A time | `2026-08-29T13:35:50+09:00` |
| Commit A subject | `feat: checkpoint local-first work stack prototype` |
| Commit A change | 72 files, 10,005 insertions, 230 deletions |
| Commit A roster manifest | 72 `path<TAB>blob<TAB>size` rows ordered by Windows PowerShell `Sort-Object` (case-insensitive/culture ordering), LF-joined, UTF-8 without BOM or final LF; SHA-256 `0e45ee605e2c7368af5681ffb4eca791460480f71adbaf054daa8b03c36a2906` |
| Handoff commit B coordinate | Head of `refs/heads/codex/workstack-cloud-checkpoint-20260829`, parent A, sole changed path `docs/WORKSTACK-CODER-BYOC-HANDOFF-2026-08-29.md`; exact B SHA/tree must be returned externally by the producer |
| Target upstream | `origin/codex/workstack-cloud-checkpoint-20260829` |
| State immediately after A | 0 staged, 0 unstaged, 0 nonignored untracked paths |
| Finality | Producer checkpoint only; independent Codex acceptance is pending |

Commit B contains only this handoff. Its SHA cannot appear inside its own content. After
checkout, derive it with `git rev-parse HEAD`, derive its tree with
`git rev-parse HEAD^{tree}`, and verify that `git rev-parse HEAD^` equals product commit
A above. The producer's final response must name B and its remote proof.

## 3. Exact pre-checkpoint leaf classification and commit A roster

The authoritative count command was:

```text
git status --porcelain=v1 --untracked-files=all
```

At the cutoff it returned exactly **72 leaf paths: 34 tracked modified and 38
untracked**. There were 0 staged and 0 unmerged paths. The LF-joined status output had
SHA-256 `c30137d6605922b30940a54dd85a8393597b70fe706722342cea66f56e831cb3`.
The corresponding 72-file content manifest had SHA-256
`11bf0f4629262935e2817b38f86476ea535f4e5822c25996fe3b32129fe9efad`.

Plain `git status --short` collapsed three new directories and displayed 65 rows
(34 modified plus 31 collapsed untracked rows). Its output hash was
`84d69e6901028de833fe510404e7e6bc1eb2d5c3294ffd76f96dabd41f3edef7`.
That is a presentation difference, not a 65-file checkpoint.

The four literal lists below are both the exact classification and the exact commit A
path roster. Every path was staged literally. No directory-wide or broad add was used.

### Product source and repository tooling — 29 paths

```text
 M frontend/src/api/client.ts
 M frontend/src/app/App.tsx
 M frontend/src/app/urlState.ts
 M frontend/src/components/Dialog.tsx
 M frontend/src/domain/schemas.ts
 M frontend/src/domain/types.ts
 M frontend/src/features/inbox/CaptureDrawer.tsx
 M frontend/src/features/inbox/CaptureImportDialog.tsx
 M frontend/src/features/inbox/InboxPage.tsx
 M frontend/src/features/tasks/QuickTaskDialog.tsx
 M frontend/src/features/tasks/TaskDrawer.tsx
 M frontend/src/features/workspace/WorkspacePage.tsx
 M frontend/src/features/workspace/views/BoardView.tsx
 M frontend/src/features/workspace/views/types.ts
 M frontend/src/styles.css
 M frontend/src/types/workspace-views.d.ts
 M scripts/audit_export.py
 M workstack/capture.py
 M workstack/server.py
 M workstack/service.py
 M workstack/store.py
?? frontend/src/config/providerGates.ts
?? frontend/src/features/focus/FocusPage.tsx
?? frontend/src/features/focus/focusModel.ts
?? frontend/src/features/focus/useLocalToday.ts
?? frontend/src/features/integrations/MicrosoftOobDialog.tsx
?? frontend/src/features/tasks/ReplyComposer.tsx
?? frontend/src/utils/clipboard.ts
?? frontend/src/vite-env.d.ts
```

`frontend/src/vite-env.d.ts` is an intentional provider-gate source declaration.
`scripts/audit_export.py` is repository-owned release/security tooling.

### Tests — 21 paths

```text
 M frontend/src/api/client.test.ts
 M frontend/src/app/App.test.tsx
 M frontend/src/features/inbox/InboxPage.test.tsx
 M frontend/src/features/tasks/TaskDrawer.test.tsx
 M tests/test_api.py
 M tests/test_audit_export.py
 M tests/test_capture.py
?? frontend/src/app/urlState.test.ts
?? frontend/src/components/Dialog.test.tsx
?? frontend/src/config/providerGates.test.ts
?? frontend/src/domain/schemas.test.ts
?? frontend/src/features/focus/FocusPage.test.tsx
?? frontend/src/features/focus/focusModel.test.ts
?? frontend/src/features/focus/useLocalToday.test.ts
?? frontend/src/features/inbox/CaptureDrawer.test.tsx
?? frontend/src/features/integrations/MicrosoftOobDialog.test.tsx
?? frontend/src/features/tasks/QuickTaskDialog.test.tsx
?? frontend/src/features/tasks/ReplyComposer.test.tsx
?? frontend/src/features/workspace/views/BoardView.test.tsx
?? frontend/src/test/providerGates.ts
?? tests/test_replies.py
```

### Documentation and frozen contract text — 15 paths

```text
 M README.md
 M SECURITY.md
 M SKILL.md
 M contracts/api-v1.md
 M docs/RELEASE-CHECKLIST.md
 M frontend/src/features/workspace/views/README.md
?? docs/BOARD-STATUS-MUTATION-STABILITY-PLAN.md
?? docs/CURRENT-IMPLEMENTATION-STATUS.md
?? docs/FOCUS-EXPERIMENT-IMPLEMENTATION-PLAN.md
?? docs/MICROSOFT-HANDOFF-ACTIVITY-IMPLEMENTATION-PLAN.md
?? docs/OOB-OUTLOOK-TEAMS-IMPLEMENTATION-PLAN.md
?? docs/PRACTICAL-ADVERSARIAL-REVIEW-POLICY.md
?? docs/QUICK-ADD-CREATE-CONTINUE-PLAN.md
?? docs/TASK-DRAWER-SAVE-SERIALIZATION-PLAN.md
?? docs/TODAY-FOCUS-IMPLEMENTATION-PLAN.md
```

### Synthetic contract fixtures — 7 paths

```text
?? contracts/oob-request-v1.outlook.fixture.json
?? contracts/oob-request-v1.teams.fixture.json
?? contracts/reply-command-v1.outlook.fixture.json
?? contracts/reply-command-v1.teams.fixture.json
?? contracts/reply-receipt-v1.failed.fixture.json
?? contracts/reply-receipt-v1.sent.fixture.json
?? contracts/reply-receipt-v1.unknown.fixture.json
```

Classification summary: 29 product/tooling, 21 tests, 15 documentation, and 7
synthetic fixtures. There were 0 candidate generated outputs, local-runtime files,
personal-data files, secret-risk files, or identifiable unrelated user changes.

Nine untracked Markdown plans contained intentional hard-break trailing spaces. The
fully staged whitespace check exposed them; only those trailing spaces were removed
before A. This changed no product semantics. It did change the committed receipt bytes,
which is recorded in the contradiction section.

## 4. Implemented capabilities

- One local PlanningTask model is rendered through Graph, Board, Treemap, and a
  deterministic read-only Focus projection.
- Graph renders objectives, tasks, notes, alignment, dependency, parent, and reference
  edges. URL state preserves surface, view, filters, search, selected Task, and Capture.
- Board select and drag share a per-Task mutation lock. Status updates use optimistic
  cache state, revision checks, scoped rollback, and a final authoritative refresh.
- Task Drawer edits title, status, priority, due date, parent, objectives, detail, tags,
  and dependencies. It serializes PATCH operations, keeps field-level latest intent,
  advances revisions monotonically, and requires explicit retry after conflict or
  ambiguous failure.
- Quick Add is available from Workspace, Focus, Context Inbox, and the command shortcut.
  It opens the created Task immediately, blocks same-tick duplicate submit/close/edit,
  and distinguishes an unverifiable 2xx response from a known failure.
- Context Inbox accepts validated sanitized Capture Packet v1 data, links context,
  converts stable action IDs, creates a Task directly from a source, and dismisses a
  Capture.
- User-mediated Outlook/Teams request/import and approval/reply/receipt contracts and
  UI exist against synthetic fixtures.
- Generic manual Capture import remains enabled when Microsoft-specific lanes are
  disabled.
- The CLI supports objectives, tasks, subtasks, worklogs, notes, weekly roll-up, graph
  export/server, and server-forwarded Capture ingest.
- Tracked demo fixtures contain 30 Tasks and 5 objectives and exercise all four planning
  statuses.

## 5. Explicit nonclaims

- There is no direct Outlook or Teams connector in the Python server.
- There is no Microsoft background sync, poller, worker, queue, runner, retry center, or
  health endpoint.
- No real Outlook/Teams read or reply Gate 0 evidence exists. All four production build
  flags default false. A flag is a UI/release-evidence gate, not backend authorization or
  provider health.
- Microsoft delivery is not externally provable exactly once. `unknown` is terminal and
  never automatically resent.
- Capture validation is defense in depth, not a raw-content redaction service. The
  upstream sanitizer is trusted.
- The service does not provide multi-user isolation, same-host adversarial isolation,
  or safe direct network exposure.
- Activepieces, AgentFlow, SQLite, Postgres, external databases, and Conduit runtime are
  not embedded.
- No Conduit Task creation, Taskroom, status synchronization, Room, Seat, Run,
  ExecutionAsset, Artifact, Evidence, Gate, Outcome, Attention Hold, authority,
  provider/session binding, or Conduit event-log append is implemented.
- Graph, Treemap, and Focus cards are read-only surfaces; the shared Drawer can mutate a
  selected Task.
- No CI workflow was observed.
- No explicit repository license file was observed. Licensing is an open human decision
  before distribution; it does not block this checkpoint.
- The prior browser receipt and a running local process are not durable acceptance
  evidence.

## 6. Architecture, store, API, mutation, recovery, and security map

### Components

| Area | Implementation |
| --- | --- |
| UI | React 19, Vite, TanStack Query, Zod, React Flow, Recharts, DnD Kit |
| HTTP/static host | Python standard-library `ThreadingHTTPServer`; serves `frontend/dist` when present, otherwise `web/index.html` |
| Domain | `workstack/service.py` |
| Capture safety boundary | `workstack/capture.py` |
| Persistence, lease, journal | `workstack/store.py` |
| API/static routing | `workstack/server.py` |
| Contracts | `contracts/api-v1.md` plus versioned synthetic fixtures |

### Local store

The store defines eight JSON documents: `workspace.json`, `backlog.json`, `okr.json`,
`worklog.json`, `notes.json`, `captures.json`, `replies.json`, and `activity.json`.
Tracked `data/*.json` files are synthetic demo inputs, not the runtime SSOT.

- `WORK_STACK_HOME` overrides the data directory.
- Windows default: `%LOCALAPPDATA%\WorkStack\data`.
- POSIX default: `~/.local/share/workstack`.
- `WORK_STACK_RUNTIME` overrides ephemeral discovery/token state.
- Default runtime state is hash-partitioned by normalized data path under
  `%LOCALAPPDATA%\WorkStack\runtime` on Windows or `~/.local/state/workstack` on POSIX.

### API

Current stable routes include session, Workspace projection, Task detail/PATCH,
Capture list/ingest/link/action conversion/generic Task creation/dismiss, reply approval,
and reply receipt under `/api/v1`. Legacy compatibility routes remain for state, Task
creation/status, objectives, worklogs, and notes.

There is **no** `POST /api/v1/tasks`. Quick Add still calls legacy `POST /api/tasks`.
Task PATCH requires a nonnegative current revision and returns 409 on conflict.
Capture and reply POSTs require idempotency keys; legacy Task creation does not.

### Mutation and recovery

- Board uses one synchronous per-Task lock for select and drag, optimistic state,
  conditional rollback, returned revision, and one refresh after the mutation group.
- Task Drawer allows one in-flight PATCH, queues latest field intent, preserves dirty
  intent on conflict/failure, and does not automatically replay ambiguous writes.
- Quick Add uses a synchronous submit/close/edit gate. Only invalid JSON/schema after a
  successful 2xx response is classified commit-unknown. A transport loss after the
  legacy server commits is surfaced as a normal failure; because that POST has no
  idempotency key, a user retry can create a duplicate Task.
- The HTTP server owns a nonblocking exclusive data-directory writer lease for its
  lifetime. Offline CLI writers acquire the same lease.
- Writes use same-directory temporary files, flush/fsync, and atomic replace.
- Multi-file mutations first persist a complete-value recovery journal with canonical
  SHA-256 digests. Startup validates and replays it.
- Invalid JSON or journal state fails closed and is preserved; it is never silently
  replaced with an empty default.
- Directory fsync and power-loss durability beyond the tested process-crash model are
  not proven.

### Security boundary

- Server startup rejects non-loopback binding.
- Requests require an exact loopback Host and listening port.
- Browser mutations require exact same-origin HTTP Origin plus a server-lifetime CSRF
  nonce.
- The runtime bearer capability is accepted only for Capture ingest and is not user
  authentication.
- JSON framing rejects invalid media type, transfer encoding, invalid length, and
  oversized bodies. Capture maximum is 64 KiB; other JSON routes use 1 MiB.
- Responses use `no-store` and `nosniff` and do not grant CORS.
- Capture validation rejects forbidden/raw-shaped fields, addresses, credential-shaped
  material, unapproved URL/tool provenance, HTML, quoted replies, and oversized source
  text.
- With Microsoft Gate 0 flags false, generic Capture import still accepts a packet that
  self-asserts `oob_verified` provenance and the UI labels it `OOB verified`. No live
  provider check makes that label trustworthy; it is supplied provenance, not Gate 0
  evidence.
- Reply approval derives the immutable target from linked sanitized context; the browser
  cannot choose a recipient, provider, or target. Receipts must match reply, provider,
  body digest, and target digest.
- Approved user-authored reply text and opaque target locators are intentionally stored.
  OAuth material, raw Microsoft bodies, recipient lists, and connector sessions are not.

## 7. PlanningTask identity and status history: current facts vs accepted target

### Currently implemented facts

- Human display IDs are sequential aliases such as `T-0001`.
- A Workspace UUID is generated and persisted when `workspace.json` is first created.
- Task UID is currently UUIDv5 of Workspace UUID plus the case-preserved display ID.
- New Tasks persist that UID. Versioned projections synthesize the same UID for legacy
  records that do not store it.
- The tracked 30-Task fixture persists neither Task UID nor Task revision.
- Frontend Task UID is optional and is validated as a string rather than a UUID.
- Raw CLI reads, legacy responses, and graph export can omit UID.
- Planning status vocabulary is `open`, `started`, `done`, and `dropped`.
- Versioned PATCH writes generic `task.updated` Activity with changed field names only.
- CLI and legacy status mutations write no Activity. Equivalent append-only status
  history across mutation paths does not exist.

### Accepted target contract, not current behavior

The accepted ADR requires immutable Workspace and PlanningTask UUID identity and the
canonical reference grammar:

```text
workstack://<workspace-uuid>/planning-tasks/<planning-task-uuid>
```

That URI is documentation only today. The accepted target also keeps PlanningTask and
Conduit Task as separate entities and permits only a copied creation snapshot plus an
optional originating PlanningTask reference.

### Open identity/history gaps

- Persist and validate immutable UUIDs for every legacy and new PlanningTask without
  changing existing display IDs or already persisted UID values.
- Expose the canonical URI consistently across CLI, API, graph export, and future
  contracts.
- Add one append-only transition fact for every successful status mutation path with
  immutable UID, old/new status, resulting revision, occurrence time, and controlled
  mutation source.
- Commit backlog state and the transition fact in one recovery-journal operation.
- A `workstack.planning-task-snapshot.v1` contract does not yet exist.

## 8. Work Stack–Conduit authority matrix and forbidden coupling

| Concern | Work Stack authority | Conduit authority |
| --- | --- | --- |
| PlanningTask identity | Owns | Optional originating reference only |
| Planning title/detail, objective, priority, due, tags | Owns | May copy an initial proposal snapshot |
| Planning dependencies, notes, worklog, sanitized context | Owns | No mutation authority |
| Planning status and future planning-status facts | Owns | No automatic mapping |
| Canonical execution Task lifecycle | No authority | Owns |
| Room, Seat, Run, ExecutionAsset | No authority | Owns |
| Artifact, Evidence, Gate, Outcome, Attention Hold | No authority | Owns |
| Execution authority and provider/session binding | No authority | Owns |

One PlanningTask may propose zero to many Conduit Tasks. A Conduit Task may retain one
optional originating PlanningTask reference. A copied proposal is a point-in-time
snapshot and creates no continuing shared authority.

Forbidden coupling:

- no shared database;
- no bidirectional planning/execution status sync;
- no direct append to the Conduit event log;
- no import of Conduit internals into Work Stack ownership;
- no use of Work Stack display IDs as cross-product canonical identity;
- no Activepieces or AgentFlow second orchestration authority.

The first four relationship rules are accepted by the ADR. The shared-database,
event-log, internals-import, and second-authority prohibitions are imposed by this
checkpoint request and are consistent with, but not literal quotations from, the ADR.

## 9. Contract and fixture versions, with migration impact

`contracts/api-v1.md` is frozen for this local prototype. Workspace projection,
Capture Packet, OOB request, and ReplyReceipt use internal `schema_version` `1.0`.
ReplyCommand v1 is versioned by its frozen contract/fixture name but intentionally has
no `schema_version` field.

| Fixture | Internal version | SHA-256 |
| --- | --- | --- |
| `capture-packet-v1.fixture.json` | 1.0 | `e447a804ce3d9ed6b9a204962206baa29f693a722bfb96ace76ab77d88c250d6` |
| `capture-packet-v1.manual.fixture.json` | 1.0 | `80a8c857c349d10da19244adefe53d3407d257a3481d722b460f693a9a2db7d0` |
| `capture-packet-v1.negative-raw.json` | 1.0 | `c43ff39c3d055966799134d4ad2e244730e69f50e90ec8c645f55fcc0fcacc4a` |
| `capture-packet-v1.stale.json` | 1.0 | `d1f54ed789cdc7e30c3a7661ad1aa737c52b3ee770d170ffa60160868596cf91` |
| `capture-packet-v1.value-negative-cases.json` | wrapper; no schema field | `51e4a6a8cb4e5437556c104a421fef0eb46cd0f6d875f2f3bd8573ef1fe2cc95` |
| `oob-request-v1.outlook.fixture.json` | 1.0 | `2a56c1626a461a75ab9592b0d7320477dfd8b124940d63b4841b5a55a3851c22` |
| `oob-request-v1.teams.fixture.json` | 1.0 | `80c3e8830ec78cb576006653016bb7045e546717a2c73613c23f334f8b572e18` |
| `reply-command-v1.outlook.fixture.json` | no schema field | `2212417a7c0a8ef64d2258b35419bfe5b7f73c155c7ac0330a3a30a4674073ec` |
| `reply-command-v1.teams.fixture.json` | no schema field | `9a4d3495cde4460ed8523d9cedefb1c2b2d5ad07a00b460ac0d354ea3faa985f` |
| `reply-receipt-v1.failed.fixture.json` | 1.0 | `73f83b9cb7ce0a28e61772f804a6023694dcf220f625dc024c03919ec2141cda` |
| `reply-receipt-v1.sent.fixture.json` | 1.0 | `46401196d50f1d16d228e5a27edd7d53bbff0e49d707355fd8bb1330b2a9d166` |
| `reply-receipt-v1.unknown.fixture.json` | 1.0 | `d16464959b7ec87e9574808273d00cdfeb9474bb3545e1a8c2b616717e150570` |

Migration rules:

- Identity/reference migration comes first. Backfill legacy identity idempotently and
  never change an existing UID or display ID.
- Preserve display-ID routes as compatibility aliases while canonical identity becomes
  available.
- Add versioned Task creation rather than changing legacy creation semantics in place.
- Do not add `schema_version` to ReplyCommand v1 in place; current strict frontend DTOs
  and fixtures omit it.
- Status facts must record the stable identity established by the first migration.
- Add the planning snapshot as a separate provider-neutral contract after identity,
  creation, and status contracts stabilize. Do not overload Workspace, Capture, OOB, or
  Reply schemas.

## 10. Reproducible Linux Coder bootstrap from a fresh clone

Requirements: Python 3.10 or newer, Node `^20.19.0` or `>=22.12.0`, npm, Git,
and preconfigured authenticated read access to the private GitHub repository through a
Coder secret, credential helper, or SSH key. Never embed a token in this document, clone
URL, image, or repository. Python runtime code uses the standard library; frontend
dependencies are lockfile-based.

```sh
git clone --branch codex/workstack-cloud-checkpoint-20260829 --single-branch \
  https://github.com/Shinick-Han/work-stack.git
cd work-stack

git log --oneline --decorate -2
test "$(git rev-parse HEAD^)" = "2fd15b4698de2fc1dad1efa2d748fef803f0cc9c"
test "$(git rev-parse HEAD^{tree})" != ""

python3 --version
node --version
npm --version

python3 -m unittest discover -s tests -v
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
python3 scripts/audit_export.py .

export WORK_STACK_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/workstack"
export WORK_STACK_RUNTIME="${XDG_STATE_HOME:-$HOME/.local/state}/workstack"
python3 run_work_stack.py graph serve --host 127.0.0.1 --port 8765 --seed-demo
```

`--seed-demo` refuses to overwrite a nonempty planning core. Runtime data must remain
outside Git.

Current Coder constraint: the server rejects non-loopback bind and validates Host and
Origin against plain HTTP loopback plus the exact socket port. A standard hostname/TLS
reverse proxy is therefore not demonstrated compatible. Use a TCP/SSH local forward
that makes the browser itself use `http://127.0.0.1:8765`. Exact Coder product
port-forward syntax and fresh Linux browser behavior are **UNKNOWN**; do not invent or
weaken the server boundary to make a proxy work.

There is POSIX `fcntl.flock` code and no fresh Linux CI evidence in this repository.
The bootstrap is reproducible as commands, but Linux/Coder acceptance remains a
successor verification task.

## 11. Windows-only acceptance and local browser checks

Verified by code and the fresh 67-test Windows run:

- Windows data default resolves under `%LOCALAPPDATA%\WorkStack\data`.
- Ephemeral runtime metadata is hash-partitioned under
  `%LOCALAPPDATA%\WorkStack\runtime`.
- `msvcrt.locking` provides the nonblocking one-byte writer lease.
- Tests cover server-lifetime lease exclusion, a second writer failing closed, CLI
  Capture forwarding while the server owns the lease, journal replay, corrupt-state
  fail-closed behavior, atomic Capture/Task/Reply recovery, and the lock sentinel.
- The symlink-rejection test skipped because this Windows account lacked symlink
  privilege. That is an environmental skip, not a passing symlink test.

Still required for a fresh Windows acceptance run:

1. Override `LOCALAPPDATA`, `WORK_STACK_HOME`, and `WORK_STACK_RUNTIME` in a child process
   to disposable directories; never mutate a personal runtime for QA.
2. Start on `127.0.0.1` at an unused port with synthetic seed.
3. Open Graph, Board, Treemap, Focus, and Context Inbox locally.
4. Create exactly one Quick Add Task and verify immediate Drawer/deep link, revision 0,
   and no duplicate after rapid submit/cancel.
5. Confirm Microsoft controls remain disabled in the default build and generic manual
   import remains available.
6. Confirm no browser console errors, stop the server, run tree-mode audit on the
   disposable data, and remove only that verified disposable directory.
7. Restart against the same disposable data and verify normal recovery; separately use
   the automated corruption/journal tests rather than hand-editing a live store.

The receipt reports earlier disposable browser QA, but it was not rerun as immutable
checkpoint evidence. Browser acceptance at commit A is therefore **UNKNOWN**, not
passed. Network-share, WSL, and power-loss filesystem behavior are also unverified.

## 12. Verification commands, versions, counts, and hashes

Host tools:

| Tool | Version |
| --- | --- |
| Python | `3.12.10` |
| Node | `v24.19.0` |
| npm | `11.17.0` |
| Git | `2.55.0.windows.3` |
| PowerShell | `7.6.4` |
| OS | `Microsoft Windows NT 10.0.26200.0` |

Evidence hashing method: capture combined stdout/stderr as text lines, join with LF,
encode UTF-8 without BOM, add no final LF, then SHA-256. Timing and local absolute paths
may make raw test/build output hashes host-specific; counts and immutable Git identity
remain the primary facts. Raw output bytes were not retained as durable artifacts, so
the durable evidence path/URI for every row is explicitly `EVIDENCE_UNAVAILABLE`.
Each summary and hash is committed in this handoff-only commit B and becomes remotely
reachable at `origin/codex/workstack-cloud-checkpoint-20260829` after the completion
push; this does not imply the underlying raw bytes are retrievable.

| Command | Exit | Result | Evidence SHA-256 | Durable evidence / committed state |
| --- | ---: | --- | --- | --- |
| `python -m unittest discover -s tests -v` | 0 | 67 tests passed; 1 Windows symlink-privilege skip; 72 captured lines | `ff2633283b9a6ad16482f365e332b8b69d9c73e379bf571325971b5f1b5b9b6c` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `npm --prefix frontend ci` | 0 | 223 packages added, 224 audited, 0 vulnerabilities; deprecation and esbuild allow-scripts review warnings retained | `736687a4457b01ffd866d47e47667fa3e05a30f58780839573134a51dcb32bd4` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `npm --prefix frontend test` | 0 | 18 files, 92 tests passed; Vitest 3.2.7 | `924b95cb36c6050c2318d456d1096d5d975fbbfc57d4d313693828724e89da26` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `npm --prefix frontend run build` | 0 | Vite 7.3.6, 906 modules; 65.74 kB CSS and 888.40 kB JS; annotation and chunk-size warnings only | `d3be0812392e7ef8c30a5dd1d42ce98811d3f31ec3f3f9c18d52ad82e73c568e` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `python scripts/audit_export.py .` at commit A | 0 | 115 UTF-8 files, source policy | `252228611c2f54e8c825e385446ee0c04fac9928167ec38abb14a9e03a30ff92` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `python scripts/audit_export.py $auditRoot --mode tree` on disposable seeded runtime | 0 | 5 UTF-8 files, tree policy | `2bc0ad7425fd41be7bbd09fb83977216037648eb050061ee39e7a26ee8931e96` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| initial tracked-only `git diff --check` | 0 | 34 CRLF conversion warnings; it could not see untracked files | `41188ab809b3066829731184b5bbd8986c669245517d3b418060832621121c00` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| initial fully staged `git diff --cached --check` | 2 | exposed 17 Markdown trailing-space lines; corrected before A | `0740ad2aa6b97613377e8194553a5360f1aa383f28ee7bb9cc81f821d1c44207` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| final fully staged `git diff --cached --check` before A | 0 | empty output | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `python scripts/audit_export.py .` with this handoff present | 0 | 116 UTF-8 files, source policy | `a94af256454fd8d3aa0944858d2348f7c7f5c9fa7db2e0797efe2588f8357d62` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| handoff-only `git diff --cached --check` before B | 0 | empty output | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |
| `python scripts/render_qr.py --help` | 1 | import fails with `ModuleNotFoundError: No module named 'qrcode'`; no repository Python dependency declaration was found | `5c6a9d63397e8489a0d0c7d711559e7c839fbe581e6becffce646ad971191e99` | `EVIDENCE_UNAVAILABLE`; summary/hash in B, remote after push |

No command drift substitution was needed. Generated `frontend/node_modules` and
`frontend/dist` were not staged.

## 13. Synthetic evidence and exclusion proof

The synthetic disposable-runtime audit used a newly created OS temporary directory,
called the repository-owned `Store.seed_demo(Path('data'))`, and then ran tree policy.
It produced exactly these files:

```text
.workstack.lock
backlog.json
notes.json
okr.json
worklog.json
```

The sorted `relative-path<TAB>SHA-256<TAB>size` manifest hash was
`9353cdcab615a5261f14318b9ef1709e68b6730f98f08596a82bb97c1114e685`.
The temporary files and their now-empty directory were deleted after the audit.

The measured content-risk scan root was `.` at the repository root and its roster was
exactly the 72 pre-checkpoint candidate leaf paths: 34 tracked modified paths from
`git diff --name-only --diff-filter=ACMRTUXB` plus 38 nonignored untracked paths from
`git ls-files --others --exclude-standard`, sorted and deduplicated. It did not scan the
unchanged baseline, Git history, ignored roots, or the later handoff. Git derived the
roster; PowerShell/.NET read each leaf as text and applied regular expressions. A 5 MiB
per-file ceiling existed; zero candidates were skipped as large or unreadable.

The pattern classes were PEM private-key headers; GitHub `gh*`, OpenAI-style `sk-`, AWS
`AKIA`/`ASIA`, and Slack `xox*` token signatures; JWT-shaped values; literal bearer
authorization; assigned access/refresh tokens, client secrets, API keys, passwords, and
tenant/client UUIDs; Windows user-profile absolute paths; email-shaped values; and
Microsoft service URLs under SharePoint, Teams, Outlook, and Graph hostnames. Email
domains allowed as synthetic were `example.com`, `example.org`, `example.net`,
`contoso.com`, `fabrikam.com`, `localhost`, any `.invalid` or `.test` domain, and
`workstack.local`. Microsoft URL context allowed only contract, fixture, sanitizer,
adversarial-test, mock/sample, placeholder-ID, and duplicate fixture-to-test evidence;
full URL values were compared through truncated SHA-256 and redacted before inspection.

Aggregate results were zero private keys; zero recognized token, JWT/bearer, OAuth,
secret/API-key/password, or tenant/client assignment values; zero nonsynthetic email
addresses; zero personal absolute paths; and zero identified real Microsoft, personal,
credential, runtime, or generated material. There were 56 Microsoft URL occurrences,
36 unique URL hashes, and nine containing files: one API contract, one explicitly
synthetic reply-receipt fixture, and seven test files. No production-source file
contained a literal Microsoft URL.

This was deterministic regex plus redacted-context review, not an entropy/history tool
such as Gitleaks. It cannot prove the absence of unknown, obfuscated, encrypted, or
unusually formatted material. Synthetic classification is evidence-based, not
cryptographic proof. Ignored runtime content physically existed but was outside scope
and was not used to claim candidate safety.

Explicitly excluded and never tracked/staged:

- `.runtime/`: local/disposable runtime and lock/state files;
- `frontend/node_modules/`: installed dependencies and caches;
- `frontend/dist/`: generated build output;
- Python `__pycache__/` and bytecode;
- logs and VCS metadata;
- personal runtime, credentials, real Microsoft content, and machine-specific files.

`git ls-files` returned zero paths beneath those excluded roots. Source audit is an
allowlist and does not prove excluded content safe; literal-path staging plus the
staged-roster comparison is the commit-boundary proof.

The ignored inventory method used `git status --short --ignored --untracked-files=normal`,
`git check-ignore -v --no-index`, and `git ls-files` probes. At inspection it measured
9 `.runtime` files (39,072 bytes), 3 `frontend/dist` files (954,691 bytes), 14,503
`frontend/node_modules` files (127,563,651 bytes), and 15 Python cache files, with zero
tracked paths under those roots. These counts describe excluded local state only and do
not make its content checkpoint evidence.

Hardening debt: `.gitignore` covers the present runtime/build/cache roots but does not
cover every conventional local secret filename or a generic `runtime/`/`.codex/` root.
No such path existed in the candidate. Continue literal staging until ignore policy is
reviewed; never use broad staging.

## 14. Technical-debt register and dependency order

| Priority | Status | Scope |
| --- | --- | --- |
| P0 checkpoint/handoff | Product A sealed; B/push required for producer READY_FOR_REVIEW | Preserve the remotely recoverable checkpoint and independent review boundary |
| P1 immutable identity/reference | NOT STARTED; partial UUID projection foundation exists | Persist/validate Workspace and PlanningTask UUIDs and emit canonical `workstack://` reference |
| P2 idempotent v1 Task create | NOT STARTED | Add strict `POST /api/v1/tasks`, required key/digest, frozen replay, conflict, and response-loss recovery; migrate Quick Add |
| P3 append-only planning transitions | NOT STARTED | Make CLI, legacy, and v1 status mutations emit equivalent immutable Work Stack facts atomically |
| Fourth dependency: planning snapshot | NOT STARTED; begin only after P1-P3 stabilize | Add provider-neutral read-only `workstack.planning-task-snapshot.v1` fixture/contract |

Red-review severities below are concrete findings, not a change to the immutable
successor dependency order above:

- **P1 — Quick Add response-loss duplication:** legacy `POST /api/tasks` has no
  idempotency key. Only an invalid successful 2xx body becomes commit-unknown; transport
  loss after commit is a normal failure, so retry can duplicate the Task.
- **P1 — incomplete planning-status history:** legacy status PATCH accepts no expected
  revision and therefore bypasses revision-conflict control; it increments the stored
  revision but writes no Activity. Versioned PATCH Activity records changed field names
  but not a complete immutable old/new status transition, so no equivalent append-only
  chain exists across CLI, legacy web, and v1 web paths.
- **P1 — unverified Capture provenance label:** while Microsoft Gate 0 is false, generic
  Capture import accepts self-asserted `oob_verified` and renders `OOB verified`. Future
  work must either restrict generic import to manual provenance or visibly label supplied
  provenance as Gate 0 unverified.
- **P2 — stale receipt wording:** `docs/CURRENT-IMPLEMENTATION-STATUS.md` in A describes a
  running server and an uncommitted checkpoint. It is historical evidence only. Product
  truth is A `2fd15b4698de2fc1dad1efa2d748fef803f0cc9c` / tree
  `43ddc39c706edc8ec5415aa1125f366b9b607acc`; handoff B is the next branch-head commit,
  parent A and handoff-only, with its exact SHA/tree returned in the producer reply.

Other registered debt: successful create plus failed background refresh can coexist;
Capture draft rebase is unspecified; cross-tab coordination, durable drafts, and undo
are absent; bundle splitting is absent; Microsoft Gate 0 is unverified; Linux/Coder and
browser acceptance are missing; CI is absent; ignore coverage is incomplete.

Optional-tooling debt is confirmed and non-blocking: `python scripts/render_qr.py --help`
exits 1 during import with `ModuleNotFoundError: No module named 'qrcode'`, and the
repository has no declared Python dependency file for that optional script. No package
was installed and no product code was changed for this checkpoint.

## 15. Exact first three successor tasks and stop conditions

### 1. P1 — immutable identity and canonical reference

Implement an idempotent migration that persists and validates immutable Workspace and
PlanningTask UUIDs, preserves all existing UID values and display IDs, and exposes
`workstack://<workspace-uuid>/planning-tasks/<planning-task-uuid>` consistently through
CLI/API/graph projections.

Stop if any identity changes across restart or repeated migration, if legacy display
routes break, if an existing UID is rewritten, if migration cannot fail closed without
partial persistence, or if Conduit/provider/session concepts enter the identity model.

### 2. P2 — versioned idempotent Task creation

Add strict `POST /api/v1/tasks` with required `Idempotency-Key`, canonical request
digest, exact frozen creation-response replay, same-key/different-body 409 conflict,
atomic Task plus idempotency persistence, safe restart/lost-response recovery, and
Quick Add migration using one stable key per user intent.

Stop if a crash, restart, or response-loss retry can create a second Task; if replay
returns a later mutated Task rather than the frozen creation response; if any automatic
retry changes the key; or if the work expands into Conduit or Microsoft integration.

### 3. P3 — append-only Work Stack planning-status transition facts

Route CLI, legacy web, and v1 web status changes through one canonical transition path.
Each successful fact must include immutable PlanningTask UID, old/new planning status,
resulting revision, strict occurrence time, and a controlled mutation-source enum.
Backlog state and fact must commit in one journal operation.

Stop if any status path bypasses the fact writer; if failed or non-status mutations emit
a transition; if history can be edited/deleted through normal mutation APIs; if a fact
uses display ID as canonical identity; or if Work Stack begins mapping to Conduit
execution lifecycle.

After all three pass independently, the fourth task is the provider-neutral read-only
planning snapshot. It must not contain Conduit IDs, Room, Seat, Run, ExecutionAsset,
Artifact, Evidence, Gate, Outcome, Attention Hold, authority, or provider sessions.

## 16. Rollback, recovery, branch deletion, contradictions, and UNKNOWNs

### Git recovery and rollback

- Recover product A in a fresh clone or worktree with
  `git switch --detach 2fd15b4698de2fc1dad1efa2d748fef803f0cc9c`.
- Recover the full handoff branch by fetching and checking out
  `origin/codex/workstack-cloud-checkpoint-20260829`.
- Do not reset, clean, stash, or force-push a dirty successor workspace. Use a fresh
  clone/worktree to compare or roll back.
- Do not merge this branch or treat it as accepted until independent review passes.
- Do not delete the remote or local branch until the owner explicitly authorizes it and
  verifies that both A and B are reachable elsewhere. Remote branch deletion is
  destructive and is not authorized by this handoff.

### Runtime recovery

- Stop the server before offline writes or backup/restore.
- Preserve the complete data directory. Do not delete a lock or journal to bypass
  contention.
- Startup replays a valid complete-value journal. Invalid JSON or journal data fails
  closed and must be repaired or quarantined on a copy.
- Test restore/recovery on a disposable copy before touching the user's SSOT.

### Resolved contradictions

- The original receipt identity matched exactly at inspection: 10,455 bytes and
  SHA-256 `3cc87e387f217ed2faa02551a5c5f831b5138a931eee06a4b630c350ace91db2`.
  The required staged whitespace check then removed two trailing spaces from each of its
  first two metadata lines. Commit A contains the same substantive receipt at 10,451
  bytes with SHA-256
  `124af422f15dd630b76ed421e6eb5fb90fc783e86e3edf2cd67ccde5dccd972c`.
- Statements inside that receipt about a running server and an uncommitted checkpoint are
  historical, not current operational truth. The immutable product coordinate is A
  above; B is the handoff-only branch head whose exact SHA/tree is reported externally.
- The observed 65-row plain status was a collapsed display. The required leaf command
  measured and classified 72 literal paths.
- Receipt counts were historical claims. Fresh evidence confirms 67 backend tests plus
  one skip, 18 frontend files/92 tests, and 115 source-audited files at A.
- The receipt's 9-file runtime audit concerned a different pre-existing runtime target.
  This checkpoint used a fresh five-file synthetic disposable runtime and did not use
  personal runtime as release evidence.
- Receipt bundle size was historical. The fresh lockfile install/build produced an
  888.40 kB JS asset and retained the chunk warning.
- The ADR accepts immutable identity and canonical reference, but implementation is only
  partial and the URI is not emitted.
- The checkpoint request assigns Work Stack future status-history ownership, but
  equivalent append-only status facts do not yet exist.
- ReplyCommand is called v1 by its frozen contract/fixture but has no internal version
  field.

### UNKNOWN or explicitly unverified

- Fresh Linux/Coder tests and browser behavior.
- Exact Coder product port-forward syntax and compatibility of its standard hostname
  proxy with the current Host/Origin boundary.
- Fresh immutable Windows browser smoke at A.
- Direct unit acceptance of the Windows default `LOCALAPPDATA` path.
- Network-share, WSL, directory-fsync, and power-loss durability.
- Actual Outlook/Teams Gate 0 capability and upstream provider retention.
- Independent acceptance of this producer checkpoint.

- Snapshot cutoff time: 2026-08-29T13:32:47+09:00
- Producer state: READY_FOR_REVIEW after the two-commit branch is pushed; independent review remains required
- Remotely recoverable: YES after the completion push; exact remote SHA proof is external to this self-referential handoff and must be returned by the producer
- Cloud resume ready: YES for independent review and the ordered successor tasks only
- Blocking conditions: None for independent review; acceptance and future implementation are not implied
- First legal action: Fetch the checkpoint branch in a fresh clone/worktree and verify product A, handoff-only B, trees, roster, hashes, and prohibitions
- Required human decisions: Independent checkpoint acceptance, repository license, CI policy, Microsoft Gate 0, and Linux/Coder browser acceptance
- Forbidden actions: Merge or delete the checkpoint branch before acceptance; rewrite A/B; use broad staging; ingest personal or real Microsoft data; implement Conduit/provider coupling or P1-P3 during checkpoint review
