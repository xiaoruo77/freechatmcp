param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path (Split-Path $PSScriptRoot -Parent) -Parent))
$shellObject = New-Object -ComObject WScript.Shell
$startupPath = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupPath 'Xiaoruo1 Desktop Bridge.lnk'
$targetPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$launcherPath = Join-Path $PSScriptRoot 'run_windows_mcp_bridge.py'
if (Test-Path -LiteralPath $shortcutPath) {
    $existingLink = $shellObject.CreateShortcut($shortcutPath)
    if ([IO.Path]::GetFullPath($existingLink.TargetPath) -ne $targetPath) {
        throw 'An unrelated shortcut uses this name; it has been preserved.'
    }
}
if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
    Write-Output 'Only the Xiaoruo1 Desktop Bridge user logon shortcut was removed.'
    return
}
if (-not (Test-Path -LiteralPath $targetPath) -or -not (Test-Path -LiteralPath $launcherPath)) {
    throw 'Install the project runtime and desktop launcher first.'
}
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.Arguments = '"' + $launcherPath + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Xiaoruo1 authenticated loopback desktop bridge in logged-in user session'
$shortcut.Save()
Write-Output ('User logon startup registered: ' + $shortcutPath)