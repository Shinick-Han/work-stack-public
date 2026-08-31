# Work Stack M22 Windows setup checksum receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `5dc0ce63b0f80f974af3bad87ee9408795c96ab5`
- Product tree: `4f0cfa097b81b00ba8051decef3127cf470e65c3`
- Push: not performed

## Delivered behavior

- `Build-WindowsInstaller.ps1` writes `<setup>.sha256` as one UTF-8-without-BOM line containing the
  lowercase SHA-256, two spaces, and the exact setup filename.
- `Test-WorkStackSetup.ps1` requires an explicit setup path, defaults to the adjacent sidecar, and
  rejects malformed sidecars, a different manifest filename, and digest mismatch.
- The install guide puts verification before execution and explicitly states that a checksum is
  not publisher authentication or Windows code signing.

## Verification

- RED-first Windows bundle contract gate failed for the missing sidecar and verifier, then passed
  all 7 installer-bundle tests after implementation.
- Full backend/tool gate: 136 tests passed, one Windows symlink privilege case skipped.
- Rebuilt setup artifact: 17,377,431 bytes.
- Sidecar: 92 bytes.
- Verified setup SHA-256:
  `5c74398160951af2544529e6cb46e45f42e26b8ae24e36ebd0341c27b8fb5184`.
- Actual verifier positive gate passed against the rebuilt artifact.
- Actual negative gates rejected both changed setup bytes and a sidecar naming another setup file.
- Frontend behavior was unchanged; most recent full gate remains 31 files / 138 tests,
  14 Playwright scenarios, and a passing production build.
- Source export audit: 226 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract, safety policy, and conformance-kit bytes were not changed. Work Stack remains
the sole planning-state authority. Setup verification adds no Conduit client, transport, watcher,
relay, back-sync, bulk import, execution inference, mutable link table, or Taskroom start. Snapshot
export behavior and bytes are unchanged.

## Nonclaims and external dependency

- SHA-256 sidecars do not prove publisher identity when obtained from the same untrusted channel.
- The prototype remains unsigned; trusted Windows publisher authentication requires an external
  code-signing certificate and release-channel policy.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
