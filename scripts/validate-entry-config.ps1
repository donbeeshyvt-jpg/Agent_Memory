param(
    [string[]]$ConfigFiles = @(
        ".\scripts\first-run-onboarding.local.json",
        ".\scripts\entry-stack.local.json",
        ".\scripts\discord-entry.local.json",
        ".\scripts\discord-relay-stack.local.json"
    ),
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$allowedSensitiveNameSet = @(
    "token_env",
    "discord_bot_token_env",
    "do_not_store_token_in_json"
)

$suspiciousNamePattern = "(^|_)(bot_token|token|api_key|apikey|secret|password|credential)(_|$)"
$discordTokenPattern = "[A-Za-z0-9_\-]{23,30}\.[A-Za-z0-9_\-]{6,8}\.[A-Za-z0-9_\-]{20,}"
$hex64Pattern = "\b[a-f0-9]{64}\b"
$openAiKeyPattern = "\bsk-[A-Za-z0-9\-_]{20,}\b"

function New-Issue {
    param(
        [string]$File,
        [string]$Path,
        [string]$Reason,
        [string]$ValuePreview = ""
    )
    return [ordered]@{
        file = $File
        path = $Path
        reason = $Reason
        value_preview = $ValuePreview
    }
}

function Add-FileSummary {
    param(
        [System.Collections.IList]$Rows,
        [string]$File,
        [bool]$Parsed,
        [int]$RelayCount,
        [int]$IssueCount
    )
    $Rows.Add([ordered]@{
        file = $File
        parsed = $Parsed
        relay_count = $RelayCount
        issue_count = $IssueCount
    }) | Out-Null
}

function Scan-JsonNode {
    param(
        [object]$Node,
        [string]$Path,
        [string]$File,
        [System.Collections.IList]$Issues
    )

    if ($null -eq $Node) {
        return
    }

    if ($Node -is [string]) {
        $text = [string]$Node
        if ([string]::IsNullOrWhiteSpace($text)) {
            return
        }
        if ($text -match $discordTokenPattern) {
            $Issues.Add((New-Issue -File $File -Path $Path -Reason "possible_discord_token_value" -ValuePreview "<masked>")) | Out-Null
        }
        if ($text.ToLowerInvariant() -match $hex64Pattern) {
            $Issues.Add((New-Issue -File $File -Path $Path -Reason "possible_hex_secret_value" -ValuePreview "<masked>")) | Out-Null
        }
        if ($text -match $openAiKeyPattern) {
            $Issues.Add((New-Issue -File $File -Path $Path -Reason "possible_openai_key_value" -ValuePreview "<masked>")) | Out-Null
        }
        return
    }

    if ($Node -is [System.Collections.IEnumerable] -and -not ($Node -is [string]) -and -not ($Node -is [pscustomobject])) {
        $index = 0
        foreach ($item in $Node) {
            Scan-JsonNode -Node $item -Path ($Path + "[" + $index + "]") -File $File -Issues $Issues
            $index += 1
        }
        return
    }

    foreach ($prop in $Node.PSObject.Properties) {
        $name = [string]$prop.Name
        $value = $prop.Value
        $childPath = if ([string]::IsNullOrWhiteSpace($Path)) { $name } else { $Path + "." + $name }
        $nameLower = $name.ToLowerInvariant()
        $isAllowedName = $allowedSensitiveNameSet -contains $nameLower
        if ((-not $isAllowedName) -and ($nameLower -match $suspiciousNamePattern)) {
            $Issues.Add((New-Issue -File $File -Path $childPath -Reason "suspicious_property_name" -ValuePreview "")) | Out-Null
        }
        Scan-JsonNode -Node $value -Path $childPath -File $File -Issues $Issues
    }
}

$result = [ordered]@{
    timestamp = (Get-Date).ToString("o")
    overall_ok = $true
    checked_files = New-Object System.Collections.ArrayList
    issues = New-Object System.Collections.ArrayList
}

foreach ($inputPath in $ConfigFiles) {
    if ([string]::IsNullOrWhiteSpace($inputPath)) {
        continue
    }

    $resolvedPath = ""
    try {
        $resolvedPath = (Resolve-Path -Path $inputPath -ErrorAction Stop).Path
    }
    catch {
        # local config not created yet -> skip
        continue
    }

    $raw = Get-Content -Path $resolvedPath -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) {
        $result.issues.Add((New-Issue -File $resolvedPath -Path "" -Reason "empty_file" -ValuePreview "")) | Out-Null
        Add-FileSummary -Rows $result.checked_files -File $resolvedPath -Parsed $false -RelayCount 0 -IssueCount 1
        $result.overall_ok = $false
        continue
    }

    $payload = $null
    try {
        $payload = $raw | ConvertFrom-Json
    }
    catch {
        $result.issues.Add((New-Issue -File $resolvedPath -Path "" -Reason "invalid_json" -ValuePreview "")) | Out-Null
        Add-FileSummary -Rows $result.checked_files -File $resolvedPath -Parsed $false -RelayCount 0 -IssueCount 1
        $result.overall_ok = $false
        continue
    }

    $issuesBefore = @($result.issues).Count
    Scan-JsonNode -Node $payload -Path "" -File $resolvedPath -Issues $result.issues
    $issuesAfter = @($result.issues).Count
    $issueCount = $issuesAfter - $issuesBefore

    $relayCount = 0
    if ($payload -and ($payload.PSObject.Properties.Name -contains "relays") -and ($payload.relays -is [System.Collections.IEnumerable])) {
        $relayCount = @($payload.relays).Count
    }

    Add-FileSummary -Rows $result.checked_files -File $resolvedPath -Parsed $true -RelayCount $relayCount -IssueCount $issueCount
}

if (@($result.issues).Count -gt 0) {
    $result.overall_ok = $false
}

if ($Json) {
    $result | ConvertTo-Json -Depth 12
    if ($result.overall_ok) { exit 0 } else { exit 1 }
}

if ($result.overall_ok) {
    Write-Host "[OK] entry config validation passed." -ForegroundColor Green
}
else {
    Write-Host "[ERR] entry config validation found issues." -ForegroundColor Red
}

foreach ($row in $result.checked_files) {
    Write-Host ("[INFO] file={0} parsed={1} relays={2} issues={3}" -f $row.file, $row.parsed, $row.relay_count, $row.issue_count)
}

if (-not $result.overall_ok) {
    foreach ($issue in $result.issues) {
        Write-Host ("[ISSUE] file={0} path={1} reason={2}" -f $issue.file, $issue.path, $issue.reason) -ForegroundColor Yellow
    }
}

if ($result.overall_ok) { exit 0 } else { exit 1 }
