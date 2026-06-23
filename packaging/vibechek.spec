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

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# PyInstaller resolves relative `datas` source paths against the SPEC file's
# directory (packaging/), not the CWD — so compute the repo root explicitly.
# SPECPATH is injected by PyInstaller and is the directory holding this spec.
_REPO_ROOT = os.path.dirname(SPECPATH)

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
# Bundle the converted ONNX classification heads (~5 MB) so the ONNX engine
# works without downloading the (unhosted) heads — only the official EffNet
# backbone is fetched from essentia. onnx_backend.bundled_onnx_assets_dir()
# reads them from <_MEIPASS>/onnx_assets at runtime; the setup flow copies them
# into <models>/onnx/. Source path is made absolute (relative paths resolve
# against the spec dir, not the repo root).
datas += [(os.path.join(_REPO_ROOT, "vibechek", "onnx_assets"), "onnx_assets")]

# Bundle the CLAP genre kNN reference library (~2 MB). clap_genre.
# bundled_clap_assets_dir() reads it from <_MEIPASS>/clap_assets at runtime. The
# 2.2 GB CLAP checkpoint is NOT bundled — the in-app CLAP setup downloads it.
datas += [(os.path.join(_REPO_ROOT, "vibechek", "clap_assets"), "clap_assets")]

# --- Windows native engine: best-effort bundle of the in-process ML stack -----
# When the DSP-only essentia wheel + onnxruntime + numpy are importable at BUILD
# time (release.yml installs them on the Windows runner before this spec runs),
# fold them into the onefile so `inference_engine="native"` runs FULLY
# in-process — no WSL, no managed venv, nothing for the user to install. The
# default essentia_tf path is unaffected: preflight.essentia_serves_engine()
# still routes essentia_tf/onnx to WSL because the DSP-only wheel lacks the
# TensorFlow algorithms. If ANY piece is absent (non-Windows, or the wheel
# wasn't built) we SKIP the bundle and the binary is byte-for-byte the lean CLI
# as before — so a native-wheel build failure can NEVER break the release.
binaries = []
_bundle_native = False
if sys.platform == "win32":
    try:
        from pathlib import Path as _Path

        from PyInstaller.utils.hooks import collect_all

        import essentia as _ess  # build-time presence probe (the wheel must be installed)
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401

        # onnxruntime + numpy have PyInstaller hooks — collect_all gets their
        # native DLLs + data correctly.
        for _pkg in ("onnxruntime", "numpy"):
            _d, _b, _h = collect_all(_pkg)
            datas += _d
            binaries += _b
            hiddenimports += _h

        # essentia has NO PyInstaller hook, and delvewheel stows its native DLLs in
        # a SIBLING `essentia.libs/` dir that essentia/__init__.py loads at runtime
        # via `os.add_dll_directory(<pkg>/../essentia.libs)`. collect_all misses
        # BOTH the compiled `_essentia*.pyd` (verified binaries=0) and that sibling
        # dir, so add them EXPLICITLY at the exact paths the delvewheel patch
        # expects inside the onefile temp root:
        #   <_MEIPASS>/essentia/_essentia*.pyd   and   <_MEIPASS>/essentia.libs/*.dll
        _ed, _eb, _eh = collect_all("essentia")  # pure-Python tree + metadata
        # collect_all can sweep the compiled `_essentia*.pyd` into datas on some
        # PyInstaller versions; we add it to `binaries` explicitly below. A
        # duplicate data-vs-binary entry at the same dest either errors at build
        # time or stages the .pyd as inert data the loader won't dependency-
        # resolve — so drop any .pyd from the collected datas and let the
        # explicit binary win.
        _ed = [(_s, _dst) for (_s, _dst) in _ed if not str(_s).lower().endswith(".pyd")]
        datas += _ed
        hiddenimports += _eh
        _ess_pkg = _Path(_ess.__file__).parent
        for _pyd in _ess_pkg.glob("_essentia*.pyd"):
            binaries.append((str(_pyd), "essentia"))
        _ess_libs = _ess_pkg.parent / "essentia.libs"
        _ndll = 0
        if _ess_libs.is_dir():
            for _dll in _ess_libs.glob("*.dll"):
                binaries.append((str(_dll), "essentia.libs"))
                _ndll += 1
        # Surface the bundle shape in the build log so a future wheel-recipe
        # change that drops the .pyd or the sibling DLL dir is auditable (and
        # so `selftest-native` failures can be cross-checked against it).
        print(f"[vibechek.spec] native bundle: "
              f"pyd={sum(1 for _src, _dst in binaries if _dst == 'essentia')} "
              f"essentia.libs/*.dll={_ndll}")

        hiddenimports += [
            "essentia", "essentia.standard", "essentia._essentia",
            "onnxruntime", "numpy",
        ]
        # Refuse to claim a native bundle unless the compiled extension is in —
        # without the .pyd `import essentia` fails and the engine is dead weight.
        _bundle_native = any(dest == "essentia" for _src, dest in binaries)
    except Exception:
        _bundle_native = False

# Heavyweight ML deps are excluded by default (users install them via WSL / the
# managed venv). When the native bundle is active we must NOT exclude essentia /
# numpy — they ARE the bundle. TensorFlow + SciPy stay excluded either way (the
# DSP-only wheel has no TF; nothing in the native path needs SciPy).
_excludes = ["tensorflow", "scipy", "pytest", "_pytest"]
if not _bundle_native:
    _excludes = ["essentia", "numpy", *_excludes]

a = Analysis(
    ["entrypoint.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_excludes,
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
