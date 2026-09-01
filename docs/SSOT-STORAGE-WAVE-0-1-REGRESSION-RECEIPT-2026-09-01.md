# SSOT Storage Wave 0/1 Regression Receipt

Date: 2026-09-01  
Scope: storage normalization Wave 0 and Wave 1 contracts

## Release matrix

| Contract surface | Test module |
| --- | --- |
| v4 schemas and examples | `tests.test_storage_v4_schema_artifacts` |
| canonical JSON and digests | `tests.test_storage_canonical` |
| frozen v3 behavior and fixtures | `tests.test_store_v3_contract_inventory` |
| read-only path validation | `tests.test_storage_path_validation` |
| cross-record and stream invariants | `tests.test_storage_cross_invariants` |
| runtime schema registry | `tests.test_storage_contracts_runtime` |
| CLI validation boundary | `tests.test_cli_characterization` |

Run locally with:

```powershell
python scripts/run_storage_regression.py
```

The reusable quality workflow runs the same command before the full backend
coverage suite. The existing mandatory `quality` release gate owns this matrix;
no new optional gate is introduced. `workstack/storage/**` is now both classified
in the storage architecture layer and included in the critical Python CCN set.
The structural baseline was regenerated only for that configuration change; it
contains no accepted Python or TypeScript critical complexity debt.

The matrix fails closed with exit code `1` when any named suite fails to import
or execute. Individual validators retain their own domain exit codes when run
through the product CLI.

## 2026-09-01 evidence

- focused storage matrix: 55 passed, 1 Windows symlink-privilege test skipped;
- full backend: 621 passed, 2 OS-privilege symlink tests skipped;
- full frontend: 343 passed after one transient App timing failure was reproduced
  successfully in isolation and then passed in the complete rerun;
- proportional coverage gate: passed with one existing noncritical `main.tsx`
  warning;
- new storage modules: canonical 97.1% statements / 100% branches, contracts
  97.5% / 100%, validation 91.3% / 81.6%;
- structural quality: 124 production files passed, storage maximum CCN 12;
- production frontend build: passed;
- export audit: 497 UTF-8 source-policy files passed;
- `git diff --check`: passed (checkout-policy line-ending warnings only).
