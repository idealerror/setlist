@echo off
REM Double-clickable entry point for run-client.ps1.
REM
REM Windows does not register a "run" verb for .ps1 -- double-clicking one
REM opens it in an editor instead, deliberately, so a downloaded script cannot
REM execute on a click. A .cmd file does launch on double-click, so this shim
REM exists purely to invoke the real script.
REM
REM -ExecutionPolicy Bypass applies to this one invocation only. It does not
REM change the machine or user policy, which stays at the default.
REM
REM This is for manual/interactive use. The venue PC should run the client via
REM the Scheduled Task from install-task.ps1, not by someone clicking an icon.

setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-client.ps1" %*

REM The supervisor loops forever, so reaching here means it exited or failed to
REM start. Hold the window open so the error is readable rather than flashing past.
echo.
echo Supervisor exited with code %ERRORLEVEL%.
pause
