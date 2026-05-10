@echo off
setlocal
cd /d "%~dp0"

REM Force UTF-8 console codepage so PowerShell can decode Python -X utf8 output correctly.
REM Without this, fresh Windows users on CJK locales (CP-950/CP-936/CP-932) see
REM "command failed: powershell ..." because ConvertFrom-Json receives mojibake.
chcp 65001 >NUL

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
