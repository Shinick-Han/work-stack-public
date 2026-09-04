# Work Stack CLI + Agent Skill implementation plan

Date: 2026-09-02

Status: launch plan ready; M0/Q0/O1/O2/T0 bootstrap artifacts not yet implemented

Execution protocol:
`WORKSTACK-CLI-AGENT-SKILL-ORACLE-GATE-EXECUTION-2026-09-02.md`

Headless worker directives:
`WORKSTACK-CLI-AGENT-SKILL-HEADLESS-WORKER-DIRECTIVES-2026-09-02.md`

Primary outcome: an agent can read one explicitly bound Task and append an idempotent,
human-visible Worklog checkpoint without editing SSOT files.

Scope: thin agent CLI and Codex Skill first; broader human CLI later.

## 1. Final decision

Proceed with CLI + Agent Skill, but deliver a deliberately narrow P0.

The first dogfood release reuses the current v3 Worklog mutation and API instead of introducing a
new journal store, new server routes, a new persistent receipt type, or a storage schema change.

The success criterion is:

> From a code repository with an explicitly selected local Work Stack authority, an agent can identify the
> correct workspace and Task, read bounded context, perform work, and append exactly one
> done/next/blockers checkpoint that immediately appears in Daily Review.

P0 does not make Work Stack a general agent platform. It establishes one trustworthy write path
whose value can be measured before expanding it.

## 2. Why this remains high ROI

The repository already contains:

- Task and Review domain operations;
- POST /api/v1/review/entries;
- canonical request digests and Idempotency-Key replay;
- atomic Worklog plus Activity/idempotency persistence;
- restart-safe replay tests;
- running-server metadata and loopback session/CSRF flow;
- Store leases and exclusive local writes;
- workspace UUID identity;
- an existing bounded agent apply command;
- local and SSH workspace profiles in the desktop application.

The missing product is mostly composition:

1. resolve one intended local authority without accidentally creating another Store;
2. use HTTP while the desktop server owns the lease;
3. use exclusive-local mode only when no server owns it;
4. expose a small deterministic agent command surface;
5. teach a Skill when to read, write and stop.

## 3. Defensive response to the adversarial review

The second hostile review argued that the previous plan was 60–70% larger than the first useful
release required. The review was not accepted wholesale.

### 3.1 Decision table

| Review claim | Decision | Defense and resulting change |
| --- | --- | --- |
| Dedicated durable receipt is overbuilt | Accept for P0 | Current add_worklog_v1 already commits an idempotency record with the Worklog and replays after restart. P0 retries the identical request with the same key once. A long-lived receipt query remains useful later, but not for immediate checkpoint recovery. |
| Enriched Worklog schema is unnecessary | Accept for P0 | Provenance would improve audit quality, but changing v3/v4 schemas and strict frontend readers is not required to accumulate useful journals. P0 keeps the existing entry shape. |
| v4 parity should not block first dogfood | Accept with guard | Released workspaces still use v3 and v4 mutation is opt-in. P0 explicitly refuses v4 with capability_not_enabled. Parity becomes a prerequisite for v4 activation, not for v3 dogfood. |
| Previous-version reader gate can be removed | Accept | P0 changes no authoritative storage shape. Restore this gate only when provenance/schema evolution is proposed. |
| Installer PATH management is low ROI | Accept for source P0 | Source dogfood uses an explicitly configured command. A packaged launcher is P0b; editing User PATH, rollback and PATH ownership remain later work. |
| Linux/XDG registry is unnecessary | Partially accept | A remote agent still needs a locator. P0 uses explicit --data-dir or a small machine-local repository locator rather than extracting the whole desktop registry or creating a new XDG registry. |
| Desktop SSH should leave P0 | Accept | Desktop-tunnel endpoint discovery touches shared desktop lifecycle code and is independent of local journaling. It becomes a later P0c slice. |
| Full Skill lifecycle is overbuilt | Accept | P0 provides one canonical Skill, manual/user-scope install instructions and validation. Automatic update, ownership receipts and remove commands follow dogfood. |
| AgentContext should not become a new platform contract | Partially accept | Agents still need stable JSON fields. P0 keeps a small CLI-v1 response and golden fixture, but no standalone AgentContext schema family or ResumePacket-like relationships. |
| New detailed CCN/platform gates are excessive | Accept | Existing structural gates remain. P0 adds focused tests only. The complete existing release matrix still runs for a public installer release, not for every dogfood iteration. |

### 3.2 What the review did not justify removing

The following remain mandatory:

