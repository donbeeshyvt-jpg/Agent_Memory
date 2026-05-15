<#
.SYNOPSIS
  共用 .env 讀寫工具 (dot-source 進其他 PS 腳本用)。

.DESCRIPTION
  Agent Memory Core 用 .env 檔當作 API key / Discord token 等敏感變數的
  PROJECT-LOCAL 存放點 (放在 agent-memory-core/.env, 已被 .gitignore 蓋住)。
  比 setx 寫 Windows registry 好刪 (一個 rm 就清掉)、好版本控管。

  使用方式 (在其他 PS script 頂端):
    . "$PSScriptRoot\_dotenv-helper.ps1"
    Import-DotEnvIntoProcess

  寫入:
    Save-EntryToDotEnv -Key "GOOGLE_API_KEY" -Value "AIza..."
#>

function Resolve-VaultRoot {
    <#
    順序找 vault root:
      1. -VaultRoot 顯式傳入
      2. ~/.agent_memory/config.toml 的 [vault].root
      3. <core>/../SecondBrains/default_second_brain (預設 fallback)
    #>
    param([string]$VaultRoot = "")

    if ($VaultRoot) {
        return $VaultRoot
    }

    $cfg = Join-Path $env:USERPROFILE ".agent_memory\config.toml"
    if (Test-Path -LiteralPath $cfg) {
        try {
            $text = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
            $m = [regex]::Match($text, 'root\s*=\s*"([^"]+)"')
            if ($m.Success) {
                # toml 用 \\ 轉義 backslash
                return ($m.Groups[1].Value -replace '\\\\', '\')
            }
        }
        catch { }
    }

    # 預設 fallback
    $scriptsDir = $PSScriptRoot
    if (-not $scriptsDir) {
        $scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    $coreRoot = Split-Path -Parent $scriptsDir
    return (Join-Path (Split-Path -Parent $coreRoot) "SecondBrains\default_second_brain")
}

function Get-DotEnvPath {
    <#
    .env 統一放在 vault root (<vault>/.env), 不在 core repo 裡.
    這樣每個 brain 有自己的 keys, 切換 brain 也切換 keys.
    #>
    param([string]$VaultRoot = "")
    $vault = Resolve-VaultRoot -VaultRoot $VaultRoot
    return (Join-Path $vault ".env")
}

function Import-DotEnvIntoProcess {
    <#
    將 <vault>/.env 載入此 PS process 的 $env:。
    語意跟 python-dotenv 的 override=False 一致:
    已存在的 process env 不會被 .env 蓋掉 (使用者 setx 優先)。
    #>
    param([string]$EnvPath = "", [string]$VaultRoot = "")
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath -VaultRoot $VaultRoot
    }
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        return $false
    }
    try {
        $lines = Get-Content -LiteralPath $EnvPath -Encoding UTF8 -ErrorAction Stop
    }
    catch {
        return $false
    }
    $loaded = 0
    foreach ($line in $lines) {
        $t = [string]$line
        $t = $t.Trim()
        if (-not $t) { continue }
        if ($t.StartsWith("#")) { continue }
        $eq = $t.IndexOf("=")
        if ($eq -le 0) { continue }
        $k = $t.Substring(0, $eq).Trim()
        $v = $t.Substring($eq + 1).Trim()
        if ($v.StartsWith('"') -and $v.EndsWith('"') -and $v.Length -ge 2) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        elseif ($v.StartsWith("'") -and $v.EndsWith("'") -and $v.Length -ge 2) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        # override=False: 已存在 (process) 不蓋
        $existing = [Environment]::GetEnvironmentVariable($k, "Process")
        if ([string]::IsNullOrEmpty($existing)) {
            Set-Item -LiteralPath "Env:$k" -Value $v -ErrorAction SilentlyContinue
            $loaded++
        }
    }
    return $loaded
}

function Save-EntryToDotEnv {
    <#
    把單一 KEY=VALUE 寫入 .env 檔 (存在就 update,沒有就 append)。
    自動建檔含 header 註解。同時更新此 process 的 $env:。
    .env 放在 <vault>/.env (跟著 brain 走)。
    #>
    param(
        [Parameter(Mandatory=$true)] [string]$Key,
        [Parameter(Mandatory=$true)] [string]$Value,
        [string]$EnvPath = "",
        [string]$VaultRoot = ""
    )
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath -VaultRoot $VaultRoot
    }
    # 確保 vault 目錄存在 (新 brain 第一次寫 .env)
    $envDir = Split-Path -Parent $EnvPath
    if (-not (Test-Path -LiteralPath $envDir)) {
        New-Item -ItemType Directory -Path $envDir -Force | Out-Null
    }

    $lines = @()
    if (Test-Path -LiteralPath $EnvPath) {
        $lines = @(Get-Content -LiteralPath $EnvPath -Encoding UTF8)
    }
    else {
        $lines = @(
            "# Agent Memory Core - local secrets",
            "# This file is gitignored. NEVER commit it.",
            "# Format: KEY=VALUE (one per line)",
            ""
        )
    }

    $newLines = New-Object System.Collections.ArrayList
    $found = $false
    $prefix = "$Key="
    foreach ($l in $lines) {
        if ([string]$l -match "^\s*$([regex]::Escape($Key))\s*=") {
            [void]$newLines.Add("$Key=$Value")
            $found = $true
        }
        else {
            [void]$newLines.Add([string]$l)
        }
    }
    if (-not $found) {
        [void]$newLines.Add("$Key=$Value")
    }

    Set-Content -LiteralPath $EnvPath -Value $newLines -Encoding UTF8
    Set-Item -LiteralPath "Env:$Key" -Value $Value -ErrorAction SilentlyContinue
    return $EnvPath
}

