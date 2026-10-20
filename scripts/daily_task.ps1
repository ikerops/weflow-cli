# Runs a daily report pass, with everything written to logs/daily-<date>.log.
#
# Two modes, because a WeChat official-account day only fills up gradually:
#
#   daily_task.ps1 noon   -> today so far (run at 13:00; half a day of push messages)
#   daily_task.ps1        -> yesterday, complete (run at 00:05; same as before)
#
# The second pass is incremental by design: `.run_state.json` makes biz_daily skip the
# articles the noon pass already handled, so 00:05 only fetches/summarises what arrived
# in the afternoon, while README/index.html are rebuilt from every md on disk.
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

# Mode is the first positional word; anything else is forwarded to pipeline.py.
$mode = 'day'
$rest = @($args)
if ($args.Count -gt 0 -and ($args[0] -eq 'noon' -or $args[0] -eq 'day')) {
    $mode = $args[0]
    $rest = if ($args.Count -gt 1) { @($args[1..($args.Count - 1)]) } else { @() }
}

$today = (Get-Date).ToString('yyyy-MM-dd')
$yesterday = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
$targetDay = if ($mode -eq 'noon') { $today } else { $yesterday }

$stamp = Get-Date -Format 'yyyy-MM-dd'
$suffix = if ($mode -eq 'noon') { '-noon' } else { '' }
$out = Join-Path $logDir ('daily-{0}{1}.log' -f $stamp, $suffix)
$err = Join-Path $logDir ('daily-{0}{1}.err.log' -f $stamp, $suffix)

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$python = (Get-Command python.exe).Source
$script = Join-Path $PSScriptRoot 'pipeline.py'
$modeArgs = if ($mode -eq 'noon') { @('--date', $today) } else { @('--yesterday') }
$allArgs = @($script) + $modeArgs + @('--prefetch-bodies') + $rest

# Idempotent "make sure the target day is as complete as it can be right now", not
# "run at 08:20".
#
# Why: this machine slept from 2026-10-17 02:09 to 2026-10-18 15:26 and BOTH 08:20
# triggers were counted as missed while StartWhenAvailable=On did not start the task
# on wake (NumberOfMissedRuns=2, no run). A schedule that only fires if the machine
# happens to be awake at one instant is not a schedule. With the guard below the task
# can be triggered cheaply and often (schedule + at logon): if the target is already
# done this exits in milliseconds, otherwise it produces/extends the day.
#
# The day-mode guard deliberately does NOT trust index.html alone: the noon pass also
# writes one, so "index.html exists" would make the 00:05 pass skip the afternoon and
# leave the day half-done behind a complete-looking page. `.articles.json`'s
# generated_at tells us when the last pass actually ran; anything during that day
# (before 23:00) is a mid-day artifact and must be extended.
#
# Note the day pass runs at 00:05 **the next day**, so a finished day is one whose
# generated_at is later than the day itself - not one stamped at 23:xx. Testing
# `hour -ge 23` alone would have made every 00:05 run redo the whole day (observed
# while verifying this guard: generated_at 00:20 was judged incomplete).
function Test-DayComplete([string]$dir, [string]$targetDay) {
    $idx = Join-Path $dir 'index.html'
    if (-not (Test-Path $idx)) { return $false }
    $art = Join-Path $dir '.articles.json'
    if (-not (Test-Path $art)) { return $true }   # old artifact without json: trust it
    try {
        $j = Get-Content $art -Raw -Encoding UTF8 | ConvertFrom-Json
        $gen = [datetime]::ParseExact([string]$j.generated_at, 'yyyy-MM-dd HH:mm:ss', $null)
        $genDay = $gen.ToString('yyyy-MM-dd')
        # finished = generated on a later day (the usual 00:05 pass), or late the same night
        return (($genDay -gt $targetDay) -or (($genDay -eq $targetDay) -and ($gen.Hour -ge 23)))
    } catch { return $true }
}

# Skipped when arguments are passed, so `daily_task.ps1 --help` still works as a
# smoke test with no side effects.
if ($rest.Count -eq 0) {
    $dayDir = Join-Path $root ('output\biz-daily\' + $targetDay)
    $done = if ($mode -eq 'noon') { Test-Path (Join-Path $dayDir '.noon_done') } else { Test-DayComplete $dayDir $targetDay }
    if ($done) {
        $note = '===== {0}  [{1}] {2} already complete - skip =====' -f `
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $mode, $targetDay
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
    while (Test-Path (Join-Path $logDir ('daily-{0}{1}.run{2}.log' -f $stamp, $suffix, $n))) { $n++ }
    Move-Item -Path $out -Destination (Join-Path $logDir ('daily-{0}{1}.run{2}.log' -f $stamp, $suffix, $n))
    if (Test-Path $err) {
        Move-Item -Path $err -Destination (Join-Path $logDir ('daily-{0}{1}.run{2}.err.log' -f $stamp, $suffix, $n))
    }
}

# Start-Process writes the raw bytes through: no PowerShell error-record wrapping, and
# no shell quoting to get wrong.
$proc = Start-Process -FilePath $python -ArgumentList $allArgs -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $out -RedirectStandardError $err

if ($proc.ExitCode -eq 0 -and $rest.Count -eq 0) {
    # Marker for the noon pass only; the day pass is judged by generated_at above.
    if ($mode -eq 'noon') {
        $dayDir = Join-Path $root ('output\biz-daily\' + $targetDay)
        if (Test-Path $dayDir) {
            Set-Content -Path (Join-Path $dayDir '.noon_done') -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -Encoding UTF8
        }
    }

    # Mail is a separate step on purpose: the day's product is already on disk, so a mail
    # failure must not turn a successful run into a failed one (and must not stop the
    # noon marker from being written). It has its own log pair.
    $mailScript = Join-Path $PSScriptRoot 'daily_mail.py'
    $mailOut = Join-Path $logDir ('daily-mail-{0}{1}.log' -f $stamp, $suffix)
    $mailErr = Join-Path $logDir ('daily-mail-{0}{1}.err.log' -f $stamp, $suffix)
    if (Test-Path $mailScript) {
        $mailProc = Start-Process -FilePath $python -ArgumentList @($mailScript, '--date', $targetDay) `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $mailOut -RedirectStandardError $mailErr
        if ($mailProc.ExitCode -ne 0) {
            Add-Content -Path (Join-Path $logDir 'daily-task.log') -Value `
                ('===== {0}  [{1}] {2} mail failed (exit {3}), see {4} =====' -f `
                    (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $mode, $targetDay, $mailProc.ExitCode, $mailErr) -Encoding UTF8
        }
    }
}

exit $proc.ExitCode
