# Work Stack M17 Optional QR Tooling Receipt

Date: 2026-08-30
Result: PASS

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `cbf5f7184b02ffef52918616db4097843872a8e6`
- Product tree: `8e9841fa2c18ed6e94ee0863d888324156da33af`
- Push: not performed

## Closed debt

- `render_qr.py --help` and `restore_from_png.py --help` no longer import optional image packages
  before argument parsing.
- `requirements-qr-windows.txt` pins qrcode 8.2, Pillow 12.3.0, zxing-cpp 3.1.1 and the Windows
  colorama dependency with exact wheel SHA-256 hashes.
- QR frame and image output directories must be new or empty. The tools no longer recursively
  delete a caller-supplied directory containing other files.
- The README identifies SQR1 as an offline optional repository-transfer aid, not a Work Stack
  product runtime or Docking interface.

## Round-trip evidence

- Source: tracked `contracts` directory.
- Deterministic ZIP: 47,081 bytes.
- Frame payload: 40 text frames at 1,200 chunk bytes.
- Rendered output: 40 PNG frames plus `contact-sheet.png`.
- Decoder result: exact byte equality.
- SHA-256 before and after:
  `6d50e3d7d8001ee52070dfc0e40b6d8a6f8d846c272c18eb4c98d07a6921a3cb`.

## Regression evidence

- Backend and tooling: 134 tests passed; one Windows symlink privilege case explicitly skipped.
- Frontend: the previously completed full gate remains 30 files / 133 tests.
- Browser: the previously completed full gate remains 11 Playwright scenarios.
- Source audit after this receipt: 219 UTF-8 text files expected.
- Diff audit: passed before product commit.

## Boundaries and nonclaims

- Optional QR dependencies are not bundled in `WorkStack-Setup-1.0.0.ps1` and are not required to
  install or use Work Stack.
- SQR1 does not replace the frozen single-file Work Stack → Conduit snapshot export contract.
- No Microsoft capability, Conduit client, transport, watcher, back-sync or planning-state mutation
  was introduced. Frozen contract, safety-policy and conformance-kit bytes were not modified.
