# Work Stack Docking v1 — WS1 Receipt

Date: 2026-08-29 (Asia/Seoul)

## Outcome

WS1 producer implementation and independent machine review are accepted. Work Stack now persists stable workspace and task identities, enforces explicit monotonic task revisions, migrates the complete supported v1 store roster through one recoverable journal operation, and refuses incomplete, corrupt, internally inconsistent, future-version, or revision-exhausted state without repairing it silently.

The user-visible demo is prepared separately and remains pending direct user observation. This receipt does not promote that observation gate to accepted.

## Coordinates

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Task worktree: task-scoped `docking-v1` worktree outside the product checkout
- Task branch: `codex/workstack-docking-v1-20260829`
- Starting commit: `709175563e4218bfb885c931046b4bc67721f901`
- Starting tree: `233361450be9961993b6e28a7de7dd746a5566be`
- Reviewed WS1 product commit: `95a52120ffa98d815851a7269178d622a9f857c8`
- Reviewed WS1 product tree: `b8882c4d01cd23a937b44ee5a514576300a3e442`
- Publication state: local commits only; no push was performed.

The bounded RED/implementation sequence was:

1. `092c82b023fcb624fce0e437f2d6904a453bc91a` — initial WS1 RED tests.
2. `94dd7e882290c1a26ffb20aad683b26defb7138b` — initial implementation.
3. `b4de85079ccf6eb6b8655e6f9a2cc2aeb7112a7a` — review-finding RED tests.
4. `ba2b720a4b960d21b683dce9cfefc21607b61d0d` — all-store readiness, revision safety, and UI repair.
5. `c165232` — RFC 4122 variant RED tests.
6. `cd2dc82` — RFC 4122 variant enforcement.
7. `95a52120ffa98d815851a7269178d622a9f857c8` — valid RFC 4122 test-fixture alignment and final reviewed product coordinate.

## Frozen inputs consumed without alteration

