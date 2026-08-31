# Work Stack Docking v1 — WS4 Closure Receipt

Date: 2026-08-30 (Asia/Seoul)

Verdict: **ACCEPTED by independent review; product demo exercised successfully**

## Outcome

Work Stack now exports one explicitly reviewed planning-task revision as the
frozen `workstack.planning-task-snapshot.v1` canonical byte sequence. The
producer uses Unicode Standard 17.0.0 deterministically, applies the frozen
high-confidence safety policy, shows the exact exported title and detail plus
the v1 omissions, requires explicit disclosure confirmation, and publishes an
exclusive user-carried file. Preview, refusal, cancellation, and successful
export do not mutate planning state or contact Conduit.

## Coordinates

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Task branch: `codex/workstack-docking-v1-20260829`
- Task worktree: dedicated local `docking-v1` worktree outside the product checkout
- Recorded checkpoint commit: `709175563e4218bfb885c931046b4bc67721f901`
- Recorded checkpoint tree: `233361450be9961993b6e28a7de7dd746a5566be`
- WS1 reviewed product commit/tree: `95a52120ffa98d815851a7269178d622a9f857c8` / `b8882c4d01cd23a937b44ee5a514576300a3e442`
- WS2 reviewed product commit/tree: `ec667421060dd24d711d48f4654d486289dd46e3` / `16be9642de711aad5ae75615363c2e98bdfe05a7`
- WS3 reviewed product commit/tree: `22b4730782feef622c44b14de88d4a13f2fb79a8` / `98fc3731274b0272ff268c6daff4f438161b16d1`
- WS4 frozen-core accepted commit/tree: `f8b4dfb7cbfd471e118858916dd029548926d3b7` / `71ba9fbdcbdd293b1691eeb61c1ae13fec2bd2d1`
- WS4 product-logic accepted commit/tree: `0d48c74afbda997f0ae62f7d091825eb6d9c59f0` / `8e2c5f3e474f1b4020171261602b5159a7682f5b`
- WS4 evidence-only test commit: `fc5ac597fa78cef52805639c3fd695a70e262cad`
- Reviewed evidence/docs HEAD/tree before this receipt: `152d338f0212164428a23c3809ff0a47a368a1ec` / `93df98f3b14ec5c7625c7fd83fb5f61cc1038203`
- Publication state: local commits only. No push was performed.

## Frozen authorities consumed without reinterpretation

- Contract SHA-256: `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety-policy root: `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Conformance-kit root: `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`
- Shipping normalization authority: `unicodedata2==17.0.0`, hash-pinned in `requirements.txt`, runtime version asserted, with license and notice retained.
- Frozen runner result: 2 valid fixtures, 44 invalid recipes, 38 safety cases,
  and 17 text-boundary cases passed, including the Unicode 17 discriminator.

## Changed paths for WS4

The complete WS3-receipt-to-WS4-docs span changes these paths:

- `README.md`
- `THIRD_PARTY_NOTICES.md`
- `contracts/api-v1.md`
- `contracts/workstack-conduit-v1/BUNDLE_ROOT.txt`
- `contracts/workstack-conduit-v1/MANIFEST.sha256`
- `contracts/workstack-conduit-v1/README.md`
- `contracts/workstack-conduit-v1/acceptance-notes.md`
- `contracts/workstack-conduit-v1/contract/CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_REVISION_4.md`
- `contracts/workstack-conduit-v1/fixtures/invalid/invalid-cases.json`
- `contracts/workstack-conduit-v1/fixtures/valid/basic.snapshot.json`
- `contracts/workstack-conduit-v1/fixtures/valid/expected-digests.json`
- `contracts/workstack-conduit-v1/fixtures/valid/unicode.snapshot.json`
- `contracts/workstack-conduit-v1/kit.json`
- `contracts/workstack-conduit-v1/safety/BUNDLE_ROOT.txt`
- `contracts/workstack-conduit-v1/safety/MANIFEST.sha256`
- `contracts/workstack-conduit-v1/safety/README.md`
- `contracts/workstack-conduit-v1/safety/snapshot-v1-safety-cases.json`
- `contracts/workstack-conduit-v1/safety/snapshot-v1-safety-policy.md`
- `contracts/workstack-conduit-v1/safety/snapshot-v1-text-boundary-cases.json`
- `contracts/workstack-conduit-v1/schema/workstack.planning-task-snapshot.v1.schema.json`
- `docs/WORKSTACK_DOCKING_V1_USER_GUIDE_2026-08-30.md`
- `frontend/src/api/client.ts`
- `frontend/src/components/Dialog.tsx`
- `frontend/src/domain/schemas.ts`
- `frontend/src/domain/types.ts`
- `frontend/src/features/tasks/SnapshotExportDialog.test.tsx`
- `frontend/src/features/tasks/SnapshotExportDialog.tsx`
- `frontend/src/features/tasks/TaskDrawer.test.tsx`
- `frontend/src/features/tasks/TaskDrawer.tsx`
- `frontend/src/styles.css`
- `licenses/unicodedata2-17.0.0-LICENSE.txt`
- `requirements.txt`
- `scripts/audit_export.py`
- `tests/test_audit_export.py`
- `tests/test_snapshot_product_export.py`
- `tests/test_snapshot_v1.py`
- `workstack/cli.py`
- `workstack/server.py`
- `workstack/service.py`
- `workstack/snapshot.py`
- `workstack/snapshot_conformance.py`
- `workstack/snapshot_export.py`
- `workstack/snapshot_safety.py`
- `workstack/store.py`
- `workstack/unicode17.py`

