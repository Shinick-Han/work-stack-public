# Structural quality gate

Work Stack uses a proportional release gate: it prevents new structural regression without
requiring legacy hotspots to be eliminated before feature work can continue.

## Local verification

Install the locked quality dependency in addition to the product dependencies:

```powershell
python -m pip install --require-hashes -r requirements.txt
python -m pip install --require-hashes -r requirements-dev.txt
npm --prefix frontend ci
```

Run the same measurement flow as CI:

```powershell
python scripts/quality_gate.py check
python -m coverage run -m unittest discover -s tests -v
python -m coverage json
npm --prefix frontend run test:coverage
python scripts/check_coverage.py
npm --prefix frontend run build
python scripts/audit_export.py .
```

Reports are written below `.artifacts/quality/` and are retained by CI for 14 days.

## What blocks a change

- an unclassified production source file, unresolved internal import, dependency cycle, or
  forbidden layer direction;
- an expired or malformed exact architecture exception;
- a new critical Python or stable-named TypeScript function above CCN 15, or growth in an
  existing critical hotspot;
- regression below a global or critical-module coverage floor;
- changed executable coverage below 80% lines or 70% branches in a critical module;
- missing or malformed required evidence, or a quality configuration digest mismatch.

TypeScript is measured from the locked ESLint configuration in `frontend/eslint.config.js`.
`complexity` is baseline-enforced for stable function coordinates in the configured critical
paths. Anonymous callback coordinates, `max-depth`, and `max-lines-per-function` remain report
diagnostics because line-based anonymous identities are not stable enough for debt comparison.
Changed-code misses outside critical modules and zero-covered noncritical composition files are
warnings. Per-file coverage and noncritical complexity remain diagnostic. This keeps the gate
useful without turning ordinary development into baseline paperwork.

## Baseline updates

`quality/structural-baseline.json` records measurement provenance, the quality configuration
digest, source-population counts, and pre-existing critical Python and TypeScript complexity
debt. Source changes do not require the baseline source digest to remain equal. Regenerate the
baseline only when the measurement configuration or an explicitly accepted critical-debt
coordinate changes:

```powershell
python scripts/quality_gate.py baseline
python scripts/quality_gate.py check
```

Review the resulting config digest, population counts, and debt diff. Do not regenerate it merely
to silence a candidate-code failure.
