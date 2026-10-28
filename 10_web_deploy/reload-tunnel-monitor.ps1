param([switch]$Apply, [switch]$Rollback)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$taskStage = Join-Path $taskRoot '10_web_deploy\migration_backups\mcp-connectivity-20261028'
$taskManifest = Get-Content -Raw -LiteralPath (Join-Path $taskStage 'manifest.json') | ConvertFrom-Json
function Get-TaskHash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}
$taskServiceSource = Join-Path $taskRoot '10_web_deploy\windows\cloudflared_service.py'
$taskPolicy = Join-Path $taskRoot '10_web_deploy\windows\tunnel_health.py'
$taskBackup = Join-Path $taskStage '10_web_deploy\windows\cloudflared_service.py'
if ((Get-TaskHash $taskServiceSource) -ne $taskManifest.service_sha256 -or
    (Get-TaskHash $taskPolicy) -ne $taskManifest.policy_sha256 -or
    (Get-TaskHash $taskBackup) -ne $taskManifest.backup_sha256) {
    throw 'Source or backup changed since validation. Review before reloading or rolling back.'
}
$taskService = Get-CimInstance Win32_Service -Filter "Name='web_agent_cloudflared'"
if (-not $taskService -or $taskService.PathName.Trim('"') -ne (Join-Path $taskRoot '.venv\pythonservice.exe')) {
    throw 'Existing tunnel service does not match this project.'
}
Write-Output 'Only web_agent_cloudflared will restart. All hostnames on that tunnel briefly lose remote access.'
Write-Output 'No MCP/Worker/browser restarts, global proxy, firewall, credential or tunnel configuration changes.'
if (-not $Apply) { Write-Output 'Preview only. Add -Apply only after the service restart is authorized.'; return }
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = New-Object Security.Principal.WindowsPrincipal($taskIdentity)
if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'An existing elevated PowerShell is required. This script does not trigger UAC.'
}
if ($Rollback) { Copy-Item -LiteralPath $taskBackup -Destination $taskServiceSource -Force }
Restart-Service -Name web_agent_cloudflared -ErrorAction Stop
Write-Output 'Tunnel service restarted. Run .venv\Scripts\python.exe 10_web_deploy\diagnose-mcp-connectivity.py to verify readiness.'