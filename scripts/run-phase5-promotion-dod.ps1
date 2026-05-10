param(
    [string]$PythonExe = "python",
    [string]$OutputRoot = "artifacts/phase5_promotion_dod_runs",
    [int]$PassMinRecall = 2,
    [int]$PassMinQueries = 2,
    [int]$FailMinRecall = 3,
    [int]$FailMinQueries = 3
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

$runId = "phase5-promotion-dod-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$runDir = Join-Path $projectRoot (Join-Path $OutputRoot $runId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

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
    $lines += "# Phase5 Promotion DoD Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- pass_thresholds: recall>=$($Payload.pass_thresholds.min_recall), queries>=$($Payload.pass_thresholds.min_queries)"
    $lines += "- fail_thresholds: recall>=$($Payload.fail_thresholds.min_recall), queries>=$($Payload.fail_thresholds.min_queries)"
    if ($Payload.pass_summary) {
        $lines += "- pass_summary: ``$($Payload.pass_summary)``"
    }
    if ($Payload.expected_fail_summary) {
        $lines += "- expected_fail_summary: ``$($Payload.expected_fail_summary)``"
    }
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
    pass_thresholds = [ordered]@{
        min_recall = $PassMinRecall
        min_queries = $PassMinQueries
    }
    fail_thresholds = [ordered]@{
        min_recall = $FailMinRecall
        min_queries = $FailMinQueries
    }
    pass_summary = ""
    expected_fail_summary = ""
    checks = New-Object System.Collections.ArrayList
    steps = @()
}

Write-Host "[INFO] Phase5 promotion DoD run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$phase5Script = Join-Path $PSScriptRoot "run-phase5-promotion-gate.ps1"

$passArgs = @(
    "-PythonExe", $PythonExe,
    "-MinRecall", "$PassMinRecall",
    "-MinQueries", "$PassMinQueries"
)
$passStep = Invoke-PowerShellScriptStep -StepName "phase5-gate-pass" -ScriptPath $phase5Script -ScriptArgs $passArgs
$summary.steps += $passStep
$summary.pass_summary = Extract-ChildSummaryPath -Step $passStep

$failArgs = @(
    "-PythonExe", $PythonExe,
    "-MinRecall", "$FailMinRecall",
    "-MinQueries", "$FailMinQueries"
)
$expectedFailStep = Invoke-PowerShellScriptStep -StepName "phase5-gate-expected-fail" -ScriptPath $phase5Script -ScriptArgs $failArgs
$summary.steps += $expectedFailStep
$summary.expected_fail_summary = Extract-ChildSummaryPath -Step $expectedFailStep

$passOk = [bool]$passStep.ok
$expectedFailTriggered = -not [bool]$expectedFailStep.ok
$passSummaryFound = -not [string]::IsNullOrWhiteSpace($summary.pass_summary)
$failSummaryFound = -not [string]::IsNullOrWhiteSpace($summary.expected_fail_summary)

Add-Check -Checks $summary.checks -Name "pass_case_succeeds" -Ok $passOk -Detail "exit_code=$($passStep.exit_code)"
Add-Check -Checks $summary.checks -Name "expected_fail_case_triggers" -Ok $expectedFailTriggered -Detail "exit_code=$($expectedFailStep.exit_code)"
Add-Check -Checks $summary.checks -Name "pass_summary_exists" -Ok $passSummaryFound -Detail $summary.pass_summary
Add-Check -Checks $summary.checks -Name "expected_fail_summary_exists" -Ok $failSummaryFound -Detail $summary.expected_fail_summary

if (-not ($passOk -and $expectedFailTriggered -and $passSummaryFound -and $failSummaryFound)) {
    $summary.overall_ok = $false
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 10 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Phase5 promotion DoD completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Phase5 promotion DoD has failures: $summaryJsonPath" -ForegroundColor Red
exit 1
