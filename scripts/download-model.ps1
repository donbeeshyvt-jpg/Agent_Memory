<#
.SYNOPSIS
  互動下載本地 GGUF 模型到 ../0_Models/。

.DESCRIPTION
  - 自動偵測 hf (新版) vs huggingface-cli (舊版 deprecated)
  - 串流進度條（不 buffer 所有輸出）
  - 預設 7 種 GGUF 模型，可選號碼或自訂

.PARAMETER ModelKey
  指定模型 key 跳過互動：gemma4 / qwen35-9b / qwen30

.PARAMETER LocalDirRoot
  下載目標的母目錄。預設 <project_root>\..\0_Models

.PARAMETER NonInteractive
  搭配 -ModelKey 使用，不問互動。

.EXAMPLE
  .\scripts\download-model.ps1
  # 互動選號碼

.EXAMPLE
  .\scripts\download-model.ps1 -ModelKey qwen3-8b
  # 直接下 Qwen3-8B Instruct Q4_K_M
#>
param(
    [string]$ModelKey = "",
    [string]$LocalDirRoot = "",
    [switch]$NonInteractive
)
$Repo = ""
$File = ""

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

if (-not $LocalDirRoot) {
    $LocalDirRoot = Join-Path $projectRoot "..\0_Models"
}
if (-not (Test-Path -LiteralPath $LocalDirRoot)) {
    New-Item -ItemType Directory -Path $LocalDirRoot -Force | Out-Null
}
$LocalDirRoot = (Resolve-Path -LiteralPath $LocalDirRoot).Path

# 模型目錄（每個 model 在自己的子資料夾）
$models = @(
    [ordered]@{
        key = "gemma4"
        display = "gemma-4 E4B Instruct Q8_0"
        size = "~4 GB"
        repo = "ggml-org/gemma-4-E4B-it-GGUF"
        file = "gemma-4-E4B-it-Q8_0.gguf"
        subdir = "gemma-4-E4B-it-GGUF"
        notes = "輕量,啟動快,推薦給 fresh user / 4-6GB RAM"
    },
    [ordered]@{
        key = "qwen35-9b"
        display = "Qwen3.5-9B Q8_0"
        size = "~10 GB"
        repo = "seerware/Qwen3.5-9B-GGUF"
        file = "Qwen3.5-9B-Q8_0.gguf"
        subdir = "Qwen3.5-9B-GGUF"
        notes = "中文流暢,適合主要對話角色"
    },
    [ordered]@{
        key = "qwen30"
        display = "Qwen3-30B-A3B UD-Q4_K_XL"
        size = "~17 GB"
        repo = "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF"
        file = "Qwen3-30B-A3B-UD-Q4_K_XL.gguf"
        subdir = ""
        notes = "Sparse MoE 大模型,推理 / 工程角色 (24GB+ VRAM 較順)"
    }
)

# 偵測 hf vs huggingface-cli
function Find-HfCli {
    $hf = Get-Command hf -ErrorAction SilentlyContinue
    if ($hf) {
        return @{ exe = "hf"; deprecated = $false }
    }
    $hfCli = Get-Command huggingface-cli -ErrorAction SilentlyContinue
    if ($hfCli) {
        return @{ exe = "huggingface-cli"; deprecated = $true }
    }
    return $null
}

function Install-HfHub {
    Write-Host "[INFO] 安裝 huggingface_hub[cli]..." -ForegroundColor Cyan
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -m pip install -q -U "huggingface_hub[cli]"
    }
    finally {
        $ErrorActionPreference = $prevEap
    }
    return ($LASTEXITCODE -eq 0)
}

# ===== 主流程 =====
Write-Host ""
Write-Host "  本地模型下載到: $LocalDirRoot" -ForegroundColor Cyan
Write-Host ""

