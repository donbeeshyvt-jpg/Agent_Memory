param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$CasesFile = "00_System/08_Runtime_Profiles/retrieval_benchmark_cases_phase4.yaml",
    [string]$TargetVariant = "hybrid_mmr_off",
    [string]$ProviderProfile = "openai",
    [string]$ProviderModel = "text-embedding-3-small",
    [double]$ProviderTimeout = 20.0,
    [double]$MinAnyPathHitRate = 0.20,
    [double]$MinTop1PathHitRate = 0.20,
    [double]$MinKeywordHitRate = 0.95,
    [double]$MaxAvgLatencyMs = 120.0,
    [switch]$SkipCompareFts,
    [switch]$RequireProviderEffective,
    [string]$OutputRoot = "artifacts/phase4_provider_ab_runs"
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

$runId = "phase4-provider-ab-" + (Get-Date -Format "yyyyMMdd-HHmmss")
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

function Evaluate-Benchmark {
    param(
        [object]$Payload
    )
    $result = [ordered]@{
        ok = $true
        checks = @()
        benchmark_recommended = ""
        target_summary = $null
        fts_summary = $null
    }
    if (-not $Payload) {
        $result.ok = $false
        $result.checks += [ordered]@{ name = "benchmark_json"; ok = $false; detail = "invalid json payload" }
        return $result
    }
    $result.benchmark_recommended = [string]$Payload.recommended
    $variants = $Payload.variants
    if (-not $variants) {
        $result.ok = $false
        $result.checks += [ordered]@{ name = "variants_exist"; ok = $false; detail = "no variants in payload" }
        return $result
    }
    $targetRow = $variants | Where-Object { [string]$_.variant -eq $TargetVariant } | Select-Object -First 1
    $ftsRow = $variants | Where-Object { [string]$_.variant -eq "fts_only" } | Select-Object -First 1
    if (-not $targetRow) {
        $result.ok = $false
        $result.checks += [ordered]@{ name = "target_variant_exists"; ok = $false; detail = "target variant missing: $TargetVariant" }
        return $result
    }

    $summary = $targetRow.summary
    $result.target_summary = $summary
    if ($ftsRow) {
        $result.fts_summary = $ftsRow.summary
    }
    $anyRate = [double]$summary.any_path_hit_rate
    $top1Rate = [double]$summary.top1_path_hit_rate
    $keywordRate = [double]$summary.keyword_hit_rate
    $avgLatency = [double]$summary.avg_latency_ms
    $casesCount = [int]$summary.cases

    $okCases = ($casesCount -ge 10)
    $result.checks += [ordered]@{ name = "cases_count"; ok = $okCases; detail = "cases=$casesCount (expected >=10)" }
    if (-not $okCases) { $result.ok = $false }

    $okAny = ($anyRate -ge $MinAnyPathHitRate)
    $result.checks += [ordered]@{ name = "any_path_hit_rate"; ok = $okAny; detail = "actual=$anyRate threshold>=$MinAnyPathHitRate" }
    if (-not $okAny) { $result.ok = $false }

    $okTop1 = ($top1Rate -ge $MinTop1PathHitRate)
    $result.checks += [ordered]@{ name = "top1_path_hit_rate"; ok = $okTop1; detail = "actual=$top1Rate threshold>=$MinTop1PathHitRate" }
    if (-not $okTop1) { $result.ok = $false }

    $okKeyword = ($keywordRate -ge $MinKeywordHitRate)
    $result.checks += [ordered]@{ name = "keyword_hit_rate"; ok = $okKeyword; detail = "actual=$keywordRate threshold>=$MinKeywordHitRate" }
    if (-not $okKeyword) { $result.ok = $false }

    $okLatency = ($avgLatency -le $MaxAvgLatencyMs)
    $result.checks += [ordered]@{ name = "avg_latency_ms"; ok = $okLatency; detail = "actual=$avgLatency threshold<=$MaxAvgLatencyMs" }
    if (-not $okLatency) { $result.ok = $false }

    if (-not $SkipCompareFts) {
        if ($ftsRow) {
            $ftsAny = [double]$ftsRow.summary.any_path_hit_rate
            $okCompare = ($anyRate -ge $ftsAny)
            $result.checks += [ordered]@{ name = "beats_or_equals_fts_any_path_hit"; ok = $okCompare; detail = "target=$anyRate fts=$ftsAny" }
            if (-not $okCompare) { $result.ok = $false }
        }
        else {
            $result.checks += [ordered]@{ name = "beats_or_equals_fts_any_path_hit"; ok = $false; detail = "fts_only variant missing" }
            $result.ok = $false
        }
    }
    return $result
}

