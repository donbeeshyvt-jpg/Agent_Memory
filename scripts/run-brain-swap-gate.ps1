param(
    [string]$SourceVaultRoot = "",
    [string]$TemplateVaultRoot = "",
    [string]$PythonExe = "python",
    [string]$PackPersona = "writer-curator",
    [string]$TargetOwnerId = "brain-swap-gate",
    [string]$TargetTransport = "web",
    [string]$TargetMode = "standard",
    [switch]$RequireNonDegraded,
    [string]$OutputRoot = "artifacts/brain_swap_gate_runs",
    [switch]$CleanupTargetVault
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found: ${PythonExe}. Install Python or pass -PythonExe with a valid path."
}

if (-not $SourceVaultRoot) {
    $defaultVault = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
    $SourceVaultRoot = (Resolve-Path $defaultVault).Path
}
else {
    $SourceVaultRoot = (Resolve-Path $SourceVaultRoot).Path
}

if (-not $TemplateVaultRoot) {
    $candidateTemplates = @(
        (Join-Path $projectRoot "templates\\persona_factory_vault"),
        (Join-Path $projectRoot "legacy_fixtures\\_tmp_persona_factory_vault"),
        (Join-Path $projectRoot "..\\_tmp_persona_factory_vault")
    )
    $resolvedTemplate = $null
    foreach ($candidate in $candidateTemplates) {
        if (Test-Path -LiteralPath $candidate) {
            $resolvedTemplate = (Resolve-Path $candidate).Path
            break
        }
    }
    if (-not $resolvedTemplate) {
        throw "Template vault not found. Checked: $($candidateTemplates -join ', ')"
    }
    $TemplateVaultRoot = $resolvedTemplate
}
else {
    $TemplateVaultRoot = (Resolve-Path $TemplateVaultRoot).Path
}

$runId = "brain-swap-gate-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$runDir = Join-Path $projectRoot (Join-Path $OutputRoot $runId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$targetVaultRoot = Join-Path $runDir "target_brain"

function Invoke-MemoryCliStep {
    param(
        [string]$StepName,
        [string]$VaultRoot,
        [string[]]$CommandArgs
    )

    $stdoutPath = Join-Path $runDir "$StepName.stdout.log"
    $stderrPath = Join-Path $runDir "$StepName.stderr.log"
    $allArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $VaultRoot) + $CommandArgs

    $oldErr = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonExe @allArgs 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErr
    }
    if ($null -eq $exitCode) {
        $exitCode = 1
    }
    if (-not (Test-Path -LiteralPath $stdoutPath)) {
        Set-Content -Path $stdoutPath -Value "" -Encoding UTF8
    }
    if (-not (Test-Path -LiteralPath $stderrPath)) {
        Set-Content -Path $stderrPath -Value "" -Encoding UTF8
    }
    $stdoutResolved = if (Test-Path -LiteralPath $stdoutPath) { (Resolve-Path $stdoutPath).Path } else { $stdoutPath }
    $stderrResolved = if (Test-Path -LiteralPath $stderrPath) { (Resolve-Path $stderrPath).Path } else { $stderrPath }

    return [ordered]@{
        name = $StepName
        command = "$PythonExe $($allArgs -join ' ')"
        exit_code = $exitCode
        ok = ($exitCode -eq 0)
        stdout = $stdoutResolved
        stderr = $stderrResolved
    }
}

