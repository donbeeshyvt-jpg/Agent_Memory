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
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it"
        )
        notes = "OpenAI-compatible 端點。沒設過 GOOGLE_API_KEY 會 SecureString prompt 你貼。"
    }
)

function Show-CurrentDefault {
    Write-Host "[INFO] 目前預設：" -ForegroundColor Cyan
    $cliArgs = @("-X", "utf8", "-m", "agent_memory.cli")
    if ($VaultRoot) { $cliArgs += @("--vault-root", $VaultRoot) }
    $cliArgs += "llm-show"
    & $PythonExe @cliArgs 2>&1 | Select-Object -First 6 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    Write-Host ""
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

# Smoke 驗證
Write-Host "[VERIFY] 跑一次管家對話 smoke..." -ForegroundColor Cyan
$smokeArgs = @("-X", "utf8", "-m", "agent_memory.cli")
if ($VaultRoot) { $smokeArgs += @("--vault-root", $VaultRoot) }
$smokeArgs += @("chat", "請只回 OK 兩個字，不要其他內容。", "--persona", "steward", "--context", "switch-llm", "--session", "switch-llm-smoke", "--allow-llm-degraded", "--json")
# PS5.1 的 2>&1 對 native command 會把 stderr 當 error 處理（NativeCommandError），
# 需要先把 EAP 設成 Continue 才能順利合併輸出。
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
if ($jsonStart -ge 0 -and $jsonEnd -gt $jsonStart) {
    try {
        $j = $smokeRaw.Substring($jsonStart, $jsonEnd - $jsonStart + 1) | ConvertFrom-Json
        $isDegraded = [bool]$j.degraded
        $resp = [string]$j.response
        if ($resp.Length -gt 80) { $resp = $resp.Substring(0, 80) + "..." }
        if ($isDegraded) {
            Write-Host "  [WARN] degraded — LLM 沒有實際回應（可能 key 無效或網路）" -ForegroundColor Yellow
            Write-Host "         response 預覽：$resp" -ForegroundColor DarkGray
        }
        else {
            Write-Host "  [OK] 切換成功，模型可正常對話" -ForegroundColor Green
            Write-Host "       response：$resp" -ForegroundColor DarkGray
        }
    }
    catch {
        Write-Host "  [WARN] smoke 回應 JSON parse 失敗" -ForegroundColor Yellow
    }
}
else {
    Write-Host "  [WARN] smoke 沒拿到 JSON" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host " 完成。下次 chat 會用：$($chosen.id) / $model" -ForegroundColor Green
Write-Host "===============================================================" -ForegroundColor Cyan
