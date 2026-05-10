param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [double]$MinScore = 0.0,
    [int]$MinRecall = 2,
    [int]$MinQueries = 2,
    [int]$MinDays = 1,
    [double]$GraceHours = 0.0,
    [int]$MaxPromotions = 5,
    [string]$OutputRoot = "artifacts/phase5_promotion_runs",
    [string]$CaseId = "default",
    [string]$CandidateBodyFile = "",
    [string[]]$SearchQueries = @(),
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

$runId = "phase5-promotion-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$runDir = Join-Path $projectRoot (Join-Path $OutputRoot $runId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$isIsolated = -not $UseMainVault
if ($UseMainVault) {
    if (-not $VaultRoot) {
        $defaultVault = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
        $VaultRoot = (Resolve-Path $defaultVault).Path
    }
    else {
        $VaultRoot = (Resolve-Path $VaultRoot).Path
    }
}
else {
    $VaultRoot = Join-Path $runDir "gate_vault"
    New-Item -ItemType Directory -Path $VaultRoot -Force | Out-Null
    $VaultRoot = (Resolve-Path $VaultRoot).Path
}

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

function Normalize-CaseId {
    param(
        [string]$Value
    )
    $raw = [string]$Value
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return "default"
    }
    $normalized = $raw.ToLowerInvariant() -replace "[^a-z0-9._-]", "-"
    $normalized = $normalized.Trim("-")
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return "default"
    }
    return $normalized
}

function Write-MarkdownSummary {
    param(
        [hashtable]$Payload,
        [string]$Path
    )

    $lines = @()
    $lines += "# Phase5 Promotion Gate Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- isolated_vault: ``$($Payload.isolated_vault)``"
    $lines += "- case_id: ``$($Payload.case_id)``"
    $lines += "- candidate_path: ``$($Payload.candidate_path)``"
    if ($Payload.promoted_target_path) {
        $lines += "- promoted_target_path: ``$($Payload.promoted_target_path)``"
    }
    if ($Payload.search_queries -and @($Payload.search_queries).Count -gt 0) {
        $lines += "- search_queries: ``$([string]::Join(', ', @($Payload.search_queries)))``"
    }
    $lines += ""
    $lines += "## Thresholds"
    $lines += ""
    $th = $Payload.thresholds
    $lines += "- min_score: ``$($th.min_score)``"
    $lines += "- min_recall: ``$($th.min_recall)``"
    $lines += "- min_queries: ``$($th.min_queries)``"
    $lines += "- min_days: ``$($th.min_days)``"
    $lines += "- grace_hours: ``$($th.grace_hours)``"
    $lines += "- max_promotions: ``$($th.max_promotions)``"
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
    isolated_vault = [bool]$isIsolated
    case_id = Normalize-CaseId -Value $CaseId
    candidate_path = ""
    promoted_target_path = ""
    search_queries = @()
    thresholds = [ordered]@{
        min_score = $MinScore
        min_recall = $MinRecall
        min_queries = $MinQueries
        min_days = $MinDays
        grace_hours = $GraceHours
        max_promotions = $MaxPromotions
    }
    checks = New-Object System.Collections.ArrayList
    steps = @()
}

Write-Host "[INFO] Phase5 promotion run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan
Write-Host "[INFO] Vault root: $VaultRoot" -ForegroundColor Cyan

if ($isIsolated) {
    $initStep = Invoke-MemoryCliStep -StepName "init" -CommandArgs @("init")
    $summary.steps += $initStep
    if (-not $initStep.ok) {
        $summary.overall_ok = $false
    }
}

$token = "phase5-$($summary.case_id)-$runId"
$candidatePath = "11_AI_Mirror/internalised_candidates/$token.md"
$candidateBody = ""
if (-not [string]::IsNullOrWhiteSpace($CandidateBodyFile)) {
    $candidateBodyPath = Resolve-Path -Path $CandidateBodyFile -ErrorAction Stop
    $candidateBody = Get-Content -Path $candidateBodyPath.Path -Raw -Encoding UTF8
}
else {
    $candidateBody = @"
# phase5 promotion gate

- token: $token
- case_id: $($summary.case_id)
- target: verify recall tracker to promotion pipeline
- expected: candidate promoted into 10_Permanent/Concepts
"@
}
$summary.candidate_path = $candidatePath

$writeStep = Invoke-MemoryCliStep -StepName "candidate-write" -CommandArgs @(
    "memory", "replace",
    "--path", $candidatePath,
    "--content", $candidateBody,
    "--source", "mirror",
    "--agent", "phase5-gate"
)
$summary.steps += $writeStep
if (-not $writeStep.ok) {
    $summary.overall_ok = $false
}

$queryList = @()
if ($SearchQueries) {
    foreach ($query in $SearchQueries) {
        $parts = ([string]$query) -split ","
        foreach ($part in $parts) {
            $text = [string]$part
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $queryList += $text.Trim()
            }
        }
    }
}
if (@($queryList).Count -eq 0) {
    $queryList += "phase5-promotion-token-$token"
    $queryList += "recall-tracker-concept-pipeline-$token"
}
$summary.search_queries = @($queryList)

$searchStepIndex = 1
foreach ($query in $queryList) {
    $searchStep = Invoke-MemoryCliStep -StepName ("search-" + $searchStepIndex) -CommandArgs @("search", $query, "--json")
    $summary.steps += $searchStep
    if (-not $searchStep.ok) {
        $summary.overall_ok = $false
    }
    $searchStepIndex += 1
}

$recallBeforeStep = Invoke-MemoryCliStep -StepName "recall-before" -CommandArgs @("recall-show", "--json")
$summary.steps += $recallBeforeStep
if (-not $recallBeforeStep.ok) { $summary.overall_ok = $false }