function Remove-EntryFromDotEnv {
    <#
    從 .env 移除指定 key。也清掉 process env。
    #>
    param(
        [Parameter(Mandatory=$true)] [string]$Key,
        [string]$EnvPath = "",
        [string]$VaultRoot = ""
    )
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath -VaultRoot $VaultRoot
    }
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        # 也清掉 process env 以免殘留
        Remove-Item -LiteralPath "Env:$Key" -ErrorAction SilentlyContinue
        return $false
    }
    $lines = @(Get-Content -LiteralPath $EnvPath -Encoding UTF8)
    $newLines = $lines | Where-Object { $_ -notmatch "^\s*$([regex]::Escape($Key))\s*=" }
    Set-Content -LiteralPath $EnvPath -Value $newLines -Encoding UTF8
    Remove-Item -LiteralPath "Env:$Key" -ErrorAction SilentlyContinue
    return $true
}

function Test-DiscordToken {
    <#
    .SYNOPSIS
      真實 ping Discord API 驗證 bot token. 回傳 hashtable: ok / bot_name / bot_id / reason.
    .DESCRIPTION
      呼叫 https://discord.com/api/v10/users/@me 帶 Authorization: Bot <token>.
      - 200 → ok=$true + bot_name + bot_id
      - 401 → ok=$false, reason="Discord 回 401 Unauthorized — token 無效"
      - 其他 → ok=$false, reason=<具體訊息>
      Timeout 10 秒. 強制 TLS 1.2 (PS5.1 預設可能 TLS 1.0).
    #>
    param(
        [Parameter(Mandatory=$true)] [string]$Token,
        [int]$TimeoutSec = 10
    )
    $result = [ordered]@{
        ok = $false
        bot_name = ""
        bot_id = ""
        reason = ""
    }
    if ([string]::IsNullOrWhiteSpace($Token)) {
        $result.reason = "token 為空"
        return $result
    }
    if ($Token.Length -lt 30) {
        $result.reason = "token 長度只有 $($Token.Length) 字元 (Discord bot token 至少 50 字元)"
        return $result
    }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
    try {
        $headers = @{ Authorization = "Bot $Token" }
        $resp = Invoke-RestMethod -Uri "https://discord.com/api/v10/users/@me" -Headers $headers -Method Get -TimeoutSec $TimeoutSec -ErrorAction Stop
        $result.ok = $true
        $result.bot_name = [string]$resp.username
        $result.bot_id = [string]$resp.id
        if ($resp.discriminator -and $resp.discriminator -ne "0") {
            $result.bot_name = "$($resp.username)#$($resp.discriminator)"
        }
        $result.reason = "ok"
    }
    catch [System.Net.WebException] {
        $statusCode = 0
        try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { }
        if ($statusCode -eq 401) {
            $result.reason = "Discord 回 401 Unauthorized — token 無效 / 已 reset / 貼錯"
        }
        elseif ($statusCode -eq 403) {
            $result.reason = "Discord 回 403 Forbidden — bot 被 ban / scope 不足"
        }
        elseif ($statusCode -eq 429) {
            $result.reason = "Discord 回 429 Rate Limited — 等幾分鐘再試"
        }
        else {
            $result.reason = "網路 / API 問題: $($_.Exception.Message)"
        }
    }
    catch {
        $msg = [string]$_.Exception.Message
        if ($msg -match '401|Unauthorized') {
            $result.reason = "Discord 回 401 Unauthorized — token 無效 / 已 reset / 貼錯"
        }
        else {
            $result.reason = "驗證失敗: $msg"
        }
    }
    return $result
}

function Format-DiscordTokenPreview {
    <#
    .SYNOPSIS
      回傳 masked token 預覽 (前 6 + ... + 後 4) 供使用者目測核對.
    #>
    param([Parameter(Mandatory=$true)] [string]$Token)
    if ([string]::IsNullOrWhiteSpace($Token)) { return "(空)" }
    if ($Token.Length -lt 12) { return "(僅 $($Token.Length) 字元)" }
    return $Token.Substring(0, 6) + "..." + $Token.Substring($Token.Length - 4) + " (長度 $($Token.Length))"
}

function Test-KeyInDotEnv {
    param([string]$Key, [string]$EnvPath = "", [string]$VaultRoot = "")
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath -VaultRoot $VaultRoot
    }
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        return $false
    }
    $lines = @(Get-Content -LiteralPath $EnvPath -Encoding UTF8)
    foreach ($l in $lines) {
        if ([string]$l -match "^\s*$([regex]::Escape($Key))\s*=") {
            return $true
        }
    }
    return $false
}
