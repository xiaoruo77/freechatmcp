$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'stop-local.ps1')
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot 'start-local.ps1')
