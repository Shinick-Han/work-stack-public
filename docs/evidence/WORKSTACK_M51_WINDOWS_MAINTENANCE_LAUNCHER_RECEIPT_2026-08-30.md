# Work Stack M51 Windows Maintenance Launcher Receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `1d3a555dcec35e24888eefba97de327ed7fded85`
Product tree: `1f72ac549e039458ffd926d81d55329750794c22`

## Outcome

PASS. The Windows installer now creates a separate **Work Stack Maintenance** Start-menu
shortcut. Its guided window exposes four bounded operations: create a verified backup, verify a
backup file, restore a verified backup, and relocate the current workspace.

Restore and relocation are offline operations. An unattended invocation fails if the exact
installed Work Stack process is running. In the guided path, stopping that exact process requires
explicit confirmation. Restore verifies the selected archive before invoking restore; replacement
passes both `--replace` and an automatic `--safety-backups` directory. Relocation accepts only a
separate empty destination, preserves the source, and updates `config.json` only after the copy has
completed and verified.

## Changed product paths

- `scripts/windows/Maintain-WorkStack.ps1`
- `scripts/windows/Install-WorkStack.ps1`
- `scripts/windows/Uninstall-WorkStack.ps1`
- `tests/test_windows_installer_bundle.py`
- `docs/WORKSTACK_WINDOWS_INSTALL_BACKUP_USER_GUIDE_2026-08-30.md`

## RED-first and verification evidence

- The Windows bundle contract initially failed because the launcher and both shortcut lifecycle
  changes did not exist.
- Windows installer/launcher contract plus maintenance-domain tests: 15 passed.
- PowerShell parser check for `Maintain-WorkStack.ps1`: passed.
- Disposable installed-layout smoke: passed backup verification, restore into an empty directory,
  relocation into a second empty directory, configuration update, and source-preservation checks.
- Full backend gate: 140 passed, 1 skipped. The skip remains the explicit Windows symlink privilege
  case.
- Source export audit before this receipt: 262 UTF-8 text files passed.
- `git diff --check`: passed before the product commit.

The smoke used a synthetic Task and temporary paths only. It did not read, copy, or modify the
user's actual Work Stack planning store.

## Frozen docking coordinates preserved

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

The launcher delegates to the existing maintenance CLI and does not alter planning serialization,
snapshot export, Unicode 17 behavior, or Conduit ownership boundaries.

## Remaining debt and nonclaims

- The exact rebuilt one-file setup artifact still needs the final aggregate release pass after M52.
- The prototype remains unsigned at the OS publisher level.
- M51 does not provide live web restore, silent process termination, automatic relocation,
  source deletion, cloud backup, or background synchronization.
- Outlook and Teams Gate 0 remain unverified; no provider flag changed.
- Work Stack does not contact or control Conduit.

At the product commit the task-scoped worktree was clean. The six protected user-owned paths in the
original checkout were not staged, reset, committed, or modified by M51.
