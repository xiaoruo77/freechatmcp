Get-Service web_mcp,web_worker,web_agent_cloudflared -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType
