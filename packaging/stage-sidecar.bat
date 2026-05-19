@echo off
REM Stage the PyInstaller-built sidecar where Tauri's externalBin AND `cargo
REM run` / `npm run tauri dev` can both find it.
REM
REM We use PyInstaller --onefile, so this is a single-file copy. Tauri's
REM `externalBin` config is a single-file contract that automatically
REM gets the platform-triple-suffixed name on Windows. We mirror the copy
REM into target\<profile>\ for dev mode, where Cargo just executes whatever
REM sits next to vibechek-desktop.exe.
REM
REM Run this AFTER packaging\build-windows.bat produces dist\vibechek.exe.
REM
REM Usage:
REM   packaging\stage-sidecar.bat

setlocal
cd /d "%~dp0\.."

set SOURCE=dist\vibechek.exe
set DEST_BIN=ui\src-tauri\binaries\vibechek-sidecar-x86_64-pc-windows-msvc.exe

if not exist "%SOURCE%" (
    echo Error: %SOURCE% not found.
    echo Run packaging\build-windows.bat first to produce the PyInstaller build.
    exit /b 1
)

if not exist "ui\src-tauri\binaries" mkdir "ui\src-tauri\binaries"

copy /Y "%SOURCE%" "%DEST_BIN%" > nul || (
    echo Error: copy to binaries\ failed.
    exit /b 1
)
echo Staged %SOURCE% -^> %DEST_BIN%

REM Stage into each Cargo profile dir that already exists so `npm run tauri
REM dev` / `cargo run` find a working sidecar without needing a second build.
REM We do NOT create profile dirs that don't exist — that confuses Cargo's
REM next build.
for %%P in (debug release) do (
    if exist "ui\src-tauri\target\%%P" (
        copy /Y "%SOURCE%" "ui\src-tauri\target\%%P\vibechek-sidecar.exe" > nul
        if errorlevel 1 (
            echo Warning: failed to stage EXE into target\%%P\
        ) else (
            echo Staged %SOURCE% -^> target\%%P\vibechek-sidecar.exe
        )
    )
)

echo.
echo Sidecar staged. `npm run tauri dev` and `cargo run` should now find a working sidecar.
endlocal
