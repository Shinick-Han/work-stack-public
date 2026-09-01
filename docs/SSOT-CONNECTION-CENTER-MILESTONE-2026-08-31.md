# SSOT Connection Center milestone

Date: 2026-08-31

## Outcome

Work Stack Desktop now exposes one bottom-pinned **SSOT connection** entry instead of a
passive duplicate label. The native dialog can inspect, validate, test, and atomically save
either a local workspace profile or a remote SSH profile. A normal browser keeps the existing
read-only local/sync status copy and does not pretend that native configuration is available.

The native bridge accepts only the fixed `workstack-ssot-host` status, test, and save commands.
It rejects passwords, private-key paths, arbitrary SSH arguments, unsafe Linux paths, invalid
ports, and non-canonical workspace identities. Remote testing reuses the read-only SSH
prerequisite check and does not persist the draft. Saving never starts a second server or
silently changes the active workspace; a changed profile is marked **restart required**.

## Click-through guide

1. Open the installed Work Stack Desktop application.
2. Click **SSOT settings**, **Local SSOT**, or **Remote SSOT** at the bottom of the sidebar.
3. Choose **Local workspace** or **Remote SSH workspace**.
4. For SSH, enter the configured SSH host alias, remote Work Stack app directory, private SSOT
   directory, canonical workspace UUID from `store-meta.json`, and the loopback ports.
5. Select **Test connection**. This validates OpenSSH, strict host-key trust, remote files,
   Python, and the Work Stack runner without writing the profile.
6. Select **Save settings**. If the profile differs from the active launch, close and reopen
   Work Stack to activate it.
7. If a test fails, use the diagnostic log path shown in the dialog. The SSOT remains unchanged.

## Verification

- Desktop SSH/host contract: 17 passed.
- Full Python suite: 307 passed, 1 environment-dependent skip.
- Frontend suite: 259 passed.
- TypeScript/Vite build: 968 modules transformed.
- Structural quality gate: 94 production files passed.
- `App` CCN reduced from 79 to 78.
- Native SSOT message handler CCN: 8; status payload builder CCN: 5.
- `git diff --check`: passed.

## Explicit limits and next packet

- The current profile uses an explicit local-forward port; automatic collision-free allocation
  is not part of this milestone.
- The workspace UUID is entered explicitly and remains a fail-closed identity pin.
- A saved profile is activated after restart; in-process server replacement is not attempted.
- Tunnel health monitoring and bounded automatic reconnection are the next implementation packet.
- No remote data, planning state, Microsoft content, or Conduit state is copied or mutated by the
  connection test.
