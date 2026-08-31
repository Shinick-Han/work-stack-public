# Work Stack M1 Release Gate Receipt

Date: 2026-08-30
Result: LOCAL_GATE_PASS; REMOTE_CI_NOT_RUN

## Product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Worktree: dedicated local `ui-actions` task worktree
- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `54539686f4badcf8f5a6b22ee684c69d24f09754`
- Product tree: `bfa37c1ec275e829f6c745cc9756be4e8d1ae1b8`
- Push: not performed

## Gate content

- A Windows GitHub Actions workflow installs the hash-locked Python dependency and the
  npm lockfile, then runs backend tests, frontend tests, production build, source privacy
  audit, Playwright Chromium smoke tests, and axe accessibility checks.
- Browser tests start Work Stack against a new OS temporary directory seeded from the
  tracked synthetic fixtures. The directory is disposed with the server process.
- Playwright outputs are excluded from product source and retained for seven days only
  when the remote workflow fails.
- The browser gate verifies Board selection toggle, idempotent Quick Add and authoritative
  drawer opening, and the five Graph/Board/Treemap/Focus/Inbox surfaces.
- The accessibility gate refuses serious or critical WCAG 2 A/AA and 2.1 A/AA axe
  violations. Three detected low-contrast labels were corrected; the rule was not
  disabled or excluded.

## Local clean-install evidence

- `npm ci`: 228 packages installed from lock; 0 reported vulnerabilities.
- Backend: 112 tests passed; one Windows symlink-privilege test skipped explicitly.
- Frontend: 22 files, 109 tests passed.
- Production build: passed; 910 modules; main JS 910.07 kB with the existing chunk-size
  advisory.
- Browser/accessibility: 3 Playwright tests passed in Chromium 151.0.7922.34.
- Source export audit: 166 UTF-8 files passed at the product commit.
- Disposable runtime audit: 10 UTF-8 files passed.
- `git diff --check`: passed.

## Remaining limits and nonclaims

- The workflow bytes are committed locally but have not run on GitHub because this
  branch has not been pushed.
- The gate currently targets `windows-latest` and one Chromium desktop profile; it does
  not claim macOS, Linux, Firefox, WebKit, mobile, or screen-reader certification.
- axe automation does not replace manual keyboard, zoom, high-contrast, or assistive
  technology review.
- The existing frontend code-splitting advisory remains registered for M6.
- Frozen docking contract, policy, and kit bytes were not changed. No Conduit or
  Microsoft provider call, transport, sync, send, Taskroom start, or agent start was
  added.