- explicit workspace identity verification;
- no fallback from an unavailable running server to direct Store access;
- one stable idempotency key per logical checkpoint;
- exact same-key/different-body conflict behavior;
- bounded exact-field input;
- response-loss handling that never generates a fresh key automatically;
- no Task completion, deletion or relationship mutation in P0;
- human visibility through the existing Daily Review;
- Skill prohibition on raw SSOT writes.

These are inexpensive because the codebase already implements most of them, and removing them
would undermine the reason to build an agent interface.

### 3.3 Defensive response to the second review

The second review correctly found schedule waste, but two of its proposed shortcuts would weaken
authority safety or claim certainty the current schema cannot provide.

| Second-review claim | Decision | Defense and resulting change |
| --- | --- | --- |
| Defer all binding/locator work and use --data-dir | Mostly accept | P0 removes binding.json, local.json, Git-root discovery and agent bind. It requires explicit --data-dir plus an independent expected --workspace-uid guard. The UID guard is retained because a valid but wrong data directory can contain the same display Task IDs. Portable binding becomes P0b convenience. |
| Reduce the input/disclosure matrix | Accept | P0 tests exact fields, UTF-8 JSON, total size, item bounds and stdout secrecy. Control-character and JSON-looking-string cases collapse into one data-preservation test; prompt-surface tests move to Skill tests. |
| Treat v4 refusal as only an error-message improvement | Reject | The default-closed guard cited by the review belongs to workstack.storage.mutation_repository. P0's local path uses the legacy v3 Store. Passing a v4 authority to that adapter must be refused before Store construction; otherwise the v4 guard is never reached. Keep one preflight test and one refusal code, without repeating it across four gates. |
| Collapse numeric exits | Accept | Use success, ordinary command failure and usage failure. Machine behavior comes from error.code; commit_state appears only where a mutation was attempted. |
| Replace contracts/agent-cli-v1 files with test fixtures | Accept | One executable contract module plus golden tests is enough. No standalone schema family is created. |
| Resolve unknown by matching worklog list content | Reject | Existing Worklog entries have neither entry UID nor intent key. Identical entries are legal, so date/task/content matching cannot prove which logical intent committed or prove uniqueness. Same-key replay remains authoritative; list is diagnostic evidence only. |
| Make Phase 0 an executable stub | Accept | Merge the envelope/error/input skeleton first so all lanes import one contract instead of reinterpreting prose. |
| Remove installer work from source dogfood | Accept | Installed launcher work becomes P0b and is not on the P0 integration critical path. |
| Reduce transport estimate | Accept with reserve | Most mechanics exist in cli.py. Budget 1–1.5 days, while preserving focused response-loss and ownership tests. |
| Name the v3 lease implementation | Accept | Local mutation relies on workstack.store.Store.transaction and .workstack.lock through the existing transactional service path. It must not import workstack.storage.lease, which is independent v4 infrastructure. |

This revision therefore removes the expensive convenience layer while preserving independent
authority identity, pre-Store format admission, one-writer behavior and honest commit uncertainty.

## 4. P0 product scope

### 4.1 Commands

Only three new agent-facing workflows are required; the existing worklog list remains available
for human diagnosis:

    work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> status
    work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> context --task T-0001
    work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> checkpoint --intent-id <safe-id> --stdin
    work-stack --data-dir <existing-v3> worklog list --date 2026-09-02

Source dogfood uses an explicitly configured Python module or checkout launcher. Packaged launcher
and PATH integration are not P0 requirements.

### 4.2 Supported authorities

P0 supports:

- an existing local v3 authority selected with explicit --data-dir;
- an independent expected workspace UID supplied with the agent command;
- a running local desktop server for that same authority;
- an agent running on Windows or Linux against an explicitly supplied local path.

P0 does not support:

- v4 mutations;
- Windows CLI discovery of a desktop-owned SSH tunnel;
- starting SSH or a remote Work Stack server;
- offline remote mutation queues;
- active-profile fallback for an agent;
- repository binding/local locator discovery;
- implicit creation of a missing Store.

### 4.3 Skill scope

P0 provides:

- one canonical Codex SKILL.md;
- one short command reference;
- one conflict/journal policy reference;
- user-scope manual installation instructions;
- quick validation;

P0 does not provide:

- Skill update/remove commands;
- automatic updater integration;
- workspace-local Skill discovery claims that have not been verified;
- adapters for every agent product;
- scripts containing persistence or retry logic.

## 5. Minimal invariants

1. Agent commands require explicit --data-dir and --workspace-uid.
2. The actual workspace UID must match the independently supplied expected UID before any Task
   content is returned or mutation sent.