# 互動選 model
if (-not $ModelKey) {
    if ($NonInteractive) {
        Write-Host "[ERR] -NonInteractive 必須指定 -ModelKey" -ForegroundColor Red
        exit 1
    }
    Write-Host "  請選擇要下載的模型：" -ForegroundColor Cyan
    Write-Host ""
    for ($i = 0; $i -lt $models.Count; $i++) {
        $m = $models[$i]
        $existsTag = ""
        $expected = Join-Path (Join-Path $LocalDirRoot $m.subdir) $m.file
        if (Test-Path -LiteralPath $expected) {
            $existsTag = "  [已下載]"
        }
        Write-Host -NoNewline ("    [{0}] " -f ($i + 1)) -ForegroundColor Yellow
        Write-Host -NoNewline ($m.display.PadRight(40)) -ForegroundColor White
        Write-Host -NoNewline (" $($m.size)".PadRight(12)) -ForegroundColor DarkGray
        Write-Host $existsTag -ForegroundColor Green
        Write-Host ("        " + $m.notes) -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "    [Q] 取消"
    Write-Host ""

    while ($true) {
        $raw = (Read-Host "  輸入 [1-$($models.Count)/Q]").Trim()
        if ($raw -in @("Q", "q")) {
            Write-Host "  [INFO] 已取消。" -ForegroundColor DarkGray
            exit 0
        }
        if ($raw -match '^\d+$') {
            $n = [int]$raw
            if ($n -ge 1 -and $n -le $models.Count) {
                $chosen = $models[$n - 1]
                $Repo = $chosen.repo
                $File = $chosen.file
                $subdir = $chosen.subdir
                $display = $chosen.display
                break
            }
        }
        Write-Host "  輸入無效。" -ForegroundColor Red
    }
}
else {
    $matched = $models | Where-Object { $_.key -eq $ModelKey }
    if ($matched) {
        $Repo = $matched.repo
        $File = $matched.file
        $subdir = $matched.subdir
        $display = $matched.display
    }
    else {
        Write-Host "[ERR] 未知的 ModelKey: $ModelKey" -ForegroundColor Red
        Write-Host "      可用: $((@($models | ForEach-Object { $_.key })) -join ', ')" -ForegroundColor Yellow
        exit 1
    }
}

$targetDir = Join-Path $LocalDirRoot $subdir
$targetFile = Join-Path $targetDir $File

# 檢查是否已存在
if (Test-Path -LiteralPath $targetFile) {
    Write-Host "  [OK] 檔案已存在,跳過下載: $targetFile" -ForegroundColor Green
    exit 0
}

# 找/裝 hf cli
$cli = Find-HfCli
if (-not $cli) {
    Write-Host "[INFO] huggingface CLI 未安裝,先 pip install..." -ForegroundColor Cyan
    if (-not (Install-HfHub)) {
        Write-Host "[ERR] huggingface_hub 安裝失敗" -ForegroundColor Red
        exit 1
    }
    $cli = Find-HfCli
    if (-not $cli) {
        Write-Host "[ERR] 裝完還是找不到 hf 指令,可能是 PATH 問題,請重開 PowerShell" -ForegroundColor Red
        exit 1
    }
}

if ($cli.deprecated) {
    Write-Host "[INFO] 使用舊版 huggingface-cli (建議升級: pip install -U huggingface_hub[cli])" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "  下載: $display" -ForegroundColor Cyan
Write-Host "  到:   $targetFile" -ForegroundColor DarkGray
Write-Host "  CLI:  $($cli.exe)" -ForegroundColor DarkGray
Write-Host ""

# 串流下載 — HF cli 自帶進度條,直接讓它印到 host
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $cli.exe download $Repo $File --local-dir $targetDir
    $exitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $prevEap
}

if ($exitCode -ne 0) {
    Write-Host ""
    Write-Host "[ERR] 下載失敗 (exit=$exitCode)" -ForegroundColor Red
    exit $exitCode
}

if (-not (Test-Path -LiteralPath $targetFile)) {
    Write-Host ""
    Write-Host "[ERR] CLI 報告成功但檔案不存在: $targetFile" -ForegroundColor Red
    exit 2
}

$fileSize = (Get-Item -LiteralPath $targetFile).Length
$fileSizeMb = [math]::Round($fileSize / 1MB, 1)
Write-Host ""
Write-Host "  [OK] 下載完成: $targetFile" -ForegroundColor Green
Write-Host "       大小: ${fileSizeMb} MB" -ForegroundColor DarkGray
exit 0
