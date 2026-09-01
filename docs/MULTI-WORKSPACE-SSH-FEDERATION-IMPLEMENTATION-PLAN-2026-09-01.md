# Work Stack multi-workspace SSH federation implementation plan

Date: 2026-09-01
Status: M0 and the isolated M1/M3 contract foundations are implemented; runtime integration remains gated
Repository branch at planning time: `codex/workstack-python-desktop-20260831`

## 1. Decision

Work Stack will support multiple local or SSH-reachable workspaces as a **federation of
independent authorities**. It will not split one writable SSOT across machines and it will not
merge replicated JSON directories.

Each workspace remains the sole authority for its own Tasks, Objectives, notes, activity, revisions,
and Store manifest. The desktop application may build a disposable aggregate projection for unified
work management, but that projection is never allowed to become an implicit second planning
authority.

The first useful release is deliberately narrower than the final product:

1. save and test multiple connection profiles;
2. switch safely between one active workspace at a time;
3. add a read-only unified Portfolio projection;
4. reflect live changes from connected workspaces while the desktop app is running;
5. route edits to the owning workspace only after the read path is proven;
6. add cross-workspace planning records only under an explicitly chosen Portfolio authority.

This sequence provides value early without introducing multi-master writes.

## 2. Existing contracts that must be preserved

The current product already has strong single-workspace boundaries:

- `workspace.json` supplies a canonical, non-nil workspace UUID.
- the Store manifest records that identity, a monotonic logical generation, authoritative file
  digests, and per-Task revision baselines;
- a writer lease and recovery journal protect multi-file transactions;
- unexpected direct file edits freeze writes until a validated adoption or workspace rebind;
- Task and Objective mutations use expected revisions where conflicts matter;
- ambiguous mutation retries use idempotency keys;
- the desktop verifies a remote workspace identity after SSH connection;
- SSH forwards are loopback-only, use strict host checking, and are owned by the desktop process;
- `/api/v1/events` is a content-free invalidation hint;
- `sync_events()` explicitly reports `delivery: bounded-process-local`.

The last point is important: an event cursor is valid only for the lifetime of that remote server
process. It cannot be persisted as proof that every historical change was consumed. Every startup,
tunnel replacement, remote process restart, or event-stream gap requires an authoritative projection
refetch and identity/generation verification.

## 3. Product model and terminology

### 3.1 Workspace authority

A workspace is an independent Work Stack Store with one stable `workspace_id`. A Task belongs to
exactly one workspace. The same logical piece of work must not be copied into two writable
workspaces and edited independently.

### 3.2 Connection profile

A connection profile describes how this desktop reaches one workspace. It is endpoint metadata, not
planning data. A profile has its own stable `profile_id`, while `workspace_id` identifies the
authority behind that endpoint.

### 3.3 Federated entity reference

An entity in an aggregate view is addressed by a structured reference:

```json
{
  "workspace_id": "11111111-1111-4111-8111-111111111111",
  "entity_type": "task",
  "entity_id": "T-0001"
}
```

Frontend state, selection, React keys, dependencies, and mutation routing must retain all three
fields. Code must not make `T-0001` globally unique and must not permanently rewrite source IDs into
concatenated display strings.

### 3.4 Portfolio projection

The Portfolio is a derived, rebuildable view of allowlisted projections from several authorities.
It may be held in memory and later cached locally for startup performance. Deleting the cache must
never delete or change a source workspace.

## 4. Non-negotiable safety invariants

These invariants are release gates, not optional implementation advice.

1. **Single owner:** every mutation resolves to exactly one verified `workspace_id` before network
   I/O begins.
2. **No JSON synchronization:** federation reads and writes only through versioned Work Stack APIs,
   never SSHFS, SFTP copying, or direct remote Store file edits.
3. **Identity before content:** a connection is not ready until `/api/v1/storage` reports the
   expected canonical workspace identity and a supported protocol/capability set.
4. **No silent endpoint substitution:** a profile reaching an unexpected workspace enters
   `identity-mismatch`; it is never relabeled automatically.
5. **No cross-workspace transaction claim:** a command affecting two authorities is not presented
   as atomic. The initial product exposes no such command.
6. **Revision guard:** routed edits include the source entity's expected revision.
7. **Idempotent ambiguity recovery:** retryable commands retain one idempotency key; transport loss
   after send is resolved by rereading the owning workspace before another mutation.
