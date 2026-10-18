# Runs the daily report for YESTERDAY, with everything written to logs/daily-<date>.log.
#
# Why this wrapper exists instead of putting the command straight into the scheduled task:
#
#  1. The task must not depend on a build artifact. It calls the python layer
#     (scripts/pipeline.py) directly, which is the same source the repo builds from.
#  2. A task whose output goes nowhere answers "it failed" and nothing else. One log
#     file per run, so "why was yesterday short?" has an answer.
#  3. Redirecting stdout to a FILE changes python's stdout encoding (it stops being the
#     console and becomes the locale codepage). A title containing a character outside
#     that codepage would then raise UnicodeEncodeError *after* the day was already
#     written, i.e. the run looks like a failure while the artifacts are fine. Hence
#     PYTHONIOENCODING/PYTHONUTF8 below.
#
# Extra arguments are appended, so `daily_task.ps1 --help` is a safe smoke test (it
# prints the pipeline's help into that day's log and exits without touching anything).
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$stamp = Get-Date -Format 'yyyy-MM-dd'
$out = Join-Path $logDir ('daily-{0}.log' -f $stamp)
$err = Join-Path $logDir ('daily-{0}.err.log' -f $stamp)

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$python = (Get-Command python.exe).Source
$script = Join-Path $PSScriptRoot 'pipeline.py'
$allArgs = @($script, '--yesterday', '--prefetch-bodies') + $args

# Idempotent "make sure yesterday is done", not "run at 08:20".
#
# Why: this machine slept from 2026-10-17 02:09 to 2026-10-18 15:26 and BOTH 08:20
# triggers were counted as missed while StartWhenAvailable=On did not start the task
# on wake (NumberOfMissedRuns=2, no run). A schedule that only fires if the machine
# happens to be awake at one instant is not a schedule. With the guard below the task
# can be triggered cheaply and often (daily + at logon): if yesterday's reader page
# already exists this exits in milliseconds, otherwise it produces the day.
#
# Note the marker is index.html, i.e. the *complete daily* form: a backfilled or
# half-written day (only .articles.json) is NOT "done" and is regenerated.
# Skipped when arguments are passed, so `daily_task.ps1 --help` still works as a
# smoke test with no side effects.
if ($args.Count -eq 0) {
    $yesterday = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
    $doneMarker = Join-Path $root ('output\biz-daily\' + $yesterday + '\index.html')
    if (Test-Path $doneMarker) {
        $note = '===== {0}  {1} already complete ({2}) - skip =====' -f `
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $yesterday, $doneMarker
        Add-Content -Path (Join-Path $logDir 'daily-task.log') -Value $note -Encoding UTF8
        exit 0
    }
}

# Start-Process OVERWRITES its redirect targets, so a same-day rerun would erase the log
# of the run that just failed - exactly the one worth keeping. Rotate any existing pair to
# daily-<date>.run<N>[.err].log first, so every attempt stays on disk. N is the first free
# index, which also works when several reruns happen in one day.
if (Test-Path $out) {
    $n = 1
    while (Test-Path (Join-Path $logDir ('daily-{0}.run{1}.log' -f $stamp, $n))) { $n++ }
    Move-Item -Path $out -Destination (Join-Path $logDir ('daily-{0}.run{1}.log' -f $stamp, $n))
    if (Test-Path $err) {
        Move-Item -Path $err -Destination (Join-Path $logDir ('daily-{0}.run{1}.err.log' -f $stamp, $n))
    }
}

# Start-Process writes the raw bytes through: no PowerShell error-record wrapping, and
# no shell quoting to get wrong.
$proc = Start-Process -FilePath $python -ArgumentList $allArgs -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $out -RedirectStandardError $err

exit $proc.ExitCode
