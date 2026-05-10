param(
    [string]$PythonExe = "python",
    [string]$CasesFile = "scripts/phase5-promotion-cases.sample.json",
    [string]$OutputRoot = "artifacts/phase5_promotion_case_matrix_runs",
    [double]$DefaultMinScore = 0.0,
    [int]$DefaultMinRecall = 2,
    [int]$DefaultMinQueries = 2,
    [int]$DefaultMinDays = 1,
    [double]$DefaultGraceHours = 0.0,
    [int]$DefaultMaxPromotions = 5,
    [string]$VaultRoot = "",
    [switch]$UseMainVault
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

$casesPath = Resolve-Path -Path $CasesFile -ErrorAction Stop
$casesRaw = Get-Content -Path $casesPath.Path -Raw -Encoding UTF8
$casesConfig = $casesRaw | ConvertFrom-Json
if ($null -eq $casesConfig -or $null -eq $casesConfig.cases) {
    throw "Invalid cases file: missing `cases` array -> $($casesPath.Path)"
}
$cases = @($casesConfig.cases)
if ($cases.Count -eq 0) {
    throw "No cases found in $($casesPath.Path)"
}

$runId = "phase5-promotion-matrix-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$runDir = Join-Path $projectRoot (Join-Path $OutputRoot $runId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$caseBodyDir = Join-Path $runDir "case_bodies"
New-Item -ItemType Directory -Path $caseBodyDir -Force | Out-Null

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

function Normalize-CaseId {
    param(
        [string]$Value,
        [int]$Index
    )
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return ("case-" + $Index)
    }
    $normalized = $text.ToLowerInvariant() -replace "[^a-z0-9._-]", "-"
    $normalized = $normalized.Trim("-")
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return ("case-" + $Index)
    }
    return $normalized
}

function Resolve-ThresholdValue {
    param(
        [object]$CaseThresholds,
        [object]$Defaults,
        [string]$Name,
        [object]$Fallback
    )
    if ($CaseThresholds -and $CaseThresholds.PSObject.Properties.Name -contains $Name) {
        return $CaseThresholds.$Name
    }
    if ($Defaults -and $Defaults.PSObject.Properties.Name -contains $Name) {
        return $Defaults.$Name
    }
    return $Fallback
}

