@echo off
setlocal
cd /d "%~dp0"

REM Force UTF-8 console codepage so PowerShell can decode Python -X utf8 output correctly.
REM Without this, fresh Windows users on CJK locales (CP-950/CP-936/CP-932) see
REM "command failed: powershell ..." because ConvertFrom-Json receives mojibake.
chcp 65001 >NUL

REM Default entry: 美化選單 (menu.ps1) — 內含快速設定 / 自訂設定 / 上線 DC / 切 LLM / CLI 試聊。
REM 想直接跑舊版 wizard：START_SETUP.bat --legacy 或自行呼叫 scripts\first-run-wizard.ps1
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
