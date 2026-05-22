<#
.SYNOPSIS
  Agent Memory Daemon — 背景自動進化 (Phase B C11).

.DESCRIPTION
  V2 藍圖 §7.3 「ETL daemon 缺口」修補. 取代「manual 跑 promote-cycle」.

  跑什麼:
    1. memory-cli promote-cycle --phase light   (短期記憶 -> 長期升格)
    2. memory-cli skill-maintain                (skill lifecycle 維護, 若存在)

  寫日誌:
    <vault>/11_AI_Mirror/ingestion_logs/daemon_runs.jsonl
      每次跑一條 JSON line: {timestamp, exit_codes, summary}

  排程方式 (Windows 不自動建, 印命令給使用者抄):
    schtasks /create /tn "AgentMemoryDaemon" /tr "powershell -NoProfile -File <絕對路徑>" /sc daily /st 03:00
    schtasks /delete /tn "AgentMemoryDaemon" /f
    schtasks /query /tn "AgentMemoryDaemon"

.PARAMETER VaultRoot
  Vault 路徑. 留空 = 用 ~/.agent_memory/config.toml 預設.

.PARAMETER Once
  一次性執行模式 (預設行為). 跑完即退出.

.PARAMETER ShowSchedule
  只印 schtasks 命令, 不執行 daemon.
#>

param(
    [string]$VaultRoot = "",
    [switch]$Once,
    [switch]$ShowSchedule
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
}
catch { }

. (Join-Path $PSScriptRoot "_dotenv-helper.ps1")
Import-DotEnvIntoProcess -VaultRoot $VaultRoot | Out-Null

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }

# Resolve vault root
$resolvedVault = Resolve-VaultRoot -VaultRoot $VaultRoot
if (-not (Test-Path -LiteralPath $resolvedVault)) {
    Write-Host "[ERR] vault root 不存在: $resolvedVault" -ForegroundColor Red
    exit 1
}

if ($ShowSchedule) {
    $scriptFull = (Resolve-Path -LiteralPath $PSCommandPath).Path
    Write-Host ""
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  Agent Memory Daemon — 排程說明" -ForegroundColor Cyan
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "建立每日凌晨 3 點自動執行的 Windows 排程任務 (整段複製貼到 cmd / PowerShell):" -ForegroundColor Yellow
    Write-Host ""
    $cmdLine = 'schtasks /create /tn "AgentMemoryDaemon" /tr "powershell -NoProfile -File \"' + $scriptFull + '\" -VaultRoot \"' + $resolvedVault + '\" -Once" /sc daily /st 03:00 /f'
    Write-Host "  $cmdLine" -ForegroundColor White
    Write-Host ""
    Write-Host "查看狀態:" -ForegroundColor Yellow
    Write-Host '  schtasks /query /tn "AgentMemoryDaemon" /v /fo list' -ForegroundColor White
    Write-Host ""
    Write-Host "立即執行 (測試, 不必等到凌晨 3 點):" -ForegroundColor Yellow
    Write-Host '  schtasks /run /tn "AgentMemoryDaemon"' -ForegroundColor White
    Write-Host ""
    Write-Host "停用 / 移除:" -ForegroundColor Yellow
    Write-Host '  schtasks /change /tn "AgentMemoryDaemon" /disable' -ForegroundColor White
    Write-Host '  schtasks /delete /tn "AgentMemoryDaemon" /f' -ForegroundColor White
    Write-Host ""
    Write-Host "註: 排程任務跑的時候你不一定要登入, 但需要 Windows 帳號通過驗證." -ForegroundColor DarkGray
    exit 0
}

# === Daemon main ===
Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Agent Memory Daemon — start ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  vault: $resolvedVault" -ForegroundColor DarkGray
Write-Host ""

$logDir = Join-Path $resolvedVault "11_AI_Mirror\ingestion_logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir "daemon_runs.jsonl"

