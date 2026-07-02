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
    # $ErrorActionPreference='Stop' does NOT apply to native commands — an
    # unchecked failed clone left an empty/partial dir that every later step
    # tripped over with misleading errors.
    if ($LASTEXITCODE -ne 0) { throw "git clone of wo80/essentia ($ForkRef) failed (exit $LASTEXITCODE)" }
}

Write-Host "==> Building C/C++ dependencies (Release)"
$bat = Join-Path $fork "packaging\build-dependencies-msvc.bat"
$p = Start-Process -FilePath cmd.exe -ArgumentList "/c `"$bat`" --build-type Release" `
    -WorkingDirectory (Join-Path $fork "packaging") -NoNewWindow -Wait -PassThru
# The exit code is the primary gate. The header probe alone let a warm WorkDir
# pass on fftw3.h from a PREVIOUS run even when a later dependency (TagLib,
# Chromaprint, the FFmpeg fetch) just failed — proceeding then either died
# later with a misleading cmake/link error or silently linked stale cached
# libs into the shipped wheel.
if ($p.ExitCode -ne 0) { throw "Dependency build failed (exit $($p.ExitCode)) — see the log above" }
if (-not (Test-Path (Join-Path $prefix "include\fftw3.h"))) { throw "Dependency build incomplete: $prefix missing headers" }

Write-Host "==> Configuring essentia (DSP-only, Python bindings)"
$build = Join-Path $fork "build-py"
# Don't pin the VS generator version. The GitHub windows-latest image moves
# forward (e.g. VS 2022 -> 2026), and a hardcoded `-G "Visual Studio 17 2022"`
# fails with "could not find any instance of Visual Studio" the moment the image
# ships a newer VS. Omitting -G lets CMake select its default generator = the
# newest installed VS (the same auto-detection the dependency build relies on);
# `-A x64` still forces a 64-bit build.
& cmake -B $build -S $fork -A x64 `
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

# Build the delvewheel DLL search path. Besides our own dependency `bin`, we
# MUST add the build toolset's own CRT so we can BUNDLE msvcp140/vcruntime140
# into the wheel (see below). delvewheel excludes the MSVC runtime by default
# (it assumes the system has it), but the GitHub runner's *compiler* toolset
# can run ahead of its installed *runtime redistributable* — we hit
# "_essentia.pyd built with toolset 14.51 vs discovered msvcp140.dll 14.40",
# and the .pyd then installs fine but FAILS to import (missing newer CRT
# symbols). Bundling the toolset's matching CRT makes the wheel self-contained
# against that skew — and against end users whose VC++ redistributable is
# older than our build toolset (a latent ship bug, not just CI).
$crtPaths = @()
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vswhere) {
    $vsInstall = (& $vswhere -latest -products * -property installationPath) | Select-Object -First 1
    if ($vsInstall) {
        $toolsRoot = Join-Path $vsInstall "VC\Tools\MSVC"
        if (Test-Path $toolsRoot) {
            # Newest toolset dir = the one that compiled _essentia.pyd; its
            # bin\Hostx64\x64 carries the matching msvcp140/vcruntime140 DLLs.
            $newestTools = Get-ChildItem $toolsRoot -Directory |
                Sort-Object { [version]$_.Name } | Select-Object -Last 1
            if ($newestTools) {
                $crtBin = Join-Path $newestTools.FullName "bin\Hostx64\x64"
                if (Test-Path $crtBin) {
                    $crtPaths += $crtBin
                    Write-Host "==> Bundling CRT from toolset $($newestTools.Name): $crtBin"
                }
            }
        }
    }
}
if ($crtPaths.Count -eq 0) {
    Write-Warning "Could not locate the MSVC toolset CRT via vswhere — the wheel will rely on the system runtime (may fail to import on a toolset/redist skew)."
}
$addPath = (@((Join-Path $prefix "bin")) + $crtPaths) -join ";"

Write-Host "==> Repairing (bundling DLLs) -> $OutDir"
# --include force-bundles these normally-excluded CRT DLLs (found on the
# toolset bin added to --add-path above). The three always exist in an x64
# toolset; keep the list minimal so a missing optional DLL can't fail repair.
& $Python -m delvewheel repair $raw.FullName `
    --add-path $addPath `
    --include "msvcp140.dll;vcruntime140.dll;vcruntime140_1.dll" `
    -w $OutDir
if ($LASTEXITCODE -ne 0) { throw "delvewheel repair failed" }

$out = Get-ChildItem -Path $OutDir -Filter "essentia-*.whl" | Sort-Object LastWriteTime | Select-Object -Last 1

# Verify-for-real: import the repaired wheel in a FRESH venv (no build-tree
# DLLs on PATH) with full error output. The header long documented this as a
# manual step; running it here turns a broken bundle into a loud failure AT
# the wheel-build step with the actual ImportError/DLL error visible — instead
# of a silenced `import essentia` failure three steps downstream in
# build-windows.bat that only said "not importable".
Write-Host "==> Verifying the repaired wheel imports in a fresh venv"
$vdir = Join-Path $WorkDir "verify-venv"
if (Test-Path $vdir) { Remove-Item -Recurse -Force $vdir }
& $Python -m venv $vdir
$vpy = Join-Path $vdir "Scripts\python.exe"
& $vpy -m pip install -q "numpy<2" six pyyaml
if ($LASTEXITCODE -ne 0) { throw "verify venv: dependency install failed" }
& $vpy -m pip install -q --no-deps $out.FullName
if ($LASTEXITCODE -ne 0) { throw "verify venv: wheel install failed" }
& $vpy -c "import essentia, essentia.standard; print('essentia', essentia.__version__, 'imports OK in a fresh venv')"
if ($LASTEXITCODE -ne 0) { throw "the repaired wheel FAILED to import in a fresh venv — the bundled runtime is incomplete (see the error above)" }
Remove-Item -Recurse -Force $vdir

Write-Host "==> DONE: $($out.FullName)"
