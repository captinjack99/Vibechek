# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Vibechek CLI.

Builds a standalone executable that bundles the pure-Python core plus its
runtime deps (Click, Rich, Mutagen, platformdirs). Essentia / TensorFlow are
deliberately NOT bundled — they're too heavy (~500MB) and don't have Windows
wheels. The CLI works without them; the `analyze` and `download-models`
subcommands instruct the user to install Essentia separately.

Build:
    pyinstaller packaging/vibechek.spec --noconfirm --clean
Result:
    dist/vibechek/                 (one-folder bundle)
    dist/vibechek/vibechek(.exe)   (entry point)

One-folder mode (not --onefile) is used because:
- ~10x faster startup (no temp extraction on every launch).
- Easier to inspect / debug what's bundled.
- Plays better with antivirus on Windows (--onefile triggers heuristic flags).
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="vibechek",
)
