$ErrorActionPreference = 'Stop'

$oldRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$parentRoot = Split-Path -Parent $oldRoot
$newRoot = Join-Path $parentRoot '00_xiaoruo1'
$serviceNames = @('web_mcp', 'web_worker', 'web_agent_cloudflared')

if ((Split-Path -Leaf $oldRoot) -ne '00_web_agent') {
    throw "Unexpected source directory: $oldRoot"
}
if (-not (Test-Path -LiteralPath $oldRoot)) {
    throw "Source directory not found: $oldRoot"
}
if (Test-Path -LiteralPath $newRoot) {
    throw "Destination already exists: $newRoot"
}

Write-Host "Stopping Xiaoruo1 services..."
foreach ($serviceName in $serviceNames) {
    Stop-Service -Name $serviceName -Force
}
Start-Sleep -Seconds 3

Set-Location -LiteralPath $parentRoot
Move-Item -LiteralPath $oldRoot -Destination $newRoot

Write-Host "Registering services from $newRoot..."
& (Join-Path $newRoot '10_web_deploy\windows\install-services.ps1')
if ($LASTEXITCODE -ne 0) {
    throw "Service installation failed."
}

& (Join-Path $newRoot '10_web_deploy\windows\start-services.ps1')
if ($LASTEXITCODE -ne 0) {
    throw "Service startup failed."
}

Get-CimInstance Win32_Service -Filter "Name='web_mcp' OR Name='web_worker' OR Name='web_agent_cloudflared'" |
    Select-Object Name, State, StartMode, PathName |
    Format-Table -AutoSize

Write-Host "Migration completed: $newRoot"