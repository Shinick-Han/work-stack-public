# Structural Quality Wave F2 receipt

Date: 2026-08-31

## Outcome

Commit `16c668cfc4e5897dfcd3074a1256584c900365ae` refactors the local search
projection into bounded builders for Tasks, Objectives, Graph notes, Captures, and allowlisted
Activity metadata. The public response, ranking order, cache generation semantics, and privacy
projection are unchanged.

## Complexity result

- `WorkStack.search_projection`: CCN 36 before, CCN 4 after.
- Largest extracted search helper: CCN 7.
- Search critical-debt coordinate removed from the structural baseline.
- Remaining Python critical complexity debt: 18 symbols.
- Updated structural baseline SHA-256:
  `66de5a8ba324f3284e1b1d195ea3e05537981f0f4e0fda01df77ca0498ce92ee`.

## Behavioral evidence

The new characterization suite covers:

- stable Task → Objective → Graph note → Capture → Activity kind ordering;
- exact six-field privacy projection through the existing API test;
- refusal to search arbitrary Activity detail values;
- invalid query and boolean-limit rejection;
- warm 10,000-Task bounded search and generation-based invalidation after mutation.

Verification results:

- focused search/API tests: 5 passed;
- full Python suite: 300 passed with one existing Windows symlink-privilege skip;
- structural quality check: passed for 92 production files.

## Nonclaims

- Search does not inspect source bodies, reply bodies, recipients, or arbitrary Activity details.
- This change does not alter ranking policy or add fuzzy/full-text search.
- This change does not alter planning state, persistence formats, or docking behavior.
