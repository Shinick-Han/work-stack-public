# Work Stack CLI + Agent Skill Oracle Gate execution protocol

Date: 2026-09-02

Status: launch-preparation protocol; G10 artifacts do not exist yet

Companion plan: `WORKSTACK-CLI-AGENT-SKILL-IMPLEMENTATION-PLAN-2026-09-02.md`

Authoritative headless adapter and packet directives:
`WORKSTACK-CLI-AGENT-SKILL-HEADLESS-WORKER-DIRECTIVES-2026-09-02.md`

## 1. Purpose

This protocol allows multiple workers to implement the P0 without relying on any worker to
remember, interpret or voluntarily follow architectural instructions.

The control mechanism is not the prompt. It is a sequence of executable admission gates:

> A worker receives frozen inputs, owns a disjoint file set, and produces a commit. A trusted gate
> independently recomputes the diff, contract digest and tests. Only a passing commit becomes an
> input to another worker or the integrator.

Explanations are useful for development, but have no merge authority. A persuasive report with no
passing receipt is rejected. A passing implementation whose report is poor can still be inspected
and merged.

## 2. Oracle properties

Every blocking gate must be:

- deterministic: fixed fixtures, fixed limits, fixed locale/timezone where relevant;
- executable: pass/fail comes from a process exit and structured report;
- independently recomputed: workers cannot submit a self-authored PASS;
- content-addressed: packet, contract, base and candidate commits are hashed;
- fail-closed: missing evidence, skipped tests, new files or contract drift fail;
- bounded: no network, SSH, browser, installer, wall-clock dates or user profile state in P0 gates;
- ownership-aware: changed paths outside the lane allowlist fail before tests run;
- mutation-aware: an invalid request must leave a byte-identical authority tree;
- composition-aware: unit success cannot substitute for the final running/stopped integration gate.

## 3. Trust boundaries

There are five roles. A person or agent may hold only the roles assigned for one wave.

### 3.1 Oracle owner

Owns only:

- `quality/agent-p0-oracle/**` except the separately owned `manifest.v1.json`, including independent
  golden files, probes, directive files, schemas and bad mutants;
- `scripts/run_agent_p0_gates.py`;
- `tests/oracle/agent_p0/**`;
- task packet schemas and receipt verification.

The Oracle owner does not edit production modules. The frozen Oracle seed is executed from a
separate trusted checkout or CI action pinned to `oracle_seed_sha`; the candidate cannot replace
the judge by editing a path in its own tree.

### 3.2 Production lane worker

Owns only the production paths in one task packet. It may not edit conformance or trusted Oracle
tests.

### 3.3 Conformance lane worker

Owns only the test paths in one task packet. It starts from G10 except for the explicit T0
post-M0/pre-G10 bootstrap packet, is not supplied its paired production candidate, and may not edit
product or Oracle code. A receipt claims enforced blindness only when an OS sandbox prevents access
to sibling/parent paths; process separation alone is recorded as best-effort dogfood isolation.

### 3.4 Integration owner

Owns shared composition files and cherry-picks only commits with valid lane receipts. The
integration owner may not waive a failed gate. A contract change returns to Gate G0 and invalidates
all later receipts.

### 3.5 Release owner

Runs public-release gates after source dogfood. Installer, updater and public artifact work are P0b
and are not allowed to delay P0 source integration.

## 4. Clean worktree and commit model

Workers never edit a shared working tree.

For each packet, the orchestrator creates a dedicated Git worktree from the packet's exact
`base_sha`. An authoring worker returns exactly one candidate commit whose sole parent is
`base_sha`, never a loose patch or commit range against a dirty tree.

Required preconditions:

1. `git status --porcelain` is empty;
2. `git rev-parse HEAD` equals `base_sha`;
3. the frozen contract digest equals `contract_sha256`;
4. no untracked file exists outside the packet allowlist;
5. the worker does not merge or rebase independently;
6. the integrator cherry-picks in the declared DAG order.

If upstream changes after dispatch, the packet expires. The orchestrator issues a new packet rather
than asking the worker to improvise a rebase.

## 5. Machine-readable task packet

Each lane receives an immutable packet from the pinned Oracle checkout or a supervisor-owned run
directory outside every candidate checkout. Committing generated packets into a candidate branch
is forbidden. The logical schema is:

