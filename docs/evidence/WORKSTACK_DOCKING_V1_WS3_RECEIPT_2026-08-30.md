# Work Stack Docking v1 — WS3 Receipt

Date: 2026-08-30 (Asia/Seoul)

Phase: WS3 — append-only planning-status facts

Verdict: **ACCEPTED by independent review; product demo awaiting user observation**

## Reviewed product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Worktree: dedicated local `docking-v1` task worktree
- Branch: `codex/workstack-docking-v1-20260829`
- RED commit: `1b33875f20cc2a94ac0ccdb3d93b296d9bb1146c`
- Product commit: `22b4730782feef622c44b14de88d4a13f2fb79a8`
- Product tree: `98fc3731274b0272ff268c6daff4f438161b16d1`
- Publication state: local commits only; no push was performed.

## Outcome

Work Stack store schema 3 now keeps a separate append-only `planning_status` fact
stream in activity schema 2. Every Task has a durable `status_fact_id` head pointer.
The retained `task.status` field is the one-way migration/bootstrap baseline only;
all current planning-status reads are rebuilt from the validated fact chain.

The canonical fact records Task display and immutable UUID identity, predecessor,
prior/result revision, prior/result planning status, UTC occurrence time, and a
server-selected actor/provenance pair. CLI, v1 PATCH, legacy PATCH, generic
Task-from-Capture, action conversion, v1/CLI creation, migration, and demo seeding use
the same controlled model. Status transitions and their backlog head/revision update
commit through one replayable multi-file journal operation.

All 12 distinct transitions among `open`, `started`, `done`, and `dropped` are valid.
A same-status request is stale-checked and otherwise performs a byte-preserving no-op.
Non-status revision changes may create a deliberate revision gap between status facts.
Planning status remains wholly separate from Conduit execution lifecycle and outcome.

Task Detail Activity now visibly renders the status transition, revision movement,
actor, and provenance. The fallback web surface also sends an explicit displayed
revision through v1 PATCH. WS3 did not add any Conduit client or transport.

## Migration and restart evidence

- Existing supported v1 stores migrate directly to schema 3 while preserving the WS1
  identity migration evidence and appending one bootstrap fact per Task.
- Existing schema-2 stores migrate once to schema 3 and retain their prior identity
  evidence plus a separate planning-status migration digest.
- Interrupted v1 and v2 migrations recover from the exact journal and append no
  duplicate facts.
- Reopening migrated and seeded stores preserves accepted backlog/activity bytes.
- Missing/stale heads, truncated or reordered facts, identity mismatch, invalid
  provenance, corrupt tails, and raw-status authority attempts fail readiness closed
  without repair or byte rewriting.
- Independent isolated probes recovered one interrupted creation and one interrupted
  transition exactly once, and passed six corruption/refusal cases byte-preserving.

## Changed paths across WS3 RED and product commits

- `contracts/api-v1.md`
- `frontend/src/domain/types.ts`
- `frontend/src/features/tasks/TaskDrawer.tsx`
- `frontend/src/features/tasks/TaskDrawer.test.tsx`
- `tests/test_api.py`
- `tests/test_capture.py`
- `tests/test_planning_status.py`
- `tests/test_store_identity.py`
- `tests/test_web_fallback.py`
- `web/index.html`
- `workstack/planning_status.py`
- `workstack/server.py`
- `workstack/service.py`
- `workstack/store.py`

## Reproduced verification

- Focused independent backend gate: 60 run, 60 passed.
- Full backend gate: 91 run, 90 passed, 1 expected Windows symlink-privilege skip.
- Frontend gate: 18 files, 95 tests passed.
- Production build: 906 modules transformed successfully.
- Source export audit: 123 UTF-8 source files passed.
- Diff whitespace audit: passed.
- Independent review: ACCEPT; no P0 or P1 findings.
- P2 evidence note: the capture-writer and demo-seed regression additions landed in
  the GREEN commit. The genuine preceding RED commit already covered the core model,
  migration, transitions, corruption, API, fallback, and Activity UI behavior.

## Frozen inputs carried forward unchanged

- Contract SHA-256: `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety-policy root: `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Conformance-kit root: `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

## Dirty-state preservation

The task worktree was clean at independent acceptance. The original checkpoint
checkout still has exactly the six pre-existing user-owned dirty paths, with their
recorded SHA-256 values unchanged:

- `frontend/package.json` — `6a0b74fcae105ebb9e7035871bb2ce05cec1e1b84a287459623abe61259eeaa4`
- `frontend/vite.config.ts` — `bbcec57b0555a04b7ffc1ec78e636273d2abb38bfdb04651b494d359bac1e64e`
- `frontend/mockup.html` — `7970dd731036cd524d6f02cbbd01682447c696d83adc5b6a5eed90130f3d4c25`
- `frontend/src/mockup/MockupApp.tsx` — `6a2cb242da470cfffdc5fa9dbf0129d6caa0cc00afd68ea6534ef1c58c66dbd2`
- `frontend/src/mockup/main.tsx` — `600f0990f99db8c89b8aba8a995083a43ea46652ad45ebc9e065615ef3d73608`
- `frontend/src/mockup/mockup.css` — `73a3e8ff2986d98de19826362876cbee1a8a429790fe6618a9a5df7a7d1b1a01`

## Demo

- Reviewed product URL: `http://127.0.0.1:8768/?surface=workspace`
- Isolated ignored runtime: `.runtime/ws3-demo-20260830`
- Expected success: move one Task from Open to In progress, open it, choose Activity,
  and observe `Open → In progress`, `Revision 0 → 1`, and `By local.user · api.v1`.
- Expected refusal: a stale status request returns revision conflict and appends no
  fact; corruption probes fail store readiness rather than repairing history.

## Remaining debt and explicit nonclaims

- User observation of the WS3 demo remains pending and must be recorded separately.
- WS4 snapshot validation, Unicode 17 NFC implementation, safety policy, canonical
  serialization, disclosure, and explicit file export are not claimed by WS3.
- WS3 performed no export, planning-to-Conduit mutation, network call to Conduit,
  provider action, back-sync, bulk import, watcher, relay, or second planning authority.
- The demo runtime is disposable ignored local data and is not part of the product tree.
