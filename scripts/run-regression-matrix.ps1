param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 16000,
    [string]$CasesFile = "00_System/08_Runtime_Profiles/retrieval_benchmark_cases.yaml",
    [string]$OutputRoot = "artifacts/regression_runs",
    [switch]$SmokeWithWeb,
    [switch]$SmokeWithLLM,
    [switch]$SkipNotionQueue,
    [switch]$SkipBridge,
    [switch]$SkipPersonaModeMatrix,
    [switch]$PersonaModeIncludePortability,
    [string]$PersonaModeTransport = "web",
    [string[]]$PersonaModes = @(),
    [switch]$RunPhase4RetrievalGate,
    [switch]$RunProviderAbGate,
    [switch]$RequireProviderEffective,
    [string]$ProviderProfile = "openai",
    [string]$ProviderModel = "text-embedding-3-small",
    [double]$ProviderTimeout = 20.0,
    [switch]$RunPhase5PromotionGate,
    [switch]$RunPhase5PromotionDod,
    [switch]$RunBrainSwapGate,
    [string]$BrainSwapPackPersona = "writer-curator",
    [bool]$BrainSwapRequireNonDegraded = $false,
    [switch]$BrainSwapCleanupTargetVault
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found: ${PythonExe}. Install Python or pass -PythonExe with a valid path."
}
$powershellCmd = Get-Command powershell -ErrorAction SilentlyContinue
if (-not $powershellCmd) {
    throw "PowerShell executable not found. Ensure powershell is available in PATH."
}
$powershellExe = $powershellCmd.Source

if (-not $VaultRoot) {
    $defaultVault = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
    $VaultRoot = (Resolve-Path $defaultVault).Path
}

$runId = "regression-" + (Get-Date -Format "yyyyMMdd-HHmmss")
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

function Invoke-PowerShellScriptStep {
    param(
        [string]$StepName,
        [string]$ScriptPath,
        [string[]]$ScriptArgs
    )

    $stdoutPath = Join-Path $runDir "$StepName.stdout.log"
    $stderrPath = Join-Path $runDir "$StepName.stderr.log"
    $allArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath) + $ScriptArgs

    $proc = Start-Process `
        -FilePath $powershellExe `
        -ArgumentList $allArgs `
        -Wait `
        -PassThru `
        -NoNewWindow `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath
    $exitCode = $proc.ExitCode
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
        command = "$powershellExe $($allArgs -join ' ')"
        exit_code = $exitCode
        ok = ($exitCode -eq 0)
        stdout = $stdoutResolved
        stderr = $stderrResolved
    }
}

function Extract-ChildSummaryPath {
    param(
        [hashtable]$Step
    )
    if (-not $Step) {
        return ""
    }
    if (-not (Test-Path -LiteralPath $Step.stdout)) {
        return ""
    }
    $raw = Get-Content -Path $Step.stdout -Raw -Encoding UTF8
    if (-not $raw) {
        return ""
    }
    $match = [regex]::Match($raw, "([A-Za-z]:\\[^\s\r\n]*summary\.json)")
    if ($match.Success) {
        return $match.Groups[1].Value
    }
    return ""
}

