# Work Stack Structural Quality Wave D2 Receipt

Date: 2026-08-31
Status: IMPLEMENTED_AND_LOCALLY_VERIFIED, UNCOMMITTED

## Scope

Wave D2 reduced the versioned HTTP GET dispatcher selected after the archive and ready-store validators. The packet introduced an explicit read-route registry and bounded per-route handlers while preserving the existing request order, response envelopes, query validation, legacy `/api/state` behavior, static-file serving, and unknown-API error behavior.

This was a behavior-preserving structural packet. It did not add an endpoint, change persistence or planning semantics, contact Conduit, or alter Microsoft/SSH integration.

## Repository coordinates

- Worktree: repository-local `.worktrees/source-providers` checkout; personal absolute prefix omitted.
- Branch: `codex/workstack-python-desktop-20260831`
- Recorded HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
- Recorded HEAD tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`
- Git state: dirty by design; Waves A through D2 are uncommitted.
- Pre-receipt porcelain leaf count: 62.

The recorded commit and tree predate this working tree and are not a reproducibility claim.

## Changed paths attributable to Wave D2

- `workstack/server.py`
- `tests/test_http_get_characterization.py`
- this receipt

## Characterization added first

The new GET characterization freezes:

- the successful data-envelope shape for every fixed and parameterized v1 read route;
- the exact objective, task, and task-snapshot path captures;
- valid query forwarding for search, review, event, work-session, and capture reads;
- rejected unsupported query strings on queryless routes;
- invalid integer, enum, and boolean query values;
- the stable JSON 404 envelope for unknown API paths.

The existing API, sync, and browser-contract suites remain the authority for legacy state, authentication/Host handling, long-polling, store-backed response content, and static serving.

## Route registry

The registry contains exactly these 14 routes:

1. `/api/v1/session`
2. `/api/v1/health`
3. `/api/v1/sync/status`
4. `/api/v1/sync/events`
5. `/api/v1/events`
6. `/api/v1/storage`
7. `/api/v1/workspace`
8. `/api/v1/search`
9. `/api/v1/review`
10. `/api/v1/work-sessions`
11. `/api/v1/objectives/{objective_id}`
12. `/api/v1/tasks/{task_id}/snapshot`
13. `/api/v1/tasks/{task_id}`
14. `/api/v1/captures`

## Preserved dispatch order

`do_GET` still performs:

1. Host validation;
2. URL parsing;
3. legacy `/api/state` handling;
4. registry dispatch for `/api/v1/*`;
5. JSON 404 for other `/api/*` paths;
6. static-file serving for non-API paths.

## Structural result

- `Handler.do_GET`: CCN 39 before, CCN 5 now.
- Registry matcher and v1 dispatcher: CCN 3 each.
- Largest extracted GET handler, `_get_review`: CCN 7.
- All other extracted GET handlers: CCN 5 or lower.
- Structural gate: PASS, 82 production files.
- Candidate source digest: `a5f5bed0734af3698ac2a10ff113313d7ca49201c9f02de9656df736335c0e05`.
- Structural configuration digest: `eb24adf4b284f7e817e899b48bddb8dde89d5a04cf5743ef9d7a23670ce59083`.
- Quality configuration SHA-256: `2bcb8df3e396229a01c0f1e4919e159748acb6df08ae8d77555568853856112b`.
- Structural baseline SHA-256: `431cf3157cc3166b0c1dde9668c5d54ad306a2acd79c37625b80ece5a179df9a`.

## Verification evidence

- GET characterization plus existing API and sync integration set: 33 tests passed.
- Full Python coverage discovery: 262 tests passed; 1 existing Windows symlink-privilege skip.
- Python covered statements: 4,412 of 5,922, 74.50%.
- Python branch coverage: 1,342 of 2,128, 63.06%.
- Combined line/branch coverage score reported by coverage.py: 71.48%.
- Proportional coverage gate: PASS with the existing noncritical `frontend/src/main.tsx` zero-line warning.
- Mutation sentinels: PASS, 3 killed and 0 survived.
- Structural quality gate: PASS, 82 production files and no blocking findings.
- Export/privacy audit before adding this receipt: PASS, 335 UTF-8 text files.
- `git diff --check`: PASS; only LF-to-CRLF working-copy notices were emitted.

The frontend was not rerun after D2 because this packet changes only Python routing and Python tests. Wave C recorded 46 frontend files / 231 tests, coverage floors, and a production build passing in the same uncommitted working tree before D1 and D2.

## Remaining ordered work

The next bounded backend packets are:

1. characterize and split `workstack.cli.main` without changing commands or exit semantics;
2. characterize and split the work-session and planning validators;
3. reassess the remaining backend hotspot ranking before entering frontend Wave E.

## Explicit nonclaims

- No commit or push occurred.
- No release artifact, installer, update manifest, or publication was created.
- No external CI or protected release environment was exercised.
- No user store, migration, restore, sync target, Microsoft session, or SSH endpoint was touched.
- No endpoint, response schema, UI, desktop, Conduit, or docking-contract behavior changed.
- This packet does not claim the remaining CLI, work-session, planning-validator, store-sync, manifest, desktop-host, or frontend composition hotspots are resolved.
