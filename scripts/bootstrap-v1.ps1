param(
    [string]$SecondBrainRoot = "",
    [string]$ModelsRoot = "",
    [string]$PythonExe = "python",
    [int]$BridgePort = 16000,
    [switch]$SetDefaultVault,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Force UTF-8 IO encoding (critical for CJK Windows where console codepage is CP-950/936).
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
}
catch { }

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Resolve-OrCreatePath {
    param([string]$PathValue)
    if (-not (Test-Path -LiteralPath $PathValue)) {
        New-Item -ItemType Directory -Path $PathValue -Force | Out-Null
    }
    return (Resolve-Path $PathValue).Path
}

function Assert-OutsideProjectRoot {
    param(
        [string]$AbsPath,
        [string]$Label
    )
    $root = (Resolve-Path $projectRoot).Path.TrimEnd("\\")
    $target = $AbsPath.TrimEnd("\\")
    if ($target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be outside project root: $target"
    }
}

function Invoke-JsonCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    $stdout = & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "command failed: $FilePath $($Arguments -join ' ')"
    }
    $raw = ""
    if ($stdout -is [array]) {
        $raw = [string]::Join([Environment]::NewLine, $stdout)
    }
    else {
        $raw = [string]$stdout
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return ($raw | ConvertFrom-Json)
}

$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    # 防呆：Get-Command 對非 PATH 的絕對路徑偶爾會 miss，但檔案實際存在。
    # 改用 Test-Path 二次確認，避免誤殺。
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Python executable not found: ${PythonExe}."
    }
}

if (-not $SecondBrainRoot) {
    $SecondBrainRoot = Join-Path $projectRoot "..\\SecondBrains\\default_second_brain"
}
if (-not $ModelsRoot) {
    $ModelsRoot = Join-Path $projectRoot "..\\0_Models"
}

$secondBrainAbs = Resolve-OrCreatePath -PathValue $SecondBrainRoot
$modelsAbs = Resolve-OrCreatePath -PathValue $ModelsRoot

Assert-OutsideProjectRoot -AbsPath $secondBrainAbs -Label "SecondBrainRoot"
Assert-OutsideProjectRoot -AbsPath $modelsAbs -Label "ModelsRoot"

$bootstrapDir = Join-Path $projectRoot "artifacts/bootstrap"
if (-not (Test-Path -LiteralPath $bootstrapDir)) {
    New-Item -ItemType Directory -Path $bootstrapDir -Force | Out-Null
}
$onboardingConfigPath = Join-Path $bootstrapDir "bootstrap-onboarding.local.json"

$onboardingConfig = [ordered]@{
    vault_root = $secondBrainAbs
    template_vault = ""
    owner_id = "owner"
    brain_id = ""
    set_default_vault = [bool]$SetDefaultVault
    channel_default_persona = "steward"
    seed = [ordered]@{
        overwrite = $false
        include_shared_skills = $false
        skip_personas = $false
        skip_persona_skills = $false
        skip_dialogue_modes = $false
    }
    butler = [ordered]@{
        enabled = $true
        persona_id = "steward"
        display_name = "Steward"
        mission = "Act as the steward persona: setup environment, manage personas, and run tool-enabled coding tasks with traceable outputs."
        style = "concise"
        language = "zh-Hant"
        role_type = "tooling"
        default_mode = "executor"
        operator = "bootstrap-v1"
    }
    channels = @()
    run_smoke = $false
}
($onboardingConfig | ConvertTo-Json -Depth 12) | Set-Content -Path $onboardingConfigPath -Encoding UTF8

$onboardingScript = Join-Path $projectRoot "scripts/setup-first-run-onboarding.ps1"
$onboardingResult = Invoke-JsonCommand -FilePath "powershell" -Arguments @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $onboardingScript,
    "-PythonExe", $PythonExe,
    "-ConfigFile", $onboardingConfigPath,
    "-Json"
)

$entryConfigPath = Join-Path $projectRoot "scripts/entry-stack.local.json"
if (-not (Test-Path -LiteralPath $entryConfigPath)) {
    Copy-Item -Path (Join-Path $projectRoot "scripts/entry-stack.sample.json") -Destination $entryConfigPath
}

$summary = [ordered]@{
    started_at = (Get-Date).ToString("o")
    project_root = $projectRoot
    second_brain_root = $secondBrainAbs
    models_root = $modelsAbs
    onboarding_config = $onboardingConfigPath
    onboarding = $onboardingResult
    defaults = [ordered]@{
        bridge_port = $BridgePort
        recommended_model_paths = [ordered]@{
            gemma4 = (Join-Path $modelsAbs "gemma-4-E4B-it-GGUF\\gemma-4-E4B-it-Q8_0.gguf")
            qwen8b = (Join-Path $modelsAbs "Qwen3.5-9B-GGUF\\Qwen3.5-9B-Q8_0.gguf")
        }
    }
    next_steps = @(
        "1) Download models into ModelsRoot.",
        "2) Edit scripts/entry-stack.local.json with model paths and channel settings.",
        "3) Run scripts/setup-entry-stack.ps1 to bind entry config.",
        "4) Start bridge: scripts/run-bridge.ps1 -VaultRoot '$secondBrainAbs' -Port $BridgePort",
        "5) Start relay: scripts/manage-discord-relay-stack.ps1 -Action start -ConfigFile scripts/discord-relay-stack.local.json"
    )
}

if ($Json) {
    $summary | ConvertTo-Json -Depth 16
    exit 0
}

Write-Host "[OK] bootstrap-v1 completed." -ForegroundColor Green
Write-Host "[INFO] second_brain_root=$secondBrainAbs"
Write-Host "[INFO] models_root=$modelsAbs"
Write-Host "[INFO] bridge_default_port=$BridgePort"
Write-Host "[INFO] gemma4_path=$(Join-Path $modelsAbs 'gemma-4-E4B-it-GGUF\\gemma-4-E4B-it-Q8_0.gguf')"
Write-Host "[INFO] qwen8b_path=$(Join-Path $modelsAbs 'Qwen3.5-9B-GGUF\\Qwen3.5-9B-Q8_0.gguf')"
