<#
.SYNOPSIS
  Agent Memory Core 主選單 (Quick Start Menu).

.DESCRIPTION
  美化的入口選單。整合：
    - 環境健康檢查（Python / 模型 / vault / Discord 綁定 / token）
    - 快速設定 / 自訂設定（dispatch 到 first-run-wizard.ps1）
    - 上線管家 (start-steward.ps1)
    - 切換 LLM (switch-llm.ps1)
    - CLI 試聊
    - 工具能力 smoke

  所有功能仍是現有 sub-script，本檔只是統一的選擇入口。

.PARAMETER VaultRoot
  指定 vault 路徑（測試用）。預設讀 user config。

.PARAMETER PythonExe
  指定 Python 執行檔。預設 "python"。
#>
param(
    [string]$VaultRoot = "",
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

# ============== 視覺常數 ==============
$BorderColor = [ConsoleColor]::Cyan
$TitleColor = [ConsoleColor]::Green
$AccentColor = [ConsoleColor]::Yellow
$MutedColor = [ConsoleColor]::DarkGray
$OkColor = [ConsoleColor]::Green
$WarnColor = [ConsoleColor]::DarkYellow
$ErrColor = [ConsoleColor]::Red
$Width = 66

# ============== Helpers ==============
function Write-Border {
    param([string]$Char = "═")
    Write-Host ("  " + ($Char * $Width)) -ForegroundColor $BorderColor
}

function Write-BoxTop {
    Write-Host ("  ╔" + ("═" * ($Width - 2)) + "╗") -ForegroundColor $BorderColor
}

function Write-BoxBottom {
    Write-Host ("  ╚" + ("═" * ($Width - 2)) + "╝") -ForegroundColor $BorderColor
}

function Write-BoxBlank {
    Write-Host ("  ║" + (" " * ($Width - 2)) + "║") -ForegroundColor $BorderColor
}

function Write-BoxLine {
    param([string]$Left, [string]$Right = "", [ConsoleColor]$LeftColor = $TitleColor, [ConsoleColor]$RightColor = $MutedColor)
    $leftLen = $Left.Length
    $rightLen = $Right.Length
    # box drawing 左右各一格
    $padding = $Width - 2 - 2 - $leftLen - $rightLen - 2
    if ($padding -lt 0) { $padding = 0 }
    Write-Host -NoNewline "  ║ " -ForegroundColor $BorderColor
    Write-Host -NoNewline $Left -ForegroundColor $LeftColor
    Write-Host -NoNewline (" " * $padding) -ForegroundColor $BorderColor
    if ($Right) {
        Write-Host -NoNewline $Right -ForegroundColor $RightColor
    }
    Write-Host " ║" -ForegroundColor $BorderColor
}

function Show-Banner {
    Clear-Host
    Write-Host ""
    Write-BoxTop
    Write-BoxBlank
    Write-BoxLine -Left "AGENT MEMORY CORE" -Right "v0.1.0" -LeftColor $TitleColor -RightColor $MutedColor
    Write-BoxLine -Left "本機 LLM × 多角色記憶 × Discord 串接" -LeftColor $MutedColor
    Write-BoxBlank
    Write-BoxBottom
    Write-Host ""
}

# ============== 健康檢查 ==============
function Test-Status {
    $rows = New-Object System.Collections.ArrayList

    # Python
    $pyCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $verRaw = & $PythonExe --version 2>&1 | Out-String
        $ver = ($verRaw -replace 'Python\s*', '').Trim()
        [void]$rows.Add(@{ name = "Python"; ok = $true; detail = "v$ver" })
    }
    else {
        [void]$rows.Add(@{ name = "Python"; ok = $false; detail = "未安裝（會自動裝 via winget）" })
    }

    # agent_memory 是否可用（試跑 --help）
    $cliOk = $false
    if ($pyCmd) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $PythonExe -m agent_memory.cli --help 2>&1 | Out-Null
            $cliOk = ($LASTEXITCODE -eq 0)
        }
        finally {
            $ErrorActionPreference = $prevEap
        }
    }
    [void]$rows.Add(@{ name = "agent_memory CLI"; ok = $cliOk; detail = if ($cliOk) { "可用" } else { "未安裝（[1]/[2] 會自動裝）" } })

    # Vault
    $vaultRoot = $VaultRoot
    if (-not $vaultRoot -and $cliOk) {
        try {
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $vaultShowOut = (& $PythonExe -X utf8 -m agent_memory.cli vault-show 2>&1 | Out-String)
            $ErrorActionPreference = $prevEap
            $m = [regex]::Match($vaultShowOut, "vault_root=([^\r\n]+)")
            if ($m.Success) { $vaultRoot = $m.Groups[1].Value.Trim() }
        }
        catch { }
    }
    $hasVault = $false
    $vaultLabel = "尚未建立"
    if ($vaultRoot -and (Test-Path -LiteralPath (Join-Path $vaultRoot "00_System"))) {
        $hasVault = $true
        $name = Split-Path -Leaf $vaultRoot
        $vaultLabel = $name
    }
    [void]$rows.Add(@{ name = "第二大腦 vault"; ok = $hasVault; detail = $vaultLabel })

    # Default LLM
    $llmDetail = "尚未設定"
    $llmOk = $false
    $llmMode = ""
    if ($cliOk -and $hasVault) {
        try {
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $llmShowOut = (& $PythonExe -X utf8 -m agent_memory.cli llm-show 2>&1 | Out-String)
            $ErrorActionPreference = $prevEap
            $m = [regex]::Match($llmShowOut, "selected=([^/]+)/\s*([^\r\n]+)")
            if ($m.Success) {
                $profile = $m.Groups[1].Value.Trim()
                $modelRaw = $m.Groups[2].Value.Trim()
                # 縮短模型路徑顯示
                $modelShort = $modelRaw
                if ($modelShort.Length -gt 32) {
                    $modelShort = "..." + $modelShort.Substring($modelShort.Length - 32)
                }
                # 區分線上 API vs 本機推理
                $apiProfiles = @("gemini", "openai", "anthropic", "openrouter", "opencode_zen", "opencode_go")
                $localProfiles = @("llama_cpp_local", "ollama_local")
                $llmMode = if ($apiProfiles -contains $profile) { "[線上 API]" }
                           elseif ($localProfiles -contains $profile) { "[本機推理]" }
                           else { "[?]" }
                $llmDetail = "$llmMode $profile / $modelShort"
                $llmOk = $true

                # API 模式：檢查 key 是否設好
                if ($apiProfiles -contains $profile) {
                    $apiKeyEnvMap = @{
                        "gemini" = "GOOGLE_API_KEY"
                        "openai" = "OPENAI_API_KEY"
                        "anthropic" = "ANTHROPIC_API_KEY"
                        "openrouter" = "OPENROUTER_API_KEY"
                        "opencode_zen" = "OPENCODE_ZEN_API_KEY"
                        "opencode_go" = "OPENCODE_GO_API_KEY"
                    }
                    $envName = $apiKeyEnvMap[$profile]
                    if ($envName) {
                        $procKey = [Environment]::GetEnvironmentVariable($envName, "Process")
                        $userKey = [Environment]::GetEnvironmentVariable($envName, "User")
                        $hasKey = (-not [string]::IsNullOrWhiteSpace($procKey)) -or (-not [string]::IsNullOrWhiteSpace($userKey))
                        if (-not $hasKey) {
                            $llmDetail = "$llmMode $profile / $modelShort (⚠ $envName 未設)"
                            $llmOk = $false
                        }
                    }
                }
            }
        }
        catch { }
    }
    [void]$rows.Add(@{ name = "LLM 預設"; ok = $llmOk; detail = $llmDetail })

    # 本機 gemma-4 模型存在
    $gemmaPath = ""
    if ($vaultRoot) {
        $candidate = Join-Path $vaultRoot "..\..\0_Models\gemma-4-E4B-it-GGUF\gemma-4-E4B-it-Q8_0.gguf"
        if (Test-Path -LiteralPath $candidate) {
            $gemmaPath = $candidate
        }
    }
    if (-not $gemmaPath) {
        $candidate2 = Join-Path $projectRoot "..\0_Models\gemma-4-E4B-it-GGUF\gemma-4-E4B-it-Q8_0.gguf"
        if (Test-Path -LiteralPath $candidate2) {
            $gemmaPath = $candidate2
        }
    }
    [void]$rows.Add(@{ name = "本機模型 gemma-4 E4B"; ok = ([bool]$gemmaPath); detail = if ($gemmaPath) { "已下載" } else { "未下載（[2] 可下載）" } })

    # Discord 設定
    $relayCfg = Join-Path $projectRoot "scripts/discord-relay-stack.local.json"
    $hasDiscord = Test-Path -LiteralPath $relayCfg
    $discordDetail = "未配置"
    $tokenEnv = "DISCORD_BOT_TOKEN_STEWARD"
    if ($hasDiscord) {
        try {
            $cfg = Get-Content -LiteralPath $relayCfg -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.relays -and $cfg.relays.Count -gt 0) {
                $primary = $cfg.relays[0]
                $tokenEnv = [string]$primary.token_env
                $cidCount = ([array]$primary.channel_ids).Count
                $discordDetail = "$($primary.persona) → $cidCount 個 channel"
            }
        }
        catch { }
    }
    [void]$rows.Add(@{ name = "Discord 設定"; ok = $hasDiscord; detail = $discordDetail })

    # Token env var
    $tokenSet = $false
    if ($tokenEnv) {
        $proc = [Environment]::GetEnvironmentVariable($tokenEnv, "Process")
        $user = [Environment]::GetEnvironmentVariable($tokenEnv, "User")
        $tokenSet = (-not [string]::IsNullOrWhiteSpace($proc)) -or (-not [string]::IsNullOrWhiteSpace($user))
    }
    [void]$rows.Add(@{ name = "Discord token"; ok = $tokenSet; detail = if ($tokenSet) { "已設 ($tokenEnv)" } else { "未設（[3] 會 prompt）" } })

    return $rows
}

