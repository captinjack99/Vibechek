@echo off
REM Stage the PyInstaller-built sidecar where Tauri's externalBin expects it.
REM
REM Tauri 2 validates externalBin paths even in dev. The naming convention is
REM `<name>-<target-triple>(.exe)`. On Windows x64 that's:
REM   binaries\vibechek-sidecar-x86_64-pc-windows-msvc.exe
REM
REM Run this AFTER packaging\build-windows.bat has produced dist\vibechek\.
REM
REM Usage:
REM   packaging\stage-sidecar.bat

setlocal
cd /d "%~dp0\.."

set SOURCE=dist\vibechek\vibechek.exe
set DEST=ui\src-tauri\binaries\vibechek-sidecar-x86_64-pc-windows-msvc.exe

if not exist "%SOURCE%" (
    echo Error: %SOURCE% not found.
    echo Run packaging\build-windows.bat first to produce the PyInstaller build.
    exit /b 1
)

if not exist "ui\src-tauri\binaries" mkdir "ui\src-tauri\binaries"

copy /Y "%SOURCE%" "%DEST%" > nul
if errorlevel 1 (
    echo Error: copy failed.
    exit /b 1
)

echo Staged %SOURCE% -^> %DEST%
endlocal
