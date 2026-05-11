<#
.SYNOPSIS
  互動式切換 agent-memory-core 的預設 LLM provider（本機 GGUF / Google Gemini /
  OpenAI / OpenRouter / Anthropic ...），並可選擇是否將 API key 寫入 Windows
  使用者環境變數，下次自動載入。

.DESCRIPTION
  這支腳本的目的：讓使用者在「本地推理」與「線上 API」間一條命令切換。

  做的事：
    1. 顯示目前預設 LLM 設定
    2. 列出可用 provider（從 llm_router.yaml 讀）
    3. 讓使用者選號碼
    4. 若該 provider 需要 API key 且環境變數沒設，安全 prompt 你貼（不顯示）
    5. 提供常見 model id 預設值，可選擇用預設或輸入自訂
    6. 寫入 user config（透過 memory-cli llm-set-default）
    7. 跑一次 chat smoke 驗證

  Token / API key 安全性：
    - 永不寫入任何檔案
    - 預設只設給目前 PowerShell process
    - 加 -PersistKey 旗標才會寫入 Windows 使用者環境變數（registry，非檔案、不推 git）
    - 寫入是 setx 行為，下次新開 PowerShell 自動載入

.PARAMETER VaultRoot
  指定 vault 路徑。預設讀 user config。

.PARAMETER PersistKey
  輸入新 API key 時自動 setx 寫入使用者環境變數，下次自動載入。

.PARAMETER PythonExe
  指定 Python 執行檔。預設 "python"。

.EXAMPLE
  .\scripts\switch-llm.ps1
  # 互動選 provider，第一次設 API key 會 prompt

.EXAMPLE
  .\scripts\switch-llm.ps1 -PersistKey
  # 同上，且 API key 寫入使用者環境變數，下次自動載入
