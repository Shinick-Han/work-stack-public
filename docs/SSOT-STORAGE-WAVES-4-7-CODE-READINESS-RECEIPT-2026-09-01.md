# SSOT storage Waves 4-7 code-readiness receipt

Date: 2026-09-01  
State: code-ready behind explicit opt-in; released startup remains v3-only

## Delivered boundary

Wave 4 provides the v4 runtime placement, writer lease, replayable journal v2,
record and stream staging, manifest compare-and-swap, bounded runtime
idempotency, `WriteSession` recovery, and default-off mutation admission.
Fault tests cover partial replacement, deletion, disk-full, locked targets,
malformed journals, duplicate recovery, writer contention, and manifest
publication boundaries.

Wave 5 provides backend-neutral command boundaries and opt-in v4 repositories
for captures/replies, notes/check-ins/worklog, objectives/key results, Task
creation/scalar changes, relationships/logical deletion, planning status, work
sessions, search, and graph reads. The released `WorkStack` constructor injects
none of these backends. A composed experimental v4 domain binds every backend
to one exact authority, runtime, generation, and manifest digest. Released v3
service code now accesses semantic `WorkspaceDocument` values through a
`DocumentRepository`; the storage adapter alone maps those values to physical
filenames, paths, single writes, and atomic multi-document writes.

Wave 6 provides a disposable versioned SQLite projection. Admission requires
both the exact verified generation and manifest digest. Missing, stale,
partial, corrupt, future-schema, or semantically mismatched projections fall
back to canonical reads and never become authority. Search and graph tests
freeze released result shape, ordering, ranking, privacy, and planning-status
overlay behavior.

Wave 7 provides deterministic v4 backup, verify, and restore commands;
read-only local/SSH format inspection; default-off activation proof and restart
binding; an in-process experimental v4 HTTP canary; and released-rollout guards
that prohibit startup, installer, or updater auto-migration. Fresh released
workspaces remain v3 and existing v3 bytes remain unchanged across startup and
restart.

## Integrated verification evidence

- Focused Waves 0-7 storage matrix: 315 passed, 3 Windows privilege-dependent
  symlink tests skipped.
- Full Python suite with branch coverage: 881 passed, 4 platform-dependent
  tests skipped.
- Python coverage: 81% total over the production source set; new storage
  modules have targeted invariant and failure-branch suites.
- Frontend coverage suite: 343 passed; 91.09% statements and 83.71% branches.
- Frontend production build: passed.
- Structural quality gate: passed for 163 production files with no new accepted
  critical CCN debt.
- Proportional coverage gate: passed with one existing noncritical `main.tsx`
  zero-coverage warning.
- Source export privacy audit: passed for 579 UTF-8 source-policy files.

## Installed Windows canary evidence

An isolated Windows canary used the preserved `1.0.5` installer and a freshly
built `1.0.6` candidate. The candidate installer checksum was
`sha256:716d04796c9ffcdfecf6cc518d0b7a3f4e5ead6011bf96c772136804184ac2ea`.
All installs used per-run temporary install, state, data, backup, and port
locations; the user's installed application and configured SSOT were not used.

The canary verified all three shipping transitions:

1. successful `1.0.5 -> 1.0.6` update;
2. installer failure after payload/config swap with rollback to `1.0.5`;
3. post-install launcher restart failure with rollback to `1.0.5` and a
   terminal `rolled-back` receipt.

For each transition, `1.0.5` first created a populated released v3 workspace.
The verifier froze a sorted manifest of every SSOT file's relative path, byte
length, and SHA-256 digest, then required an exact match after update or
rollback. Configuration bytes, custom backup contents, installed version, and
the SSOT marker were checked separately. The temporary canary roots were
removed after verification.

The focused matrix is wired into the reusable quality workflow through
`scripts/run_storage_regression.py`. Its roster is frozen by
`tests/test_storage_release_matrix.py`, so a new storage contract suite cannot
be silently omitted without changing the executable release definition.

## Deliberately unclaimed work

This receipt means code-ready, not rollout-evidence complete.

- v4 reference edges are supported explicitly; the v3 adapter refuses that
  v4-only capability instead of inventing a lossy legacy representation.
- Released startup, desktop, and connection-registry activation remain v3-only.
- Real SSH latency/disconnect/reconnect, cross-machine restore, and two elapsed
  public compatibility releases require external evidence and are not
  simulated into completion here.

These gates keep the safe critical path explicit:

`installed SSH canary -> explicit user opt-in -> later new-workspace default decision`.
