param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$OutputRoot = "artifacts/phase3_mainline_runs",
    [string]$Persona = "core",
    [string]$Transport = "web",
    [string]$DialogueMode = "standard",
    [switch]$SkipLlmProbe
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

$runId = "phase3-mainline-" + (Get-Date -Format "yyyyMMdd-HHmmss")
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
    $lines += "# Phase3 Mainline Gate Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- llm_probe_skipped: ``$($Payload.llm_probe_skipped)``"
    $lines += ""
    $probe = $Payload.llm_probe
    if ($probe) {
        $lines += "## LLM Probe"
        $lines += ""
        $lines += "- persona: ``$($probe.persona)``"
        $lines += "- transport: ``$($probe.transport)``"
        $lines += "- mode: ``$($probe.dialogue_mode)``"
        $lines += "- profile: ``$($probe.llm_profile)``"
        $lines += "- model: ``$($probe.llm_model)``"
        $lines += "- degraded: ``$($probe.degraded)``"
        $lines += "- session_exists: ``$($probe.session_path_exists)``"
        if ($probe.session_path) {
            $lines += "- session_path: ``$($probe.session_path)``"
        }
        $lines += ""
    }
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
    llm_probe_skipped = [bool]$SkipLlmProbe
    llm_probe = [ordered]@{
        persona = $Persona
        transport = $Transport
        dialogue_mode = $DialogueMode
        llm_profile = ""
        llm_model = ""
        degraded = $true
        session_path_exists = $false
        session_path = ""
    }
    steps = @()
}

Write-Host "[INFO] Phase3 mainline run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$smokeStep = Invoke-MemoryCliStep -StepName "smoke-test" -CommandArgs @("smoke-test", "--json")
$summary.steps += $smokeStep
if (-not $smokeStep.ok) {
    $summary.overall_ok = $false
}

$coreStep = Invoke-MemoryCliStep -StepName "core-e2e" -CommandArgs @("core-e2e", "--json")
$summary.steps += $coreStep
if (-not $coreStep.ok) {
    $summary.overall_ok = $false
}

if (-not $SkipLlmProbe) {
    $probeSession = "$runId-core"
    $probeMessage = "phase3_mainline_probe_$runId"
    $chatStep = Invoke-MemoryCliStep -StepName "mainline-llm-probe" -CommandArgs @(
        "chat",
        $probeMessage,
        "--persona", $Persona,
        "--transport", $Transport,
        "--context", "phase3-mainline",
        "--session", $probeSession,
        "--mode", $DialogueMode,
        "--allow-llm-degraded",
        "--require-nondegraded",
        "--json"
    )
    $summary.steps += $chatStep
    if (-not $chatStep.ok) {
        $summary.overall_ok = $false
    }
    $chatPayload = Parse-StepJson -Step $chatStep
    if (-not $chatPayload) {
        $summary.overall_ok = $false
    }
    else {
        $probe = $summary.llm_probe
        $probe.llm_profile = [string]$chatPayload.llm.profile
        $probe.llm_model = [string]$chatPayload.llm.model
        $probe.degraded = [bool]$chatPayload.degraded
        $probe.session_path = [string]$chatPayload.memory_paths.session
        if ($probe.session_path) {
            $sessionAbs = Join-Path $VaultRoot $probe.session_path
            $probe.session_path_exists = Test-Path -LiteralPath $sessionAbs
        }
        if ($probe.degraded -or -not $probe.session_path_exists) {
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
    Write-Host "[OK] Phase3 mainline gate completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Phase3 mainline gate has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

