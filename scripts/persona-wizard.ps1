<#
.SYNOPSIS
  Persona Manager — 列表 / 建立 / 改名更新 / 停用 / 設模型.

.DESCRIPTION
  V2 Phase A C8. 對應藍圖 §1.1 「persona-in-vault」模型:
    一個 vault 容納多個分身, 各綁不同 model + 不同 channel,
    記憶共用 10_Permanent, 但 session log 跟私有 skill 沙盒按 persona_id 隔離.

  本 wizard 是 memory-cli 內 persona-* CLI 的互動 UI 封裝:
    persona-create --auto-approve | persona-update | persona-disable | persona-list
    llm-set-persona  (per-persona 模型 override)
#>

param(
    [string]$VaultRoot = ""
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
}
catch { }

. (Join-Path $PSScriptRoot "_dotenv-helper.ps1")
Import-DotEnvIntoProcess -VaultRoot $VaultRoot | Out-Null

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = $env:PYTHON_EXE
if (-not $pythonExe) { $pythonExe = "python" }

function Invoke-PersonaCli {
    # R12 C46: param name 從 $Args 改 $CliArgs — 避免跟 PowerShell 自動變數衝突 (Codex T8.2 FAIL)
    param([string[]]$CliArgs)
    $full = @("-X", "utf8", "-m", "agent_memory.cli")
    if ($VaultRoot) { $full += @("--vault-root", $VaultRoot) }
    $full += $CliArgs
    $full += "--json"
    Push-Location $projectRoot
    try {
        $output = & $pythonExe @full 2>&1 | Out-String
    }
    finally {
        Pop-Location
    }
    $jsonStart = $output.IndexOf('{')
    $jsonEnd = $output.LastIndexOf('}')
    if ($jsonStart -ge 0 -and $jsonEnd -gt $jsonStart) {
        $jsonRaw = $output.Substring($jsonStart, $jsonEnd - $jsonStart + 1)
        try {
            return @{ ok = $true; data = ($jsonRaw | ConvertFrom-Json); raw = $output }
        }
        catch {
            return @{ ok = $false; error = "JSON parse"; raw = $output }
        }
    }
    return @{ ok = $false; error = "no json"; raw = $output }
}

function Show-PersonaList {
    Write-Host ""
    Write-Host "  目前已啟用的人格 (registry.yaml):" -ForegroundColor Cyan
    $r = Invoke-PersonaCli -CliArgs @("persona-list")
    if (-not $r.ok) {
        Write-Host "  [ERR] persona-list 失敗: $($r.error)" -ForegroundColor Red
        Write-Host "  $($r.raw)" -ForegroundColor DarkGray
        return @()
    }
    $rows = @()
    $personas = @($r.data.personas)
    if ($personas.Count -eq 0) {
        Write-Host "  (尚無 persona)" -ForegroundColor DarkGray
        return @()
    }
    foreach ($p in $personas) {
        $rows += [pscustomobject]@{
            persona_id   = [string]$p.persona_id
            display_name = [string]$p.display_name
            status       = [string]$p.status
            role_type    = [string]$p.role_type
        }
    }
    $idx = 1
    foreach ($p in $rows) {
        $statusLabel = if ($p.status -eq "active") { "✓" } else { "✗ ($($p.status))" }
        Write-Host ("    [{0}] {1,-12} — {2,-20} {3}" -f $idx, $p.persona_id, $p.display_name, $statusLabel) -ForegroundColor White
        $idx++
    }
    return $rows
}

function Read-NonEmpty {
    param([string]$Prompt, [string]$Default = "")
    while ($true) {
        $val = (Read-Host $Prompt).Trim()
        if (-not $val -and $Default) { return $Default }
        if ($val) { return $val }
        Write-Host "  此項不能空" -ForegroundColor Yellow
    }
}

function Read-YesNo {
    param([string]$Prompt, [bool]$Default = $true)
    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    $val = (Read-Host "$Prompt $suffix").Trim().ToLower()
    if (-not $val) { return $Default }
    return ($val -in @("y", "yes", "1", "true"))
}

