param(
    [string]$PreferredPythonVersion = "3.12",
    [switch]$InstallEditable,
    [switch]$SkipInstallEditable,
    [switch]$UpgradePipPackages,
    [switch]$SkipLlamaCpp,
    [switch]$SkipModelCheck,
    [switch]$SkipModelDownload,
    [switch]$SkipConfigureLLM,
    [string]$VaultRoot = "",
    [switch]$SetupDiscord,
    [string]$DiscordPersona = "steward",
    [string]$DiscordChannelId = "",
    [string]$DiscordTokenEnv = "DISCORD_BOT_TOKEN_STEWARD",
    [switch]$NonInteractive,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Force UTF-8 IO encoding so Python -X utf8 child output is decoded correctly.
# PS5.1 default $OutputEncoding is ASCII; on CJK Windows the console codepage is
# usually CP-950/936 — both will mangle Python JSON output and break ConvertFrom-Json.
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
}
catch { }

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

# 自動載入 .env (API key / Discord token 等)
. (Join-Path $PSScriptRoot "_dotenv-helper.ps1")
Import-DotEnvIntoProcess | Out-Null

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

function Save-DiagnosticLog {
    param(
        [string]$ProjectRoot,
        [string]$Prefix,
        [string]$Text
    )
    try {
        $dir = Join-Path $ProjectRoot "artifacts/bootstrap"
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $path = Join-Path $dir ("{0}-{1}.log" -f $Prefix, $stamp)
        $value = if ([string]::IsNullOrWhiteSpace($Text)) { "(empty)" } else { $Text }
        Set-Content -LiteralPath $path -Value $value -Encoding UTF8
        return $path
    }
    catch {
        return ""
    }
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

        # 重要：優先用 Get-Command 拿到的 .Source 路徑（Win32 API 直接吐 UTF-16，
        # 中文路徑保證正確）。Python -c print(sys.executable) 的 stdout 會被 PS5.1
        # 用錯誤 codepage 解碼成 mojibake，路徑變壞 (e.g. 練習用 → �m�ߥ�)。
        $exePath = ""
        if ($cmd.Source) {
            $exePath = [string]$cmd.Source
        }
        else {
            $pathProbe = Invoke-External -Exe $launcher -ArgList ($prefix + @("-c", "import sys;print(sys.executable)"))
            if ($pathProbe.exit_code -eq 0) {
                $exePath = (First-Line -Text $pathProbe.output)
            }
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
        [string[]]$ArgList
    )

    return Invoke-External -Exe $Python.launcher -ArgList ($Python.prefix + $ArgList)
}

function Invoke-PythonStreaming {
    param(
        [object]$Python,
        [string[]]$ArgList
    )

    # 注意：不要再用 `cmd.exe /d /s /c "..."` 包一層 — 在 CJK locale + 中文路徑下
    # cmd.exe 的 codepage 處理會把路徑當作不存在（"The system cannot find the path specified"）。
    # 直接用 PowerShell native call，stdout/stderr 自動串到 host。
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $exe = $Python.executable_path
        if (-not $exe) {
            $exe = $Python.launcher
        }
        $allArgs = @($Python.prefix + $ArgList)
        & $exe @allArgs
    }
    finally {
        $ErrorActionPreference = $previousEap
    }
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    return [ordered]@{
        exit_code = $exitCode
        output = ""
    }
}

function Resolve-PowerShellExe {
    $candidates = @(
        (Join-Path $PSHOME "powershell.exe"),
        "powershell.exe",
        "powershell"
    )
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            return [string]$cmd.Source
        }
    }
    return ""
}

