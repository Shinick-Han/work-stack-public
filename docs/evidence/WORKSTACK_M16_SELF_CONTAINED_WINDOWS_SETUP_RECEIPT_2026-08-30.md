# Work Stack M16 Self-Contained Windows Setup Receipt

Date: 2026-08-30
Result: PASS

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `47e9cdab11608466514b31f8a12c91b2ace43ee4`
- Product tree: `3b4520e9cc2aaca6e13e3fc577a0915298bca8d7`
- Push: not performed

## Shipped behavior

- The build fetches the official CPython 3.12.10 64-bit embeddable archive from python.org and
  refuses any bytes other than pinned SHA-256
  `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3`.
- The hash-locked `unicodedata2==17.0.0` wheel is expanded into the isolated runtime during
  artifact creation. The target installer does not access the network or discover host Python.
- Install, launch, stop, backup, restore and explicit setup-artifact update use
  `runtime\python.exe`. Upgrade retains a one-time fallback solely to back up an earlier
  venv-based Work Stack install before replacement.
- The updater requires an explicit `.ps1` setup path and preserves the configured data directory
  and port. It does not download or silently apply an update.
- The Python Software Foundation runtime license remains in `runtime\LICENSE.txt`; source and
  user planning data remain outside the runtime bundle.

## Artifact and clean-account-style evidence

- Setup artifact: `WorkStack-Setup-1.0.0.ps1`
- Bytes: `16,882,786`
- SHA-256: `4c4ea9f4b269cc77760d79f3539eb3db441e70f19c227fabe0ac00b7aaf006a1`
- The setup succeeded once with host Python removed from `PATH`.
- The bundled interpreter reported CPython 3.12.10 64-bit, Work Stack 1.0.0 and Unicode 17.0.0.
- First launch returned `/api/v1/health` status `ready`, API `v1`; storage reported schema 3 and
  nine store files.
- An explicit updater run preserved the synthetic custom data directory and port 8771 and created
  one verified pre-upgrade backup.
- Default uninstall removed the application and preserved `workspace.json`; the separate final
  cleanup removed only the fully resolved synthetic integration state below `LOCALAPPDATA`.

## Regression evidence

- Backend: 131 tests passed; one Windows symlink privilege case remained explicitly skipped.
- Frontend: 30 files / 133 tests passed.
- Browser: 11 Playwright scenarios passed, including axe checks on primary planning surfaces.
- Production build: passed; initial JS 490.90 kB, CSS 87.12 kB.
- Source audit after this receipt: 216 UTF-8 text files expected.
- Diff audit: passed before product commit.

## Boundaries and nonclaims

- The setup artifact is not code-signed. Windows publisher reputation and certificate-backed
  provenance remain release hardening.
- Artifact creation still requires a build environment with Python/pip and Node.js; target install
  and normal use do not.
- No silent updater, background service, cloud backup, Microsoft synchronization, Conduit client,
  watcher, relay, back-sync or bulk import was added.
- Work Stack remains the sole planning-state authority. The frozen Docking v1 contract, safety
  policy and conformance-kit bytes were not modified.
