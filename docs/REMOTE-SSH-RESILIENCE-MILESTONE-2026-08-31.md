# Remote SSH resilience milestone — 2026-08-31

## Outcome

The desktop-owned Remote SSH SSOT connection now treats the configured local
forward port as a preference, monitors only its own SSH child and health
endpoint, and performs bounded in-process recovery. The change builds on the
SSOT Connection Center milestone at commit
`c7b4ac8e93c0bbf7cded8f085010d0532c853860`.

## Implemented behavior

- A preferred port collision selects an OS-assigned loopback runtime port. It
  does not probe, replace, or terminate the existing listener and does not
  rewrite the saved preference.
- SSH forwarding retains strict host checking and
  `ExitOnForwardFailure=yes`.
- The monitor considers the owned SSH process and the Work Stack health check.
  It reacts after two consecutive health failures or immediately when the
  child exits.
- Recovery is capped at three attempts with bounded, interruptible backoff.
- The web view reloads only after the replacement tunnel and workspace
  identity pass verification.
- Closing the desktop application cancels the monitor and prevents an
  in-flight reconnect from publishing or retaining a late tunnel.
- The SSOT Connection Center displays reconnecting and terminal disconnected
  states and disables test/save controls during recovery.

## Verification

- Python: 335 tests passed, 1 skipped.
- Frontend: 59 files and 263 tests passed.
- Production build: TypeScript and Vite passed; 968 modules transformed.
- Structural quality gate: passed for 96 production files.
- `git diff --check`: passed (line-ending notices only).
- Added focused coverage for profile validation, preferred-port fallback,
  monitor bounds, shutdown races, malformed native messages, atomic-save
  preservation, and unknown-field rejection.

## Manual verification guide

1. Open **SSOT connection** in the sidebar.
2. Select **Remote SSH**, enter the existing OpenSSH host alias and absolute
   remote app/data paths, then choose **Test connection**.
3. Save a passing profile and restart Work Stack at the explicit boundary.
4. To exercise recovery, stop the owned SSH process while the desktop window
   remains open. The UI should show `Reconnecting`, reload only after a healthy
   replacement is ready, or settle on `Disconnected` after the bounded budget.
5. Close Work Stack during a reconnect and confirm no SSH child remains.

## Explicit nonclaims and remaining debt

- This checkpoint does not claim an end-to-end run against the corporate Linux
  SSH environment; that requires the user's real host alias and directories.
- It is not a daemon and does not reconnect after the desktop process exits.
- It does not implement field-level merge or silently resolve concurrent SSOT
  edits. Revision-aware synchronization remains the next independent data
  integrity milestone.
- It does not persist the temporary runtime port.
- It does not change Conduit, the frozen docking contract, or planning-state
  authority: Work Stack remains the sole planning-state authority.
