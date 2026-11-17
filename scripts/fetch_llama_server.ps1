<#
.SYNOPSIS
  Downloads pinned llama.cpp llama-server builds and verifies their SHA-256 (SDD 14, step 2).

.DESCRIPTION
  Extracts each build into backend/bin/llama/<variant>/ where the backend looks for it
  (CUDA > Vulkan > CPU, see app/services/llm_runner.py). Vulkan works on NVIDIA, AMD and Intel GPUs;
  the CPU build picks the best instruction set (AVX2/AVX512/no-AVX) at runtime.
  The CUDA build is optional (large) and only useful on NVIDIA GPUs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\fetch_llama_server.ps1
  powershell -ExecutionPolicy Bypass -File scripts\fetch_llama_server.ps1 -Variants cpu,vulkan,cuda
#>
param(
    [string[]]$Variants = @("cpu", "vulkan"),
    [string]$Dest = (Join-Path $PSScriptRoot "..\backend\bin\llama")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar

# Pinned release. To upgrade: change $Tag and the SHA-256 values (GitHub shows them as asset digests).
$Tag = "b12475"
$Base = "https://github.com/ggml-org/llama.cpp/releases/download/$Tag"
$Assets = @{
    "cpu"    = @(@{ File = "llama-$Tag-bin-win-cpu-x64.zip";    Sha256 = "4aa225c9c3fdabdf4b3489627863866438b9b884f6e1808e7b9a7df58cbda385" })
    "vulkan" = @(@{ File = "llama-$Tag-bin-win-vulkan-x64.zip"; Sha256 = "e72374fc1b54f52f52c7daad62348a646aaa1e5553c9592515d8dc959087c5fd" })
    "cuda"   = @(
        @{ File = "llama-$Tag-bin-win-cuda-13.5-x64.zip";   Sha256 = "a284fd1c2a17e243a5ae1a26838d54d1463338911c1802bd8f3ff689f21a4945" },
        @{ File = "cudart-llama-bin-win-cuda-13.5-x64.zip"; Sha256 = "c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba" }
    )
}

$Dest = [System.IO.Path]::GetFullPath($Dest)
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) "smartspace-llama-$Tag"
New-Item -ItemType Directory -Force -Path $Tmp, $Dest | Out-Null

foreach ($variant in $Variants) {
    if (-not $Assets.ContainsKey($variant)) { throw "Unknown variant '$variant' (use cpu, vulkan, cuda)" }
    $target = Join-Path $Dest $variant
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null

    foreach ($asset in $Assets[$variant]) {
        $zip = Join-Path $Tmp $asset.File
        if (-not (Test-Path $zip)) {
            Write-Host "Downloading $($asset.File) ..."
            Invoke-WebRequest -Uri "$Base/$($asset.File)" -OutFile $zip -UseBasicParsing
        }
        $hash = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
        if ($hash -ne $asset.Sha256) {
            Remove-Item -Force $zip
            throw "SHA-256 mismatch for $($asset.File): got $hash"
        }
        Write-Host "  SHA-256 OK"
        Expand-Archive -Path $zip -DestinationPath $target -Force
    }

    # Flatten if the archive contains a sub folder.
    $exe = Get-ChildItem -Path $target -Recurse -Filter "llama-server.exe" | Select-Object -First 1
    if (-not $exe) { throw "llama-server.exe not found in the $variant build" }
    if ($exe.DirectoryName -ne $target) {
        Get-ChildItem -Path $exe.DirectoryName | Move-Item -Destination $target -Force
    }
    Write-Host "Installed $variant build -> $target"
}

Write-Host ""
Write-Host "Done. The backend finds llama-server automatically in backend\bin\llama\<variant>\."