```json
{
  "packet_version": 1,
  "packet_kind": "authoring",
  "role": "production",
  "packet_id": "agent-p0-lane-a-transport",
  "base_sha": "<40-hex>",
  "oracle_seed_sha": "<40-hex>",
  "contract_sha256": "<64-hex>",
  "interface_manifest_sha256": "<64-hex>",
  "worker_directive_sha256": "<64-hex>",
  "lane": "transport",
  "owned_paths": ["workstack/agent_transport.py"],
  "required_outputs": ["workstack/agent_transport.py"],
  "declared_context_paths": ["workstack/agent_cli_contract.py", "workstack/cli.py", "workstack/server.py", "workstack/service.py"],
  "forbidden_paths": ["workstack/store.py", "workstack/storage/**", "frontend/**", "desktop/**", "quality/agent-p0-oracle/**"],
  "allowed_change_types": ["add"],
  "required_exports": ["<names frozen by manifest.v1.json>"],
  "required_gates": ["G00", "G10", "G21-A"],
  "forbidden_imports": ["workstack.store", "workstack.service", "workstack.storage", "subprocess"],
  "forbidden_calls": ["os.system", "subprocess.*"],
  "dependency_receipts": ["agent-p0-g10"],
  "worker_resource_class": "author",
  "gate_resource_class": "light-test",
  "timeout_seconds": 1800
}
```

`lane` is the only optional field and is informational until M0 supplies the ownership map. All
other fields above are required and unknown fields are rejected. This is the same schema shown in
the headless-worker directives; generated packets validate against the pinned Oracle schema before
dispatch.

Each production packet has a separately issued conformance packet. For example, TA owns
`tests/test_agent_transport_contract.py`, depends on G10 rather than A, and cannot read or edit A's
candidate branch. G21-A later evaluates an ephemeral composition of the independently produced A
and TA commits.

The gate runner rejects:

- unknown packet fields;
- missing or noncanonical hashes;
- overlapping production ownership between active packets;
- a candidate whose changed paths exceed `owned_paths`;
- a candidate based on another commit;
- a dependency receipt whose candidate/contract digest no longer matches.

The runner derives a lane from `git diff --raw -z --find-renames base_sha...candidate_sha`, including
status, mode and both rename paths. The derived lane must be unique and equal the packet lane. A
label or worker claim never decides ownership. Symlink mode `120000`, submodule mode `160000`,
case-fold path collisions and Oracle/workflow edits are rejected.

### 5.1 Worker state machine

Only these transitions exist:

```text
UNISSUED -> STARTED -> CANDIDATE -> RECEIPTED -> ADMITTED -> MERGED
                         |             |
                         +-----------> REJECTED

any discovered contract gap -> CONTRACT_CHANGE_REQUEST -> STOP
```

A worker cannot self-promote a state. STARTED is an Oracle-issued ticket; RECEIPTED and ADMITTED
are trusted-runner results. A contract-gap response contains the requested symbol and failing
fixture but no implementation edits. If the Oracle owner changes the contract, every dependent
ticket and receipt is invalidated and reissued from the new G10 SHA.

## 6. Frozen executable contract

Phase 0 produces one executable contract, not a large family of JSON schemas.

### 6.1 Frozen production artifact

`workstack/agent_cli_contract.py` defines only:

- three new command identifiers: status, context and checkpoint;
- immutable `StatusRequest`, `ContextRequest`, `CheckpointRequest`, `AgentOutcome`,
  `AuthorityAdmission`, `RuntimeDependencies` and `ServerCoordinates` values;
- `AgentBackend`, `JsonRequester` and `StoreFactory` protocols used at the running-server,
  exclusive-local and injected-I/O seams;
- the exact checkpoint parser, canonical UTF-8 request bytes and bounds;
- success/error envelope builders;
- compact one-object-plus-LF renderer;
- the compact error-code catalog and exit mapping;
- response-loss constants.

It imports neither `Store`, `WorkStack`, `http.client` nor filesystem mutation helpers.

### 6.2 Oracle declaration

`quality/agent-p0-oracle/manifest.v1.json` records the values the trusted runner must assert,
including exact public export names and callable signatures used by blind conformance workers:

- CLI contract string;
- command identifiers;
- input field set and byte/item limits;
- required/optional envelope fields;
- stable error codes;
- forbidden production paths;
- lane ownership map;
- required sentinel names.

This file is a test/gate declaration, not a new public storage schema.

The trusted seed layout is:

```text
quality/agent-p0-oracle/
  manifest.v1.json
  golden/*.jsonl
  probes/*.py
  mutants/*.py
scripts/run_agent_p0_gates.py
tests/oracle/agent_p0/**
```

### 6.3 Gate G10

G10 passes only when:

1. the contract module imports in isolation;
2. all Oracle-declared constants match;
3. exact-field and bound tests pass;
4. success/failure builders emit one deterministic JSON object;
5. no forbidden import exists;
6. golden output is generated through the builders, not copied into production code;
7. the contract SHA-256 is written into the trusted receipt.

After G10, downstream workers import the module. They may not copy or redefine its constants.

### 6.4 Decisions that G10 must encode as bytes

G10 fails if any of these remains prose for a lane to decide:

