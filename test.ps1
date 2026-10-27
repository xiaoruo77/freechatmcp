$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$env:PYTHONPATH = '02_web_mcp;03_web_worker;04_web_canvas;05_web_memory;06_web_tools;07_web_config;09_web_logs'
& (Join-Path $Root '.venv\Scripts\python.exe') -m pytest -q