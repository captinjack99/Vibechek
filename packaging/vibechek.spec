# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Vibechek CLI / sidecar binary.

Builds a SINGLE-FILE executable that bundles the pure-Python core plus its
runtime deps (Click, Rich, Mutagen, platformdirs). Essentia / TensorFlow are
deliberately NOT bundled — they're heavy (~500MB) and Windows lacks wheels.
The CLI works without them; analyze/download-models flows instruct the user
to install Essentia separately (via the managed venv or WSL).

Build:
    pyinstaller packaging/vibechek.spec --noconfirm --clean
Result:
    dist/vibechek(.exe)            (single self-contained binary)

# Why --onefile, not --onedir?

Earlier revisions used `--onedir` because that mode starts ~3x faster (no
self-extraction step) and AV vendors flag --onefile less often after they
learn to whitelist a signed publisher. *But* shipping --onedir through a
Tauri 2 sidecar broke in two important ways:

  1. Tauri's `externalBin` config is single-file by contract. It copies one
     file from `binaries/` into `target/<profile>/` at build time. PyInstaller
     `--onedir` produces an EXE that LOADS its sibling `_internal/` directory
     at startup (Python DLL, native pyds). Without explicit secondary staging,
     `_internal/` never reached the dev binary's directory and the sidecar
     died at launch with "Failed to load Python DLL python314.dll".

  2. macOS code-signing + notarization. `tauri-action`'s codesign step
     correctly signs the `externalBin` (the sidecar EXE), but not its sibling
     `.dylib` / `.so` files under `_internal/`. Apple's notarytool refuses
     bundles containing any unsigned Mach-O. We'd need a custom recursive
     signing pass over `_internal/` per release, and Apple has been
     deprecating `codesign --deep` in favor of explicit per-binary signing
     (which scales poorly when PyInstaller drops 30+ shared libraries).

Switching to `--onefile` collapses both problems to one signed binary per
platform. The 500ms cold-start cost is paid ONCE when the Tauri app spawns
its sidecar at session start, not per RPC call — entirely acceptable for a
long-lived RPC server.

AV note: we keep `upx=False` to avoid the Windows AV heuristic that flags
UPX-packed onefile binaries. Combined with Authenticode signing (configured
in `.github/workflows/release.yml`), SmartScreen warnings go away after the
publisher reputation builds.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Mutagen ships pure-Python; no special data files. Rich bundles some color
# themes we want to keep accessible.
hiddenimports = [
    *collect_submodules("vibechek"),
    *collect_submodules("mutagen"),
    "click",
    "rich",
    "platformdirs",
]

datas = []
datas += collect_data_files("rich")

a = Analysis(
    ["entrypoint.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Don't pull in any heavyweight ML deps; users install those separately
        "essentia",
        "numpy",
        "tensorflow",
        "scipy",
        # Test-only deps
        "pytest",
        "_pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onefile equivalent: pass the binaries / zipfiles / datas directly to EXE
# (instead of routing them through a separate COLLECT step). PyInstaller
# embeds everything inside the EXE and the bootloader self-extracts to a
# temp dir at runtime.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="vibechek",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression breaks signing & confuses some antivirus
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