function Probe-EmbeddingBackend {
    $step = Invoke-MemoryCliStep -StepName "probe-vector-backend" -CommandArgs @(
        "search",
        "task_completion_log.md task_id completed_at",
        "--strategy", "vector",
        "--limit", "1",
        "--json"
    )
    $payload = Parse-StepJson -Step $step
    $backend = ""
    $rows = @($payload)
    if ($rows.Count -gt 0) {
        $first = $rows[0]
        if ($first -and $first.PSObject.Properties.Match("metadata").Count -gt 0) {
            $meta = $first.metadata
            if ($meta -and $meta.PSObject.Properties.Match("embedding_backend").Count -gt 0) {
                $backend = [string]$meta.embedding_backend
            }
        }
    }
    return [ordered]@{
        step = $step
        backend = $backend
    }
}

function Write-MarkdownSummary {
    param(
        [hashtable]$Payload,
        [string]$Path
    )
    $lines = @()
    $lines += "# Phase4 Provider A/B Report"
    $lines += ""
    $lines += "- run_id: ``$($Payload.run_id)``"
    $lines += "- started_at: ``$($Payload.started_at)``"
    $lines += "- ended_at: ``$($Payload.ended_at)``"
    $lines += "- overall_ok: ``$($Payload.overall_ok)``"
    $lines += "- vault_root: ``$($Payload.vault_root)``"
    $lines += "- cases_file: ``$($Payload.cases_file)``"
    $lines += "- target_variant: ``$($Payload.target_variant)``"
    $lines += "- require_provider_effective: ``$($Payload.require_provider_effective)``"
    $lines += ""
    foreach ($mode in @("hash_run", "provider_run")) {
        $row = $Payload[$mode]
        if (-not $row) { continue }
        $lines += "## $mode"
        $lines += ""
        $lines += "- ok: ``$($row.ok)``"
        $lines += "- benchmark_recommended: ``$($row.benchmark_recommended)``"
        $lines += "- embedding_backend_probe: ``$($row.embedding_backend_probe)``"
        if ($row.target_summary) {
            $lines += "- target_any_path_hit_rate: ``$($row.target_summary.any_path_hit_rate)``"
            $lines += "- target_top1_path_hit_rate: ``$($row.target_summary.top1_path_hit_rate)``"
            $lines += "- target_keyword_hit_rate: ``$($row.target_summary.keyword_hit_rate)``"
            $lines += "- target_avg_latency_ms: ``$($row.target_summary.avg_latency_ms)``"
        }
        $lines += ""
        foreach ($check in $row.checks) {
            $status = if ($check.ok) { "PASS" } else { "FAIL" }
            $lines += "- [$status] ``$($check.name)`` | $($check.detail)"
        }
        $lines += ""
    }
    if ($Payload.delta) {
        $lines += "## Delta (provider - hash)"
        $lines += ""
        $lines += "- any_path_hit_rate: ``$($Payload.delta.any_path_hit_rate)``"
        $lines += "- top1_path_hit_rate: ``$($Payload.delta.top1_path_hit_rate)``"
        $lines += "- keyword_hit_rate: ``$($Payload.delta.keyword_hit_rate)``"
        $lines += "- avg_latency_ms: ``$($Payload.delta.avg_latency_ms)``"
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
    cases_file = $CasesFile
    target_variant = $TargetVariant
    provider_profile = $ProviderProfile
    provider_model = $ProviderModel
    require_provider_effective = [bool]$RequireProviderEffective
    hash_run = $null
    provider_run = $null
    delta = $null
    steps = @()
}

$routerPath = Join-Path $VaultRoot "00_System/08_Runtime_Profiles/retrieval_router.yaml"
$routerBackup = Join-Path $runDir "retrieval_router.before.yaml"
if (-not (Test-Path -LiteralPath $routerPath)) {
    throw "retrieval_router.yaml not found: $routerPath"
}
Copy-Item -LiteralPath $routerPath -Destination $routerBackup -Force

Write-Host "[INFO] Phase4 provider A/B run id: $runId" -ForegroundColor Cyan
Write-Host "[INFO] Output dir: $runDir" -ForegroundColor Cyan

try {
    $showBefore = Invoke-MemoryCliStep -StepName "retrieval-show-before" -CommandArgs @("retrieval-show", "--json")
    $summary.steps += $showBefore

    # A: hash
    $setHash = Invoke-MemoryCliStep -StepName "set-embedding-hash" -CommandArgs @(
        "retrieval-set-embedding",
        "--mode", "hash",
        "--timeout", ([string]$ProviderTimeout),
        "--json"
    )
    $summary.steps += $setHash
    if (-not $setHash.ok) {
        $summary.overall_ok = $false
    }
    $benchHash = Invoke-MemoryCliStep -StepName "benchmark-hash" -CommandArgs @(
        "retrieval-benchmark",
        "--cases-file", $CasesFile,
        "--json"
    )
    $summary.steps += $benchHash
    $hashPayload = Parse-StepJson -Step $benchHash
    $hashEval = Evaluate-Benchmark -Payload $hashPayload
    $hashProbe = Probe-EmbeddingBackend
    $summary.steps += $hashProbe.step
    $summary.hash_run = [ordered]@{
        ok = [bool]$hashEval.ok
        benchmark_recommended = [string]$hashEval.benchmark_recommended
        target_summary = $hashEval.target_summary
        fts_summary = $hashEval.fts_summary
        checks = $hashEval.checks
        embedding_backend_probe = [string]$hashProbe.backend
    }
    if (-not $hashEval.ok) {
        $summary.overall_ok = $false
    }

    # B: provider
    $setProvider = Invoke-MemoryCliStep -StepName "set-embedding-provider" -CommandArgs @(
        "retrieval-set-embedding",
        "--mode", "provider",
        "--profile", $ProviderProfile,
        "--model", $ProviderModel,
        "--timeout", ([string]$ProviderTimeout),
        "--json"
    )
    $summary.steps += $setProvider
    if (-not $setProvider.ok) {
        $summary.overall_ok = $false
    }
    $benchProvider = Invoke-MemoryCliStep -StepName "benchmark-provider" -CommandArgs @(
        "retrieval-benchmark",
        "--cases-file", $CasesFile,
        "--json"
    )
    $summary.steps += $benchProvider
    $providerPayload = Parse-StepJson -Step $benchProvider
    $providerEval = Evaluate-Benchmark -Payload $providerPayload
    $providerProbe = Probe-EmbeddingBackend
    $summary.steps += $providerProbe.step
    $providerEffective = ([string]$providerProbe.backend -eq "provider")
    $providerEffectiveOk = $true
    if ($RequireProviderEffective) {
        $providerEffectiveOk = $providerEffective
    }
    $providerEffectiveDetail = if ($RequireProviderEffective) {
        "detected_backend=$($providerProbe.backend) expected=provider"
    }
    else {
        "detected_backend=$($providerProbe.backend) (informational; pass -RequireProviderEffective to enforce provider path)"
    }
    $providerChecks = @($providerEval.checks)
    $providerChecks += [ordered]@{
        name = "provider_embedding_backend_probe"
        ok = $providerEffectiveOk
        detail = $providerEffectiveDetail
    }
    $providerRunOk = ([bool]$providerEval.ok -and $providerEffectiveOk)
    $summary.provider_run = [ordered]@{
        ok = [bool]$providerRunOk
        benchmark_recommended = [string]$providerEval.benchmark_recommended
        target_summary = $providerEval.target_summary
        fts_summary = $providerEval.fts_summary
        checks = $providerChecks
        embedding_backend_probe = [string]$providerProbe.backend
        provider_effective = [bool]$providerEffective
    }
    if (-not $providerRunOk) {
        $summary.overall_ok = $false
    }

    if ($summary.hash_run.target_summary -and $summary.provider_run.target_summary) {
        $h = $summary.hash_run.target_summary
        $p = $summary.provider_run.target_summary
        $summary.delta = [ordered]@{
            any_path_hit_rate = [math]::Round(([double]$p.any_path_hit_rate - [double]$h.any_path_hit_rate), 4)
            top1_path_hit_rate = [math]::Round(([double]$p.top1_path_hit_rate - [double]$h.top1_path_hit_rate), 4)
            keyword_hit_rate = [math]::Round(([double]$p.keyword_hit_rate - [double]$h.keyword_hit_rate), 4)
            avg_latency_ms = [math]::Round(([double]$p.avg_latency_ms - [double]$h.avg_latency_ms), 2)
        }
    }
}
finally {
    if (Test-Path -LiteralPath $routerBackup) {
        Copy-Item -LiteralPath $routerBackup -Destination $routerPath -Force
    }
}

$summary.ended_at = (Get-Date).ToString("o")

$summaryJsonPath = Join-Path $runDir "summary.json"
$summaryMdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryJsonPath -Encoding UTF8
Write-MarkdownSummary -Payload $summary -Path $summaryMdPath

if ($summary.overall_ok) {
    Write-Host "[OK] Phase4 provider A/B completed: $summaryJsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] Phase4 provider A/B has failures: $summaryJsonPath" -ForegroundColor Red
exit 1

