# Work Stack M25 Windows update checksum gate receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `98593b562405805a2789390612c9f786bf81514d`
- Product tree: `b0ddccfbe4bdbaae181ee85396ec520250ccb3de`
- Push: not performed

## Delivered behavior

- `Update-WorkStack.ps1` accepts an optional explicit checksum path and otherwise requires the
  sidecar adjacent to the selected setup artifact.
- The updater invokes `Test-WorkStackSetup.ps1` before reading `config.json` or executing setup code.
- Only a verified setup receives the preserved installed data directory, port, install root, state
  root, and shortcut choice.
- The user guide states this ordering and keeps background download/update out of scope.

## Verification

- RED-first installer contract coverage failed because the updater did not reference a checksum or
  verifier before setup invocation.
- All 7 Windows installer-bundle contract tests passed after implementation.
- Disposable positive smoke: the verified setup received the exact temporary data directory, port
  `9123`, and install root through the real updater.
- Disposable negative smoke: changed setup bytes produced `hash mismatch` and no invocation marker.
- Full backend/tool gate: 136 tests passed, one Windows symlink privilege case skipped.
- Frontend behavior was unchanged; most recent full gate remains 31 files / 141 tests,
  15 Playwright scenarios, and a passing production build.
- Source export audit: 229 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. Update verification changes no snapshot schema or canonical
bytes and adds no Conduit client, transport, watcher, relay, back-sync, bulk import, execution
inference, mutable link table, or Taskroom start.

## Nonclaims and external dependency

- A sidecar obtained beside a setup from the same untrusted source is integrity evidence, not proof
  of publisher identity.
- Trusted publisher authentication still requires an external Windows code-signing certificate and
  release policy.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