function Do-CreatePersona {
    Write-Host ""
    Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Cyan
    Write-Host "  │ 建立新人格 (新分身)                              │" -ForegroundColor Cyan
    Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Cyan

    $displayName = Read-NonEmpty -Prompt "  顯示名稱 (例如: 程式碼助手 / 寫作者)"

    Write-Host ""
    Write-Host "  persona_id (英數+底線, 用於檔案路徑, 不可改, 留空自動 normalize)" -ForegroundColor DarkGray
    $personaIdInput = (Read-Host "  persona_id").Trim()
    $personaArgs = @("persona-create", "--display-name", $displayName, "--auto-approve")
    if ($personaIdInput) { $personaArgs += @("--persona-id", $personaIdInput) }

    Write-Host ""
    $mission = Read-NonEmpty -Prompt "  mission (1-2 句說明此角色的核心任務)"
    $personaArgs += @("--mission", $mission)

    Write-Host ""
    Write-Host "  風格 (預設 concise = 精簡。也可寫 detailed / casual / formal)" -ForegroundColor DarkGray
    $style = (Read-Host "  style (Enter 用預設 concise)").Trim()
    if ($style) { $personaArgs += @("--style", $style) }

    Write-Host ""
    $enableTools = Read-YesNo -Prompt "  啟用工具能力? (允許自主寫記憶 + 寫 skill, tooling 角色建議 Y)" -Default $true
    if ($enableTools) {
        $personaArgs += @("--enable-tools", "--role-type", "tooling")
    } else {
        $personaArgs += @("--disable-tools", "--role-type", "chat")
    }

    Write-Host ""
    Write-Host "  即將執行:" -ForegroundColor Yellow
    Write-Host "    memory-cli $($personaArgs -join ' ')" -ForegroundColor DarkGray
    $go = Read-YesNo -Prompt "  確認建立?" -Default $true
    if (-not $go) {
        Write-Host "  [取消]" -ForegroundColor Yellow
        return
    }

    $r = Invoke-PersonaCli -CliArgs $personaArgs
    if ($r.ok) {
        Write-Host "  ✓ 已建立並核准." -ForegroundColor Green
        if ($r.data.persona_id) {
            $newId = $r.data.persona_id
            Write-Host "    persona_id = $newId" -ForegroundColor DarkGray
            Write-Host ""

            $setModel = Read-YesNo -Prompt "  要立即為這個 persona 綁定模型嗎? (留預設 = 用 global_default)" -Default $false
            if ($setModel) {
                Write-Host ""
                Write-Host "  可用模型 preset:" -ForegroundColor DarkGray
                Write-Host "    gemma4 / qwen9 / qwen30 / gemini / gemini-pro / gemma-31b / gemma-26b" -ForegroundColor DarkGray
                $modelKey = (Read-Host "  選一個 preset key (Enter 取消)").Trim()
                if ($modelKey) {
                    $llmArgs = @("llm-set-persona", "--persona", $newId, "--key", $modelKey)
                    $r2 = Invoke-PersonaCli -CliArgs $llmArgs
                    if ($r2.ok) {
                        Write-Host "  ✓ 模型已綁定." -ForegroundColor Green
                    } else {
                        Write-Host "  ✗ 綁定失敗: $($r2.error)" -ForegroundColor Red
                        Write-Host "  $($r2.raw)" -ForegroundColor DarkGray
                    }
                }
            }
        }
    } else {
        Write-Host "  ✗ 失敗: $($r.error)" -ForegroundColor Red
        Write-Host "  $($r.raw)" -ForegroundColor DarkGray
    }
}

function Do-UpdatePersona {
    $rows = Show-PersonaList
    if ($rows.Count -eq 0) { return }
    Write-Host ""
    $pick = (Read-Host "  選號碼 (Enter 取消)").Trim()
    if (-not $pick) { return }
    $picked = [int]$pick - 1
    if ($picked -lt 0 -or $picked -ge $rows.Count) {
        Write-Host "  無效選擇" -ForegroundColor Red
        return
    }
    $target = $rows[$picked]
    Write-Host ""
    Write-Host "  目標 persona_id = $($target.persona_id) (display = $($target.display_name))" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "    [1] 改 display_name" -ForegroundColor White
    Write-Host "    [2] 改 mission" -ForegroundColor White
    Write-Host "    [3] 啟用工具能力" -ForegroundColor White
    Write-Host "    [4] 停用工具能力" -ForegroundColor White
    Write-Host "    [B] 回上層" -ForegroundColor DarkGray
    $sub = (Read-Host "  選").Trim().ToUpper()

    $updateArgs = @("persona-update", "--persona", $target.persona_id)
    switch ($sub) {
        "1" {
            $newName = Read-NonEmpty -Prompt "  新顯示名稱"
            $updateArgs += @("--display-name", $newName)
        }
        "2" {
            $newMission = Read-NonEmpty -Prompt "  新 mission"
            $updateArgs += @("--mission", $newMission)
        }
        "3" { $updateArgs += @("--enable-tools") }
        "4" { $updateArgs += @("--disable-tools") }
        default { Write-Host "  [取消]" -ForegroundColor Yellow; return }
    }
    $r = Invoke-PersonaCli -CliArgs $updateArgs
    if ($r.ok) {
        Write-Host "  ✓ 更新成功." -ForegroundColor Green
    } else {
        Write-Host "  ✗ 失敗: $($r.error)" -ForegroundColor Red
        Write-Host "  $($r.raw)" -ForegroundColor DarkGray
    }
}