- compact sorted-key UTF-8 JSON, exactly one object and one trailing LF;
- final serialized envelope, not merely `data`, is bounded to 32 KiB;
- exit 0 success/replay, exit 1 parsed-command failure, exit 2 parser usage;
- retryable and commit_state appear only in the states defined by the contract;
- POST attempt maximum is two and both attempts reuse pre-serialized bytes and the same key;
- session/storage/read failures are not automatically retried;
- `CheckpointRequest` contains `task_id`, `date`, `done`, `next`, `blockers` and the separately
  supplied `intent_id`; no external `workspace_uid` enters the existing API body;
- expected workspace UID is checked against the current storage `workspace_id` before content is
  returned or a mutation is sent;
- date comes from an injected clock;
- exact recent Worklog entries are selected by date descending, original within-day order, same Task,
  at most five, within a bounded 31-day lookback;
- HTTP context must inspect daily entries rather than reconstruct them from the deduplicating weekly
  projection;
- HTTP context performs exactly 31 bounded daily-review GETs, newest date first through today minus
  30 days, so overflow can be decided without relying on the weekly projection;
- overflow removes oldest Worklog entries first and reports `recent_worklog_overflow`;
- a Task core projection that alone exceeds the envelope bound returns context_too_large;
- Task detail is rebuilt from an allowlist and never forwards capture/reply/activity extras;
- unexpected exceptions produce a content-free internal_error envelope;
- paths, CSRF values, tokens and raw server bodies never enter error details; successful status data
  reports path availability as a boolean and never emits the resolved absolute path;
- existing `agent apply` is not registered in or modified by the new P0 registry.

## 7. Production ownership map

| Owner | Exclusive production files | Allowed read-only dependencies | Forbidden production edits |
| --- | --- | --- | --- |
| M0 — interface manifest | `quality/agent-p0-oracle/manifest.v1.json` | written decisions and Oracle schema | all product/test modules and quality thresholds |
| Q0 — quality topology | `quality/quality-config.json`, `quality/structural-baseline.json` | M0 and current structural gate | all product/test modules and Oracle behavior |
| Contract | `workstack/agent_cli_contract.py` | current CLI/API behavior | Store, service, server, storage, frontend |
| Lane A — running server | `workstack/agent_transport.py` | contract, current CLI transport helpers, server/API | CLI, server, service, Store, local fallback, storage, frontend |
| Lane B1 — authority | `workstack/agent_authority.py` | contract, storage validation/read admission | CLI, Store, WorkStack, server, mutation modules |
| Lane B2 — local backend | `workstack/agent_local_backend.py` | contract, Store/WorkStack current v3 behavior | CLI, server, storage.lease, storage mutation modules |
| Lane C1 — status | `workstack/agent_command_status.py` | contract and injected admission/backend state | CLI, HTTP, Store, WorkStack, filesystem, frontend |
| Lane C2 — context | `workstack/agent_command_context.py` | contract and injected AgentBackend | CLI, HTTP, Store, WorkStack, filesystem, frontend |
| Lane C3 — checkpoint | `workstack/agent_command_checkpoint.py` | contract and injected AgentBackend | CLI, HTTP, Store, WorkStack, filesystem, frontend |
| Lane D — Skill | `integrations/agent-skill/work-stack/**`, `docs/WORKSTACK-AGENT-SOURCE-DOGFOOD.md` | frozen contract and examples | all Python product code, SSOT files, installer |
| Integrator | `workstack/agent_commands.py` static registry, `workstack/agent_runtime.py`, thin edits to `workstack/cli.py` | all accepted lanes and conformance tests | server, service, store, storage schemas, frontend, desktop, installer, conformance tests |
| Oracle | gate config/runner and `tests/oracle/agent_p0/**` | all product code read-only | all product modules |

Conformance tests have independent, disjoint owners:

- T0: `tests/test_agent_cli_contract.py`;
- TA: `tests/test_agent_transport_contract.py`;
- TB1: `tests/test_agent_authority_contract.py`;
- TB2: `tests/test_agent_local_backend_contract.py`;
- TC1: `tests/test_agent_command_status_contract.py`;
- TC2: `tests/test_agent_command_context_contract.py`;
- TC3: `tests/test_agent_command_checkpoint_contract.py`;
- TD: `tests/test_agent_skill_contract.py`;
- TE: `tests/test_agent_cli_e2e_contract.py`.

Implementation workers cannot edit those files, and conformance workers cannot edit product code.
Their packets share only the frozen contract digest. This is a two-key gate: neither a plausible
implementation nor a plausible test suite is sufficient by itself.

Only the Oracle owner edits `tests/oracle/agent_p0/**`.

## 8. Composition seam

The integrator creates `workstack/agent_runtime.py` as the only composition root.

It:

1. registers new subcommands into the existing `agent` parser;
2. reads `--data-dir` and `--workspace-uid`;
3. calls authority admission before any `Store` construction;
4. for an admitted v3 authority only, constructs Store and obtains its canonical runtime/server-info
   path instead of duplicating runtime-root calculation;
