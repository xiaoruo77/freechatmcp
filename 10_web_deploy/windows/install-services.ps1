$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "Virtual environment not found: $Python" }
& (Join-Path $PSScriptRoot 'prepare-pythonservice.ps1') -Root $Root
$serviceNames = @('web_mcp', 'web_worker', 'web_agent_cloudflared')
foreach ($script in @('web_mcp_service.py', 'web_worker_service.py', 'cloudflared_service.py')) {
  $startup = if ($script -eq 'cloudflared_service.py') { 'delayed' } else { 'auto' }
  & $Python (Join-Path $PSScriptRoot $script) --startup $startup install
  if ($LASTEXITCODE -ne 0) { throw "Failed to install $script" }
}
sc.exe failure web_mcp reset= 86400 actions= restart/5000/restart/10000/""/0 | Out-Null
sc.exe failure web_worker reset= 86400 actions= restart/5000/restart/10000/""/0 | Out-Null
sc.exe failure web_agent_cloudflared reset= 86400 actions= restart/5000/restart/10000/""/0 | Out-Null
$missing = @($serviceNames | Where-Object { -not (Get-Service -Name $_ -ErrorAction SilentlyContinue) })
if ($missing.Count -gt 0) {
  throw "Service registration did not complete. Run this script from an elevated PowerShell window. Missing: $($missing -join ', ')"
}
$legacy = Get-Service -Name Cloudflared -ErrorAction SilentlyContinue
if ($legacy) {
  Stop-Service -Name Cloudflared -Force -ErrorAction SilentlyContinue
  Set-Service -Name Cloudflared -StartupType Disabled
}
Write-Host 'Installed: web_mcp, web_worker, web_agent_cloudflared'