8. **No offline write queue initially:** disconnected workspaces are readable only from an
   explicitly stale projection. Commands are disabled until authority is reverified.
9. **Event hints are not evidence:** SSE invalidates a workspace projection; only a successful
   authoritative refetch advances the displayed coordinate.
10. **Per-workspace failure isolation:** one disconnected, corrupt, externally changed, or
    incompatible workspace does not freeze healthy workspaces. It freezes only its own mutations
    and marks its aggregate records stale or unavailable.
11. **No credential persistence:** registry files contain no password, private key, agent token,
    decrypted secret, or host-key bypass. OpenSSH configuration and the user's agent remain the
    credential authority.
12. **Owned process shutdown:** Work Stack stops only tunnels and remote commands it started.
13. **Projection provenance:** every aggregate record carries source workspace, generation,
    observed time, and stale state.
14. **Rollback does not touch SSOT:** disabling federation or reverting the desktop must leave every
    source workspace byte-for-byte untouched.

## 5. Target architecture

```text
Installed Work Stack UI
        |
        | versioned native federation bridge (read-only first)
        v
Federation Coordinator in desktop host
        |
        +-- Connection Supervisor A -- SSH tunnel -- Work Stack API A -- SSOT A
        +-- Connection Supervisor B -- SSH tunnel -- Work Stack API B -- SSOT B
        +-- Local Workspace Adapter ---------------- Work Stack API C -- SSOT C
        |
        +-- disposable aggregate projection
```

### 5.1 Why the native bridge is the first federation boundary

The current UI is served by one active Work Stack origin, and its relative HTTP client assumes one
workspace. Turning the existing API into a transparent multi-authority write proxy in the first
release would combine routing, authentication, CSRF, version negotiation, connection recovery, and
mutation ambiguity in one change.

The first Portfolio therefore uses a narrow, versioned native bridge for aggregate **reads and
status only**. Existing single-workspace HTTP mutation paths remain unchanged. Selecting Edit from
Portfolio initially switches to and verifies the owning workspace before opening the existing Task
drawer.

The bridge accepts messages only from the exact verified Work Stack origin, validates every payload
against a schema version and size bound, and never accepts a raw URL, SSH argument, filesystem path,
or arbitrary HTTP request from frontend content.

After this path is stable, a separate decision may introduce a same-origin federation gateway for
direct routed writes. It is not required to deliver the read-only Portfolio or safe workspace
switching.

### 5.2 Components and module boundaries

New desktop-side modules should be extracted rather than added to the already large
`workstack_desktop.py`:

- `connection_registry.py`: schema, validation, atomic migration, and persistence;
- `ssh_config_discovery.py`: safe alias suggestions and `ssh -G` resolution checks;
- `workspace_connection.py`: one profile's lifecycle and verified HTTP client;
- `connection_supervisor.py`: bounded reconnect/stop state machine for one profile;
- `federation_coordinator.py`: profile orchestration and aggregate projection assembly;
- `federation_bridge.py`: bounded native message schemas and UI publication.

Frontend additions should remain feature-scoped:

- `app/federationHostBridge.ts`: strict command/status/projection decoding;
- `features/federation/`: Portfolio surface, workspace switcher, status and filters;
- `domain/federationSchemas.ts`: structured references and projection validation.

The core Store and service must not import desktop federation modules. Federation coordinates
existing authorities; it does not weaken or bypass Store validation.

## 6. Persistent and runtime data contracts

### 6.1 Connection registry

Replace the singleton connection setting with an atomic, versioned registry under the Windows state
directory. A proposed schema is:

```json
{
  "schema_version": 1,
  "active_profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "profiles": [
    {
      "profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      "label": "Company engineering",
      "kind": "ssh",
      "enabled": true,
      "live_updates": true,
      "ssh_host_alias": "work-linux",
      "remote_app_dir": "/srv/workstack/app",
      "remote_data_dir": "/srv/workstack/engineering",
      "expected_workspace_id": "11111111-1111-4111-8111-111111111111",
      "preferred_forward_port": 18765,
      "remote_port": 8765
    }
  ]
}
```

The registry does not persist runtime ports, PIDs, health, event cursors, CSRF tokens, or errors.
Those belong to session memory and diagnostic logs.

Profiles are a strict discriminated union. An SSH profile has only the fields illustrated above. A
local profile has `kind: local`, its canonical `data_dir`, and expected workspace identity, but no
SSH or port fields. The existing installer-selected local data directory remains the default local
profile; adding arbitrary local directories is not required for the first SSH-federation release.

