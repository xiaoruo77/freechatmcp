param([switch]$Apply, [string]$ValidationReport = '', [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$taskStage = Join-Path $taskRoot '10_web_deploy\migration_backups\native-ipc-v72-20261028'
$taskEvidence = Join-Path $taskRoot '10_web_deploy\validation_runs\native-ipc-v72-20261028'

function Get-NativeDeployHash([string]$LiteralPath) {
    $stream = [IO.File]::OpenRead($LiteralPath)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Resolve-NativeDeployPath([string]$RelativePath, [string]$Base) {
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath)) { throw 'Manifest path must be relative.' }
    $resolved = [IO.Path]::GetFullPath((Join-Path $Base $RelativePath))
    if (-not $resolved.StartsWith($Base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path leaves its allowed directory.' }
    return $resolved
}

Write-Output 'Native IPC v72: only web_mcp and the existing Xiaoruo1 Browser Bridge task may reload.'
Write-Output 'No browser, Edge extension, Worker, desktop bridge, Cloudflare, profile or credential changes.'
$taskManifestPath = Join-Path $taskStage 'delivery-manifest.json'
if (-not (Test-Path -LiteralPath $taskManifestPath -PathType Leaf)) { throw 'Reviewed delivery manifest is missing.' }
$taskManifest = Get-Content -Raw -LiteralPath $taskManifestPath | ConvertFrom-Json
if ($taskManifest.Count -eq 0) { throw 'Reviewed delivery manifest is empty.' }
foreach ($item in $taskManifest) {
    $target = Resolve-NativeDeployPath $item.path $taskRoot
    if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-NativeDeployHash $target) -ne $item.sha256) {
        throw "Delivery hash mismatch; preserve and review before deployment: $($item.path)"
    }
}
if (-not $Apply -and -not $ValidateOnly) { Write-Output 'Preview only. Verified delivery hashes; use -ValidationReport and -Apply only after isolated validation.'; return }
if ([string]::IsNullOrWhiteSpace($ValidationReport)) { throw 'An explicit isolated live validation report is required.' }
$taskValidationPath = [IO.Path]::GetFullPath($ValidationReport)
if (-not $taskValidationPath.StartsWith($taskEvidence + '\', [StringComparison]::OrdinalIgnoreCase) -or
    [IO.Path]::GetExtension($taskValidationPath) -ne '.json' -or -not (Test-Path -LiteralPath $taskValidationPath -PathType Leaf)) {
    throw 'Validation report must be a JSON file in this native IPC validation directory.'
}
$taskValidation = Get-Content -Raw -LiteralPath $taskValidationPath | ConvertFrom-Json
if ($taskValidation.passed -isnot [bool] -or -not $taskValidation.passed -or
    $taskValidation.validation_mode -ne 'isolated' -or
    $taskValidation.engines.cef.passed -isnot [bool] -or -not $taskValidation.engines.cef.passed -or
    $taskValidation.engines.lightpanda.passed -isnot [bool] -or -not $taskValidation.engines.lightpanda.passed) {
    throw 'Both isolated CEF and capability-aware Lightpanda validations must have passed.'
}
if ($ValidateOnly) { Write-Output 'Validated source hashes and isolated CEF/Lightpanda report. No service action performed.'; return }

$taskResultPath = Join-Path $taskEvidence 'deployment.json'
$taskStartedAt = Get-Date -Format o
try {
    $taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    $taskDatabase = Join-Path $taskRoot '08_web_data\web_agent.sqlite3'
    if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf) -or -not (Test-Path -LiteralPath $taskDatabase -PathType Leaf)) {
        throw 'Existing Python or task database is unavailable; deployment is not safe.'
    }
    $taskActiveCount = & $taskPython -c 'import sqlite3,sys; c=sqlite3.connect(''file:''+sys.argv[1]+''?mode=ro'',uri=True); print(c.execute(''SELECT COUNT(*) FROM tasks WHERE status IN (?,?)'',(''queued'',''running'')).fetchone()[0]); c.close()' $taskDatabase
    if ($LASTEXITCODE -ne 0 -or [int]$taskActiveCount -gt 0) { throw 'Active queued/running tasks or unreadable state; defer deployment.' }
    $taskName = 'Xiaoruo1 Browser Bridge'
    $taskScheduled = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    if (@($taskScheduled.Actions).Count -ne 1 -or $taskScheduled.Actions[0].Arguments -notmatch [regex]::Escape((Join-Path $taskRoot 'run_browser_bridge.py'))) {
        throw 'Existing Browser Bridge task action does not match this project.'
    }
    Stop-ScheduledTask -TaskName $taskName
    $taskDeadline = (Get-Date).AddSeconds(30)
    do {
        if (@(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $taskDeadline)
    if (@(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'Original bridge still owns its port; no unrelated process will be killed.'
    }
    Start-ScheduledTask -TaskName $taskName
    Restart-Service -Name web_mcp
    $taskHealthy = $false
    for ($taskAttempt=0; $taskAttempt -lt 30; $taskAttempt++) {
        try {
            $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:48271/health' -TimeoutSec 2
            if ($taskHealth.status -eq 'ok' -and $taskHealth.orchestration_mode -eq 'chatgpt_direct' -and
                @(Get-NetTCPConnection -LocalPort 48272 -State Listen -ErrorAction SilentlyContinue).Count -eq 1) {
                $taskHealthy = $true; break
            }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $taskHealthy) { throw 'The existing services did not return healthy direct mode.' }
    $taskResult = @{success=$true;started_at=$taskStartedAt;completed_at=(Get-Date -Format o);
        mode=$taskHealth.orchestration_mode;restarted=@('web_mcp',$taskName);
        validation_report=$taskValidationPath;validation_sha256=(Get-NativeDeployHash $taskValidationPath);
        manifest_sha256=(Get-NativeDeployHash $taskManifestPath);browser_restart=$false;credentials_modified=$false}
    $taskResult | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath ($taskResultPath + '.new') -Encoding utf8
    Move-Item -LiteralPath ($taskResultPath + '.new') -Destination $taskResultPath -Force
    Write-Output 'Native IPC integration reloaded; direct mode healthy. Existing browser and profiles preserved.'
} catch {
    @{success=$false;started_at=$taskStartedAt;completed_at=(Get-Date -Format o);
        error_type=$_.Exception.GetType().Name;message='Native IPC deployment did not finish; inspect authorized local service diagnostics.'} |
        ConvertTo-Json | Set-Content -LiteralPath $taskResultPath -Encoding utf8
    throw
}