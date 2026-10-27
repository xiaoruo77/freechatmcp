$ErrorActionPreference = 'Continue'
Stop-Service web_agent_cloudflared -Force
Stop-Service web_worker -Force
Stop-Service web_mcp -Force
