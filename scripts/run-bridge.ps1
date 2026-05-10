param(
    [string]$VaultRoot = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 16000,
    [string]$PythonExe = "python"
)

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

Write-Host "[INFO] Starting Agent Memory transport bridge..." -ForegroundColor Cyan
Write-Host "[INFO] vault_root=$VaultRoot"
Write-Host "[INFO] bind=$BindHost`:$Port"

$cmd = @(
    "-m", "agent_memory.cli",
    "--vault-root", $VaultRoot,
    "serve-transport-bridge",
    "--host", $BindHost,
    "--port", "$Port"
)

Write-Host "[INFO] Run health smoke check (after 2s)..." -ForegroundColor Yellow
$proc = Start-Process -FilePath $PythonExe -ArgumentList $cmd -PassThru -WindowStyle Hidden

try {
    Start-Sleep -Seconds 2
    $health = Invoke-RestMethod -Method Get -Uri "http://$BindHost`:$Port/health" -TimeoutSec 5
    if ($health.ok -ne $true) {
        throw "health check returned non-ok"
    }
    Write-Host "[OK] bridge health check passed, switching to foreground..." -ForegroundColor Green
}
catch {
    if (!$proc.HasExited) {
        Stop-Process -Id $proc.Id -Force
    }
    throw
}

if (!$proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
}

Write-Host "[INFO] Running in foreground, press Ctrl+C to stop." -ForegroundColor Cyan
& $PythonExe @cmd