3. Storage format and authority identity are inspected before constructing the legacy Store.
4. Resolver failure must not create a directory or Store.
5. If valid server ownership metadata exists, reads and writes use the loopback server.
6. If server ownership metadata exists but the server is unavailable, fail; do not open Store
   directly.
7. Exclusive-local mode is allowed only when no server owns the authority and the writer lease is
   acquired.
8. The checkpoint uses the existing add_worklog_v1/POST review-entry contract.
9. The input is one bounded exact-field UTF-8 JSON object from stdin.
10. At least one of done, next or blockers must contain a non-empty item.
11. One logical checkpoint retains one Idempotency-Key across retry.
12. Same key plus same canonical request replays; same key plus another request conflicts.
13. Worklog and idempotency record commit atomically through the existing Store journal.
14. Response loss never causes a retry with a fresh key.
15. P0 does not mutate Task status, fields, relationships, Objective state or external systems.
16. The Skill never edits JSON/NDJSON/SQLite authority files.
17. The Skill records concise observable results, not raw prompts, hidden reasoning, credentials or
    environment dumps.
18. Checkpoints are meaningful milestones, blockers or handoffs rather than command-by-command
    spam.

## 6. Explicit authority admission

P0 separates location and expected identity without persisting a new binding format:

- --data-dir supplies the intended existing local authority;
- --workspace-uid supplies the independently expected canonical non-nil UUID;
- a repository AGENTS.md or user invocation may carry both arguments for dogfood;
- neither value is inferred from the active desktop profile or WORK_STACK_HOME.

Admission order:

1. require both arguments;
2. resolve the data path without creating it;
3. require an existing directory and recognizable Work Stack authority;
4. inspect format and workspace identity without constructing Store;
5. refuse v4 and unknown formats with capability_not_enabled or invalid_authority;
6. require actual UID equals expected UID;
7. only then construct the v3 Store or contact its declared loopback server owner.

The explicit UID is not redundant. A wrong but valid Work Stack directory can contain T-0001 just
as the intended workspace can. Comparing a UID read from that same wrong directory to itself would
not detect the selection error.

P0 intentionally does not implement Git-boundary traversal, binding.json, local.json, agent bind,
symlink policy or source-control ignore management. Those are cross-machine convenience features
admitted in P0b after the write path proves useful.

## 7. CLI contract

### 7.1 Output

New agent commands emit one UTF-8 JSON object plus LF on stdout.

Diagnostics go to stderr. Tracebacks do not appear on stdout.

Success:

    {
      "contract": "workstack.cli.v1",
      "data": {},
      "meta": {
        "command": "agent.checkpoint",
        "workspace_uid": "<uuid>",
        "task_id": "T-0001",
        "transport": "running-server | exclusive-local",
        "intent_id": "<id-or-null>",
        "replayed": false,
        "commit_state": "committed | unknown"
      }
    }

Failure:

    {
      "contract": "workstack.cli.v1",
      "error": {
        "code": "workspace_mismatch",
        "message": "The selected authority does not match this repository.",
        "details": {}
      },
      "meta": {
        "command": "agent.checkpoint"
      }
    }

Fields that do not apply to a command are omitted rather than filled with not_applicable. A failure
includes retryable only when the CLI can give a sound retry recommendation, and includes
commit_state only after a mutation attempt.

### 7.2 Exit categories

| Exit | Meaning |
| ---: | --- |
| 0 | success or idempotent replay |
| 1 | parsed command failed; inspect error.code |
| 2 | command-line usage/parser failure |

error.code carries detailed behavior. Commit uncertainty is error.code=commit_unknown with
commit_state=unknown, not another permanent numeric compatibility surface.

### 7.3 Static capabilities

    work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> status

P0 status combines only:

- installed CLI contract/version;
- resolved data directory availability, redacted in JSON by default;
- expected and actual workspace UID;
- storage format;
- sync/readiness state;
- running-server or exclusive-local availability;
- P0 capability supported/refused reason.

There is no separate broad capability catalog in P0.

## 8. Agent context

### 8.1 Input

    work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> context --task T-0001

### 8.2 Output data

    {
      "workspace_uid": "<uuid>",
      "task": {
        "id": "T-0001",
        "uid": "<uuid>",
        "revision": 4,
        "title": "...",
        "detail": "...",
        "status": "started",
        "priority": "P1",
        "due": null
      },
      "recent_worklog": [
        {
          "date": "2026-09-02",
          "done": ["..."],
          "next": ["..."],
          "blockers": []
        }
      ],
      "omitted": [
        "objectives",
        "relationships",
        "captures",
        "attachments",
        "work_sessions"
      ]
    }

