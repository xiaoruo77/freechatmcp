$ErrorActionPreference = 'Continue'
foreach ($name in @('web_agent\web_mcp', 'web_agent\web_worker', 'web_agent\cloudflared')) {
  Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}