function Show-Status {
    $rows = Test-Status
    Write-Host "  目前環境狀態：" -ForegroundColor $BorderColor
    foreach ($r in $rows) {
        $marker = if ($r.ok) { "[✓]" } else { "[○]" }
        $markerColor = if ($r.ok) { $OkColor } else { $MutedColor }
        $nameColor = if ($r.ok) { [ConsoleColor]::White } else { $MutedColor }
        Write-Host "    " -NoNewline
        Write-Host $marker -NoNewline -ForegroundColor $markerColor
        $namePadded = $r.name.PadRight(28)
        Write-Host " $namePadded" -NoNewline -ForegroundColor $nameColor
        Write-Host ": $($r.detail)" -ForegroundColor $MutedColor
    }
    Write-Host ""
}

# ============== 選單 ==============
function Show-Menu {
    Write-Host "  請選擇：" -ForegroundColor $BorderColor
    Write-Host ""

    function Write-Option {
        param([string]$Key, [string]$Title, [string]$Desc)
        Write-Host -NoNewline "    "
        Write-Host -NoNewline "[$Key]" -ForegroundColor $AccentColor
        $titlePadded = (" " + $Title).PadRight(28)
        Write-Host -NoNewline $titlePadded -ForegroundColor White
        Write-Host $Desc -ForegroundColor $MutedColor
    }

    Write-Option "1" "快速設定" "自動建大腦 + 配本機模型 + 跑 chat 驗證"
    Write-Option "2" "自訂設定" "逐步互動：選 LLM、要不要 Discord、要不要下載"
    Write-Host ""
    Write-Option "3" "上線管家到 Discord" "啟 bridge + relay，貼 token 即上線"
    Write-Option "4" "切換 LLM 模型" "本機 (gemma-4/Qwen) ↔ Google API (Gemini/Gemma)"
    Write-Option "5" "下載本地模型" "gemma-4 / Qwen3.5-9B / Qwen3-30B 三選一"
    Write-Host ""
    Write-Option "6" "CLI 對話模式" "直接連續和管家對話 (不用 Discord,REPL)"
    Write-Option "7" "跑工具能力 smoke" "驗證 /tool 寫檔 + 角色權限治理"
    Write-Host ""
    Write-Option "8" "重新掃描狀態" "刷新上面的環境檢查"
    Write-Host ""
    Write-Host "    [Q] " -NoNewline -ForegroundColor $MutedColor
    Write-Host "離開" -ForegroundColor $MutedColor
    Write-Host ""
}

