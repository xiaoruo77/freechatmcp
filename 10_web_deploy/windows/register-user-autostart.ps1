$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$RunServer = Join-Path $Root 'run_server.py'
$RunWorker = Join-Path $Root 'run_worker.py'
$cloudflared = (Get-Command cloudflared -ErrorAction Stop).Source
$cloudConfig = Join-Path $Root '10_web_deploy\cloudflared\config.yml'
$userId = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn
$quote = [char]34
$tasks = @(
  @{ Name = 'web_agent\web_mcp'; Action = New-ScheduledTaskAction -Execute $Python -Argument ($quote + $RunServer + $quote) -WorkingDirectory $Root },
  @{ Name = 'web_agent\web_worker'; Action = New-ScheduledTaskAction -Execute $Python -Argument ($quote + $RunWorker + $quote) -WorkingDirectory $Root },
  @{ Name = 'web_agent\cloudflared'; Action = New-ScheduledTaskAction -Execute $cloudflared -Argument ('--config ' + $quote + $cloudConfig + $quote + ' tunnel run codex') -WorkingDirectory $Root }
)
foreach ($item in $tasks) {
  Register-ScheduledTask -TaskName $item.Name -Action $item.Action -Trigger $trigger -Principal $principal -Force | Out-Null
}
Write-Host 'Registered per-user logon auto-start tasks: web_agent\web_mcp, web_agent\web_worker, web_agent\cloudflared'