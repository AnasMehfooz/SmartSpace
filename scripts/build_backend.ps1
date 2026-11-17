<#
.SYNOPSIS
  Build the backend sidecar with PyInstaller (one-folder) - SDD 14, step 1.

.DESCRIPTION
  Output: backend\dist\smartspace-backend\smartspace-backend.exe plus its _internal\ folder.
  For Tauri (Developer A): ship the whole folder as a resource and spawn smartspace-backend.exe as
  a sidecar, or copy the exe as binaries\smartspace-backend-x86_64-pc-windows-msvc.exe next to _internal.
  llama-server builds (backend\bin\llama\*) are copied to dist\...\bin\llama so the backend finds them.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build_backend.ps1
#>
$ErrorActionPreference = "Stop"
$backend = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\backend"))
Push-Location $backend
try {
    uv run --python 3.11 --with pyinstaller pyinstaller smartspace-backend.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    $out = Join-Path $backend "dist\smartspace-backend"
    $llama = Join-Path $backend "bin\llama"
    if (Test-Path $llama) {
        New-Item -ItemType Directory -Force -Path (Join-Path $out "bin") | Out-Null
        Copy-Item -Recurse -Force $llama (Join-Path $out "bin\llama")
    }
    $size = (Get-ChildItem -Recurse $out | Measure-Object -Property Length -Sum).Sum / 1MB
    Write-Host ("Built {0} ({1:N0} MB)" -f $out, $size)
} finally {
    Pop-Location
}
