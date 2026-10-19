# Register / update the two scheduled tasks for the official-account daily report.
# Idempotent: running it again overwrites the definitions with the same values.
#
# Why this cannot always be done for you: writing to Task Scheduler is refused in a
# restricted shell / agent sandbox, and refused as a *class* - measured on 2026-10-19 with
# an elevated Administrator token, all three write paths said "Access is denied" while
# reads kept working:
#     schtasks /create ...                      -> Access is denied
#     (New-Object -ComObject Schedule.Service)   -> RegisterTaskDefinition 0x80070005
#     New-ScheduledTaskAction / -SettingsSet ... -> 拒绝访问  (even the in-memory builders)
# So "I can see the tasks but cannot create them" is not an illusion, and it is not fixed
# by "run as administrator". The one reliable route is a terminal the sandbox does not
# supervise: double-click scripts\install_tasks.cmd (it asks for elevation itself), or run
# this file from your own PowerShell window.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\install_tasks.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\install_tasks.ps1 -Uninstall
#
# The two tasks:
#   WeFlow Daily       00:05 + at logon   -> YESTERDAY (the last complete day)
#   WeFlow Daily Noon  13:00              -> TODAY so far (half a day of pushes)
#
# Why 00:05 the next day instead of 24:00 tonight: it is the same instant, but "yesterday"
# then lines up with the date directory the run writes into, and it does not collide with
# other jobs on the hour. Official accounts keep pushing until 24:00, so this is the only
# moment that has the complete day.
#
# This file is intentionally ASCII-only: Windows PowerShell 5.1 reads a BOM-less file as
# the ANSI codepage, so non-ASCII text here turns into mojibake *and* a parse error.
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\daily_task.ps1'
if (-not (Test-Path $script)) { throw "not found: $script (this script belongs in the repo's scripts\ folder)" }

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Need elevation: reopen PowerShell as administrator, then run this again.' -ForegroundColor Yellow
    exit 1
}

if ($Uninstall) {
    foreach ($name in @('WeFlow Daily Noon', 'WeFlow Daily')) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "removed: $name"
        }
    }
    exit 0
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$psExe = 'powershell.exe'
$common = '-NoProfile -ExecutionPolicy Bypass -File ' + $script

# End of day: yesterday. The at-logon trigger is the catch-up: this machine slept for
# 37 hours and StartWhenAvailable did NOT actually start the missed run (NumberOfMissedRuns=2,
# no run record), so "it will catch up by itself" is not something to rely on.
Register-ScheduledTask -TaskName 'WeFlow Daily' `
    -Action (New-ScheduledTaskAction -Execute $psExe -Argument $common) `
    -Trigger @((New-ScheduledTaskTrigger -Daily -At '00:05'),
               (New-ScheduledTaskTrigger -AtLogOn)) `
    -Settings $settings `
    -Description 'WeChat daily report for yesterday (00:05 + at logon, idempotent)' -Force | Out-Null

# Noon: today so far. Deliberately NO at-logon trigger: a morning logon would produce a
# very thin "noon" report and write the .noon_done marker, which would then make the real
# 13:00 run skip itself (the guard keys on that marker).
Register-ScheduledTask -TaskName 'WeFlow Daily Noon' `
    -Action (New-ScheduledTaskAction -Execute $psExe -Argument ($common + ' noon')) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At '13:00') `
    -Settings $settings `
    -Description 'WeChat daily report for today so far (13:00)' -Force | Out-Null

foreach ($name in @('WeFlow Daily', 'WeFlow Daily Noon')) {
    $t = Get-ScheduledTask -TaskName $name
    $i = Get-ScheduledTaskInfo -TaskName $name
    Write-Host ("{0,-20} {1,-8} next {2}" -f $t.TaskName, $t.State, $i.NextRunTime) -ForegroundColor Green
}
Write-Host ''
Write-Host 'Run one now instead of waiting:'
Write-Host '  Start-ScheduledTask -TaskName "WeFlow Daily Noon"   # today so far'
Write-Host '  Start-ScheduledTask -TaskName "WeFlow Daily"        # yesterday, complete'
Write-Host 'Logs: <repo>\logs\daily-<date>[-noon].log'
