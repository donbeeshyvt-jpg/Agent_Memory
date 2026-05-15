param(
    [ValidateSet("start", "stop", "restart", "status", "stop-stray")]
    [string]$Action = "status",
    [string]$ConfigFile = "",
    [string]$StateFile = "",
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not $ConfigFile) {
    $ConfigFile = Join-Path $PSScriptRoot "discord-relay-stack.local.json"
}
if (-not $StateFile) {
    $StateFile = Join-Path $projectRoot "artifacts/discord-relay-stack/state.json"
}

function Get-OptionalValue {
    param(
        [object]$Source,
        [string]$Name,
        [object]$Default
    )
    if ($null -eq $Source) {
        return $Default
    }
    if ($Source.PSObject.Properties.Name -contains $Name) {
        $value = $Source.$Name
        if ($null -ne $value) {
            return $value
        }
    }
    return $Default
}

function To-StringArray {
    param(
        [object]$InputObject
    )
    if ($null -eq $InputObject) {
        return @()
    }
    if ($InputObject -is [string]) {
        $single = [string]$InputObject
        if ([string]::IsNullOrWhiteSpace($single)) {
            return @()
        }
        return @($single.Trim())
    }

    $values = New-Object System.Collections.Generic.List[string]
    if ($InputObject -is [System.Collections.IEnumerable]) {
        foreach ($item in $InputObject) {
            if ($null -eq $item) {
                continue
            }
            $text = [string]$item
            if ([string]::IsNullOrWhiteSpace($text)) {
                continue
            }
            $values.Add($text.Trim())
        }
    }
    else {
        $text = [string]$InputObject
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            $values.Add($text.Trim())
        }
    }
    return @($values.ToArray())
}

function Ensure-ParentDirectory {
    param(
        [string]$Path
    )
    $parent = Split-Path -Parent $Path
    if (-not $parent) {
        return
    }
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}

function Read-JsonFile {
    param(
        [string]$Path,
        [bool]$Required = $true
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        if ($Required) {
            throw "JSON file not found: $Path"
        }
        return $null
    }
    $raw = Get-Content -Path $Path -Encoding UTF8 -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        if ($Required) {
            throw "JSON file is empty: $Path"
        }
        return $null
    }
    return ($raw | ConvertFrom-Json)
}

function Write-JsonFile {
    param(
        [string]$Path,
        [object]$Payload
    )
    Ensure-ParentDirectory -Path $Path
    ($Payload | ConvertTo-Json -Depth 10) | Set-Content -Path $Path -Encoding UTF8
}

function Test-PidRunning {
    param(
        [int]$ProcessId
    )
    if ($ProcessId -le 0) {
        return $false
    }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    return $null -ne $proc
}

