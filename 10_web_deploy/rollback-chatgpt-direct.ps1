param([switch]$Apply, [switch]$RestartService)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$backup = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'chatgpt-direct-backup-path.txt')).Trim()
$backup = [IO.Path]::GetFullPath($backup)
if (-not $backup.StartsWith((Join-Path $projectRoot '10_web_deploy\migration_backups\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Backup path is outside project backup directory' }
$manifest = Get-Content -Raw -LiteralPath (Join-Path $backup 'delivery-manifest.json') | ConvertFrom-Json
foreach ($item in $manifest) {
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $item.path))
    if (-not $target.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escaped project' }
    Write-Output "$($item.kind): $($item.path)"
    if ($Apply -and (Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $item.sha256) {
        throw "File changed after delivery; preserve and review it before rollback: $($item.path)"
    }
}
if (-not $Apply) { Write-Output 'Preview only. -Apply restores source and quarantines new source files; no database/token files are restored.'; return }
if ($RestartService) {
    $dbPath = Join-Path $projectRoot '08_web_data\web_agent.sqlite3'
    if (Test-Path -LiteralPath $dbPath) {
        $active = & (Join-Path $projectRoot '.venv\Scripts\python.exe') -c 'import sqlite3,sys; c=sqlite3.connect(''file:''+sys.argv[1]+''?mode=ro'',uri=True); print(c.execute(''SELECT COUNT(*) FROM tasks WHERE status IN (?,?)'',(''queued'',''running'')).fetchone()[0]); c.close()' $dbPath
        if ($LASTEXITCODE -ne 0 -or [int]$active -gt 0) { throw 'Active tasks or unreadable task state: defer service restart.' }
    }
}
$desktopLauncher = Join-Path $projectRoot '10_web_deploy\windows\run_windows_mcp_bridge.py'
if (Test-Path -LiteralPath $desktopLauncher) {
    $desktopPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    & $desktopPython $desktopLauncher --stop | Out-Null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $desktopState = (& $desktopPython $desktopLauncher --status | ConvertFrom-Json)
        if (-not $desktopState.running) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($desktopState.running) { throw 'Owned desktop bridge did not stop; no source files were restored.' }
    $desktopAutostart = Join-Path $projectRoot '10_web_deploy\windows\install-desktop-autostart.ps1'
    if (Test-Path -LiteralPath $desktopAutostart) { & $desktopAutostart -Remove }
}
if ($RestartService) { Stop-Service -Name 'web_mcp' }
foreach ($item in $manifest) {
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $item.path))
    if ($item.kind -eq 'modified') {
        Copy-Item -LiteralPath (Join-Path $backup $item.path) -Destination $target -Force
    } elseif ((Test-Path -LiteralPath $target) -and $item.path -notlike '10_web_deploy\*') {
        $quarantine = [IO.Path]::GetFullPath((Join-Path $backup ('rollback-quarantine\' + $item.path)))
        if (-not $quarantine.StartsWith($backup + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Quarantine path escaped backup' }
        New-Item -ItemType Directory -Force -Path (Split-Path $quarantine) | Out-Null
        Move-Item -LiteralPath $target -Destination $quarantine -Force
    }
}
if ($RestartService) { Start-Service -Name 'web_mcp' }
Write-Output 'Source restored. Goal tables and all task/OAuth/token data were preserved. Isolated Windows-MCP runtime was retained.'