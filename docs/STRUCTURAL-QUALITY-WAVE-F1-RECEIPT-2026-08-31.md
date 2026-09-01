# Structural Quality Wave F1 receipt

Date: 2026-08-31
Starting commit: `2d55de1daf6db8b16b2c6372cc77ac56295d3880`

## Outcome

The release-owned structural gate now measures TypeScript and TSX control-flow complexity with
the locked ESLint toolchain. Stable named functions in configured critical frontend paths use
the same proportional rule as Python: a new function above CCN 15 or an increase in an accepted
hotspot fails the gate. Existing debt remains usable while it is reduced deliberately.

Anonymous callback coordinates, nesting depth, and function length are retained as diagnostics.
They do not block releases because anonymous line coordinates are not stable debt identities and
size alone is not a correctness signal.

## Frozen measurement

- Production files classified: 92.
- Python critical complexity debt: 19 symbols, reduced from the prior 26-symbol baseline because
  seven previously accepted hotspots have already been refactored.
- TypeScript complexity findings above CCN 15: 21.
- Stable critical TypeScript debt coordinates: 13.
- TypeScript depth diagnostics: 2.
- TypeScript function-length diagnostics: 21.
- Quality configuration digest:
  `ebb19d826960ffc0f607efe3c0d4deebd9d5d6abff480c3ea2b16bde18416805`.
- Structural baseline SHA-256:
  `fa8f219357155c76ccfbd5c19fb150b08890070d3331bbdb83c77e97c9df9144`.
- Local generated report SHA-256:
  `fd46f152c2ea6bd43ec99ed779dc7f8ef4f459f1a038fd9c003628fc9eea37b2`.

The generated report remains under ignored `.artifacts/quality/`; it is not product state.

## Verification

- Quality-gate unit tests: 11 passed.
- Full Python suite: 297 passed with one existing Windows symlink-privilege skip.
- Frontend suite: 252 passed across 56 files.
- Production TypeScript/Vite build: passed; 966 modules transformed.
- Locked `npm ci`: passed; 364 packages audited, zero vulnerabilities.
- Structural quality check: passed for 92 production files.
- Export audit: passed for 375 UTF-8 source-policy files.

One first full frontend-suite run reported the theme test before its expected heading appeared.
The test passed in isolation and the complete 252-test suite passed on immediate rerun. No product
code was changed to mask it; this timing/isolation flake remains visible operational debt.

## Release behavior

CI already installs the locked frontend dependency graph before invoking
`scripts/quality_gate.py`, so the new measurement is part of the existing reusable quality gate
without a parallel release path. Windows subprocess output is decoded explicitly as UTF-8, which
prevents the host CP949 locale from corrupting ESLint JSON.

## Nonclaims

- This does not claim that CCN alone proves design quality or test completeness.
- This does not make anonymous callbacks or size diagnostics release blockers.
- This does not eliminate the 13 accepted frontend hotspots.
- This does not alter Work Stack planning behavior, persistence, docking contracts, or Microsoft
  source-provider behavior.
