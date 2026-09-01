# Work Stack SSOT storage normalization implementation plan

Date: 2026-09-01  
Status: approved; Waves 0-4 implemented; Waves 5-7 code paths implemented behind explicit opt-in; rollout evidence pending  
Scope: canonical SSOT format, migration, local query projection, backup/restore, local and SSH
authorities  
Decision posture: evolutionary migration; no in-place destructive conversion

Implementation evidence is recorded in
`SSOT-STORAGE-WAVE-0-1-REGRESSION-RECEIPT-2026-09-01.md`,
`SSOT-STORAGE-WAVE-2-REGRESSION-RECEIPT-2026-09-01.md`,
`SSOT-STORAGE-WAVE-3-REGRESSION-RECEIPT-2026-09-01.md`, and
`SSOT-STORAGE-WAVES-4-7-CODE-READINESS-RECEIPT-2026-09-01.md`. Wave 3 can now plan,
preview, execute, resume, and independently verify a non-destructive sibling
candidate. Candidates remain inactive; released builds still create and write
only v3. The runtime idempotency-ledger conversion policy is now implemented;
activation remains gated even though the v4 `WriteSession`, repository mutation contracts,
domain-slice backends, projection, backup/restore, and experimental HTTP canary are implemented.
Released startup still injects no v4 backend and creates only v3 workspaces until the remaining
strangulation and external rollout evidence gates are satisfied.

## 1. Outcome

Work Stack will evolve from nine feature-oriented JSON collection files to a normalized,
human-readable SSOT package:

- independently changing domain records are stored as individual JSON records;
- audit-like facts are stored in bounded append-only NDJSON streams;
- stable UIDs own relationships, while human-readable IDs remain presentation labels;
- a machine-local SQLite projection accelerates search and graph queries but is never
  authoritative and is never synchronized;
- the current journal, lease, revision, digest, external-change, backup, and recovery guarantees
  remain in force throughout the transition;
- legacy v3 stores remain readable for a compatibility window and are never modified by the v4
  migration process.

This is a storage-format change, not a public REST or frontend-domain redesign. Existing API
responses, Task IDs, workspace identities, capture provenance, planning-state semantics, and
Connection Center behavior remain stable unless a separately reviewed contract change says
otherwise.

## 2. Why this must be incremental

The current persistence implementation is not an unsafe pile of files. `Store` already provides:

- a process lock and data-directory writer lease;
- replayable multi-file journals;
- atomic file replacement;
- exact SHA-256 authority manifests and generation tracking;
- revision-aware Task semantics;
- fail-closed external-change detection and explicit adoption/rebind flows;
- verified backup, restore, and relocation.

The present structural gate reports 120 production files and zero accepted critical Python or
TypeScript complexity debt above CCN 15. Relevant current measurements are:

| Symbol | Current CCN |
| --- | ---: |
| `Store.save_many` | 5 |
| `Store._commit_prepared_locked` | 9 |
| `Store._validate_ready_state_locked` | 2 |
| `validate_and_project` | 3 |
| `restore_store` | 9 |
| `WorkStack.patch_task` | 12 |

The frozen v3 inventory identifies 32 production call expressions that reach `save_many`, plus
legacy `save()` delegation and test call sites. Replacing those semantics and
the physical format in one change would discard hard-won integrity behavior and create a large
branching orchestrator. The implementation therefore uses a strangler boundary: preserve the
`Store` facade, introduce format-neutral repositories behind it, prove v3 behavior, and admit v4
one capability at a time.

## 3. Non-negotiable invariants

Every phase must preserve these invariants.

1. **One writer per authority.** Local and remote Stores keep the existing lease model.
2. **No silent authority choice.** Connection Center selects the exact local path or SSH profile.
3. **No in-place v3 conversion.** Migration writes a sibling candidate, verifies it, and activates
   it through the existing connection registry.
4. **Source bytes remain unchanged.** Testing, previewing, migrating, rejecting, or rolling back
   may not alter the v3 source Store.
5. **UIDs are identity.** Path names, titles, `T-00xx`, and `O-x` are labels, not foreign keys.
6. **Planning status remains fact-owned.** A Task record does not become a second authoritative
   status owner.
7. **Derived state is disposable.** SQLite indexes, projections, caches, locks, and trust baselines
   stay outside the canonical SSOT root.
