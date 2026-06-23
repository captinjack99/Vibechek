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
python -m pip install -e . pyinstaller || exit /b 1

REM --- Best-effort: bundle the native (WSL-free) engine when its wheel exists.
REM release.yml builds the DSP-only essentia wheel (scripts\build_native_essentia_wheel.ps1)
REM and sets VIBECHEK_NATIVE_WHEEL to its path. Installing it + onnxruntime + numpy
REM here lets packaging\vibechek.spec fold them into the onefile so
REM inference_engine="native" runs in-process (no WSL). Failure is NON-FATAL: the
REM spec skips the bundle when essentia isn't importable, so the build still ships
REM the lean CLI. Unset (local dev builds) -> skipped, behaviour unchanged.
if defined VIBECHEK_NATIVE_WHEEL (
    echo Installing native engine wheel: %VIBECHEK_NATIVE_WHEEL%
    python -m pip install "%VIBECHEK_NATIVE_WHEEL%" onnxruntime numpy || echo WARNING: native wheel install failed - building WITHOUT native bundle
) else (
    echo VIBECHEK_NATIVE_WHEEL not set - building without the native engine bundle.
)

echo Running PyInstaller...
pyinstaller packaging\vibechek.spec --noconfirm --clean || exit /b 1

echo Smoke-test the built binary...
dist\vibechek.exe --version || exit /b 1
dist\vibechek.exe --help > nul || exit /b 1

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
