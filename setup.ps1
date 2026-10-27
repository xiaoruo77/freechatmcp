$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv\Scripts\python.exe'))) {
  python -m venv (Join-Path $Root '.venv')
}
$Python = Join-Path $Root '.venv\Scripts\python.exe'
& $Python -m pip install -e (Join-Path $Root '.[dev]')
& (Join-Path $Root '10_web_deploy\windows\prepare-pythonservice.ps1')
Push-Location (Join-Path $Root '01_web_frontend')
npm install
npm run build
Pop-Location
Write-Host 'Xiaoruo1 setup complete.'