param([switch]$Apply, [switch]$RestartService, [switch]$Legacy, [string]$DeploymentTaskId = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$modeFile = Join-Path $projectRoot '07_web_config\orchestration.json'
$mode = if ($Legacy) { 'legacy_sampling' } else { 'chatgpt_direct' }
Write-Output "Mode: $mode"
Write-Output "Mode file: $modeFile"
Write-Output 'This script never changes tokens, OAuth, Cloudflare or desktop permissions.'
if (-not $Apply) { Write-Output 'Preview only. Use -Apply after approving the rollout; add -RestartService for web_mcp.'; return }
if ($RestartService) {
    $dbPath = Join-Path $projectRoot '08_web_data\web_agent.sqlite3'
    if (Test-Path -LiteralPath $dbPath) {
        # Python single quotes survive Windows PowerShell 5.1 native argv parsing.
        # An authenticated Worker deployment excludes only its own known task ID.
        $active = & (Join-Path $projectRoot '.venv\Scripts\python.exe') -c 'import sqlite3,sys; c=sqlite3.connect(''file:''+sys.argv[1]+''?mode=ro'',uri=True); print(c.execute(''SELECT COUNT(*) FROM tasks WHERE status IN (?,?) AND id<>?'',(''queued'',''running'',sys.argv[2] if len(sys.argv)>2 else '''')).fetchone()[0]); c.close()' $dbPath $DeploymentTaskId
        if ($LASTEXITCODE -ne 0 -or [int]$active -gt 0) { throw 'Active tasks or unreadable task state: defer restarting web_mcp until work is safe.' }
    }
}
if (Test-Path -LiteralPath $modeFile) {
    Copy-Item -LiteralPath $modeFile -Destination ($modeFile + '.' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.bak')
}
$temporary = $modeFile + '.new'
@{orchestration_mode=$mode} | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
Move-Item -LiteralPath $temporary -Destination $modeFile -Force
if ($RestartService) {
    Restart-Service -Name 'web_mcp'
    Get-Service -Name 'web_mcp','web_worker'
    $verified = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:48271/health' -TimeoutSec 2
            if ($health.status -eq 'ok' -and $health.orchestration_mode -eq $mode) { $verified = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $verified) { throw 'Expected mode did not become healthy; inspect service logs and use the rollback instructions.' }
    Write-Output "Verified healthy mode: $mode"
} else { Write-Output 'Saved. Existing processes retain their current settings until restarted.' }