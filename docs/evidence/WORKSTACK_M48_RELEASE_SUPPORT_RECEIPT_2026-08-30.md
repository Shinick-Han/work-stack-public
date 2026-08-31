# Work Stack M48 release-support receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `3accd5446aeeb39ad0b2ffacb998ec4a600115ed`
- Product tree: `f44722aa3f6bfc9b2b917d87f3b6e6fa7b590801`
- Push: not yet performed for this product commit when the receipt was written.

## Delivered behavior

- Local continuity displays the authoritative local product version and store schema beside backup
  readiness.
- It explicitly says that Work Stack does not download or run background updates.
- Update guidance retains the implemented sequence: verify the setup against its adjacent
  `.sha256`, then let the verified updater create a pre-upgrade backup and preserve configuration.
- `Copy safe support summary` copies only product version, store schema/readiness, aggregate file
  count/size, backup format, and offline-restore mode.
- The summary construction omits workspace UUID, Task/Objective/Capture content, filesystem paths,
  recipient data, credentials, and provider claims.
- Clipboard refusal remains local, visible, and retryable; no server write occurs.

## Verification

- RED-first UI test failed before release identity and the support-summary control existed.
- Full frontend gate: 35 files / 177 tests passed.
- The existing full Chromium gate passed 23/23 after M47. The M48-updated backup/release scenario
  then passed in isolation against the rebuilt production artifact.
- Production build passed without a chunk-size warning: initial JS 440.01 kB, WorkspacePage
  62.45 kB, Table 5.15 kB, Task Drawer 35.59 kB, Graph 173.04 kB, Treemap 241.16 kB.
- Source export audit before this receipt: 259 UTF-8 text files. Expected count after adding this
  receipt: 260.
- `git diff --check` passed.
- Backend behavior was unchanged; its most recent complete gate remains 138 tests passed with one
  explicit Windows symlink privilege skip.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Release/support presentation cannot mutate Task identity, revision, planning-status
facts, canonical snapshot serialization, or disclosure confirmation.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- SHA-256 sidecars provide transfer integrity, not publisher identity. OS code signing remains
  external release debt.
- The safe summary is not an automatic telemetry or support upload.
- Real Microsoft provider Gate 0 evidence remains unavailable and no provider was enabled.
- No Conduit consumer implementation was performed.
