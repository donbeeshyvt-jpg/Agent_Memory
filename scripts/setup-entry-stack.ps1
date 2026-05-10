param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$ConfigFile = "",
    [string]$LlmDefaultProfile = "llama_cpp_local",
    [string]$LlmDefaultModel = "",
    [string]$LlmWriterProfile = "",
    [string]$LlmWriterModel = "",
    [string]$LlmCoderProfile = "",
    [string]$LlmCoderModel = "",
    [string]$DiscordChannelId = "",
    [string]$DiscordPersona = "core",
    [string]$DefaultPersona = "core",
    [string]$Operator = "entry-setup",
    [string]$BridgeUrl = "http://127.0.0.1:16000",
    [string]$Mode = "standard",
    [bool]$AllowLlmDegraded = $true,
    [switch]$RequireNonDegraded,
    [switch]$ProbeBridge,
    [switch]$SkipLlmConfig,
    [switch]$SkipDiscordSetup,
    [switch]$SkipSmoke,
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

    if ($config.llm) {
        if ($config.llm.default_profile) { $LlmDefaultProfile = [string]$config.llm.default_profile }
        if ($config.llm.default_model) { $LlmDefaultModel = [string]$config.llm.default_model }
        if ($config.llm.writer_curator_profile) { $LlmWriterProfile = [string]$config.llm.writer_curator_profile }
        if ($config.llm.writer_curator_model) { $LlmWriterModel = [string]$config.llm.writer_curator_model }
        if ($config.llm.coder_profile) { $LlmCoderProfile = [string]$config.llm.coder_profile }
        if ($config.llm.coder_model) { $LlmCoderModel = [string]$config.llm.coder_model }
        if ($config.llm.PSObject.Properties.Name -contains "require_nondegraded") {
            $RequireNonDegraded = ([bool]$config.llm.require_nondegraded)
        }
    }
    if ($config.discord) {
        if ($config.discord.channel_id) { $DiscordChannelId = [string]$config.discord.channel_id }
        if ($config.discord.persona) { $DiscordPersona = [string]$config.discord.persona }
        if ($config.discord.default_persona) { $DefaultPersona = [string]$config.discord.default_persona }
        if ($config.discord.operator) { $Operator = [string]$config.discord.operator }
        if ($config.discord.bridge_url) { $BridgeUrl = [string]$config.discord.bridge_url }
        if ($config.discord.mode) { $Mode = [string]$config.discord.mode }
        if ($config.discord.PSObject.Properties.Name -contains "probe_bridge") {
            $ProbeBridge = ([bool]$config.discord.probe_bridge)
        }
        if ($config.discord.PSObject.Properties.Name -contains "allow_llm_degraded") {
            $AllowLlmDegraded = [bool]$config.discord.allow_llm_degraded
        }
        if ($config.discord.PSObject.Properties.Name -contains "require_nondegraded") {
            $RequireNonDegraded = ([bool]$config.discord.require_nondegraded)
        }
    }

    if ($config.PSObject.Properties.Name -contains "llm_default_profile" -and $config.llm_default_profile) { $LlmDefaultProfile = [string]$config.llm_default_profile }
    if ($config.PSObject.Properties.Name -contains "llm_default_model" -and $config.llm_default_model) { $LlmDefaultModel = [string]$config.llm_default_model }
    if ($config.PSObject.Properties.Name -contains "llm_writer_curator_profile" -and $config.llm_writer_curator_profile) { $LlmWriterProfile = [string]$config.llm_writer_curator_profile }
    if ($config.PSObject.Properties.Name -contains "llm_writer_curator_model" -and $config.llm_writer_curator_model) { $LlmWriterModel = [string]$config.llm_writer_curator_model }
    if ($config.PSObject.Properties.Name -contains "llm_coder_profile" -and $config.llm_coder_profile) { $LlmCoderProfile = [string]$config.llm_coder_profile }
    if ($config.PSObject.Properties.Name -contains "llm_coder_model" -and $config.llm_coder_model) { $LlmCoderModel = [string]$config.llm_coder_model }
    if ($config.PSObject.Properties.Name -contains "discord_channel_id" -and $config.discord_channel_id) { $DiscordChannelId = [string]$config.discord_channel_id }
    if ($config.PSObject.Properties.Name -contains "discord_persona" -and $config.discord_persona) { $DiscordPersona = [string]$config.discord_persona }
    if ($config.PSObject.Properties.Name -contains "default_persona" -and $config.default_persona) { $DefaultPersona = [string]$config.default_persona }
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

