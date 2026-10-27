$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$env:WEB_AGENT_HOME = $Root
$env:WEB_AGENT_WORKSPACE = $Root
$env:WEB_AGENT_HOST = '127.0.0.1'
$env:WEB_AGENT_PORT = '48271'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
Start-Process -FilePath $Python -ArgumentList (Join-Path $Root 'run_server.py') -WorkingDirectory $Root -WindowStyle Hidden
Start-Process -FilePath $Python -ArgumentList (Join-Path $Root 'run_worker.py') -WorkingDirectory $Root -WindowStyle Hidden
Write-Host 'Started web_mcp and web_worker as background processes.'
