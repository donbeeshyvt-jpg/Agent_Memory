param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Invoke-MemoryCliJson {
    param(
        [string]$CurrentVaultRoot,
        [string[]]$CommandArgs
    )
    $allArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $CurrentVaultRoot) + $CommandArgs
    $stdout = & $PythonExe @allArgs
    if ($LASTEXITCODE -ne 0) {
        throw "memory-cli failed: $PythonExe $($allArgs -join ' ')"
    }
    $raw = ""
    if ($stdout -is [array]) {
        $raw = [string]::Join([Environment]::NewLine, $stdout)
    }
    else {
        $raw = [string]$stdout
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return ($raw | ConvertFrom-Json)
}

if (-not $VaultRoot) {
    $VaultRoot = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
}
if (-not (Test-Path -LiteralPath $VaultRoot)) {
    New-Item -ItemType Directory -Path $VaultRoot -Force | Out-Null
}
$VaultRoot = (Resolve-Path $VaultRoot).Path

$summary = [ordered]@{
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    vault_root = $VaultRoot
    overall_ok = $true
    steps = @()
    outputs = [ordered]@{
        steward_result = $null
        chat_result = $null
        steward_probe_file = ""
        chat_probe_file = ""
    }
}

try {
    $null = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -CommandArgs @("brain-shell", "--json")
    $summary.steps += [ordered]@{ name = "brain-shell"; ok = $true; detail = "ok" }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stewardProbe = "artifacts/tooling_smoke/steward_probe_$stamp.txt"
    $chatProbe = "artifacts/tooling_smoke/chat_probe_$stamp.txt"
    $summary.outputs.steward_probe_file = (Join-Path $projectRoot $stewardProbe)
    $summary.outputs.chat_probe_file = (Join-Path $projectRoot $chatProbe)

    $stewardMsg = "/tool {action:write_file,target:workspace,path:$stewardProbe,content:tooling-smoke-ok}"
    $stewardResult = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -CommandArgs @(
        "chat",
        $stewardMsg,
        "--persona", "steward",
        "--context", "tooling-smoke",
        "--session", "tooling-smoke-steward",
        "--transport", "cli",
        "--json"
    )
    $summary.outputs.steward_result = $stewardResult
    $stewardFileAbs = Join-Path $projectRoot $stewardProbe
    $stewardOk = $false
    if ($stewardResult -and $stewardResult.tool_payload -and $stewardResult.tool_payload.ok -eq $true) {
        $stewardOk = (Test-Path -LiteralPath $stewardFileAbs)
    }
    $summary.steps += [ordered]@{ name = "steward-tool-write"; ok = [bool]$stewardOk; detail = $stewardFileAbs }
    if (-not $stewardOk) {
        throw "steward tool write failed"
    }

    $personaList = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -CommandArgs @("persona-list", "--json")
    $hasChatPersona = $false
    if ($personaList -and $personaList.personas) {
        $hasChatPersona = $personaList.personas.PSObject.Properties.Name -contains "chat-smoke"
    }
    if (-not $hasChatPersona) {
        $null = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -CommandArgs @(
            "persona-create",
            "--display-name", "chat-smoke",
            "--persona-id", "chat-smoke",
            "--role-type", "chat",
            "--auto-approve",
            "--json"
        )
    }
    $summary.steps += [ordered]@{ name = "chat-persona-ready"; ok = $true; detail = "chat-smoke" }

    $chatMsg = "/tool {action:write_file,target:workspace,path:$chatProbe,content:chat-should-deny}"
    $chatResult = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -CommandArgs @(
        "chat",
        $chatMsg,
        "--persona", "chat-smoke",
        "--context", "tooling-smoke",
        "--session", "tooling-smoke-chat",
        "--transport", "cli",
        "--json"
    )
    $summary.outputs.chat_result = $chatResult
    $chatFileAbs = Join-Path $projectRoot $chatProbe
    $chatDenied = $false
    if ($chatResult -and $chatResult.tool_payload) {
        $chatDenied = ($chatResult.tool_payload.error -eq "tools_disabled_for_persona") -and (-not (Test-Path -LiteralPath $chatFileAbs))
    }
    $summary.steps += [ordered]@{ name = "chat-tool-deny"; ok = [bool]$chatDenied; detail = $chatFileAbs }
    if (-not $chatDenied) {
        throw "chat persona tool deny failed"
    }
}
catch {
    $summary.overall_ok = $false
    $summary.error = $_.Exception.Message
    $summary.steps += [ordered]@{ name = "error"; ok = $false; detail = $_.Exception.Message }
}

foreach ($step in $summary.steps) {
    if (-not [bool]$step.ok) {
        $summary.overall_ok = $false
        break
    }
}
$summary.ended_at = (Get-Date).ToString("o")

if ($Json) {
    $summary | ConvertTo-Json -Depth 14
    if ($summary.overall_ok) { exit 0 } else { exit 1 }
}

if ($summary.overall_ok) {
    Write-Host "[OK] tooling smoke passed." -ForegroundColor Green
}
else {
    Write-Host "[ERR] tooling smoke failed." -ForegroundColor Red
}
Write-Host "[INFO] vault_root=$($summary.vault_root)"
Write-Host "[INFO] steward_probe_file=$($summary.outputs.steward_probe_file)"
Write-Host "[INFO] chat_probe_file=$($summary.outputs.chat_probe_file)"
if ($summary.overall_ok) { exit 0 } else { exit 1 }
