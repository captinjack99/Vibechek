<#
.SYNOPSIS
  Build a self-contained, DSP-only native-Windows essentia wheel for the
  `inference_engine="native"` (WSL-free) analyze path.

.DESCRIPTION
  essentia has no official Windows wheel — the sole reason Windows analysis
  otherwise needs WSL. This builds one from the wo80 CMake fork (which solved the
  MSVC RogueVector/build-system blockers), DSP-only (USE_TENSORFLOW=OFF: no
  TensorFlow C lib; the ONNX backbone does inference, a NumPy frontend does the
  mel), then delvewheel-bundles the dependency DLLs so the wheel is portable.

  Proven recipe (validated 2026-06-20): import works in a fresh wheel-only venv
  with no DLL-path tricks; RhythmExtractor2013/KeyExtractor match WSL-essentia
  exactly (KEY 12/12, BPM 11/12 on a 12-track check). NOTE: the wheel is tied to
  the target CPython ABI — build it with the SAME Python the shipped sidecar uses
  (the release pipeline pins 3.12; see .github/workflows/release.yml).

.PARAMETER Python
  Path to the target python.exe (must match the shipping CPython, e.g. 3.12).
  Defaults to the `python` on PATH.

.PARAMETER WorkDir
  Scratch dir for the wo80 checkout + dependency builds. Default: $env:TEMP\vibechek-native-build.

.PARAMETER OutDir
  Where the repaired wheel is written. Default: .\packaging\wheels.

.EXAMPLE
  pwsh scripts/build_native_essentia_wheel.ps1 -Python C:\hostedtoolcache\windows\Python\3.12.x\x64\python.exe

.NOTES
  Requirements (all present on a standard Windows dev box / GitHub windows runner):
  Visual Studio 2022 (MSVC + C++), CMake >= 3.24, git, curl, tar. Builds ~9 C/C++
  deps (Eigen/FFTW/libsamplerate/zlib/TagLib/yaml/Chromaprint/vamp + a prebuilt
  audio-only FFmpeg) the first time (~15-30 min); subsequent runs reuse them.
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$WorkDir = (Join-Path $env:TEMP "vibechek-native-build"),
    [string]$OutDir = (Join-Path $PSScriptRoot "..\packaging\wheels"),
    [string]$ForkRef = "cmake"
)
$ErrorActionPreference = "Stop"

$fork = Join-Path $WorkDir "essentia-wo80"
$prefix = Join-Path $fork "packaging\msvc"
New-Item -ItemType Directory -Force -Path $WorkDir, $OutDir | Out-Null

Write-Host "==> Target Python: $Python"
& $Python --version
# Pin numpy<2 to match the wheel's own declared dependency (the wo80 fork pins
# numpy<2.0) and the freeze venv, so build-ABI == install-ABI for _essentia.pyd.
& $Python -m pip install -q --upgrade pip wheel setuptools "numpy<2" delvewheel

if (-not (Test-Path $fork)) {
    Write-Host "==> Cloning wo80/essentia ($ForkRef)"
    git clone --depth 1 -b $ForkRef https://github.com/wo80/essentia.git $fork
}

Write-Host "==> Building C/C++ dependencies (Release)"
$bat = Join-Path $fork "packaging\build-dependencies-msvc.bat"
$p = Start-Process -FilePath cmd.exe -ArgumentList "/c `"$bat`" --build-type Release" `
    -WorkingDirectory (Join-Path $fork "packaging") -NoNewWindow -Wait -PassThru
if (-not (Test-Path (Join-Path $prefix "include\fftw3.h"))) { throw "Dependency build incomplete: $prefix missing headers" }

Write-Host "==> Configuring essentia (DSP-only, Python bindings)"
$build = Join-Path $fork "build-py"
& cmake -B $build -S $fork -G "Visual Studio 17 2022" -A x64 `
    -DCMAKE_PREFIX_PATH="$prefix" `
    -DBUILD_PYTHON_BINDINGS=ON -DUSE_TENSORFLOW=OFF -DUSE_GAIA2=OFF `
    -DBUILD_TESTS=OFF -DBUILD_VAMP_PLUGIN=OFF -DBUILD_EXAMPLES=OFF `
    -DPython3_EXECUTABLE="$Python"
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

Write-Host "==> Building essentia + Python wheel"
& cmake --build $build --config Release --parallel
if ($LASTEXITCODE -ne 0) { throw "essentia build failed" }
& cmake --build $build --config Release --target wheel
if ($LASTEXITCODE -ne 0) { throw "wheel target failed" }

$raw = Get-ChildItem -Path (Join-Path $build "wheel") -Filter "essentia-*.whl" | Select-Object -First 1
if (-not $raw) { throw "no wheel produced under $build\wheel" }
Write-Host "==> Repairing (bundling DLLs) -> $OutDir"
& $Python -m delvewheel repair $raw.FullName --add-path (Join-Path $prefix "bin") -w $OutDir
if ($LASTEXITCODE -ne 0) { throw "delvewheel repair failed" }

$out = Get-ChildItem -Path $OutDir -Filter "essentia-*.whl" | Sort-Object LastWriteTime | Select-Object -Last 1
Write-Host "==> DONE: $($out.FullName)"
Write-Host "    Verify: <fresh venv>\python -m pip install numpy six pyyaml; pip install --no-deps '$($out.FullName)'; python -c 'import essentia.standard'"
