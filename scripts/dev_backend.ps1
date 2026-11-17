<#
.SYNOPSIS
  Run the SMART SPACE backend in dev mode (SDD 14, step 6).

.DESCRIPTION
  Starts the FastAPI sidecar on http://127.0.0.1:8765 with SS_TOKEN=dev and Swagger UI at /docs.
  The frontend points at it with VITE_DEV_BACKEND=http://127.0.0.1:8765 (token "dev").
  Data lives in %APPDATA%\SmartSpace unless -DataDir is given.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev_backend.ps1
  powershell -ExecutionPolicy Bypass -File scripts\dev_backend.ps1 -DataDir C:\temp\ss-data -Port 8800
#>
param(
    [int]$Port = 8765,
    [string]$Token = "dev",
    [string]$DataDir = ""
)
$ErrorActionPreference = "Stop"
$backend = Join-Path $PSScriptRoot "..\backend"
$env:SS_PORT = "$Port"
$env:SS_TOKEN = $Token
$env:SS_DEV = "1"
if ($DataDir) { $env:SS_DATA_DIR = $DataDir }
Push-Location $backend
try {
    Write-Host "SMART SPACE backend -> http://127.0.0.1:$Port  (token: $Token, docs: http://127.0.0.1:$Port/docs)"
    uv run --python 3.11 python -m app
} finally {
    Pop-Location
}
