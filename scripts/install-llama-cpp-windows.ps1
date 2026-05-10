param(
    [string]$PythonExe = "python",
    [switch]$ForceReinstall,
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

function First-Line {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    return (($Text -split "`r?`n") | Where-Object { $_.Trim().Length -gt 0 } | Select-Object -First 1)
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

function Quote-CmdArg {
    param([string]$Arg)
    if ($null -eq $Arg -or $Arg.Length -eq 0) {
        return '""'
    }
    $safe = $Arg -replace '"', '\\"'
    if ($safe -match '[\s\&\(\)\^\|\<\>]') {
        return '"' + $safe + '"'
    }
    return $safe
}

function Build-CmdLine {
    param(
        [string]$Exe,
        [string[]]$ArgList
    )
    $parts = @((Quote-CmdArg -Arg $Exe))
    foreach ($arg in $ArgList) {
        $parts += (Quote-CmdArg -Arg $arg)
    }
    return ($parts -join " ")
}

function Invoke-CmdLineStreaming {
    param([string]$CmdLine)

    & cmd.exe /d /s /c "$CmdLine 2>&1" | Out-Host
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    return [ordered]@{
        exit_code = $exitCode
        output = ""
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

function Resolve-PythonExecutable {
    param([string]$Requested)

    if (Test-Path -LiteralPath $Requested) {
        return (Resolve-Path -LiteralPath $Requested).Path
    }

    $cmd = Get-Command $Requested -ErrorAction SilentlyContinue
    if ($cmd) {
        return [string]$cmd.Source
    }

    return ""
}

function Get-LlamaCppVersion {
    param([string]$ResolvedPythonExe)

    $probe = Invoke-External -Exe $ResolvedPythonExe -ArgList @("-m", "pip", "show", "llama-cpp-python")
    if ($probe.exit_code -eq 0) {
        foreach ($line in ($probe.output -split "`r?`n")) {
            if ($line -match "^Version:\s*(.+)$") {
                return $Matches[1].Trim()
            }
        }
        return "installed"
    }
    return ""
}

$summary = [ordered]@{
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    overall_ok = $true
    python_exe = $PythonExe
    python_resolved = ""
    vcvars64 = ""
    mode = ""
    command = ""
    output = ""
    installed_before = ""
    installed_after = ""
    error = ""
}

try {
    $resolvedPython = Resolve-PythonExecutable -Requested $PythonExe
    if (-not $resolvedPython) {
        throw "Python executable not found: $PythonExe"
    }
    $summary.python_resolved = $resolvedPython

    $installedBefore = Get-LlamaCppVersion -ResolvedPythonExe $resolvedPython
    $summary.installed_before = $installedBefore
    if ($installedBefore -and -not $ForceReinstall) {
        $summary.mode = "already-installed"
        $summary.output = "llama-cpp-python already installed: $installedBefore"
        $summary.installed_after = $installedBefore
    }
    else {
        $vcvars = Find-Vcvars64
        $summary.vcvars64 = $vcvars

        $pipArgs = @("-m", "pip", "install", "--upgrade", "llama-cpp-python")
        if ($ForceReinstall) {
            $pipArgs += @("--force-reinstall", "--no-cache-dir")
        }

        if ($vcvars) {
            $summary.mode = if ($ForceReinstall) { "vcvars64-force" } else { "vcvars64" }
            $pipCmd = Build-CmdLine -Exe $resolvedPython -ArgList $pipArgs
            $cmdLine = "call " + (Quote-CmdArg -Arg $vcvars) + " && set CMAKE_ARGS=-DCMAKE_C_FLAGS=/utf-8 -DCMAKE_CXX_FLAGS=/utf-8 && " + $pipCmd
            $summary.command = $cmdLine
            if ($Json) {
                $run = Invoke-External -Exe "cmd.exe" -ArgList @("/d", "/s", "/c", $cmdLine)
            }
            else {
                Write-Host "[INFO] Using VC build environment for llama-cpp installation..." -ForegroundColor Cyan
                Write-Host "[INFO] This step can take several minutes." -ForegroundColor DarkCyan
                $run = Invoke-CmdLineStreaming -CmdLine $cmdLine
            }
        }
        else {
            $summary.mode = if ($ForceReinstall) { "plain-pip-force" } else { "plain-pip" }
            $summary.command = (Build-CmdLine -Exe $resolvedPython -ArgList $pipArgs)
            if ($Json) {
                $run = Invoke-External -Exe $resolvedPython -ArgList $pipArgs
            }
            else {
                Write-Host "[INFO] Installing llama-cpp without vcvars64 (wheel if available)..." -ForegroundColor Cyan
                Write-Host "[INFO] This step can take several minutes." -ForegroundColor DarkCyan
                $run = Invoke-CmdLineStreaming -CmdLine $summary.command
            }
        }

        $summary.output = $run.output
        if ($run.exit_code -ne 0) {
            throw "llama-cpp-python install failed. Install VS 2022 Build Tools (C++) and rerun this script."
        }

        $installedAfter = Get-LlamaCppVersion -ResolvedPythonExe $resolvedPython
        $summary.installed_after = $installedAfter
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
    if ($summary.mode -eq "already-installed") {
        Write-Host "[OK] llama-cpp-python already installed: $($summary.installed_before)" -ForegroundColor Green
    }
    else {
        $ver = if ($summary.installed_after) { $summary.installed_after } else { "(unknown version)" }
        Write-Host "[OK] llama-cpp-python installation completed: $ver" -ForegroundColor Green
    }
}
else {
    Write-Host "[ERR] llama-cpp-python installation failed." -ForegroundColor Red
    if ($summary.error) {
        Write-Host "[ERR] $($summary.error)" -ForegroundColor Yellow
    }
    Write-Host "[TIP] Install Build Tools: winget install -e --id Microsoft.VisualStudio.2022.BuildTools" -ForegroundColor Yellow
}

if ($summary.overall_ok) { exit 0 } else { exit 1 }
