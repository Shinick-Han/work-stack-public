# ADR: Work Stack SSOT v4 storage authority and normalization

Status: accepted for implementation  
Date: 2026-09-01  
Owners: Work Stack storage boundary  
Supersedes: no existing format; v3 remains supported during the compatibility window

## Context

Work Stack v3 stores planning state in nine JSON documents. The current `Store` adds a writer lease,
replayable journal, atomic replacement, a runtime authority manifest, revision checks, external
change review, and verified backup/restore. Those guarantees are valuable and must survive any
physical normalization.

The collection layout nevertheless concentrates independently changing records in large arrays,
mixes activity, idempotency, and planning-status lifecycles, and requires complete document rewrites.
Work Stack also needs human recovery, local and SSH authorities, future RAG/Obsidian export, and a
stable docking boundary for Conduit.

## Decision

### Canonical authority

The canonical v4 SSOT remains a human-readable directory of JSON records and bounded NDJSON
streams. SQLite is not canonical. A machine-local SQLite database may be built as a disposable
materialized projection.

The runtime authority manifest, lock, journal, sync receipts, and SQLite projection remain outside
the canonical SSOT root. A backup receives a generated export manifest so it is independently
verifiable.

### Record identity and layout

- workspace and record UIDs are immutable identity;
- `T-00xx`, `O-x`, names, titles, paths, and profile labels are presentation identifiers;
- references persist the target UID;
- independently changing Tasks, objectives, captures, replies, and notes are individual records;
- subtasks and key results remain embedded until independent lifecycle evidence justifies promotion;
- record paths use a deterministic two-character UID-prefix bucket;
- date segmentation is reserved for append-oriented event streams, not mutable records.

### Streams

Planning-status, activity, and worklog are separate append-only streams. Request idempotency is
bounded runtime metadata rather than permanent activity history. Closed monthly segments are
immutable. The initial writer stages and atomically replaces the active segment; raw append is not
trusted until fault-injection evidence proves equivalent durability.

### Agent and external access

An agent may mutate authoritative Work Stack state only through one of these governed paths:

1. the Work Stack API;
2. a revision- and digest-aware Work Stack command;
3. a human-reviewed import packet.

Direct raw-file editing is not a supported agent write protocol. Read-only inspection and emergency
human recovery remain possible because the canonical format is transparent. If any unowned process
changes canonical bytes, Work Stack treats the change as external, freezes affected mutations, and
requires explicit review. It never silently merges or uses last-writer-wins.

SSH does not imply replicated multi-writer storage. The remote Work Stack process owns the writer
lease for the remote authority; the Windows application sends commands to that owner. Offline replica
merge and Git semantic merge are separate, currently unsupported product capabilities.

### Format boundary

`Store` remains the compatibility facade. Application services migrate from physical document names
to a format-neutral repository and write-session interface. Format selection happens once at the
storage boundary; v3/v4 conditionals must not spread through service or transport code.

### Migration and downgrade

v3-to-v4 migration is non-destructive:

- freeze and hash the source;
- create a verified backup;
- write a sibling v4 candidate;
- validate schemas and cross-record invariants;
- compare a canonical semantic projection;
- prove source-byte immutability;
- activate through Connection Center with a receipt;
- retain the v3 source for downgrade.

Startup and application update never auto-migrate a user Store. Reverse conversion is not the
downgrade mechanism.

### Projection trust

SQLite projection metadata records:

- projection schema version;
- authority generation;
- exact authority-manifest digest;
- completed publication marker.

SQL reads are admitted only when all values match the currently verified authority. Otherwise the
projection is `Rebuilding` or `Bypassed`; bounded reads use the canonical Store. File presence alone
never establishes freshness. Rebuild completion verifies expected record counts and critical
aggregate digests before publishing `Verified`.

The projection is excluded from backup and synchronization. Missing, stale, corrupt, or
integrity-check-failing projection data is discarded and rebuilt; it is never reported as canonical
corruption.

### Canonical bytes

v4 uses the constrained `workstack.canonical-json.v1` codec recorded in
`SSOT-CANONICALIZATION-DECISION-2026-09-01.md`. It emits compact, key-sorted UTF-8 JSON, rejects
floats, unsafe integers, invalid Unicode, non-string keys, unsupported containers, and cycles, and
does not normalize Unicode. This is a Work Stack byte contract, not an RFC 8785 claim. Future
non-Python readers must reproduce its golden vectors before using canonical digests as a trust
boundary.

## Consequences

### Positive

- changes and conflicts are scoped to independent records;
- transparent data remains inspectable, portable, and recoverable;
- current one-writer, journal, revision, digest, and review semantics are preserved;
- query and graph performance can improve without making a cache authoritative;
- Conduit and agents use stable application contracts rather than physical files;
- v3 remains an explicit, testable compatibility backend.

### Costs

- repository, migration, projection, and dual-format contract tests become permanent maintenance;
- record-level multi-object transactions require journal v2 rather than ordinary file writes;
- small-file behavior must be measured on Windows and SSH authorities;
- an older build cannot write v4, so the preserved v3 authority is required for downgrade;
- JSON Schema cannot enforce cross-record ownership and references without semantic validation.

## Rejected alternatives

### One authoritative SQLite database

Rejected for v4 because it weakens transparent recovery and transport while requiring all tooling to
share one database protocol. It can be reconsidered only if a future ADR removes file transparency as
a product requirement and mandates API-only access for every recovery and integration path.

### Keep the nine collection files unchanged

Rejected as the long-term format because unrelated records share rewrite and conflict boundaries and
`activity.json` mixes different retention and authority semantics.

### Full event sourcing

Rejected at the current product scale. Task and objective current state remain records; only facts
whose audit/replay value justifies append-only storage become streams.

### Automatic Git/merge-driver reconciliation

Rejected because it would normalize multiple unowned writers and bypass revision-aware domain
commands. A future offline-replica product may define a semantic merge protocol in a separate ADR.

## Acceptance evidence

The decision is considered implemented only when the definition of done in
`SSOT-STORAGE-NORMALIZATION-IMPLEMENTATION-PLAN-2026-09-01.md` is satisfied. Each wave records
schema compatibility, source-byte preservation, recovery, packaged Windows, real SSH, coverage, and
CCN evidence.