5. selects running-server when owner metadata exists, otherwise exclusive-local;
6. invokes command orchestration;
7. renders the typed handler outcome and performs the sole stdout/stderr write;
8. maps a parsed agent command before legacy `main()` constructs its default `Store`.

This order is a material safety requirement, not style. The current legacy `main()` constructs
`Store(...)` before its existing `agent apply` dispatch, and `Store.__init__` can create authority
and runtime directories. The integration edit must early-dispatch only the three new P0 actions;
existing `agent apply` remains on its characterized legacy path.

The edit to `workstack/cli.py` is deliberately thin: import, parser registration and early dispatch.
No lane worker edits it. This avoids a shared-file merge queue.

## 9. Elastic headless-worker execution DAG

OpenCode headless workers remove the fixed four-slot assumption. Concurrency is limited by
disjoint ownership and machine resources, not by a hardcoded agent count.

```text
G00 baseline
   |
   +-- O1 Oracle runner/directives/schema
   +-- M0 public interface manifest
   +-- Q0 quality configuration/baseline
   +-- X1 HTTP characterization
   +-- X2 Store/v3/v4 side-effect characterization
   +-- X3 review/context projection characterization
   +-- R0 read-only adversarial consistency review
                     |
             M0 interface freeze
                     |
              O2 contract + T0 conformance
                     |
                  G10 freeze
                     |
   +-- implementation: A B1 B2 C1 C2 C3 D
   +-- conformance:    TA TB1 TB2 TC1 TC2 TC3 TD TE
   |          (all fifteen packets start independently)
   |
   +-- pairwise ephemeral Oracle composition
       A+TA  B1+TB1  B2+TB2  C1+TC1  C2+TC2  C3+TC3  D+TD
        |       |       |       |       |       |       |
      G21-A  G21-B1  G21-B2  G21-C1  G21-C2  G21-C3  G21-D
        +-------+-------+-------+-------+-------+-------+
                     |
             rolling admission
                     |
       I1 static registry + I2 runtime composition
                     |
             I3 thin cli.py hook
                     |
               G30 E2E -> G40
```

O/X/R packets are read-only or Oracle-owned and may run concurrently. Production packets A, B1,
B2, C1, C2, C3 and D and their seven conformance counterparts all start from the same G10 receipt
and own disjoint files. A conformance worker is not given its paired implementation candidate; the
receipt records whether that blindness was sandbox-enforced or best-effort. B2
consumes the contract's immutable AuthorityAdmission type, not B1's concrete implementation, so
the two can be implemented concurrently and joined only at integration.

I1 and I2 may be prepared concurrently once their respective receipts exist. I3 remains a single
owner because `cli.py` is the true shared composition boundary.

The maximum useful post-G10 authoring concurrency is therefore about fifteen workers: seven
production implementations, seven paired conformance suites and one independent black-box CLI E2E
suite. Spawning
competing implementations of the same packet still creates duplicate solutions and comparison
overhead; surplus agents should audit receipts, exercise bad mutants or investigate a rejected
invariant instead.

Expected critical path:

```text
G00/M0/G10 1–1.5 d -> slowest production packet 1–1.5 d
-> registry/runtime/CLI integration 0.75–1.25 d -> G30/G40 0.5–1 d
```

Best-case wall time is roughly 3–4.25 working days. Four to six days remains the external planning
commitment because a G10 change invalidates every downstream receipt.

### 9.1 Resource-aware scheduler

Agent-process concurrency and test-process concurrency are separate pools:

- author pool: one isolated worktree per headless worker; scale until memory pressure or useful
  packets are exhausted;
- light gate pool: bounded by available logical CPUs and memory;
- heavy integration/full-suite pool: one job at a time;
- merge/admission pool: one serialized integration branch.

Conformance workers are scheduled before duplicate implementation experiments. Their output adds
an independent executable witness and shortens the implementation packets without multiplying
production authorities.

No fixed numeric worker maximum is encoded in the product plan. Before dispatch, the orchestrator
records a machine budget and stops new workers when free memory falls below the configured reserve,
CPU remains saturated, or disk queue/checkout latency crosses the configured threshold. A worker
waiting for a heavy test token may continue code inspection but may not start another full suite.

The initial author capacity is computed, not guessed:

```text
memory_reserve = max(4 GiB, 20% of physical RAM)
author_capacity = max(0, floor((available_RAM - memory_reserve) / measured_headless_worker_peak))
dispatch_capacity = min(author_capacity, count(READY packets with disjoint ownership))
light_gate_capacity = max(1, min(floor(logical_CPU / 2), measured_gate_capacity))
heavy_gate_capacity = 1
merge_capacity = 1
```