#>
param(
    [string]$VaultRoot = "",
    [switch]$PersistKey,
    [switch]$NonInteractive,
    [switch]$RemoveKey,
    [int]$Provider = 0,
    [string]$Model = "",
    [string]$PythonExe = "python"
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

# ===== Provider 選單定義 =====
# recommended_models 第一個是預設；其餘列出讓使用者選或輸入自訂。
$providers = @(
    [ordered]@{
        id = "llama_cpp_local"
        display = "本機 llama-cpp-python (GGUF)"
        api_key_env = ""
        recommended_models = @(
            "../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf",
            "../../0_Models/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q8_0.gguf",
            "../../0_Models/Qwen3-30B-A3B-GGUF/Qwen3-30B-A3B-UD-Q4_K_XL.gguf"
        )
        notes = "需要 GGUF 已下載 + llama-cpp-python 已裝。沒有的可用 download-model.ps1 抓。"
    },
    [ordered]@{
        id = "gemini"
        display = "Google Gemini / Gemma API"
        api_key_env = "GOOGLE_API_KEY"
        recommended_models = @(
            "gemma-4-31b-it",
            "gemini-2.5-pro",
            "gemma-4-26b-a4b-it",
            "gemini-2.5-flash"
        )
        notes = "OpenAI-compatible 端點。沒設過 GOOGLE_API_KEY 會 SecureString prompt 你貼。"
    }
)

function Show-CurrentDefault {
    Write-Host "[INFO] 目前預設：" -ForegroundColor Cyan
    $cliArgs = @("-X", "utf8", "-m", "agent_memory.cli")
    if ($VaultRoot) { $cliArgs += @("--vault-root", $VaultRoot) }
    $cliArgs += "llm-show"
    # 只顯示前 2 行（selected 那條）,不秀完整 fallback chain
    & $PythonExe @cliArgs 2>&1 | Select-Object -First 2 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    Write-Host ""
}

function Remove-StoredApiKey {
    param([string]$EnvVarName)
    if (-not $EnvVarName) { return }
    [Environment]::SetEnvironmentVariable($EnvVarName, $null, "User")
    Remove-Item -LiteralPath "Env:$EnvVarName" -ErrorAction SilentlyContinue
    Write-Host "  [OK] 已移除 $EnvVarName (User 環境變數 + 此 process)" -ForegroundColor Yellow
}

function Read-Choice {
    param([int]$Max)
    while ($true) {
        $raw = (Read-Host "  請輸入號碼 [1-$Max]").Trim()
        if ($raw -match '^\d+$') {
            $n = [int]$raw
            if ($n -ge 1 -and $n -le $Max) { return $n }
        }
        Write-Host "  輸入無效，請輸入 1~$Max 之間的整數" -ForegroundColor Yellow
    }
}

function Get-OrPromptApiKey {
    param([string]$EnvVarName, [string]$ProviderDisplay)
    $key = [Environment]::GetEnvironmentVariable($EnvVarName, "Process")
    if (-not $key) {
        $key = [Environment]::GetEnvironmentVariable($EnvVarName, "User")
        if ($key) {
            Set-Item -LiteralPath "Env:$EnvVarName" -Value $key
            Write-Host "  [OK] 從使用者環境變數載入 $EnvVarName" -ForegroundColor Green
            return $key
        }
    }
    if ($key) {
        Write-Host "  [OK] $EnvVarName 已就緒" -ForegroundColor Green
        return $key
    }

    if ($NonInteractive) {
        Write-Host "  [WARN] $EnvVarName 未設且 -NonInteractive 模式不 prompt；設預設後 LLM 會 degraded 直到你手動 setx $EnvVarName" -ForegroundColor Yellow
        return ""
    }

    Write-Host "  [INFO] $EnvVarName 未設，需要你貼 $ProviderDisplay 的 API key (輸入時不顯示)" -ForegroundColor Yellow
    $sec = Read-Host -Prompt "  $ProviderDisplay API key" -AsSecureString
    if (-not $sec -or $sec.Length -eq 0) {
        Write-Host "  [WARN] 沒貼 key，繼續但 LLM 呼叫會失敗" -ForegroundColor Yellow
        return ""
    }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Set-Item -LiteralPath "Env:$EnvVarName" -Value $key

    if ($PersistKey) {
        [Environment]::SetEnvironmentVariable($EnvVarName, $key, "User")
        Write-Host "  [OK] $EnvVarName 已寫入 Windows 使用者環境變數（registry，下次新開 PowerShell 自動載入）" -ForegroundColor Green
    }
    else {
        Write-Host "  [INFO] $EnvVarName 只在此視窗有效。要持久化：加 -PersistKey 重跑。" -ForegroundColor DarkGray
    }
    return $key
}

# ===== 主流程 =====
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host " LLM Provider Switcher" -ForegroundColor Cyan
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host ""

Show-CurrentDefault

# 早期分支：-RemoveKey 跳出互動,先讓使用者選要清哪個 provider 的 key
if ($RemoveKey) {
    Write-Host "[移除 API key 模式]" -ForegroundColor Yellow
    Write-Host ""
    $apiProviders = New-Object System.Collections.ArrayList
    foreach ($p in $providers) {
        $envName = [string]$p["api_key_env"]
        if (-not [string]::IsNullOrWhiteSpace($envName)) {
            [void]$apiProviders.Add($p)
        }
    }
    for ($i = 0; $i -lt $apiProviders.Count; $i++) {
        $p = $apiProviders[$i]
        $envName = [string]$p["api_key_env"]
        $hasKey = (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($envName, "Process"))) `
              -or (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($envName, "User")))
        $state = if ($hasKey) { "[key:ok]" } else { "[key:missing]" }
        Write-Host ("  [{0}] 移除 {1} ({2}) {3}" -f ($i + 1), $p["display"], $envName, $state) -ForegroundColor Yellow
    }
    Write-Host "  [Q] 取消"
    Write-Host ""
    $rc = (Read-Host "  選號碼").Trim()
    if ($rc -in @("Q", "q") -or -not ($rc -match '^\d+$')) {
        Write-Host "[INFO] 已取消。" -ForegroundColor DarkGray
        exit 0
    }
    $rci = [int]$rc - 1
    if ($rci -lt 0 -or $rci -ge $apiProviders.Count) {
        Write-Host "[ERR] 號碼無效" -ForegroundColor Red
        exit 1
    }
    Remove-StoredApiKey -EnvVarName $apiProviders[$rci]["api_key_env"]
    Write-Host "[OK] 完成。" -ForegroundColor Green
    exit 0
}

Write-Host "[INFO] 可選 provider：" -ForegroundColor Cyan
for ($i = 0; $i -lt $providers.Count; $i++) {
    $p = $providers[$i]
    $keyState = ""
    if ($p.api_key_env) {
        $hasKey = (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($p.api_key_env, "Process"))) `
              -or (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($p.api_key_env, "User")))
        $keyState = if ($hasKey) { " [key:ok]" } else { " [key:missing]" }
    }
    Write-Host ("  [{0}] {1}{2}" -f ($i + 1), $p.display, $keyState) -ForegroundColor Yellow
    Write-Host ("      {0}" -f $p.notes) -ForegroundColor DarkGray
}
Write-Host ""

if ($Provider -ge 1 -and $Provider -le $providers.Count) {
    $choiceIdx = $Provider - 1
}
elseif ($NonInteractive) {
    Write-Host "[ERR] -NonInteractive 需要顯式 -Provider <1-$($providers.Count)>" -ForegroundColor Red
    exit 1
}
else {
    $choiceIdx = (Read-Choice -Max $providers.Count) - 1
}
$chosen = $providers[$choiceIdx]
Write-Host ""
Write-Host "[INFO] 選擇：$($chosen.display)" -ForegroundColor Cyan

# API key
if ($chosen.api_key_env) {
    Get-OrPromptApiKey -EnvVarName $chosen.api_key_env -ProviderDisplay $chosen.display | Out-Null
}

