# Work Stack Structural Quality Wave C Receipt

Date: 2026-08-31
Status: IMPLEMENTED_AND_LOCALLY_VERIFIED, UNCOMMITTED

## Scope

Wave C completed the two bounded critical refactors authorized by the relaxed structural-quality plan:

1. decompose `WorkStack.patch_task` without changing its persistence, revision, planning-status, activity, or error contract;
2. replace the monolithic `Handler.do_POST` branch tree with an ordered exact/regex route registry and bounded endpoint handlers without changing the HTTP security-boundary order.

No product feature, persistence schema, docking contract, Microsoft access behavior, or user-visible UI was intentionally changed.

## Repository coordinates

- Worktree: repository-local `.worktrees/source-providers` checkout; the personal absolute prefix is intentionally omitted from this exportable receipt.
- Branch: `codex/workstack-python-desktop-20260831`
- Recorded HEAD: `fc3e64e84f8b4759d6143a792a865ff008544ba4`
- Recorded HEAD tree: `b1e1427b73f1cefda9713967d4521096bd177ca2`
- Git state at final measurement: dirty by design; Wave A, Wave B, and Wave C remain uncommitted.
- Pre-receipt porcelain leaf count: 55.

The HEAD/tree coordinates predate every uncommitted structural-quality change and are identity coordinates only. They are not a claim that the current working tree is reproducible from that commit.

## Changed paths attributable to Wave C

- `workstack/service.py`
- `workstack/server.py`
- `tests/test_patch_task_characterization.py`
- `tests/test_http_post_characterization.py`
- this receipt

## Behavior frozen before decomposition

The new characterization suites explicitly cover:

- every accepted Task patch field and its normalization;
- invalid types, enum values, date shapes, estimates, lists, objective IDs, parent IDs, and dependency IDs;
- stale revision details and byte-identical failure/no-op behavior;
- the existing compatibility behavior where a same non-status value still creates a revision;
- append-only planning-status facts and privacy-minimal `task.updated` activity;
- parent and dependency cycle refusal;
- pre-commit persistence failure;
- Host, content type, JSON, body limit, Origin, CSRF, and idempotency validation order;
- exact v1 not-found and legacy retirement envelopes;
- task-create replay and idempotency conflict behavior.

One existing compatibility detail was discovered and retained: a whitespace-only Task title reaches the older `_required_text` validator and raises `ValueError`; the HTTP layer continues to map that class through its existing `invalid_request` path.

## Structural result

- `WorkStack.patch_task`: CCN 72 at the frozen baseline, CCN 12 now.
- `Handler.do_POST`: CCN 3 now.
- `Handler._handle_v1_post`: CCN 8 now.
- Largest new POST endpoint handler: CCN 8.
- All new Task patch helpers: CCN 10 or lower.
- Ordered v1 POST registry: 20 routes.
- Structural gate: PASS, 82 production files.
- Candidate source digest: `e4c446dda2872763bc40627a3bbaeae74b2da71fa90b8e57b7519b53d18cae76`.
- Structural configuration digest: `eb24adf4b284f7e817e899b48bddb8dde89d5a04cf5743ef9d7a23670ce59083`.
- `quality/quality-config.json` SHA-256: `2bcb8df3e396229a01c0f1e4919e159748acb6df08ae8d77555568853856112b`.
- `quality/structural-baseline.json` SHA-256: `431cf3157cc3166b0c1dde9668c5d54ad306a2acd79c37625b80ece5a179df9a`.

The historical baseline still records the old `patch_task` debt against the recorded clean commit. It was deliberately not regenerated from an uncommitted tree. A future clean checkpoint may explicitly refresh it and remove that resolved allowance.

## Verification evidence

### Backend

- Full discovery: 250 tests passed.
- Existing environment skip: 1 Windows symbolic-link privilege test.
- Python coverage run: 250 tests passed, same 1 skip.
- Python statements: 4,306 of 5,877 lines covered; 70.09% line coverage.
- Python branches: 1,319 of 2,148 covered; 61.41% branch coverage.
- Proportional coverage gate: PASS.

### Frontend

- Vitest: 46 files, 231 tests passed.
- V8 coverage: 89.22% lines, 81.60% branches, 72.55% functions.
- Production build: PASS, 956 modules transformed.
- Existing non-blocking warning remains: several generated chunks exceed 500 kB after minification.

One coverage run performed concurrently with the slower pure-Python coverage job hit the existing one-second initial-render wait in one `App.test.tsx` case. The ordinary suite had already passed, and an immediate isolated full coverage rerun passed all 231 tests without changing code or timeouts. This is recorded as a load-sensitive test observation, not hidden as a pass on the failed invocation.

### Other gates

- Mutation sentinels: PASS, 3 killed and 0 survived.
- Export/privacy audit: PASS, 331 UTF-8 text files, including this receipt.
- `git diff --check`: PASS; Git emitted only existing LF-to-CRLF working-copy notices.

## Preserved boundary order

The v1 POST dispatcher still performs the externally important work in this order:

1. exact loopback Host validation;
2. bounded JSON content-type/length/UTF-8 parsing;
3. exact capture-ingest agent bearer exception, otherwise same-origin and CSRF validation;
4. idempotency-key validation for the same route population as before;
5. ordered route dispatch or the same not-found response.

The bounded-body-before-authorization behavior is intentionally retained because returning an authorization error while unread request bytes remain can reset Windows client sockets before the JSON response arrives.

## Explicit nonclaims

- No commit was created.
- No branch was pushed.
- No release artifact was rebuilt or published for Wave C.
- No remote CI, GitHub protected environment, signing identity, or public release token was exercised.
- No installer, upgrade, uninstall, browser-compatibility, or native Chromium smoke was rerun because Wave C changed only Python domain/HTTP structure and characterization tests; those release-selected gates remain enforced by Wave B.
- No planning-state migration occurred.
- No Conduit request, callback, transport, back-sync, watcher, or docking payload was added.
- No Microsoft content was accessed or retained.
