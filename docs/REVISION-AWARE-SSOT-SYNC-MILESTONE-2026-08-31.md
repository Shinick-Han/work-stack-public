# Revision-aware SSOT Synchronization Milestone — 2026-08-31

## Outcome

Work Stack now exposes an explicit review-and-accept workflow for external SSOT edits and protects that workflow against stale candidates, simultaneous edits, response loss, and restart recovery. The backend, frontend, adversarial tests, and independent review ran as separate parallel work packets before integration.

Work Stack remains the sole planning-state authority. Detection and review do not merge, overwrite, or contact Conduit. Only an explicit acceptance advances the authoritative baseline.

## Supported writer contract

- Browser and desktop writes use Work Stack's revision-guarded API.
- Agent writers must use the guarded API/CLI and honor the Work Stack lease. They must not edit authoritative files concurrently behind the lease.
- A raw writer that ignores both the API and lease is detected before or after ordinary file operations, but no portable pathname API can make the final compare-to-`os.replace` instruction an atomic compare/exchange. That physical instruction-window race is an explicit unsupported boundary, not a claimed guarantee.

## Backend guarantees

- A local commit records only its declared target values. A concurrent edit to an untouched file is left outside the accepted manifest, immediately detected, and write-freezing.
- A declared target that no longer matches its baseline fails closed. Its bytes and the recovery journal are preserved rather than overwritten.
- Recovery replays only targets that still match either the committed baseline or the journal's intended bytes. Third-party target bytes are never treated as a recoverable local write.
- Recovery finalizes only the intended local targets. Unrelated divergence remains an external candidate after restart.
- Fresh initialization and v1/v2 migrations keep their supported bootstrap and interrupted-recovery behavior.
- `POST /api/v1/sync/adopt` requires a safe `Idempotency-Key` and the exact reviewed generation and candidate digest.
- Exact acceptance replay succeeds after response loss, process restart, or failure to persist the optional runtime receipt. Replay proof is bounded to the immediately advanced generation.
- Reusing the latest recorded key for different coordinates returns `409 idempotency_conflict`; stale or unrelated coordinates return `409 store_sync_required`.
- The runtime receipt contains only the operation key, generations, digests, and workspace UUID. It contains no Task or source content.

## Stable API contract

```http
POST /api/v1/sync/adopt
Idempotency-Key: workstack:<stable review-session UUID>
X-WorkStack-CSRF: <same-origin session token>
Content-Type: application/json
```

```json
{
  "expected_generation": 7,
  "expected_manifest_digest": "sha256:<64 lowercase hex>"
}
```

Success returns `200 {"data": <SyncStatus>}`. The response schema remains backward-compatible; no fictional remote numeric generation was added.

## User-visible workflow

1. When an external agent or SSH-side process changes the SSOT, the top control changes to **SSOT update ready**.
2. Select **Sync / review SSOT changes**.
3. Compare **Accepted baseline · Generation N** with **Detected external candidate · Revision <digest>** and inspect the changed-file list.
4. Select **Refresh comparison** to re-read without mutation, or **Accept reviewed candidate** to advance the baseline explicitly.
5. If the candidate changed, is invalid, or the acceptance response is uncertain, writes stay paused and the dialog remains visibly unaccepted. Refresh before choosing again.
6. Retrying the unchanged review reuses its stable idempotency key, so a lost success response does not advance the baseline twice.

## Verification receipt

- Python full suite, final uncontended rerun: 361 passed, 1 environment-dependent skip
- Frontend full suite: 60 files, 274 tests passed
- Focused sync, migration, and recovery matrix: 59 passed
- New adversarial revision-sync scenarios: 11 passed
- Independent backend review matrix: 40 passed; verdict ACCEPT within the supported API/lease writer contract
- Vite production build: 968 modules transformed
- Production structural quality gate: 96 files passed
- `Store.save_many` CCN: 6 after decomposition; highest new commit helper CCN: 9
- Export/privacy audit: 395 UTF-8 source-policy files passed before this receipt was added
- `git diff --check`: passed

One initial parallel Python run reproduced the already documented Windows test-harness socket reset (`WinError 10053`) in the capture size-limit API test. The isolated test passed 1/1 and the immediate complete uncontended rerun passed 361/361 with one environment skip; no assertion or product expectation was weakened.

## Adversarial findings closed

- unrelated external edits silently absorbed by a local commit;
- adoption success response lost after manifest commit;
- adoption receipt persistence failing after manifest commit;
- restart recovery silently baselining an unrelated external edit;
- restart recovery overwriting a concurrently changed declared target;
- temporary fresh-initialization and v2→v3 migration regressions introduced while closing the races.

## Explicit nonclaims and remaining debt

- This is conflict detection and explicit acceptance, not field-level or semantic auto-merge.
- The final compare-to-path-replacement race for a raw writer that ignores the API/lease remains outside the supported writer contract. Platform atomic exchange or displaced-byte preservation would be required to support that adversary fully.
- The runtime adoption receipt remembers the latest accepted key. Coordinate replay is generation-bounded, but an older key is not retained forever after later acceptances. A bounded multi-key ledger is P2 hardening if dogfood demonstrates a need.
- Sync events are bounded and process-local; this milestone does not add a cloud relay, watcher daemon, or Conduit back-sync.