8. **Unknown future formats fail closed.** An older build never guesses how to write a newer Store.
9. **External changes require review.** A digest or revision mismatch freezes affected mutation;
   it is not resolved with last-writer-wins.
10. **Append-only means compensating records.** Deletion of facts uses tombstones or compensating
    events; destructive record removal remains an explicit, separately confirmed domain action.
11. **Agents use governed write paths.** An agent may mutate authoritative state only through the
    Work Stack API, a revision-aware command, or a human-reviewed import packet. Direct raw-file
    edits are not a supported write protocol; when detected, they are treated as external changes
    and freeze affected mutation until explicit review.

## 4. Target format: Work Stack SSOT v4

The canonical directory is intentionally readable and transportable:

```text
<workspace-root>/
  store.json
  workspace.json
  records/
    tasks/
      ab/
        <task-uid>.json
    objectives/
      7f/
        <objective-uid>.json
    captures/
      31/
        <capture-uid>.json
    replies/
      20/
        <reply-uid>.json
    notes/
      e4/
        <note-uid>.json
  streams/
    planning-status/
      2026-09.ndjson
    activity/
      2026-09.ndjson
    worklog/
      2026-09.ndjson
  migrations/
    <migration-uid>.json
```

The two-character UID bucket prevents one directory from accumulating an impractical number of
entries while preserving deterministic paths. The bucket is derived from the canonical UID and is
not part of identity.

### 4.1 Canonical metadata

`store.json` is stable domain metadata, not a generated hash inventory:

```json
{
  "format": "workstack.ssot",
  "schema_version": 4,
  "schema_set": "workstack.ssot.v4",
  "workspace_uid": "<stable UUID>",
  "created_at": "2026-09-01T00:00:00Z"
}
```

The generated authority manifest remains below the machine-local runtime root. External editors
must not be required to recalculate the runtime trust baseline. Self-contained backup archives
receive a generated export manifest at backup time.

### 4.2 Record envelope

All independent records use a common envelope:

```json
{
  "format": "workstack.task",
  "schema_version": 1,
  "workspace_uid": "<workspace UUID>",
  "uid": "<record UUID>",
  "display_id": "T-0031",
  "revision": 4,
  "created_at": "2026-09-01T01:00:00Z",
  "updated_at": "2026-09-01T02:00:00Z",
  "title": "A clear next action"
}
```

Rules:

- schemas declare their JSON Schema dialect; instance records declare `format` and
  `schema_version`;
- references persist target UID and may carry a denormalized display label for presentation;
- record revision increments exactly once per committed semantic change;
- unknown top-level fields fail validation until an explicit compatibility rule admits them;
- timestamp, Unicode, number, nullability, enum, and length rules are contract-owned, not scattered
  across API handlers;
- subtasks and objective key results initially remain embedded because their lifecycle is currently
  owned by the parent aggregate; promote them only if independent references or contention emerge;
- Task notes that grow independently become note records linked by `task_uid`;
- relationship graph indexes are projections, not a second canonical edge store.

### 4.3 Streams

The current `activity.json` mixes three different lifecycles. v4 separates them:

- `planning-status`: authoritative append-only status facts;
- `activity`: audit and user-visible activity facts;
- request idempotency: bounded runtime metadata outside canonical long-term history.

Worklog records become a separate append-oriented stream. Streams are monthly NDJSON segments with:

- one complete UTF-8 JSON object per line;
- a monotonic sequence within the workspace stream;
- event UID, workspace UID, record UID, timestamp, actor, provenance, and schema version;
- previous-event digest and current-event digest when hash chaining is enabled;
- immutable closed segments and one writable current segment;
- explicit compaction policy only for non-domain runtime ledgers such as idempotency.

Appending uses a staged replacement in the first implementation rather than relying on a raw
filesystem append. This preserves the existing crash and digest model on Windows and remote
filesystems. A true durable append optimization is allowed only after fault-injection evidence.

### 4.4 Runtime state

Each local or remote Work Stack process builds its own runtime directory:

```text
<runtime-root>/<authority-key>/
  authority-manifest.json
  journal/
  index.sqlite
  projection-state.json
  sync-receipts/
```