function Read-MenuChoice {
    while ($true) {
        Write-Host -NoNewline "  請輸入 " -ForegroundColor $BorderColor
        Write-Host -NoNewline "[1-8/Q]" -ForegroundColor $AccentColor
        Write-Host -NoNewline ": " -ForegroundColor $BorderColor
        $raw = (Read-Host).Trim().ToUpper()
        if ($raw -in @("1", "2", "3", "4", "5", "6", "7", "8", "Q")) {
            return $raw
        }
        Write-Host "  無效輸入。" -ForegroundColor $ErrColor
    }
}

function Pause-MainMenu {
    Write-Host ""
    Write-Host -NoNewline "  按 " -ForegroundColor $MutedColor
    Write-Host -NoNewline "Enter" -ForegroundColor $AccentColor
    Write-Host -NoNewline " 回主選單..." -ForegroundColor $MutedColor
    [void](Read-Host)
}

# ============== Action handlers ==============
function Invoke-Quick {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [快速設定] 引導式設定 (自動建大腦 + 選 LLM + Discord)" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    # 不再用 -NonInteractive — 互動引導使用者一路設完
    $wizardArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "first-run-wizard.ps1"))
    if ($VaultRoot) { $wizardArgs += @("-VaultRoot", $VaultRoot) }
    & powershell @wizardArgs
}

