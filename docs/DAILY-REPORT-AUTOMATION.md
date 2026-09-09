# Schedule a daily report draft with an existing scheduler

Use Windows Task Scheduler, your Linux scheduler, or your existing orchestrator to invoke the Work Stack CLI. Work Stack does not need an internal timer or an LLM for this operation. This guide does not enable a schedule, finalize a report, or send one.

## Check the build and owner first

Use the Python executable and launcher belonging to the **same verified Work Stack build**. A version number alone does not prove that build contains this command. In PowerShell, substitute your absolute paths:

```powershell
$Python = 'C:\path\to\workstack\runtime\python.exe'
$Launcher = 'C:\path\to\workstack\run_work_stack.py'
& $Python $Launcher report create --help
```

The help must show the required `--date` option. If `report` is an invalid choice, update to a build that contains this feature before scheduling. The installed 1.0.13 build inspected during development did not contain it; later builds must be checked themselves.

Keep the existing GUI owner open. Select the same data directory and runtime configuration that owner uses. If your installation requires `WORK_STACK_RUNTIME`, use that installation's matching value; do not point it at another running workspace. The CLI refuses an unavailable or mismatched owner. Do not start a second `graph serve`, take its lease, or edit storage JSON to get past a refusal.

First try one explicit date manually:

```powershell
$DataDir = 'C:\path\to\selected\workspace'
& $Python $Launcher --data-dir $DataDir report create --date '2026-09-08'
$LASTEXITCODE
```

Use a date appropriate to your own workspace. This creates a `daily-v1` **draft** through the running owner's HTTP API. It accepts no arbitrary Markdown or Task body. In Daily Review, use **Saved reports → Refresh list** to see a draft created by the CLI.

## Windows Task Scheduler recipe

Save a small PowerShell launcher outside the repository. Replace the paths and choose your reporting-day policy before using it:

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Python = 'C:\path\to\workstack\runtime\python.exe'
$Repo = 'C:\path\to\workstack'
$DataDir = 'C:\path\to\selected\workspace'
$DayPolicy = 'PreviousLocalDay' # or CurrentLocalDay
if ($DayPolicy -notin @('PreviousLocalDay', 'CurrentLocalDay')) {
    throw 'Choose a reporting-day policy.'
}
$Offset = if ($DayPolicy -eq 'PreviousLocalDay') { -1 } else { 0 }
$ReportDate = (Get-Date).Date.AddDays($Offset).ToString(
    'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
Set-Location -LiteralPath $Repo
& $Python (Join-Path $Repo 'run_work_stack.py') --data-dir $DataDir report create --date $ReportDate
exit $LASTEXITCODE
```

Each variable is passed as a separate argument, including paths containing spaces. The date uses the machine's local day; configure a different timezone explicitly if that is not your reporting timezone. `PreviousLocalDay` shortly after midnight covers a completed day. `CurrentLocalDay` in the evening creates an interim draft that may become stale after later work.

Configure an existing Task Scheduler task to run `powershell.exe` with `-NoProfile -NonInteractive -File "C:\path\to\your\launcher.ps1"`, using the account that owns the GUI. Use your environment's approved script execution policy. Choose:

- Run only when that user is logged on, with the GUI owner already available.
- Do not restart on failure or run missed triggers later.
- If an instance is running, **Do not start a new instance**.
- A bounded execution timeout; a killed invocation has an uncertain outcome.

Task Scheduler provides execution history and exit results. Do not add a second retry queue or report-history file. These settings are a recipe: automatic triggers, sleep/resume and organization policies still need testing in your own environment.

## Linux equivalent

The same CLI can be called from a shell script while the owner is running. Pin actual executable paths. For a GNU/Linux host with GNU `date`:

```sh
#!/bin/sh
set -eu
report_date=$(TZ=Asia/Seoul /bin/date -d yesterday +%Y-%m-%d)
exec /absolute/path/python3 /absolute/path/workstack/run_work_stack.py \
  --data-dir '/absolute/path/to/selected workspace' \
  report create --date "$report_date"
```

Configure the scheduler's trigger timezone separately. `CRON_TZ` is not supported by every cron and must not be assumed to set the child's date timezone. Use a nonblocking lock such as your host's `flock -n` to prevent overlapping instances. Disable catch-up and retries; missed days require a deliberate explicit-date invocation after the owner returns. This guide has not tested an actual corporate cron deployment.

## Results and retries

Exit **0** means the CLI validated a draft-create receipt. Stdout contains a bounded JSON summary, not the report body. Exit **2** is shared by usage errors, owner/refusal errors, duplicate periods and commit-unknown; it does **not** identify the reason by itself. Other nonzero exits and timeouts are also failures, never successful skips.

On failure, do not automatically invoke the command again. Read its diagnostic and inspect the current Saved reports state through the owner. A stale or empty GUI list alone does not prove an uncertain write was absent. Every new invocation creates a new idempotency intent; only the CLI's internal identical replay preserves the original one. A duplicate active report for the same workspace/day is refused, leaving the existing draft intact.

Confirm the manual create/read-back and duplicate refusal first. Test unavailable-owner and interrupted-write scenarios in an isolated workspace, without closing or altering your current work session. Review/finalize/send remain deliberate operations outside this recipe; weekly report persistence is not supported by this command.

Development acceptance used a synthetic owner kept open throughout an actual CLI and browser round-trip. It verified one draft, explicit GUI refresh, duplicate refusal, and unchanged unrelated storage documents. That evidence does not claim an installed release, a real scheduled trigger, or company-environment acceptance.
