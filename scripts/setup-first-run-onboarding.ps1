param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$ConfigFile = "",
    [string]$TemplateVault = "",
    [string]$OwnerId = "owner",
    [string]$BrainId = "",
    [string]$ChannelDefaultPersona = "steward",
    [switch]$SetDefaultVault,
    [switch]$Interactive,
    [switch]$RunSmoke,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Get-OptionalValue {
    param(
        [object]$Source,
        [string]$Name,
        [object]$Default
    )
    if ($null -eq $Source) {
        return $Default
    }
    if ($Source.PSObject.Properties.Name -contains $Name) {
        $value = $Source.$Name
        if ($null -ne $value) {
            return $value
        }
    }
    return $Default
}

function To-Bool {
    param(
        [object]$Value,
        [bool]$Default = $false
    )
    if ($null -eq $Value) {
        return $Default
    }
    if ($Value -is [bool]) {
        return [bool]$Value
    }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $Default
    }
    switch ($text.Trim().ToLowerInvariant()) {
        "1" { return $true }
        "true" { return $true }
        "yes" { return $true }
        "on" { return $true }
        "0" { return $false }
        "false" { return $false }
        "no" { return $false }
        "off" { return $false }
        default { return $Default }
    }
}

function To-ObjectArray {
    param(
        [object]$InputObject
    )
    if ($null -eq $InputObject) {
        return @()
    }
    if ($InputObject -is [System.Collections.IEnumerable] -and -not ($InputObject -is [string])) {
        $items = New-Object System.Collections.ArrayList
        foreach ($item in $InputObject) {
            $items.Add($item) | Out-Null
        }
        return @($items)
    }
    return @($InputObject)
}

function Invoke-MemoryCliJson {
    param(
        [string]$CurrentVaultRoot,
        [string]$StepName,
        [string[]]$CommandArgs
    )
    $allArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $CurrentVaultRoot) + $CommandArgs
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

function Add-StepResult {
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

if ($ConfigFile) {
    $configAbs = (Resolve-Path $ConfigFile).Path
    $configRaw = Get-Content -Path $configAbs -Encoding UTF8 -Raw
    $config = $configRaw | ConvertFrom-Json

    $VaultRoot = [string](Get-OptionalValue -Source $config -Name "vault_root" -Default $VaultRoot)
    $TemplateVault = [string](Get-OptionalValue -Source $config -Name "template_vault" -Default $TemplateVault)
    $OwnerId = [string](Get-OptionalValue -Source $config -Name "owner_id" -Default $OwnerId)
    $BrainId = [string](Get-OptionalValue -Source $config -Name "brain_id" -Default $BrainId)
    $ChannelDefaultPersona = [string](Get-OptionalValue -Source $config -Name "channel_default_persona" -Default $ChannelDefaultPersona)
    $SetDefaultVault = To-Bool -Value (Get-OptionalValue -Source $config -Name "set_default_vault" -Default $SetDefaultVault) -Default:$SetDefaultVault
    $RunSmoke = To-Bool -Value (Get-OptionalValue -Source $config -Name "run_smoke" -Default $RunSmoke) -Default:$RunSmoke
}
else {
    $config = $null
}

if ($Interactive) {
    if (-not $VaultRoot) {
        $VaultRoot = [string](Read-Host "Second brain vault path")
    }
    if (-not $TemplateVault) {
        $TemplateVault = [string](Read-Host "Template vault path (optional, press Enter to skip)")
    }
}

if (-not $VaultRoot) {
    $VaultRoot = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
}

$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found: ${PythonExe}."
}

if (-not (Test-Path -LiteralPath $VaultRoot)) {
    New-Item -ItemType Directory -Path $VaultRoot -Force | Out-Null
}
$VaultRoot = (Resolve-Path $VaultRoot).Path

$seedCfg = if ($null -ne $config) { Get-OptionalValue -Source $config -Name "seed" -Default $null } else { $null }
$seedOverwrite = To-Bool -Value (Get-OptionalValue -Source $seedCfg -Name "overwrite" -Default $false) -Default:$false
$seedIncludeSharedSkills = To-Bool -Value (Get-OptionalValue -Source $seedCfg -Name "include_shared_skills" -Default $false) -Default:$false
$seedSkipPersonas = To-Bool -Value (Get-OptionalValue -Source $seedCfg -Name "skip_personas" -Default $false) -Default:$false
$seedSkipPersonaSkills = To-Bool -Value (Get-OptionalValue -Source $seedCfg -Name "skip_persona_skills" -Default $false) -Default:$false
$seedSkipDialogueModes = To-Bool -Value (Get-OptionalValue -Source $seedCfg -Name "skip_dialogue_modes" -Default $false) -Default:$false

