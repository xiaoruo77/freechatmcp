$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
foreach ($script in @('web_mcp_service.py', 'web_worker_service.py', 'cloudflared_service.py')) {
  & $Python (Join-Path $PSScriptRoot $script) remove
}