## Final machine gates

At evidence/docs HEAD `152d338f0212164428a23c3809ff0a47a368a1ec`:

- Backend: 107 tests run; 106 passed; 1 expected Windows symlink-permission skip; 0 failures.
- Frontend: 19 test files; 97 tests passed; 0 failures.
- Production build: passed; 907 modules transformed.
- Source/privacy audit: passed; 154 UTF-8 source-policy files audited.
- `git diff --check`: passed with no output.
- Task worktree: clean before creation of this receipt.
- Independent review: **ACCEPT**, with no remaining P0 or P1.

The build retains its pre-existing non-blocking large-chunk warning. The audit
process also emitted unrelated PowerShell profile deserialization noise while
returning exit code 0 and the explicit `EXPORT AUDIT PASSED` result.

## Migration, restart, failure, and no-mutation evidence

- WS1 migrates the complete supported store roster, stable workspace/task UUIDs,
  and initial task revisions through one replayable journal operation. Injected
  interruption followed by restart replays the complete accepted state once;
  repeated startup preserves the resulting identities and bytes.
- WS2 commits a new Task and its idempotency evidence atomically. Response-loss,
  process restart, user retry, and two concurrent requests replay one frozen
  creation response and create one Task.
- WS3 migrates and appends planning-status facts atomically. Restart preserves the
  append-only chain and projected status; truncation or reordering fails closed.
- WS4 preview and export use one leased consistent read. A pending journal causes
  the real CLI to exit 2 with `SNAPSHOT_STORE_NOT_READY`; it emits no preview or
  output file, performs no replay, migration, repair, or cleanup of the journal,
  and leaves every store and journal byte-identical.
- Canonical bytes and digest remain deterministic across a new process/restart.
  A committed planning edit advances revision and changes the bytes and digest.
- Missing confirmation, stale revision, stale digest, unsafe content, collision,
  and too-long output paths refuse without planning-state mutation or partial file.
- Exclusive publication never overwrites an existing target and removes temporary
  material after failure.
- The loopback integration test proves that the successful response body is exactly
  the reviewed canonical bytes.

## Reviewed product demo

The user-visible flow was exercised against the built product at
`http://127.0.0.1:8769/?view=board` with deterministic demo data:

1. Opened `T-0004` and recorded its stable UID, `Revision 0`, and `open` status.
2. Opened **Export to Conduit** and verified the exact title, exact detail,
   snapshot/copy/no-back-update statements, execution confirmation statement,
   and all five omissions.
3. Verified **Save snapshot file** was disabled before disclosure confirmation.
4. Cancelled and verified UID, revision, and status were unchanged.
5. Reopened, checked the explicit confirmation, and saved the snapshot.
6. Verified the dialog closed, UID/revision/status remained unchanged, and the
   browser console contained zero errors.

The click-by-click guide is `docs/WORKSTACK_DOCKING_V1_USER_GUIDE_2026-08-30.md`.

## Preserved user-owned state

The original checkout remains on `codex/workstack-cloud-checkpoint-20260829` at
the recorded checkpoint commit/tree. Its exact six pre-existing dirty paths were
not copied, staged, committed, reset, cleaned, stashed, or overwritten:

- `frontend/package.json` — `6a0b74fcae105ebb9e7035871bb2ce05cec1e1b84a287459623abe61259eeaa4`
- `frontend/vite.config.ts` — `bbcec57b0555a04b7ffc1ec78e636273d2abb38bfdb04651b494d359bac1e64e`
- `frontend/mockup.html` — `7970dd731036cd524d6f02cbbd01682447c696d83adc5b6a5eed90130f3d4c25`
- `frontend/src/mockup/MockupApp.tsx` — `6a2cb242da470cfffdc5fa9dbf0129d6caa0cc00afd68ea6534ef1c58c66dbd2`
- `frontend/src/mockup/main.tsx` — `600f0990f99db8c89b8aba8a995083a43ea46652ad45ebc9e065615ef3d73608`
- `frontend/src/mockup/mockup.css` — `73a3e8ff2986d98de19826362876cbee1a8a429790fe6618a9a5df7a7d1b1a01`

## Remaining debt

- P2 defense-in-depth: the browser verifies the returned digest header and
  filename but does not independently hash the received Blob. The server path
  and loopback integration test nevertheless prove exact response bytes.
- P2 delivery optimization: the production bundle remains larger than Vite's
  default chunk warning threshold.
- The optional pre-existing `scripts/render_qr.py` still has no declared
  `qrcode` dependency and is outside docking v1.

## Explicit nonclaims

This receipt does **not** claim that Work Stack:

- sends a snapshot to Conduit or imports anything into Conduit;
- contains a Conduit client, loopback transport, watcher, cloud relay,
  back-sync, or bulk importer;
- lets Conduit or another system mutate Work Stack planning state;
- contacts Microsoft, Teams, Outlook, SharePoint, or any other provider during
  snapshot preview/export;
- exports objectives, dependencies, subtasks, notes, or tags in snapshot v1;
- provides comprehensive secret detection beyond the frozen narrow,
  high-confidence safety policy;
- has been pushed, merged, released, packaged as an installer, or observed on a
  remote host.

WS1 through WS4 are complete within the written docking-v1 scope. Any push or
merge remains a separate, exact-branch authorization decision.
