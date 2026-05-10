param(
    [string]$VaultRoot = "",
    [string]$PythonExe = "python",
    [string]$CudaRuntimePath = "C:/Users/Cane/AppData/Local/Programs/Ollama/lib/ollama/cuda_v12",
    [string]$OutputRoot = "artifacts/gguf_chat_smoke_runs"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not $VaultRoot) {
    $defaultVault = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
    $VaultRoot = (Resolve-Path $defaultVault).Path
}

$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found: ${PythonExe}"
}

$env:PATH = "$CudaRuntimePath;$env:PATH"

$runId = "gguf-chat-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$runDir = Join-Path $projectRoot (Join-Path $OutputRoot $runId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

function Invoke-ChatProbe {
    param(
        [string]$StepName,
        [string[]]$ExtraArgs
    )

    $stdoutPath = Join-Path $runDir "$StepName.stdout.log"
    $stderrPath = Join-Path $runDir "$StepName.stderr.log"
    $allArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $VaultRoot, "chat", "請只回 OK", "--transport", "cli", "--context", "gguf-smoke", "--session", "$runId-$StepName", "--require-nondegraded", "--json") + $ExtraArgs

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
    return [ordered]@{
        name = $StepName
        command = "$PythonExe $($allArgs -join ' ')"
        exit_code = $exitCode
        ok = ($exitCode -eq 0)
        stdout = (Resolve-Path $stdoutPath).Path
        stderr = (Resolve-Path $stderrPath).Path
    }
}

function Get-StepPayload {
    param([hashtable]$Step)
    $raw = Get-Content -Path $Step.stdout -Encoding UTF8 -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    $degradedMatch = [regex]::Match($raw, '"degraded"\s*:\s*(true|false)', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    $profileMatch = [regex]::Match($raw, '"profile"\s*:\s*"([^"]+)"')
    $modelMatch = [regex]::Match($raw, '"model"\s*:\s*"([^"]+)"')
    $responseMatch = [regex]::Match($raw, '"response"\s*:\s*"([^"]*)"')
    if (-not $degradedMatch.Success) {
        return $null
    }
    $degraded = $degradedMatch.Groups[1].Value.ToLowerInvariant() -eq "true"
    return [ordered]@{
        degraded = $degraded
        profile = if ($profileMatch.Success) { $profileMatch.Groups[1].Value } else { "" }
        model = if ($modelMatch.Success) { $modelMatch.Groups[1].Value } else { "" }
        response = if ($responseMatch.Success) { $responseMatch.Groups[1].Value } else { "" }
    }
}

$steps = @()
$steps += Invoke-ChatProbe -StepName "core-default" -ExtraArgs @("--persona", "core")
$steps += Invoke-ChatProbe -StepName "coder-override" -ExtraArgs @("--persona", "coder")
$steps += Invoke-ChatProbe -StepName "core-qwen30-override" -ExtraArgs @("--persona", "core", "--override-profile", "llama_cpp_local", "--override-model", "../../0_Models/Qwen3-30B-A3B-Q4_K_M.gguf")

$results = @()
$overallOk = $true
foreach ($step in $steps) {
    $payload = Get-StepPayload -Step $step
    $entry = [ordered]@{
        name = $step.name
        ok = $step.ok
        exit_code = $step.exit_code
        degraded = $null
        profile = ""
        model = ""
        response_preview = ""
        stdout = $step.stdout
        stderr = $step.stderr
    }
    if ($payload) {
        $entry.degraded = [bool]$payload.degraded
        $entry.profile = [string]$payload.profile
        $entry.model = [string]$payload.model
        $entry.response_preview = [string]$payload.response
        if ($entry.degraded) {
            $entry.ok = $false
        }
    } else {
        $entry.ok = $false
    }
    if (-not $entry.ok) {
        $overallOk = $false
    }
    $results += $entry
}

$summary = [ordered]@{
    run_id = $runId
    started_at = (Get-Date).ToString("o")
    vault_root = $VaultRoot
    overall_ok = $overallOk
    results = $results
}

$jsonPath = Join-Path $runDir "summary.json"
$mdPath = Join-Path $runDir "summary.md"
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $jsonPath -Encoding UTF8

$lines = @()
$lines += "# GGUF Chat Smoke Report"
$lines += ""
$lines += "- run_id: ``$runId``"
$lines += "- overall_ok: ``$overallOk``"
$lines += "- vault_root: ``$VaultRoot``"
$lines += ""
$lines += "## Results"
$lines += ""
foreach ($r in $results) {
    $status = if ($r.ok) { "PASS" } else { "FAIL" }
    $lines += "- [$status] ``$($r.name)`` | profile=``$($r.profile)`` | degraded=``$($r.degraded)``"
    $lines += "  - model: ``$($r.model)``"
    $lines += "  - response_preview: ``$($r.response_preview)``"
    $lines += "  - stdout: ``$($r.stdout)``"
    $lines += "  - stderr: ``$($r.stderr)``"
}
Set-Content -Path $mdPath -Value ($lines -join [Environment]::NewLine) -Encoding UTF8

if ($overallOk) {
    Write-Host "[OK] GGUF chat smoke passed: $jsonPath" -ForegroundColor Green
    exit 0
}

Write-Host "[ERR] GGUF chat smoke failed: $jsonPath" -ForegroundColor Red
exit 1