Rules:

- response body maximum 32 KiB;
- at most five recent Task-linked Worklog entries;
- Task identity, core fields and current revision only;
- revision is context metadata, not an append-only checkpoint precondition;
- no relationship traversal;
- no source body, attachment, recipient or hidden page content;
- one consistent read when local; corresponding server projections when online;
- this is a CLI-v1 data shape with golden tests, not a separate ResumePacket family.

## 9. Agent checkpoint

### 9.1 Input packet

    {
      "task_id": "T-0001",
      "date": "2026-09-02",
      "done": ["Implemented the workspace preflight."],
      "next": ["Add response-loss coverage."],
      "blockers": []
    }

The packet intentionally mirrors the existing review-entry contract exactly. Authority identity is
the independent --workspace-uid command argument, so the HTTP body needs no transform.

Rules:

- body maximum 32 KiB;
- exact fields only;
- canonical Task display ID;
- date uses YYYY-MM-DD;
- each list maximum 20 items;
- each item maximum 1,000 characters after trimming;
- at least one non-empty item across the three lists;
- no summary, evidence, raw commands, changed-files roster, references or Task changes in P0.

### 9.2 Running-server path

1. Resolve the existing authority path and expected UID without creating Store.
2. Complete the bounded format and UID preflight without creating Store.
3. For admitted v3 only, construct Store to obtain its canonical runtime server-info path; do not
   enter a Store transaction on the running-server path.
4. Read and validate server metadata, then GET session and storage identity.
5. Require expected UID equals server UID.
6. POST the exact canonical review entry to /api/v1/review/entries.
7. Send exact Origin, X-WorkStack-CSRF, Content-Type and Idempotency-Key.
8. Validate the JSON response and workspace/request context.

No new server route or server mutation is added in P0.

### 9.3 Exclusive-local path

1. Confirm server ownership metadata is absent.
2. Validate the existing v3 authority.
3. Acquire the current Store writer lease.
4. Reread actual workspace UID under the lease.
5. Invoke WorkStack.add_worklog_v1 with the same canonical body and intent key.
6. Return the same semantic CLI envelope as the HTTP path.

If the selected storage format is v4, return capability_not_enabled. Do not fall back to a v3
writer.

### 9.4 Response-loss behavior

The current mutation already persists the Worklog and idempotency record in one transaction.

For a lost HTTP response:

1. keep the same canonical body;
2. keep the same Idempotency-Key;
3. reconnect once within a bounded timeout;
4. resend the exact request once;
5. if committed earlier, accept the existing replay response;
6. if not committed earlier, accept the new first commit;
7. if the server cannot establish either result, return commit_state=unknown.

Never:

- generate a new key automatically;
- infer success from matching Task values;
- switch to exclusive-local mode after a server transport failure;
- loop retries.

The existing worklog list may be shown as diagnostic evidence after an unknown result, but matching
date, Task and text cannot resolve the state authoritatively. Current entries contain no unique
entry ID or intent key, and two identical entries are valid. Only a replay of the same idempotency
key and canonical body proves the outcome.

### 9.5 Known P0 limitation

The existing Worklog entry does not distinguish agent and human authors and has no long-lived
intent lookup UI. The intent namespace may use agent:<run>:<checkpoint>, but this is operational
metadata, not visible provenance.

This limitation is accepted for dogfood because:

- the user asked first for automatic accumulation in one agreed location;
- the current Worklog is already human-visible;
- schema/provenance work can be based on actual retrieval and audit needs;
- no authoritative storage compatibility is changed prematurely.

## 10. Skill contract

### 10.1 Layout

Canonical source:

    integrations/agent-skill/work-stack/
      SKILL.md
      references/
        commands.md
        journal-policy.md

Optional agents/openai.yaml is added only if clean-profile validation proves useful.

### 10.2 Progressive disclosure

SKILL.md contains:

- when to use Work Stack;
- the four-command workflow;
- exact stop conditions;
- prohibition on SSOT file edits;
- checkpoint timing rules;
- links to the two references.

commands.md contains exact JSON examples and error handling.

journal-policy.md contains what may and may not be written.

No Skill script implements HTTP, filesystem persistence, SSH, idempotency or retry.

### 10.3 Workflow

At start:

1. run agent status;
2. stop on missing explicit authority, identity mismatch, unsupported format, sync/recovery issue or
   unavailable owner;
3. find/confirm one Task;
4. run agent context.

During work:

- use Work Stack only for the Task the user selected;
- checkpoint at a meaningful milestone or blocker;
- report observable facts;
- keep one intent ID per logical checkpoint.

