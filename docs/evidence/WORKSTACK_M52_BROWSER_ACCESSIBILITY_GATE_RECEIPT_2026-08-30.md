# Work Stack M52 Browser and Accessibility Gate Receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Initial product commit: `05dc87c082ca72ce73aabea16c65d6246876c4bd`
Fail-closed hardening commit: `291e08678a1b986112304c0710153484d79800f9`
Hardened product tree: `3ca0b6dac9b264988e70824036ac748a6f8f11aa`
Status: `PASS`

## Outcome

The expanded gate is implemented and locally verified where the host permits execution. The prior
single test scanned eight pages under one 30-second budget; the remote run therefore timed out on
the aggregate axe call after its first 22 product scenarios had passed. M52 gives each surface an
independent test timeout and retains the same serious/critical WCAG tag policy.

The full product flow remains Chromium-only. The additional compatibility suite is intentionally
bounded to two high-value, read-only interactions: Board Task select/deselect with the shared
drawer and keyboard navigation from a Graph Objective to the canonical Objective Hub. CI runs this
small suite in Firefox and WebKit rather than multiplying the complete mutation suite by three.

## Changed product paths

- `.github/workflows/ci.yml`
- `frontend/e2e/workstack.spec.ts`
- `frontend/e2e/compatibility.spec.ts`
- `frontend/playwright.config.ts`
- `frontend/playwright.compat.config.ts`
- `frontend/package.json`
- `tests/test_browser_gate_contract.py`

## RED-first and local verification evidence

- Historical RED: GitHub Actions run `33302786618` passed backend, frontend, build, source audit,
  and 22 Chromium product scenarios, then failed when the aggregate eight-surface axe test exceeded
  30 seconds on both attempts.
- New browser-gate contract tests were run before implementation and failed for all four missing
  properties: split axe cases, Firefox/WebKit config, forced-colors/reflow checks, and CI wiring.
- Browser-gate contract after implementation: 4 passed.
- Frontend unit gate: 35 files, 177 tests passed.
- Backend gate: 144 passed, 1 skipped. The skip is the explicit Windows symlink privilege case.
- Production build: passed; initial JS remains 440.01 kB with no chunk-size warning. The existing
  Zod annotation-removal warning remains non-blocking.
- Chromium: 32 of 32 passed in about one minute. This includes 22 established product scenarios,
  forced-colors focus/visibility, a 640x480 200%-reflow-equivalent viewport, and eight separately
  budgeted axe scans.
- WebKit: 2 of 2 compatibility scenarios passed.
- Source export audit before this receipt: 264 UTF-8 text files passed.
- `git diff --check`: passed before the product commit.

## First remote run and fail-closed correction

GitHub Actions run `33304031482` at `0c6538938c8b0c756d61b9dfbc5aba0abc6c173d`
proved that the expanded browser suite runs on the Windows runner: 32 Chromium scenarios passed in
2.6 minutes and all four Firefox/WebKit compatibility scenarios passed in 24.4 seconds. Backend,
build, source audit, and browser-install steps also completed.

Full-log review then found that one frontend unit test had timed out while loading the first lazy
Workspace chunk. The workflow had placed frontend test plus build in one PowerShell step; the later
successful build replaced the earlier non-zero process exit. The browser step had the same masking
risk. The run's green conclusion is therefore not used as aggregate release acceptance.

Commit `291e08678a1b986112304c0710153484d79800f9` separates locked Python install, locked frontend
install, frontend tests, build, Chromium, and Firefox/WebKit into independent Actions steps. A new
contract regression prevents recombining those gates. The first lazy Workspace assertion now has a
bounded five-second wait; its 16-test file passed three consecutive focused runs and the full local
35-file/177-test suite passed afterward.

Strict GitHub Actions run `33304485683` at aggregate commit
`499ecc07d290a7fb54e17e438b797853f4e35a5b` then passed every independent step in 7 minutes
28 seconds: 145 backend tests with one explicit symlink skip, 35 frontend files with 177 tests,
production build, 265-file source audit, 32 Chromium product/accessibility scenarios, and four
Firefox/WebKit compatibility scenarios. This run is the remote acceptance evidence.

## Explicit local Firefox limitation

The local Playwright Firefox archive downloaded successfully. Both `firefox.exe` and
`mozglue.dll` were present in the pinned runtime directory. Windows then refused to start that
downloaded executable under the host application-control policy before any Work Stack page or
test code ran. Playwright surfaced the launch refusal as a missing-dependency error.

This receipt does not count Firefox as locally passed or silently skip it. Strict `windows-latest`
run `33304485683` supplies the independent Firefox acceptance evidence.

## Frozen docking coordinates preserved

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

No planning writer, canonical serialization, snapshot export, provider gate, or Conduit boundary
changed.

## Nonclaims

- These automated gates are not a complete screen-reader usability study.
- The reflow check verifies essential controls at the CSS viewport corresponding to 200% desktop
  zoom; it is not evidence for every OS scaling and display combination.
- Firefox is not locally claimed as passed; its acceptance evidence is the independent strict CI run.
- M52 does not enable Microsoft lanes, sign the Windows artifact, or add any Conduit transport.

The task-scoped worktree was clean at the product commit. The six protected user-owned paths in the
original checkout were not staged, reset, committed, or modified by M52.