`index.sqlite` is rebuildable and contains search, due/status, objective, graph, and projection
indexes. It is excluded from backup, SSH transfer, authoritative hashing, and external merge. A
missing, corrupt, old-schema, or generation-mismatched index is deleted and rebuilt without asking
the user to repair canonical data. Projection metadata records both the authority generation and
the exact authority-manifest digest used to build it. SQL reads are admitted only when both values
match the current verified authority; otherwise Work Stack bypasses the index, serves a canonical
scan where bounded, and reports `Rebuilding` or `Bypassed` rather than returning silently stale
results.

## 5. Proposed code boundaries

`Store` remains the public persistence facade. New code is introduced below it:

```text
workstack/storage/
  contracts.py       JSON Schema loading plus cross-record invariant validation
  canonical.py       deterministic bytes and digests
  layout.py          v3/v4 detection and safe path construction
  records.py         record catalog, reads, staged puts, tombstones
  streams.py         NDJSON segment validation and staged append
  manifest.py        runtime authority manifest v2
  migration.py       v3 -> v4 planning, conversion, parity and receipts
  projection.py      rebuildable SQLite materialized view
  repository.py      format-neutral read/write session used by application service
```

Supporting artifacts:

```text
contracts/workstack-ssot-v4/schemas/*.schema.json
contracts/workstack-ssot-v4/examples/**
tests/fixtures/store-v3/**
tests/fixtures/store-v4/**
scripts/storage_format.py
docs/SSOT-STORAGE-V4-ADR.md
```

Layering changes:

- `workstack/storage/**`, `workstack/store.py`, and `workstack/maintenance.py` belong to
  `py_storage`;
- storage may import foundation contracts but never service, server, desktop, or frontend code;
- service consumes `WorkspaceRepository` and `WriteSession` interfaces;
- server and desktop do not inspect physical storage paths beyond connection and maintenance APIs;
- SQLite is used only inside `projection.py`.

Avoid a generic framework. Interfaces cover only behaviors Work Stack already performs. Do not add
plugins, ORM entities, event buses, or generalized CQRS infrastructure to implement v4.

## 6. CCN and maintainability policy

The existing release rule remains authoritative:

- new critical Python/TypeScript functions above CCN 15 fail;
- existing critical complexity may not increase;
- no baseline exception is created for this project.

The storage implementation uses these working targets:

| Function kind | Preferred | Review trigger | Hard gate |
| --- | ---: | ---: | ---: |
| Orchestrator / migration step | CCN <= 8 | CCN 9-12 | CCN > 15 |
| Pure validator / parser | CCN <= 10 | CCN 11-15 | CCN > 15 |
| Repository query / mapper | CCN <= 8 | CCN 9-12 | CCN > 15 |
| CLI/UI coordinator | CCN <= 10 | CCN 11-15 | CCN > 15 |

CCN 9-15 is allowed when splitting would obscure a single invariant. It requires named branch tests
and a short rationale in the change receipt; it does not require a baseline exception. CCN 10 is a
design target, not a mechanical rejection threshold.

Additional rules:

- migration is represented as named steps returning typed results, not one condition-heavy
  function;
- parsing, validation, semantic parity, writing, activation, and rollback are separate phases;
- format dispatch occurs once at the storage boundary; avoid repeated `if version == ...` branches
  throughout service code;
- errors are typed by responsibility: format, schema, reference, conflict, migration, projection,
  and I/O;
- no boolean parameter controls unrelated behaviors; use explicit commands or strategy objects;
- file-path safety and UID/reference validation are centralized;
- functions above 100 NLOC or nesting depth above four are diagnostics and should normally be
  decomposed, but remain review judgments rather than automatic project blockers;
- each implementation wave runs `quality_gate.py report` before and after, and records changed
  symbol CCN in its receipt.

## 7. Delivery sequence

### Wave 0 — ADR, contract inventory, and frozen characterization

Goal: define what may change before writing a v4 byte.

Deliverables:

- `SSOT-STORAGE-V4-ADR.md` with authority, identity, aggregate, stream, cache, and downgrade decisions;
- an explicit access-path decision stating that agent writes use the API, revision-aware command,
  or reviewed import packet and that raw-file changes enter external-change review;
- complete v3 field inventory for all nine documents;
- JSON Schema v4 drafts and valid/invalid examples;
- canonical semantic snapshot used to compare v3 and v4;
- characterization tests for every existing store operation and public API projection;
- explicit enumeration of the 32 production `save_many` call expressions by domain operation,
  including the legacy `save()` delegation boundary.

