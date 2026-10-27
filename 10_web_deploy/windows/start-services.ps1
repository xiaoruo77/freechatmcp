$ErrorActionPreference = 'Stop'
Start-Service web_mcp
Start-Service web_worker
Start-Service web_agent_cloudflared
