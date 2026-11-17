<#
.SYNOPSIS
  Create a demo workspace (German + English PDFs, DOCX, PPTX, notes, images with text, a scanned PDF).
  Same files as the integration tests and the search eval (backend\tests\search_eval.json).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\make_demo_workspace.ps1
#>
param([string]$Dest = (Join-Path $PSScriptRoot "..\demo_workspace"))
$ErrorActionPreference = "Stop"
$Dest = [System.IO.Path]::GetFullPath($Dest)
Push-Location (Join-Path $PSScriptRoot "..\backend")
try {
    uv run --python 3.11 python -m tests.fixtures.make_fixtures "$Dest"
} finally {
    Pop-Location
}
