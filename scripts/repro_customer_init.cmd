@echo off
chcp 65001 >nul
setlocal
rem ============================================================================
rem  Reproduce the customer's failing scenario on YOUR machine, safely.
rem
rem  What he hit (2026-10-18):
rem      init     -> "NT scan failed (exit 9009)"  ...  then "初始化完成!"
rem      sessions -> "未找到会话"
rem  The tool could not read the database but still reported success.
rem
rem  This window recreates it:
rem    * throw-away HOME (D:\tmp\wf-test) - your real config is never touched
rem    * PATH with node + system tools but NO python (the "Store placeholder" case)
rem    * init / sessions / check --report, in that order
rem  Read-only. No network, no API key, no cost.
rem
rem  PREREQUISITE: open WeChat and log in first.
rem ============================================================================

cd /d "%~dp0.."
if not exist "dist\bin\weflow-cli.js" (
  echo [ERROR] dist\bin\weflow-cli.js not found. Run "npm run build" in this repo first.
  pause
  exit /b 1
)

set "SCOPE=D:\tmp\wf-test"
if not exist "%SCOPE%" mkdir "%SCOPE%"
set "USERPROFILE=%SCOPE%"
set "HOME=%SCOPE%"

set "NODEDIR="
for /f "delims=" %%i in ('where node 2^>nul') do if not defined NODEDIR set "NODEDIR=%%~dpi"
if not defined NODEDIR (
  echo [ERROR] node.exe not found on PATH.
  pause
  exit /b 1
)
set "PATH=%NODEDIR%;C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0"

set "DATA=D:\微信\xwechat_files"

echo ==========================================================================
echo  A) Environment: python MUST fail here - that is the whole point
echo ==========================================================================
python --version
echo.
echo   ^(error / "not recognized" above = correct^)
echo.

echo ==========================================================================
echo  B1) init with an explicit path - guarantees we reach the key step
echo ==========================================================================
if exist "%DATA%" (
  node dist\bin\weflow-cli.js init --path "%DATA%"
) else (
  echo   [WARN] %DATA% not found - running init without a path instead
  node dist\bin\weflow-cli.js init
)
echo.
echo --------------------------------------------------------------------------
echo  B2) init again WITHOUT a path - does auto-discovery still work?
echo --------------------------------------------------------------------------
node dist\bin\weflow-cli.js init
echo.

echo ==========================================================================
echo  C) sessions - should point you at check --report
echo ==========================================================================
node dist\bin\weflow-cli.js sessions
echo.

echo ==========================================================================
echo  D) check --report - should blame Python, NOT the WeChat version
echo ==========================================================================
node dist\bin\weflow-cli.js check --report
echo.

echo ==========================================================================
echo  DONE.
echo    - If every section matches what you expected, the bug is fixed.
echo    - Otherwise send a screenshot of this window (or the lines that differ).
echo    - Close this window when done: HOME/PATH were changed inside it only.
echo ==========================================================================
pause