`measured_headless_worker_peak` comes from one calibration worker plus its child-process tree, with
a safety multiplier recorded in the run manifest. `measured_gate_capacity` comes from a concurrent
focused-test probe, not the full suite. The scheduler samples free RAM, CPU saturation and child
process count before every dispatch; resource pressure pauses new dispatch but does not reinterpret
a packet result. API/provider throttling is a separate backpressure signal: a throttled worker is
requeued with the same immutable packet and base, never converted into an implementation failure.

READY priority is deterministic:

1. O1/M0/Q0 bootstrap and read-only characterization;
2. O2/T0 executable-contract pair after M0;
3. implementation/conformance pair bundles in critical-path order; with two free tokens dispatch
   both sides, and with one free token dispatch the conformance side first;
4. the independent integration E2E contract packet;
5. bad-mutant, receipt-audit and read-only investigation packets.

Within one class, sort by critical-path rank, then packet ID. Finished workers release author
tokens immediately; tests enter the appropriate gate queue rather than consuming an author token.

This prevents twenty headless agents from launching twenty redundant full regressions while still
allowing their mostly independent authoring work to proceed in parallel.

### 9.2 OpenCode headless worker adapter

Each OpenCode process receives a self-contained launch envelope; it does not depend on inherited
chat memory or direct messages from another worker. The detailed adapter and terminal schema in
`WORKSTACK-CLI-AGENT-SKILL-HEADLESS-WORKER-DIRECTIVES-2026-09-02.md` is authoritative:

```json
{
  "attempt_id": "<unique attempt>",
  "packet_path": "<absolute packet path outside candidate>",
  "repository": "<absolute disposable clone or isolated worktree>",
  "branch": "agent-p0/<packet-id>",
  "base_sha": "<40-hex>",
  "oracle_seed_sha": "<40-hex>",
  "contract_sha256": "<64-hex>",
  "opencode_version": "<adapter-pinned version>",
  "event_adapter_version": 1,
  "may_push": false,
  "may_merge": false,
  "may_spawn_agents": false,
  "may_start_background_processes": false
}
```

The supervising process, not the worker, creates the checkout and branch. Every packet uses a
unique branch/ref; workers never commit to a shared branch. The supervisor captures exit status,
last heartbeat and parses only the complete final assistant text event from the version-pinned raw
OpenCode JSON stream, then runs the trusted finish gate itself. Foreground test subprocesses are
allowed inside a Job Object/process group; deliberate daemon/background processes are forbidden.

Accepted worker terminal states use the exact-field, attempt-bound schemas in the authoritative
headless-worker directive. Do not extract JSON-looking substrings from logs or tool output.

Natural-language output is retained only as a log. It cannot change state. A silent exit, timeout,
dirty worktree without a candidate commit, wrong-base commit or malformed terminal object becomes a
failed packet and releases its resource token.

Workers do not coordinate through mutable files, chat or branch rebases. The only cross-worker
inputs are frozen contract imports and Oracle-verified receipts. This makes headless agents
replaceable: a crashed worker can be restarted from the same packet without changing anyone else's
state.

### 9.3 Residual bottlenecks and host containment

Elastic workers do not remove these serialized boundaries:

- G10 contract/Oracle freeze;
- protected integration branch admission;
- final `cli.py` hook;
- heavy G30 and real G40 dogfood.

Trying to parallelize those boundaries creates multiple authorities rather than speed.

The supervisor must also contain effects the Git Oracle cannot observe:

- launch with a disposable checkout as CWD and the smallest practical filesystem scope;
- strip GitHub, SMTP, cloud, SSH-agent and other unrelated credentials from the worker environment;
- do not provide push-capable Git credentials or approval tokens;
- use an OS sandbox/restricted account for enforced isolation; a linked Git worktree is concurrency
  isolation only;
- forbid destructive operations outside the packet checkout;
- terminate the complete process tree through a Job Object/process group on timeout;
- inspect checkout and parent-repository status after termination;
- let only the integration/release owner access remotes.

If containment cannot be enforced technically, worker prompts still state the boundary but the
orchestrator treats that as residual host risk, not as an Oracle guarantee.

More workers also amplify contract mistakes. Therefore speculative implementation before G10 is
forbidden even when idle capacity exists. Spare pre-G10 agents perform characterization or
adversarial review whose output can be discarded without invalidating production branches.

## 10. Lane Oracle gates

### 10.1 G21-A — transport

Required behavior:

- validate loopback server metadata;
- GET session and storage identity;
- read the exact bounded context projection through the frozen online backend seam;
- send exact Origin, CSRF, Content-Type and Idempotency-Key;
- POST the current review-entry body unchanged;
- on response loss, replay the same bytes and key at most once;
- never open direct Store after any online failure;
- report replay, first commit or commit_unknown without secrets.

Trusted sentinels:

