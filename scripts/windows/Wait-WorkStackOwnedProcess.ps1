# Work Stack owned-process wait helper.
#
# FUNCTIONS ONLY. Dot-sourcing this file must have no effect: no I/O, no
# compiler selection, no process start and no installer effect. Callers pass
# the exact owned instance. Missing ExitCode after a finished wait is unknown,
# never success, and is never inferred from an output file existing.

function Wait-WorkStackOwnedProcess {
    <#
        Bound wait for one already-started process. Does not select, start or
        retry a compiler. Callers must pass the exact owned instance.

        Windows PowerShell 5.1 PassThru process objects only report ExitCode if
        Handle is read while the process still lives. A missing ExitCode after
        a finished wait is unknown, never success.
    #>
    param(
        [Parameter(Mandatory = $true)]$Process,
        [int]$TimeoutMilliseconds = 30000,
        [int]$KillConfirmMilliseconds = 5000
    )

    if ($TimeoutMilliseconds -lt 1 -or $KillConfirmMilliseconds -lt 1) {
        throw 'Owned process waits must be positive and bounded.'
    }

    try { [void]($Process.Handle) } catch { }

    $initialExited = $false
    try {
        $initialExited = [bool]$Process.WaitForExit($TimeoutMilliseconds)
    } catch {
        return [pscustomobject]@{
            State = 'unknown'
            ExitCode = $null
            TimedOut = $false
            Confirmed = $false
            Error = $_.Exception.Message
        }
    }

    if (-not $initialExited) {
        $terminationError = ''
        try { $Process.Kill() } catch { $terminationError = $_.Exception.Message }
        $confirmed = $false
        try {
            $confirmed = [bool]$Process.WaitForExit($KillConfirmMilliseconds)
        } catch {
            $terminationError = ($terminationError + ' ' + $_.Exception.Message).Trim()
        }
        if (-not $confirmed) {
            return [pscustomobject]@{
                State = 'unknown'
                ExitCode = $null
                TimedOut = $true
                Confirmed = $false
                Error = $terminationError
            }
        }
        return [pscustomobject]@{
            State = 'timeout'
            ExitCode = $null
            TimedOut = $true
            Confirmed = $true
            Error = $terminationError
        }
    }

    if ($null -eq $Process.ExitCode) {
        return [pscustomobject]@{
            State = 'unknown'
            ExitCode = $null
            TimedOut = $false
            Confirmed = $true
            Error = ''
        }
    }

    $code = [int]$Process.ExitCode
    if ($code -ne 0) {
        return [pscustomobject]@{
            State = 'failed'
            ExitCode = $code
            TimedOut = $false
            Confirmed = $true
            Error = ''
        }
    }

    return [pscustomobject]@{
        State = 'exited'
        ExitCode = 0
        TimedOut = $false
        Confirmed = $true
        Error = ''
    }
}
