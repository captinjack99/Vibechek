@echo off
REM Stage the PyInstaller-built sidecar where Tauri's externalBin AND `cargo
REM run` / `npm run tauri dev` can both find it.
REM
REM We use PyInstaller --onefile, so this is a single-file copy. Tauri appends
REM the HOST target triple to the `externalBin` name (on every platform, not
REM just Windows), so the bundle-facing copy must be named for the toolchain's
REM own host triple — the .sh counterpart derives it from `rustc -vV` for
REM exactly this reason. We mirror the copy into target\<profile>\ for dev
REM mode, where Cargo just executes whatever sits next to vibechek-desktop.exe.
REM
REM Run this AFTER packaging\build-windows.bat produces dist\vibechek.exe.
REM
REM Usage:
REM   packaging\stage-sidecar.bat

setlocal
cd /d "%~dp0\.."

set SOURCE=dist\vibechek.exe

REM Derive the host triple the way packaging\stage-sidecar.sh does. A hard-coded
REM x86_64-pc-windows-msvc silently staged the wrong filename on any other host
REM (an aarch64 toolchain), and Tauri then failed the bundle with a
REM missing-externalBin error that named a file this script never wrote.
set TRIPLE=
for /f "tokens=2" %%T in ('rustc -vV 2^>nul ^| findstr /b "host:"') do set TRIPLE=%%T
if not defined TRIPLE (
    echo Error: could not read the host target triple from `rustc -vV`.
    echo Install the Rust toolchain ^(rustup^) — Tauri needs it to build anyway.
    exit /b 1
)
set DEST_BIN=ui\src-tauri\binaries\vibechek-sidecar-%TRIPLE%.exe

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
