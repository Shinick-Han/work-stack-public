# SSOT storage Wave 3 regression receipt

Date: 2026-09-01  
State: verified inactive candidate; not released or activated

## Delivered boundary

Wave 3 now provides explicit `storage migration plan`, `preview`, `execute`,
`resume`, `verify`, and `receipt` operations. The engine:

- freezes the exact nine-file v3 authority and holds the existing public
  `Store.consistent_read()` writer lease during execution;
- requires source and conversion digests before CLI execution or resume;
- creates a deterministic, exact, verified ZIP backup;
- writes a same-authority sibling v4 staging package using exclusive creation;
- validates schemas, canonical bytes, cross-record invariants, stream chains,
  the candidate manifest, and v3/v4 semantic parity;
- publishes only a verified candidate and writes its content-free receipt at
  `migrations/<migration-uid>.json`;
- re-verifies every receipt field from retained source, backup, and candidate
  artifacts in a fresh process;
- never changes registry authority or activates v4.

The restart path is explicit. If execution stops after candidate publication
but before its receipt is written, `resume` verifies the retained source,
backup, candidate, expected digests, and semantic projection before publishing
the missing receipt. It never guesses or silently adopts a candidate.

## Safety corrections from adversarial review

- Staging cleanup is permitted only after this process successfully created the
  exclusive staging directory. A pre-existing same-name directory is preserved.
- Link and Windows reparse-point staging replacements are never followed during
  cleanup.
- Canonical JSON artifacts contain no BOM, indentation, insignificant
  whitespace, or trailing newline. NDJSON alone uses the required line
  terminator.
- The v4 reader and offline validator compare actual JSON bytes with canonical
  bytes; parsing to the same value is insufficient.
- Receipt reads are bounded, regular-file-only, and protected by before/opened/
  after file identity checks.
- Fault injection covers every named transition from lease acquisition through
  receipt persistence, with source-byte and staging-cleanup assertions.

## Verification evidence

- Focused storage regression matrix: 133 passed, 3 Windows privilege-dependent
  symlink tests skipped.
- Full Python regression suite: 699 passed, 4 platform-dependent tests skipped.
- Structural quality gate: passed for 133 production files.
- Source export privacy audit: passed for 518 UTF-8 source-policy files.
- Storage coverage: 91.38% statement coverage and 82.83% branch coverage.
- CLI smoke preview reproduced the frozen populated conversion digest
  `sha256:12a090beabcb3b0ebde5568201fcec8716154721c003200b990c05c0c3254140`
  after normalizing the bounded runtime idempotency ledger into the conversion
  package without writing that ledger inside the canonical candidate;
  without creating candidate or backup artifacts.

## Gate status after the Wave 4-7 implementation pass

The bounded runtime idempotency ledger conversion policy is now implemented
and independently verified. It remains runtime evidence outside the canonical
candidate rather than becoming SSOT content. The candidate still ends at
`verified_candidate`: connection-registry activation remains unavailable in
released startup until the remaining service strangulation and external
Windows/SSH rollout evidence gates are satisfied.