Tests:

- empty and populated v3 fixtures;
- Unicode, boundary-length, nullability, and unknown-field fixtures;
- Task/subtask/note/objective/key-result/capture/reply/worklog/activity coverage;
- v3 backup/restore and connection rebind remain byte-stable.

Exit gate:

- no production behavior or Store bytes changed;
- all contract decisions are represented by executable fixtures;
- structural quality and existing release gate pass.

### Wave 1 — canonical codec and schema validator

Goal: create reusable format primitives without activating v4.

Deliverables:

- deterministic canonical JSON codec;
- decision spike: pinned RFC 8785 dependency versus a constrained internal codec;
- JSON Schema loader with pinned offline-build dependency and license receipt if selected;
- cross-document validator for UID uniqueness, workspace ownership, reference existence, revisions,
  and planning-status semantics;
- `storage validate <path>` read-only command producing machine-readable diagnostics.

Tests:

- published RFC canonicalization vectors where applicable;
- Python-version and locale-independent bytes;
- duplicate UID, dangling reference, wrong workspace, future schema, invalid timestamp, and invalid
  stream chain rejection;
- validator never modifies the target directory.

Exit gate:

- new critical functions remain below CCN 15 with no exception;
- validator produces the same result on repeated runs and packaged Windows Python.

### Wave 2 — v4 layout, reader, and semantic projection

Implementation status: complete on 2026-09-01 behind explicit test-only,
read-only admission. No v4 writer or released-format activation was enabled.

Goal: read a hand-built v4 fixture without enabling v4 writes.

Deliverables:

- safe v4 layout/path resolver;
- record and stream readers;
- v4 runtime manifest construction;
- format-neutral `WorkspaceSnapshot` projection;
- v3 and v4 repositories producing identical domain snapshots;
- feature flag or explicit test-only format admission.

Tests:

- path traversal, device path, symlink/reparse, case collision, duplicate record, malformed NDJSON,
  truncated final line, and oversized record rejection;
- shuffled filesystem enumeration produces the same projection;
- v3/v4 parity for API responses, Focus, Daily Review, graph, and search source data.

Exit gate:

- application can boot read-only from a v4 fixture;
- normal released builds still create and write only v3.

### Wave 3 — non-destructive migration engine

Status: verified inactive-candidate engine implemented. Connection-registry
activation is intentionally deferred behind the runtime-ledger and released-v4
write gates; ordinary startup and auto-update do not invoke migration.

Goal: convert v3 to a verified sibling v4 candidate.

Migration state machine:

1. acquire source writer lease;
2. freeze source manifest and semantic snapshot;
3. create exact verified ZIP backup;
4. create a uniquely named sibling candidate directory on the same authority;
5. convert records and streams using deterministic ordering;
6. validate all schemas and cross-record invariants;
7. compare v3 and v4 semantic snapshots;
8. verify source bytes still match the frozen digest;
9. write a content-free migration receipt;
10. offer activation through the connection registry;
11. restart against candidate and confirm authority;
12. retain source and backup until explicit later cleanup.

Deliverables:

- `migration plan`, `preview`, `execute`, `verify`, and `receipt` operations;
- deterministic candidate naming with a user-visible path override;
- resumable or safely discardable staging state;
- activation and restore-previous integration;
- no automatic migration during ordinary startup or auto-update.

Fault-injection tests interrupt before and after every state transition. On restart the system must
either verify and continue an unambiguous candidate or quarantine it; it never edits or silently
activates the source.

Exit gate:

- source byte hashes are identical before and after successful, failed, canceled, and interrupted
  migration;
- a second migration of the same frozen source produces the same semantic and canonical record
  digests;
- downgrade is achieved by selecting the preserved v3 source, not by reverse-converting v4.

### Parallel execution map through Wave 7

The wave numbers describe product dependency, not a requirement to serialize every file. Work is
scheduled in four file-owned lanes with explicit join barriers:

