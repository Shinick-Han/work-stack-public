# Structural Quality Wave F5 receipt

Date: 2026-09-01

## Outcome

Commit `b5d8d60d04b53d432f5f5285d7a6a8879e659b43` decomposes the Snapshot S005
authority validator without changing its credential-detection decisions or the frozen Snapshot
conformance contract.

## Complexity result

- `snapshot_safety._authority_valid`: CCN 25 before, CCN 7 after.
- Largest extracted helper: CCN 6.
- Measured Python critical complexity debt above CCN 15: 11 symbols before, 10 after.
- Updated structural baseline SHA-256:
  `6348364553b9037fca0fb7c174f51dda4a451112f55c243272f7b267acf9dbed`.

## Frozen behavior evidence

Characterization now fixes these authority and S005 properties:

- authority text remains ASCII-only and requires non-empty, syntactically valid userinfo;
- valid IPv6 and IPvFuture literals remain accepted only inside brackets;
- bracketed IPv4, malformed literals, trailing literal text, and non-decimal ports remain rejected;
- reg-name hosts retain their existing ASCII and percent-triplet validation;
- unbracketed multi-colon hosts, empty hosts paired with a port separator, and malformed percent
  triplets remain rejected;
- the existing empty-host and empty-port authority variants remain accepted;
- public `evaluate_safety` decisions remain REFUSE only when the valid authority also contains a
  non-placeholder password of at least eight decoded characters.

## Verification

- Focused Snapshot and product-export tests: 21 passed.
- Snapshot, export-audit, and product-export verification: 32 passed with one existing Windows
  symlink-privilege skip.
- Full Python suite: 406 passed with one existing Windows symlink-privilege skip.
- Critical mutation sentinels: 3 killed, 0 survived.
- Python compileall: passed.
- Structural quality check: passed for 97 production files.
- Export audit: passed for 403 UTF-8 source-policy files.

## Nonclaims

- No Snapshot schema, canonical byte format, digest, text normalization, or refusal metadata changed.
- No URL extraction boundary, password decoding rule, placeholder list, or safety-rule precedence
  changed.
- No frontend, Microsoft source, remote SSH, updater, installer, SSOT synchronization, or Task
  behavior changed.
- This packet does not claim that the remaining 10 Python critical hotspots are resolved.