- first call committed but response lost: second call uses identical body/key and returns replay;
- first call not committed: second identical call commits once;
- second call unavailable: commit_unknown;
- same key with changed body: conflict;
- stale/dead owner metadata: no local adapter invocation;
- request/response token values absent from stdout/stderr.

A session/storage failure occurs before a mutation attempt and therefore omits commit_state.
commit_unknown is valid only after a POST may have reached the server and the identical bounded
replay also cannot establish the result.

### 10.2 G21-B1/B2 — authority and exclusive-local backend

Required behavior:

- require explicit existing `--data-dir` and expected `--workspace-uid`;
- inspect without constructing legacy Store;
- reject missing/unknown/v4 authority before mutation-capable code;
- compare canonical actual/expected UID;
- return immutable `AuthorityAdmission` for a valid v3 authority;
- never use active desktop profile or `WORK_STACK_HOME`.

`agent_authority.py` is B1 and may read and validate but may not import or construct Store.
`agent_local_backend.py` is the independent B2 packet and may import Store and WorkStack only
through an already admitted
`AuthorityAdmission`. It must:

- reread workspace UID inside the existing v3 transaction;
- expose the frozen bounded local context read without leaking unprojected Store data;
- call current `add_worklog_v1` with caller-supplied canonical body and intent key;
- rely on `Store.transaction()` / `.workstack.lock` through the existing transactional service;
- never import `workstack.storage.lease`;
- preserve current replay/conflict behavior.

Trusted sentinels:

- patch `Store.__init__` to fail if called; missing, mismatched and v4 cases must still return the
  correct refusal;
- hash the surrounding temporary tree before/after every refusal; bytes and paths remain equal;
- valid but wrong workspace containing T-0001 fails UID comparison;
- spaces and non-ASCII in an explicit path pass one representative test; no Git traversal matrix.
- local replay and same-key/different-body conflict match the current service semantics.

The full composition order is a G30/TE assertion, not a B1/B2 lane assertion. For a valid local
case it is:

```text
resolve -> format_probe -> uid_probe -> Store.__init__ -> owner_probe -> transaction
```

Missing, unknown, v4 and UID-mismatch cases still require zero Store constructor calls. Store
construction is allowed only after v3 identity admission; it may create the canonical runtime
directory but must not initialize or mutate the authority before backend selection.

The running-server trace ends in HTTP and never enters a Store transaction. B1 tests only the
Store-free admission prefix; B2 tests only the admitted local backend suffix.

### 10.3 G21-C1/C2/C3 — command handlers

Required behavior:

- depend only on contract protocols and injected fakes;
- C1 implements status only;
- C2 implements bounded context for one Task and at most five recent entries within 32 KiB;
- C3 implements checkpoint orchestration only;
- never mutate Task/Objectives/relationships;
- return frozen typed outcomes without writing stdout or stderr;
- keep diagnostics off stdout.

Static Oracle rules reject imports of:

- `http.client`;
- `workstack.store`;
- `workstack.service`;
- storage mutation modules;
- subprocess or shell execution.

Each C implementation packet owns one module, while its paired TC packet exclusively owns the
contract test. Neither side may create a central registry. Dynamic fakes record
every call. Unexpected calls, reordered mutation steps or more than one checkpoint request fail.
The integration owner later composes the explicit static tuple
`(STATUS_COMMAND, CONTEXT_COMMAND, CHECKPOINT_COMMAND)` and the Oracle rejects duplicates, missing
names or inclusion of legacy `apply`.

### 10.4 G21-D — Skill

Required behavior:

- canonical user-scope Skill and progressive-disclosure references validate;
- all commands use the explicit configured command prefix, data dir and workspace UID;
- the Skill calls only status, context, checkpoint and diagnostic worklog list;
- unknown state tells the agent to stop and retain the intent ID;
- no Task completion/delete/rebind/send/sync-adopt behavior appears;
- no instruction tells an agent to edit JSON, NDJSON, SQLite or SSOT files directly;
- no raw prompt, environment dump, credential or command transcript is requested for Worklog.

The trusted test parses executable examples. A code fence that only resembles a valid command does
not count; every canonical example must parse under the actual CLI parser in G30.

## 11. Rolling integration and Oracle G30

The integration owner creates a fresh protected worktree at the frozen base. As each receipt
passes, its disjoint commit is admitted and cherry-picked; integration does not wait idly for every
lane. Wiring begins against the frozen protocols, but G30 begins only after all required receipts.

Accepted commits are ordered as follows:

1. O1/M0/Q0 bootstrap, then the O2/T0 G10 pair;
2. each admitted implementation and its paired conformance commit, with disjoint pairs in any
   order;
3. TE black-box CLI conformance;
4. I1 registry and I2 runtime in either order;
5. I3 thin CLI hook.

