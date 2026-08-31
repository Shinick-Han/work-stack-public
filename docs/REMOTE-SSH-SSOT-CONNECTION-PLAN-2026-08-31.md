# Work Stack remote-SSH SSOT connection plan

Date: 2026-08-31

## Decision

For the described Windows-desktop/Linux-SSOT environment, Work Stack should support two explicit
storage modes:

1. `local`: the current `%LOCALAPPDATA%\WorkStack\data` store.
2. `ssh-remote`: an ephemeral SSH tunnel to a Work Stack server that runs on the Linux host only
   while the Windows desktop app is open.

The Windows app must not mount the Linux directory and treat it as a local JSON directory. It must
also not keep an independently writable Windows replica in the first release. Both alternatives
create avoidable locking, atomic-rename, partial-transaction, and merge-conflict problems.

In `ssh-remote` mode, Linux remains the sole planning-state authority. Reads and writes from the
Windows UI are HTTP requests forwarded over SSH to the Linux process, and that process reads and
writes the Linux SSOT locally. There is therefore no foreground copy-to-copy synchronization step.

## Why a mounted SSH path is not the contract

The current Store relies on:

- one lifetime writer lease (`.workstack.lock`);
- atomic temporary-file replacement;
- a multi-file recovery journal;
- workspace identity and monotonic Task revisions;
- fail-closed validation of the complete store roster.

SSHFS/SFTP-style mounts do not provide a sufficiently portable guarantee that Windows and Linux
observe those operations and locks with the same semantics. A transient network break can also
make a successful remote write look like a failed local file operation. Pointing `data_dir` at a
mounted drive would therefore weaken the existing durability model.

## Session architecture

```text
Windows Work Stack desktop
        |
        | http://127.0.0.1:<local-forward-port>
        v
OpenSSH child process (alive only with the desktop session)
        |
        | encrypted local port forward
        v
Linux Work Stack server on 127.0.0.1:<remote-session-port>
        |
        | local Linux filesystem operations and Store lease
        v
protected Linux SSOT directory
```

The public Apache directory is not in this write path. Apache may continue serving a published
read-only artifact, but the desktop app uses SSH and the loopback-only remote server for live work.

## Connection profile

Store non-secret connection metadata in the Windows state directory, separate from planning data:

```json
{
  "storage_mode": "ssh-remote",
  "ssh_host_alias": "workstack-linux",
  "remote_app_dir": "/srv/workstack/app",
  "remote_data_dir": "/srv/workstack/ssot",
  "local_forward_port": 18765,
  "workspace_id": "11111111-1111-4111-8111-111111111111",
  "remote_port": 8765
}
```

Use the user's existing OpenSSH configuration, SSH agent, key, host-key policy, and optional
ProxyJump settings. Work Stack should not store a password, private key, OOB token, or host-key
bypass flag. The first connection must fail closed if host verification or authentication fails.

## Desktop startup and shutdown

Startup must:

1. Validate the profile and resolve `ssh.exe` from a configured or system path.
2. Ask OpenSSH to establish a loopback-only forward with `ExitOnForwardFailure=yes`.
3. Start one foreground Linux Work Stack server command using the configured remote application and
   data directories. The remote server must bind only to `127.0.0.1`.
4. Wait for `/api/v1/health`, then verify the profile's expected workspace identity and store readiness before
   showing the planning UI.
5. Record process ownership so the desktop app stops only the SSH/server session it started.

Normal shutdown must close the UI, terminate the owned SSH session, wait for the remote command to
exit, and leave the Linux SSOT intact. Crash recovery relies on the existing Store journal and
readiness validation at the next start.

## Live changes and the meaning of synchronization

Windows-originated changes need no replication: a successful API response means the Linux SSOT has
committed the change. Transport loss after a write remains commit-unknown and must be resolved by
rereading the affected Task and its revision, not by blindly replaying a non-idempotent request.

Linux-originated changes should normally use the same running Work Stack API while the desktop
session owns the Store lease. This includes changes requested from an agent. The product must ship
an agent-facing CLI that discovers the active loopback server and forwards a revision-guarded,
idempotent mutation rather than opening the JSON files itself. When no server is active, the same CLI
may acquire the Store lease and perform the transaction locally.

Direct file modification cannot simply be assumed away. An agent, legacy skill, editor, or recovery
script may ignore the lease and replace one or more SSOT files. The running server therefore needs an
external-change detector and a fail-closed reconciliation path in addition to its normal API event
stream.

For immediate UI reflection without periodic polling, add a loopback-only event stream:

- `GET /api/v1/events` using Server-Sent Events;
- events contain no Task content, only `workspace_id`, committed store generation, entity kind/ID,
  and revision;
- after an event the frontend invalidates and refetches the authoritative query;
- reconnect uses the latest generation and always performs a full authoritative refetch;
- an event is emitted only after the Store transaction commits.

Linux scripts and agents should call the loopback API while the remote session is active. When it is
inactive they may use the existing CLI, which acquires the Store lease itself. This preserves one
writer model rather than creating a second synchronization engine.

## Agent writes and external SSOT reconciliation

### Preferred agent mutation path

Add an `agent apply` surface whose input includes:

- workspace ID and target entity ID;
- expected entity revision;
- one bounded field patch or domain command;
- a caller-generated idempotency key;
- actor/source metadata that contains no credential material.

The active server validates the command, applies it through the existing Work Stack service, advances
the revision, writes the recovery-journal transaction, and emits a post-commit event. A stale
revision returns a conflict containing only the current revision and safe field metadata. The agent
must reread and deliberately rebase; it must not overwrite or retry a changed command blindly.

