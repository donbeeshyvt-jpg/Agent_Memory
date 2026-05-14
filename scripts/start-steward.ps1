<#
.SYNOPSIS
  一鍵啟動 steward (管家) 上線。讀取既有 .local.json 設定 + 安全 prompt token +
  在背景啟 bridge + 在前景啟 relay + Ctrl+C 自動清理。

.DESCRIPTION
  fresh user 跑完 first-run-wizard.ps1 之後，把所有設定（channel id / 角色 / 模型）
  存在 scripts/discord-relay-stack.local.json 與 vault 內的 channel_bindings.yaml。
  本腳本只負責「讀設定 + 拿 token + 啟動 process + 清理殘留」，使用者不用再重複輸入。

  Token 安全：
  - 永不寫入任何檔案
  - 預設讀 process / user 環境變數
  - 缺則用 SecureString prompt（輸入時不顯示）
  - 可選 -PersistToken 寫入 Windows 使用者環境變數（registry，不是檔案）

.PARAMETER ConfigFile
  Relay 設定檔路徑。預設 scripts/discord-relay-stack.local.json。

.PARAMETER BridgePort
  Bridge 端口，預設 16000。

.PARAMETER PersistToken
  將輸入的 token 寫入 Windows 使用者環境變數，下次開機自動載入（不寫進任何檔案）。

.PARAMETER VaultRoot
  指定 vault root（測試用）。預設由 user config 自動解析。

.EXAMPLE
  .\scripts\start-steward.ps1
  # 第一次跑會 prompt 你貼 token，然後啟動 bridge + relay。Ctrl+C 結束。

.EXAMPLE
  .\scripts\start-steward.ps1 -PersistToken
  # 同上，但把 token 記住到使用者環境變數，下次自動載入不再 prompt。
#>
param(
    [string]$ConfigFile = "",
    [int]$BridgePort = 16000,
    [switch]$PersistToken,
    [string]$VaultRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
}
catch { }

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

# 自動載入 .env (<vault>/.env, DISCORD_BOT_TOKEN_* 等)
. (Join-Path $PSScriptRoot "_dotenv-helper.ps1")
Import-DotEnvIntoProcess -VaultRoot $VaultRoot | Out-Null

if (-not $ConfigFile) {
    $ConfigFile = Join-Path $projectRoot "scripts/discord-relay-stack.local.json"
}

# ===== 1. 讀 relay 設定 =====
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    Write-Host "[ERR] Relay 設定不存在：$ConfigFile" -ForegroundColor Red
    Write-Host "      請先跑：.\START_SETUP.bat -SetupDiscord 或 wizard 結尾的 Discord 設定" -ForegroundColor Yellow
    exit 1
}

$cfg = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $cfg.relays -or $cfg.relays.Count -eq 0) {
    Write-Host "[ERR] $ConfigFile 沒有 relays 區塊" -ForegroundColor Red
    exit 1
}
$primary = $cfg.relays[0]
$tokenEnv = [string]$primary.token_env
$persona = [string]$primary.persona
$channelIds = @($primary.channel_ids)

Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host " Steward Launcher" -ForegroundColor Cyan
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host " 設定檔：$ConfigFile"
Write-Host " 角色：$persona"
Write-Host " 頻道：$($channelIds -join ', ')"
Write-Host " Token 環境變數名：$tokenEnv"
Write-Host " Bridge 端口：$BridgePort"
Write-Host ""