$dryRunStep = Invoke-MemoryCliStep -StepName "promotion-dry-run" -CommandArgs @(
    "promote-cycle",
    "--phase", "light",
    "--min-score", "$MinScore",
    "--min-recall", "$MinRecall",
    "--min-queries", "$MinQueries",
    "--min-days", "$MinDays",
    "--grace-hours", "$GraceHours",
    "--max-promotions", "$MaxPromotions",
    "--dry-run",
    "--json"
)
$summary.steps += $dryRunStep
if (-not $dryRunStep.ok) { $summary.overall_ok = $false }

$liveStep = Invoke-MemoryCliStep -StepName "promotion-live" -CommandArgs @(
    "promote-cycle",
    "--phase", "light",
    "--min-score", "$MinScore",
    "--min-recall", "$MinRecall",
    "--min-queries", "$MinQueries",
    "--min-days", "$MinDays",
    "--grace-hours", "$GraceHours",
    "--max-promotions", "$MaxPromotions",
    "--json"
)
$summary.steps += $liveStep
if (-not $liveStep.ok) { $summary.overall_ok = $false }

$recallAfterStep = Invoke-MemoryCliStep -StepName "recall-after-promoted" -CommandArgs @("recall-show", "--promoted-only", "--json")
$summary.steps += $recallAfterStep
if (-not $recallAfterStep.ok) { $summary.overall_ok = $false }

$recallBefore = Parse-StepJson -Step $recallBeforeStep
$dryRun = Parse-StepJson -Step $dryRunStep
$live = Parse-StepJson -Step $liveStep
$recallAfter = Parse-StepJson -Step $recallAfterStep

if (-not $recallBefore) {
    Add-Check -Checks $summary.checks -Name "recall_before_json" -Ok $false -Detail "invalid json"
    $summary.overall_ok = $false
}
if (-not $dryRun) {
    Add-Check -Checks $summary.checks -Name "promotion_dry_run_json" -Ok $false -Detail "invalid json"
    $summary.overall_ok = $false
}
if (-not $live) {
    Add-Check -Checks $summary.checks -Name "promotion_live_json" -Ok $false -Detail "invalid json"
    $summary.overall_ok = $false
}
if (-not $recallAfter) {
    Add-Check -Checks $summary.checks -Name "recall_after_json" -Ok $false -Detail "invalid json"
    $summary.overall_ok = $false
}

if ($recallBefore) {
    $entry = $null
    if ($recallBefore.entries) {
        $entry = $recallBefore.entries | Where-Object { [string]$_.path -eq $candidatePath } | Select-Object -First 1
    }
    $entryFound = $null -ne $entry
    Add-Check -Checks $summary.checks -Name "recall_entry_exists" -Ok $entryFound -Detail "path=$candidatePath"
    if (-not $entryFound) {
        $summary.overall_ok = $false
    }
    else {
        $recallCount = [int]$entry.recall_count
        $uniqueQueries = 0
        if ($entry.unique_query_hashes) {
            $uniqueQueries = @($entry.unique_query_hashes).Count
        }
        $okRecallCount = ($recallCount -ge $MinRecall)
        $okUniqueQueries = ($uniqueQueries -ge $MinQueries)
        Add-Check -Checks $summary.checks -Name "recall_count_threshold" -Ok $okRecallCount -Detail "actual=$recallCount expected>=$MinRecall"
        Add-Check -Checks $summary.checks -Name "unique_queries_threshold" -Ok $okUniqueQueries -Detail "actual=$uniqueQueries expected>=$MinQueries"
        if (-not $okRecallCount -or -not $okUniqueQueries) {
            $summary.overall_ok = $false
        }
    }
}

if ($dryRun) {
    $dryCandidate = $null
    if ($dryRun.candidates) {
        $dryCandidate = $dryRun.candidates | Where-Object { [string]$_.path -eq $candidatePath } | Select-Object -First 1
    }
    $okDryCandidate = $null -ne $dryCandidate
    Add-Check -Checks $summary.checks -Name "dry_run_candidate_exists" -Ok $okDryCandidate -Detail "path=$candidatePath"
    if (-not $okDryCandidate) {
        $summary.overall_ok = $false
    }
}

if ($live) {
    $promotedItem = $null
    if ($live.promoted) {
        $promotedItem = $live.promoted | Where-Object { [string]$_.path -eq $candidatePath } | Select-Object -First 1
    }
    $okPromoted = $null -ne $promotedItem
    Add-Check -Checks $summary.checks -Name "live_promoted_exists" -Ok $okPromoted -Detail "path=$candidatePath"
    if (-not $okPromoted) {
        $summary.overall_ok = $false
    }
    else {
        $targetPath = [string]$promotedItem.target_path
        $summary.promoted_target_path = $targetPath
        $targetExists = $false
        if ($targetPath) {
            $targetAbs = Join-Path $VaultRoot $targetPath
            $targetExists = Test-Path -LiteralPath $targetAbs
        }
        Add-Check -Checks $summary.checks -Name "promoted_target_exists" -Ok $targetExists -Detail "target=$targetPath"
        if (-not $targetExists) {
            $summary.overall_ok = $false
        }
    }
}

if ($recallAfter) {
    $entry = $null
    if ($recallAfter.entries) {
        $entry = $recallAfter.entries | Where-Object { [string]$_.path -eq $candidatePath } | Select-Object -First 1
    }
    $okPromotedMarked = ($null -ne $entry -and [bool]$entry.promoted)
    Add-Check -Checks $summary.checks -Name "recall_promoted_marked" -Ok $okPromotedMarked -Detail "entry promoted flag after cycle"
    if (-not $okPromotedMarked) {
        $summary.overall_ok = $false
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Phase5 promotion gate completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Phase5 promotion gate has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

