# Work Stack Docking v1 — WS2 Receipt

Date: 2026-08-29

Phase: WS2 — Idempotent Task Creation API

Verdict: **ACCEPT**

## Reviewed product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Task branch: `codex/workstack-docking-v1-20260829`
- Product commit: `ec667421060dd24d711d48f4654d486289dd46e3`
- Product tree: `16be9642de711aad5ae75615363c2e98bdfe05a7`
- WS2 base: `7ac3fa367274317b1f20727518782362ffec52a5`
- Push: not performed; separate exact-branch authorization is still required.

The reviewed task worktree was clean. The separate main checkout remained at the
recorded checkpoint and retained exactly its six user-owned dirty paths.

## Bounded commit sequence

1. `30e544e` — RED: define the idempotent Task-creation gate.
2. `32c96f4` — GREEN: implement strict v1 creation and migrate Quick Add.
3. `53c8012` — RED: reproduce fallback response-loss risk found by independent review.
4. `ec66742` — GREEN: retain one fallback create identity through transport and user retry.

No history was rewritten or squashed.

## Product outcome

- `POST /api/v1/tasks` is the only browser Task-create writer.
- The endpoint requires a valid `Idempotency-Key` and a strict, allow-listed JSON body.
- Validation canonicalizes only the documented Task-create fields before computing the
  request identity.
- The first accepted request returns HTTP 201 with `meta.replayed=false`.
- An exact replay returns HTTP 200 with the frozen original Task projection and
  `meta.replayed=true`, even after a later Task edit or process restart.
- Reusing a key with a different canonical request returns HTTP 409
  `idempotency_conflict` without a second Task.
- Backlog and idempotency evidence commit through one recovery-journal operation.
- Legacy `POST /api/tasks` returns HTTP 410 `legacy_task_writer_disabled`.
- React Quick Add creates one key per visible submit, retains it through network and
  CSRF retries, and blocks duplicate submission while active.
- The served legacy fallback uses the same v1 route, retains the pending key after an
  ambiguous transport failure, and reuses it on explicit user retry.

## Changed product and test paths

- `contracts/api-v1.md`
- `frontend/src/api/client.test.ts`
- `frontend/src/api/client.ts`
- `frontend/src/app/App.test.tsx`
- `tests/test_api.py`
- `tests/test_task_creation_v1.py`
- `tests/test_web_fallback.py`
- `web/index.html`
- `workstack/server.py`
- `workstack/service.py`

## Verification evidence

At product commit `ec667421060dd24d711d48f4654d486289dd46e3`:

- Focused backend: 6 of 6 passed.
- Full backend: 83 run; 82 passed; 1 skipped because Windows did not grant symbolic-link
  privilege. There were no failures.
- Full frontend: 18 files; 94 of 94 tests passed.
- Production build: passed; 906 modules transformed.
- Source export audit: passed; 120 UTF-8 source-policy files inspected.
- `git diff --check`: clean.
- Task-worktree status: clean.

The production build retained the known third-party annotation warnings and the existing
large-chunk advisory. Neither warning is introduced by WS2.

## Restart, recovery, and concurrency evidence

WS2 does not add a store-schema migration. It persists Task-create idempotency records in
the existing `activity.json` roster and commits them atomically with `backlog.json`.

Automated and independent probes demonstrated:

- a committed response replayed as the same frozen Task after constructing a new
  `WorkStack` instance over the same data directory;
- an injected interruption after the backlog write left a valid journal, and restart
  recovered exactly one Task and one idempotency record before returning HTTP 200 replay;
- two concurrent identical requests produced one HTTP 201 and one HTTP 200, with one Task;
- concurrent different bodies using one key produced one HTTP 201 and one HTTP 409, with
  one Task and one record;
- two simulated fallback transport failures followed by explicit user retry made three
  POST attempts with the same key and body, then cleared the key only after replay.

## Independent review

The independent reviewer returned **ACCEPT** for the exact product commit and tree above,
with no remaining P0, P1, or P2 WS2 finding. The reviewer performed no implementation,
staging, commit, installation, server launch, or push.

## Frozen docking inputs

- Contract SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety-policy bundle root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

Their bytes were not altered or reinterpreted in WS2.

## Protected user-owned state

The six protected main-checkout paths remained outside every docking commit and retained
their recorded SHA-256 values:

- `frontend/package.json` — `6a0b74fcae105ebb9e7035871bb2ce05cec1e1b84a287459623abe61259eeaa4`
- `frontend/vite.config.ts` — `bbcec57b0555a04b7ffc1ec78e636273d2abb38bfdb04651b494d359bac1e64e`
- `frontend/mockup.html` — `7970dd731036cd524d6f02cbbd01682447c696d83adc5b6a5eed90130f3d4c25`
- `frontend/src/mockup/MockupApp.tsx` — `6a2cb242da470cfffdc5fa9dbf0129d6caa0cc00afd68ea6534ef1c58c66dbd2`
- `frontend/src/mockup/main.tsx` — `600f0990f99db8c89b8aba8a995083a43ea46652ad45ebc9e065615ef3d73608`
- `frontend/src/mockup/mockup.css` — `73a3e8ff2986d98de19826362876cbee1a8a429790fe6618a9a5df7a7d1b1a01`

## Remaining debt and explicit nonclaims

- Direct user observation of the prepared WS2 browser demo is pending.
- WS3 append-only planning-status facts and projection are not implemented by WS2.
- WS4 Unicode 17 validation, frozen safety-policy adapter, canonical snapshot
  serialization, disclosure UI, and explicit file export are not implemented by WS2.
- WS2 does not add a Conduit client, transport, watcher, relay, synchronization,
  back-sync, bulk import, provider execution, or second planning-state authority.
- WS2 does not claim that export exists or that any Work Stack state can yet be imported
  into Conduit.