# ===== 2. Token：先讀環境變數，缺則 SecureString prompt =====
$token = [Environment]::GetEnvironmentVariable($tokenEnv, "Process")
if (-not $token) {
    $token = [Environment]::GetEnvironmentVariable($tokenEnv, "User")
    if ($token) {
        Write-Host "[INFO] 從使用者環境變數載入 token (registry)" -ForegroundColor DarkGray
        Set-Item -LiteralPath "Env:$tokenEnv" -Value $token
    }
}
if (-not $token) {
    Write-Host "[INFO] 環境變數 $tokenEnv 未設，需要你貼 token（輸入時不顯示）" -ForegroundColor Yellow
    $sec = Read-Host -Prompt "  Discord Bot Token for $persona" -AsSecureString
    if (-not $sec -or $sec.Length -eq 0) {
        Write-Host "[ERR] 沒有 token，無法啟動 relay" -ForegroundColor Red
        exit 1
    }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Set-Item -LiteralPath "Env:$tokenEnv" -Value $token

    if ($PersistToken) {
        [Environment]::SetEnvironmentVariable($tokenEnv, $token, "User")
        Write-Host "[OK] token 已寫入 Windows 使用者環境變數（registry，下次開機自動載入）" -ForegroundColor Green
        Write-Host "     此操作不寫進任何檔案、不會推 git。要清除：[Environment]::SetEnvironmentVariable('$tokenEnv', `$null, 'User')" -ForegroundColor DarkGray
    }
    else {
        Write-Host "[INFO] token 只在這個視窗有效。下次跑想自動載入：start-steward.ps1 -PersistToken" -ForegroundColor DarkGray
    }
}
else {
    Write-Host "[OK] token 已就緒 (從環境變數載入)" -ForegroundColor Green
}

# ===== 3. 啟動 bridge 在背景 =====
Write-Host ""
Write-Host "[INFO] 啟動 bridge :$BridgePort 在背景..." -ForegroundColor Cyan
$bridgeArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $projectRoot "scripts/run-bridge.ps1"), "-Port", $BridgePort)
if ($VaultRoot) {
    $bridgeArgs += @("-VaultRoot", $VaultRoot)
}
$bridgeProc = Start-Process -PassThru -WindowStyle Hidden -FilePath "powershell" -ArgumentList $bridgeArgs

Start-Sleep -Seconds 4
$bridgeReady = $false
for ($i = 0; $i -lt 5; $i++) {
    try {
        $h = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:$BridgePort/health" -TimeoutSec 5 -ErrorAction Stop
        if ($h.ok) {
            $bridgeReady = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $bridgeReady) {
    Write-Host "[ERR] bridge 啟動失敗。停止 process。" -ForegroundColor Red
    if ($bridgeProc -and -not $bridgeProc.HasExited) {
        Stop-Process -Id $bridgeProc.Id -Force -ErrorAction SilentlyContinue
    }
    exit 1
}
Write-Host "[OK] bridge ready (pid=$($bridgeProc.Id), vault=$($h.vault_root))" -ForegroundColor Green

# ===== 4. 啟動 relay =====
Write-Host ""
Write-Host "[INFO] 啟動 Discord relay..." -ForegroundColor Cyan
$relayManager = Join-Path $projectRoot "scripts/manage-discord-relay-stack.ps1"

# 包 try/finally 處理 Ctrl+C 與意外結束
$cleanupDone = $false
$cleanup = {
    if ($script:cleanupDone) { return }
    $script:cleanupDone = $true
    Write-Host ""
    Write-Host "[INFO] 收尾中..." -ForegroundColor Yellow
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $relayManager -Action stop -ConfigFile $ConfigFile 2>&1 | Out-Null
    }
    catch { }
    if ($bridgeProc -and -not $bridgeProc.HasExited) {
        Stop-Process -Id $bridgeProc.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[OK] 已停止 bridge + relay" -ForegroundColor Green
}

try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $relayManager -Action start -ConfigFile $ConfigFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERR] relay 啟動失敗 (exit=$LASTEXITCODE)" -ForegroundColor Red
        & $cleanup
        exit 1
    }

    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host " Steward 已上線！到 Discord 對應頻道 @管家 開始對話。" -ForegroundColor Green
    Write-Host " 按 Ctrl+C 關閉所有 process。" -ForegroundColor Green
    Write-Host "===============================================================" -ForegroundColor Green

    # 一直等到使用者 Ctrl+C 或 bridge process 死掉
    while ($true) {
        Start-Sleep -Seconds 30
        if ($bridgeProc.HasExited) {
            Write-Host "[WARN] bridge process 已退出，整體收尾" -ForegroundColor Yellow
            break
        }
    }
}
finally {
    & $cleanup
}
