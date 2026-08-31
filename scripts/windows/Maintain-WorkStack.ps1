[CmdletBinding()]
param(
    [ValidateSet('Menu', 'Backup', 'Verify', 'Restore', 'Relocate')]
    [string]$Action = 'Menu',
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [string]$BackupPath = '',
    [string]$Destination = '',
    [switch]$Confirm
)

$ErrorActionPreference = 'Stop'
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$statePath = [IO.Path]::GetFullPath($StateRoot)
$configPath = Join-Path $statePath 'config.json'
$pythonPath = Join-Path $installPath 'runtime\python.exe'
$entryPath = Join-Path $installPath 'run_work_stack.py'
$interactive = $Action -eq 'Menu'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Work Stack is not configured. Install Work Stack before opening Maintenance.'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf) -or -not (Test-Path -LiteralPath $entryPath -PathType Leaf)) {
    throw 'Work Stack installation is incomplete. Re-run the verified installer.'
}

$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$dataPath = [IO.Path]::GetFullPath([string]$config.data_dir)
$backupDirectory = [IO.Path]::GetFullPath([string]$config.backup_dir)

function Show-WorkStackMessage {
    param([string]$Text, [string]$Title = 'Work Stack Maintenance', [string]$Icon = 'Information')
    if ($interactive) {
        Add-Type -AssemblyName System.Windows.Forms
        $messageIcon = [Enum]::Parse([Windows.Forms.MessageBoxIcon], $Icon)
        [void][Windows.Forms.MessageBox]::Show(
            $Text,
            $Title,
            [Windows.Forms.MessageBoxButtons]::OK,
            $messageIcon
        )
    } else {
        Write-Host $Text
    }
}

function Confirm-WorkStackAction {
    param([string]$Text)
    if (-not $interactive) { return [bool]$Confirm }
    Add-Type -AssemblyName System.Windows.Forms
    $answer = [Windows.Forms.MessageBox]::Show(
        $Text,
        'Work Stack Maintenance',
        [Windows.Forms.MessageBoxButtons]::YesNo,
        [Windows.Forms.MessageBoxIcon]::Warning
    )
    return $answer -eq [Windows.Forms.DialogResult]::Yes
}