function Parse-StepJson {
    param(
        [hashtable]$Step
    )
    $raw = Get-Content -Path $Step.stdout -Encoding UTF8 -Raw
    if ($null -eq $raw) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    try {
        return ($raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Add-Check {
    param(
        [System.Collections.IList]$Checks,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    $Checks.Add([ordered]@{
        name = $Name
        ok = $Ok
        detail = $Detail
    }) | Out-Null
}

function Write-MarkdownSummary {
    param(
        [hashtable]$Payload,
        [string]$Path
    )

    $lines = @()
    $lines += "# Brain Swap Gate Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- source_vault_root: ``$($Payload.source_vault_root)``"
    $lines += "- template_vault_root: ``$($Payload.template_vault_root)``"
    $lines += "- target_vault_root: ``$($Payload.target_vault_root)``"
    $lines += "- target_vault_cleaned: ``$($Payload.target_vault_cleaned)``"
    $lines += "- pack_persona: ``$($Payload.pack_persona)``"
    $lines += "- bundle_path: ``$($Payload.bundle_path)``"
    $lines += "- source_brain_id: ``$($Payload.source_brain_id)``"
    $lines += "- target_brain_id: ``$($Payload.target_brain_id)``"
    $lines += "- require_nondegraded: ``$($Payload.require_nondegraded)``"
    $lines += ""
    $lines += "## Checks"
    $lines += ""
    foreach ($check in $Payload.checks) {
        $status = if ($check.ok) { "PASS" } else { "FAIL" }
        $lines += "- [$status] ``$($check.name)`` | $($check.detail)"
    }
    $lines += ""
    $lines += "## Steps"
    $lines += ""
    foreach ($step in $Payload.steps) {
        $status = if ($step.ok) { "PASS" } else { "FAIL" }
        $lines += "- [$status] ``$($step.name)`` | exit_code=$($step.exit_code)"
        $lines += "  - command: ``$($step.command)``"
        $lines += "  - stdout: ``$($step.stdout)``"
        $lines += "  - stderr: ``$($step.stderr)``"
    }
    Set-Content -Path $Path -Value ($lines -join [Environment]::NewLine) -Encoding UTF8
}

$summary = [ordered]@{
    run_id = $runId
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    overall_ok = $true
    source_vault_root = $SourceVaultRoot
    template_vault_root = $TemplateVaultRoot
    target_vault_root = $targetVaultRoot
    target_vault_cleaned = $false
    pack_persona = $PackPersona
    bundle_path = ""
    source_brain_id = ""
    target_brain_id = ""
    require_nondegraded = [bool]$RequireNonDegraded
    checks = New-Object System.Collections.ArrayList
    steps = @()
}

Write-Host "[INFO] Brain swap gate run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$sourceBrainStep = Invoke-MemoryCliStep -StepName "source-brain-show" -VaultRoot $SourceVaultRoot -CommandArgs @(
    "brain-show",
    "--json"
)
$summary.steps += $sourceBrainStep
if (-not $sourceBrainStep.ok) {
    $summary.overall_ok = $false
}
$sourceBrainPayload = Parse-StepJson -Step $sourceBrainStep
if ($sourceBrainPayload -and $sourceBrainPayload.manifest) {
    $summary.source_brain_id = [string]$sourceBrainPayload.manifest.brain_id
}
Add-Check -Checks $summary.checks -Name "source_brain_id_exists" -Ok ([bool]$summary.source_brain_id) -Detail "source_brain_id=$($summary.source_brain_id)"
if (-not $summary.source_brain_id) {
    $summary.overall_ok = $false
}

$sourcePersonaStep = Invoke-MemoryCliStep -StepName "source-persona-list" -VaultRoot $SourceVaultRoot -CommandArgs @(
    "persona-list",
    "--json"
)
$summary.steps += $sourcePersonaStep
if (-not $sourcePersonaStep.ok) {
    $summary.overall_ok = $false
}
$sourcePersonaPayload = Parse-StepJson -Step $sourcePersonaStep
$sourcePersonas = @{}
if ($sourcePersonaPayload -and $sourcePersonaPayload.personas) {
    $sourcePersonas = $sourcePersonaPayload.personas
}
$personaExists = ($sourcePersonas.PSObject.Properties.Name -contains $PackPersona)
Add-Check -Checks $summary.checks -Name "source_pack_persona_exists" -Ok $personaExists -Detail "persona=$PackPersona"
if (-not $personaExists) {
    $summary.overall_ok = $false
}

$packStep = Invoke-MemoryCliStep -StepName "source-persona-pack" -VaultRoot $SourceVaultRoot -CommandArgs @(
    "persona-pack",
    "--persona", $PackPersona,
    "--json"
)
$summary.steps += $packStep
if (-not $packStep.ok) {
    $summary.overall_ok = $false
}
$packPayload = Parse-StepJson -Step $packStep
$bundlePath = ""
if ($packPayload) {
    $bundlePath = [string]$packPayload.bundle_path
}
$summary.bundle_path = $bundlePath
$bundleExists = ($bundlePath -and (Test-Path -LiteralPath $bundlePath))
Add-Check -Checks $summary.checks -Name "persona_bundle_exists" -Ok $bundleExists -Detail "bundle_path=$bundlePath"
if (-not $bundleExists) {
    $summary.overall_ok = $false
}

if (Test-Path -LiteralPath $targetVaultRoot) {
    Remove-Item -LiteralPath $targetVaultRoot -Recurse -Force
}

$targetShellStep = Invoke-MemoryCliStep -StepName "target-brain-shell" -VaultRoot $targetVaultRoot -CommandArgs @(
    "brain-shell",
    "--owner-id", $TargetOwnerId,
    "--json"
)
$summary.steps += $targetShellStep
if (-not $targetShellStep.ok) {
    $summary.overall_ok = $false
}

$seedStep = Invoke-MemoryCliStep -StepName "target-seed-template" -VaultRoot $targetVaultRoot -CommandArgs @(
    "brain-seed-template",
    "--template-vault", $TemplateVaultRoot,
    "--json"
)
$summary.steps += $seedStep
if (-not $seedStep.ok) {
    $summary.overall_ok = $false
}
$seedPayload = Parse-StepJson -Step $seedStep
$importedCount = 0
if ($seedPayload -and $seedPayload.imported_personas) {
    $importedCount = @($seedPayload.imported_personas).Count
}
Add-Check -Checks $summary.checks -Name "template_seed_imported_personas" -Ok ($importedCount -ge 1) -Detail "imported_personas=$importedCount"
if ($importedCount -lt 1) {
    $summary.overall_ok = $false
}

$targetBrainStep = Invoke-MemoryCliStep -StepName "target-brain-show" -VaultRoot $targetVaultRoot -CommandArgs @(
    "brain-show",
    "--json"
)
$summary.steps += $targetBrainStep
if (-not $targetBrainStep.ok) {
    $summary.overall_ok = $false
}
$targetBrainPayload = Parse-StepJson -Step $targetBrainStep
if ($targetBrainPayload -and $targetBrainPayload.manifest) {
    $summary.target_brain_id = [string]$targetBrainPayload.manifest.brain_id
}
Add-Check -Checks $summary.checks -Name "target_brain_id_exists" -Ok ([bool]$summary.target_brain_id) -Detail "target_brain_id=$($summary.target_brain_id)"
if (-not $summary.target_brain_id) {
    $summary.overall_ok = $false
}
$brainSeparated = ($summary.source_brain_id -and $summary.target_brain_id -and ($summary.source_brain_id -ne $summary.target_brain_id))
Add-Check -Checks $summary.checks -Name "brain_id_separated" -Ok $brainSeparated -Detail "source=$($summary.source_brain_id) target=$($summary.target_brain_id)"
if (-not $brainSeparated) {
    $summary.overall_ok = $false
}

$unpackStep = Invoke-MemoryCliStep -StepName "target-persona-unpack" -VaultRoot $targetVaultRoot -CommandArgs @(
    "persona-unpack",
    "--package", $bundlePath,
    "--overwrite",
    "--json"
)
$summary.steps += $unpackStep
if (-not $unpackStep.ok) {
    $summary.overall_ok = $false
}
$unpackPayload = Parse-StepJson -Step $unpackStep
$copiedCount = 0
if ($unpackPayload) {
    $copiedCount = [int]$unpackPayload.copied_count
}
Add-Check -Checks $summary.checks -Name "persona_unpack_copied" -Ok ($copiedCount -gt 0) -Detail "copied_count=$copiedCount"
if ($copiedCount -le 0) {
    $summary.overall_ok = $false
}

$targetPersonaStep = Invoke-MemoryCliStep -StepName "target-persona-list" -VaultRoot $targetVaultRoot -CommandArgs @(
    "persona-list",
    "--json"
)
$summary.steps += $targetPersonaStep
if (-not $targetPersonaStep.ok) {
    $summary.overall_ok = $false
}
$targetPersonaPayload = Parse-StepJson -Step $targetPersonaStep
$targetPersonas = @{}
if ($targetPersonaPayload -and $targetPersonaPayload.personas) {
    $targetPersonas = $targetPersonaPayload.personas
}
$targetPackPersonaExists = ($targetPersonas.PSObject.Properties.Name -contains $PackPersona)
Add-Check -Checks $summary.checks -Name "target_pack_persona_exists" -Ok $targetPackPersonaExists -Detail "persona=$PackPersona"
if (-not $targetPackPersonaExists) {
    $summary.overall_ok = $false
}
$targetCoreExists = ($targetPersonas.PSObject.Properties.Name -contains "core")
Add-Check -Checks $summary.checks -Name "target_core_exists" -Ok $targetCoreExists -Detail "persona=core"
if (-not $targetCoreExists) {
    $summary.overall_ok = $false
}

$chatArgs = @(
    "chat",
    "brain_swap_probe_$runId",
    "--persona", $PackPersona,
    "--transport", $TargetTransport,
    "--context", "brain-swap-gate",
    "--session", "$runId-$PackPersona",
    "--mode", $TargetMode,
    "--allow-llm-degraded",
    "--json"
)
if ($RequireNonDegraded) {
    $chatArgs += "--require-nondegraded"
}
$chatStep = Invoke-MemoryCliStep -StepName "target-chat-probe" -VaultRoot $targetVaultRoot -CommandArgs $chatArgs
$summary.steps += $chatStep
if (-not $chatStep.ok) {
    $summary.overall_ok = $false
}
$chatPayload = Parse-StepJson -Step $chatStep
$chatSessionExists = $false
$chatRouteEventExists = $false
$chatDegraded = $true
$chatMode = ""
if ($chatPayload) {
    $chatDegraded = [bool]$chatPayload.degraded
    if ($chatPayload.dialogue_mode) {
        $chatMode = [string]$chatPayload.dialogue_mode.mode
    }
    $chatRouteEventExists = ($null -ne $chatPayload.llm_route_event)
    $chatSessionPath = ""
    if ($chatPayload.memory_paths) {
        $chatSessionPath = [string]$chatPayload.memory_paths.session
    }
    if ($chatSessionPath) {
        $sessionAbs = Join-Path $targetVaultRoot $chatSessionPath
        $chatSessionExists = Test-Path -LiteralPath $sessionAbs
    }
}
else {
    $summary.overall_ok = $false
}
Add-Check -Checks $summary.checks -Name "target_chat_mode_match" -Ok ($chatMode -eq $TargetMode) -Detail "mode=$chatMode expected=$TargetMode"
if ($chatMode -ne $TargetMode) {
    $summary.overall_ok = $false
}
Add-Check -Checks $summary.checks -Name "target_chat_session_written" -Ok $chatSessionExists -Detail "session_exists=$chatSessionExists"
if (-not $chatSessionExists) {
    $summary.overall_ok = $false
}
Add-Check -Checks $summary.checks -Name "target_chat_route_event_exists" -Ok $chatRouteEventExists -Detail "route_event_exists=$chatRouteEventExists"
if (-not $chatRouteEventExists) {
    $summary.overall_ok = $false
}
$degradedCheckOk = $true
if ($RequireNonDegraded) {
    $degradedCheckOk = -not $chatDegraded
}
Add-Check -Checks $summary.checks -Name "target_chat_degraded_policy" -Ok $degradedCheckOk -Detail "degraded=$chatDegraded require_nondegraded=$RequireNonDegraded"
if (-not $degradedCheckOk) {
    $summary.overall_ok = $false
}

$targetE2eStep = Invoke-MemoryCliStep -StepName "target-core-e2e" -VaultRoot $targetVaultRoot -CommandArgs @(
    "core-e2e",
    "--json"
)
$summary.steps += $targetE2eStep
if (-not $targetE2eStep.ok) {
    $summary.overall_ok = $false
}
$targetE2ePayload = Parse-StepJson -Step $targetE2eStep
$targetE2eOk = $false
if ($targetE2ePayload) {
    $targetE2eOk = [bool]$targetE2ePayload.overall_ok
}
Add-Check -Checks $summary.checks -Name "target_core_e2e_ok" -Ok $targetE2eOk -Detail "overall_ok=$targetE2eOk"
if (-not $targetE2eOk) {
    $summary.overall_ok = $false
}

if ($CleanupTargetVault) {
    if (Test-Path -LiteralPath $targetVaultRoot) {
        Remove-Item -LiteralPath $targetVaultRoot -Recurse -Force
        $summary.target_vault_cleaned = $true
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Brain swap gate completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Brain swap gate has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

