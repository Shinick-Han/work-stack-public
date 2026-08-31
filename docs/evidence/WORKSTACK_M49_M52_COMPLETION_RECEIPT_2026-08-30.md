# Work Stack M49-M52 Completion Receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Tested aggregate commit: `499ecc07d290a7fb54e17e438b797853f4e35a5b`
Tested aggregate tree: `9b9ad12474217309494ddde48a373e4239136125`
Remote: `https://github.com/Shinick-Han/work-stack.git`
Strict CI run: `https://github.com/Shinick-Han/work-stack/actions/runs/33304485683`
Verdict: `PASS`

## Completed sequence

1. M49 retired every recognized unversioned browser writer after the existing Host, JSON,
   Origin, and CSRF boundary. Valid legacy calls return HTTP 410 without planning mutation.
2. M50 reconciled the roadmap, current status, release checklist, pushed coordinates, and known
   CI evidence instead of representing local results as remote results.
3. M51 added a separate Start-menu Windows Maintenance window for verified backup creation and
   verification, offline restore with safety backup, and source-preserving verified relocation.
4. M52 split axe scans into per-surface timeout budgets, added forced-colors and
   200%-reflow-equivalent checks, added bounded Firefox/WebKit compatibility, and made every CI
   command an independent fail-closed step.

## Bounded commits

- M49 product: `514d5835fb86c6ed3f58fefa17a8b0244323d9a8`
- M50 documentation reconciliation: `8aa1364b1c98cc9e320083baccb39b1b396a37cd`
- M50 formatting correction: `e42d0d0b05be6c2ff7dd2886f3fef44cc1c09d5a`
- M51 product: `1d3a555dcec35e24888eefba97de327ed7fded85`
- M51 receipt: `acb7df6770319c3d2aa23415592999372c499545`
- M52 initial product: `05dc87c082ca72ce73aabea16c65d6246876c4bd`
- M52 initial receipt: `0c6538938c8b0c756d61b9dfbc5aba0abc6c173d`
- M52 fail-closed hardening: `291e08678a1b986112304c0710153484d79800f9`
- Strict acceptance-boundary documentation: `499ecc07d290a7fb54e17e438b797853f4e35a5b`

## Verification evidence

- Local frontend: 35 files, 177 tests passed.
- Local backend after the new CI contract: 145 tests passed with one explicit Windows symlink
  privilege skip.
- Local production build: passed; initial JS 440.01 kB and no chunk-size warning.
- Local Chromium: 32 scenarios passed, including eight separately budgeted axe scans.
- Local WebKit: both bounded compatibility scenarios passed.
- Windows Maintenance disposable installed-layout smoke: backup verification, empty-destination
  restore, verified relocation, configuration switch after success, and source preservation passed.
- Strict remote run `33304485683`: all independent dependency install, backend, frontend, build,
  source audit, browser install, Chromium, and Firefox/WebKit steps passed. Counts were backend 145,
  frontend 177, source audit 265, Chromium 32, and Firefox/WebKit 4.
- `git diff --check`: passed at every bounded commit boundary.

## Migration, restart, and safety evidence

The full backend suite retains WS1 identity/revision migration and restart checks, WS2 idempotent
creation replay, WS3 append-only status projection/restart checks, WS4 canonical export and frozen
validator conformance, journal recovery, backup validation, restore, and relocation tests. M49-M52
did not alter those persistence or serialization contracts.

The M51 smoke used synthetic temporary data only. It did not inspect or mutate the user's actual
planning store. Replacement restore remains safety-backed and offline; relocation never deletes
the source.

## Frozen coordinates

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

Work Stack remains the sole planning-state authority. Export remains explicit, read-only, and
single-file. No Conduit client, watcher, relay, cloud transport, back-sync, bulk import, agent
start, or taskroom creation was added.

## Git and external-state receipt

At the tested aggregate commit, the task-scoped worktree was clean and the exact branch was pushed
to origin. The original checkout still contained exactly the six protected user-owned paths:

- modified `frontend/package.json`
- modified `frontend/vite.config.ts`
- untracked `frontend/mockup.html`
- untracked `frontend/src/mockup/MockupApp.tsx`
- untracked `frontend/src/mockup/main.tsx`
- untracked `frontend/src/mockup/mockup.css`

Those six paths were not reset, cleaned, staged, committed, or otherwise modified by this sequence.

## Remaining debt and nonclaims

- Outlook and Teams Gate 0 remain unverified; all four provider flags remain false.
- The Windows prototype is not publisher-signed.
- The one-file setup artifact should be rebuilt with its SHA-256 sidecar for the next distributed
  release candidate; the launcher source and installer contract are verified here.
- Automated accessibility checks are not a complete assistive-technology usability study.
- Real-time multi-user editing, persisted FTS, and automatic priority/scoring remain evidence-gated
  future work rather than current blockers.
