# ADR: Federate independent Work Stack workspace authorities

Date: 2026-09-01
Status: accepted for incremental implementation

## Context

Work Stack currently connects one desktop session to one local or SSH-remote planning Store. The
Store's workspace identity, writer lease, recovery journal, manifest, revisions, idempotency keys,
and external-change review form one authority boundary. Users now need to observe and manage work
owned by several remote environments without manually consolidating their SSOT directories.

The current event stream is a bounded, process-local invalidation mechanism. It is not a durable
replication log. The current frontend HTTP client also assumes that relative requests target one
active workspace origin.

## Decision

Work Stack will federate several independent workspace authorities. It will not replicate or merge
their authoritative JSON files.

- A source workspace remains the sole authority for every entity it owns.
- Federated references retain `workspace_id`, `entity_type`, and `entity_id` as separate fields.
- A derived Portfolio projection is disposable and includes source provenance and staleness.
- Startup, reconnect, and event gaps perform a complete authoritative projection refetch after
  identity and protocol verification.
- The first Portfolio release is read-only and uses a bounded, versioned native bridge.
- Schema v1 permits at most one enabled profile per workspace authority; disabled backup endpoints
  may be saved, but implicit primary or failover selection is forbidden.
- Schema v1 uses one bounded, unpaginated envelope for at most 128 profiles and 128 projected Tasks.
  A higher limit requires an explicit paginated protocol rather than asymmetric decoder limits.
- The first Edit action switches to the owning workspace and reuses the existing single-authority
  mutation path.
- Direct routed mutations require a later explicit contract containing target workspace identity,
  expected revision, idempotency key, and commit-unknown reread behavior.
- Cross-workspace Objectives or edges, when implemented, are owned by one explicitly selected
  Portfolio workspace rather than copied into every source.

## Consequences

### Positive

- Existing Store durability and conflict rules remain unchanged.
- Equal entity IDs from different workspaces cannot collide when structured references are used.
- A broken workspace can be isolated without blocking healthy authorities.
- The product can ship safe profile switching and read-only aggregation before accepting the risk of
  routed writes.
- Disabling or rolling back federation never requires rewriting planning data.

### Costs

- The desktop must supervise several independently verified connections and event listeners.
- Aggregate UI state must carry provenance instead of passing bare Task IDs.
- Changes made while the desktop is closed are discovered on the next complete refetch, not through
  a claimed background synchronization service.
- Seamless direct editing from Portfolio is deferred until its routing and ambiguity contract is
  independently tested.

## Rejected alternatives

### Mount or copy remote SSOT directories

Rejected because cross-platform filesystem locks, atomic replacement, recovery journals, and
partial transfer failure cannot preserve the Store contract reliably.

### Writable local replica with last-write-wins merge

Rejected because it creates a second authority, loses field ownership, and can silently overwrite
agent or remote edits.

### Treat Task IDs as globally unique

Rejected because `T-0001` is allocated inside a workspace and can legitimately exist in every
authority.

### Transparent multi-workspace write proxy in the first release

Rejected for the first packet because it combines routing, CSRF/session handling, remote version
negotiation, reconnect, and commit ambiguity before the aggregate read path has evidence.

### Persist SSE cursors as a durable replication coordinate

Rejected because the server explicitly reports process-local event delivery. Reconnect correctness
must rely on identity verification and authoritative refetch.

## Implementation reference

The bounded implementation order, invariants, migration contract, failure tests, and release gates
are specified in `MULTI-WORKSPACE-SSH-FEDERATION-IMPLEMENTATION-PLAN-2026-09-01.md`.
