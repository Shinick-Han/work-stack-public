# Work Stack Structural Quality Wave D4 Receipt

Date: 2026-08-31
Status: IMPLEMENTED_AND_LOCALLY_VERIFIED, UNCOMMITTED

## Scope

Wave D4 reduced two fail-closed persistence validators selected by complexity times integrity impact:

1. append-only planning-status fact validation and projection;
2. persisted work-session record validation.

The packet preserved validation order, accepted values, error messages, append-only head semantics, segment chronology rules, single-active-session enforcement, and returned object identity/order. It did not repair malformed data or change storage bytes.

## Repository coordinates

- Worktree: repository-local `.worktrees/source-providers` checkout; personal absolute prefix omitted.
- Branch: `codex/workstack-python-desktop-20260831`
- Recorded HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
- Recorded HEAD tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`
- Git state: dirty by design; Waves A through D4 are uncommitted.
- Pre-receipt porcelain leaf count: 69.

The recorded commit and tree predate this working tree and are not a reproducibility claim.

## Changed paths attributable to Wave D4

- `workstack/planning_status.py`
- `workstack/service.py`
- `tests/test_planning_status_characterization.py`
- `tests/test_work_session_records_characterization.py`
- this receipt

## Characterization added first

Planning-status characterization freezes:

- valid bootstrap/transition projection;
- store, task, fact-schema, fact-order, type, identity, status, timestamp, and provenance refusal;
- bootstrap, source-baseline, predecessor, revision, prior-status, same-status, fact-bound, and stale-head refusal;
- exact validation-error messages.

Work-session characterization freezes:

- valid running, paused, and stopped records, insertion order, and object identity;
- day/session envelope, ID uniqueness, date/task identity, title, state, worklog-state, and timestamp refusal;
- segment schema, overlap, open-position, negative-duration, and state/open-segment consistency refusal;
- single-active-session enforcement and exact corruption messages.

Both characterization suites passed against the pre-refactor implementations before production code was changed.

## Structural result

- `validate_and_project`: CCN 32 before, CCN 3 now.
- Largest planning-status helper: CCN 6.
- `_work_session_records`: CCN 34 before, CCN 5 now.
- Largest work-session helper, `_work_session_header`: CCN 13, within the current critical threshold.
- Work-session segment helper: CCN 8; segment-collection validator: CCN 7.
- Structural gate: PASS, 82 production files.
- Candidate source digest: `a6a7c4e5adf9f4df3f33cf055daa112ff0a91c1b3a06e5fabbd09c8092bcb4e3`.
- Structural configuration digest: `eb24adf4b284f7e817e899b48bddb8dde89d5a04cf5743ef9d7a23670ce59083`.

## Preserved planning validation order

1. facts list and task index shape;
2. exact fact fields, sequential ID, and fact type;
3. task identity and accepted status;
4. timestamp and actor/provenance pair;
5. bootstrap or transition-chain semantics;
6. task revision upper bound;
7. task head completeness and projection.

## Preserved work-session validation order

1. days envelope and canonical review date;
2. day and sessions envelopes;
3. session shape, unique ID, date, task identity/title, state, and worklog state;
4. session timestamps;
5. segment shape and chronological chain;
6. state/open-segment consistency;
7. at-most-one active session across all days.

## Verification evidence

- New planning and work-session characterization: 10 tests passed before and after refactoring.
- Related planning, store, work-session, API, maintenance, and core service set: 45 tests passed after refactoring.
- Full Python coverage discovery: 280 tests passed; 1 existing Windows symlink-privilege skip.
- Python covered statements: 4,550 of 5,970, 76.21%.
- Python branch coverage: 1,401 of 2,112, 66.34%.
- Combined line/branch coverage score reported by coverage.py: 73.63%.
- Proportional coverage gate: PASS with the existing noncritical `frontend/src/main.tsx` zero-line warning.
- Mutation sentinels: PASS, 3 killed and 0 survived.
- Structural quality gate: PASS, 82 production files and no blocking findings.
- Export/privacy audit before adding this receipt: PASS, 340 UTF-8 text files.
- `git diff --check`: PASS; only LF-to-CRLF working-copy notices were emitted.

One concurrent PowerShell process emitted a host-profile `Import-Clixml` warning before the successful structural/export commands. Those Python commands returned success and their results were independently visible. The warning was not emitted by Work Stack code and did not alter repository files.

The frontend was not rerun after D4 because this packet changes only Python validators and Python tests. Wave C recorded 46 frontend files / 231 tests, coverage floors, and a production build passing in the same uncommitted working tree.

## Remaining ordered work

1. refresh hotspot ranking rather than automatically continuing the stale initial list;
2. consider store manifest/sync validators next because they remain high-complexity integrity boundaries;
3. enter frontend Wave E after a bounded backend stopping decision.

## Explicit nonclaims

- No commit or push occurred.
- No release artifact, installer, update manifest, or publication was created.
- No external CI or protected release environment was exercised.
- No user store, migration, restore, sync target, Microsoft session, or SSH endpoint was touched.
- No persistence schema, planning semantics, API, CLI, UI, desktop, Conduit, or docking behavior changed.
- This packet does not claim the remaining store-sync, manifest, identity, journal, migration, agent/capture, desktop-host, or frontend composition hotspots are resolved.
