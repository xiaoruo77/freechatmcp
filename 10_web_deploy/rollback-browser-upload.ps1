param([switch]$Apply)
$ErrorActionPreference = 'Stop'
function Get-TaskFileHash([string]$LiteralPath) {
    $stream = [IO.File]::OpenRead($LiteralPath)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

$root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$stage = Join-Path $root '10_web_deploy\migration_backups\bilibili-web-upload-20261027'
$manifest = Get-Content -Raw -LiteralPath (Join-Path $stage 'delivery-manifest.json') | ConvertFrom-Json
foreach ($item in $manifest) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $item.path))
    if (-not $target.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Rollback target outside MCP project.' }
    if ((Test-Path -LiteralPath $target) -and (Get-TaskFileHash $target) -ne $item.sha256) {
        throw "File changed after delivery; preserve and review before rollback: $($item.path)"
    }
    if ($item.kind -eq 'modified') {
        $source = [IO.Path]::GetFullPath((Join-Path $stage $item.path))
        if (-not $source.StartsWith($stage + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Backup source outside stage.' }
        if (-not (Test-Path -LiteralPath $source) -or (Get-TaskFileHash $source) -ne $item.original_sha256) {
            throw "Original backup hash mismatch: $($item.path)"
        }
    }
    Write-Output "$($item.kind): $($item.path)"
}
if (-not $Apply) { Write-Output 'Preview only. Preserves media, ffprobe, credentials, upload receipts and database.'; return }
$quarantine = Join-Path $stage ('rollback-new-source-' + (Get-Date -Format yyyyMMdd-HHmmss))
foreach ($item in $manifest) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $item.path))
    if ($item.kind -eq 'modified') {
        Copy-Item -LiteralPath (Join-Path $stage $item.path) -Destination $target -Force
    } elseif ((Test-Path -LiteralPath $target) -and ($item.path -like '06_web_tools\*' -or $item.path -like 'tests\*' -or $item.path -like '10_web_deploy\edge_extension\*')) {
        $destination = [IO.Path]::GetFullPath((Join-Path $quarantine $item.path))
        if (-not $destination.StartsWith($quarantine + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Quarantine outside stage.' }
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Move-Item -LiteralPath $target -Destination $destination
    }
}
Write-Output 'Source restored. Reload only web_mcp, the existing Browser Bridge and the Edge extension after the normal restart authorization.'