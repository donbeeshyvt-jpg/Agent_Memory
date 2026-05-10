param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$OutputRoot = "artifacts/persona_matrix_runs",
    [string[]]$Personas = @("core", "writer-curator", "research-synthesizer"),
    [string]$Transport = "web",
    [string[]]$Modes = @(),
    [switch]$UseAllModes,
    [switch]$RequireNonDegraded,
    [switch]$SkipPortability
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

$runId = "persona-matrix-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$runDir = Join-Path $projectRoot (Join-Path $OutputRoot $runId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

function Invoke-MemoryCliStep {
    param(
        [string]$StepName,
        [string[]]$CommandArgs
    )

    $stdoutPath = Join-Path $runDir "$StepName.stdout.log"
    $stderrPath = Join-Path $runDir "$StepName.stderr.log"
    $allArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $VaultRoot) + $CommandArgs

    $proc = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $allArgs `
        -Wait `
        -PassThru `
        -NoNewWindow `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath
    $exitCode = $proc.ExitCode

    return [ordered]@{
        name = $StepName
        command = "$PythonExe $($allArgs -join ' ')"
        exit_code = $exitCode
        ok = ($exitCode -eq 0)
        stdout = (Resolve-Path $stdoutPath).Path
        stderr = (Resolve-Path $stderrPath).Path
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

function Write-MarkdownSummary {
    param(
        [hashtable]$Payload,
        [string]$Path
    )

    $lines = @()
    $lines += "# Persona Collaboration Matrix Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- transport: ``$($Payload.transport)``"
    $lines += "- require_nondegraded: ``$($Payload.require_nondegraded)``"
    $lines += "- modes: ``$($Payload.modes_used -join ', ')``"
    $lines += "- portability_skipped: ``$($Payload.portability_skipped)``"
    $lines += "- passed_cases: ``$($Payload.passed_cases)`` / ``$($Payload.total_cases)``"
    $lines += ""
    $lines += "## Persona Checks"
    $lines += ""
    foreach ($row in $Payload.persona_checks) {
        $status = if ($row.ok) { "PASS" } else { "FAIL" }
        $lines += "- [$status] persona=$($row.persona) | requested=$($row.requested_mode) | resolved=$($row.resolved_mode) | source=$($row.resolved_source) | degraded=$($row.degraded) | llm=$($row.llm_profile) | session=$($row.session_path_exists) | note=$($row.note)"
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
    vault_root = $VaultRoot
    transport = $Transport
    require_nondegraded = [bool]$RequireNonDegraded
    modes_used = @()
    portability_skipped = [bool]$SkipPortability
    total_cases = 0
    passed_cases = 0
    persona_checks = @()
    steps = @()
}

Write-Host "[INFO] Persona matrix run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$personaListStep = Invoke-MemoryCliStep -StepName "persona-list" -CommandArgs @("persona-list", "--json")
$summary.steps += $personaListStep
if (-not $personaListStep.ok) {
    $summary.overall_ok = $false
}
$personaListPayload = Parse-StepJson -Step $personaListStep
$personaRegistry = @{}
if ($personaListPayload -and $personaListPayload.personas) {
    $personaRegistry = $personaListPayload.personas
}

$modeCatalogStep = Invoke-MemoryCliStep -StepName "dialogue-mode-catalog" -CommandArgs @("dialogue-mode-show", "--json")
$summary.steps += $modeCatalogStep
if (-not $modeCatalogStep.ok) {
    $summary.overall_ok = $false
}
$modeCatalogPayload = Parse-StepJson -Step $modeCatalogStep
$availableModes = @()
if ($modeCatalogPayload -and $modeCatalogPayload.modes) {
    $availableModes = @($modeCatalogPayload.modes.PSObject.Properties.Name)
}

$defaultModeByPersona = @{
    "core" = "standard"
    "writer-curator" = "coach"
    "research-synthesizer" = "strategist"
}

$requestedModesGlobal = @()
if ($UseAllModes) {
    $requestedModesGlobal = @($availableModes | Sort-Object -Unique)
}
elseif ($Modes -and $Modes.Count -gt 0) {
    $requestedModesGlobal = @(
        $Modes |
        ForEach-Object { "$_" -split "[,;]" } |
        ForEach-Object { "$_".Trim() } |
        Where-Object { $_ } |
        Sort-Object -Unique
    )
}

$modeMatrixByPersona = @{}
foreach ($personaName in $Personas) {
    $personaNameTrim = "$personaName".Trim()
    if (-not $personaNameTrim) {
        continue
    }
    if ($requestedModesGlobal.Count -gt 0) {
        $modeMatrixByPersona[$personaNameTrim] = @($requestedModesGlobal)
    }
    elseif ($defaultModeByPersona.ContainsKey($personaNameTrim)) {
        $modeMatrixByPersona[$personaNameTrim] = @([string]$defaultModeByPersona[$personaNameTrim])
    }
    else {
        $modeMatrixByPersona[$personaNameTrim] = @("standard")
    }
}

$summary.modes_used = @(
    $modeMatrixByPersona.Values |
    ForEach-Object { $_ } |
    Sort-Object -Unique
)

if ($availableModes.Count -gt 0) {
    $unknownModes = @($summary.modes_used | Where-Object { $_ -notin $availableModes })
    if ($unknownModes.Count -gt 0) {
        $summary.persona_checks += [ordered]@{
            persona = ""
            requested_mode = ($unknownModes -join ",")
            resolved_mode = ""
            resolved_source = ""
            llm_profile = ""
            degraded = $true
            session_path_exists = $false
            ok = $false
            note = "unknown_modes_in_request"
        }
        $summary.overall_ok = $false
    }
}

foreach ($persona in $Personas) {
    $personaId = "$persona".Trim()
    if (-not $personaId) {
        continue
    }
    if ($personaRegistry -and -not $personaRegistry.PSObject.Properties.Name.Contains($personaId)) {
        $personaModes = @($modeMatrixByPersona[$personaId])
        foreach ($requestedMode in $personaModes) {
            $summary.total_cases += 1
            $summary.persona_checks += [ordered]@{
                persona = $personaId
                requested_mode = $requestedMode
                resolved_mode = ""
                resolved_source = ""
                llm_profile = ""
                degraded = $true
                session_path_exists = $false
                ok = $false
                note = "persona_missing_in_registry"
            }
        }
        $summary.overall_ok = $false
        continue
    }

    $personaModes = @($modeMatrixByPersona[$personaId])
    foreach ($requestedMode in $personaModes) {
        $summary.total_cases += 1

        $resolveStep = Invoke-MemoryCliStep -StepName "mode-resolve-$personaId-$requestedMode" -CommandArgs @(
            "dialogue-mode-show",
            "--resolve",
            "--persona", $personaId,
            "--transport", $Transport,
            "--mode", $requestedMode,
            "--json"
        )
        $summary.steps += $resolveStep
        if (-not $resolveStep.ok) {
            $summary.overall_ok = $false
        }

        $resolvePayload = Parse-StepJson -Step $resolveStep
        $resolvedMode = ""
        $resolvedSource = ""
        if ($resolvePayload -and $resolvePayload.resolved) {
            $resolvedMode = [string]$resolvePayload.resolved.mode
            $resolvedSource = [string]$resolvePayload.resolved.source
        }
        $resolveModeMatch = ($resolvedMode -eq $requestedMode)

        $token = "persona_matrix_" + $runId + "_" + $personaId + "_" + $requestedMode
        $message = "token_$token"
        $sessionId = "persona-matrix-$runId-$personaId-$requestedMode"
        $channelId = "$Transport-$personaId-$requestedMode"
        $chatArgs = @(
            "chat",
            $message,
            "--persona", $personaId,
            "--transport", $Transport,
            "--channel-id", $channelId,
            "--context", "persona-matrix",
            "--session", $sessionId,
            "--mode", $requestedMode,
            "--allow-llm-degraded"
        )
        if ($RequireNonDegraded) {
            $chatArgs += "--require-nondegraded"
        }
        $chatArgs += "--json"
        $chatStep = Invoke-MemoryCliStep -StepName "chat-$personaId-$requestedMode" -CommandArgs $chatArgs
        $summary.steps += $chatStep
        if (-not $chatStep.ok) {
            $summary.overall_ok = $false
        }

        $chatPayload = Parse-StepJson -Step $chatStep
        $llmProfile = ""
        $degraded = $true
        $sessionExists = $false
        $chatMode = ""
        $routeEventExists = $false
        if ($chatPayload) {
            if ($chatPayload.llm) {
                $llmProfile = [string]$chatPayload.llm.profile
            }
            $degraded = [bool]$chatPayload.degraded
            if ($chatPayload.dialogue_mode) {
                $chatMode = [string]$chatPayload.dialogue_mode.mode
            }
            $routeEventExists = ($null -ne $chatPayload.llm_route_event)
            $sessionPath = ""
            if ($chatPayload.memory_paths) {
                $sessionPath = [string]$chatPayload.memory_paths.session
            }
            if ($sessionPath) {
                $sessionAbs = Join-Path $VaultRoot $sessionPath
                $sessionExists = Test-Path -LiteralPath $sessionAbs
            }
        }
        else {
            $summary.overall_ok = $false
        }

        $degradedOk = $true
        if ($RequireNonDegraded) {
            $degradedOk = -not $degraded
        }
        $chatModeMatch = ($chatMode -eq $requestedMode)
        $rowOk = ($resolveStep.ok -and $chatStep.ok -and $resolveModeMatch -and $chatModeMatch -and $sessionExists -and $routeEventExists -and $degradedOk)
        if ($rowOk) {
            $summary.passed_cases += 1
        }
        else {
            $summary.overall_ok = $false
        }

        $notes = New-Object System.Collections.Generic.List[string]
        if (-not $resolveModeMatch) { $notes.Add("resolve_mode_mismatch:$resolvedMode") | Out-Null }
        if (-not $chatModeMatch) { $notes.Add("chat_mode_mismatch:$chatMode") | Out-Null }
        if (-not $sessionExists) { $notes.Add("session_missing") | Out-Null }
        if (-not $routeEventExists) { $notes.Add("llm_route_event_missing") | Out-Null }
        if ($RequireNonDegraded -and $degraded) { $notes.Add("degraded=true") | Out-Null }
        if ($notes.Count -eq 0) { $notes.Add("ok") | Out-Null }

        $summary.persona_checks += [ordered]@{
            persona = $personaId
            requested_mode = $requestedMode
            resolved_mode = $resolvedMode
            resolved_source = $resolvedSource
            llm_profile = $llmProfile
            degraded = $degraded
            session_path_exists = $sessionExists
            ok = $rowOk
            note = ($notes -join ";")
        }
    }
}

if (-not $SkipPortability) {
    $portabilityPersona = "writer-curator"
    if (-not ($Personas -contains $portabilityPersona)) {
        $portabilityPersona = "core"
    }

    $packStep = Invoke-MemoryCliStep -StepName "persona-pack" -CommandArgs @(
        "persona-pack",
        "--persona", $portabilityPersona,
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
    if (-not $bundlePath) {
        $summary.overall_ok = $false
    }

    $tempVault = Join-Path (Split-Path -Parent $VaultRoot) "_tmp_persona_matrix_portability"
    if (Test-Path -LiteralPath $tempVault) {
        Remove-Item -LiteralPath $tempVault -Recurse -Force
    }

    $shellOut = Join-Path $runDir "portability-shell.stdout.log"
    $shellErr = Join-Path $runDir "portability-shell.stderr.log"
    $shellArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $tempVault, "brain-shell", "--owner-id", "matrix", "--json")
    $shellProc = Start-Process -FilePath $PythonExe -ArgumentList $shellArgs -Wait -PassThru -NoNewWindow -RedirectStandardOutput $shellOut -RedirectStandardError $shellErr
    $shellStep = [ordered]@{
        name = "portability-brain-shell"
        command = "$PythonExe $($shellArgs -join ' ')"
        exit_code = $shellProc.ExitCode
        ok = ($shellProc.ExitCode -eq 0)
        stdout = (Resolve-Path $shellOut).Path
        stderr = (Resolve-Path $shellErr).Path
    }
    $summary.steps += $shellStep
    if (-not $shellStep.ok) {
        $summary.overall_ok = $false
    }

    if ($bundlePath) {
        $unpackOut = Join-Path $runDir "portability-unpack.stdout.log"
        $unpackErr = Join-Path $runDir "portability-unpack.stderr.log"
        $unpackArgs = @(
            "-X", "utf8", "-m", "agent_memory.cli",
            "--vault-root", $tempVault,
            "persona-unpack",
            "--package", $bundlePath,
            "--json"
        )
        $unpackProc = Start-Process -FilePath $PythonExe -ArgumentList $unpackArgs -Wait -PassThru -NoNewWindow -RedirectStandardOutput $unpackOut -RedirectStandardError $unpackErr
        $unpackStep = [ordered]@{
            name = "portability-unpack"
            command = "$PythonExe $($unpackArgs -join ' ')"
            exit_code = $unpackProc.ExitCode
            ok = ($unpackProc.ExitCode -eq 0)
            stdout = (Resolve-Path $unpackOut).Path
            stderr = (Resolve-Path $unpackErr).Path
        }
        $summary.steps += $unpackStep
        if (-not $unpackStep.ok) {
            $summary.overall_ok = $false
        }
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Persona collaboration matrix completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Persona collaboration matrix has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