| Lane | Can start | Owns | Joins the critical path at |
| --- | --- | --- | --- |
| A — writer | immediately | runtime placement, record/stream staging, ledger, journal v2, manifest CAS, `WriteSession` | Wave 4 repository contract |
| B — service | after read-contract characterization; production changes after Lane A API freeze | v3/v4 read contract, then one domain slice at a time | Wave 5 complete workflow parity |
| C — projection | immediately from `WorkspaceSnapshot` plus an explicit authority stamp | disposable SQLite build, publication, freshness admission, equivalence tests | Wave 4 manifest wiring, then Wave 5 search/graph reads |
| D — rollout | inactive artifacts immediately; activation only after B | v4 backup/restore, format inspection, migration UX/protocol, installer and release gates | Wave 7 opt-in activation |

The only hard serial chain is:

`journal/runtime policy → WriteSession recovery → repository mutation contract → Wave 5 domain
slices → released v4 opt-in → activation proof and restart confirmation`.

Three join barriers prevent speculative coupling:

1. **Writer barrier:** staging, journal, manifest CAS, runtime ledger, and fault recovery pass before
   any production service method writes v4.
2. **Workflow barrier:** all Wave 5 slices pass the same v3/v4 behavior suite before released v4
   startup or registry activation is enabled.
3. **Rollout barrier:** packaged local/SSH restore, update-without-migration, and explicit activation
   evidence pass before recommending migration.

Common integration files (`workstack/storage/__init__.py`, the storage regression runner, release
matrix, workflow, and receipts) are changed only by the integration lane after each barrier. This
keeps parallel workers from hiding conflicts in the quality or release definition.

Wave 7 has two separately reported meanings:

- **code-complete:** disabled/opt-in migration, backup, inspection, projection, activation, restore,
  installer, and compatibility gates exist and pass locally;
- **rollout-evidence complete:** installed Windows and real SSH canaries pass, and v3 retirement is
  considered only after two public compatibility releases.

External canaries and elapsed public releases are never marked complete merely because the code is
ready. They remain visible evidence gates while all code-independent Wave 6/7 work is pulled forward.

### Wave 4 — v4 write session and journal v2

Status: code-complete and included in the focused storage regression matrix.

Goal: support safe record-level mutation behind an opt-in gate.

Deliverables:

- `WorkspaceRepository` read interface;
- `WriteSession` staging records, stream segments, expected revisions, and expected digests;
- journal v2 containing complete intended writes and recovery information;
- runtime manifest v2 with record roster, stream heads, generation, and semantic Task baselines;
- compare-and-swap conflict checks for targeted and unrelated external changes;
- operation-id idempotency outside the long-term activity stream.

Commit protocol:

1. validate command and expected revisions;
2. load one consistent baseline;
3. create immutable proposed records/events in memory;
4. validate the proposed semantic state;
5. write and fsync the replayable journal;
6. stage target files on the same volume;
7. atomically replace targets in deterministic order;
8. rebuild and verify the runtime manifest;
9. publish the generation event;
10. remove the journal.

Tests cover stale revision, external target change, unrelated external change, duplicate operation,
partial replacement, disk-full simulation, process termination, locked file, invalid replay, and
successful recovery.

Exit gate:

- no v4 mutation can partially appear as a valid committed generation;
- `Store.save_many` remains available only to the v3 adapter and migration tooling;
- new mutation orchestration stays within the CCN working targets.

### Wave 5 — service strangulation by domain slice

Status: all seven domain slices have opt-in v4 command/read backends and backend-neutral service
injection tests. The released constructor remains v3-only. Final removal of physical v3 document
names from the legacy service implementation is still an explicit exit-gate item, not silently
claimed complete.

Goal: move application mutations from collection dictionaries to repository sessions without a
big-bang rewrite.

Order:

1. captures and replies;
2. standalone notes and worklog;
3. objectives and key results;
4. Task create and simple scalar patch;
5. dependencies, parent, references, and deletion/tombstones;
6. planning-status transition and work-session lifecycle;
7. search and graph read projections.

For each slice:

- characterize current request, response, activity, and idempotency behavior;
- implement repository command and read model;
- run the same tests once against v3 and once against v4;
- remove that service method's direct `load`/`save_many` usage;
- record before/after CCN and branch coverage;
- keep commits slice-sized and independently revertible.

Exit gate:

- no production service code names physical files such as `backlog.json` or `activity.json`;
- v3 and v4 backends pass the same repository contract suite;
- v4 opt-in can perform the complete product workflow.

### Wave 6 — SQLite materialized projection

Status: code-complete for disposable build, exact freshness admission, canonical fallback,
search/graph equivalence, and corruption/staleness recovery.