function Test-WorkStackRunning {
    foreach ($candidate in Get-CimInstance Win32_Process) {
        if (-not $candidate.ExecutablePath -or -not $candidate.CommandLine) { continue }
        try { $executable = [IO.Path]::GetFullPath([string]$candidate.ExecutablePath) } catch { continue }
        if (
            $executable.Equals($pythonPath, [StringComparison]::OrdinalIgnoreCase) -and
            ([string]$candidate.CommandLine).IndexOf($entryPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        ) { return $true }
    }
    return $false
}

function Assert-WorkStackOffline {
    if (-not (Test-WorkStackRunning)) { return }
    if (-not $interactive) {
        throw 'Work Stack must be stopped before offline maintenance.'
    }
    if (-not (Confirm-WorkStackAction 'Work Stack must be stopped before offline maintenance. Stop only the Work Stack process now?')) {
        throw 'Maintenance was cancelled; Work Stack is still running.'
    }
    $stopScript = Join-Path $installPath 'scripts\windows\Stop-WorkStack.ps1'
    & $stopScript -InstallRoot $installPath | Out-Null
    if ($LASTEXITCODE -ne 0 -or (Test-WorkStackRunning)) {
        throw 'Work Stack could not be stopped. No maintenance action was attempted.'
    }
}

function Invoke-WorkStackRuntime {
    param([string[]]$RuntimeArguments)
    $result = @(& $pythonPath $entryPath @RuntimeArguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = ($result | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "Maintenance validation failed. No later step was attempted.`n$detail"
    }
    return ($result | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
}

function Select-BackupArchive {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object Windows.Forms.OpenFileDialog
    $dialog.Title = 'Choose a Work Stack backup'
    $dialog.Filter = 'Work Stack backup (*.zip)|*.zip'
    $dialog.CheckFileExists = $true
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { return '' }
    return $dialog.FileName
}

function Select-DestinationFolder {
    param([string]$Description)
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Description
    $dialog.ShowNewFolderButton = $true
    if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { return '' }
    return $dialog.SelectedPath
}

function Select-MaintenanceAction {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $script:maintenanceChoice = ''
    $form = New-Object Windows.Forms.Form
    $form.Text = 'Work Stack Maintenance'
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object Drawing.Size(430, 305)
    $form.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false

    $heading = New-Object Windows.Forms.Label
    $heading.Text = 'Protect and move your local Work Stack data'
    $heading.Font = New-Object Drawing.Font('Segoe UI', 12, [Drawing.FontStyle]::Bold)
    $heading.AutoSize = $true
    $heading.Location = New-Object Drawing.Point(24, 20)
    $form.Controls.Add($heading)

    $note = New-Object Windows.Forms.Label
    $note.Text = "Restore and relocation run only while Work Stack is stopped.`nThe source is never deleted, and replacement creates a safety backup."
    $note.AutoSize = $true
    $note.Location = New-Object Drawing.Point(24, 55)
    $form.Controls.Add($note)

    $actions = @(
        @('Create verified backup', 'Backup'),
        @('Verify a backup file', 'Verify'),
        @('Restore from a backup', 'Restore'),
        @('Move workspace data safely', 'Relocate')
    )
    for ($index = 0; $index -lt $actions.Count; $index++) {
        $button = New-Object Windows.Forms.Button
        $button.Text = $actions[$index][0]
        $button.Tag = $actions[$index][1]
        $button.Size = New-Object Drawing.Size(180, 50)
        $button.Location = New-Object Drawing.Point(24 + (($index % 2) * 200), 105 + ([Math]::Floor($index / 2) * 65))
        $button.Add_Click({
            $script:maintenanceChoice = [string]$this.Tag
            $form.Close()
        })
        $form.Controls.Add($button)
    }
    [void]$form.ShowDialog()
    return $script:maintenanceChoice
}

try {
    if ($interactive) {
        $Action = Select-MaintenanceAction
        if (-not $Action) { exit 0 }
    }

    if ($Action -in @('Backup', 'Restore', 'Relocate')) { Assert-WorkStackOffline }

    if ($Action -eq 'Backup') {
        if (-not (Test-Path -LiteralPath (Join-Path $dataPath 'workspace.json') -PathType Leaf)) {
            throw 'No initialized Work Stack workspace was found to back up.'
        }
        New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
        $result = Invoke-WorkStackRuntime @('--data-dir', $dataPath, 'maintenance', 'backup', '--out', $backupDirectory)
        Show-WorkStackMessage "Verified backup created.`n`n$result"
        exit 0
    }

    if (-not $BackupPath -and $interactive) { $BackupPath = Select-BackupArchive }
    if ($Action -in @('Verify', 'Restore')) {
        if (-not $BackupPath) { throw 'Choose a backup file before continuing.' }
        $BackupPath = [IO.Path]::GetFullPath($BackupPath)
        $verified = Invoke-WorkStackRuntime @('maintenance', 'verify', $BackupPath)
        if ($Action -eq 'Verify') {
            Show-WorkStackMessage "Backup verification passed. No data was changed.`n`n$verified"
            exit 0
        }
    }

    if ($Action -eq 'Restore') {
        if (-not $Destination -and $interactive) {
            if (Confirm-WorkStackAction "Restore into the current Work Stack data folder?`n`nA verified safety backup will be created first.") {
                $Destination = $dataPath
            } else {
                $Destination = Select-DestinationFolder 'Choose an empty folder for the restored workspace'
            }
        }
        if (-not $Destination) { throw 'Choose a restore destination before continuing.' }
        $destinationPath = [IO.Path]::GetFullPath($Destination)
        $hasStore = Test-Path -LiteralPath (Join-Path $destinationPath 'workspace.json') -PathType Leaf
        if ((Test-Path -LiteralPath $destinationPath -PathType Container) -and -not $hasStore) {
            $unrelated = @(Get-ChildItem -LiteralPath $destinationPath -Force)
            if ($unrelated.Count -gt 0) { throw 'Restore destination must be empty or the configured Work Stack data folder.' }
        }
        if (-not (Confirm-WorkStackAction "Restore the verified backup to:`n$destinationPath`n`nThis action cannot be undone from the Maintenance window.")) {
            throw 'Restore was cancelled before any data was changed.'
        }
        $restoreArguments = @('maintenance', 'restore', $BackupPath, '--to', $destinationPath)
        if ($hasStore) {
            $safetyDirectory = Join-Path $backupDirectory 'pre-restore'
            $restoreArguments += @('--replace', '--safety-backups', $safetyDirectory)
        }
        $result = Invoke-WorkStackRuntime $restoreArguments
        Show-WorkStackMessage "Restore completed and verified.`n`n$result"
        exit 0
    }

    if ($Action -eq 'Relocate') {
        if (-not $Destination -and $interactive) {
            $Destination = Select-DestinationFolder 'Choose an empty folder for the relocated workspace'
        }
        if (-not $Destination) { throw 'Choose a relocation destination before continuing.' }
        $destinationPath = [IO.Path]::GetFullPath($Destination)
        $sourcePrefix = $dataPath.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if ($destinationPath.Equals($dataPath, [StringComparison]::OrdinalIgnoreCase) -or
            $destinationPath.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Relocation destination must be separate from the current data folder.'
        }
        if (Test-Path -LiteralPath $destinationPath -PathType Container) {
            if (@(Get-ChildItem -LiteralPath $destinationPath -Force).Count -gt 0) {
                throw 'Relocation destination must be empty.'
            }
        }
        if (-not (Confirm-WorkStackAction "Copy and verify the workspace at:`n$destinationPath`n`nThe original source will be preserved.")) {
            throw 'Relocation was cancelled before any data was changed.'
        }
        $result = Invoke-WorkStackRuntime @('--data-dir', $dataPath, 'maintenance', 'relocate', '--to', $destinationPath)

        $config.data_dir = $destinationPath
        $temporaryConfig = "$configPath.pending-$PID"
        [IO.File]::WriteAllText($temporaryConfig, ($config | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryConfig -Destination $configPath -Force
        Show-WorkStackMessage "Relocation completed and verified. The original source was preserved.`n`n$result"
        exit 0
    }
} catch {
    Show-WorkStackMessage $_.Exception.Message 'Work Stack Maintenance' 'Error'
    exit 1
}