function Build-RelaySpec {
    param(
        [object]$Relay,
        [object]$Global
    )
    $name = [string](Get-OptionalValue -Source $Relay -Name "name" -Default "")
    $tokenEnv = [string](Get-OptionalValue -Source $Relay -Name "token_env" -Default "")
    if ([string]::IsNullOrWhiteSpace($name)) {
        throw "relay.name is required in config."
    }
    if ([string]::IsNullOrWhiteSpace($tokenEnv)) {
        throw "relay.token_env is required for relay '$name'."
    }

    $bridgeUrl = [string](Get-OptionalValue -Source $Relay -Name "bridge_url" -Default (Get-OptionalValue -Source $Global -Name "bridge_url" -Default "http://127.0.0.1:16000"))
    $pythonExe = [string](Get-OptionalValue -Source $Relay -Name "python_exe" -Default (Get-OptionalValue -Source $Global -Name "python_exe" -Default "python"))
    $mode = [string](Get-OptionalValue -Source $Relay -Name "mode" -Default (Get-OptionalValue -Source $Global -Name "mode" -Default "standard"))
    $persona = [string](Get-OptionalValue -Source $Relay -Name "persona" -Default "")
    $timeoutSec = [int](Get-OptionalValue -Source $Relay -Name "timeout_sec" -Default (Get-OptionalValue -Source $Global -Name "timeout_sec" -Default 90))
    if ($timeoutSec -lt 5) {
        $timeoutSec = 5
    }

    $allowDegraded = [bool](Get-OptionalValue -Source $Relay -Name "allow_llm_degraded" -Default (Get-OptionalValue -Source $Global -Name "allow_llm_degraded" -Default $true))
    $disableMessageIntent = [bool](Get-OptionalValue -Source $Relay -Name "disable_message_content_intent" -Default (Get-OptionalValue -Source $Global -Name "disable_message_content_intent" -Default $false))

    # PS5.1 function return 經過單一賦值 caller 會解包單元素陣列為 scalar / $null,
    # 必須 @() wrap 才能保證收到 array 並能用 .Count
    $channelIds = @(To-StringArray -InputObject (Get-OptionalValue -Source $Relay -Name "channel_ids" -Default @()))
    $mentionOnlyIds = @(To-StringArray -InputObject (Get-OptionalValue -Source $Relay -Name "mention_only_channel_ids" -Default @()))
    if ($channelIds.Count -eq 0) {
        throw "relay.channel_ids is required for relay '$name'."
    }

    $args = @(
        "-X", "utf8",
        ".\scripts\discord_bridge_relay.py",
        "--token-env", $tokenEnv,
        "--bridge-url", $bridgeUrl,
        "--mode", $mode,
        "--timeout", [string]$timeoutSec
    )
    if (-not [string]::IsNullOrWhiteSpace($persona)) {
        $args += @("--persona", $persona)
    }
    foreach ($cid in $channelIds) {
        $args += @("--channel-id", $cid)
    }
    foreach ($mid in $mentionOnlyIds) {
        $args += @("--mention-only-channel-id", $mid)
    }
    if ($allowDegraded) {
        $args += "--allow-llm-degraded"
    }
    if ($disableMessageIntent) {
        $args += "--disable-message-content-intent"
    }

    return [ordered]@{
        name = $name
        token_env = $tokenEnv
        bridge_url = $bridgeUrl
        python_exe = $pythonExe
        mode = $mode
        persona = $persona
        timeout_sec = $timeoutSec
        allow_llm_degraded = $allowDegraded
        disable_message_content_intent = $disableMessageIntent
        channel_ids = $channelIds
        mention_only_channel_ids = $mentionOnlyIds
        args = $args
    }
}

function Build-SpecsFromConfig {
    param(
        [object]$Config
    )
    # PS5.1 從 PSCustomObject property 抓單元素陣列時會解包成單一物件
    # 用 @(...) 強制當成陣列，避免 [System.Collections.IEnumerable] 判斷誤觸發
    $relayNodes = @(Get-OptionalValue -Source $Config -Name "relays" -Default @())
    if ($relayNodes.Count -eq 0) {
        throw "config.relays must contain at least one relay (got empty array)."
    }
    $specs = New-Object System.Collections.ArrayList
    foreach ($relayNode in $relayNodes) {
        if ($null -eq $relayNode) { continue }
        $spec = Build-RelaySpec -Relay $relayNode -Global $Config
        $specs.Add($spec) | Out-Null
    }
    return @($specs)
}

function Load-StateOrEmpty {
    param(
        [string]$Path
    )
    $state = Read-JsonFile -Path $Path -Required:$false
    if ($null -eq $state) {
        return [ordered]@{
            updated_at = ""
            action = ""
            relays = @()
        }
    }
    $relays = To-StringArray -InputObject @()
    if ($state.PSObject.Properties.Name -contains "relays" -and $state.relays -is [System.Collections.IEnumerable]) {
        $relays = $state.relays
    }
    return [ordered]@{
        updated_at = [string](Get-OptionalValue -Source $state -Name "updated_at" -Default "")
        action = [string](Get-OptionalValue -Source $state -Name "action" -Default "")
        relays = @($relays)
    }
}

function Stop-RelayPids {
    param(
        [object[]]$RelayRows
    )
    $stopped = New-Object System.Collections.ArrayList
    foreach ($row in $RelayRows) {
        $name = [string](Get-OptionalValue -Source $row -Name "name" -Default "")
        $pidValue = 0
        try {
            $pidValue = [int](Get-OptionalValue -Source $row -Name "pid" -Default 0)
        }
        catch {
            $pidValue = 0
        }
        $wasRunning = Test-PidRunning -ProcessId $pidValue
        if ($wasRunning) {
            Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
        }
        $stopped.Add([ordered]@{
            name = $name
            pid = $pidValue
            was_running = $wasRunning
            now_running = (Test-PidRunning -ProcessId $pidValue)
        }) | Out-Null
    }
    return @($stopped)
}