function Invoke-PowerShellScriptJson {
    param(
        [string]$ScriptPath,
        [hashtable]$SplatArgs = @{}
    )

    # 重要：用 in-process invoke 避免 spawn powershell.exe 時 Windows 把中文路徑用
    # 當前 codepage 編碼壞掉（即使 chcp 65001 也救不到 argv）。
    # 用 hashtable splat 確保 switch+named param 都能正確 bind（array splat 在 PS5.1
    # 對「-SwitchA -ParamB Value」這種混合會 binding 失敗）。
    $finalSplat = $SplatArgs.Clone()
    $finalSplat["Json"] = $true

    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $stdout = & $ScriptPath @finalSplat 2>&1
    }
    finally {
        $ErrorActionPreference = $previousEap
    }
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    return [ordered]@{
        exit_code = $exitCode
        output = (Join-OutputText -Value $stdout).Trim()
        command = "& $ScriptPath @<splat:$($finalSplat.Keys -join ',')>"
    }
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

    if ($UpgradePipPackages) {
        Write-Host "[INFO] Upgrading pip/setuptools/wheel..." -ForegroundColor Cyan
        $pipUpgrade = Invoke-Python -Python $python -ArgList @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
        Add-Step -Rows $summary.steps -Name "pip-upgrade" -Ok ($pipUpgrade.exit_code -eq 0) -Detail (First-Line -Text $pipUpgrade.output)
        if ($pipUpgrade.exit_code -ne 0) {
            throw "pip upgrade failed."
        }
    }
    else {
        Add-Step -Rows $summary.steps -Name "pip-upgrade" -Ok $true -Detail "skipped (use -UpgradePipPackages to enable)"
    }

    $shouldInstallEditable = -not $SkipInstallEditable
    # 注意：不能單純用 `import agent_memory` 偵測，因為 cwd 有 source 時 Python
    # 會直接吃本地檔案，但依賴（PyYAML 等）沒裝 CLI 還是會炸。改成試跑 --help。
    $importProbe = Invoke-Python -Python $python -ArgList @("-m", "agent_memory.cli", "--help")
    $alreadyImportable = ($importProbe.exit_code -eq 0)
    if ($alreadyImportable -and -not $InstallEditable) {
        $shouldInstallEditable = $false
        Add-Step -Rows $summary.steps -Name "pip-install-editable" -Ok $true -Detail "skipped (agent_memory already importable)"
    }
    elseif ($shouldInstallEditable) {
        Write-Host "[INFO] Installing core package (pip install -e .)..." -ForegroundColor Cyan
        Write-Host "[INFO] First install may take 1-10 minutes at metadata/build steps. Please wait." -ForegroundColor DarkCyan
        if ($Json) {
            $editableInstall = Invoke-Python -Python $python -ArgList @("-m", "pip", "install", "-e", ".", "-v", "--disable-pip-version-check", "--no-input")
        }
        else {
            Write-Host "[INFO] pip output will be shown below..." -ForegroundColor DarkCyan
            $editableInstall = Invoke-PythonStreaming -Python $python -ArgList @("-m", "pip", "install", "-e", ".", "-v", "--disable-pip-version-check", "--no-input")
        }
        Add-Step -Rows $summary.steps -Name "pip-install-editable" -Ok ($editableInstall.exit_code -eq 0) -Detail (First-Line -Text $editableInstall.output)
        if ($editableInstall.exit_code -ne 0) {
            throw "pip install -e . failed."
        }
    }
    else {
        Add-Step -Rows $summary.steps -Name "pip-install-editable" -Ok $true -Detail "skipped (use without -SkipInstallEditable to enable)"
        $summary.notes.Add("Core package install skipped. Run: python -m pip install -e .") | Out-Null
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
            if ($Json -or $NonInteractive) {
                $llamaOut = & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -PythonExe $python.executable_path -Json
                $llamaExit = $LASTEXITCODE
                if ($llamaExit -eq 0) {
                    try {
                        $llamaSummary = $llamaOut | ConvertFrom-Json
                        $llamaDetail = if ($llamaSummary.mode) { [string]$llamaSummary.mode } else { "ok" }
                        Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $true -Detail $llamaDetail
                    }
                    catch {
                        Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $true -Detail "ok"
                    }
                }
                else {
                    $llamaText = Join-OutputText -Value $llamaOut
                    Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $false -Detail (First-Line -Text $llamaText)
                    $summary.notes.Add("llama-cpp install failed. Rerun scripts/install-llama-cpp-windows.ps1 later.") | Out-Null
                }
            }
            else {
                & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -PythonExe $python.executable_path
                $llamaExit = $LASTEXITCODE
                if ($llamaExit -eq 0) {
                    Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $true -Detail "ok"
                }
                else {
                    Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $false -Detail "failed (see output above)"
                    $summary.notes.Add("llama-cpp install failed. Rerun scripts/install-llama-cpp-windows.ps1 later.") | Out-Null
                }
            }
        }
    }
    else {
        Add-Step -Rows $summary.steps -Name "install-llama-cpp" -Ok $true -Detail "skipped"
    }

    Write-Host "[INFO] Running bootstrap-v1..." -ForegroundColor Cyan
    $bootstrapScript = Join-Path $projectRoot "scripts/bootstrap-v1.ps1"
    $attempts = New-Object System.Collections.ArrayList
    $fallbackSecondBrainRoot = ""
    $fallbackModelsRoot = ""
    if ($env:USERPROFILE) {
        $fallbackSecondBrainRoot = Join-Path $env:USERPROFILE "SecondBrains\\default_second_brain"
        $fallbackModelsRoot = Join-Path $env:USERPROFILE "0_Models"
    }

    $plans = New-Object System.Collections.ArrayList
    if ($VaultRoot) {
        # 使用者顯式指定 vault root，只試這一個（用於測試或自訂大腦位置）。
        $plans.Add([ordered]@{
                name = "user_specified_vault_root"
                python = [string]$python.executable_path
                second_brain = $VaultRoot
                models = ""
            }) | Out-Null
    }
    else {
        $plans.Add([ordered]@{
                name = "python_exe_default_paths"
                python = [string]$python.executable_path
                second_brain = ""
                models = ""
            }) | Out-Null
        if ($python.launcher -and ($python.launcher -ne $python.executable_path)) {
            $plans.Add([ordered]@{
                    name = "python_launcher_default_paths"
                    python = [string]$python.launcher
                    second_brain = ""
                    models = ""
                }) | Out-Null
        }
        if ($fallbackSecondBrainRoot -and $fallbackModelsRoot) {
            $plans.Add([ordered]@{
                    name = "python_exe_userprofile_paths"
                    python = [string]$python.executable_path
                    second_brain = $fallbackSecondBrainRoot
                    models = $fallbackModelsRoot
                }) | Out-Null
            if ($python.launcher -and ($python.launcher -ne $python.executable_path)) {
                $plans.Add([ordered]@{
                        name = "python_launcher_userprofile_paths"
                        python = [string]$python.launcher
                        second_brain = $fallbackSecondBrainRoot
                        models = $fallbackModelsRoot
                    }) | Out-Null
            }
        }
    }

    $bootstrapOk = $false
    $bootstrapRaw = ""
    foreach ($plan in $plans) {
        # 用 hashtable splat 避免 array splat 在 switch+param 混用時 binding 錯亂
        $splat = @{
            PythonExe = [string]$plan.python
            SetDefaultVault = $true
        }
        if ($plan.second_brain) {
            $splat["SecondBrainRoot"] = [string]$plan.second_brain
        }
        if ($plan.models) {
            $splat["ModelsRoot"] = [string]$plan.models
        }

        $run = Invoke-PowerShellScriptJson -ScriptPath $bootstrapScript -SplatArgs $splat
        $attemptLine = "{0} exit={1} detail={2}" -f $plan.name, $run.exit_code, (First-Line -Text $run.output)
        $attempts.Add($attemptLine) | Out-Null

        if ($run.exit_code -eq 0) {
            $bootstrapOk = $true
            $bootstrapRaw = $run.output
            if ($plan.second_brain -and $plan.name -ne "user_specified_vault_root") {
                $summary.notes.Add("bootstrap used fallback roots under USERPROFILE.") | Out-Null
            }
            break
        }
    }

    if (-not $bootstrapOk) {
        $bootstrapText = [string]::Join([Environment]::NewLine, ($attempts | ForEach-Object { [string]$_ }))
        $bootstrapDetail = First-Line -Text $bootstrapText
        if (-not $bootstrapDetail) {
            $bootstrapDetail = "bootstrap failed; see diagnostic log"
        }
        Add-Step -Rows $summary.steps -Name "bootstrap-v1" -Ok $false -Detail $bootstrapDetail
        $diagPath = Save-DiagnosticLog -ProjectRoot $projectRoot -Prefix "bootstrap-v1-error" -Text $bootstrapText
        if ($diagPath) {
            $summary.notes.Add("bootstrap diagnostic: $diagPath") | Out-Null
        }
        throw "bootstrap-v1 failed."
    }

    try {
        $summary.bootstrap = ($bootstrapRaw | ConvertFrom-Json)
    }
    catch {
        $summary.bootstrap = $null
    }
    Add-Step -Rows $summary.steps -Name "bootstrap-v1" -Ok $true -Detail "ok"

    $resolvedVaultRoot = ""
    if ($summary.bootstrap -and $summary.bootstrap.second_brain_root) {
        $resolvedVaultRoot = [string]$summary.bootstrap.second_brain_root
    }

    # Step: explicit vault-set (fix for SetDefaultVault not propagating through wizard)
    # 注意：當使用者透過 -VaultRoot 指定（通常是測試大腦）時，不去動 user config 預設值，
    # 避免污染正式 vault 路徑。
    if ($VaultRoot) {
        Add-Step -Rows $summary.steps -Name "vault-set-default" -Ok $true -Detail "skipped (-VaultRoot 指定，不修改 user config 預設值)"
    }
    elseif ($resolvedVaultRoot) {
        $vaultSetRun = Invoke-Python -Python $python -ArgList @("-X", "utf8", "-m", "agent_memory.cli", "vault-set", $resolvedVaultRoot)
        $vaultSetOk = ($vaultSetRun.exit_code -eq 0)
        Add-Step -Rows $summary.steps -Name "vault-set-default" -Ok $vaultSetOk -Detail (First-Line -Text $vaultSetRun.output)
        if (-not $vaultSetOk) {
            $summary.notes.Add("vault-set failed; you may need to pass --vault-root explicitly. Detail: $(First-Line -Text $vaultSetRun.output)") | Out-Null
        }
    }
    else {
        Add-Step -Rows $summary.steps -Name "vault-set-default" -Ok $false -Detail "second_brain_root not resolved from bootstrap output"
    }

    # Step: model presence check (steward 預設用 gemma-4 E4B)
    $modelsRoot = ""
    if ($summary.bootstrap -and $summary.bootstrap.models_root) {
        $modelsRoot = [string]$summary.bootstrap.models_root
    }
    if (-not $modelsRoot) {
        $modelsRoot = (Join-Path $projectRoot "..\\0_Models")
    }
    $gemmaDir = Join-Path $modelsRoot "gemma-4-E4B-it-GGUF"
    $gemmaModel = Join-Path $gemmaDir "gemma-4-E4B-it-Q8_0.gguf"
    $qwen8bModel = Join-Path $modelsRoot "Qwen3.5-9B-GGUF\Qwen3.5-9B-Q8_0.gguf"
    $hasGemma = Test-Path -LiteralPath $gemmaModel
    $hasQwen8b = Test-Path -LiteralPath $qwen8bModel

    if ($SkipModelCheck) {
        Add-Step -Rows $summary.steps -Name "model-check" -Ok $true -Detail "skipped (-SkipModelCheck)"
    }
    else {
        $gemmaState = if ($hasGemma) { "ok" } else { "missing" }
        $qwen8bState = if ($hasQwen8b) { "ok" } else { "missing" }
        Add-Step -Rows $summary.steps -Name "model-check" -Ok $true -Detail "gemma4=$gemmaState qwen8b=$qwen8bState"

        # Step: 模型缺失時提供 3-way choice — 下載本機 / 用 Google API / 跳過
        $useGoogleApiAsDefault = $false
        if (-not $hasGemma) {
            if ($SkipModelDownload) {
                Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "skipped (-SkipModelDownload)"
            }
            elseif ($NonInteractive) {
                Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "skipped (NonInteractive)"
                $summary.notes.Add("無本地模型；NonInteractive 模式跳過。互動跑時會問你下載 vs Google API vs 跳過。") | Out-Null
            }
            else {
                Write-Host ""
                Write-Host "  未偵測到本地模型。選擇要怎麼用核心：" -ForegroundColor Yellow
                Write-Host "    [1] 下載本地模型 (gemma-4 / Qwen3.5-9B / Qwen3-30B)" -ForegroundColor White
                Write-Host "    [2] 直接用 Google Gemini API (不下載任何東西,需 GOOGLE_API_KEY)" -ForegroundColor White
                Write-Host "    [3] 都先跳過 (之後用 menu [5] 下載或 [4] 切換)" -ForegroundColor DarkGray
                Write-Host ""
                $choice = ""
                while ($true) {
                    $raw = (Read-Host "  選 [1-3]").Trim()
                    if ($raw -in @("1", "2", "3")) {
                        $choice = $raw
                        break
                    }
                    Write-Host "  請輸入 1 / 2 / 3" -ForegroundColor Red
                }

                if ($choice -eq "1") {
                    $downloader = Join-Path $projectRoot "scripts/download-model.ps1"
                    $prevEap = $ErrorActionPreference
                    $ErrorActionPreference = "Continue"
                    try {
                        & powershell -NoProfile -ExecutionPolicy Bypass -File $downloader -LocalDirRoot $modelsRoot
                        $dlExit = $LASTEXITCODE
                    }
                    finally {
                        $ErrorActionPreference = $prevEap
                    }
                    $hasGemma = Test-Path -LiteralPath $gemmaModel
                    if ($dlExit -eq 0) {
                        Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "downloaded local model"
                    }
                    else {
                        Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $false -Detail "download script exit=$dlExit"
                    }
                }
                elseif ($choice -eq "2") {
                    # Google API path — 取得 key + 設 global_default
                    $useGoogleApiAsDefault = $true
                    $envName = "GOOGLE_API_KEY"
                    $existingKey = [Environment]::GetEnvironmentVariable($envName, "Process")
                    if (-not $existingKey) {
                        $existingKey = [Environment]::GetEnvironmentVariable($envName, "User")
                        if ($existingKey) {
                            Set-Item -LiteralPath "Env:$envName" -Value $existingKey
                        }
                    }

                    $needPrompt = $true
                    if ($existingKey) {
                        # 已有 key — 顯示遮蔽片段,問要不要沿用 / 重貼 / 跳過
                        $maskedKey = ""
                        if ($existingKey.Length -ge 8) {
                            $maskedKey = $existingKey.Substring(0, 4) + "..." + $existingKey.Substring($existingKey.Length - 4)
                        }
                        else {
                            $maskedKey = "***"
                        }
                        Write-Host ""
                        Write-Host "  [偵測到] 已有 $envName ($maskedKey)" -ForegroundColor Green
                        Write-Host "    [1] 沿用此 key" -ForegroundColor Yellow
                        Write-Host "    [2] 重貼新 key" -ForegroundColor Yellow
                        Write-Host "    [3] 移除 key 並跳過 API" -ForegroundColor Yellow
                        while ($true) {
                            $subChoice = (Read-Host "  選 [1-3]").Trim()
                            if ($subChoice -in @("1", "2", "3")) { break }
                            Write-Host "  請輸入 1 / 2 / 3" -ForegroundColor Red
                        }
                        if ($subChoice -eq "1") {
                            $needPrompt = $false
                        }
                        elseif ($subChoice -eq "3") {
                            [Environment]::SetEnvironmentVariable($envName, $null, "User")
                            Remove-Item -LiteralPath "Env:$envName" -ErrorAction SilentlyContinue
                            $existingKey = ""
                            $useGoogleApiAsDefault = $false
                            Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "removed existing key, skipped API"
                            $needPrompt = $false
                        }
                        # 選 [2] 落入 $needPrompt = true 走 prompt 流程
                    }

                    if ($needPrompt -and $useGoogleApiAsDefault) {
                        Write-Host ""
                        Write-Host "  需要 Google AI Studio API key (https://aistudio.google.com/apikey 申請,有免費層)" -ForegroundColor Yellow
                        Write-Host "  輸入時不會顯示,Enter 確認" -ForegroundColor DarkGray
                        $sec = Read-Host -Prompt "  $envName" -AsSecureString
                        if ($sec -and $sec.Length -gt 0) {
                            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
                            try {
                                $existingKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
                            }
                            finally {
                                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                            }
                            Set-Item -LiteralPath "Env:$envName" -Value $existingKey
                            # 3-way: .env / setx / 只此次
                            Write-Host ""
                            Write-Host "  記住 key 到哪裡?" -ForegroundColor Yellow
                            Write-Host "    [1] .env 檔 (推薦,純檔案,gitignored,好刪除)" -ForegroundColor White
                            Write-Host "    [2] Windows 使用者環境變數 (registry,全域)" -ForegroundColor White
                            Write-Host "    [3] 只此次有效" -ForegroundColor DarkGray
                            while ($true) {
                                $persistChoice = (Read-Host "  選 [1-3]").Trim()
                                if ($persistChoice -in @("1", "2", "3")) { break }
                                Write-Host "  請輸入 1 / 2 / 3" -ForegroundColor Red
                            }
                            if ($persistChoice -eq "1") {
                                $envPath = Save-EntryToDotEnv -Key $envName -Value $existingKey
                                Write-Host "  [OK] $envName 寫入 $envPath" -ForegroundColor Green
                            }
                            elseif ($persistChoice -eq "2") {
                                [Environment]::SetEnvironmentVariable($envName, $existingKey, "User")
                                Write-Host "  [OK] $envName 寫入 Windows 使用者環境變數" -ForegroundColor Green
                            }
                        }
                    }

                    if ($useGoogleApiAsDefault) {
                        if ($existingKey) {
                            Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "Google API key configured"
                        }
                        else {
                            Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "Google API chosen but no key (chat will degrade)"
                            $summary.notes.Add("沒貼 key,對話會 degraded。menu [4] 重設,或 setx GOOGLE_API_KEY <key>") | Out-Null
                        }
                    }
                }
                else {
                    Add-Step -Rows $summary.steps -Name "model-or-api" -Ok $true -Detail "user skipped"
                }
            }
        }

        # Step: 配 LLM 預設 — 優先 Google API（如果使用者選 [2]）；否則本機 gemma-4
        if ($SkipConfigureLLM) {
            Add-Step -Rows $summary.steps -Name "configure-llm" -Ok $true -Detail "skipped (-SkipConfigureLLM)"
        }
        elseif ($useGoogleApiAsDefault -and $resolvedVaultRoot) {
            # 設 global_default 為 gemma-4-31b-it（預設首選）
            $setRun = Invoke-Python -Python $python -ArgList @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $resolvedVaultRoot, "llm-set-default", "--profile", "gemini", "--model", "gemma-4-31b-it", "--json")
            if ($setRun.exit_code -eq 0) {
                Add-Step -Rows $summary.steps -Name "configure-llm" -Ok $true -Detail "global_default=gemini/gemma-4-31b-it"
            }
            else {
                Add-Step -Rows $summary.steps -Name "configure-llm" -Ok $false -Detail "llm-set-default failed: $(First-Line -Text $setRun.output)"
            }
        }
        elseif ($hasGemma -and $resolvedVaultRoot) {
            $routerYaml = Join-Path $resolvedVaultRoot "00_System\08_Runtime_Profiles\llm_router.yaml"
            $modelRefRel = "../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf"
            $cudaPath = ""
            $ollamaCuda = Join-Path $env:LOCALAPPDATA "Programs\Ollama\lib\ollama\cuda_v12"
            if (Test-Path -LiteralPath $ollamaCuda) {
                $cudaPath = $ollamaCuda -replace '\\', '/'
            }
            $configArgs = @("scripts/_set_llm_router.py", "--router-yaml", $routerYaml, "--model", $modelRefRel, "--profile", "llama_cpp_local", "--json")
            if ($cudaPath) {
                $configArgs += @("--cuda-path", $cudaPath)
            }
            $configRun = Invoke-Python -Python $python -ArgList $configArgs
            if ($configRun.exit_code -eq 0) {
                $cudaDetail = if ($cudaPath) { "cuda=$cudaPath" } else { "cuda=none (依賴系統 PATH)" }
                Add-Step -Rows $summary.steps -Name "configure-llm" -Ok $true -Detail "model=gemma-4-E4B-Q8_0 $cudaDetail"
            }
            else {
                Add-Step -Rows $summary.steps -Name "configure-llm" -Ok $false -Detail "yaml patch failed: $(First-Line -Text $configRun.output)"
            }
        }
        else {
            Add-Step -Rows $summary.steps -Name "configure-llm" -Ok $true -Detail "skipped (no model, no API selected)"
        }

        # Step: verify-llm — 真實 chat 驗證 (在 Discord setup 之前確認 LLM 可用)
        # 跳過: NonInteractive 或者前面選了 skip (沒設模型也沒設 API)
        $shouldVerifyLlm = ($resolvedVaultRoot) -and ($useGoogleApiAsDefault -or $hasGemma)
        if ($shouldVerifyLlm) {
            $verifyArgs = @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $resolvedVaultRoot, "chat", "請只回 OK", "--persona", "steward", "--context", "wizard-verify", "--session", "wizard-verify", "--allow-llm-degraded", "--json")
            $verifyRun = Invoke-Python -Python $python -ArgList $verifyArgs
            $verifyOk = $false
            $verifyDetail = "no JSON output"
            if ($verifyRun.exit_code -eq 0) {
                $rawV = [string]$verifyRun.output
                $jsStart = $rawV.IndexOf('{')
                $jsEnd = $rawV.LastIndexOf('}')
                if ($jsStart -ge 0 -and $jsEnd -gt $jsStart) {
                    try {
                        $jv = $rawV.Substring($jsStart, $jsEnd - $jsStart + 1) | ConvertFrom-Json
                        $isDeg = [bool]$jv.degraded
                        $modelUsed = [string]$jv.llm.model
                        if ($isDeg) {
                            $verifyDetail = "degraded — LLM 沒實際回應 (key 無效 / model 不支援 / 網路)"
                        }
                        else {
                            $verifyOk = $true
                            $shortModel = if ($modelUsed.Length -gt 40) { "..." + $modelUsed.Substring($modelUsed.Length - 40) } else { $modelUsed }
                            $verifyDetail = "ok — $shortModel"
                        }
                    }
                    catch {
                        $verifyDetail = "JSON parse fail"
                    }
                }
            }
            else {
                $verifyDetail = "exit=$($verifyRun.exit_code)"
            }
            Add-Step -Rows $summary.steps -Name "verify-llm" -Ok $verifyOk -Detail $verifyDetail
            if (-not $verifyOk -and -not $NonInteractive) {
                Write-Host ""
                Write-Host "  ⚠ LLM 驗證失敗: $verifyDetail" -ForegroundColor Yellow
                Write-Host "    Discord setup 仍可繼續,但 chat 時管家不會回應。" -ForegroundColor DarkGray
                Write-Host "    要先修 LLM 嗎? [Y/n]: " -NoNewline -ForegroundColor Yellow
                $fix = Ask-YesNo -Prompt "" -Default $true
                if ($fix) {
                    Write-Host ""
                    Write-Host "  可選:" -ForegroundColor Cyan
                    Write-Host "    .\scripts\switch-llm.ps1                改 model 或重貼 key" -ForegroundColor DarkGray
                    Write-Host "    .\scripts\switch-llm.ps1 -RemoveKey     移除 API key 重試" -ForegroundColor DarkGray
                    Write-Host "    .\scripts\download-model.ps1            下載本機 GGUF 走本機路徑" -ForegroundColor DarkGray
                    Write-Host ""
                    $summary.notes.Add("LLM verify 失敗 — 使用者選擇繼續 Discord,可後續用 switch-llm.ps1 修") | Out-Null
                }
            }
        }
        else {
            Add-Step -Rows $summary.steps -Name "verify-llm" -Ok $true -Detail "skipped (no LLM configured)"
        }
    }

    # Step: Discord setup — 互動模式預設會問,要用 -SetupDiscord 強制開,要 NonInteractive 才完全跳過
    $shouldRunDiscordSetup = $false
    $discordSkipReason = ""
    if ($SetupDiscord) {
        $shouldRunDiscordSetup = $true
    }
    elseif (-not $NonInteractive -and $resolvedVaultRoot) {
        # 偵測既有 relay config — 已配置過就 skip
        $existingRelayCfg = Join-Path $projectRoot "scripts/discord-relay-stack.local.json"
        if (-not (Test-Path -LiteralPath $existingRelayCfg)) {
            Write-Host ""
            Write-Host "  Discord 整合: 把管家上線到 Discord 頻道?" -ForegroundColor Cyan
            Write-Host "    需要先準備好: (a) 頻道 ID  (b) Discord Bot Token" -ForegroundColor DarkGray
            $shouldRunDiscordSetup = Ask-YesNo -Prompt "  現在設?" -Default $false
        }
    }

    if ($shouldRunDiscordSetup -and $resolvedVaultRoot) {
        $discordOk = $true
        $discordDetail = "ready"

        # ===== 先問 Token (Discord 要先有 bot 才知道往哪個 channel 講話) =====
        $tokenVal = [Environment]::GetEnvironmentVariable($DiscordTokenEnv, "Process")
        if (-not $tokenVal) {
            $tokenVal = [Environment]::GetEnvironmentVariable($DiscordTokenEnv, "User")
            if ($tokenVal) { Set-Item -LiteralPath "Env:$DiscordTokenEnv" -Value $tokenVal }
        }

        $tokenOk = $false
        if ($tokenVal) {
            # 已有 token — 顯示遮蔽 + 三選一
            $maskedT = if ($tokenVal.Length -ge 12) { $tokenVal.Substring(0, 6) + "..." + $tokenVal.Substring($tokenVal.Length - 4) } else { "***" }
            Write-Host ""
            Write-Host "  [偵測到] 已有 $DiscordTokenEnv ($maskedT)" -ForegroundColor Green
            if (-not $NonInteractive) {
                Write-Host "    [1] 沿用此 token" -ForegroundColor Yellow
                Write-Host "    [2] 重貼新 token" -ForegroundColor Yellow
                Write-Host "    [3] 移除 token 並跳過 Discord" -ForegroundColor Yellow
                while ($true) {
                    $tsub = (Read-Host "  選 [1-3]").Trim()
                    if ($tsub -in @("1", "2", "3")) { break }
                    Write-Host "  請輸入 1 / 2 / 3" -ForegroundColor Red
                }
                if ($tsub -eq "1") { $tokenOk = $true }
                elseif ($tsub -eq "3") {
                    [Environment]::SetEnvironmentVariable($DiscordTokenEnv, $null, "User")
                    Remove-Item -LiteralPath "Env:$DiscordTokenEnv" -ErrorAction SilentlyContinue
                    $tokenVal = ""
                    $tokenOk = $false
                    $shouldRunDiscordSetup = $false
                    $discordSkipReason = "removed token by user choice"
                }
                # tsub == "2" 落入下面 prompt
            }
            else {
                $tokenOk = $true
            }
        }

        if ($shouldRunDiscordSetup -and -not $tokenOk -and -not $NonInteractive) {
            Write-Host ""
            Write-Host "  Discord Bot Token (從 Discord Developer Portal → Bot → Token 取得)" -ForegroundColor Yellow
            Write-Host "  (輸入時不會顯示, Enter 確認, 留空跳過 Discord)" -ForegroundColor DarkGray
            $sec = Read-Host -Prompt "  $DiscordTokenEnv" -AsSecureString
            if ($sec -and $sec.Length -gt 0) {
                $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
                try {
                    $tokenVal = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
                }
                finally {
                    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                }
                Set-Item -LiteralPath "Env:$DiscordTokenEnv" -Value $tokenVal
                # 3-way: .env / setx / 只此次
                Write-Host ""
                Write-Host "  記住 token 到哪裡?" -ForegroundColor Yellow
                Write-Host "    [1] .env 檔 (推薦,純檔案,gitignored,好刪除)" -ForegroundColor White
                Write-Host "    [2] Windows 使用者環境變數 (registry,全域)" -ForegroundColor White
                Write-Host "    [3] 只此次有效" -ForegroundColor DarkGray
                while ($true) {
                    $tokenPersist = (Read-Host "  選 [1-3]").Trim()
                    if ($tokenPersist -in @("1", "2", "3")) { break }
                    Write-Host "  請輸入 1 / 2 / 3" -ForegroundColor Red
                }
                if ($tokenPersist -eq "1") {
                    $envPath = Save-EntryToDotEnv -Key $DiscordTokenEnv -Value $tokenVal
                    Write-Host "  [OK] $DiscordTokenEnv 寫入 $envPath" -ForegroundColor Green
                }
                elseif ($tokenPersist -eq "2") {
                    [Environment]::SetEnvironmentVariable($DiscordTokenEnv, $tokenVal, "User")
                    Write-Host "  [OK] $DiscordTokenEnv 寫入 Windows 使用者環境變數" -ForegroundColor Green
                }
                $tokenOk = $true
            }
            else {
                # 留空 → 跳過 Discord
                $shouldRunDiscordSetup = $false
                $discordSkipReason = "no token provided"
            }
        }
    }

    # ===== 再問 Channel ID (token 完成才有意義) =====
    if ($shouldRunDiscordSetup -and $resolvedVaultRoot) {
        $effectiveChannelId = $DiscordChannelId
        if (-not $effectiveChannelId -and -not $NonInteractive) {
            Write-Host ""
            Write-Host "  Discord 頻道 ID (在 Discord 啟用開發者模式後對頻道右鍵→複製 ID)" -ForegroundColor Yellow
            $effectiveChannelId = (Read-Host "  channel_id").Trim()
        }
        if ([string]::IsNullOrWhiteSpace($effectiveChannelId)) {
            Add-Step -Rows $summary.steps -Name "discord-setup" -Ok $true -Detail "skipped (no channel id)"
        }
        else {
            $relayConfig = [ordered]@{
                bridge_url = "http://127.0.0.1:16000"
                python_exe = "python"
                allow_llm_degraded = $true
                disable_message_content_intent = $false
                timeout_sec = 120
                notes = [ordered]@{
                    generated_by = "first-run-wizard"
                    credentials_must_be_env_var_only = $true
                }
                relays = @(
                    [ordered]@{
                        name = "$DiscordPersona-relay"
                        token_env = $DiscordTokenEnv
                        mode = "executor"
                        persona = $DiscordPersona
                        channel_ids = @($effectiveChannelId)
                    }
                )
            }
            $relayPath = Join-Path $projectRoot "scripts/discord-relay-stack.local.json"
            ($relayConfig | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $relayPath -Encoding UTF8

            $bindRun = Invoke-Python -Python $python -ArgList @("-X", "utf8", "-m", "agent_memory.cli", "--vault-root", $resolvedVaultRoot, "channel-bind", "--transport", "discord", "--channel-id", $effectiveChannelId, "--persona", $DiscordPersona, "--operator", "first-run-wizard", "--json")
            $bindOk = ($bindRun.exit_code -eq 0)
            if (-not $bindOk) {
                Add-Step -Rows $summary.steps -Name "discord-setup" -Ok $false -Detail "channel-bind failed: $(First-Line -Text $bindRun.output)"
            }
            else {
                $tokenStatus = if ($tokenOk) { "token=ok" } else { "token=missing" }
                Add-Step -Rows $summary.steps -Name "discord-setup" -Ok $true -Detail "channel=$effectiveChannelId persona=$DiscordPersona $tokenStatus"
            }
        }
    }
    else {
        $reason = if ($discordSkipReason) { $discordSkipReason } else { "user declined or NonInteractive" }
        Add-Step -Rows $summary.steps -Name "discord-setup" -Ok $true -Detail "skipped ($reason)"
    }
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

if ($summary.overall_ok) {
    $vaultForOutput = ""
    if ($summary.bootstrap -and $summary.bootstrap.second_brain_root) {
        $vaultForOutput = [string]$summary.bootstrap.second_brain_root
    }

    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Host " 核心已就緒 / Core ready" -ForegroundColor Cyan
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host " 第二大腦 vault: $vaultForOutput"
    Write-Host ""
    Write-Host " [1] 立刻和管家對話一次（驗證核心可用，模型未下載會 degraded）：" -ForegroundColor Yellow
    Write-Host "     python -X utf8 -m agent_memory.cli chat `"hello`" --persona steward --context first-run --session smoke-1 --allow-llm-degraded"
    Write-Host ""
    Write-Host " [2] 啟動 Bridge（Discord/LINE/Web 都會走這個 :16000 入口）：" -ForegroundColor Yellow
    Write-Host "     .\scripts\run-bridge.ps1 -Port 16000"
    Write-Host ""
    Write-Host " [3] 跑工具能力 smoke（驗證 steward 可用 /tool 寫檔）：" -ForegroundColor Yellow
    Write-Host "     .\scripts\run-tooling-smoke.ps1 -Json"
    Write-Host ""
    Write-Host " [3.5] 換用線上 API 模型（OpenAI / Gemini / OpenRouter / Claude）：" -ForegroundColor Yellow
    Write-Host "       .\scripts\switch-llm.ps1                # 互動選號碼"
    Write-Host "       .\scripts\switch-llm.ps1 -PersistKey     # 同上，API key 寫入 user 環境變數，下次自動載入"
    Write-Host ""

    $hasDiscordStep = $false
    foreach ($row in $summary.steps) {
        if ($row.name -eq "discord-setup" -and $row.detail -notlike "skipped*") {
            $hasDiscordStep = $true
            break
        }
    }
    # 也偵測既有 relay 設定（之前跑過 wizard 留下的）。
    $existingRelayConfig = Join-Path $projectRoot "scripts/discord-relay-stack.local.json"
    $hasExistingRelay = Test-Path -LiteralPath $existingRelayConfig

    if ($hasDiscordStep -or $hasExistingRelay) {
        Write-Host " [4] 上線管家到 Discord（一鍵）：" -ForegroundColor Yellow
        Write-Host "     .\scripts\start-steward.ps1                # 第一次會 prompt 你貼 token"
        Write-Host "     .\scripts\start-steward.ps1 -PersistToken   # 同上，但記住 token，下次自動載入"
        if ($hasExistingRelay -and -not $hasDiscordStep) {
            Write-Host "     (偵測到既有 discord-relay-stack.local.json，可直接使用)" -ForegroundColor DarkGray
        }
        Write-Host ""
    }
    else {
        Write-Host " [4] (選配) 之後想串 Discord：等下會問你，或重跑 wizard 加 -SetupDiscord。" -ForegroundColor DarkGray
        Write-Host ""
    }
    Write-Host "===============================================================" -ForegroundColor Cyan
    # 結尾的 chat smoke 已移到 verify-llm step (在 Discord setup 之前),這裡不再重複。

    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Host " 完成！可以關掉這個視窗，或按任意鍵結束。" -ForegroundColor Green
    Write-Host "===============================================================" -ForegroundColor Cyan
    exit 0
}
else {
    exit 1
}