function Invoke-CliCmd {
    # R18 C80 (Codex 第 28 輪 T11.2/T11.3/T13.1 修): 對齊 R12 C46 同 bug fix.
    # 原本 param([string[]]$Args, ...) 用 PowerShell **reserved auto variable** $Args,
    # 被 PowerShell 自動 shadow → $full += $Args 拿空陣列 → CLI 沒收到 subcommand
    # → 噴 "usage: memory-cli [-h] [--vault-root VAULT_ROOT]". 改 $CliArgs.
    param([string[]]$CliArgs, [string]$Label)
    Write-Host "[INFO] $Label..." -ForegroundColor Yellow
    $full = @("-X", "utf8", "-m", "agent_memory.cli")
    if ($VaultRoot) { $full += @("--vault-root", $VaultRoot) }
    $full += $CliArgs
    $full += "--json"
    Push-Location $projectRoot
    try {
        $output = & $pythonExe @full 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    $jsStart = $output.IndexOf('{')
    $jsEnd = $output.LastIndexOf('}')
    $summary = ""
    if ($jsStart -ge 0 -and $jsEnd -gt $jsStart) {
        try {
            $json = $output.Substring($jsStart, $jsEnd - $jsStart + 1) | ConvertFrom-Json
            $summary = ($json | ConvertTo-Json -Compress -Depth 4)
            if ($summary.Length -gt 500) { $summary = $summary.Substring(0, 500) + "..." }
        }
        catch { $summary = "JSON parse fail" }
    }
    $status = if ($exitCode -eq 0) { "OK" } else { "FAIL exit=$exitCode" }
    $color = if ($exitCode -eq 0) { "Green" } else { "Red" }
    Write-Host "  [$status] $Label" -ForegroundColor $color
    if ($summary) { Write-Host "    $($summary.Substring(0, [Math]::Min(200, $summary.Length)))" -ForegroundColor DarkGray }
    return @{ label = $Label; exit_code = $exitCode; summary = $summary }
}

$results = @()
$results += Invoke-CliCmd -CliArgs @("promote-cycle", "--phase", "light", "--max-promotions", "20") -Label "promote-cycle (短期→長期升格)"

# skill-maintain 不是所有版本都有, 用 try
$skillExists = $true
try {
    $checkOutput = (& $pythonExe -X utf8 -m agent_memory.cli --help 2>&1 | Out-String)
    if ($checkOutput -notmatch 'skill-maintain') { $skillExists = $false }
}
catch { $skillExists = $false }

if ($skillExists) {
    $results += Invoke-CliCmd -CliArgs @("skill-maintain") -Label "skill-maintain (技能 lifecycle)"
}

# C13: wikilinks graph rebuild (給 chat 一跳擴展用)
# 直接用 Python inline (還沒包成 CLI sub-command)
Write-Host "[INFO] wikilinks-graph (Phase A C13)..." -ForegroundColor Yellow
$wgScript = @"
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from agent_memory.wikilinks_graph import rebuild_and_save
r = rebuild_and_save(Path(r'$resolvedVault'))
import json
print(json.dumps(r, ensure_ascii=False))
"@
Push-Location $projectRoot
try {
    $wgOut = & $pythonExe -X utf8 -c $wgScript 2>&1 | Out-String
    $wgExit = $LASTEXITCODE
}
finally {
    Pop-Location
}
$wgStatus = if ($wgExit -eq 0) { "OK" } else { "FAIL exit=$wgExit" }
$wgColor = if ($wgExit -eq 0) { "Green" } else { "Red" }
Write-Host "  [$wgStatus] wikilinks-graph" -ForegroundColor $wgColor
if ($wgOut) {
    $wgPreview = $wgOut.Trim()
    if ($wgPreview.Length -gt 200) { $wgPreview = $wgPreview.Substring(0, 200) + "..." }
    Write-Host "    $wgPreview" -ForegroundColor DarkGray
}
$results += @{ label = "wikilinks-graph"; exit_code = $wgExit; summary = $wgOut.Trim() }

# 寫 jsonl log
$entry = [ordered]@{
    timestamp = (Get-Date).ToUniversalTime().ToString("o")
    vault = $resolvedVault
    actions = $results
    overall_ok = ([bool](($results | Where-Object { $_.exit_code -ne 0 }).Count -eq 0))
}
$line = ($entry | ConvertTo-Json -Compress -Depth 6)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::AppendAllText($logFile, $line + "`n", $utf8NoBom)

Write-Host ""
if ($entry.overall_ok) {
    $okStr = "OK"
    $okColor = "Green"
    $exitCode = 0
} else {
    $okStr = "PARTIAL FAIL"
    $okColor = "Yellow"
    $exitCode = 1
}
Write-Host "[$okStr] daemon 跑完. log: $logFile" -ForegroundColor $okColor
Write-Host ""
exit $exitCode
