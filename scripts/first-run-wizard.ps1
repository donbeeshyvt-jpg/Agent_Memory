param(
    [string]$PreferredPythonVersion = "3.12",
    [switch]$SkipLlamaCpp,
    [switch]$NonInteractive,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Add-Step {
    param(
        [System.Collections.IList]$Rows,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )

    $Rows.Add([ordered]@{
            name = $Name
            ok = $Ok
            detail = $Detail
        }) | Out-Null
}

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

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return ""
    }
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

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($machine -and $user) {
        $env:Path = "$machine;$user"
        return
    }
    if ($machine) {
        $env:Path = $machine
        return
    }
    if ($user) {
        $env:Path = $user
    }
}

function Ask-YesNo {
    param(
        [string]$Prompt,
        [bool]$Default = $true
    )

    if ($NonInteractive) {
        return $Default
    }

    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    $raw = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }

    $text = $raw.Trim().ToLowerInvariant()
    if ($text -in @("y", "yes", "1", "true")) { return $true }
    if ($text -in @("n", "no", "0", "false")) { return $false }
    return $Default
}

function Compare-VersionGte {
    param(
        [string]$CurrentVersion,
        [string]$MinVersion
    )

    try {
        return ([Version]$CurrentVersion -ge [Version]$MinVersion)
    }
    catch {
        return $false
    }
}

function Get-VersionFromText {
    param([string]$Text)

    $match = [regex]::Match($Text, "(\d+\.\d+\.\d+)")
    if ($match.Success) {
        return $match.Groups[1].Value
    }
    return ""
}

function Resolve-PythonCommand {
    $candidates = @(
        [ordered]@{ launcher = "python"; prefix = @() },
        [ordered]@{ launcher = "py"; prefix = @("-3") }
    )

    foreach ($item in $candidates) {
        $launcher = [string]$item.launcher
        $prefix = [string[]]$item.prefix

        $cmd = Get-Command $launcher -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }

        $probe = Invoke-External -Exe $launcher -ArgList ($prefix + @("--version"))
        if ($probe.exit_code -ne 0) { continue }

        $version = Get-VersionFromText -Text $probe.output
        if (-not $version) { continue }
        if (-not (Compare-VersionGte -CurrentVersion $version -MinVersion "3.10")) { continue }

        $pathProbe = Invoke-External -Exe $launcher -ArgList ($prefix + @("-c", "import sys;print(sys.executable)"))
        if ($pathProbe.exit_code -ne 0) { continue }

        $exePath = (First-Line -Text $pathProbe.output)
        if (-not $exePath -and $launcher -eq "python") {
            $exePath = [string]$cmd.Source
        }
        if (-not $exePath) { continue }

        $pipProbe = Invoke-External -Exe $launcher -ArgList ($prefix + @("-m", "pip", "--version"))
        if ($pipProbe.exit_code -ne 0) { continue }

        return [ordered]@{
            launcher = $launcher
            prefix = $prefix
            version = $version
            executable_path = $exePath
            pip_version = (First-Line -Text $pipProbe.output)
        }
    }

    return $null
}

function Invoke-WingetInstall {
    param([string]$PackageId)

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        return [ordered]@{ ok = $false; detail = "winget_not_found" }
    }

    $args = @(
        "install", "-e",
        "--id", $PackageId,
        "--source", "winget",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )
    $run = Invoke-External -Exe "winget" -ArgList $args
    Refresh-ProcessPath
    return [ordered]@{
        ok = ($run.exit_code -eq 0)
        detail = $run.output
    }
}

function Invoke-Python {
    param(
        [object]$Python,
        [string[]]$Args
    )

    return Invoke-External -Exe $Python.launcher -ArgList ($Python.prefix + $Args)
}

$summary = [ordered]@{
    started_at = (Get-Date).ToString("o")
    ended_at = ""
    project_root = $projectRoot
    overall_ok = $true
    python = $null
    bootstrap = $null
    error = ""
    steps = New-Object System.Collections.ArrayList
    notes = New-Object System.Collections.ArrayList
}