function Write-MarkdownSummary {
    param(
        [hashtable]$Payload,
        [string]$Path
    )

    $lines = @()
    $lines += "# Regression Matrix Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- notion_queue_skipped: ``$($Payload.notion_queue_skipped)``"
    $lines += "- bridge_skipped: ``$($Payload.bridge_skipped)``"
    $lines += "- persona_mode_matrix_skipped: ``$($Payload.persona_mode_matrix_skipped)``"
    if ($Payload.persona_mode_matrix_summary) {
        $lines += "- persona_mode_matrix_summary: ``$($Payload.persona_mode_matrix_summary)``"
    }
    $lines += "- phase4_retrieval_gate_enabled: ``$($Payload.phase4_retrieval_gate_enabled)``"
    if ($Payload.phase4_retrieval_gate_summary) {
        $lines += "- phase4_retrieval_gate_summary: ``$($Payload.phase4_retrieval_gate_summary)``"
    }
    $lines += "- provider_ab_gate_enabled: ``$($Payload.provider_ab_gate_enabled)``"
    if ($Payload.provider_ab_gate_summary) {
        $lines += "- provider_ab_gate_summary: ``$($Payload.provider_ab_gate_summary)``"
    }
    $lines += "- phase5_promotion_gate_enabled: ``$($Payload.phase5_promotion_gate_enabled)``"
    if ($Payload.phase5_promotion_gate_summary) {
        $lines += "- phase5_promotion_gate_summary: ``$($Payload.phase5_promotion_gate_summary)``"
    }
    $lines += "- phase5_promotion_dod_enabled: ``$($Payload.phase5_promotion_dod_enabled)``"
    if ($Payload.phase5_promotion_dod_summary) {
        $lines += "- phase5_promotion_dod_summary: ``$($Payload.phase5_promotion_dod_summary)``"
    }
    $lines += "- brain_swap_gate_enabled: ``$($Payload.brain_swap_gate_enabled)``"
    if ($Payload.brain_swap_gate_summary) {
        $lines += "- brain_swap_gate_summary: ``$($Payload.brain_swap_gate_summary)``"
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
    cases_file = $CasesFile
    notion_queue_skipped = [bool]$SkipNotionQueue
    bridge_skipped = [bool]$SkipBridge
    persona_mode_matrix_skipped = [bool]$SkipPersonaModeMatrix
    persona_mode_matrix_summary = ""
    phase4_retrieval_gate_enabled = [bool]$RunPhase4RetrievalGate
    phase4_retrieval_gate_summary = ""
    provider_ab_gate_enabled = [bool]$RunProviderAbGate
    provider_ab_gate_summary = ""
    phase5_promotion_gate_enabled = [bool]$RunPhase5PromotionGate
    phase5_promotion_gate_summary = ""
    phase5_promotion_dod_enabled = [bool]$RunPhase5PromotionDod
    phase5_promotion_dod_summary = ""
    brain_swap_gate_enabled = [bool]$RunBrainSwapGate
    brain_swap_gate_summary = ""
    steps = @()
}

Write-Host "[INFO] Regression run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$smokeArgs = @("smoke-test", "--json")
if ($SmokeWithWeb) {
    $smokeArgs += "--with-web"
}
if ($SmokeWithLLM) {
    $smokeArgs += "--with-llm"
}
$smoke = Invoke-MemoryCliStep -StepName "smoke-test" -CommandArgs $smokeArgs
$summary.steps += $smoke
if (-not $smoke.ok) {
    $summary.overall_ok = $false
}

$e2e = Invoke-MemoryCliStep -StepName "core-e2e" -CommandArgs @("core-e2e", "--json")
$summary.steps += $e2e
if (-not $e2e.ok) {
    $summary.overall_ok = $false
}

if (-not $SkipPersonaModeMatrix) {
    $personaScript = Join-Path $PSScriptRoot "run-persona-collab-matrix.ps1"
    $personaArgs = @(
        "-VaultRoot", $VaultRoot,
        "-PythonExe", $PythonExe,
        "-Transport", $PersonaModeTransport,
        "-RequireNonDegraded"
    )
    $normalizedModes = @(
        $PersonaModes |
        ForEach-Object { "$_".Trim() } |
        Where-Object { $_ } |
        Sort-Object -Unique
    )
    if ($normalizedModes.Count -gt 0) {
        $personaArgs += "-Modes"
        $personaArgs += ($normalizedModes -join ",")
    }
    else {
        $personaArgs += "-UseAllModes"
    }
    if (-not $PersonaModeIncludePortability) {
        $personaArgs += "-SkipPortability"
    }
    $personaMatrix = Invoke-PowerShellScriptStep -StepName "persona-mode-matrix" -ScriptPath $personaScript -ScriptArgs $personaArgs
    $summary.steps += $personaMatrix
    $summary.persona_mode_matrix_summary = Extract-ChildSummaryPath -Step $personaMatrix
    if (-not $personaMatrix.ok) {
        $summary.overall_ok = $false
    }
}

if (-not $SkipNotionQueue) {
    $queueTitle = "$runId-notion-queue-probe"
    $queueBody = "regression_run_id_${runId}_matrix_probe"
    $queue = Invoke-MemoryCliStep -StepName "notion-queue" -CommandArgs @(
        "notion-queue",
        "--title", $queueTitle,
        "--body", $queueBody,
        "--tag", "regression",
        "--priority", "normal",
        "--json"
    )
    $summary.steps += $queue
    if (-not $queue.ok) {
        $summary.overall_ok = $false
    }

    $queueList = Invoke-MemoryCliStep -StepName "notion-queue-list" -CommandArgs @(
        "notion-queue-list",
        "--status", "pending",
        "--limit", "20",
        "--json"
    )
    $summary.steps += $queueList
    if (-not $queueList.ok) {
        $summary.overall_ok = $false
    }
}

$benchmark = Invoke-MemoryCliStep -StepName "retrieval-benchmark" -CommandArgs @(
    "retrieval-benchmark",
    "--cases-file", $CasesFile,
    "--json"
)
$summary.steps += $benchmark
if (-not $benchmark.ok) {
    $summary.overall_ok = $false
}

if ($RunPhase4RetrievalGate) {
    $phase4Script = Join-Path $PSScriptRoot "run-phase4-retrieval-gate.ps1"
    $phase4Args = @(
        "-VaultRoot", $VaultRoot,
        "-PythonExe", $PythonExe
    )
    $phase4Step = Invoke-PowerShellScriptStep -StepName "phase4-retrieval-gate" -ScriptPath $phase4Script -ScriptArgs $phase4Args
    $summary.steps += $phase4Step
    $summary.phase4_retrieval_gate_summary = Extract-ChildSummaryPath -Step $phase4Step
    if (-not $phase4Step.ok) {
        $summary.overall_ok = $false
    }
}

if ($RunProviderAbGate) {
    $providerScript = Join-Path $PSScriptRoot "run-phase4-provider-ab.ps1"
    $providerArgs = @(
        "-VaultRoot", $VaultRoot,
        "-PythonExe", $PythonExe,
        "-ProviderProfile", $ProviderProfile,
        "-ProviderModel", $ProviderModel,
        "-ProviderTimeout", ([string]$ProviderTimeout)
    )
    if ($RequireProviderEffective) {
        $providerArgs += "-RequireProviderEffective"
    }
    $providerStep = Invoke-PowerShellScriptStep -StepName "provider-ab-gate" -ScriptPath $providerScript -ScriptArgs $providerArgs
    $summary.steps += $providerStep
    $summary.provider_ab_gate_summary = Extract-ChildSummaryPath -Step $providerStep
    if (-not $providerStep.ok) {
        $summary.overall_ok = $false
    }
}

if ($RunPhase5PromotionGate) {
    $phase5Script = Join-Path $PSScriptRoot "run-phase5-promotion-gate.ps1"
    $phase5Args = @(
        "-PythonExe", $PythonExe
    )
    $phase5Step = Invoke-PowerShellScriptStep -StepName "phase5-promotion-gate" -ScriptPath $phase5Script -ScriptArgs $phase5Args
    $summary.steps += $phase5Step
    $summary.phase5_promotion_gate_summary = Extract-ChildSummaryPath -Step $phase5Step
    if (-not $phase5Step.ok) {
        $summary.overall_ok = $false
    }
}

if ($RunPhase5PromotionDod) {
    $phase5DodScript = Join-Path $PSScriptRoot "run-phase5-promotion-dod.ps1"
    $phase5DodArgs = @(
        "-PythonExe", $PythonExe
    )
    $phase5DodStep = Invoke-PowerShellScriptStep -StepName "phase5-promotion-dod" -ScriptPath $phase5DodScript -ScriptArgs $phase5DodArgs
    $summary.steps += $phase5DodStep
    $summary.phase5_promotion_dod_summary = Extract-ChildSummaryPath -Step $phase5DodStep
    if (-not $phase5DodStep.ok) {
        $summary.overall_ok = $false
    }
}

if ($RunBrainSwapGate) {
    $brainSwapScript = Join-Path $PSScriptRoot "run-brain-swap-gate.ps1"
    $brainSwapArgs = @(
        "-SourceVaultRoot", $VaultRoot,
        "-PythonExe", $PythonExe,
        "-PackPersona", $BrainSwapPackPersona
    )
    if ($BrainSwapRequireNonDegraded) {
        $brainSwapArgs += "-RequireNonDegraded"
    }
    if ($BrainSwapCleanupTargetVault) {
        $brainSwapArgs += "-CleanupTargetVault"
    }
    $brainSwapStep = Invoke-PowerShellScriptStep -StepName "brain-swap-gate" -ScriptPath $brainSwapScript -ScriptArgs $brainSwapArgs
    $summary.steps += $brainSwapStep
    $summary.brain_swap_gate_summary = Extract-ChildSummaryPath -Step $brainSwapStep
    if (-not $brainSwapStep.ok) {
        $summary.overall_ok = $false
    }
}

if (-not $SkipBridge) {
    $bridgeStep = [ordered]@{
        name = "bridge-samples"
        command = "run bridge + send-bridge-samples.ps1"
        exit_code = 1
        ok = $false
        stdout = ""
        stderr = ""
    }
    $bridgeStdout = Join-Path $runDir "bridge-samples.stdout.log"
    $bridgeStderr = Join-Path $runDir "bridge-samples.stderr.log"
    $bridgeProc = $null
    try {
        $bridgeArgs = @(
            "-m", "agent_memory.cli",
            "--vault-root", $VaultRoot,
            "serve-transport-bridge",
            "--host", $BindHost,
            "--port", "$Port"
        )
        $bridgeProc = Start-Process -FilePath $PythonExe -ArgumentList $bridgeArgs -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 2

        $health = Invoke-RestMethod -Method Get -Uri "http://$BindHost`:$Port/health" -TimeoutSec 8
        if ($health.ok -ne $true) {
            throw "bridge health check failed"
        }

        $sampleOutput = & "$PSScriptRoot/send-bridge-samples.ps1" -BaseUrl "http://$BindHost`:$Port" 2>&1 | Out-String
        Set-Content -Path $bridgeStdout -Value $sampleOutput -Encoding UTF8
        Set-Content -Path $bridgeStderr -Value "" -Encoding UTF8
        $bridgeStep.exit_code = 0
        $bridgeStep.ok = $true
    }
    catch {
        $errText = ($_ | Out-String)
        Set-Content -Path $bridgeStderr -Value $errText -Encoding UTF8
        if (-not (Test-Path $bridgeStdout)) {
            Set-Content -Path $bridgeStdout -Value "" -Encoding UTF8
        }
        if ($errText -match "OPENAI_API_KEY|OPENROUTER_API_KEY|GGUF") {
            $bridgeStep.exit_code = 0
            $bridgeStep.ok = $true
            $bridgeStep.command = "run bridge + send-bridge-samples.ps1 (degraded: llm unavailable)"
        }
        else {
            $bridgeStep.exit_code = 1
            $bridgeStep.ok = $false
        }
    }
    finally {
        if ($bridgeProc -and -not $bridgeProc.HasExited) {
            Stop-Process -Id $bridgeProc.Id -Force
        }
        if (Test-Path $bridgeStdout) {
            $bridgeStep.stdout = (Resolve-Path $bridgeStdout).Path
        }
        if (Test-Path $bridgeStderr) {
            $bridgeStep.stderr = (Resolve-Path $bridgeStderr).Path
        }
    }
    $summary.steps += $bridgeStep
    if (-not $bridgeStep.ok) {
        $summary.overall_ok = $false
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Regression matrix completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Regression matrix has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