function Stop-StrayRelayProcesses {
    $rows = New-Object System.Collections.ArrayList
    $targets = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "python.exe" -and (
            $_.CommandLine -match "discord_bridge_relay.py" -or
            $_.CommandLine -match "serve-transport-bridge"
        )
    }
    foreach ($proc in $targets) {
        $pidValue = 0
        try {
            $pidValue = [int]$proc.ProcessId
        }
        catch {
            $pidValue = 0
        }
        $cmd = [string](Get-OptionalValue -Source $proc -Name "CommandLine" -Default "")
        $kind = if ($cmd -match "discord_bridge_relay.py") { "discord-relay" } else { "transport-bridge" }
        if ($pidValue -gt 0) {
            Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
        }
        $rows.Add([ordered]@{
            pid = $pidValue
            kind = $kind
            stopped = -not (Test-PidRunning -ProcessId $pidValue)
            command = $cmd
        }) | Out-Null
    }
    return @($rows)
}

$config = $null
$specs = @()
if ($Action -ne "stop-stray") {
    if (-not (Test-Path -LiteralPath $ConfigFile)) {
        throw "Config file not found: $ConfigFile`nPlease copy scripts/discord-relay-stack.sample.json to a local config and fill required values."
    }
    $config = Read-JsonFile -Path $ConfigFile -Required:$true
    $specs = Build-SpecsFromConfig -Config $config
}
$existingState = Load-StateOrEmpty -Path $StateFile
$resolvedConfigPath = $ConfigFile
if (Test-Path -LiteralPath $ConfigFile) {
    $resolvedConfigPath = (Resolve-Path $ConfigFile).Path
}

$result = [ordered]@{
    action = $Action
    timestamp = (Get-Date).ToString("o")
    config_file = $resolvedConfigPath
    state_file = $StateFile
    ok = $true
    items = New-Object System.Collections.ArrayList
    note = ""
}

