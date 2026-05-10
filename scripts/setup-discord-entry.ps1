param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$ConfigFile = "",
    [string]$DiscordChannelId = "",
    [string]$DiscordPersona = "writer-curator",
    [string]$DefaultPersona = "core",
    [string]$Operator = "entry-setup",
    [string]$BridgeUrl = "http://127.0.0.1:16000",
    [string]$Mode = "standard",
    [string]$SampleMessage = "discord entry setup probe",
    [bool]$AllowLlmDegraded = $true,
    [switch]$RequireNonDegraded,
    [switch]$ProbeBridge,
    [switch]$Interactive,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found: ${PythonExe}. Install Python or pass -PythonExe with a valid path."
}

if (-not $VaultRoot) {
    $defaultVault = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
    $VaultRoot = (Resolve-Path $defaultVault).Path
}
else {
    $VaultRoot = (Resolve-Path $VaultRoot).Path
}

if ($ConfigFile) {
    $configAbs = (Resolve-Path $ConfigFile).Path
    $configRaw = Get-Content -Path $configAbs -Encoding UTF8 -Raw
    $config = $configRaw | ConvertFrom-Json

    if ($config.default_persona) {
        $DefaultPersona = [string]$config.default_persona
    }
    if ($config.discord_channel_id) {
        $DiscordChannelId = [string]$config.discord_channel_id
    }
    if ($config.discord_persona) {
        $DiscordPersona = [string]$config.discord_persona
    }
    if ($config.operator) {
        $Operator = [string]$config.operator
    }
    if ($config.bridge_url) {
        $BridgeUrl = [string]$config.bridge_url
    }
    if ($config.mode) {
        $Mode = [string]$config.mode
    }
    if ($config.PSObject.Properties.Name -contains "allow_llm_degraded") {
        $AllowLlmDegraded = [bool]$config.allow_llm_degraded
    }
    if ($config.PSObject.Properties.Name -contains "probe_bridge") {
        $ProbeBridge = ([bool]$config.probe_bridge)
    }
    if ($config.PSObject.Properties.Name -contains "require_nondegraded") {
        $RequireNonDegraded = ([bool]$config.require_nondegraded)
    }
}

if ($Interactive) {
    if (-not $DiscordChannelId) {
        $DiscordChannelId = [string](Read-Host "Discord channel/thread id")
    }
    if (-not $DiscordPersona) {
        $DiscordPersona = [string](Read-Host "Discord persona id")
    }
    if (-not $DefaultPersona) {
        $DefaultPersona = [string](Read-Host "Default persona id")
    }
}

if (-not $DiscordChannelId) {
    throw "Missing -DiscordChannelId. Example: -DiscordChannelId guild-main"
}
if (-not $DiscordPersona) {
    throw "Missing -DiscordPersona."
}
if (-not $DefaultPersona) {
    throw "Missing -DefaultPersona."
}

function Invoke-MemoryCliJson {
    param(
        [string]$StepName,
        [string[]]$CommandArgs
    )

    $allArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $VaultRoot) + $CommandArgs
    $stdout = & $PythonExe @allArgs
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 1
    }
    if ($exitCode -ne 0) {
        throw "Step '$StepName' failed (exit=$exitCode): $PythonExe $($allArgs -join ' ')"
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

$result = [ordered]@{
    vault_root = $VaultRoot
    bridge_url = $BridgeUrl
    requested = [ordered]@{
        default_persona = $DefaultPersona
        discord_channel_id = $DiscordChannelId
        discord_persona = $DiscordPersona
        mode = $Mode
        allow_llm_degraded = [bool]$AllowLlmDegraded
        require_nondegraded = [bool]$RequireNonDegraded
        probe_bridge = [bool]$ProbeBridge
    }
    setup = [ordered]@{}
    probe = $null
}

Write-Host "[INFO] Configuring channel default persona..." -ForegroundColor Cyan
$defaultPayload = Invoke-MemoryCliJson -StepName "channel-default-persona" -CommandArgs @(
    "channel-default-persona",
    "--persona", $DefaultPersona,
    "--operator", $Operator,
    "--json"
)
$result.setup.default_persona = $defaultPayload

Write-Host "[INFO] Binding discord channel to persona..." -ForegroundColor Cyan
$bindPayload = Invoke-MemoryCliJson -StepName "channel-bind" -CommandArgs @(
    "channel-bind",
    "--transport", "discord",
    "--channel-id", $DiscordChannelId,
    "--persona", $DiscordPersona,
    "--operator", $Operator,
    "--json"
)
$result.setup.binding = $bindPayload

Write-Host "[INFO] Loading channel binding snapshot..." -ForegroundColor Cyan
$bindingsPayload = Invoke-MemoryCliJson -StepName "channel-bindings" -CommandArgs @(
    "channel-bindings",
    "--json"
)
$result.setup.bindings = $bindingsPayload

if ($ProbeBridge) {
    Write-Host "[INFO] Probing bridge webhook/discord..." -ForegroundColor Cyan
    $probeBody = [ordered]@{
        content = $SampleMessage
        channel_id = $DiscordChannelId
        author = @{
            id = "setup-probe-user"
        }
        mode = $Mode
        allow_llm_degraded = [bool]$AllowLlmDegraded
    }
    $probeResponse = Invoke-RestMethod `
        -Method Post `
        -Uri "$BridgeUrl/webhook/discord" `
        -ContentType "application/json" `
        -Body ($probeBody | ConvertTo-Json -Depth 8)

    $expectedPersona = [string]$bindPayload.persona_id
    $actualPersona = [string]$probeResponse.persona
    $degraded = [bool]$probeResponse.degraded
    $probeOk = ($actualPersona -eq $expectedPersona)
    if ($RequireNonDegraded -and $degraded) {
        $probeOk = $false
    }

    $result.probe = [ordered]@{
        ok = $probeOk
        expected_persona = $expectedPersona
        actual_persona = $actualPersona
        degraded = $degraded
        transport = [string]$probeResponse.transport
        channel_id = [string]$probeResponse.channel_id
        session_path = [string]$probeResponse.memory_paths.session
    }
    if (-not $probeOk) {
        throw "Bridge probe failed: expected_persona=$expectedPersona actual_persona=$actualPersona degraded=$degraded"
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 12
    exit 0
}

Write-Host "[OK] DC entry setup completed." -ForegroundColor Green
Write-Host "[OK] default_persona=$($result.setup.default_persona.default_persona)"
Write-Host "[OK] binding_key=$($result.setup.binding.key)"
Write-Host "[OK] binding_persona=$($result.setup.binding.persona_id)"
if ($ProbeBridge) {
    Write-Host "[OK] probe_persona=$($result.probe.actual_persona) degraded=$($result.probe.degraded)"
}

