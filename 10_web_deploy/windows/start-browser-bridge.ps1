param(
    [string]$Root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Xiaoruo1 Browser Bridge'

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    & (Join-Path $PSScriptRoot 'install-browser-bridge.ps1') -Root $Root
    exit $LASTEXITCODE
}

Start-ScheduledTask -TaskName $TaskName
Write-Host "Started: $TaskName"