$ErrorActionPreference = 'Continue'
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match '00_xiaoruo1.*run_(server|worker)\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host 'Stopped local Xiaoruo1 processes.'