### Detecting direct file changes

The Linux server records a committed Store manifest after every transaction. The manifest contains
the workspace ID, store schema version, logical generation, and SHA-256 for every authoritative JSON
file. Linux filesystem notifications are only a wake-up signal; hashes and full Store validation are
the evidence. Timestamp and file size alone are insufficient.

If an authoritative file changes without a matching server commit:

1. Move the session to `external-change-detected` and temporarily reject new planning mutations.
2. Wait for the writer's rename burst to settle, then read a complete candidate snapshot once.
3. Validate every required file, workspace identity, Task revisions, relationship integrity,
   append-only planning facts, and recovery-journal state.
4. Preserve the prior committed manifest before adoption. A byte-for-byte pre-adoption backup is
   required before a future **Restore previous SSOT** action may be exposed.
5. Produce an entity/field change summary without exposing full sensitive content in diagnostics.
6. Notify the Windows app and enable **Review SSOT changes**.

Invalid, partial, cross-workspace, revision-regressing, or unexplained journal states move to
`external-change-invalid`. In that state all writes remain disabled and the UI offers only inspect,
verified backup, retry validation, or restore. Work Stack must never silently normalize or overwrite
the external bytes.

### Reconciliation states shown by the desktop app

| State | UI | Mutation policy |
| --- | --- | --- |
| `in-sync` | Sync indicator is quiet; button disabled | Normal revision-guarded writes |
| `agent-update-committed` | Brief changed badge, then automatic refetch | Normal writes after refetch |
| `external-change-detected` | **Review SSOT changes** enabled | New writes paused |
| `conflict` | Conflicting Tasks/fields listed | Only explicit field-level resolution |
| `external-change-invalid` | Red integrity warning | All planning writes blocked |
| `disconnected` | SSH disconnected; last view marked stale | No writes until health and identity revalidation |

The button is not a generic “copy whichever side is newer” operation. It opens a reconciliation
review. The first implementation permits only acceptance of an unchanged, fully validated external
candidate. Restore and field-level conflict resolution remain unavailable until the private backup
and three-way merge work is implemented; writes remain paused instead of pretending those actions
exist.

### Three-way conflict handling

For every Task or Objective visible or being edited, the frontend retains:

- `base`: the revision/value last read from the authoritative server;
- `local`: any unsaved form intent;
- `remote`: the newly validated SSOT value.

If only `remote` changed, refetch automatically after review. If `local` and `remote` changed
different fields, offer a deterministic disjoint-field rebase. If both changed the same field, require
an explicit choice and submit it against the new remote revision. Planning status, relationships,
and deletions are domain operations rather than free-form field merges and always require their
normal invariant validation.

There is no writable Windows planning replica in this design. “Sync” means reconciling the current
Windows view and unsaved intent with a changed Linux authority, not merging two independent stores.

### Changes made while the desktop app is closed

The Windows state directory stores only the last observed workspace ID, logical generation, and
manifest digest. On the next SSH connection the desktop compares those coordinates with the current
Linux server. If they differ, it shows **Changes since your last session** and enables the same review
flow before accepting new edits. The Linux activity/change journal supplies the bounded summary; the
Windows machine does not retain a second copy of all planning content merely to compute the diff.

## GitLab backup

The hourly backup job must not copy an arbitrary mixture of JSON files during a transaction. It
should either:

- request the existing verified backup from the running server and commit that consistent artifact;
  or
- acquire the Store lease and run `maintenance backup` when no server owns it.

The Git repository must remain outside `public_html`, be readable only by the Linux user, and exclude
runtime locks, tokens, server metadata, SSH material, browser profiles, and Microsoft content not
intended for the planning SSOT.

## Existing non-Work-Stack documents

The current Work Stack SSOT is a validated JSON store, not an arbitrary directory of OKR, backlog,
worklog, weekly-report, status, and memo documents. If the Linux SSOT uses a different format, do not
point `remote_data_dir` at it directly.

Choose one authority before implementation:

- migrate those records into the Work Stack store and make Work Stack the planning SSOT; or
- keep the existing documents authoritative and build a deterministic adapter with stable IDs,
  revisions, and an explicit write-back map.

The first option is the shortest reliable product path. A bidirectional adapter is a separate data
integration project and must define conflict ownership per field before it is allowed to write.

## Bounded implementation order

1. Add a connection-profile schema and a read-only `ssh check` command.
2. Add desktop-owned SSH process lifecycle, health/workspace verification, and failure UI.
3. Reuse the current frontend and API through the local port forward; do not add a local planning
   replica.
4. Add post-commit SSE invalidation and reconnect tests.
5. Add the agent-facing forwarding CLI with expected revisions and idempotency keys.
6. Add external-file manifest detection, write freeze, validated reconciliation, and the
   **Review SSOT changes** state machine.
7. Add server-mediated backup evidence for the GitLab cron workflow.
8. Test disconnect-before-write, disconnect-after-commit, stale revision, concurrent Linux API
   mutation, direct-file rename bursts, partial multi-file writes, revision regression, invalid
   journals, restart reconciliation, crash recovery, SSH host-key refusal, and clean shutdown.

## Explicit nonclaims

This plan does not make an SSH filesystem mount safe, does not provide offline multi-master editing,
does not allow simultaneous direct JSON writers, and does not make `public_html` a planning-state
authority. Those are intentionally outside the first remote-SSOT release.