function Invoke-DiscordSetupJson {
    $setupScript = Join-Path $PSScriptRoot "setup-discord-entry.ps1"
    if (-not (Test-Path -LiteralPath $setupScript)) {
        throw "setup script not found: $setupScript"
    }

    $setupParams = @{
        VaultRoot = $VaultRoot
        PythonExe = $PythonExe
        DiscordChannelId = $DiscordChannelId
        DiscordPersona = $DiscordPersona
        DefaultPersona = $DefaultPersona
        Operator = $Operator
        BridgeUrl = $BridgeUrl
        Mode = $Mode
        AllowLlmDegraded = $AllowLlmDegraded
        Json = $true
    }
    if ($ProbeBridge) { $setupParams.ProbeBridge = $true }
    if ($RequireNonDegraded) { $setupParams.RequireNonDegraded = $true }

    $stdout = & $setupScript @setupParams
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 1
    }
    if ($exitCode -ne 0) {
        throw "Discord setup failed (exit=$exitCode): $setupScript"
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

function Add-SmokeCheck {
    param(
        [System.Collections.IList]$Rows,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    $Rows.Add([ordered]@{
        name = $Name
        ok = $Ok
        detail = $Detail
    }) | Out-Null
}

$result = [ordered]@{
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    overall_ok = $true
    vault_root = $VaultRoot
    config_file = $ConfigFile
    llm = [ordered]@{
        configured = $false
        default_profile = $LlmDefaultProfile
        default_model = $LlmDefaultModel
        writer_profile = $LlmWriterProfile
        writer_model = $LlmWriterModel
        coder_profile = $LlmCoderProfile
        coder_model = $LlmCoderModel
        core_route = $null
        writer_route = $null
        coder_route = $null
    }
    discord = [ordered]@{
        configured = $false
        channel_id = $DiscordChannelId
        persona = $DiscordPersona
        default_persona = $DefaultPersona
        probe_bridge = [bool]$ProbeBridge
        summary = $null
    }
    smoke = [ordered]@{
        executed = $false
        checks = New-Object System.Collections.ArrayList
    }
}

try {
    if (-not $SkipLlmConfig) {
        Write-Host "[INFO] Resolving LLM route baseline..." -ForegroundColor Cyan
        $currentCore = Invoke-MemoryCliJson -StepName "llm-show-core-before" -CommandArgs @("llm-show", "--persona", "core", "--json")
        if (-not $LlmDefaultProfile) {
            $LlmDefaultProfile = [string]$currentCore.resolved.selected_profile
        }
        if (-not $LlmDefaultModel) {
            $LlmDefaultModel = [string]$currentCore.resolved.selected_model
        }
        if (-not $LlmDefaultProfile -or -not $LlmDefaultModel) {
            throw "Unable to resolve LLM default profile/model."
        }

        Write-Host "[INFO] Applying default LLM route..." -ForegroundColor Cyan
        $null = Invoke-MemoryCliJson -StepName "llm-set-default" -CommandArgs @(
            "llm-set-default",
            "--profile", $LlmDefaultProfile,
            "--model", $LlmDefaultModel,
            "--json"
        )

        if ($LlmWriterProfile -and $LlmWriterModel) {
            Write-Host "[INFO] Applying writer-curator persona LLM override..." -ForegroundColor Cyan
            $null = Invoke-MemoryCliJson -StepName "llm-set-writer-curator" -CommandArgs @(
                "llm-set-persona",
                "--persona", "writer-curator",
                "--profile", $LlmWriterProfile,
                "--model", $LlmWriterModel,
                "--json"
            )
        }

        if ($LlmCoderProfile -and $LlmCoderModel) {
            Write-Host "[INFO] Applying coder persona LLM override..." -ForegroundColor Cyan
            $null = Invoke-MemoryCliJson -StepName "llm-set-coder" -CommandArgs @(
                "llm-set-persona",
                "--persona", "coder",
                "--profile", $LlmCoderProfile,
                "--model", $LlmCoderModel,
                "--json"
            )
        }

        $result.llm.configured = $true
        $result.llm.default_profile = $LlmDefaultProfile
        $result.llm.default_model = $LlmDefaultModel
        $result.llm.core_route = Invoke-MemoryCliJson -StepName "llm-show-core-after" -CommandArgs @("llm-show", "--persona", "core", "--json")
        $result.llm.writer_route = Invoke-MemoryCliJson -StepName "llm-show-writer-after" -CommandArgs @("llm-show", "--persona", "writer-curator", "--json")
        $result.llm.coder_route = Invoke-MemoryCliJson -StepName "llm-show-coder-after" -CommandArgs @("llm-show", "--persona", "coder", "--json")
    }

    if (-not $SkipDiscordSetup) {
        if (-not $DiscordChannelId) {
            throw "Missing DiscordChannelId. Provide -DiscordChannelId or config.discord.channel_id."
        }
        Write-Host "[INFO] Applying Discord entry setup..." -ForegroundColor Cyan
        $discordSummary = Invoke-DiscordSetupJson
        $result.discord.configured = $true
        $result.discord.summary = $discordSummary
    }

    if (-not $SkipSmoke) {
        if (-not $DiscordChannelId) {
            throw "Missing DiscordChannelId for smoke checks."
        }

        $result.smoke.executed = $true
        Write-Host "[INFO] Running smoke check: binding/core..." -ForegroundColor Cyan
        $coreArgs = @(
            "chat",
            "entry stack smoke core",
            "--transport", "discord",
            "--channel-id", $DiscordChannelId,
            "--use-binding",
            "--mode", $Mode,
            "--timeout", "90",
            "--json"
        )
        if ($AllowLlmDegraded) { $coreArgs += "--allow-llm-degraded" }
        if ($RequireNonDegraded) { $coreArgs += "--require-nondegraded" }
        $coreSmoke = Invoke-MemoryCliJson -StepName "smoke-core-binding" -CommandArgs $coreArgs

        $expectedPersona = [string]$DiscordPersona
        if ($result.discord.summary -and $result.discord.summary.setup -and $result.discord.summary.setup.binding) {
            $expectedPersona = [string]$result.discord.summary.setup.binding.persona_id
        }
        $coreActualPersona = [string]$coreSmoke.persona
        $coreDegraded = [bool]$coreSmoke.degraded
        $coreOk = ($coreActualPersona -eq $expectedPersona)
        if ($RequireNonDegraded) {
            $coreOk = $coreOk -and (-not $coreDegraded)
        }
        Add-SmokeCheck -Rows $result.smoke.checks -Name "core_binding_route" -Ok $coreOk -Detail "expected=$expectedPersona actual=$coreActualPersona degraded=$coreDegraded"

        Write-Host "[INFO] Running smoke check: writer-curator/persona..." -ForegroundColor Cyan
        $personaList = Invoke-MemoryCliJson -StepName "persona-list" -CommandArgs @("persona-list", "--json")
        $hasWriter = $false
        $hasCoder = $false
        if ($personaList -and $personaList.personas) {
            $hasWriter = $personaList.personas.PSObject.Properties.Name -contains "writer-curator"
            $hasCoder = $personaList.personas.PSObject.Properties.Name -contains "coder"
        }
        if ($hasWriter) {
            $writerArgs = @(
                "chat",
                "entry stack smoke writer-curator",
                "--persona", "writer-curator",
                "--transport", "cli",
                "--context", "smoke",
                "--session", "entry-stack-writer",
                "--mode", "coach",
                "--timeout", "90",
                "--json"
            )
            if ($AllowLlmDegraded) { $writerArgs += "--allow-llm-degraded" }
            if ($RequireNonDegraded) { $writerArgs += "--require-nondegraded" }
            $writerSmoke = Invoke-MemoryCliJson -StepName "smoke-writer-curator" -CommandArgs $writerArgs
            $writerDegraded = [bool]$writerSmoke.degraded
            $writerOk = $true
            if ($RequireNonDegraded) {
                $writerOk = -not $writerDegraded
            }
            Add-SmokeCheck -Rows $result.smoke.checks -Name "writer_curator_route" -Ok $writerOk -Detail "degraded=$writerDegraded model=$($writerSmoke.llm.model)"
        }
        else {
            Add-SmokeCheck -Rows $result.smoke.checks -Name "writer_curator_route" -Ok $true -Detail "skipped (persona writer-curator not found)"
        }

        if ($hasCoder) {
            $coderArgs = @(
                "chat",
                "entry stack smoke coder",
                "--persona", "coder",
                "--transport", "cli",
                "--context", "smoke",
                "--session", "entry-stack-coder",
                "--mode", "executor",
                "--timeout", "90",
                "--json"
            )
            if ($AllowLlmDegraded) { $coderArgs += "--allow-llm-degraded" }
            if ($RequireNonDegraded) { $coderArgs += "--require-nondegraded" }
            $coderSmoke = Invoke-MemoryCliJson -StepName "smoke-coder" -CommandArgs $coderArgs
            $coderDegraded = [bool]$coderSmoke.degraded
            $coderOk = $true
            if ($RequireNonDegraded) {
                $coderOk = -not $coderDegraded
            }
            Add-SmokeCheck -Rows $result.smoke.checks -Name "coder_route" -Ok $coderOk -Detail "degraded=$coderDegraded model=$($coderSmoke.llm.model)"
        }
        else {
            $coderModeled = [bool]($LlmCoderProfile -and $LlmCoderModel)
            if ($coderModeled) {
                $coderArgs = @(
                    "chat",
                    "entry stack smoke coder",
                    "--persona", "coder",
                    "--transport", "cli",
                    "--context", "smoke",
                    "--session", "entry-stack-coder",
                    "--mode", "executor",
                    "--timeout", "90",
                    "--json"
                )
                if ($AllowLlmDegraded) { $coderArgs += "--allow-llm-degraded" }
                if ($RequireNonDegraded) { $coderArgs += "--require-nondegraded" }
                $coderSmoke = Invoke-MemoryCliJson -StepName "smoke-coder-virtual" -CommandArgs $coderArgs
                $coderDegraded = [bool]$coderSmoke.degraded
                $coderOk = $true
                if ($RequireNonDegraded) {
                    $coderOk = -not $coderDegraded
                }
                Add-SmokeCheck -Rows $result.smoke.checks -Name "coder_route" -Ok $coderOk -Detail "virtual persona check degraded=$coderDegraded model=$($coderSmoke.llm.model)"
            }
            else {
                Add-SmokeCheck -Rows $result.smoke.checks -Name "coder_route" -Ok $true -Detail "skipped (persona coder not found)"
            }
        }

        foreach ($row in $result.smoke.checks) {
            if (-not [bool]$row.ok) {
                $result.overall_ok = $false
            }
        }
    }
}
catch {
    $result.overall_ok = $false
    $result.error = $_.Exception.Message
}

$result.ended_at = (Get-Date).ToString("o")

if ($Json) {
    $result | ConvertTo-Json -Depth 14
    if ($result.overall_ok) { exit 0 } else { exit 1 }
}

if ($result.overall_ok) {
    Write-Host "[OK] Entry stack setup completed." -ForegroundColor Green
}
else {
    Write-Host "[ERR] Entry stack setup has failures." -ForegroundColor Red
}
Write-Host "[INFO] vault_root=$($result.vault_root)"
Write-Host "[INFO] llm_default=$($result.llm.default_profile) / $($result.llm.default_model)"
Write-Host "[INFO] discord_channel=$($result.discord.channel_id) persona=$($result.discord.persona)"
if ($result.smoke.executed) {
    foreach ($row in $result.smoke.checks) {
        Write-Host ("[INFO] smoke {0} ok={1} detail={2}" -f $row.name, $row.ok, $row.detail)
    }
}
if ($result.error) {
    Write-Host "[ERR] $($result.error)"
}
if ($result.overall_ok) { exit 0 } else { exit 1 }

