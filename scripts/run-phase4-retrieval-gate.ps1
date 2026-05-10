param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$CasesFile = "00_System/08_Runtime_Profiles/retrieval_benchmark_cases_phase4.yaml",
    [string]$TargetVariant = "hybrid_mmr_off",
    [double]$MinAnyPathHitRate = 0.20,
    [double]$MinTop1PathHitRate = 0.20,
    [double]$MinKeywordHitRate = 0.95,
    [double]$MaxAvgLatencyMs = 120.0,
    [switch]$SkipCompareFts,
    [string]$OutputRoot = "artifacts/phase4_retrieval_runs"
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

$runId = "phase4-retrieval-" + (Get-Date -Format "yyyyMMdd-HHmmss")
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
    $lines += "# Phase4 Retrieval Gate Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- cases_file: ``$($Payload.cases_file)``"
    $lines += "- target_variant: ``$($Payload.target_variant)``"
    $lines += "- benchmark_recommended: ``$($Payload.benchmark_recommended)``"
    $lines += ""
    $lines += "## Target Summary"
    $lines += ""
    $target = $Payload.target_summary
    if ($target) {
        $lines += "- cases: ``$($target.cases)``"
        $lines += "- avg_latency_ms: ``$($target.avg_latency_ms)``"
        $lines += "- top1_path_hit_rate: ``$($target.top1_path_hit_rate)``"
        $lines += "- any_path_hit_rate: ``$($target.any_path_hit_rate)``"
        $lines += "- keyword_hit_rate: ``$($target.keyword_hit_rate)``"
    }
    else {
        $lines += "- (target summary missing)"
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
    vault_root = $VaultRoot
    cases_file = $CasesFile
    target_variant = $TargetVariant
    benchmark_recommended = ""
    target_summary = $null
    fts_summary = $null
    thresholds = [ordered]@{
        min_any_path_hit_rate = $MinAnyPathHitRate
        min_top1_path_hit_rate = $MinTop1PathHitRate
        min_keyword_hit_rate = $MinKeywordHitRate
        max_avg_latency_ms = $MaxAvgLatencyMs
    }
    checks = New-Object System.Collections.ArrayList
    steps = @()
}

Write-Host "[INFO] Phase4 retrieval run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

$showStep = Invoke-MemoryCliStep -StepName "retrieval-show" -CommandArgs @("retrieval-show", "--json")
$summary.steps += $showStep
if (-not $showStep.ok) {
    $summary.overall_ok = $false
}

$benchStep = Invoke-MemoryCliStep -StepName "retrieval-benchmark" -CommandArgs @(
    "retrieval-benchmark",
    "--cases-file", $CasesFile,
    "--json"
)
$summary.steps += $benchStep
if (-not $benchStep.ok) {
    $summary.overall_ok = $false
}

$benchPayload = Parse-StepJson -Step $benchStep
if (-not $benchPayload) {
    Add-Check -Checks $summary.checks -Name "benchmark_json" -Ok $false -Detail "benchmark output is not valid json"
    $summary.overall_ok = $false
}
else {
    $summary.benchmark_recommended = [string]$benchPayload.recommended
    $variants = $benchPayload.variants
    if (-not $variants) {
        Add-Check -Checks $summary.checks -Name "variants_exist" -Ok $false -Detail "no variants in benchmark payload"
        $summary.overall_ok = $false
    }
    else {
        $targetRow = $variants | Where-Object { [string]$_.variant -eq $TargetVariant } | Select-Object -First 1
        $ftsRow = $variants | Where-Object { [string]$_.variant -eq "fts_only" } | Select-Object -First 1
        if (-not $targetRow) {
            Add-Check -Checks $summary.checks -Name "target_variant_exists" -Ok $false -Detail "target variant not found: $TargetVariant"
            $summary.overall_ok = $false
        }
        else {
            $targetSummary = $targetRow.summary
            $summary.target_summary = $targetSummary
            if ($ftsRow) {
                $summary.fts_summary = $ftsRow.summary
            }

            $anyRate = [double]$targetSummary.any_path_hit_rate
            $top1Rate = [double]$targetSummary.top1_path_hit_rate
            $keywordRate = [double]$targetSummary.keyword_hit_rate
            $avgLatency = [double]$targetSummary.avg_latency_ms
            $casesCount = [int]$targetSummary.cases

            $okCases = ($casesCount -ge 10)
            Add-Check -Checks $summary.checks -Name "cases_count" -Ok $okCases -Detail "cases=$casesCount (expected >=10)"
            if (-not $okCases) { $summary.overall_ok = $false }

            $okAny = ($anyRate -ge $MinAnyPathHitRate)
            Add-Check -Checks $summary.checks -Name "any_path_hit_rate" -Ok $okAny -Detail "actual=$anyRate threshold>=$MinAnyPathHitRate"
            if (-not $okAny) { $summary.overall_ok = $false }

            $okTop1 = ($top1Rate -ge $MinTop1PathHitRate)
            Add-Check -Checks $summary.checks -Name "top1_path_hit_rate" -Ok $okTop1 -Detail "actual=$top1Rate threshold>=$MinTop1PathHitRate"
            if (-not $okTop1) { $summary.overall_ok = $false }

            $okKeyword = ($keywordRate -ge $MinKeywordHitRate)
            Add-Check -Checks $summary.checks -Name "keyword_hit_rate" -Ok $okKeyword -Detail "actual=$keywordRate threshold>=$MinKeywordHitRate"
            if (-not $okKeyword) { $summary.overall_ok = $false }

            $okLatency = ($avgLatency -le $MaxAvgLatencyMs)
            Add-Check -Checks $summary.checks -Name "avg_latency_ms" -Ok $okLatency -Detail "actual=$avgLatency threshold<=$MaxAvgLatencyMs"
            if (-not $okLatency) { $summary.overall_ok = $false }

            if (-not $SkipCompareFts) {
                if ($ftsRow) {
                    $ftsAny = [double]$ftsRow.summary.any_path_hit_rate
                    $okCompare = ($anyRate -ge $ftsAny)
                    Add-Check -Checks $summary.checks -Name "beats_or_equals_fts_any_path_hit" -Ok $okCompare -Detail "target=$anyRate fts=$ftsAny"
                    if (-not $okCompare) { $summary.overall_ok = $false }
                }
                else {
                    Add-Check -Checks $summary.checks -Name "beats_or_equals_fts_any_path_hit" -Ok $false -Detail "fts_only variant missing"
                    $summary.overall_ok = $false
                }
            }
        }
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 10 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Phase4 retrieval gate completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Phase4 retrieval gate has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

