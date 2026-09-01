# Per-workspace SSOT path activation implementation plan

Date: 2026-09-01
Status: P0-P4 and activation safety primitives implemented behind release gates; packaged activation smoke and recovery UX remain

## 1. Outcome

Work Stack must let a user save several independent workspace profiles and configure the SSOT
location for each profile:

- local profile: one protected Windows `data_dir`;
- SSH profile: one `ssh_host_alias`, `remote_app_dir`, and `remote_data_dir`;
- every profile: one verified `expected_workspace_id`.

The selected active profile must become the workspace used by the normal Work Stack runtime after a
controlled restart. Saving a profile must never silently change, copy, merge, initialize, or repair
planning data.

## 2. Current state and gap

Already implemented:

- strict version-1 `connection-registry.json` persistence;
- separate local and SSH profile schemas;
- workspace-specific `data_dir` and `remote_data_dir` fields;
- canonical workspace/profile identities;
- bounded SSH alias discovery;
- strict native/frontend registry bridge;
- atomic registry replacement and duplicate-authority refusal.

Not yet implemented:

- migration of the active legacy singleton connection into the registry;
- registry-driven desktop startup;
- profile list/add/edit/delete/activate UI;
- native local-directory picker;
- per-profile connection testing and detected identity display;
- restart activation and startup recovery.

The existing desktop still starts from the legacy singleton draft. Therefore the new registry bridge
must remain hidden from normal UI until the activation transaction below is complete.

## 3. Authority rules

1. `connection-registry.json` becomes the only configuration authority after migration.
2. Each SSOT remains its own planning-data authority. The registry stores only connection metadata.
3. A profile path is not accepted as proof of workspace identity.
4. `expected_workspace_id` is learned or confirmed by a successful test and is verified again on
   every activation.
5. A mismatched identity blocks startup. Work Stack never silently adopts the directory it happens
   to find and never falls back to another profile.
6. At most one enabled profile may declare a given workspace authority in schema version 1.
7. Saving and activating are separate actions. Editing an inactive profile cannot redirect the
   running writer.

## 4. Configuration model

The existing registry fields remain the schema-v1 persistence contract.

### 4.1 Local profile

```json
{
  "profile_id": "canonical UUID",
  "label": "Personal laptop",
  "kind": "local",
  "enabled": true,
  "live_updates": true,
  "data_dir": "C:\\WorkStack\\workspaces\\personal",
  "expected_workspace_id": "canonical UUID"
}
```

`data_dir` is the directory containing the authoritative Work Stack Store files. Backups remain
app-managed and profile-scoped under
`%LOCALAPPDATA%\WorkStack\workspace-backups\<workspace_id>` in v1; backup location is not inferred
from or mixed with the SSOT directory.

### 4.2 SSH profile

```json
{
  "profile_id": "canonical UUID",
  "label": "Company Linux",
  "kind": "ssh",
  "enabled": true,
  "live_updates": true,
  "ssh_host_alias": "work-linux",
  "remote_app_dir": "/srv/workstack/app",
  "remote_data_dir": "/srv/workstack/ssot",
  "expected_workspace_id": "canonical UUID",
  "preferred_forward_port": 18765,
  "remote_port": 8765
}
```

OpenSSH remains authoritative for hostname, user, key, proxy, and agent configuration. Work Stack
stores no password, private-key path, raw SSH argument, or expanded command.

### 4.3 Runtime-only state

Connection health, actual workspace identity, selected runtime port, product/protocol version,
generation, observation time, pending action ID, and error code are session-only status. They do not
belong in the registry.

## 5. Path validation

### 5.1 Local path

The native validator is authoritative; frontend validation is an early UX check only.

Reject:

- relative paths;
- `/`, `C:\`, and other filesystem or drive roots;
- UNC paths and Windows device namespaces in v1;
- NUL, CR, LF, `.` or `..` segments;
- paths longer than the bounded schema limit;
- symlinks, junctions, or reparse points until their trust and recovery semantics are explicit;
- a directory that partially resembles a Store but fails the complete Store contract.

Allow two explicit flows:

- **Connect existing workspace**: directory exists, complete Store is read-only inspected, and its
  workspace identity is returned;
- **Create new workspace here**: target does not exist or is empty, the user separately confirms
  initialization, and normal Store initialization creates it under the writer lease.

A Test action never creates directories or Store files.

### 5.2 SSH path

Test through a fixed-shape `ssh` invocation using the selected alias. The remote check verifies:

- host key and noninteractive authentication;
- `remote_app_dir` contains the expected Work Stack runner;
- `remote_data_dir` contains a complete Store;
- actual workspace identity matches the confirmed identity;
- remote product and protocol versions are compatible;
- the remote server can bind only to loopback.

The check is read-only. It does not run `mkdir`, copy a Store, change permissions, or repair data.

## 6. User experience

The existing top-right and sidebar SSOT indicators continue to open one Connection Center.

### 6.1 Profile list

The dialog first shows:

- profile label and Local/SSH badge;
- active, enabled, stale, disconnected, or identity-mismatch state;
- shortened verified workspace ID;
- SSOT path (`data_dir` or `alias:remote_data_dir`);
- actions: Edit, Test, Activate, Disable, Remove.

Removing the active profile is blocked. Removing an inactive profile deletes only connection
metadata and never its local or remote SSOT directory.

### 6.2 Add/edit local profile

Fields:

- profile label;
- local SSOT directory;
- Browse button backed by a native folder picker;
- Connect existing / Create new choice;
- detected workspace identity, shown read-only after Test;
- live-update preference.

Typing a workspace UUID manually is an advanced recovery action, not the default setup path.

### 6.3 Add/edit SSH profile

Fields:

- profile label;
- SSH alias dropdown populated from `~/.ssh/config`, with manual safe alias entry;
- remote app directory;
- remote SSOT directory;
- preferred local port and remote port under an Advanced disclosure;
- detected workspace identity and protocol information after Test;
- live-update preference.

### 6.4 Save and activation

- **Save profile** writes only registry metadata.
- **Save and activate after restart** writes the registry with the selected `active_profile_id` and
  returns `restart_required: true`.
- The currently running workspace remains unchanged until its child server/tunnel has stopped.
- Hot switching is deferred until restart activation is proven; no UI copy may imply immediate
  switching.

Every async response is accepted only when both `request_id` and `operation` match the pending UI
action. Late or duplicate replies are ignored.

## 7. Native bridge extensions

Extend the existing versioned registry bridge with bounded operations:

- `get-registry`;
- `save-registry`;
- `discover-ssh-aliases`;
- `choose-local-directory`;
- `test-profile`;
- `activate-profile`;
- `initialize-local-profile` only after a separate explicit confirmation milestone.

Responses contain structured safe fields, for example:

- `state`;
- `profile_id`;
- `actual_workspace_id`;
- `product_version` and `protocol_version` where applicable;
- `restart_required`;
- bounded `error_code` and user-facing message.

They never contain a raw command line, credential, environment dump, private-key path, Store
content, or an arbitrary filesystem enumeration.

Registry work runs on one bounded worker queue. Saves and activations are serialized. WebView UI
callbacks only enqueue work and marshal the final response back to the form thread; SSH config
scanning and file `fsync` never block the UI thread.

## 8. Legacy migration

Migration executes before normal desktop startup when no registry exists.

1. Read and strictly validate the legacy singleton draft.
2. Resolve the currently configured local `data_dir`, or retain the existing SSH fields.
3. Read the actual workspace identity without mutating the Store.
4. Derive a stable profile ID from the installation identity and workspace identity.
5. Build and round-trip a one-profile registry in memory.
6. Write the registry atomically.
7. Preserve an exact backup of the legacy connection file and write a content-free migration
   receipt containing schema versions, profile/workspace IDs, and digests.
8. Re-read the registry before selecting its active profile.

After a migration receipt exists, startup does not import changes back from the legacy draft. During
one compatibility release, the active profile may be exported as a generated legacy mirror for
downgrade support, but the mirror is never treated as an authority by the new version.

If migration cannot verify the current workspace identity, the old singleton runtime remains
unchanged and Connection Center shows a migration-required recovery state. Partial registry files
are never accepted.

## 9. Registry-driven startup

Replace the current singleton startup branch with this sequence:

1. Load and validate the registry.
2. Resolve `active_profile_id`; require the profile to exist and be enabled.
3. Revalidate its path and expected identity before starting a process or tunnel.
4. For a local profile, start the child server with that profile's `data_dir` and profile-scoped
   backup directory.
5. For an SSH profile, allocate a runtime loopback port, start the fixed-shape tunnel, verify remote
   identity/protocol, and then navigate the main WebView.
6. Publish active-profile status only after health and identity verification.
7. Keep inactive profiles closed in the first activation milestone.

If activation fails, do not start a different workspace. Keep the native shell alive on a bounded
recovery page that can open Connection Center, Test another profile, restore the previous active
profile, or exit. This avoids the current failure mode where a bad path prevents access to the UI
needed to repair it.

## 10. Activation transaction and rollback

Activation is a configuration transaction, not a planning-data transaction.

1. Validate the candidate registry and selected profile.
2. Require a recent successful Test result bound to the exact canonical profile digest.
3. Save a rollback copy of the previous registry.
4. Atomically replace the registry with the new `active_profile_id`.
5. Write an activation receipt containing old/new profile IDs and candidate digest.
6. Report restart required; do not touch the running server.
7. On next startup, verify the receipt and candidate registry before launch.
8. After successful health and identity verification, mark the activation confirmed.

If startup fails before confirmation, offer an explicit **Restore previous connection** action.
Automatic fallback is forbidden because it could show or modify an unexpected workspace.

## 11. Concurrency and external changes

- One process-local lock serializes registry saves and activation changes.
- A registry save carries the revision/digest last read by the editor; stale editors receive a
  conflict and must reload.
- The worker queue is bounded and coalesces duplicate Get/Discover requests.
- Per-profile Test actions have coordinator-issued action IDs. Late results cannot update a newer
  candidate or clear identity/integrity errors.
- A remote Store generation change is handled by the existing authoritative refetch/review path;
  changing profile paths never resolves an SSOT content conflict.

## 12. Implementation milestones

### P0 — freeze contracts and RED tests

- document authoritative registry/startup ownership;
- add path-root, UNC/device, reparse-point, partial-Store, duplicate-authority tests;
- add request correlation, stale response, and bridge-size tests;
- add migration interruption and activation rollback fixtures.

Exit gate: unsafe paths and split-brain startup are reproduced by failing tests.

### P1 — native profile test and folder picker

- bounded worker queue;
- `choose-local-directory` and `test-profile` operations;
- read-only local Store inspection;
- SSH alias selection and fixed-shape remote verification;
- structured detected-identity response.

Exit gate: Test produces no filesystem or remote mutation and cannot freeze the WebView callback.

### P2 — deterministic legacy migration

- stable one-profile migration;
- atomic registry write, legacy backup, and receipt;
- no reverse import after migration;
- interrupted migration recovery.

Exit gate: existing local and SSH installations restart on the same authority with unchanged Store
digests.

### P3 — registry-driven startup

- active local path passed to child server startup;
- active SSH profile passed to tunnel supervision;
- identity/protocol gate before main navigation;
- recovery shell when activation fails.

The implementation remains behind `WORKSTACK_CONNECTION_REGISTRY_V1=1`. Activation receipts,
optimistic-concurrency guards, one-way legacy mirroring, and registry-aware remote rebind are now
implemented. The default release keeps the singleton path authoritative until packaged local/SSH
activation smoke and the user-facing restore-previous recovery surface are green together.

Exit gate: changing `active_profile_id`, restarting, and verifying the UI switches authority without
writing either Store during configuration.

### P4 — multi-profile Connection Center

- profile list and add/edit forms;
- native local browse and SSH alias dropdown;
- Save versus Save and activate after restart;
- request correlation, dirty-state protection, accessible keyboard flow;
- exact path and detected identity display.

Exit gate: the user can configure at least two local/SSH profiles and activate either one after
restart without editing JSON manually.

### P5 — activation recovery and compatibility retirement

- activation receipt confirmation;
- explicit restore-previous action;
- generated legacy mirror for one compatibility window if required;
- remove legacy reads after migration evidence and downgrade policy are complete.

Exit gate: crash at every activation boundary leaves either the old or new validated registry and
never silently chooses a workspace.

### P6 — federation runtime

- action epochs and sticky identity/integrity blocks;
- active-first bounded scheduling;
- full refetch after reconnect/event gaps;
- read-only Portfolio projection for inactive authorities.

Exit gate: late actions cannot clear a newer safety state, and inactive workspace visibility never
creates a second writer.

## 13. Regression matrix

Automated release tests cover:

- existing local singleton migration;
- existing SSH singleton migration;
- new local directory and existing local Store;
- two profiles with identical labels but distinct identities;
- two enabled profiles claiming the same identity;
- local root, UNC, device, reparse, traversal, and overlong paths;
- SSH option-shaped alias and unsafe remote paths;
- empty, partial, corrupt, future-schema, and identity-mismatched Stores;
- active profile disabled/deleted between save and restart;
- crash before registry replace, after replace, and before activation confirmation;
- stale registry editor and concurrent Save/Activate requests;
- reordered/oversized native messages and late request IDs;
- occupied preferred tunnel port;
- remote process loss and reconnect with changed identity;
- previous-version downgrade behavior during the compatibility window;
- exact Store file hashes before and after migration, testing, activation, and rollback.

## 14. Release gate

The profile UI remains disabled until P0-P3 pass. A releasable build requires:

- full Python and frontend suites;
- production frontend build and structural quality gate;
- Windows packaged-app smoke for local and SSH activation;
- migration and rollback smoke from the last public installer;
- no Store byte changes during Test, migration, or configuration activation;
- clean restart proof for each selected profile;
- installer/update compatibility verification.

Implementation checkpoint (2026-09-01):

- complete: bounded native worker, local/SSH read-only Test, deterministic migration, registry-driven
  startup, multi-profile UI, request correlation/timeouts, digest CAS, Test-bound activation proof,
  rollback/activation receipts, explicit native restore/confirm primitives, generated legacy mirror,
  CAS-bound remote rebind, and a native startup-failure recovery page with an explicit Restore
  previous connection action;
- complete automated payload evidence: the installer build now runs a deterministic bundled-runtime
  smoke covering gate-off read-only startup, local migration/activation/restart/confirm, injected SSH
  selection and fixed command construction without network access, failed-startup pending evidence,
  explicit restore, and exact SHA-256 stability for 18 Store files;
- verified: 557 Python tests (1 skipped), 339 frontend tests, production frontend build, and the
  structural quality gate over 117 production files;
- verified candidate installer evidence: exact sidecar checksum verification, an isolated no-shortcut
  install, two bundled-runtime local server cold starts with `api_version=v1`/`status=ready`, and a
  desktop local-profile cold start plus restart with exit code 0, one persisted active profile, and no
  startup error log;
- verified previous-release compatibility: the isolated 1.0.5 to 1.0.6 candidate upgrade preserved
  configuration bytes, SSOT and custom-backup markers, restored 1.0.5 after an injected installer
  failure, and restored 1.0.5 after an injected post-install restart failure;
- still required before enabling the release gates: one installed-app activation/restart against a
  user-selected real SSH authority. The automated SSH smoke is necessary but does not substitute for
  that live network check.

## 15. Non-goals for this plan

- copying or merging SSOT directories;
- writable local replicas of remote Stores;
- background synchronization while Work Stack is closed;
- automatic failover to a second endpoint;
- hot switching before restart activation is proven;
- user-configurable SSH commands or private-key paths;
- cross-workspace planning mutations.
