$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'stop-services.ps1')
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot 'start-services.ps1')

# A Windows service can report Running while cloudflared is still reconnecting.
# Verify the actual public origin and retry the tunnel service when the previous
# stop/start overlapped with cloudflared's shutdown.
$PublicHealthUrl = 'https://mcp.example.com/health'
$TunnelHealthy = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri $PublicHealthUrl -UseBasicParsing -TimeoutSec 15
        if ([int]$response.StatusCode -eq 200) {
            $TunnelHealthy = $true
            break
        }
    } catch {
        # The edge returns 530 while the tunnel has no active connector.
    }
    if ($attempt -lt 3) {
        Restart-Service -Name web_agent_cloudflared -Force
        Start-Sleep -Seconds 8
    }
}
if ($TunnelHealthy) {
    Write-Host 'Cloudflare Tunnel: public health check passed'
} else {
    Write-Warning "Cloudflare Tunnel public health check failed: $PublicHealthUrl"
    Write-Host 'Run cloudflared tunnel info codex and inspect 09_web_logs\cloudflared.log.'
}

# The Edge bridge belongs to the interactive user session rather than the
# LocalSystem MCP service. Restart it as part of the same recovery command so
# a stuck extension WebSocket cannot keep browser_read_page unavailable.
$BrowserBridgeTask = 'Xiaoruo1 Browser Bridge'
$Task = Get-ScheduledTask -TaskName $BrowserBridgeTask -ErrorAction SilentlyContinue
if ($null -ne $Task) {
    Stop-ScheduledTask -TaskName $BrowserBridgeTask -ErrorAction SilentlyContinue
    # Stop-ScheduledTask returns before the python child has necessarily
    # released the listening socket. Starting immediately can produce
    # WinError 10048 and leave the task stopped, which makes the public MCP
    # endpoint look like an OAuth/account failure.
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $listener = @(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue)
        if ($listener.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    if (@(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'Xiaoruo1 Browser Bridge still owns 127.0.0.1:48272; restart was aborted safely.'
    }

    Start-ScheduledTask -TaskName $BrowserBridgeTask
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $listener = @(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue)
        if ($listener.Count -gt 0) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    if (@(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -eq 0) {
        $info = Get-ScheduledTaskInfo -TaskName $BrowserBridgeTask -ErrorAction SilentlyContinue
        throw "Xiaoruo1 Browser Bridge failed to start. TaskResult=$($info.LastTaskResult). See 09_web_logs\browser_bridge_startup_error.log."
    }

    Write-Host "Restarted: $BrowserBridgeTask (127.0.0.1:48272 listening)"
}