Before G30, the trusted runner builds a fresh disposable composition containing the complete
protected integration candidate and every accepted conformance commit, including TE. It records
that exact conformance list in the G30 receipt. Pairwise disposable refs alone are not sufficient
evidence that G30 exercised the tests.

After every cherry-pick, the trusted runner recomputes changed-path ownership and the contract
digest. Conflict resolution by editing a lane-owned file invalidates that lane receipt and requires
its gate to rerun on the new commit.

If disjoint lane ownership nevertheless produces a cherry-pick conflict, the candidate is rejected.
The integrator does not hand-edit worker files. A seam mismatch returns a bounded failure packet to
the owning lane or triggers a G10 contract-change request.

G30 must prove:

- parser help exposes exactly the intended new commands;
- agent dispatch occurs before legacy default Store construction;
- running owner uses HTTP;
- absent owner uses the v3 local service path whose transaction acquires
  `workstack.store.Store.transaction()` / `.workstack.lock` automatically;
- dead owner never falls back locally;
- v4 never reaches legacy Store;
- expected/actual UID mismatch reveals no Task content;
- same-key replay across restart creates one Worklog entry;
- same-key/different-body conflicts;
- lost response follows the bounded retry state machine;
- the entry is visible through current Daily Review and worklog list;
- existing `agent apply` and legacy CLI characterization remain unchanged.

Explicitly forbidden imports/edits:

- do not import `workstack.storage.lease.StorageWriterLease` for v3;
- do not add a server route or service mutation;
- do not change Worklog/storage schemas;
- do not modify frontend, desktop, SSH, installer, updater or uninstaller.

## 12. Trusted receipt

The Oracle runner is loaded from the separate checkout pinned by `oracle_seed_sha` and writes a
receipt outside the candidate diff, for example
`.artifacts/agent-p0/<gate>/<implementation-sha>-<conformance-sha>.json`:

```json
{
  "receipt_version": 1,
  "gate": "G21-A",
  "verdict": "pass",
  "implementation_packet_sha256": "<64-hex>",
  "conformance_packet_sha256": "<64-hex>",
  "oracle_seed_sha": "<40-hex>",
  "contract_sha256": "<64-hex>",
  "base_sha": "<40-hex>",
  "implementation_sha": "<40-hex>",
  "conformance_sha": "<40-hex>",
  "implementation_diff_sha256": "<64-hex>",
  "conformance_diff_sha256": "<64-hex>",
  "implementation_changed_paths": ["workstack/agent_transport.py"],
  "conformance_changed_paths": ["tests/test_agent_transport_contract.py"],
  "checks": [
    {"id": "owned-paths", "exit": 0, "output_sha256": "<64-hex>"},
    {"id": "transport-oracle", "exit": 0, "output_sha256": "<64-hex>"}
  ],
  "skipped_tests": 0
}
```

The integrator does not trust the file merely because it exists. The verifier reruns the pinned
trusted Oracle against an ephemeral base + implementation + conformance composition and requires
the recomputed Oracle seed, both candidate commits, both packets, contract and diff digests to
match. The composition ref is disposable and never becomes an integration authority.

## 13. Gate commands

The implementation adds one trusted entry point:

```text
python -I <trusted-oracle>/scripts/run_agent_p0_gates.py --oracle-root <trusted-oracle> --implementation-root <candidate> --conformance-root <test-candidate> --implementation-packet <packet.json> --conformance-packet <test-packet.json>
python -I <trusted-oracle>/scripts/run_agent_p0_gates.py --oracle-root <trusted-oracle> --candidate-root <candidate> --gate G30 --base <sha> --candidate <sha>
```

The runner executes bounded commands equivalent to:

```text
python -m unittest tests.test_agent_cli_contract -v
python -m unittest tests.test_agent_transport_contract -v
python -m unittest tests.test_agent_authority_contract -v
python -m unittest tests.test_agent_local_backend_contract -v
python -m unittest tests.test_agent_command_status_contract -v
python -m unittest tests.test_agent_command_context_contract -v
python -m unittest tests.test_agent_command_checkpoint_contract -v
python -m unittest tests.test_agent_skill_contract -v
python -m unittest tests.test_agent_cli_e2e_contract -v
python -m unittest discover -s tests/oracle/agent_p0 -p "test_*.py" -v
python -m unittest tests.test_cli_characterization tests.test_agent_apply tests.test_intent_mutations_v1 -v
python scripts/quality_gate.py check --root .
```

Per-lane gates run only their lane test, trusted Oracle subset and existing characterization
dependencies. G30 runs the complete list. Public release gates remain separate.

The runner sets a temporary data root, fixed UTF-8 locale, fixed timezone and bounded process
timeout. It treats skipped/expected-failure tests in the trusted suite as failure.