function Invoke-Custom {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [自訂設定] 跑 first-run-wizard.ps1（互動模式）" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    $wizardArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "first-run-wizard.ps1"))
    if ($VaultRoot) { $wizardArgs += @("-VaultRoot", $VaultRoot) }
    & powershell @wizardArgs
}

function Invoke-StartSteward {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [上線管家] 跑 start-steward.ps1（按 Ctrl+C 結束）" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "start-steward.ps1"), "-PersistToken")
    & powershell @args
}

function Invoke-SwitchLlm {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [切換 LLM]" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "switch-llm.ps1"), "-PersistKey")
    if ($VaultRoot) { $args += @("-VaultRoot", $VaultRoot) }
    & powershell @args
}

function Invoke-DownloadModel {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [下載本地模型]" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "download-model.ps1"))
    & powershell @args
}

function Invoke-CliChat {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [CLI 對話模式 — 直接和管家連續對話]" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    Write-Host "  指令：/exit /quit :q 離開回主選單" -ForegroundColor $MutedColor
    Write-Host "        /llm <key>     對話中切模型 (gemma4/qwen9/qwen30/gemini/gemini-pro/gemma-31b/gemma-26b)" -ForegroundColor $MutedColor
    Write-Host "        同 session 連續累積對話記憶" -ForegroundColor $MutedColor
    Write-Host ""

    $sessionId = "cli-repl-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Write-Host "  session: $sessionId" -ForegroundColor $MutedColor
    Write-Host ""

    while ($true) {
        Write-Host -NoNewline "  你 > " -ForegroundColor $AccentColor
        $msg = (Read-Host).Trim()
        if (-not $msg) { continue }
        if ($msg -in @("/exit", "/quit", ":q", "exit", "quit")) {
            Write-Host "  [離開對話模式]" -ForegroundColor $MutedColor
            return
        }

        # 跑 chat
        $cliArgs = @("-X", "utf8", "-m", "agent_memory.cli")
        if ($VaultRoot) { $cliArgs += @("--vault-root", $VaultRoot) }
        $cliArgs += @("chat", $msg, "--persona", "steward", "--context", "cli-repl", "--session", $sessionId, "--allow-llm-degraded", "--json")

        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $raw = (& $PythonExe @cliArgs 2>&1 | Out-String)
        }
        finally {
            $ErrorActionPreference = $prevEap
        }

        $jsStart = $raw.IndexOf('{')
        $jsEnd = $raw.LastIndexOf('}')
        $printed = $false
        if ($jsStart -ge 0 -and $jsEnd -gt $jsStart) {
            try {
                $j = $raw.Substring($jsStart, $jsEnd - $jsStart + 1) | ConvertFrom-Json
                $isDegraded = [bool]$j.degraded
                $response = [string]$j.response
                Write-Host -NoNewline "  管家 > " -ForegroundColor Green
                Write-Host $response
                if ($isDegraded) {
                    Write-Host "    [⚠ degraded — LLM 沒實際回應,可能 key/模型/網路問題]" -ForegroundColor Yellow
                    Write-Host "    試 /llm gemma4 切本機,或回主選單 [4] 切換 LLM" -ForegroundColor $MutedColor
                }
                $printed = $true
            }
            catch { }
        }
        if (-not $printed) {
            Write-Host "  管家 > [no JSON response]" -ForegroundColor Red
            Write-Host "    raw: $($raw.Substring(0, [Math]::Min(200, $raw.Length)))" -ForegroundColor $MutedColor
        }
        Write-Host ""
    }
}

function Invoke-ToolingSmoke {
    Write-Host ""
    Write-Border "─"
    Write-Host "  [工具能力 smoke]" -ForegroundColor $TitleColor
    Write-Border "─"
    Write-Host ""
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "run-tooling-smoke.ps1"))
    if ($VaultRoot) { $args += @("-VaultRoot", $VaultRoot) }
    & powershell @args
}

# ============== Main loop ==============
while ($true) {
    Show-Banner
    Show-Status
    Show-Menu
    $choice = Read-MenuChoice

    switch ($choice) {
        "1" { Invoke-Quick; Pause-MainMenu }
        "2" { Invoke-Custom; Pause-MainMenu }
        "3" { Invoke-StartSteward; Pause-MainMenu }
        "4" { Invoke-SwitchLlm; Pause-MainMenu }
        "5" { Invoke-DownloadModel; Pause-MainMenu }
        "6" { Invoke-CliChat; Pause-MainMenu }
        "7" { Invoke-ToolingSmoke; Pause-MainMenu }
        "8" {
            # 純 status refresh — 主迴圈下一輪會重新顯示
        }
        "Q" {
            Write-Host ""
            Write-Host "  bye." -ForegroundColor $TitleColor
            Write-Host ""
            return
        }
    }
}
