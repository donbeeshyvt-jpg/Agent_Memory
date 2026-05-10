param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$OutputRoot = "artifacts/transport_role_sim_runs",
    [switch]$RequireNonDegraded,
    [switch]$KeepBindings
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

$runId = "transport-role-sim-" + (Get-Date -Format "yyyyMMdd-HHmmss")
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

function Write-MarkdownSummary {
    param(
        [hashtable]$Payload,
        [string]$Path
    )

    $lines = @()
    $lines += "# Transport Role Simulation Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- require_nondegraded: ``$($Payload.require_nondegraded)``"
    $lines += "- keep_bindings: ``$($Payload.keep_bindings)``"
    $lines += ""
    $lines += "## Case Checks"
    $lines += ""
    foreach ($row in $Payload.case_checks) {
        $lines += (
            "- case={0} | transport={1} | expected={2} | actual={3} | degraded={4} | binding={5} | session={6} | ok={7}" -f
            $row.case_id,
            $row.transport,
            $row.expected_persona,
            $row.actual_persona,
            $row.degraded,
            $row.resolved_by_binding,
            $row.session_path_exists,
            $row.ok
        )
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
    require_nondegraded = [bool]$RequireNonDegraded
    keep_bindings = [bool]$KeepBindings
    case_checks = @()
    steps = @()
}

Write-Host "[INFO] Transport role sim run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$bindingsPath = Join-Path $VaultRoot "00_System/08_Runtime_Profiles/channel_bindings.yaml"
$bindingsBackup = Join-Path $runDir "channel_bindings.before.yaml"
$hadBindingsFile = Test-Path -LiteralPath $bindingsPath
if ($hadBindingsFile) {
    Copy-Item -LiteralPath $bindingsPath -Destination $bindingsBackup -Force
}

try {
    $personaStep = Invoke-MemoryCliStep -StepName "persona-list" -CommandArgs @("persona-list", "--json")
    $summary.steps += $personaStep
    if (-not $personaStep.ok) {
        $summary.overall_ok = $false
    }

    $personaPayload = Parse-StepJson -Step $personaStep
    $personaRegistry = @{}
    if ($personaPayload -and $personaPayload.personas) {
        $personaRegistry = $personaPayload.personas
    }

    $cases = @(
        @{
            case_id = "web-core"
            transport = "web"
            channel_id = "web-main"
            expected_persona = "core"
            payload = @{
                message = "transport role sim web core"
                channel_id = "web-main"
                user_id = "web-user-1"
            }
        },
        @{
            case_id = "discord-writer"
            transport = "discord"
            channel_id = "guild-main"
            expected_persona = "writer-curator"
            payload = @{
                content = "transport role sim discord writer"
                channel_id = "guild-main"
                author = @{ id = "discord-user-1" }
            }
        },
        @{
            case_id = "line-research"
            transport = "line"
            channel_id = "group-demo"
            expected_persona = "research-synthesizer"
            payload = @{
                events = @(
                    @{
                        type = "message"
                        message = @{
                            type = "text"
                            text = "transport role sim line research"
                        }
                        source = @{
                            type = "group"
                            groupId = "group-demo"
                            userId = "line-user-1"
                        }
                    }
                )
            }
        }
    )

    foreach ($row in $cases) {
        $personaId = [string]$row.expected_persona
        if ($personaRegistry -and -not $personaRegistry.PSObject.Properties.Name.Contains($personaId)) {
            $summary.case_checks += [ordered]@{
                case_id = [string]$row.case_id
                transport = [string]$row.transport
                expected_persona = $personaId
                actual_persona = ""
                degraded = $true
                resolved_by_binding = $false
                session_path_exists = $false
                ok = $false
                note = "persona_missing_in_registry"
            }
            $summary.overall_ok = $false
            continue
        }

        $bindStep = Invoke-MemoryCliStep -StepName ("bind-" + [string]$row.case_id) -CommandArgs @(
            "channel-bind",
            "--transport", [string]$row.transport,
            "--channel-id", [string]$row.channel_id,
            "--persona", $personaId,
            "--operator", "transport-role-sim",
            "--json"
        )
        $summary.steps += $bindStep
        if (-not $bindStep.ok) {
            $summary.overall_ok = $false
            continue
        }

        $payloadFile = Join-Path $runDir ([string]$row.case_id + ".payload.json")
        ($row.payload | ConvertTo-Json -Depth 8) | Set-Content -Path $payloadFile -Encoding UTF8

        $ingestArgs = @(
            "transport-ingest",
            "--transport", [string]$row.transport,
            "--payload-file", $payloadFile,
            "--allow-llm-degraded",
            "--json"
        )
        if ($RequireNonDegraded) {
            $ingestArgs += "--require-nondegraded"
        }

        $ingestStep = Invoke-MemoryCliStep -StepName ("ingest-" + [string]$row.case_id) -CommandArgs $ingestArgs
        $summary.steps += $ingestStep
        if (-not $ingestStep.ok) {
            $summary.overall_ok = $false
        }

        $ingestPayload = Parse-StepJson -Step $ingestStep
        if (-not $ingestPayload) {
            $summary.case_checks += [ordered]@{
                case_id = [string]$row.case_id
                transport = [string]$row.transport
                expected_persona = $personaId
                actual_persona = ""
                degraded = $true
                resolved_by_binding = $false
                session_path_exists = $false
                ok = $false
                note = "invalid_json_response"
            }
            $summary.overall_ok = $false
            continue
        }

        $actualPersona = [string]$ingestPayload.persona
        $degraded = [bool]$ingestPayload.degraded
        $resolvedByBinding = [bool]$ingestPayload.resolved_by_binding
        $sessionExists = $false
        $sessionPath = [string]$ingestPayload.memory_paths.session
        if ($sessionPath) {
            $sessionAbs = Join-Path $VaultRoot $sessionPath
            $sessionExists = Test-Path -LiteralPath $sessionAbs
        }
        $rowOk = ($actualPersona -eq $personaId) -and $resolvedByBinding -and $sessionExists
        if ($RequireNonDegraded) {
            $rowOk = $rowOk -and (-not $degraded)
        }
        if (-not $rowOk) {
            $summary.overall_ok = $false
        }

        $summary.case_checks += [ordered]@{
            case_id = [string]$row.case_id
            transport = [string]$row.transport
            expected_persona = $personaId
            actual_persona = $actualPersona
            degraded = $degraded
            resolved_by_binding = $resolvedByBinding
            session_path_exists = $sessionExists
            ok = $rowOk
            note = ""
        }
    }
}
finally {
    if (-not $KeepBindings) {
        if (Test-Path -LiteralPath $bindingsBackup) {
            Copy-Item -LiteralPath $bindingsBackup -Destination $bindingsPath -Force
        }
        elseif ((Test-Path -LiteralPath $bindingsPath) -and (-not $hadBindingsFile)) {
            Remove-Item -LiteralPath $bindingsPath -Force
        }
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Transport role simulation completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Transport role simulation has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