### 6.2 Migration from the singleton profile

Migration must be lossless and reversible:

1. read and strictly validate the existing `remote-connection.json`;
2. generate one `profile_id` and preserve every connection field;
3. write the new registry atomically;
4. reread and compare the normalized result;
5. retain the old file as an inactive migration backup until one successful launch;
6. never mutate remote or local planning data during migration.

If migration fails, the existing single-profile mode continues. No partial registry is accepted.

### 6.3 Duplicate authorities

Two profiles may accidentally reach the same `workspace_id`. The first release must not show their
Tasks twice. Schema v1 permits saved backup endpoints only when at most one profile for that
authority is enabled. A later failover milestone may replace this invariant with an explicit
per-authority primary profile, but implicit endpoint selection is never allowed because it requires
explicit host-key and endpoint trust semantics.

### 6.4 Runtime status

Each profile reports one of:

- `idle`
- `connecting`
- `ready`
- `stale`
- `reconnecting`
- `disconnected`
- `identity-mismatch`
- `integrity-blocked`
- `incompatible`

Runtime status includes the actual workspace ID, product/protocol version, session port, last
confirmed generation, last successful observation time, and a bounded error code. It excludes Task
content and SSH command lines.

## 7. Connection and update lifecycle

The detailed migration, path validation, restart activation, recovery, and Connection Center plan
is defined in `PER-WORKSPACE-SSOT-PATH-ACTIVATION-IMPLEMENTATION-PLAN-2026-09-01.md`.

### 7.1 Startup

1. Load and validate the registry.
2. Start the active workspace first so existing single-workspace UX remains responsive.
3. Start other enabled live connections with bounded concurrency and OS-assigned loopback ports.
4. For each source, verify health, storage identity, protocol/capabilities, and sync status.
5. Fetch its complete allowlisted workspace projection.
6. Publish a Portfolio snapshot only after validating every included source projection.
7. Show unavailable sources separately; do not discard healthy results.

Schema v1 stores and projects at most 128 workspace profiles in one registry. This explicit bound
matches the unpaginated native bridge contract; lifting it requires paginated status and Portfolio
messages rather than increasing only one side. Connection startup is concurrency-bounded to avoid
an SSH/process storm, but queued profiles remain visible and user-configurable.

### 7.2 Live updates while the app is open

Each ready connection owns an SSE listener. A content-free event causes only that workspace to be
refetched. The aggregate projection is replaced after schema, identity, and generation checks. Event
bursts are coalesced per workspace so one rapid transaction sequence does not trigger unbounded
parallel reads.

If a source reports `external-change-detected`, `invalid`, or workspace-rebind availability, its
unadopted candidate bytes are not promoted into Portfolio. Portfolio retains the last confirmed
projection with an `integrity-blocked` badge while the existing source-specific review flow remains
the only place that can adopt or rebind that authority.

### 7.3 Reconnect and app restart

On event gap, process exit, tunnel replacement, laptop resume, or app restart:

- discard the process-local SSE cursor;
- reverify the expected workspace identity;
- fetch storage and sync status;
- perform a complete authoritative workspace projection fetch;
- replace only that workspace's aggregate slice.

The last observed generation may be shown to the user, but it is not sufficient to skip the full
refetch.

### 7.4 App closed

SSH-only desktop federation cannot receive changes while Work Stack is closed. The next launch
catches up by full refetch. A 24/7 central federation daemon or hosted service is a separate future
deployment model and must not be implied by the desktop release.

## 8. Portfolio and mutation UX

### 8.1 Portfolio v1

Portfolio v1 is read-only and supports:

- all-workspace Task list and counts;
- workspace health/staleness indicators;
- filters by workspace, status, priority, due state, and Objective label;
- source workspace badge on every card/row/detail;
- aggregate progress computed from confirmed source projections;
- explicit refresh of one workspace or all failed workspaces;
- open-in-owning-workspace action.

Its unpaginated schema v1 envelope is bounded to 128 workspace profiles, 128 projected Tasks, and
16 same-workspace Objective references per Task. The worst-case schema-valid payload is tested
against the native bridge's 1 MiB serialized-message ceiling. Larger portfolios require a later
paginated protocol with explicit completeness semantics.

The existing single-workspace Workspace, Focus, Context Inbox, Daily Review, and Objective Hub remain
unchanged until each receives an explicit federation design.

### 8.2 Safe editing v1