try {
    Write-Host "[INFO] Checking Git..." -ForegroundColor Cyan
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCmd) {
        Add-Step -Rows $summary.steps -Name "git-check" -Ok $true -Detail ([string]$gitCmd.Source)
    }
    else {
        Add-Step -Rows $summary.steps -Name "git-check" -Ok $false -Detail "git_not_found"
        $installGit = Ask-YesNo -Prompt "Git not found. Install Git.Git with winget?" -Default $true
        if ($installGit) {
            $gitInstall = Invoke-WingetInstall -PackageId "Git.Git"
            $gitCmd = Get-Command git -ErrorAction SilentlyContinue
            if ($gitInstall.ok -and $gitCmd) {
                Add-Step -Rows $summary.steps -Name "git-install" -Ok $true -Detail ([string]$gitCmd.Source)
            }
            else {
                Add-Step -Rows $summary.steps -Name "git-install" -Ok $false -Detail (First-Line -Text $gitInstall.detail)
                $summary.notes.Add("Git install may need a new terminal session to refresh PATH.") | Out-Null
            }
        }
        else {
            Add-Step -Rows $summary.steps -Name "git-install" -Ok $false -Detail "skipped_by_user"
        }
    }

    Write-Host "[INFO] Checking Python >= 3.10..." -ForegroundColor Cyan
    $python = Resolve-PythonCommand
    if (-not $python) {
        Add-Step -Rows $summary.steps -Name "python-check" -Ok $false -Detail "python_not_found_or_too_old"
        $installPy = Ask-YesNo -Prompt "Python not found. Install Python.Python.$PreferredPythonVersion with winget?" -Default $true
        if (-not $installPy) {
            throw "Python is required."
        }

        $pkg = "Python.Python.$PreferredPythonVersion"
        $pyInstall = Invoke-WingetInstall -PackageId $pkg
        if (-not $pyInstall.ok) {
            Add-Step -Rows $summary.steps -Name "python-install" -Ok $false -Detail (First-Line -Text $pyInstall.detail)
            throw "Python install failed."
        }

        $python = Resolve-PythonCommand
        if (-not $python) {
            Add-Step -Rows $summary.steps -Name "python-install" -Ok $false -Detail "installed_but_not_in_path_yet"
            throw "Python installed but not visible in PATH. Reopen terminal and rerun setup."
        }

        Add-Step -Rows $summary.steps -Name "python-install" -Ok $true -Detail ("{0} ({1})" -f $python.version, $python.executable_path)
    }
    else {
        Add-Step -Rows $summary.steps -Name "python-check" -Ok $true -Detail ("{0} ({1})" -f $python.version, $python.executable_path)
    }

    $summary.python = $python

    Write-Host "[INFO] Upgrading pip/setuptools/wheel..." -ForegroundColor Cyan
    $pipUpgrade = Invoke-Python -Python $python -Args @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
    Add-Step -Rows $summary.steps -Name "pip-upgrade" -Ok ($pipUpgrade.exit_code -eq 0) -Detail (First-Line -Text $pipUpgrade.output)
    if ($pipUpgrade.exit_code -ne 0) {
        throw "pip upgrade failed."
    }

    Write-Host "[INFO] Installing core package (pip install -e .)..." -ForegroundColor Cyan
    $editableInstall = Invoke-Python -Python $python -Args @("-m", "pip", "install", "-e", ".")
    Add-Step -Rows $summary.steps -Name "pip-install-editable" -Ok ($editableInstall.exit_code -eq 0) -Detail (First-Line -Text $editableInstall.output)
    if ($editableInstall.exit_code -ne 0) {
        throw "pip install -e . failed."
    }

    $shouldInstallLlama = $false
    if (-not $SkipLlamaCpp) {
        $shouldInstallLlama = Ask-YesNo -Prompt "Install llama-cpp-python now? (optional; may require VS Build Tools)" -Default $false
    }

    if ($shouldInstallLlama) {
        Write-Host "[INFO] Installing llama-cpp-python (optional)..." -ForegroundColor Cyan
        $installer = Join-Path $projectRoot "scripts/install-llama-cpp-windows.ps1"
        if (-not (Test-Path -LiteralPath $installer)) {
            Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $false -Detail "installer_script_not_found"
            $summary.notes.Add("Missing scripts/install-llama-cpp-windows.ps1") | Out-Null
        }
        else {
            $llamaOut = & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -PythonExe $python.executable_path -Json
            $llamaExit = $LASTEXITCODE
            if ($llamaExit -eq 0) {
                Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $true -Detail "ok"
            }
            else {
                $llamaText = Join-OutputText -Value $llamaOut
                Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $false -Detail (First-Line -Text $llamaText)
                $summary.notes.Add("llama-cpp install failed. Rerun scripts/install-llama-cpp-windows.ps1 later.") | Out-Null
            }
        }
    }
    else {
        Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $true -Detail "skipped"
    }

    Write-Host "[INFO] Running bootstrap-v1..." -ForegroundColor Cyan
    $bootstrapScript = Join-Path $projectRoot "scripts/bootstrap-v1.ps1"
    $bootstrapOut = & powershell -NoProfile -ExecutionPolicy Bypass -File $bootstrapScript -PythonExe $python.executable_path -SetDefaultVault -Json
    $bootstrapExit = $LASTEXITCODE
    if ($bootstrapExit -ne 0) {
        Add-Step -Rows $summary.steps -Name "bootstrap-v1" -Ok $false -Detail (First-Line -Text (Join-OutputText -Value $bootstrapOut))
        throw "bootstrap-v1 failed."
    }

    try {
        $summary.bootstrap = ($bootstrapOut | ConvertFrom-Json)
    }
    catch {
        $summary.bootstrap = $null
    }
    Add-Step -Rows $summary.steps -Name "bootstrap-v1" -Ok $true -Detail "ok"
}
catch {
    $summary.overall_ok = $false
    $summary.error = $_.Exception.Message
    Add-Step -Rows $summary.steps -Name "error" -Ok $false -Detail $_.Exception.Message
}

foreach ($row in $summary.steps) {
    if (-not [bool]$row.ok) {
        $summary.overall_ok = $false
    }
}

$summary.ended_at = (Get-Date).ToString("o")

if ($Json) {
    $summary | ConvertTo-Json -Depth 30
    if ($summary.overall_ok) { exit 0 } else { exit 1 }
}

if ($summary.overall_ok) {
    Write-Host "[OK] First-run wizard completed." -ForegroundColor Green
}
else {
    Write-Host "[ERR] First-run wizard completed with failures." -ForegroundColor Red
}

Write-Host "[INFO] project_root=$($summary.project_root)"
if ($summary.python) {
    Write-Host "[INFO] python=$($summary.python.version) path=$($summary.python.executable_path)"
}

foreach ($row in $summary.steps) {
    Write-Host ("[STEP] {0} ok={1} detail={2}" -f $row.name, $row.ok, $row.detail)
}

foreach ($note in $summary.notes) {
    Write-Host ("[NOTE] {0}" -f $note) -ForegroundColor Yellow
}

if ($summary.overall_ok) { exit 0 } else { exit 1 }
