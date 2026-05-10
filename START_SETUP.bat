@echo off
setlocal
cd /d "%~dp0"

chcp 65001 >NUL

if /I "%~1"=="--legacy" (
  shift
  echo [INFO] Starting legacy first-run-wizard.ps1...
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\first-run-wizard.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\menu.ps1" %*
)
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo [ERR] Exited with errors. exit=%EXITCODE%
)

echo.
pause
exit /b %EXITCODE%