Goal: accelerate reads without changing authority.

Initial indexes:

- Task UID/display ID, status, priority, due, objective, and updated time;
- parent/dependency/reference edges;
- capture status/source and Task attachment;
- normalized searchable text and record provenance;
- manifest generation and projection schema version.

Behavior:

- build from one verified canonical snapshot;
- publish with a transaction, authority generation, and authority-manifest digest;
- query only when projection generation and manifest digest both match the verified authority;
- otherwise fall back to canonical scan or show bounded rebuilding state;
- enable SQLite foreign keys on every connection;
- treat integrity-check failure as cache loss, never SSOT corruption;
- do not copy or synchronize the database between local and SSH authorities.

Tests:

- canonical scan and SQL query result equivalence;
- deleted, corrupt, partial, future-schema, and stale-generation index rebuild;
- same-generation but manifest-digest-mismatched index bypass and rebuild;
- rebuild-completion checks for expected record counts and critical aggregate digests;
- concurrent readers during rebuild;
- restart during projection publication;
- local and remote instances produce equivalent results from the same canonical bytes.

Exit gate:

- deleting the runtime directory changes no product semantics;
- startup and search work without SQLite, only more slowly;
- projection failure cannot block canonical backup or repair operations.

### Wave 7 — rollout, compatibility, and v3 retirement decision

Status: code-ready guards are implemented. Released startup/update remains v3-only and cannot
auto-migrate. Real installed-Windows, real-SSH, and elapsed two-public-release evidence remains
outstanding by design.

Rollout stages:

1. internal fixtures only;
2. developer opt-in with disposable copies;
3. user opt-in migration with explicit backup and restore path;
4. v4 default for newly created workspaces;
5. v4 migration recommendation for existing workspaces;
6. consider retiring the v3 writer only after at least two public compatibility releases.

Do not auto-migrate an existing Store as part of application update. An older application cannot
safely write v4; preserving the v3 directory is the downgrade contract.

Exit evidence:

- installed Windows local authority migration and restart;
- real SSH authority migration with latency and disconnect injection;
- update from the previous public installer without migration;
- explicit v3-to-v4 migration followed by restore-previous;
- backup made on one machine restored and indexed on another;
- no source byte mutation, hidden identity change, or automatic authority switch.

## 8. Regression matrix

### Format and schema

- empty, minimal, representative, and large v3/v4 Stores;
- missing, duplicate, unknown, and future schemas;
- duplicate UIDs/display IDs and cross-workspace references;
- Unicode normalization, long strings, CRLF/LF, BOM, locale, and timezone boundaries;
- deterministic canonical bytes and digests.

### Domain equivalence

- Task counts, IDs, UIDs, revisions, titles, notes, subtasks, tags, and dates;
- objective/key-result ownership;
- parent, dependency, reference, and capture links;
- current planning status derived from facts;
- activity/worklog ordering and provenance;
- API, snapshot export, Conduit contract, Focus, Review, graph, board, and search projections.

### Transaction and recovery

- interruption at every journal transition;
- target and unrelated external modifications;
- writer contention, stale revisions, duplicate operation IDs, and late events;
- disk full, permission denied, locked files, malformed journal, and replay;
- Windows replace semantics and remote latency.

### Backup, restore, and sync

- v3 and v4 backup manifests;
- mixed-format and path-traversal archives rejected;
- verified relocation to empty destination;
- SSH disconnect/reconnect and remote authority identity change;
- SQLite excluded from archive and rebuilt after restore;
- candidate/source/backup displayed with exact paths in recovery UI.

### Scale

Generate 1k, 10k, and 50k Task fixtures with realistic relationships and activity. For the first
three CI runs, retain timings as diagnostic evidence only. Then set reviewed budgets for:

- cold validation;
- manifest generation;
- one-record mutation;
- projection rebuild;
- search and graph queries;
- backup size and duration;
- Windows small-file behavior with antivirus enabled where a release machine permits it.

Avoid inventing hard timing thresholds before stable measurements exist. A measured regression
greater than 20% prompts review but does not automatically block until the budget is ratified.

## 9. Release gate additions

A release that changes v4 storage code must run:

```powershell
python scripts/quality_gate.py check
python -m coverage run -m unittest discover -s tests -v
python -m coverage json
npm --prefix frontend run test:coverage
python scripts/check_coverage.py
npm --prefix frontend run build
python scripts/audit_export.py .
```

