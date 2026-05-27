<#
.SYNOPSIS
  Companion Curator Daemon — V3-I1 自動跑 L3 24h + L4 7d (user 2026-05-27 audit Finding 4).

.DESCRIPTION
  V3-I1 對齊 V2 agent-memory-daemon.ps1 pattern, V3 companion 版.

  跑什麼 (按 last_run_at 判定):
    - layer3_24h_medium: 距上次 ≥ 24h 才跑 (LLM 摘要 daily_knowledge + Mood Diary + Daily Journal + Inside Joke 偵測)
    - layer4_7d_deep:    距上次 ≥ 7d 才跑 (LLM 摘要 external_ingest + 90d archive)

  寫日誌:
    <vault>/.ai/companion_curator_state.json   (last_run_at 持久化)
    <vault>/.ai/companion_daemon_runs.jsonl    (每跑 1 條 JSON line)

  排程方式 (Windows, 用 -ShowSchedule 印命令):
    schtasks /create /tn "CompanionCuratorDaemon" /tr "powershell -NoProfile -File <絕對路徑> -VaultRoot <vault>" /sc hourly /st 00:00

  推薦: 每小時跑一次, daemon 內判 24h/7d gate.

.PARAMETER VaultRoot
  Vault 路徑 (絕對路徑). e.g. Z:\Cursor練習用\Agent_Memory\test\SecondBrains\companion_test

.PARAMETER Once
  跑一次就退 (預設行為, 適合 schtasks 排程).

.PARAMETER Loop
  常駐模式 - sleep 300s 後重跑 (給 nohup / Windows Service 用).

.PARAMETER Force
  強制跑 (忽略 last_run_at gate, debug 用).

.PARAMETER ShowSchedule
  只印 schtasks 命令, 不跑 daemon.

.EXAMPLE
  # 一次性跑 (預設)
  .\scripts\companion-curator-daemon.ps1 -VaultRoot "Z:\Cursor練習用\Agent_Memory\test\SecondBrains\companion_test"

  # 看排程命令
  .\scripts\companion-curator-daemon.ps1 -VaultRoot "..." -ShowSchedule

  # 常駐 loop 模式
  .\scripts\companion-curator-daemon.ps1 -VaultRoot "..." -Loop
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$VaultRoot,
    [switch]$Once,
    [switch]$Loop,
    [switch]$Force,
    [switch]$ShowSchedule
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
}
catch { }

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }

# Resolve vault root
$resolvedVault = (Resolve-Path -LiteralPath $VaultRoot -ErrorAction SilentlyContinue).Path
if (-not $resolvedVault -or -not (Test-Path -LiteralPath $resolvedVault)) {
    Write-Host "[ERR] vault root 不存在: $VaultRoot" -ForegroundColor Red
    exit 1
}

if ($ShowSchedule) {
    $scriptFull = (Resolve-Path -LiteralPath $PSCommandPath).Path
    Write-Host ""
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  Companion Curator Daemon — 排程說明 (V3-I1)" -ForegroundColor Cyan
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "建議: 每小時跑 1 次 (daemon 內部判 24h/7d gate)" -ForegroundColor Yellow
    Write-Host ""
    $cmdLine = 'schtasks /create /tn "CompanionCuratorDaemon" /tr "powershell -NoProfile -File \"' + $scriptFull + '\" -VaultRoot \"' + $resolvedVault + '\" -Once" /sc hourly /mo 1 /st 00:30 /f'
    Write-Host "  $cmdLine" -ForegroundColor White
    Write-Host ""
    Write-Host "查看狀態:" -ForegroundColor Yellow
    Write-Host '  schtasks /query /tn "CompanionCuratorDaemon" /v /fo list' -ForegroundColor White
    Write-Host ""
    Write-Host "立即執行 (測試):" -ForegroundColor Yellow
    Write-Host '  schtasks /run /tn "CompanionCuratorDaemon"' -ForegroundColor White
    Write-Host ""
    Write-Host "停用 / 移除:" -ForegroundColor Yellow
    Write-Host '  schtasks /change /tn "CompanionCuratorDaemon" /disable' -ForegroundColor White
    Write-Host '  schtasks /delete /tn "CompanionCuratorDaemon" /f' -ForegroundColor White
    Write-Host ""
    Write-Host "註: 排程任務跑的時候你不一定要登入, 但需要 Windows 帳號通過驗證." -ForegroundColor DarkGray
    exit 0
}

# State + log file paths
$aiDir = Join-Path $resolvedVault ".ai"
if (-not (Test-Path -LiteralPath $aiDir)) {
    New-Item -ItemType Directory -Path $aiDir -Force | Out-Null
}
$stateFile = Join-Path $aiDir "companion_curator_state.json"
$logFile = Join-Path $aiDir "companion_daemon_runs.jsonl"

function Read-State {
    if (-not (Test-Path -LiteralPath $stateFile)) {
        return @{ last_layer3_at = $null; last_layer4_at = $null }
    }
    try {
        $raw = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8
        $obj = $raw | ConvertFrom-Json
        $hash = @{}
        $obj.PSObject.Properties | ForEach-Object { $hash[$_.Name] = $_.Value }
        if (-not $hash.ContainsKey('last_layer3_at')) { $hash['last_layer3_at'] = $null }
        if (-not $hash.ContainsKey('last_layer4_at')) { $hash['last_layer4_at'] = $null }
        return $hash
    }
    catch {
        return @{ last_layer3_at = $null; last_layer4_at = $null }
    }
}

