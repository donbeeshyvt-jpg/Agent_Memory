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

function Get-DotEnvPath {
    # 自動推 repo root = scripts/.. = agent-memory-core/
    $scriptsDir = $PSScriptRoot
    if (-not $scriptsDir) {
        $scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    return (Join-Path (Split-Path -Parent $scriptsDir) ".env")
}

function Import-DotEnvIntoProcess {
    <#
    將 agent-memory-core/.env 載入此 PS process 的 $env:。
    語意跟 python-dotenv 的 override=False 一致:
    已存在的 process env 不會被 .env 蓋掉 (使用者 setx 優先)。
    #>
    param([string]$EnvPath = "")
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath
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
    #>
    param(
        [Parameter(Mandatory=$true)] [string]$Key,
        [Parameter(Mandatory=$true)] [string]$Value,
        [string]$EnvPath = ""
    )
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath
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
        [string]$EnvPath = ""
    )
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath
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

function Test-KeyInDotEnv {
    param([string]$Key, [string]$EnvPath = "")
    if (-not $EnvPath) {
        $EnvPath = Get-DotEnvPath
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