At exit:

- append one final done/next/blockers checkpoint;
- show whether the result was first commit, replay or unknown;
- do not mark the Task complete;
- stop and report any unknown commit state.

### 10.4 Confirmation and authority

| Operation | P0 Skill |
| --- | --- |
| status/context/worklog read | allowed |
| append bounded checkpoint for selected Task | allowed |
| create Task | unavailable |
| patch/start/complete/drop/delete Task | unavailable |
| change relations/Objectives | unavailable |
| sync adoption/rebind/migration/restore | unavailable |
| send reply/external message | unavailable |

### 10.5 Installation

P0 validates the canonical Skill using the pinned repository-owned
`quality/agent-p0-oracle/validate_skill.py` validator and documents a user-scope manual
installation. Unpinned user-profile validators are advisory only.

For source dogfood, installation records an explicit command prefix for the checkout, such as a
verified Python-module invocation. The canonical Skill template embeds neither a user path nor a
workspace path. Packaged Windows launcher integration is P0b.

Skill automatic update, modified-tree ownership handling and uninstall are deferred.

## 11. Target implementation boundary

P0 should add a few cohesive modules rather than a new framework:

    workstack/
      agent_cli_contract.py
      agent_authority.py
      agent_local_backend.py
      agent_transport.py
      agent_command_status.py
      agent_command_context.py
      agent_command_checkpoint.py
      agent_commands.py
      agent_runtime.py
      cli.py

    integrations/
      agent-skill/work-stack/**

Responsibilities:

- agent_cli_contract.py: envelopes, errors, limits and JSON parsing;
- agent_authority.py: explicit authority/UID validation and read-only v3 preflight;
- agent_local_backend.py: admitted v3 Store/WorkStack context/checkpoint backend and existing
  transaction path;
- agent_transport.py: running-server context/checkpoint backend, session/CSRF, HTTP request and one
  bounded replay;
- agent_command_status.py: pure typed status handler with no output I/O;
- agent_command_context.py: pure typed bounded-context handler with no output I/O;
- agent_command_checkpoint.py: pure typed checkpoint handler with no output I/O;
- agent_commands.py: integration-owner static registry only;
- agent_runtime.py: integration-owner composition, frozen rendering and the sole stdout/stderr
  writer for new agent commands;
- cli.py: thin parser registration and legacy delegation only.

Existing server.py, service.py, storage schemas, desktop registry/SSH and frontend do not change in
P0.

## 12. Parallel implementation plan

### Phase 0 — executable contract and trusted Oracle seed, 1–1.5 elapsed days

O1 first creates the trusted runner/directives/schema, M0 freezes exact public export names and
signatures, and Q0 classifies every planned module in the existing structural quality gate. After
the M0 interface freeze, O2 and the explicit pre-G10 conformance packet T0 implement the executable
contract and its independent test in parallel. The Oracle executes from a separately pinned trusted
checkout, so a candidate cannot weaken its own judge. G10 freezes the executable contract digest,
interface manifest digest, directive digests, quality topology and Oracle seed SHA before
production lanes start.

The skeleton contains:

- the three new command identifiers;
- one success and one failure builder;
- the compact error-code catalog and exit mapping;
- immutable admission/context/mutation values and the shared backend protocol;
- the exact checkpoint parser, canonical bytes, renderer and limits;
- deterministic golden context/checkpoint inputs and outputs for tests;
- response-loss and refusal constants.

It must be importable and executable, not merely a prose freeze. Test fixtures are generated or
asserted from these builders. There is no separate contracts/agent-cli-v1 schema directory.

Idle headless capacity in Phase 0 runs disjoint read-only packets for HTTP characterization,
Store/v3/v4 construction side effects, review/context projection behavior and adversarial contract
review. Those evidence packets cannot approve G10; O1/O2 must reproduce their findings. Production
implementation remains forbidden until the single G10 contract/Oracle receipt
is issued; otherwise one contract correction invalidates a large fan-out of speculative code.

### Phase 1 — elastic disjoint production packets

#### Lane A: running-server backend

Owned files:

- agent_transport.py only.

Work:

- implement the new backend from characterized current server-coordinate/request behavior without
  editing or moving the legacy helpers;
- reuse current session/CSRF behavior;
- implement review-entry POST;
- implement one same-key/same-body response-loss replay;
- classify unavailable versus commit-unknown;
- never perform direct fallback after online failure.

Acceptance:

- valid running server;
- stale server metadata;
- wrong workspace UID;
- response lost before/after commit;
- same key/different body;
- token/session data absent from output.

#### Lane B1: authority admission

Owned files:

- agent_authority.py only.

Work:

- require explicit --data-dir and --workspace-uid;
- inspect existing workspace identity without creating Store;
- detect v3 versus v4;
- return immutable admitted authority without importing Store.

Acceptance:

- resolver failure creates no files/directories;
- expected/actual UID mismatch;
- v4 is refused before legacy Store construction;
- a valid explicit v3 authority is admitted.

#### Lane B2: exclusive-local backend

Owned files:

- agent_local_backend.py only.

Work:

- consume only the frozen AuthorityAdmission contract;
- after admission, reuse Store/WorkStack and current add_worklog_v1 transaction;
- reread UID inside the transaction;
- never import v4 storage.lease.

Acceptance:

- local replay/conflict semantics match the current service;
- workstack.storage.lease is never imported.

#### Lanes C1/C2/C3: pure command handlers

Owned files:

- C1: agent_command_status.py only;
- C2: agent_command_context.py only;
- C3: agent_command_checkpoint.py only.

Work:

- one handler per packet: status, bounded context or checkpoint;
- stable typed outcomes and error mapping through the frozen contract;
- concrete backends and filesystem are available only as injected protocols.

Acceptance:

- typed-outcome fixtures and contract-renderer JSON goldens;
- byte/item bounds;
- empty journal rejection;
- no handler-level stdout/stderr calls;
- online/local semantic parity with fakes.

#### Lane D: Skill and dogfood documentation

Owned files:

- integrations/agent-skill/work-stack/**;
- docs/WORKSTACK-AGENT-SOURCE-DOGFOOD.md.

Work:

- thin SKILL.md;
- command reference;
- journal/stop policy;
- quick validation;
- static check for direct-file/high-risk instructions;
- manual user-scope install guide.

Acceptance:

- progressive disclosure;
- no secrets/paths embedded in canonical template;
- no SSOT edit instructions;
- all examples are checked against the executable contract.

A, B1, B2, C1, C2, C3 and D may all start from the same G10 receipt in separate worktrees.

At the same time, independent TA, TB1, TB2, TC1, TC2, TC3 and TD workers write only the matching
contract tests. TE writes the black-box CLI E2E contract test. A test worker starts from G10 and
cannot inspect or edit its paired implementation candidate; an implementation worker cannot edit
the conformance tests. The trusted Oracle tests each ephemeral implementation + conformance
composition and records both commit identities in the receipt.

This yields up to fifteen useful post-G10 authoring packets without creating competing production
implementations. The orchestrator uses an adaptive author pool and separate light/heavy test
semaphores rather than a fixed agent count. Additional agents perform read-only review or Oracle
mutation work instead of producing competing implementations.

OpenCode headless workers and Codex rescue workers receive the same self-contained
packet/worktree/base/contract boundaries and cannot push, merge, rebase or coordinate through
shared mutable files. GLM/OpenCode is the default bulk authoring and characterization pool. Codex is
the bounded rescue pool for contract reconciliation, cross-candidate audit, code-versus-environment
failure diagnosis and a critical-path worker that materially exceeds its peer median. Engine choice
never changes ownership or admission: every natural-language report is nonauthoritative and only a
supervisor-recomputed Oracle receipt admits a candidate. A rescue worker reviews or replaces an
expired attempt; it never authors a competing candidate against a still-admissible attempt.

### Phase 2 — rolling admission and integration-owner wiring

Only the integration owner modifies shared files, using the separately specified Oracle Gate
protocol:

1. compose the explicit C1/C2/C3 static registry in agent_commands.py;
2. compose A, B1 and B2 with that registry in agent_runtime.py;
3. prepare registry and runtime commits concurrently when their dependency receipts allow;
4. give one owner the final cli.py hook, leaving legacy worklog list and agent apply intact;
5. route new agent commands through read-only authority preflight before Store construction; for an
   admitted v3 authority, construct Store before its canonical owner probe while retaining all
   existing legacy parser/behavior;
6. run focused integration tests;
7. run source-checkout smoke;
8. perform one real dogfood checkpoint.

Receipted disjoint commits are admitted as they finish. The integration owner never repairs a lane
by editing its files; seam failures return to that owner. No worker in this phase edits server.py,
service.py, store.py, storage schemas, frontend, desktop SSH or updater behavior.

### Phase 3 — dogfood release gate

Blocking tests:

- existing CLI characterization;
- existing review-entry idempotency/restart tests;
- focused agent contract/workspace/transport tests;
- running-server and stopped-server checkpoint E2E;
- wrong-workspace and server-owner refusal;
- same-key replay and same-key/different-body conflict;
- response-loss bounded retry and commit-unknown;
- canonical Skill quick validation;
- existing structural architecture/CCN gate;

Not blocking dogfood:

- v4 mutation parity;
- desktop SSH;
- Linux registry;
- Skill lifecycle;
- PATH integration;
- frontend provenance UI;
- full public release/browser matrix.
- installed Windows launcher.

For an actual public installer release, all existing release gates, candidate-SHA rules, checksum,
updater and public-export audits still apply.

## 13. Test matrix

### 13.1 Identity and selection

- explicit --data-dir plus independent --workspace-uid identify the expected workspace;
- expected/actual UID mismatch fails before Task content is returned;
- missing path does not create a Store;
- v4 is refused before legacy Store construction;
- active desktop profile is never used implicitly.

### 13.2 Transport and lease

- desktop server running uses HTTP;
- server absent uses exclusive-local v3;
- metadata present but server dead fails without direct fallback;
- server identity mismatch fails;
- server start/local lease race has one winner;
- local and online return the same CLI semantic data.

### 13.3 Idempotency and response loss

- same key/body creates exactly one entry;
- same key/body after restart replays;
- same key/different body conflicts;
- first response lost after commit, replay returns existing result;
- first request lost before commit, retry commits once;
- server unavailable for retry returns unknown;
- no fresh intent ID is generated;
- no retry loop.

### 13.4 Input and disclosure

- exact fields only;
- valid UTF-8 JSON and 32 KiB total bound;
- list count and item length;
- one representative string-preservation case;
- no raw prompt/environment/token in examples or output;

### 13.5 Skill

- Skill validates;
- references resolve;
- examples are fixture-derived;
- direct Store/JSON/NDJSON/SQLite edits are absent;
- completion/delete/send/rebind commands are absent;
- unknown commit tells the agent to stop.

## 14. Existing code to reuse

| Need | Existing basis |
| --- | --- |
| Worklog mutation | WorkStack.add_worklog_v1 |
| Idempotent replay | _idempotency_replay and Activity record |
| Atomic persistence | documents.save_many and Store recovery journal |
| Running server coordinates | current agent apply/capture CLI helpers |
| Session/CSRF | GET /api/v1/session and current agent apply |
| Workspace identity | GET /api/v1/storage and workspace.json |
| Task detail | GET /api/v1/tasks/{task_id} / WorkStack.get_task |
| Review projection | GET /api/v1/review / review_projection |
| Exclusive writer | workstack.store.Store.transaction / .workstack.lock; not storage.lease |
| Skill validation | pinned repository-owned `quality/agent-p0-oracle/validate_skill.py` |

## 15. Deferred backlog with admission triggers

### 15.1 Portable repository binding

Admit after source dogfood proves the explicit path workflow and repeated path/UID entry is the
measured friction.

Then consider:

- commit-safe binding.json containing only workspace UID;
- ignored machine-local locator containing the authority path;
- Git worktree boundary and nested-repository policy;
- agent bind/unbind helpers;
- source-control ignore and symlink/reparse behavior;
- multi-machine Windows/Linux validation.

These features must preserve the explicit UID guard and must never create a missing authority.

### 15.2 Provenance and durable receipt

Defer:

- actor/provenance in Worklog;
- entry UID;
- long-lived intent lookup route;
- receipt UI.

Admit when:

- users need to distinguish human and agent entries;
- replay history needs outlive current idempotency retention;
- more than one external client consumes journals;
- compliance/audit requirements emerge.

Then restore:

- explicit v3/v4 schema design;
- previous-reader compatibility;
- strict frontend projection;
- authoritative receipt retention policy.

### 15.3 v4 parity

Admit before:

- v4 becomes a released writable authority;
- a dogfood workspace actually activates v4.

The v4 implementation must then use its command backends and explicit schema rather than P0's v3
adapter.

### 15.4 Desktop SSH

Admit when:

- Windows agents need to target remote SSOT while the desktop owns the tunnel.

Required work:

- content-free live endpoint descriptor;
- desktop publish/update/clear lifecycle;
- CLI health/protocol/workspace revalidation;
- reconnect/port/stale descriptor tests.

One-shot SSH and offline queues remain separate decisions.

### 15.5 Skill lifecycle

Admit after:

- manual installation/update becomes recurring friction;
- more than one user receives the Skill.

Then add:

- managed manifest/digest;
- unchanged-tree update;
- modified-tree refusal and remediation;
- uninstall ownership.

### 15.6 Human CLI and PATH

Admit when:

- users begin invoking the CLI directly outside agent workflows.

Then add:

- explicit Windows bin/work-stack.cmd launcher and installed-runtime smoke;
- User PATH opt-in and ownership receipt;
- text renderer and completion;
- Task/session/Objective commands;
- packaged Linux entrypoint.

## 16. Complexity and release policy

Use the existing structural quality gate. Do not create new release-blocking CCN thresholds solely
for P0.

Design guidance:

- new orchestration functions should remain small enough to review;
- no new monolithic cli.py branch tree;
- parser changes are thin;
- transport, workspace resolution and command logic remain separated;
- existing critical CCN violations remain prohibited by the current gate.

Dogfood and public release are distinct:

- dogfood uses focused new tests plus existing structural gates;
- public release uses the full existing candidate-SHA, installer, checksum, updater, export and
  platform matrix.

## 17. Estimates

### 17.1 P0 source dogfood

| Work | Engineer-days |
| --- | ---: |
| trusted Oracle seed + interface manifest + quality topology + executable contract | 1.75–2.75 |
| running-server lane | 1–1.5 |
| authority B1 + local B2 | 1.25–1.75 |
| status/context/checkpoint C1–C3 | 1.25–2 |
| Skill/docs lane | 0.75–1 |
| registry/runtime/CLI wiring | 0.75–1.25 |
| focused E2E/dogfood gate | 0.5–1 |
| total | 7.25–12.25 (plan as 7.5–12.5) |

With elastic headless workers, independent conformance authors and separate
author/light-test/heavy-test pools:

- 4–6 working days is realistic;
- 3–4.25 days is possible if M0/G10 land cleanly and existing review/API
  characterization requires no repair.

The elastic plan spends slightly more aggregate engineer time on extra packet boundaries and
receipts in exchange for lower wall time. More than seven simultaneous production implementers has
negative ROI, but seven independently owned conformance suites and one E2E suite remain useful
parallel work. Capacity beyond those fifteen post-G10 packets is assigned to Oracle mutants,
receipt audit and read-only investigation.

### 17.2 P0b installed dogfood addition

- explicit Windows launcher/bundle smoke: +1–2 engineer-days;
- no PATH management.

### 17.3 Deferred additions

| Capability | Additional effort |
| --- | ---: |
| portable binding/local locator | 1.5–2.5 days |
| provenance + durable receipt + v3/v4 schema | 7–11 days |
| v4 mutation parity without provenance schema | 4–6 days |
| Desktop SSH endpoint discovery | 4–6 days |
| Skill lifecycle | 2–4 days |
| PATH + broader human CLI | 5–8 days |

## 18. Definition of done

P0 is complete when:

1. the canonical Codex Skill validates and is manually installable in the verified user-scope
   location;
2. explicit data-dir plus expected UID admit the intended existing v3 authority without creating
   any path;
3. agent status refuses missing, mismatched, unsupported-format or unsafe authority;
4. agent context returns one Task and at most five recent entries within 32 KiB;
5. agent checkpoint appends done/next/blockers through the existing idempotent Worklog path;
6. the same intent never creates two entries across retry or restart;
7. same intent with a different request is rejected;
8. response loss reuses the same request once or reports unknown;
9. running desktop uses HTTP and stopped desktop uses exclusive-local mode;
10. an unavailable server owner never falls back to direct Store;
11. the checkpoint appears in existing Daily Review and worklog list;
12. no P0 command can change Task or external state beyond appending the journal;
13. focused tests and the existing structural gate pass;
14. one real source-checkout dogfood session succeeds.

Installed P0b is complete when the same workflow additionally passes through the explicit packaged
Windows launcher. Public distribution remains subject to the full existing release checklist.

## 19. Recommended execution order

1. Merge the executable contract skeleton for three new commands, exact checkpoint body and error
   envelope.
2. Launch A, B1, B2, C1, C2, C3, D plus TA, TB1, TB2, TC1, TC2, TC3, TD and TE from the same G10
   receipt in isolated worktrees.
3. Pair each implementation with its independent conformance commit and admit only Oracle receipts
   that bind both identities.
4. Merge through one integration owner.
5. Run source-checkout dogfood.
6. Dogfood for several real Tasks.
7. Measure whether portable binding, installed launcher, provenance, long-lived receipt, v4, SSH or
   Skill lifecycle is the next actual
   bottleneck.

Go/no-go:

> Can the agent, using an explicit existing v3 path and independently expected workspace UID, leave one idempotent
> done/next/blockers entry in the correct Daily Review without any direct SSOT edit?

If yes, the primary ROI has been delivered.
