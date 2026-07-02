@echo off
REM Build the Vibechek CLI / sidecar binary on Windows.
REM
REM Prereqs:
REM   - python 3.10+ on PATH
REM   - run from the repo root, NOT from packaging/
REM
REM Usage:
REM   packaging\build-windows.bat
REM
REM Output:
REM   dist\vibechek.exe                — single-file executable (PyInstaller --onefile)
REM   dist\vibechek-windows-x64.zip    (if PowerShell available)

setlocal
cd /d "%~dp0\.."

if not exist .venv (
    echo Creating venv...
    python -m venv .venv || exit /b 1
)

call .venv\Scripts\activate.bat || exit /b 1

echo Installing build deps...
python -m pip install --upgrade pip wheel || exit /b 1
REM Pin the PyInstaller major so collect_all()/onefile staging behaviour in
REM vibechek.spec stays reproducible across runs (6.x supports Python 3.12).
python -m pip install -e . "pyinstaller>=6,<7" || exit /b 1

REM --- Best-effort: bundle the native (WSL-free) engine when its wheel exists.
REM release.yml builds the DSP-only essentia wheel (scripts\build_native_essentia_wheel.ps1)
REM and sets VIBECHEK_NATIVE_WHEEL to its path. Installing it + onnxruntime + numpy
REM here lets packaging\vibechek.spec fold them into the onefile so
REM inference_engine="native" runs in-process (no WSL). Failure is NON-FATAL: the
REM spec skips the bundle when essentia isn't importable, so the build still ships
REM the lean CLI. Unset (local dev builds) -> skipped, behaviour unchanged.
REM The DSP-only essentia wheel pins numpy<2; install that same numpy (not a
REM resolver-picked 2.x) plus essentia's own runtime deps (pyyaml, six) so they
REM are present for PyInstaller to fold into the onefile.
if defined VIBECHEK_NATIVE_WHEEL (
    echo Installing native engine wheel: %VIBECHEK_NATIVE_WHEEL%
    python -m pip install "%VIBECHEK_NATIVE_WHEEL%" onnxruntime "numpy<2" pyyaml six || echo WARNING: native wheel install failed - building WITHOUT native bundle
    REM Only EXPECT a working native onefile if essentia actually imports in the
    REM build venv. A failed wheel build leaves essentia uninstalled, so we ship
    REM the lean CLI with no gate; a successful install arms the post-freeze
    REM self-test below.
    python -c "import essentia, essentia.standard" >nul 2>&1 && set VIBECHEK_NATIVE_BUNDLED=1
    if not defined VIBECHEK_NATIVE_BUNDLED echo WARNING: essentia not importable after install - building WITHOUT native bundle
) else (
    echo VIBECHEK_NATIVE_WHEEL not set - building without the native engine bundle.
)

REM Release gate: the native engine is the WINDOWS DEFAULT since v0.6.3, so a
REM tagged release build must not silently ship without it. release.yml sets
REM VIBECHEK_REQUIRE_NATIVE=1 on tag refs; when set, a missing/unimportable
REM native bundle fails the build here (fast, before the PyInstaller freeze)
REM instead of degrading to the lean CLI. Local/dispatch builds leave it unset
REM and keep the best-effort behaviour above.
if defined VIBECHEK_REQUIRE_NATIVE if not defined VIBECHEK_NATIVE_BUNDLED (
    echo ERROR: VIBECHEK_REQUIRE_NATIVE is set but the native engine did not bundle
    echo ^(wheel missing or essentia not importable^). Native is the Windows default
    echo engine - refusing to build a release without it.
    exit /b 1
)

echo Running PyInstaller...
pyinstaller packaging\vibechek.spec --noconfirm --clean || exit /b 1

echo Smoke-test the built binary...
dist\vibechek.exe --version || exit /b 1
dist\vibechek.exe --help > nul || exit /b 1

REM Loud native gate: when the native wheel was bundled (essentia imported in
REM the build venv), the frozen exe MUST load + run it in-process. vibechek.spec
REM swallows bundle errors, so without this a broken native bundle would ship
REM silently as a lean CLI on a green build. Fail the build instead.
REM --gold-dir adds the ACCURACY gate: the frozen exe analyzes the committed
REM openly-licensed reference clips and must reproduce the genre/BPM/key pinned
REM in tests\fixtures\gold\manifest.json — a result regression fails the build
REM even when nothing crashes. (cwd is the repo root; see `cd` at the top.)
if defined VIBECHEK_NATIVE_BUNDLED (
    echo Self-testing the bundled native engine inside the frozen exe...
    dist\vibechek.exe selftest-native --gold-dir tests\fixtures\gold || exit /b 1
)

echo Packaging zip...
REM Onefile output is a single .exe rather than a folder; zip it as a one-file
REM archive so the release artifact still has a consistent .zip name.
where powershell > nul 2>&1 && (
    powershell -Command "Compress-Archive -Force -Path dist\vibechek.exe -DestinationPath dist\vibechek-windows-x64.zip"
    echo Created dist\vibechek-windows-x64.zip
)

echo.
echo Build complete: dist\vibechek.exe
endlocal
