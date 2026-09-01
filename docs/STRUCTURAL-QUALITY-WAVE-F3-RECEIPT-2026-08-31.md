# Structural Quality Wave F3 receipt

Date: 2026-08-31

## Outcome

Commit `8307e60835282b64302e83f9ebebcdfb19e190a2` decomposes the frozen
planning-task snapshot validators without changing the accepted contract bytes, refusal order,
diagnostic classification, canonical serialization, safety policy, or export behavior.

## Complexity result

- `_has_duplicate_top_level_key`: CCN 34 before, CCN 9 after.
- `validate_snapshot_object`: CCN 24 before, CCN 2 after.
- `validate_snapshot_bytes`: CCN 22 before, CCN 4 after.
- `validate_text`: CCN 19 before, CCN 3 after.
- Largest extracted snapshot helper: CCN 9.
- All four snapshot critical-debt coordinates were removed from the baseline.
- Remaining Python critical complexity debt: 14 symbols.
- Updated structural baseline SHA-256:
  `4abebba17a6e8575bf9567f06f2c3913240ce633d1b05eb4fe446999a725c096`.

## Frozen contract evidence

- Contract SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`.
- Safety-policy root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`.
- Conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`.
- Valid fixtures: 2.
- Invalid fixtures: 44.
- Safety fixtures: 38.
- Text-boundary fixtures: 17.
- Shipping Unicode data version: 17.0.0.

Additional characterization fixes these precedence properties:

- a duplicate key is refused before a non-shortest revision numeric form;
- key-like text inside a JSON string is not treated as an object member;
- nested duplicate keys are refused before snapshot field-type validation;
- a CRLF envelope refusal precedes duplicate-key inspection.

## Verification

- Snapshot conformance and product-export tests: 19 passed.
- Full Python suite: 303 passed with one existing Windows symlink-privilege skip.
- Critical mutation sentinels: 3 killed, 0 survived.
- Structural quality check: passed for 92 production files.

## Nonclaims

- Frozen contract, policy, and kit bytes were not edited or reinterpreted.
- Export remains explicit, read-only, and does not contact Conduit.
- No planning-state schema or projection semantics changed.
- This does not claim that all remaining backend complexity debt is resolved.
