# Work Stack Structural Quality Wave D3 Receipt

Date: 2026-08-31
Status: IMPLEMENTED_AND_LOCALLY_VERIFIED, UNCOMMITTED

## Scope

Wave D3 reduced the command-line entry dispatcher selected after the HTTP GET packet. It preserved every parser command, argument mapping, output payload, Store initialization boundary, server call, file write, error message, and exit code while moving domain-specific execution out of `main`.

This was a behavior-preserving structural packet. It did not add a command, mutate a schema, change planning authority, or create a release artifact.

## Repository coordinates

- Worktree: repository-local `.worktrees/source-providers` checkout; personal absolute prefix omitted.
- Branch: `codex/workstack-python-desktop-20260831`
- Recorded HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
- Recorded HEAD tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`
- Git state: dirty by design; Waves A through D3 are uncommitted.
- Pre-receipt porcelain leaf count: 65.

The recorded commit and tree predate this working tree and are not a reproducibility claim.

## Changed paths attributable to Wave D3

- `workstack/cli.py`
- `tests/test_cli_characterization.py`
- this receipt

## Characterization added first

Eight CLI characterization tests freeze:

- capture and agent stdin forwarding without WorkStack initialization;
- all four maintenance actions and their exact emitted receipt shapes;
- backlog add argument order and task/subtask status mappings;
- OKR, worklog, weekly-report, and graph-note method dispatch;
- snapshot preview/export read-only initialization and output payloads;
- graph export bytes, demo seeding, and server invocation;
- DomainError, ValueError, and OSError stderr formats and exit code 2.

The characterization suite passed against the pre-refactor implementation before production code was changed.

## Structural result

- `main`: CCN 34 before, CCN 7 now.
- Largest extracted domain executor, `_run_backlog`: CCN 7.
- `_run_okr`: CCN 6.
- `_run_maintenance`: CCN 5.
- All other domain executors: CCN 3 or lower.
- Structural gate: PASS, 82 production files.
- Candidate source digest: `4230e7c0952a318bbf3353ccaa2b3a76c38dffd13b7db226958f409453eb5227`.
- Structural configuration digest: `eb24adf4b284f7e817e899b48bddb8dde89d5a04cf5743ef9d7a23670ce59083`.

## Preserved lifecycle boundaries

The new `main` still performs:

1. parser construction and argument parsing;
2. data-directory-specific Store construction;
3. capture/agent forwarding before WorkStack construction;
4. maintenance operations before WorkStack construction;
5. read-only WorkStack initialization for snapshot commands only;
6. domain executor dispatch;
7. stable DomainError and ValueError/OSError reporting.

## Verification evidence

- New CLI characterization: 8 tests passed before and after refactoring.
- CLI plus snapshot, agent, maintenance, planning-status, and core WorkStack set: 39 tests passed.
- Full Python coverage discovery: 270 tests passed; 1 existing Windows symlink-privilege skip.
- Python covered statements: 4,480 of 5,934, 75.50%.
- Python branch coverage: 1,371 of 2,114, 64.85%.
- Combined line/branch coverage score reported by coverage.py: 72.70%.
- Proportional coverage gate: PASS with the existing noncritical `frontend/src/main.tsx` zero-line warning.
- Mutation sentinels: PASS, 3 killed and 0 survived.
- Structural quality gate: PASS, 82 production files and no blocking findings.
- Export/privacy audit before adding this receipt: PASS, 337 UTF-8 text files.
- `git diff --check`: PASS; only LF-to-CRLF working-copy notices were emitted.

The first parallel full-coverage command used `unittest discover -t .`, which could not treat the hidden worktree path as an importable start directory. It collected no tests and did not change product state. The repository workflow's established `unittest discover -s tests` form was then used; all 270 tests passed and the coverage report was regenerated from that successful run.

The frontend was not rerun after D3 because this packet changes only Python CLI dispatch and Python tests. Wave C recorded 46 frontend files / 231 tests, coverage floors, and a production build passing in the same uncommitted working tree.

## Remaining ordered work

1. characterize and split the work-session and planning validators;
2. reassess the remaining backend hotspot ranking, including store sync/manifest code;
3. enter frontend Wave E only after the backend packet ranking is refreshed.

## Explicit nonclaims

- No commit or push occurred.
- No release artifact, installer, update manifest, or publication was created.
- No external CI or protected release environment was exercised.
- No user store, migration, restore, sync target, Microsoft session, or SSH endpoint was touched.
- No CLI command contract, API contract, UI, desktop, Conduit, or docking behavior changed.
- This packet does not claim the remaining agent/capture helpers, work-session, planning-validator, store-sync, manifest, desktop-host, or frontend composition hotspots are resolved.