$butlerCfg = if ($null -ne $config) { Get-OptionalValue -Source $config -Name "butler" -Default $null } else { $null }
$butlerEnabled = To-Bool -Value (Get-OptionalValue -Source $butlerCfg -Name "enabled" -Default $false) -Default:$false
$butlerPersonaId = [string](Get-OptionalValue -Source $butlerCfg -Name "persona_id" -Default "steward")
$butlerDisplayName = [string](Get-OptionalValue -Source $butlerCfg -Name "display_name" -Default "Steward")
$butlerMission = [string](Get-OptionalValue -Source $butlerCfg -Name "mission" -Default "Act as the steward persona: setup environment, manage personas, and run tool-enabled coding tasks with traceable outputs.")
$butlerStyle = [string](Get-OptionalValue -Source $butlerCfg -Name "style" -Default "concise")
$butlerLanguage = [string](Get-OptionalValue -Source $butlerCfg -Name "language" -Default "zh-Hant")
$butlerRoleType = [string](Get-OptionalValue -Source $butlerCfg -Name "role_type" -Default "tooling")
$butlerMode = [string](Get-OptionalValue -Source $butlerCfg -Name "default_mode" -Default "executor")
$butlerOperator = [string](Get-OptionalValue -Source $butlerCfg -Name "operator" -Default "first-run")

$channels = @()
if ($null -ne $config) {
    $channels = To-ObjectArray -InputObject (Get-OptionalValue -Source $config -Name "channels" -Default @())
}

$summary = [ordered]@{
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    overall_ok = $true
    vault_root = $VaultRoot
    template_vault = $TemplateVault
    owner_id = $OwnerId
    brain_id = $BrainId
    set_default_vault = [bool]$SetDefaultVault
    run_smoke = [bool]$RunSmoke
    steps = New-Object System.Collections.ArrayList
    outputs = [ordered]@{
        brain_shell = $null
        seed = $null
        butler = $null
        channel_default = $null
        channel_bindings = @()
        smoke = @()
    }
}

