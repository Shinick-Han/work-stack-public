# Structural Quality Waves F6-F10 Receipt

Date: 2026-09-01  
Scope: critical Python cyclomatic-complexity debt (`CCN > 15`)

## Outcome

The remaining critical Python complexity baseline was reduced from 10 entries to zero without changing the product schema or external contracts.

| Area | Symbol | Before | After |
| --- | --- | ---: | ---: |
| Capture | `validate_capture_packet` | 24 | 2 |
| Capture | `_project_provenance` | 16 | 3 |
| Reply receipt | `_validate_reply_receipt` | 17 | 2 |
| Reply receipt | `apply_reply_receipt` | 16 | 8 |
| Projections | `weekly_report` | 20 | 3 |
| Projections | `snapshot` | 16 | 6 |
| Store | `Store.initialize` | 19 | 4 |
| Store | `Store._migrate_v2_locked` | 19 | 1 |
| Store | `Store._validate_task_identities` | 17 | 2 |
| Updater | `parse_update_manifest` | 17 | 3 |

Critical Python `CCN > 15` debt: **10 -> 0**.

## Contract evidence

- Capture characterization covers action identifiers, deduplication, and provenance precedence.
- Reply-receipt characterization preserves validation precedence and application behavior.
- Weekly-report and snapshot characterization preserves exact aggregation and node/edge projection.
- Store characterization preserves readiness, migration, and task-identity validation behavior.
- Updater characterization preserves manifest validation and failure precedence.
- No storage schema, HTTP contract, capture packet shape, or update manifest shape was intentionally changed.

## Verification

- Full Python suite: 411 tests run, 1 skipped, all remaining tests passed.
- Mutation sentinels: 3 killed, 0 survived.
- Python bytecode compilation: passed.
- Structural quality gate: passed for 97 production files.
- Export audit: passed.
- CodeGraph index: synchronized and up to date (230 files, 4,025 nodes).

## Commits measured

- `fdab4ee` - capture packet projection decomposition
- `1d0fca9` - reply receipt processing decomposition
- `cf20640` - weekly report and graph snapshot decomposition
- `9de304c` - store readiness and migration decomposition
- `e3d239e` - update manifest validation decomposition

The zero-debt baseline is measured at `e3d239e8b2bf42c1f7f90d04bc3a7e77fe5ebeb9`.

Baseline SHA-256: `54b9ce595ed4662e7c2c041874d4cf28294d3faab23376403060719f2a232a75`

## Non-claims

This receipt closes the critical Python `CCN > 15` population that was accepted for waves F6-F10. It does not claim that every function has minimal complexity, nor does it close the independently tracked TypeScript structural-debt baseline.
