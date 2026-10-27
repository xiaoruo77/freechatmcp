# One-time elevation wrapper for the user-approved MCP rollout. No permission/ACL edits.
$ErrorActionPreference = 'Stop'
$reportRoot = Join-Path $PSScriptRoot 'migration_backups\20261026-141209-chatgpt-direct'
$resultPath = Join-Path $reportRoot 'deployment-result.json'
$outputPath = Join-Path $reportRoot 'deployment-output.txt'
try {
    @{success=$null;stage='starting';service='web_mcp';started_at=(Get-Date -Format o)} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
    & (Join-Path $PSScriptRoot 'deploy-chatgpt-direct.ps1') -Apply -RestartService *> $outputPath
    @{success=$true;service='web_mcp';mode='chatgpt_direct';completed_at=(Get-Date -Format o)} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
    exit 0
} catch {
    # Deployment only emits mode/service diagnostics; no token values are logged.
    @{success=$false;service='web_mcp';error_type=$_.Exception.GetType().Name;message=$_.Exception.Message;completed_at=(Get-Date -Format o)} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
    exit 1
}