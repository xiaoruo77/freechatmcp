$ErrorActionPreference = 'Stop'

$legacyName = 'Cloudflared'
$managedName = 'web_agent_cloudflared'
$legacy = Get-CimInstance Win32_Service -Filter "Name='$legacyName'" -ErrorAction SilentlyContinue
$managed = Get-CimInstance Win32_Service -Filter "Name='$managedName'" -ErrorAction SilentlyContinue

if (-not $managed) {
    throw "Managed service not found: $managedName"
}

if ($legacy) {
    $legacyPath = [string]$legacy.PathName
    if ($legacyPath -notmatch '(?i)cloudflared\.exe') {
        throw "Refusing to change an unexpected Cloudflared service: $legacyPath"
    }
    Write-Host "Stopping the legacy Cloudflared service..."
    Stop-Service -Name $legacyName -Force -ErrorAction SilentlyContinue
    Set-Service -Name $legacyName -StartupType Disabled
    Write-Host "Legacy Cloudflared service disabled (not uninstalled)."
}

Set-Service -Name $managedName -StartupType Automatic
sc.exe config $managedName start= delayed-auto | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to set delayed automatic startup for $managedName"
}
sc.exe failure $managedName reset= 86400 actions= restart/5000/restart/15000/restart/30000 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to configure recovery actions for $managedName"
}
sc.exe failureflag $managedName 1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enable recovery on non-crash exits for $managedName"
}
Restart-Service -Name $managedName -Force
Start-Sleep -Seconds 8

Get-CimInstance Win32_Service -Filter "Name='$legacyName' OR Name='$managedName'" |
    Select-Object Name, DisplayName, State, StartMode, PathName |
    Format-Table -AutoSize

Write-Host "Checking the codex tunnel..."
& (Get-Command cloudflared -ErrorAction Stop).Source --config (Join-Path $PSScriptRoot '..\cloudflared\config.yml') tunnel info codex