Receipt core data excludes timestamps, durations, temporary paths and allocated ports. These may
appear in nonauthoritative logs but cannot affect receipt identity. The core JSON is emitted with
sorted keys and compact separators before hashing.

## 14. Failure protocol

| Failure | Deterministic action |
| --- | --- |
| changed path outside ownership | reject commit; worker splits/reverts it |
| contract digest mismatch | expire packet; rerun G10 and redispatch affected lanes |
| lane unit failure | return only to that lane |
| trusted Oracle failure | return failing sentinel ID and fixture digest; no waiver |
| integration conflict | reject manual cross-owner resolution; issue replacement packet |
| flaky result | run the trusted suite in two fresh roots with distinct fixed PYTHONHASHSEED values; any normalized-fact disagreement is ORACLE_NONDETERMINISTIC and blocks merge |
| timeout | fail with `gate_timeout`; worker must reduce nondeterminism, not raise timeout without Oracle-owner change |
| skipped trusted test | fail |
| dirty tree/untracked output | fail before executing product tests |
| commit_unknown in product test | pass only in the sentinel that intentionally makes both responses unverifiable |

Workers may propose a contract change, but cannot bundle it with lane implementation. The proposal
returns to the Oracle owner, produces a new G10 receipt and causes dependent packets to be
regenerated.

Every rejection returns a machine-readable failure packet rather than free-form coaching:

```json
{
  "oracle_id": "P0-NO-FALLBACK-01",
  "candidate_sha": "<40-hex>",
  "observed": {"local_calls": 1},
  "expected": {"local_calls": 0},
  "repair_owner": "implementation",
  "owned_repair_paths": ["workstack/agent_transport.py"],
  "forbidden_repair_paths": ["workstack/agent_authority.py", "workstack/agent_commands.py", "quality/agent-p0-oracle/**"],
  "reproduction": ["python", "-I", "<trusted-runner>", "--invariant", "P0-NO-FALLBACK-01"]
}
```

The worker receives only this bounded repair scope. If the correct fix needs another owner's file,
the orchestrator opens a new packet for that owner instead of widening the current lane informally.

## 15. Anti-gaming rules

The following are Oracle-owned checks, so a production worker cannot satisfy them by weakening its
own tests:

- changed-path allowlist and forbidden-path scan;
- AST import/call scan;
- Store-before-preflight sentinel;
- direct-fallback sentinel;
- request byte/key equality recording;
- authority tree before/after digest;
- stdout single-JSON and secret-canary scan;
- duplicate Worklog count after restart;
- test skip/expected-failure count;
- contract and fixture digests.

The Oracle seed must prove itself by rejecting at least four bounded bad mutants:

- Store construction before preflight;
- online failure followed by local fallback;
- fresh idempotency key on replay;
- token/path canary emitted on stdout.

Oracle tests use public seams and injected canaries rather than asserting source text wherever
possible. Static source checks are reserved for architectural prohibitions that runtime behavior
cannot cheaply observe.

Changing the Oracle runner/config requires:

1. an Oracle-only commit;
2. its own self-tests;
3. explicit integration-owner review;
4. regeneration of every affected receipt.

## 16. Scope deliberately outside these gates

The following must not appear in a P0 packet:

- portable binding/local locator;
- desktop SSH/tunnel discovery;
- v4 mutation parity;
- durable receipt query or Worklog schema change;
- Task patch/completion/delete;
- installer launcher, PATH, updater or uninstall changes;
- frontend changes;
- network-dependent live Microsoft tests;
- full public-release matrix.

They require separate contracts and Oracle gates after P0 dogfood.

## 17. Orchestrator runbook

The orchestrator performs this exact loop:

1. choose and record a clean `base_sha`;
2. dispatch Oracle, contract and characterization packets;
3. run G00 and G10 independently;
4. freeze `contract_sha256`;
5. generate A, B1, B2, C1, C2, C3, D and TA, TB1, TB2, TC1, TC2, TC3, TD, TE packets from that digest;
6. calibrate author/light-test/heavy-test resource pools and create one isolated worktree per packet;
7. dispatch every useful packet that fits the current resource budget;
8. compose each implementation/conformance pair ephemerally and run lane gates concurrently under
   the light-test pool when both commits arrive;
9. never deliver a failed commit as another worker's dependency;
10. roll admitted disjoint commits into the protected integration worktree;
11. prepare I1 registry and I2 runtime composition in parallel, then give I3 sole ownership of the
    thin cli.py hook;
12. run G30 from scratch under the single heavy-test token;
13. run source-checkout dogfood and record G40 evidence;
14. only then decide whether P0b binding or installed launcher has measured priority.

The worker prompt may summarize intent, but its only authoritative sentence is:

> Implement packet `<packet-id>` at `<base-sha>`; completion means the independently recomputed
> required-gate receipt passes with no ownership violation.

This converts intent from memorable prose into a merge precondition.