switch ($Action) {
    "stop-stray" {
        $stoppedRows = Stop-StrayRelayProcesses
        foreach ($row in $stoppedRows) {
            $result.items.Add($row) | Out-Null
        }
        if (Test-Path -LiteralPath $StateFile) {
            Remove-Item -LiteralPath $StateFile -Force
        }
        $result.note = "stopped stray relay/bridge processes"
    }
    "stop" {
        $rows = @()
        if ($existingState.relays -is [System.Collections.IEnumerable]) {
            $rows = @($existingState.relays)
        }
        $stoppedRows = Stop-RelayPids -RelayRows $rows
        foreach ($row in $stoppedRows) {
            $result.items.Add($row) | Out-Null
        }
        if (Test-Path -LiteralPath $StateFile) {
            Remove-Item -LiteralPath $StateFile -Force
        }
        $result.note = "relay stack stopped"
    }
    "status" {
        $stateByName = @{}
        foreach ($row in $existingState.relays) {
            $name = [string](Get-OptionalValue -Source $row -Name "name" -Default "")
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $stateByName[$name] = $row
            }
        }
        foreach ($spec in $specs) {
            $row = $stateByName[[string]$spec.name]
            $pidValue = 0
            if ($null -ne $row) {
                try {
                    $pidValue = [int](Get-OptionalValue -Source $row -Name "pid" -Default 0)
                }
                catch {
                    $pidValue = 0
                }
            }
            $running = Test-PidRunning -ProcessId $pidValue
            $result.items.Add([ordered]@{
                name = [string]$spec.name
                pid = $pidValue
                running = $running
                persona = [string]$spec.persona
                mode = [string]$spec.mode
                token_env = [string]$spec.token_env
                channels = @($spec.channel_ids)
                mention_only_channels = @($spec.mention_only_channel_ids)
            }) | Out-Null
        }
        $result.note = "relay stack status"
    }
    "start" {
        $stateByName = @{}
        foreach ($row in $existingState.relays) {
            $name = [string](Get-OptionalValue -Source $row -Name "name" -Default "")
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $stateByName[$name] = $row
            }
        }

        $newStateRows = New-Object System.Collections.ArrayList
        foreach ($spec in $specs) {
            $tokenValue = ""
            $tokenVar = Get-Item -Path ("Env:" + [string]$spec.token_env) -ErrorAction SilentlyContinue
            if ($null -ne $tokenVar -and $tokenVar.PSObject.Properties.Name -contains "Value") {
                $tokenValue = [string]$tokenVar.Value
            }
            if ([string]::IsNullOrWhiteSpace($tokenValue)) {
                $result.ok = $false
                $result.items.Add([ordered]@{
                    name = [string]$spec.name
                    started = $false
                    reason = "missing env token: $($spec.token_env)"
                }) | Out-Null
                continue
            }

            $oldRow = $stateByName[[string]$spec.name]
            if ($null -ne $oldRow) {
                $oldPid = 0
                try {
                    $oldPid = [int](Get-OptionalValue -Source $oldRow -Name "pid" -Default 0)
                }
                catch {
                    $oldPid = 0
                }
                if (Test-PidRunning -ProcessId $oldPid) {
                    $newStateRows.Add($oldRow) | Out-Null
                    $result.items.Add([ordered]@{
                        name = [string]$spec.name
                        started = $true
                        reused = $true
                        pid = $oldPid
                    }) | Out-Null
                    continue
                }
            }

            $pythonCmd = Get-Command ([string]$spec.python_exe) -ErrorAction SilentlyContinue
            if (-not $pythonCmd) {
                $result.ok = $false
                $result.items.Add([ordered]@{
                    name = [string]$spec.name
                    started = $false
                    reason = "python executable not found: $($spec.python_exe)"
                }) | Out-Null
                continue
            }

            $proc = Start-Process -FilePath ([string]$spec.python_exe) -ArgumentList $spec.args -PassThru -WindowStyle Hidden -WorkingDirectory $projectRoot
            Start-Sleep -Milliseconds 500
            $alive = Test-PidRunning -ProcessId $proc.Id
            if (-not $alive) {
                $result.ok = $false
            }

            $row = [ordered]@{
                name = [string]$spec.name
                pid = [int]$proc.Id
                running = $alive
                started_at = (Get-Date).ToString("o")
                token_env = [string]$spec.token_env
                persona = [string]$spec.persona
                mode = [string]$spec.mode
                bridge_url = [string]$spec.bridge_url
                channel_ids = @($spec.channel_ids)
                mention_only_channel_ids = @($spec.mention_only_channel_ids)
                args = @($spec.args)
            }
            $newStateRows.Add($row) | Out-Null

            $result.items.Add([ordered]@{
                name = [string]$spec.name
                started = $alive
                reused = $false
                pid = [int]$proc.Id
            }) | Out-Null
        }

        $statePayload = [ordered]@{
            updated_at = (Get-Date).ToString("o")
            action = "start"
            relays = @($newStateRows)
        }
        Write-JsonFile -Path $StateFile -Payload $statePayload
        $result.note = "relay stack start completed"
    }
    "restart" {
        $rows = @()
        if ($existingState.relays -is [System.Collections.IEnumerable]) {
            $rows = @($existingState.relays)
        }
        $null = Stop-RelayPids -RelayRows $rows

        if (Test-Path -LiteralPath $StateFile) {
            Remove-Item -LiteralPath $StateFile -Force
        }

        $restartCmd = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $PSCommandPath,
            "-Action", "start",
            "-ConfigFile", $ConfigFile
        )
        if ($Json) {
            $restartCmd += "-Json"
        }

        & powershell @restartCmd
        exit $LASTEXITCODE
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 10
    if ($result.ok) { exit 0 } else { exit 1 }
}

if ($result.ok) {
    Write-Host "[OK] $($result.note)" -ForegroundColor Green
}
else {
    Write-Host "[ERR] $($result.note)" -ForegroundColor Red
}
Write-Host "[INFO] config=$($result.config_file)"
Write-Host "[INFO] state=$($result.state_file)"
foreach ($item in $result.items) {
    if ($item.PSObject.Properties.Name -contains "reason") {
        Write-Host ("[INFO] relay={0} started={1} reason={2}" -f $item.name, $item.started, $item.reason)
        continue
    }
    if ($item.PSObject.Properties.Name -contains "running") {
        Write-Host ("[INFO] relay={0} pid={1} running={2}" -f $item.name, $item.pid, $item.running)
        continue
    }
    Write-Host ("[INFO] relay={0} started={1} reused={2} pid={3}" -f $item.name, $item.started, $item.reused, $item.pid)
}

if ($result.ok) { exit 0 } else { exit 1 }
