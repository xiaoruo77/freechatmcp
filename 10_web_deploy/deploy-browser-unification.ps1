param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$evidence = Join-Path $root '10_web_deploy\validation_runs\browser-unification-20261027'
$resultPath = Join-Path $evidence 'deployment.json'
Write-Output 'Only web_mcp and the existing Xiaoruo1 Browser Bridge scheduled task are reloaded.'
Write-Output 'Edge, Worker, Cloudflare, tokens, OAuth and configuration files are preserved.'
if (-not $Apply) { Write-Output 'Preview only. Use -Apply after the requested service-reload authorization.'; return }
try {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $dbPath = Join-Path $root '08_web_data\web_agent.sqlite3'
    $active = & $python -c 'import sqlite3,sys; c=sqlite3.connect(''file:''+sys.argv[1]+''?mode=ro'',uri=True); print(c.execute(''SELECT COUNT(*) FROM tasks WHERE status IN (?,?)'',(''queued'',''running'')).fetchone()[0]); c.close()' $dbPath
    if ($LASTEXITCODE -ne 0 -or [int]$active -gt 0) { throw 'Active tasks or unreadable state; defer reload.' }
    $taskName = 'Xiaoruo1 Browser Bridge'
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Stop-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(30)
    do {
        if (@(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if (@(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'Existing bridge still owns port48272; no unrelated process will be killed.'
    }
    Start-ScheduledTask -TaskName $taskName
    Restart-Service -Name web_mcp
    $healthy = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:48271/health' -TimeoutSec 2
            $bridge = @(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue)
            if ($health.status -eq 'ok' -and $health.orchestration_mode -eq 'chatgpt_direct' -and $bridge.Count -eq 1) { $healthy = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { throw 'Reload did not become healthy; inspect the original service logs.' }
    @{success=$true;mode=$health.orchestration_mode;completed_at=(Get-Date -Format o);restarted=@('web_mcp',$taskName)} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
    Write-Output 'Reload healthy. Reload the existing Edge extension1.9; optional debugger permission remains ungranted. Existing manual CEF instance is discovered without replacing its extension.'
} catch {
    @{success=$false;error_type=$_.Exception.GetType().Name;message=$_.Exception.Message;completed_at=(Get-Date -Format o)} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
    throw
}