function Write-State($state) {
    $json = $state | ConvertTo-Json -Depth 4 -Compress
    Set-Content -LiteralPath $stateFile -Value $json -Encoding UTF8
}

function Should-Run-Layer($lastRun, $hoursThreshold) {
    if ($Force) { return $true }
    if (-not $lastRun) { return $true }
    try {
        $lastDt = [DateTime]::Parse($lastRun)
        $age = (Get-Date).ToUniversalTime() - $lastDt.ToUniversalTime()
        return $age.TotalHours -ge $hoursThreshold
    }
    catch {
        return $true
    }
}

function Invoke-Layer($layerName, $pythonModuleCall) {
    $startTime = (Get-Date).ToUniversalTime().ToString("o")
    Write-Host "[INFO] 跑 $layerName..." -ForegroundColor Yellow
    Push-Location $projectRoot
    try {
        # 用 python -c 跑 layer
        $pyCode = "import json,sys; from pathlib import Path; from agent_memory.companion.companion_curator import $pythonModuleCall as run; result = run(Path(r'$resolvedVault')); print(json.dumps({'layer': result.layer, 'actions': result.actions_performed}, ensure_ascii=False))"
        $output = & $pythonExe -X utf8 -c $pyCode 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    $endTime = (Get-Date).ToUniversalTime().ToString("o")

    $summary = ""
    $actions = @()
    $jsStart = $output.IndexOf('{')
    $jsEnd = $output.LastIndexOf('}')
    if ($jsStart -ge 0 -and $jsEnd -gt $jsStart) {
        try {
            $json = $output.Substring($jsStart, $jsEnd - $jsStart + 1) | ConvertFrom-Json
            $summary = ($json | ConvertTo-Json -Compress -Depth 4)
            if ($json.actions) { $actions = $json.actions }
        }
        catch { }
    }
    $status = if ($exitCode -eq 0) { "OK" } else { "FAIL exit=$exitCode" }
    $color = if ($exitCode -eq 0) { "Green" } else { "Red" }
    Write-Host "  [$status] $layerName" -ForegroundColor $color
    if ($actions.Count -gt 0) {
        foreach ($a in $actions) {
            Write-Host "    - $a" -ForegroundColor DarkGray
        }
    }
    elseif ($exitCode -ne 0) {
        Write-Host "    stderr: $($output.Substring(0, [Math]::Min(300, $output.Length)))" -ForegroundColor DarkRed
    }
    return @{
        layer = $layerName
        started_at = $startTime
        ended_at = $endTime
        exit_code = $exitCode
        actions = $actions
    }
}

function Run-Daemon-Once {
    $state = Read-State
    $nowIso = (Get-Date).ToUniversalTime().ToString("o")

    Write-Host ""
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  Companion Curator Daemon — start ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))" -ForegroundColor Cyan
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  vault: $resolvedVault" -ForegroundColor DarkGray
    Write-Host "  last L3: $($state['last_layer3_at'])" -ForegroundColor DarkGray
    Write-Host "  last L4: $($state['last_layer4_at'])" -ForegroundColor DarkGray
    Write-Host ""

    $runResults = @()

    # L3 24h gate
    if (Should-Run-Layer $state['last_layer3_at'] 24) {
        $r = Invoke-Layer "layer3_24h_medium" "run_layer3_24h_medium"
        $runResults += $r
        if ($r.exit_code -eq 0) {
            $state['last_layer3_at'] = $r.ended_at
        }
    }
    else {
        Write-Host "[SKIP] layer3 距上次 < 24h, 跳過" -ForegroundColor DarkGray
    }

    # L4 7d gate
    if (Should-Run-Layer $state['last_layer4_at'] 168) {
        $r = Invoke-Layer "layer4_7d_deep" "run_layer4_7d_deep"
        $runResults += $r
        if ($r.exit_code -eq 0) {
            $state['last_layer4_at'] = $r.ended_at
        }
    }
    else {
        Write-Host "[SKIP] layer4 距上次 < 7d, 跳過" -ForegroundColor DarkGray
    }

    # Persist state
    Write-State $state

    # Append daemon_runs log
    if ($runResults.Count -gt 0) {
        $logEntry = @{
            timestamp = $nowIso
            vault = $resolvedVault
            runs = $runResults
        } | ConvertTo-Json -Compress -Depth 5
        Add-Content -LiteralPath $logFile -Value $logEntry -Encoding UTF8
    }

    Write-Host ""
    Write-Host "[DONE] ran $($runResults.Count) layer(s)" -ForegroundColor Green
    Write-Host ""
}

# === Main entrypoint ===
if ($Loop) {
    Write-Host "[INFO] Loop 模式 (sleep 300s 後重跑)" -ForegroundColor Yellow
    while ($true) {
        try {
            Run-Daemon-Once
        }
        catch {
            Write-Host "[ERR] daemon iteration error: $_" -ForegroundColor Red
        }
        Start-Sleep -Seconds 300
    }
}
else {
    Run-Daemon-Once
}
