param(
  [string]$Root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = 'Stop'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
  throw "Virtual environment not found: $Python"
}

$runtime = @(& $Python -c "import sys; print(sys.base_prefix); print(f'{sys.version_info.major}{sys.version_info.minor}')")
if ($LASTEXITCODE -ne 0 -or $runtime.Count -lt 2) {
  throw 'Unable to determine the Python runtime used by the virtual environment.'
}
$BasePython = $runtime[0].Trim()
$PythonTag = $runtime[1].Trim()
$Venv = Split-Path -Parent (Split-Path -Parent $Python)

foreach ($name in @("python$PythonTag.dll", 'python3.dll')) {
  $source = Join-Path $BasePython $name
  $destination = Join-Path $Venv $name
  if (-not (Test-Path -LiteralPath $source)) { throw "Python runtime DLL not found: $source" }
  if (-not (Test-Path -LiteralPath $destination)) {
    Copy-Item -LiteralPath $source -Destination $destination
  }
}

$pywin32 = Join-Path $Venv "Lib\site-packages\pywin32_system32\pywintypes$PythonTag.dll"
if (-not (Test-Path -LiteralPath $pywin32)) {
  throw "pywin32 helper DLL not found: $pywin32"
}
$pywintypesDestination = Join-Path $Venv "pywintypes$PythonTag.dll"
if (-not (Test-Path -LiteralPath $pywintypesDestination)) {
  Copy-Item -LiteralPath $pywin32 -Destination $pywintypesDestination
}

$pth = Join-Path $Venv "python$PythonTag._pth"
$paths = @(
  (Join-Path $BasePython "python$PythonTag.zip"),
  (Join-Path $BasePython 'DLLs'),
  (Join-Path $BasePython 'Lib'),
  $BasePython,
  $Venv,
  (Join-Path $Venv 'Lib\site-packages'),
  (Join-Path $Venv 'Lib\site-packages\win32'),
  (Join-Path $Venv 'Lib\site-packages\win32\lib'),
  (Join-Path $Venv 'Lib\site-packages\pythonwin'),
  (Join-Path $Venv 'Lib\site-packages\pywin32_system32'),
  'import site'
)
[IO.File]::WriteAllText($pth, (($paths -join [Environment]::NewLine) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
Write-Host "Prepared pywin32 service host: $pth"