- Docking contract Revision 4 SHA-256: `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety-policy root: `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Conformance-kit root: `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

WS1 did not copy, regenerate, reinterpret, or edit these frozen bytes. They are pinned here for dependency continuity into WS4.

## Changed paths from the recorded checkpoint

- `frontend/src/app/App.test.tsx`
- `frontend/src/domain/schemas.test.ts`
- `frontend/src/domain/schemas.ts`
- `frontend/src/domain/types.ts`
- `frontend/src/features/focus/FocusPage.test.tsx`
- `frontend/src/features/focus/focusModel.test.ts`
- `frontend/src/features/tasks/TaskDrawer.test.tsx`
- `frontend/src/features/tasks/TaskDrawer.tsx`
- `frontend/src/styles.css`
- `frontend/src/test/fixtures.ts`
- `tests/test_capture.py`
- `tests/test_store_identity.py`
- `tests/test_workstack.py`
- `workstack/service.py`
- `workstack/store.py`

The backend store and service own identity, readiness, migration, and revision semantics. The frontend schema/type files remove legacy identity fallbacks. The Task Drawer exposes the reviewed stable UID and revision. The remaining test paths provide migration, restart, recovery, stale-write, corruption, overflow, and UI evidence.

## Behavior and migration evidence

- Fresh stores atomically create the complete required store roster, one stable RFC 4122 workspace UUID, and schema/migration evidence.
- New tasks persist a stable RFC 4122 UUID and explicit revision `0`.
- Supported v1 data deterministically derives missing task UUIDs with UUIDv5 from the preserved workspace UUID and legacy display ID, adds revision `0`, preserves valid existing task UUIDs, and records a digest of the complete source roster.
- The v1-to-v2 migration writes the complete roster and metadata through one recovery journal. An injected interruption after the workspace write leaves the journal; restart replays the complete accepted state and removes the journal.
- Repeated migration/startup preserves accepted bytes and identity.
- Missing auxiliary stores, malformed auxiliary JSON, missing identity, duplicate/nil/malformed/non-RFC UUIDs, missing or unsafe revisions, partial migration evidence, and future schema versions fail closed.
- Every current task mutation route uses one checked next-revision helper. A successful mutation advances exactly once; a stale, rejected, read-only, or maximum-revision mutation advances zero times and persists no planning change.
- The frontend requires a stable non-nil RFC 4122 task UUID and an explicit revision no greater than JavaScript's safe-integer limit. The Task Drawer visibly presents both values.

## Verification

Shipping runtimes observed:

- Python `3.12.10`
- Node.js `v24.19.0`
- npm `11.17.0`
- Git `2.55.0.windows.3`

Final producer gate at product commit `95a5212`:

- Targeted backend identity suite: 10 run, 10 passed, 0 failed, 0 skipped.
- Full backend suite: 77 run, 76 passed, 0 failed, 1 skipped. The skip is the pre-existing Windows symbolic-link privilege case.
- Full frontend suite: 18 files, 93 tests passed, 0 failed.
- Production frontend build: passed; 906 modules transformed. Existing Zod annotation and bundle-size warnings remain non-blocking.
- Source export audit: passed; 117 UTF-8 text files inspected.
- Diff check from the recorded checkpoint through the reviewed product commit: passed with no output.
- Task worktree after the reviewed product commit: clean.

Independent reviewer result at the exact product commit/tree: `ACCEPT`, with no remaining P0 or P1 finding. The reviewer independently reproduced reserved-variant refusal for both task and workspace UUIDs and observed byte preservation. The reviewer reran the same numeric gates. One first backend run observed an existing transient Windows `WinError 10053` in the capture size-limit API test; an isolated rerun passed 1/1 and the immediate complete rerun passed. This is retained as non-blocking P2 flake evidence rather than hidden.

## Preserved user-owned state

The original checkout remained on `codex/workstack-cloud-checkpoint-20260829` and retained exactly these six pre-existing user-owned dirty paths; none was staged or copied into WS1 commits:

- `frontend/package.json` — SHA-256 `6a0b74fcae105ebb9e7035871bb2ce05cec1e1b84a287459623abe61259eeaa4`
- `frontend/vite.config.ts` — SHA-256 `bbcec57b0555a04b7ffc1ec78e636273d2abb38bfdb04651b494d359bac1e64e`
- `frontend/mockup.html` — SHA-256 `7970dd731036cd524d6f02cbbd01682447c696d83adc5b6a5eed90130f3d4c25`
- `frontend/src/mockup/MockupApp.tsx` — SHA-256 `6a2cb242da470cfffdc5fa9dbf0129d6caa0cc00afd68ea6534ef1c58c66dbd2`
- `frontend/src/mockup/main.tsx` — SHA-256 `600f0990f99db8c89b8aba8a995083a43ea46652ad45ebc9e065615ef3d73608`
- `frontend/src/mockup/mockup.css` — SHA-256 `73a3e8ff2986d98de19826362876cbee1a8a429790fe6618a9a5df7a7d1b1a01`

## Remaining debt and explicit nonclaims

- Direct user observation of the real WS1 product demo is pending.
- The transient Windows capture-test socket reset remains P2 test-infrastructure debt.
- WS1 does not provide idempotent `POST /api/v1/tasks`; that is WS2.
- WS1 does not provide append-only planning-status facts; that is WS3.
- WS1 does not provide Unicode 17 snapshot validation, frozen safety-policy enforcement, canonical snapshot serialization, disclosure UI, or file export; those are WS4.
- No Conduit client, transport, watcher, relay, back-sync, bulk import, provider execution, or Microsoft live access was added.
- No planning export occurred, no Conduit endpoint was contacted, no external publication occurred, and no branch was pushed.

Producer state: `COMPLETED` at the pinned product commit.

Independent reviewer state: `ACCEPTED` at the pinned product commit/tree.

User-visible stage state: `PENDING_DIRECT_OBSERVATION`.