function Do-DisablePersona {
    $rows = Show-PersonaList
    if ($rows.Count -eq 0) { return }
    Write-Host ""
    $pick = (Read-Host "  停用哪一個? 選號碼 (Enter 取消)").Trim()
    if (-not $pick) { return }
    $picked = [int]$pick - 1
    if ($picked -lt 0 -or $picked -ge $rows.Count) {
        Write-Host "  無效選擇" -ForegroundColor Red
        return
    }
    $target = $rows[$picked]
    if ($target.persona_id -in @("core", "steward")) {
        Write-Host ""
        Write-Host "  ⚠ 警告: $($target.persona_id) 是系統核心 persona, 停用可能影響整個 vault 功能" -ForegroundColor Yellow
    }
    $reason = (Read-Host "  停用原因 (留空也可)").Trim()
    $confirm = Read-YesNo -Prompt "  確認停用 $($target.persona_id) ?" -Default $false
    if (-not $confirm) { Write-Host "  [取消]" -ForegroundColor Yellow; return }
    $disableArgs = @("persona-disable", "--persona", $target.persona_id)
    if ($reason) { $disableArgs += @("--reason", $reason) }
    $r = Invoke-PersonaCli -CliArgs $disableArgs
    if ($r.ok) {
        Write-Host "  ✓ 已停用 (檔案仍保留, 可重新核准恢復)." -ForegroundColor Green
    } else {
        Write-Host "  ✗ 失敗: $($r.error)" -ForegroundColor Red
        Write-Host "  $($r.raw)" -ForegroundColor DarkGray
    }
}

function Do-SetModel {
    $rows = Show-PersonaList
    if ($rows.Count -eq 0) { return }
    Write-Host ""
    $pick = (Read-Host "  幫哪個 persona 設模型? 選號碼 (Enter 取消)").Trim()
    if (-not $pick) { return }
    $picked = [int]$pick - 1
    if ($picked -lt 0 -or $picked -ge $rows.Count) {
        Write-Host "  無效選擇" -ForegroundColor Red
        return
    }
    $target = $rows[$picked]
    Write-Host ""
    Write-Host "  可用 preset key:" -ForegroundColor DarkGray
    Write-Host "    本機: gemma4 / qwen9 / qwen30" -ForegroundColor DarkGray
    Write-Host "    雲端: gemini (Flash) / gemini-pro / gemma-31b / gemma-26b" -ForegroundColor DarkGray
    $modelKey = Read-NonEmpty -Prompt "  選 preset key"
    $r = Invoke-PersonaCli -CliArgs @("llm-set-persona", "--persona", $target.persona_id, "--key", $modelKey)
    if ($r.ok) {
        Write-Host "  ✓ 已綁定 $($target.persona_id) → $modelKey" -ForegroundColor Green
    } else {
        Write-Host "  ✗ 失敗: $($r.error)" -ForegroundColor Red
        Write-Host "  $($r.raw)" -ForegroundColor DarkGray
    }
}

# Main loop
while ($true) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  Persona Manager — 多分身設定" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan

    Show-PersonaList | Out-Null

    Write-Host ""
    Write-Host "  操作:" -ForegroundColor Yellow
    Write-Host "    [N] 建立新角色" -ForegroundColor White
    Write-Host "    [U] 改名 / 改 mission / 切換工具能力" -ForegroundColor White
    Write-Host "    [D] 停用角色" -ForegroundColor White
    Write-Host "    [M] 為角色設定模型 (per-persona override)" -ForegroundColor White
    Write-Host "    [Q] 回主選單" -ForegroundColor DarkGray
    Write-Host ""
    $choice = (Read-Host "  選 [N/U/D/M/Q]").Trim().ToUpper()

    switch ($choice) {
        "N" { Do-CreatePersona }
        "U" { Do-UpdatePersona }
        "D" { Do-DisablePersona }
        "M" { Do-SetModel }
        "Q" { return }
        default { Write-Host "  無效選擇" -ForegroundColor Red }
    }
}
