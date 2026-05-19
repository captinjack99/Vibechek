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
