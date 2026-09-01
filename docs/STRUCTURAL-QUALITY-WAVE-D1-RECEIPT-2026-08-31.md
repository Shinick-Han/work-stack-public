# Work Stack Structural Quality Wave D1 Receipt

Date: 2026-08-31
Status: IMPLEMENTED_AND_LOCALLY_VERIFIED, UNCOMMITTED

## Scope

Wave D1 reduced the first two remaining backend integrity hotspots selected by complexity times failure impact:

1. verified backup archive reading and semantic validation;
2. ready-store identity, migration-evidence, auxiliary-schema, activity, and planning-chain validation.

This was a behavior-preserving structural packet. It did not change backup format, store schema, migration rules, restore transaction behavior, SSOT authority, or any Work Stack/Conduit interface.

## Repository coordinates

- Worktree: repository-local `.worktrees/source-providers` checkout; personal absolute prefix omitted.
- Branch: `codex/workstack-python-desktop-20260831`
- Recorded HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
- Recorded HEAD tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`
- Git state: dirty by design; Waves A through D1 are uncommitted.
- Pre-receipt porcelain leaf count: 60.

The recorded commit and tree predate this working tree and are not a reproducibility claim.

## Changed paths attributable to Wave D1

- `workstack/maintenance.py`
- `workstack/store.py`
- `tests/test_maintenance_characterization.py`
- `tests/test_store_readiness_characterization.py`
- this receipt

## Characterization added first

Archive tests freeze:

- missing and unreadable archive refusal;
- exact member-set, duplicate-member, and directory-entry refusal;
- manifest field, schema, product version, timestamp, timezone, workspace, and files-envelope errors;
- file-record field, identity, size, and digest errors;
- semantic store, workspace identity, and schema mismatches;
- successful receipt derivation from exact archive bytes.

Ready-store tests freeze:

- metadata fields, version, current/future schema behavior, and migrations envelope;
- fresh and migrated identity evidence;
- fresh and migrated planning-status evidence;
- auxiliary and activity store shapes;
- planning-status chain projection refusal;
- byte-identical failure and stable restart readiness.

## Structural result

- `_read_verified_archive`: CCN 40 before, CCN 1 now.
- Largest extracted archive helper, `_read_archive_members`: CCN 11.
- `Store._validate_ready_state_locked`: CCN 36 before, CCN 2 now.
- Largest extracted readiness helper: CCN 8.
- Structural gate: PASS, 82 production files.
- Candidate source digest: `4c34d2f678189bbf94ca00b77f1a23ede874214e837cb045b7f3777efcd3a772`.
- Structural configuration digest: `eb24adf4b284f7e817e899b48bddb8dde89d5a04cf5743ef9d7a23670ce59083`.

## Verification evidence

- Archive/maintenance characterization and integration: 12 tests passed.
- Store readiness, identity, planning migration, recovery, and maintenance set: 41 tests passed.
- Full Python coverage discovery: 260 tests passed; 1 existing Windows symlink-privilege skip.
- Python line coverage: 4,378 of 5,914, 71.02%.
- Python statement coverage: 74.03%.
- Python branch coverage: 1,348 of 2,148, 62.76%.
- Proportional coverage gate: PASS with the existing noncritical `frontend/src/main.tsx` zero-line warning.
- Mutation sentinels: PASS, 3 killed and 0 survived.
- Export/privacy audit before adding this receipt: PASS, 333 UTF-8 text files.
- `git diff --check` for the D1 product and tests: PASS; only LF-to-CRLF working-copy notices were emitted.

The frontend was not rerun after D1 because this packet changes only Python archive/store validators and Python tests. Wave C had already recorded 46 frontend files / 231 tests and a production build passing in the same uncommitted working tree before D1.

## Preserved integrity order

Archive verification still performs:

1. archive path and compressed-size envelope;
2. exact ZIP member envelope and expanded-size bounds;
3. manifest UTF-8/JSON and exact fields;
4. manifest header semantics;
5. every member size and digest;
6. isolated temporary-store semantic initialization;
7. workspace/schema cross-check;
8. final artifact digest from exact archive bytes.

Ready-store validation still performs:

1. all required files read without repair;
2. workspace identity and Task identity/revision validation;
3. store metadata and migration evidence;
4. auxiliary stores and activity shape;
5. append-only planning-status projection;
6. readiness projection.

## Explicit nonclaims

- No commit or push occurred.
- No release artifact, installer, update manifest, or publication was created.
- No external CI or protected release environment was exercised.
- No migration or restore was run against user data.
- No runtime Microsoft or SSH connection was contacted.
- No UI or frontend behavior changed.
- This packet does not claim the remaining `do_GET`, CLI, work-session, planning validator, or frontend composition hotspots are resolved.
