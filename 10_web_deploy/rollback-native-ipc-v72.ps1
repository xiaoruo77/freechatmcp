param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$taskStage = Join-Path $taskRoot '10_web_deploy\migration_backups\native-ipc-v72-20261028'

function Get-NativeRollbackHash([string]$LiteralPath) {
    $stream = [IO.File]::OpenRead($LiteralPath)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}
function Resolve-NativeRollbackPath([string]$RelativePath, [string]$Base) {
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath)) { throw 'Rollback paths must be relative.' }
    $resolved = [IO.Path]::GetFullPath((Join-Path $Base $RelativePath))
    if (-not $resolved.StartsWith($Base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Rollback path leaves its allowed directory.' }
    return $resolved
}
function Test-NativeSourcePath([string]$RelativePath) {
    $normalized = $RelativePath.Replace('/', '\')
    if ($normalized -eq 'README.md') { return $true }
    return $normalized -like '02_web_mcp\*' -or $normalized -like '06_web_tools\*' -or
        $normalized -like '07_web_config\web_config\*' -or $normalized -like 'tests\*' -or $normalized -like '10_web_deploy\*'
}

$taskManifest = Get-Content -Raw -LiteralPath (Join-Path $taskStage 'delivery-manifest.json') | ConvertFrom-Json
if ($taskManifest.Count -eq 0) { throw 'Rollback manifest is empty.' }
foreach ($item in $taskManifest) {
    if (-not (Test-NativeSourcePath $item.path)) { throw 'Rollback may restore source and delivery files only; configuration, data and credentials are excluded.' }
    $target = Resolve-NativeRollbackPath $item.path $taskRoot
    if ((Test-Path -LiteralPath $target) -and (Get-NativeRollbackHash $target) -ne $item.sha256) {
        throw "File changed after delivery; preserve and review it before rollback: $($item.path)"
    }
    if ($item.kind -eq 'modified') {
        $source = Resolve-NativeRollbackPath $item.backup_path $taskStage
        if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or (Get-NativeRollbackHash $source) -ne $item.original_sha256) {
            throw "Original source backup hash mismatch: $($item.path)"
        }
    } elseif ($item.kind -ne 'added') { throw 'Unknown rollback manifest entry kind.' }
    Write-Output "$($item.kind): $($item.path)"
}
if (-not $Apply) { Write-Output 'Preview only. Preserves all browser profiles, media, SQLite data, receipts, discovery files and credentials.'; return }
$taskQuarantine = Join-Path $taskStage ('rollback-new-source-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
foreach ($item in $taskManifest) {
    $target = Resolve-NativeRollbackPath $item.path $taskRoot
    if ($item.kind -eq 'modified') {
        Copy-Item -LiteralPath (Join-Path $taskStage $item.backup_path) -Destination $target -Force
    } elseif ((Test-Path -LiteralPath $target) -and ($item.path.Replace('/', '\') -like '06_web_tools\*' -or $item.path.Replace('/', '\') -like 'tests\*')) {
        $destination = Resolve-NativeRollbackPath $item.path $taskQuarantine
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Move-Item -LiteralPath $target -Destination $destination
    }
}
Write-Output 'Native integration source restored. Reload only web_mcp and the existing Browser Bridge through the normal authorized service workflow.'