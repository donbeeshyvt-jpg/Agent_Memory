param(
    [string]$BaseUrl = "http://127.0.0.1:16000"
)

$ErrorActionPreference = "Stop"

Write-Host "[INFO] Sending Discord sample payload..." -ForegroundColor Cyan
$discordPayload = @{
    content = "Please summarize today's highlights (sample)"
    channel_id = "guild123-thread456"
    author = @{
        id = "user-42"
    }
} | ConvertTo-Json -Depth 4

$discordResult = Invoke-RestMethod `
    -Method Post `
    -Uri "$BaseUrl/webhook/discord" `
    -ContentType "application/json" `
    -Body $discordPayload

Write-Host "[OK] Discord response:" -ForegroundColor Green
$discordResult | ConvertTo-Json -Depth 6

Write-Host "[INFO] Sending LINE sample payload..." -ForegroundColor Cyan
$linePayload = @{
    events = @(
        @{
            type = "message"
            message = @{
                type = "text"
                text = "Please generate today's summary (sample)"
            }
            source = @{
                type = "user"
                userId = "U1234567890"
            }
        }
    )
} | ConvertTo-Json -Depth 6

$lineResult = Invoke-RestMethod `
    -Method Post `
    -Uri "$BaseUrl/webhook/line" `
    -ContentType "application/json" `
    -Body $linePayload

Write-Host "[OK] LINE response:" -ForegroundColor Green
$lineResult | ConvertTo-Json -Depth 6
