# Work Stack M9 search-scale receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `9ebfa8da72492422c18576262c46c01fb8e4b63b`
Product tree: `2bc0a13d3bc0c0e32aa4a0c01f1465b9db519f95`

## Outcome

Work Stack keeps the local JSON files as the sole planning SSOT. Search now builds one
process-local, privacy-allowlisted projection and reuses its pre-folded terms. Every successful
Store save or recovery increments a process-local generation; the next search detects the new
generation and rebuilds before returning results. The index is neither persisted nor exported.

The Command palette no longer constructs or renders every Task when opened. It shows ten product
commands plus at most twenty initial Tasks, scans locally into at most thirty immediate Task
matches, merges the authoritative bounded server results, de-duplicates Tasks, and caps the final
result DOM at fifty options.

## Measured synthetic evidence

The same deterministic task shape was measured before and after the index on this host:

| Tasks | Before median | Indexed cold build | Indexed warm median | Indexed warm max |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 105.50 ms | 104.23 ms | 0.43 ms | 0.74 ms |
| 5,000 | 536.95 ms | 593.80 ms | 1.48 ms | 1.90 ms |
| 10,000 | 1,836.82 ms | 1,327.96 ms | 5.01 ms | 5.29 ms |

The committed 10,000-Task regression gate builds the index, requires a warm search below 1,000ms,
then creates a new Task and proves that the post-mutation search returns that Task. The generous
machine budget detects loss of indexing without treating the host-specific 5ms observation as a
portable timing promise.

## Complete local gate

- Backend: 126 passed, one explicit Windows symlink privilege skip.
- Frontend: 30 files, 125 tests passed.
- Production build: passed; initial JS 487.17 kB.
- Browser and accessibility: 10 Playwright scenarios passed.
- Source privacy audit: 207 UTF-8 source-policy files passed after this receipt was added.
- Diff audit: passed.
- Original checkout: exactly the six pre-existing user-owned frontend/mockup paths remained
  outside every commit.

## Privacy and docking boundaries

- Search result fields remain exactly kind, ID, title, subtitle, target kind, and target ID.
- Reply body/target, raw locator, recipient, and credential fields are not indexed or returned.
- The cached Capture text is only the already-sanitized stored projection; no raw Microsoft
  material or token is introduced.
- Frozen contract, safety-policy, and conformance-kit bytes were not changed.
- Search does not mutate planning state or contact Conduit.
- No persisted FTS database, Conduit client, sync, back-sync, watcher, relay, bulk import, agent
  start, or taskroom start was added.

## Remaining scale debt

- The first search after process start or a mutation rebuilds the index; 10,000 synthetic Tasks
  took about 1.33 seconds on this host. Incremental indexing is deferred until dogfood shows that
  cold latency is material.
- Graph viewport virtualization and persisted FTS remain deferred. The current measured warm path
  does not justify changing the JSON SSOT or introducing a database migration.