The first Edit action does not mutate through the Portfolio bridge. It:

1. records the structured entity reference;
2. switches the active profile;
3. verifies the owning authority;
4. reloads the normal workspace projection;
5. opens the existing Task drawer with its normal revision guards.

This adds one transition but reuses the proven mutation path.

### 8.3 Direct routed edits

Direct Portfolio mutations may be added only after a dedicated remote client contract exists. Each
command must contain:

- structured entity reference;
- expected revision;
- one domain command or bounded field patch;
- one caller-generated idempotency key;
- target workspace identity repeated in the request envelope.

The coordinator verifies the target immediately before send and rereads it after an ambiguous
failure. It never reroutes the command merely because another profile is healthy.

### 8.4 Cross-workspace Objectives and dependencies

Source workspaces must not each store competing copies of a cross-workspace edge. When this feature
is introduced, the user chooses one normal Work Stack workspace as the **Portfolio authority**. It
owns only portfolio Objectives and cross-workspace references; source Tasks remain owned by their
original workspaces.

No cross-workspace transaction is claimed. A source Task update and a Portfolio edge update are two
observable operations with independent revisions and recovery.

## 9. Implementation milestones and acceptance gates

### M0 — freeze the federation contract

Deliverables:

- architecture decision record derived from this plan;
- structured entity-reference schemas in Python and TypeScript;
- characterization tests proving current singleton behavior, identity verification, reconnect, and
  shutdown remain unchanged;
- architecture rules preventing Store or service imports from federation UI/desktop orchestration.

Exit gate: existing local and single-remote installations behave identically with federation
disabled.

### M1 — multi-profile registry and SSH-config discovery

Deliverables:

- atomic registry and singleton migration;
- add/edit/remove/test profiles;
- alias suggestions from the normal OpenSSH config location while keeping manual input;
- `ssh -G <alias>` resolution verification delegated to OpenSSH;
- active-workspace switcher;
- one active tunnel at a time.

Exit gate: the user can restart and switch repeatedly among at least three distinct workspace IDs
without identity drift, port collision, orphan process, or SSOT modification.

### M2 — connection supervisors

Deliverables:

- extracted one-profile connection state machine;
- coordinator for multiple independent supervisors;
- dynamic runtime ports and per-profile logs;
- bounded parallel startup, reconnect, cancellation, and shutdown;
- per-profile protocol and identity diagnostics.

Exit gate: failure of one of three connections does not reload, freeze, or stop either healthy
connection.

### M3 — read-only Portfolio

Deliverables:

- versioned read-only native bridge;
- aggregate projection with structured references and provenance;
- Portfolio UI, filters, workspace badges, stale/error states;
- duplicate workspace detection;
- open/switch to owning workspace.

Exit gate: two workspaces containing the same `T-0001` remain separately selectable and always open
the correct authority. No Portfolio code path can issue a planning mutation.

### M4 — live invalidation and recovery

Deliverables:

- one SSE listener per ready workspace;
- per-workspace invalidation coalescing;
- full refetch after reconnect or event gap;
- resume/online recovery and clean shutdown;
- generation and observation-time display.

Exit gate: a committed remote change appears in Portfolio while the app is open, and changes made
while it is closed appear after the next verified launch. Missing an SSE event cannot leave a source
permanently stale.

### M5 — routed single-workspace mutations

Deliverables:

- narrow remote mutation client with session/CSRF handling;
- revision and idempotency envelopes;
- commit-unknown reread flow;
- per-workspace optimistic update isolation;
- no offline queue.

Exit gate: stale revision, disconnect-before-send, disconnect-after-commit, and unchanged retry all
produce deterministic results without duplicate or cross-workspace writes.

### M6 — Portfolio authority and cross-workspace relationships

Deliverables:

- explicit Portfolio-authority selection;
- revisioned cross-workspace reference records;
- aggregate Objective progress;
- dangling/unavailable reference UX;
- export/backup behavior for Portfolio metadata.

Exit gate: removing a connection cannot delete its source Task; cross-workspace references become
dangling and recoverable rather than being silently removed.

### M7 — optional disposable cache and scale work

Only add a durable aggregate cache if measured startup and reconnect performance require it. The
cache must be versioned, bounded, rebuildable, and excluded from all authority/backup claims. A
privacy decision must define which projected fields may be retained locally before implementation.

## 10. Required tests

### 10.1 Registry and migration