try {
    $brainArgs = @("brain-shell", "--owner-id", $OwnerId, "--json")
    if (-not [string]::IsNullOrWhiteSpace($BrainId)) {
        $brainArgs += @("--brain-id", $BrainId)
    }
    if ($SetDefaultVault) {
        $brainArgs += "--set-default"
    }
    $brainPayload = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "brain-shell" -CommandArgs $brainArgs
    $summary.outputs.brain_shell = $brainPayload
    Add-StepResult -Rows $summary.steps -Name "brain-shell" -Ok $true -Detail "second brain scaffold initialized"

    if ($TemplateVault) {
        if (-not (Test-Path -LiteralPath $TemplateVault)) {
            throw "template vault not found: $TemplateVault"
        }
        $TemplateVault = (Resolve-Path $TemplateVault).Path
        $seedArgs = @(
            "brain-seed-template",
            "--template-vault", $TemplateVault,
            "--json"
        )
        if ($seedOverwrite) { $seedArgs += "--overwrite" }
        if ($seedIncludeSharedSkills) { $seedArgs += "--include-shared-skills" }
        if ($seedSkipPersonas) { $seedArgs += "--skip-personas" }
        if ($seedSkipPersonaSkills) { $seedArgs += "--skip-persona-skills" }
        if ($seedSkipDialogueModes) { $seedArgs += "--skip-dialogue-modes" }

        $seedPayload = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "brain-seed-template" -CommandArgs $seedArgs
        $summary.outputs.seed = $seedPayload
        $importedCount = 0
        if ($seedPayload -and $seedPayload.imported_personas) {
            $importedCount = @($seedPayload.imported_personas).Count
        }
        Add-StepResult -Rows $summary.steps -Name "brain-seed-template" -Ok $true -Detail "imported_personas=$importedCount"
    }
    else {
        Add-StepResult -Rows $summary.steps -Name "brain-seed-template" -Ok $true -Detail "skipped (no template_vault)"
    }

    if (-not [string]::IsNullOrWhiteSpace($ChannelDefaultPersona)) {
        $personaCatalog = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "persona-list-for-default" -CommandArgs @(
            "persona-list",
            "--json"
        )
        $resolvedDefaultPersona = $ChannelDefaultPersona
        $personaExists = $false
        if ($personaCatalog -and $personaCatalog.personas) {
            $personaExists = $personaCatalog.personas.PSObject.Properties.Name -contains $resolvedDefaultPersona
        }
        if (-not $personaExists) {
            if ($personaCatalog -and $personaCatalog.personas -and ($personaCatalog.personas.PSObject.Properties.Name -contains "core")) {
                $resolvedDefaultPersona = "core"
            }
            Add-StepResult -Rows $summary.steps -Name "channel-default-persona-fallback" -Ok $true -Detail "requested=$ChannelDefaultPersona resolved=$resolvedDefaultPersona"
        }
        $defaultPayload = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "channel-default-persona" -CommandArgs @(
            "channel-default-persona",
            "--persona", $resolvedDefaultPersona,
            "--operator", "first-run",
            "--json"
        )
        $summary.outputs.channel_default = $defaultPayload
        Add-StepResult -Rows $summary.steps -Name "channel-default-persona" -Ok $true -Detail "default=$resolvedDefaultPersona"
    }

    if ($butlerEnabled) {
        $personaList = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "persona-list" -CommandArgs @("persona-list", "--json")
        $exists = $false
        if ($personaList -and $personaList.personas) {
            $exists = $personaList.personas.PSObject.Properties.Name -contains $butlerPersonaId
        }
        if ($exists) {
            $updatePayload = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "persona-update-butler" -CommandArgs @(
                "persona-update",
                "--persona", $butlerPersonaId,
                "--display-name", $butlerDisplayName,
                "--mission", $butlerMission,
                "--style", $butlerStyle,
                "--language", $butlerLanguage,
                "--role-type", $butlerRoleType,
                "--default-mode", $butlerMode,
                "--operator", $butlerOperator,
                "--json"
            )
            $summary.outputs.butler = $updatePayload
            Add-StepResult -Rows $summary.steps -Name "butler-persona-update" -Ok $true -Detail "persona=$butlerPersonaId"
        }
        else {
            $createPayload = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "persona-create-butler" -CommandArgs @(
                "persona-create",
                "--display-name", $butlerDisplayName,
                "--persona-id", $butlerPersonaId,
                "--mission", $butlerMission,
                "--style", $butlerStyle,
                "--language", $butlerLanguage,
                "--role-type", $butlerRoleType,
                "--default-mode", $butlerMode,
                "--operator", $butlerOperator,
                "--auto-approve",
                "--json"
            )
            $summary.outputs.butler = $createPayload
            Add-StepResult -Rows $summary.steps -Name "butler-persona-create" -Ok $true -Detail "persona=$butlerPersonaId"
        }
    }
    else {
        Add-StepResult -Rows $summary.steps -Name "butler-persona" -Ok $true -Detail "skipped (disabled)"
    }

    foreach ($item in $channels) {
        $transport = [string](Get-OptionalValue -Source $item -Name "transport" -Default "")
        $channelId = [string](Get-OptionalValue -Source $item -Name "channel_id" -Default "")
        $persona = [string](Get-OptionalValue -Source $item -Name "persona" -Default "")
        $operator = [string](Get-OptionalValue -Source $item -Name "operator" -Default "first-run")
        if ([string]::IsNullOrWhiteSpace($transport) -or [string]::IsNullOrWhiteSpace($channelId) -or [string]::IsNullOrWhiteSpace($persona)) {
            Add-StepResult -Rows $summary.steps -Name "channel-bind" -Ok $false -Detail "invalid config row (transport/channel_id/persona required)"
            $summary.overall_ok = $false
            continue
        }
        $bindPayload = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "channel-bind-$transport-$channelId" -CommandArgs @(
            "channel-bind",
            "--transport", $transport,
            "--channel-id", $channelId,
            "--persona", $persona,
            "--operator", $operator,
            "--json"
        )
        $summary.outputs.channel_bindings += $bindPayload
        Add-StepResult -Rows $summary.steps -Name "channel-bind" -Ok $true -Detail "${transport}:${channelId} -> $persona"
    }

    if ($RunSmoke) {
        $coreSmoke = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "smoke-core" -CommandArgs @(
            "chat",
            "first run smoke core",
            "--persona", "core",
            "--context", "first-run",
            "--session", "smoke-core",
            "--transport", "cli",
            "--mode", "standard",
            "--allow-llm-degraded",
            "--json"
        )
        $summary.outputs.smoke += $coreSmoke
        Add-StepResult -Rows $summary.steps -Name "smoke-core" -Ok $true -Detail "degraded=$($coreSmoke.degraded)"

        if ($butlerEnabled) {
            $butlerSmoke = Invoke-MemoryCliJson -CurrentVaultRoot $VaultRoot -StepName "smoke-butler" -CommandArgs @(
                "chat",
                "first run smoke butler",
                "--persona", $butlerPersonaId,
                "--context", "first-run",
                "--session", "smoke-butler",
                "--transport", "cli",
                "--mode", $butlerMode,
                "--allow-llm-degraded",
                "--json"
            )
            $summary.outputs.smoke += $butlerSmoke
            Add-StepResult -Rows $summary.steps -Name "smoke-butler" -Ok $true -Detail "persona=$butlerPersonaId degraded=$($butlerSmoke.degraded)"
        }
    }
    else {
        Add-StepResult -Rows $summary.steps -Name "smoke" -Ok $true -Detail "skipped"
    }
}
catch {
    $summary.overall_ok = $false
    $summary.error = $_.Exception.Message
    Add-StepResult -Rows $summary.steps -Name "error" -Ok $false -Detail $_.Exception.Message
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
    Write-Host "[OK] first-run onboarding completed." -ForegroundColor Green
}
else {
    Write-Host "[ERR] first-run onboarding has failures." -ForegroundColor Red
}
Write-Host "[INFO] vault_root=$($summary.vault_root)"
Write-Host "[INFO] template_vault=$($summary.template_vault)"
$stepCount = @($summary.steps).Count
Write-Host "[INFO] steps=$stepCount"
Write-Host "[INFO] next steps prepared."
if ($summary.overall_ok) { exit 0 } else { exit 1 }
