<#
.SYNOPSIS
  Downloads the runtimes (llama.cpp, whisper.cpp) and the models Foresight uses on Windows.

.DESCRIPTION
  Files go into runtime\ and models\ at the root of the repository. What is already there is
  skipped, so the script can be re-run after an interrupted download.

  Default download: ~4.9 GB (llama.cpp, EmbeddingGemma 2, Gemma 4 E2B, whisper.cpp + model).

.PARAMETER Quality
  Also download Gemma 4 E4B (5.2 GB), the slower "quality" model you can pick in the app.

.PARAMETER NoClasses
  Skip Whisper (live transcription of classes, ~1.3 GB). Search and answers still work.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Quality
#>
param([switch]$Quality, [switch]$NoClasses)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root "runtime"
$models = Join-Path $root "models"
New-Item -ItemType Directory -Force -Path $runtime, $models | Out-Null

# Versions the app is tested with.
$llamaBuild = "b11559"
$whisperBuild = "b5454"
$gh = "https://github.com/ggml-org"
$hf = "https://huggingface.co"

function Get-File([string]$url, [string]$dest) {
    $name = Split-Path -Leaf $dest
    if (Test-Path $dest) {
        Write-Host "  ok   $name"
        return
    }
    Write-Host "  get  $name"
    # curl.exe ships with Windows 10+; -C - resumes a partial download.
    & curl.exe -L --fail --retry 3 -C - --progress-bar -o "$dest.part" $url
    if ($LASTEXITCODE -ne 0) { throw "Download failed ($LASTEXITCODE): $url" }
    Move-Item -Force "$dest.part" $dest
}

function Get-Zip([string]$url, [string]$dir, [string]$check) {
    if (Test-Path (Join-Path $dir $check)) {
        Write-Host "  ok   $(Split-Path -Leaf $dir)"
        return
    }
    $zip = "$dir.zip"
    Get-File $url $zip
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    & tar.exe -xf $zip -C $dir
    if ($LASTEXITCODE -ne 0) { throw "Could not extract $zip" }
    Remove-Item $zip
}

Write-Host "Runtimes"
# Vulkan build: the app runs it on an NVIDIA GPU when there is one, otherwise on the CPU.
Get-Zip "$gh/llama.cpp/releases/download/$llamaBuild/llama-$llamaBuild-bin-win-vulkan-x64.zip" `
    (Join-Path $runtime "llama.cpp") "llama-server.exe"
if (-not $NoClasses) {
    # CUDA 12.4 build (it bundles cuBLAS): live transcription needs an NVIDIA GPU to keep up.
    Get-Zip "$gh/whisper.cpp/releases/download/$whisperBuild/whisper-bin-win-cuda-12.4.0-x64.zip" `
        (Join-Path $runtime "whisper.cpp") "Release\whisper-server.exe"
}

Write-Host "Models"
Get-File "$hf/unsloth/embeddinggemma-2-GGUF/resolve/main/embeddinggemma-2-Q8_0.gguf" `
    (Join-Path $models "embeddinggemma-2-Q8_0.gguf")
Get-File "$hf/google/gemma-4-E2B-it-qat-q4_0-gguf/resolve/main/gemma-4-E2B_q4_0-it.gguf" `
    (Join-Path $models "gemma-4-e2b-it-qat.gguf")
if ($Quality) {
    Get-File "$hf/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/main/gemma-4-E4B_q4_0-it.gguf" `
        (Join-Path $models "gemma-4-e4b-it-qat.gguf")
}
if (-not $NoClasses) {
    Get-File "$hf/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin" `
        (Join-Path $models "ggml-large-v3-turbo-q5_0.bin")
}

Write-Host "`nDone. Next: build the app (see README > Install)."