function Get-OptionalProperty {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    $prop = $Object.PSObject.Properties[$Name]
    if ($prop) {
        return $prop.Value
    }
    return $null
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
    $lines += "# Phase5 Promotion Case Matrix Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- cases_file: ``$($Payload.cases_file)``"
    $lines += "- cases_total: ``$($Payload.cases_total)``"
    $lines += "- cases_passed: ``$($Payload.cases_passed)``"
    $lines += ""
    $lines += "## Cases"
    $lines += ""
    foreach ($case in $Payload.case_results) {
        $status = if ($case.ok) { "PASS" } else { "FAIL" }
        $lines += "- [$status] ``$($case.id)`` | $($case.description)"
        $lines += "  - min_score=$($case.thresholds.min_score), min_recall=$($case.thresholds.min_recall), min_queries=$($case.thresholds.min_queries), min_days=$($case.thresholds.min_days)"
        $lines += "  - summary: ``$($case.summary_path)``"
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
    cases_file = $casesPath.Path
    cases_total = $cases.Count
    cases_passed = 0
    checks = New-Object System.Collections.ArrayList
    case_results = @()
    steps = @()
}

$defaults = Get-OptionalProperty -Object $casesConfig -Name "defaults"
$phase5Script = Join-Path $PSScriptRoot "run-phase5-promotion-gate.ps1"

Write-Host "[INFO] Phase5 promotion matrix run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan
Write-Host "[INFO] Cases file: $($casesPath.Path)" -ForegroundColor Cyan

$index = 1
foreach ($case in $cases) {
    $caseId = Normalize-CaseId -Value ([string]$case.id) -Index $index
    $caseDescription = [string]$case.description
    if ([string]::IsNullOrWhiteSpace($caseDescription)) {
        $caseDescription = "phase5 promotion case $caseId"
    }

    $caseThresholds = Get-OptionalProperty -Object $case -Name "thresholds"
    $minScore = [double](Resolve-ThresholdValue -CaseThresholds $caseThresholds -Defaults $defaults -Name "min_score" -Fallback $DefaultMinScore)
    $minRecall = [int](Resolve-ThresholdValue -CaseThresholds $caseThresholds -Defaults $defaults -Name "min_recall" -Fallback $DefaultMinRecall)
    $minQueries = [int](Resolve-ThresholdValue -CaseThresholds $caseThresholds -Defaults $defaults -Name "min_queries" -Fallback $DefaultMinQueries)
    $minDays = [int](Resolve-ThresholdValue -CaseThresholds $caseThresholds -Defaults $defaults -Name "min_days" -Fallback $DefaultMinDays)
    $graceHours = [double](Resolve-ThresholdValue -CaseThresholds $caseThresholds -Defaults $defaults -Name "grace_hours" -Fallback $DefaultGraceHours)
    $maxPromotions = [int](Resolve-ThresholdValue -CaseThresholds $caseThresholds -Defaults $defaults -Name "max_promotions" -Fallback $DefaultMaxPromotions)

    $bodyLines = @()
    $caseBodyLines = Get-OptionalProperty -Object $case -Name "body_lines"
    if ($caseBodyLines) {
        foreach ($line in @($caseBodyLines)) {
            $bodyLines += [string]$line
        }
    }
    $caseBody = ""
    if ($bodyLines.Count -gt 0) {
        $caseBody = ($bodyLines -join [Environment]::NewLine)
    }
    else {
        $caseBodyRaw = [string](Get-OptionalProperty -Object $case -Name "body")
        if (-not [string]::IsNullOrWhiteSpace($caseBodyRaw)) {
            $caseBody = $caseBodyRaw
        }
    }
    if ([string]::IsNullOrWhiteSpace($caseBody)) {
        $caseBody = @"
# phase5 promotion matrix

- case_id: $caseId
- description: $caseDescription
"@
    }
    $caseBodyPath = Join-Path $caseBodyDir ($caseId + ".md")
    Set-Content -Path $caseBodyPath -Value $caseBody -Encoding UTF8

    $queries = @()
    $caseQueries = Get-OptionalProperty -Object $case -Name "queries"
    if ($caseQueries) {
        foreach ($query in @($caseQueries)) {
            $text = [string]$query
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $queries += $text.Trim()
            }
        }
    }
    if ($queries.Count -eq 0) {
        $queries += "phase5-$caseId-query-a"
        $queries += "phase5-$caseId-query-b"
    }

    $scriptArgs = @(
        "-PythonExe", $PythonExe,
        "-MinScore", "$minScore",
        "-MinRecall", "$minRecall",
        "-MinQueries", "$minQueries",
        "-MinDays", "$minDays",
        "-GraceHours", "$graceHours",
        "-MaxPromotions", "$maxPromotions",
        "-CaseId", $caseId,
        "-CandidateBodyFile", $caseBodyPath
    )
    if ($queries.Count -gt 0) {
        $scriptArgs += @("-SearchQueries", [string]::Join(",", $queries))
    }
    if ($UseMainVault) {
        $scriptArgs += "-UseMainVault"
        if (-not [string]::IsNullOrWhiteSpace($VaultRoot)) {
            $scriptArgs += @("-VaultRoot", $VaultRoot)
        }
    }

    $stepName = "case-$('{0:d2}' -f $index)-$caseId"
    $caseStep = Invoke-PowerShellScriptStep -StepName $stepName -ScriptPath $phase5Script -ScriptArgs $scriptArgs
    $summary.steps += $caseStep

    $childSummaryPath = Extract-ChildSummaryPath -Step $caseStep
    $childOk = $false
    if (-not [string]::IsNullOrWhiteSpace($childSummaryPath) -and (Test-Path -LiteralPath $childSummaryPath)) {
        try {
            $childPayload = Get-Content -Path $childSummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $childOk = [bool]$childPayload.overall_ok
        }
        catch {
            $childOk = $false
        }
    }
    $caseOk = [bool]$caseStep.ok -and $childOk
    if ($caseOk) {
        $summary.cases_passed += 1
    }
    else {
        $summary.overall_ok = $false
    }

    Add-Check -Checks $summary.checks -Name ("case_step_ok_" + $caseId) -Ok ([bool]$caseStep.ok) -Detail ("exit_code=" + $caseStep.exit_code)
    Add-Check -Checks $summary.checks -Name ("case_summary_ok_" + $caseId) -Ok $childOk -Detail $childSummaryPath

    $summary.case_results += [ordered]@{
        id = $caseId
        description = $caseDescription
        ok = $caseOk
        summary_path = $childSummaryPath
        thresholds = [ordered]@{
            min_score = $minScore
            min_recall = $minRecall
            min_queries = $minQueries
            min_days = $minDays
            grace_hours = $graceHours
            max_promotions = $maxPromotions
        }
        queries = $queries
    }

    $index += 1
}

Add-Check -Checks $summary.checks -Name "matrix_all_cases_pass" -Ok ($summary.cases_passed -eq $summary.cases_total) -Detail ("passed=" + $summary.cases_passed + "/" + $summary.cases_total)
if ($summary.cases_passed -ne $summary.cases_total) {
    $summary.overall_ok = $false
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Phase5 promotion case matrix completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Phase5 promotion case matrix has failures: $summaryJsonPath" -ForegroundColor Red
exit 1
