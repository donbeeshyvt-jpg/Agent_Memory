param(
    [string]$PythonExe = "python",
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Join-OutputText {
    param([object]$Value)
    if ($null -eq $Value) { return "" }
    if ($Value -is [array]) {
        return [string]::Join([Environment]::NewLine, ($Value | ForEach-Object { [string]$_ }))
    }
    return [string]$Value
}

function Invoke-External {
    param(
        [string]$Exe,
        [string[]]$ArgList
    )
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $merged = & $Exe @ArgList 2>&1
    }
    finally {
        $ErrorActionPreference = $previousEap
    }
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    [ordered]@{
        exit_code = $exitCode
        output = (Join-OutputText -Value $merged).Trim()
    }
}

function Find-Vcvars64 {
    $pf86 = ${env:ProgramFiles(x86)}
    $pf = ${env:ProgramFiles}
    $candidates = @(
        (Join-Path $pf86 "Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf86 "Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf86 "Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf86 "Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf "Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf "Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf "Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path $pf "Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat")
    )

    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            return $path
        }
    }

    return ""
}

$summary = [ordered]@{
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    overall_ok = $true
    python_exe = $PythonExe
    vcvars64 = ""
    mode = ""
    command = ""
    output = ""
    error = ""
}

try {
    $pythonExists = (Get-Command $PythonExe -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $PythonExe)
    if (-not $pythonExists) {
        throw "Python executable not found: $PythonExe"
    }

    $vcvars = Find-Vcvars64
    $summary.vcvars64 = $vcvars

    if ($vcvars) {
        $summary.mode = "vcvars64"
        $cmdLine = "call `"$vcvars`" && set CMAKE_ARGS=-DCMAKE_C_FLAGS=/utf-8 -DCMAKE_CXX_FLAGS=/utf-8 && `"$PythonExe`" -m pip install --upgrade --no-cache-dir --force-reinstall llama-cpp-python"
        $summary.command = $cmdLine
        $run = Invoke-External -Exe "cmd.exe" -ArgList @("/d", "/s", "/c", $cmdLine)
        $summary.output = $run.output
        if ($run.exit_code -ne 0) {
            throw "llama-cpp-python install failed under vcvars64 environment."
        }
    }
    else {
        $summary.mode = "plain-pip"
        $summary.command = "$PythonExe -m pip install --upgrade --no-cache-dir --force-reinstall llama-cpp-python"
        $run = Invoke-External -Exe $PythonExe -ArgList @("-m", "pip", "install", "--upgrade", "--no-cache-dir", "--force-reinstall", "llama-cpp-python")
        $summary.output = $run.output
        if ($run.exit_code -ne 0) {
            throw "llama-cpp-python install failed. Install VS 2022 Build Tools (C++) and rerun this script."
        }
    }
}
catch {
    $summary.overall_ok = $false
    $summary.error = $_.Exception.Message
}

$summary.ended_at = (Get-Date).ToString("o")

if ($Json) {
    $summary | ConvertTo-Json -Depth 20
    if ($summary.overall_ok) { exit 0 } else { exit 1 }
}

if ($summary.overall_ok) {
    Write-Host "[OK] llama-cpp-python installation completed." -ForegroundColor Green
}
else {
    Write-Host "[ERR] llama-cpp-python installation failed." -ForegroundColor Red
    if ($summary.error) {
        Write-Host "[ERR] $($summary.error)" -ForegroundColor Yellow
    }
    Write-Host "[TIP] Install Build Tools: winget install -e --id Microsoft.VisualStudio.2022.BuildTools" -ForegroundColor Yellow
}

if ($summary.overall_ok) { exit 0 } else { exit 1 }
