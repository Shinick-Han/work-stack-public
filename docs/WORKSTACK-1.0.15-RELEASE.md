# Work Stack 1.0.15

Adds Task-linked notes from the graph, a Recent changes panel, and an optional remote Agent Skill installation step.

## Changes

- Add a Context memo directly from a Task's graph popup. It is linked to that Task using the existing note workflow. Unsaved or pending submissions are protected when closing the popup.
- Browse recorded status changes in Daily Review, refresh or page explicitly, and undo supported reversible changes. Uncertain outcomes are shown as unknown instead of successful.
- Changes submitted through Agent apply now show their Agent CLI origin in the change record.
- After a verified remote update, explicitly check or install the bundled Work Stack Agent Skill in the remote user's `~/.agents/skills/work-stack`. Existing edited or foreign files are preserved. Reopening the panel cannot publish a stale result from its previous page.

## Install or update

Download `WorkStack-Setup-1.0.15.ps1` and its checksum from [this release](https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.15). Keep your existing application, settings and data paths; do not delete the old installation first.

Installer SHA-256: `afb3a8be4649684ceb2a3bddba57c5286a4cd7850c8853b164d209fe7892e1f8`

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath "$env:USERPROFILE\Downloads\WorkStack-Setup-1.0.15.ps1"
```

After comparing the hash, run the installer using your existing approved PowerShell installation procedure. See [installation guide](WORKSTACK_INSTALL_OPERATION_GUIDE.ko.md) for path options.

Windows and Linux updates remain separate explicit operations. For SSH connections, update this PC first, then use the connected-server update workflow. The Windows installer includes the matching Linux bundle with built frontend and Python dependencies; the supported remote Python runtime is still required. Existing SSOT and workspace identity must be retained.

## Validation and limits

- Source: `3f4120b57410c8e6a89ffa45995fb83a3b382739`. Windows and Linux package source identities and frozen UI bytes match.
- Actual public 1.0.14 installer to 1.0.15 upgrade passed in an isolated Windows installation: Task, linked Note, workspace ID, settings, backup and installed file hashes checked.
- Remote-update regression: 813 tests run, no failures, 5 environment-dependent skips. Structural checks passed for 594 production files without increasing debt allowances.
- The Skill flow passed 7 real Windows CPython/browser/OpenSSH-to-isolated-Linux checks. This was not a full WebView2 desktop-host or corporate-environment test.
- Unsigned executable launch and automatic restart depend on Windows application-control policy. This release does not provide code signing or bypass that policy. Company NFS/csh validation remains separate.
- Remote Skill repair/uninstall and the broader settings workflow remain outside this release. Work Stack does not launch agents through this feature.
- These are locally verified release artifacts; a successful GitHub Actions automatic release is not claimed.
