param(
    [string]$Root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Xiaoruo1 Browser Bridge'

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "Task: Not installed"
    exit 0
}

$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Task: $($Task.State)"
Write-Host "LastRun: $($Info.LastRunTime)"
Write-Host "LastResult: $($Info.LastTaskResult)"

$Connection = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue
if ($null -eq $Connection) {
    Write-Host 'Bridge: Not listening on 127.0.0.1:48272'
} else {
    Write-Host 'Bridge: Listening on 127.0.0.1:48272 (Edge extension WebSocket)'
}

$TokenFile = Join-Path $Root '08_web_data\browser_bridge.token'
if (Test-Path -LiteralPath $TokenFile) {
    try {
        $Token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
        $Health = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:48272/health' -Headers @{ 'X-Xiaoruo1-Browser-Token' = $Token } -TimeoutSec 3
        Write-Host ("Extension connected: " + [bool]$Health.extension_connected)
    } catch {
        Write-Host 'Extension connected: unable to query health'
    }
}