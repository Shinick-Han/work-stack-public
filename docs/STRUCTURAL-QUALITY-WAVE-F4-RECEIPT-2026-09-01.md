# Structural Quality Wave F4 receipt

Date: 2026-09-01

## Outcome

Commit `62c78ec7def3043c9a62d0b525a3bbfb025065e9` decomposes the two highest
remaining Task-creation mutation hotspots without changing validation precedence, normalized
payloads, relationship handling, idempotency, bootstrap activity, or persistence behavior.

## Complexity result

- `WorkStack._validate_task_create_v1`: CCN 28 before, CCN 1 after.
- `WorkStack._append_task`: CCN 25 before, CCN 3 after.
- Largest extracted helper: CCN 9.
- Measured Python critical complexity debt: 13 symbols before, 11 after.
- The refreshed baseline also removes the already-resolved stale
  `WorkStack.create_task_from_capture` coordinate; that reduction is not attributed to F4.
- The refreshed TypeScript baseline removes the already-resolved stale `SourceCaptureDialog`
  coordinate and records the existing `App` decrease from CCN 79 to 78; F4 changes no frontend
  production source.
- Updated structural baseline SHA-256:
  `ea5ee505bfd7b9ab112f2546fb7fa5e74557a9801bf58fb3fb372973b58977f5`.

## Frozen behavior evidence

Characterization now fixes these validation and normalization properties:

- strict Task-create refusal order remains unknown fields, title, detail, priority, due,
  scheduled, estimate, tags, and Objective IDs;
- every refused create leaves planning and idempotency bytes unchanged;
- scheduled date, estimate, tags, Objective IDs, title, and detail retain their canonical forms;
- low-level Task append still validates priority and dates before relationships, relationships
  before title, and title before Objective references;
- parent and dependency IDs remain trimmed, uppercased, deduplicated, and deterministically sorted;
- successful CLI and API creation still append one bootstrap planning-status fact atomically;
- exact replay, response-loss restart replay, conflict detection, and concurrent-intent collapse
  remain unchanged.

## Verification

- Focused Task creation, planning-status, Capture, intent, and aligned-flow tests: 58 passed.
- Full Python suite: 404 passed with one existing Windows symlink-privilege skip.
- Critical mutation sentinels: 3 killed, 0 survived.
- Python compileall: passed.
- Structural quality check: passed for 97 production files.

## Nonclaims

- No Task schema, ID allocation, UID derivation, projection, or API response changed.
- No frontend, Microsoft source, remote SSH, updater, installer, or SSOT synchronization behavior
  changed.
- This packet does not claim that the remaining 11 Python critical hotspots are resolved.