# Model 選擇
Write-Host ""
Write-Host "[INFO] 推薦 model：" -ForegroundColor Cyan
for ($i = 0; $i -lt $chosen.recommended_models.Count; $i++) {
    $marker = if ($i -eq 0) { " (預設)" } else { "" }
    Write-Host ("  [{0}] {1}{2}" -f ($i + 1), $chosen.recommended_models[$i], $marker) -ForegroundColor Yellow
}
Write-Host ("  [{0}] 自訂（手動輸入 model id）" -f ($chosen.recommended_models.Count + 1)) -ForegroundColor Yellow

if ($Model) {
    $model = $Model
    Write-Host "  [INFO] 使用 -Model 參數指定的：$model" -ForegroundColor Cyan
}
elseif ($NonInteractive) {
    $model = $chosen.recommended_models[0]
    Write-Host "  [INFO] -NonInteractive 用該 provider 的預設 model：$model" -ForegroundColor Cyan
}
else {
    $modelChoice = Read-Choice -Max ($chosen.recommended_models.Count + 1)
    if ($modelChoice -le $chosen.recommended_models.Count) {
        $model = $chosen.recommended_models[$modelChoice - 1]
    }
    else {
        $model = (Read-Host "  輸入 model id").Trim()
        if (-not $model) {
            Write-Host "[ERR] model id 不能空" -ForegroundColor Red
            exit 1
        }
    }
}
Write-Host "[INFO] 將設為預設：$($chosen.id) / $model" -ForegroundColor Cyan
Write-Host ""

# 套用
$setArgs = @("-X", "utf8", "-m", "agent_memory.cli")
if ($VaultRoot) { $setArgs += @("--vault-root", $VaultRoot) }
$setArgs += @("llm-set-default", "--profile", $chosen.id, "--model", $model, "--json")
Write-Host "[INFO] 執行 llm-set-default..." -ForegroundColor Cyan
$setRun = & $PythonExe @setArgs 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERR] llm-set-default 失敗:" -ForegroundColor Red
    Write-Host $setRun -ForegroundColor Red
    exit 1
}
Write-Host "[OK] 預設已更新" -ForegroundColor Green
Write-Host ""

# 真實 chat 驗證
Write-Host "[檢查] 跑一次小對話驗證 ($($chosen.id))..." -ForegroundColor Cyan
$smokeArgs = @("-X", "utf8", "-m", "agent_memory.cli")
if ($VaultRoot) { $smokeArgs += @("--vault-root", $VaultRoot) }
$smokeArgs += @("chat", "請只回 OK", "--persona", "steward", "--context", "switch-llm", "--session", "switch-llm-smoke", "--allow-llm-degraded", "--json")
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $smokeRaw = (& $PythonExe @smokeArgs 2>&1 | Out-String)
}
finally {
    $ErrorActionPreference = $prevEap
}
$jsonStart = $smokeRaw.IndexOf('{')
$jsonEnd = $smokeRaw.LastIndexOf('}')
$isDegraded = $true
$resp = ""
if ($jsonStart -ge 0 -and $jsonEnd -gt $jsonStart) {
    try {
        $j = $smokeRaw.Substring($jsonStart, $jsonEnd - $jsonStart + 1) | ConvertFrom-Json
        $isDegraded = [bool]$j.degraded
        $resp = [string]$j.response
        if ($resp.Length -gt 80) { $resp = $resp.Substring(0, 80) + "..." }
    }
    catch { }
}

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Cyan
if (-not $isDegraded) {
    Write-Host "  ✓ 切換 + 驗證成功" -ForegroundColor Green
    Write-Host "    provider: $($chosen.id)" -ForegroundColor DarkGray
    Write-Host "    model:    $model" -ForegroundColor DarkGray
    Write-Host "    回應:     $resp" -ForegroundColor DarkGray
}
else {
    Write-Host "  ⚠ 設定已存,但實際呼叫沒回應" -ForegroundColor Yellow
    if ($chosen.api_key_env) {
        Write-Host ""
        Write-Host "  可能原因:" -ForegroundColor Yellow
        Write-Host "    1. $($chosen.api_key_env) 無效或過期" -ForegroundColor DarkGray
        Write-Host "    2. model id `"$model`" 帳號不支援" -ForegroundColor DarkGray
        Write-Host "    3. 網路 / 配額" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  下一步建議:" -ForegroundColor Yellow
        Write-Host "    .\scripts\switch-llm.ps1                重選 model 或重貼 key" -ForegroundColor DarkGray
        Write-Host "    .\scripts\switch-llm.ps1 -RemoveKey      移除 $($chosen.api_key_env)" -ForegroundColor DarkGray
        Write-Host "    或直接切回本機: /llm gemma4 (在對話中)" -ForegroundColor DarkGray
    }
    else {
        Write-Host "  可能是本機模型載入失敗 (檔案路徑 / llama-cpp-python / CUDA)。" -ForegroundColor Yellow
        Write-Host "  下一步: 檢查 ../0_Models/ 路徑,或 .\scripts\switch-llm.ps1 -Provider 2 切到 Google API。" -ForegroundColor DarkGray
    }
}
Write-Host "===============================================================" -ForegroundColor Cyan