It also runs a storage matrix job containing:

- repository contract tests against v3 and v4;
- migration parity and source-byte immutability tests;
- journal fault-injection and recovery tests;
- backup/restore tests for both formats;
- projection rebuild/equivalence tests;
- packaged Windows migration preview and read-only v4 startup;
- SSH command construction in CI and a real SSH canary before enabling v4 by default.

Coverage policy:

- keep the existing global and changed-critical-code floors;
- target at least 90% line and 80% branch coverage in new storage modules;
- require explicit tests for each integrity error branch even when aggregate coverage is already
  high;
- run mutation sentinels for canonical digest comparison, revision comparison, manifest generation,
  source-byte verification, and projection-generation checks;
- do not lower a floor or regenerate the structural baseline merely to admit this work.

## 10. Commit and review discipline

Each wave is split into reviewable commits:

1. tests/fixtures and contract first;
2. one production capability;
3. migration or compatibility adapter;
4. release/quality wiring;
5. receipt with evidence.

Avoid mixing frontend, physical format, installer, and migration-engine changes in one commit.
Migration and writer changes receive an adversarial review focused on:

- loss of source data;
- accidental authority switching;
- split-brain writers;
- stale revision acceptance;
- partially valid generations;
- downgrade traps;
- cache treated as authority;
- schema validation bypass;
- CCN moved into unnamed helpers instead of reduced.

## 11. Observability and user experience

Connection Center shows:

- format: legacy v3 or normalized v4;
- exact authoritative path/SSH alias and remote path;
- workspace label and hidden stable identity status;
- validation and projection state;
- pending external change or migration candidate;
- Preview migration, Migrate copy, Activate candidate, Restore previous, Rebuild index, and Export
  diagnostics actions as appropriate.

Projection state is user-visible as `Verified`, `Rebuilding`, or `Bypassed`; Work Stack never labels
an index verified from file presence alone.

Diagnostics contain paths, versions, generations, counts, and content-free digests. They do not
include Task titles, notes, Microsoft content, credentials, SSH keys, or Store bodies.

## 12. Explicit non-goals

- replacing the SSOT with one authoritative SQLite database;
- full event sourcing for Tasks and objectives;
- automatic semantic merge of conflicting agent edits;
- syncing the SQLite projection;
- automatic migration during update or startup;
- deleting the v3 source after successful migration;
- reverse conversion from v4 to v3;
- changing REST, Conduit, or frontend contracts merely to match physical storage;
- making arbitrary raw-file edits the recommended agent write interface.

An agent-safe revision/digest-aware patch command can follow the repository boundary, but it is a
separate product capability rather than a prerequisite for reading v4.

## 13. Definition of done

The normalization project is complete when:

1. JSON Schema and cross-record validators define every canonical v4 record and stream.
2. v3 and v4 satisfy one repository contract suite and produce equivalent domain projections.
3. migration is non-destructive, deterministic, interruptible, verifiable, and reversible by
   authority selection.
4. the full Work Stack workflow writes v4 without service code naming physical collection files.
5. local and SSH authorities preserve one-writer, revision, digest, and external-change guarantees.
6. SQLite accelerates reads but can be deleted and rebuilt without semantic change.
7. backup, restore, installer, updater, and Connection Center understand both formats.
8. no new accepted critical CCN debt exists, all structural/release gates pass, and changed storage
   code has targeted branch and mutation evidence.
9. at least one installed Windows local canary and one real SSH canary complete migration, restart,
   mutation, backup, restore-previous, and index rebuild.
10. v3 retirement remains a later explicit decision backed by compatibility telemetry, not an
    assumption embedded in v4 rollout.

## 14. Recommended first implementation batch

Start with Wave 0 and Wave 1 only. They are parallelizable after the ADR freezes names:

- track A: v3 field inventory, fixtures, and repository characterization;
- track B: JSON Schemas and cross-record invariant catalog;
- track C: canonicalization spike and golden vectors;
- track D: storage quality/release matrix scaffolding.

Merge order is A, ADR decision, B/C, then D. Do not begin the migration writer until the reader,
semantic parity comparator, and source-byte immutability tests are green. This gives the project
useful contracts and diagnostics immediately without placing real user data at risk.
