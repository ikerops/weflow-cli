@echo off
chcp 65001 >nul
setlocal
rem One-click installer for the two daily-report scheduled tasks.
rem
rem Why this .cmd exists: agent sandboxes and restricted shells refuse Task Scheduler writes
rem as a class (schtasks /create, the Schedule.Service COM API, and even the in-memory
rem New-ScheduledTask* builders all return Access is denied while reads keep working).
rem A process started from your own desktop is outside that boundary, so this asks for
rem elevation itself and runs install_tasks.ps1 there.

set "PS1=%~dp0install_tasks.ps1"
if not exist "%PS1%" (
  echo [ERROR] install_tasks.ps1 not found next to this file.
  pause
  exit /b 1
)

echo Requesting administrator privileges...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','\"%PS1%\"') -Verb RunAs -ErrorAction Stop; exit 0 } catch { Write-Host ('Could not elevate: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
if errorlevel 1 (
  echo.
  echo If the elevation prompt was cancelled: right-click this file and pick "Run as administrator".
  pause
  exit /b 1
)

echo.
echo A new window opened and is registering the tasks. It stays open so you can read the result.
echo To remove them later:  install_tasks.ps1 -Uninstall
echo.
pause
