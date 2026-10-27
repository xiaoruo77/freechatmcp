$ErrorActionPreference = 'Stop'
$evidenceRoot = Join-Path $PSScriptRoot 'migration_backups\desktop-enable-20261026-174256'
$resultPath = Join-Path $evidenceRoot 'mcp-restart-result.json'
try {
    @{stage='starting';started_at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content -LiteralPath $resultPath -Encoding utf8
    & (Join-Path $PSScriptRoot 'deploy-chatgpt-direct.ps1') -Apply -RestartService *> (Join-Path $evidenceRoot 'mcp-restart-output.txt')
    @{stage='healthy';success=$true;completed_at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content -LiteralPath $resultPath -Encoding utf8
    exit 0
} catch {
    @{stage='failed';success=$false;error_type=$_.Exception.GetType().Name;message=$_.Exception.Message;completed_at=(Get-Date -Format o)} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
    exit 1
}