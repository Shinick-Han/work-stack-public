# Work Stack Windows install, backup, and recovery guide

## Install from one setup file

No Python or Node.js installation is required on the target machine. The setup file includes
a hash-verified official 64-bit Python 3.12 runtime, the hash-locked Unicode 17 wheel, and
the locked Python/WebView desktop dependencies.
The installer itself does not download dependencies.

1. Download `WorkStack-Setup-1.0.1.ps1` and its adjacent
   `WorkStack-Setup-1.0.1.ps1.sha256` file.
2. Open PowerShell in the download folder.
3. Verify that the setup bytes match the sidecar before running them:

   ```powershell
   $expected = (Get-Content .\WorkStack-Setup-1.0.1.ps1.sha256 -Raw).Split(' ', 2)[0]
   $actual = (Get-FileHash .\WorkStack-Setup-1.0.1.ps1 -Algorithm SHA256).Hash.ToLowerInvariant()
   if ($actual -ne $expected) { throw "Work Stack setup checksum mismatch" }
   ```

   A source checkout can instead use the strict filename-and-digest verifier:

   ```powershell
   .\scripts\windows\Test-WorkStackSetup.ps1 -SetupPath .\WorkStack-Setup-1.0.1.ps1
   ```

4. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\WorkStack-Setup-1.0.1.ps1
   ```

5. Open **Work Stack** from the Start menu.

The checksum detects a damaged or substituted download only when the expected digest or sidecar
comes from a channel you trust. It is not a replacement for Windows code signing; the current
prototype setup remains unsigned.

The application is installed under `%LOCALAPPDATA%\Programs\WorkStack`. Planning data,
configuration, logs, and backups live separately under `%LOCALAPPDATA%\WorkStack`.

## Normal launch and automatic backups

The Start-menu shortcut opens a dedicated Work Stack desktop window through the bundled,
Python Software Foundation-signed `pythonw.exe`. Work Stack remains the permanent surface;
when Context Inbox is open, Outlook, Teams, or OneNote is composed only inside its source
viewport. There is no extra browser tab or shell-level Microsoft toolbar.

Before each offline launch, the host invokes the existing backup-aware local launcher when
the loopback server is not already ready. It keeps the newest 14 backups by default. If
backup verification or creation fails, the server does not start. Closing the desktop
window stops the local server only when that window started it.

The target PC must have the Microsoft Edge WebView2 Runtime. Current Windows 10/11 and
Microsoft 365 installations normally provide it. Microsoft tenant sign-in and Conditional
Access remain controlled by the organization and must be verified in the target environment.

## Upgrade

Run a newer one-file setup artifact. The installer:

1. stops only the Python process belonging to the exact Work Stack installation;
2. creates a verified pre-upgrade backup;
3. builds and validates a staged installation;
4. swaps the application directory; and
5. restores the previous application if a later installation step fails.

Planning data is never installed inside the application directory.

From an existing installation, the same flow can be invoked while preserving the configured
data directory and port:

```powershell
.\scripts\windows\Update-WorkStack.ps1 -SetupPath "C:\path\to\WorkStack-Setup-1.0.1.ps1"
```

Keep the matching `.sha256` sidecar beside the setup file. The updater verifies the exact filename
and digest before reading the installed configuration, stopping Work Stack, or executing any setup
code. Use `-ChecksumPath` only when the trusted sidecar is stored elsewhere. Work Stack does not
download or silently apply updates in the background.

## Manual backup and verification

Open **Work Stack Maintenance** from the Start menu for the guided path. It can create or
verify a backup, restore into the current or an empty data folder, and relocate the workspace.
Restore and relocation require explicit confirmation. If Work Stack is running, the window asks
before stopping only the exact installed Work Stack process. Replacement restore automatically
creates a verified safety backup; relocation verifies the copy, switches `config.json` only after
success, and never deletes the source.

The equivalent command-line path remains available for auditing and recovery. Run these commands
from the installed directory:

```powershell
.\runtime\python.exe .\run_work_stack.py maintenance backup --out "$env:LOCALAPPDATA\WorkStack\backups"
.\runtime\python.exe .\run_work_stack.py maintenance verify "C:\path\to\workstack-backup-....zip"
```

Stop Work Stack before offline maintenance. A running server owns the data directory and
backup/restore commands fail closed instead of reading a moving store.

## Restore safely

Restore into an empty directory first when possible:

```powershell
.\runtime\python.exe .\run_work_stack.py maintenance restore "C:\path\backup.zip" --to "C:\path\restored-data"
```

Replacing an existing store requires both `--replace` and a safety-backup directory:

```powershell
.\runtime\python.exe .\run_work_stack.py maintenance restore "C:\path\backup.zip" --to "$env:LOCALAPPDATA\WorkStack\data" --replace --safety-backups "$env:LOCALAPPDATA\WorkStack\backups\pre-restore"
```

The archive is fully validated before the destination is written. Restore uses the same
recovery journal as normal multi-file planning mutations.

## Relocate a workspace

With the server stopped, copy and verify the workspace into an empty destination:

```powershell
.\runtime\python.exe .\run_work_stack.py --data-dir "$env:LOCALAPPDATA\WorkStack\data" maintenance relocate --to "D:\WorkStack\data"
```

Relocation never deletes the source. After inspecting the destination, update
`%LOCALAPPDATA%\WorkStack\config.json` so `data_dir` points to it, then launch Work Stack.

## Uninstall

Run `scripts\windows\Uninstall-WorkStack.ps1` from the installation. By default it removes
the application and Start-menu shortcut while preserving planning data and backups. Use
`-RemoveData` only when permanent local data removal is intentional.
