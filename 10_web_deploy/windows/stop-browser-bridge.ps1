$ErrorActionPreference = 'Stop'
$TaskName = 'Xiaoruo1 Browser Bridge'

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "Not installed: $TaskName"
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName
Write-Host "Stopped: $TaskName"