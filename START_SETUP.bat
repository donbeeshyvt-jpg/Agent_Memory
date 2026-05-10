@echo off
setlocal
cd /d "%~dp0"

echo [INFO] Starting Agent Memory Core setup wizard...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\first-run-wizard.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo [ERR] Setup finished with errors. exit=%EXITCODE%
) else (
  echo [OK] Setup finished successfully.
)

echo.
pause
exit /b %EXITCODE%