- malformed, unknown, duplicate, and oversized fields fail closed;
- existing singleton profile migrates byte-for-field without changing planning data;
- interrupted write preserves the prior registry;
- duplicate workspace identities do not duplicate entities;
- deleting a profile never deletes a remote directory or workspace.

### 10.2 Connection adversarial matrix

- preferred-port collision and bind race;
- wrong workspace behind a valid tunnel;
- unsupported product/protocol/capability;
- strict host-key refusal and authentication failure;
- one remote process dies while others remain healthy;
- reconnect during desktop shutdown;
- laptop resume/network loss and recovery;
- external-change and rebind state in only one workspace;
- remote app upgraded while another remains on the prior compatible version.

### 10.3 Projection and UI

- duplicate Task IDs across workspace identities;
- mixed healthy, stale, incompatible, and disconnected sources;
- no source badge omission on aggregate entities;
- Task selection preserves structured provenance through URL and drawer transitions;
- stale projection cannot enable mutation;
- malformed native messages are ignored;
- one source refetch replaces only its aggregate slice.

### 10.4 Mutation adversarial matrix

- route selection cannot be influenced by display label or Task ID alone;
- stale expected revision;
- same idempotency key with changed payload;
- disconnect before send;
- disconnect after remote commit but before response;
- remote identity changes between projection and send;
- simultaneous edits to equal Task IDs in different workspaces;
- Portfolio authority unavailable during a source update.

### 10.5 Packaged Windows tests

The installer lane must include a deterministic multi-workspace fixture using at least three local
loopback servers with distinct workspace IDs. Extended Windows tests additionally verify real
OpenSSH command construction, process ownership, shortcut launch, clean shutdown, and no orphaned
SSH or Python child.

## 11. Release regression integration

Federation paths must select proportional release gates in `quality/release-path-policy.json`.
Required release evidence for a federation-affecting build includes:

- complete Python and frontend suites;
- structural quality and architecture contracts;
- registry migration tests;
- three-workspace integration fixture;
- connection shutdown/orphan-process test;
- native bridge schema/adversarial tests;
- mutation sentinels for workspace routing, duplicate-ID isolation, and stale revision rejection;
- production frontend build and packaged first-launch smoke;
- unchanged single-local and single-remote compatibility tests.

No baseline update may waive the safety invariants in section 4.

## 12. Observability and supportability

The Connection Center becomes a workspace registry and shows one concise row per profile:

- label and verified workspace name/ID suffix;
- SSH alias or Local;
- state and runtime port;
- last confirmed generation and observation time;
- Test, Connect, Make active, Pause live updates, Edit, and Remove actions.

Logs are separated per `profile_id`, bounded by retention, and redact query strings, cookies,
tokens, Task content, private-key paths, and full SSH commands. User-facing failures use stable error
codes such as `identity_mismatch`, `host_key_refused`, `authentication_failed`,
`protocol_incompatible`, and `remote_integrity_blocked`.

## 13. Rollout and rollback

- Federation starts behind a persisted product setting and defaults off for upgraded users.
- M1 may ship before Portfolio; the old single-profile UX remains available through migration.
- M3 Portfolio ships read-only before any routed mutation flag exists.
- M5 mutation routing has its own kill switch; disabling it preserves Portfolio reads.
- A rollback restores the prior application and registry reader only. It never rewrites a source
  workspace.
- Release notes must state that updates are live only while the desktop is open; no background cloud
  synchronization is claimed.

## 14. First implementation packet

The first code packet should stop at M0 plus the non-UI half of M1:

1. add `connection_registry.py` with versioned types and atomic persistence;
2. add RED tests for migration, duplicate profile IDs, duplicate workspace IDs, interruption, and
   unsupported fields;
3. migrate the existing singleton draft in memory and prove a lossless normalized round trip;
4. add SSH config alias discovery as suggestions only;
5. validate a selected alias through `ssh -G` without accepting arbitrary arguments;
6. leave current startup and UI behavior unchanged;
7. run the complete quality/release regression suite before wiring the registry into startup.

This packet is independently useful, low blast-radius, and establishes the data contract needed by
every later milestone without prematurely running several tunnels or changing mutation authority.

## 15. Explicit nonclaims

This plan does not provide distributed transactions, writable replicas, merge arbitrary SSOT JSON,
receive updates while the desktop is closed, make SSHFS safe, silently fail over between identities,
or make a disposable Portfolio projection authoritative. Those capabilities require separate
contracts and evidence rather than optimistic extension of the first federation release.
