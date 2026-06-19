# Maintainers / bus-factor notes

The stuff you'd otherwise have to learn the hard way. If you're picking this project up
cold, read this once. For the contributor workflow see [CONTRIBUTING.md](../CONTRIBUTING.md);
for the release mechanics see [RELEASING.md](RELEASING.md).

## The five version manifests must agree

A release bumps **five** files. They must all match, or the version-drift guard and CI
will complain:

1. `vibechek/__init__.py` — `__version__` (e.g. `"0.5.0-beta"`)
2. `pyproject.toml` — PEP 440 form (`0.5.0b0`)
3. `ui/src-tauri/Cargo.toml`
4. `ui/src-tauri/tauri.conf.json`
5. `ui/package.json`

The git tag form is `vMAJOR.MINOR.PATCH-beta` (e.g. `v0.5.0-beta`); PEP 440 maps the
`-beta` suffix to `b0`. (We dropped the old `-beta.N` iteration counter — the `0.x`
line already signals pre-1.0/beta status, so versions just bump `MINOR`.)

**Two lockfiles also carry the version** and must be bumped in the same commit or a
clean CI checkout shows a dirty tree: `ui/src-tauri/Cargo.lock` (the `vibechek-desktop`
package entry) and `ui/package-lock.json` (root + self-reference). See
[RELEASING.md](RELEASING.md) for the exact fields.

## Release flow (short version)

1. Bump the five manifests, update `CHANGELOG.md`, run `python scripts/update_readme_stats.py`.
2. Commit, then `git tag vX.Y.Z-beta`, then `git push origin main && git push origin <tag>`.
3. The tag push triggers [`.github/workflows/release.yml`](../.github/workflows/release.yml):
   builds the PyInstaller CLI + the Tauri installers per OS, then **publishes** a GitHub
   Release for the tag (`draft: false`, pre-release for `-beta`/`-rc`). No manual click.
4. Optionally edit the auto-generated notes on the Releases page. That's it.

The workflow runs an idempotent **pre-delete** step first (deletes any pre-existing
release object for the tag) so re-runs and the old `draft: true` leftovers can't collide.
Full detail (signing, retag, troubleshooting) is in [RELEASING.md](RELEASING.md).

## Architecture landmines (the non-obvious ones)

- **Windows analysis runs in WSL.** Essentia has no Windows wheel, so `analyze` routes
  through `vibechek` installed in a WSL venv (`~/.vibechek/venv/`). Paths translate
  `C:\…` ↔ `/mnt/c/…` and that translation lives **only** in `vibechek/wsl.py` — the
  frontend never sees `/mnt/c`.
- **Version-drift guard.** The analyzer refuses to dispatch to WSL when the WSL install's
  `__version__` ≠ the sidecar's. A stale WSL install is the #1 "analyze silently fails"
  cause. The fix is the one-click "Update WSL install" (`upgrade_vibechek_in_wsl`) or
  re-running setup (which `pip install --upgrade`s).
- **`setsid -w` in the WSL launchers.** Both the install path and the run path wrap the
  WSL process in `setsid -w` so cancellation can kill the whole process group, AND so the
  parent waits instead of fork-and-exit. Dropping the `-w` makes the GUI report "done"
  while apt/pip/analyze keep running orphaned — a landmine that has regressed before.
- **Console-script vs `python -c`.** The production sidecar runs the venv console script
  (`venv/bin/vibechek`), whose `sys.path[0]` is `venv/bin`. Ad-hoc `python -c`/`-m` probes
  add the *cwd* to `sys.path`, so if cwd contains a `vibechek/` package (e.g. a stale git
  worktree), the probe imports the wrong code. Probes must `cd "$HOME"` first.
- **PyInstaller `--onefile`.** The sidecar is one self-contained binary per platform
  (`packaging/vibechek.spec`). `--onedir` broke Tauri's single-file `externalBin` contract.
  On Windows, `--clean` can hit a `PermissionError` removing `build/…/localpycs` when the
  repo lives on a file-syncing or networked drive (e.g. a cloud-sync folder); rebuild
  without `--clean`.
- **GEOB/PRIV preservation is the product.** Every tag write must capture and restore
  Rekordbox's binary GEOB/PRIV frames (cue points, beat grids). This is guarded by a
  synthetic-MP3 regression test — never let a refactor strip it.
- **Config is JSON, not TOML.** Since 0.3.0. A pre-0.3.0 `config.toml` is read once as a
  migration and rewritten as `config.json`. Unknown keys are dropped on load, so adding
  fields is backwards-safe.
- **`numpy` lives in the `[dev]` extra, not core.** The analysis code imports numpy
  *lazily* (inside the functions that need it), but the pure-logic tests (ONNX patch math,
  direction/BPM, …) import it *directly*. Declaring it under `[dev]` keeps it out of the
  runtime dependency surface while ensuring a clean `[dev]`-only CI install doesn't error
  at test collection. Don't move it to core, and don't drop it from `[dev]`.
- **ONNX inference engine (`vibechek/onnx_backend.py`).** A complete, user-selectable
  TF-free engine (`AnalysisConfig.inference_engine = "onnx"`; default stays `essentia_tf`).
  Picked via the Settings toggle / `analyze --engine onnx` / the `inference_engine` config
  field. It mirrors `analyzer.load_models`'s dict + callable signatures exactly so the
  analyzer's downstream logic is byte-unchanged, and imports `onnxruntime`/`essentia`
  lazily. Runs on **plain `essentia` + `onnxruntime`, zero TensorFlow**: MTG's official
  EffNet ONNX backbone (already emits the 400-class genre output, reused rather than
  re-run) + tf2onnx-converted heads. Validated TF-free on a real track (embedding cosine
  0.99942). See `docs/ONNX_MIGRATION.md`.
  - **Separate venv.** The ONNX stack installs into its own `~/.vibechek/venv-onnx`
    (TF engine uses `~/.vibechek/venv`), each with its own essentia build. Install /
    routing / preflight / GPU-probe are all engine-aware across `wsl.py`,
    `native_install.py`, `analyzer.py`, `preflight.py`, and `rpc.py` (the `engine` /
    `inference_engine` params). An onnx-only install with the wrong engine routed at
    analyze time fails on the missing venv — the RPC plumbs the selected engine through.
  - **GPU is real and validated** (NVIDIA **CUDA**, RTX 4070). The installer auto-picks
    the GPU stack when `nvidia-smi` is present: `onnxruntime-gpu` + the `nvidia-*-cu12`
    runtime wheels, loaded at runtime via `onnxruntime.preload_dlls()` (needs
    onnxruntime ≥ 1.19) — **no system CUDA toolkit / `LD_LIBRARY_PATH`**. Apple **CoreML**
    + AMD **ROCm** are wired but hardware-unverified. **Not DirectML** — on Windows the
    ONNX engine runs in WSL (essentia has no Windows wheel), so the providers are
    CUDA/ROCm/CoreML/CPU only. Extras: `vibechek[onnx]` (CPU) / `vibechek[onnx-gpu]`
    (CUDA). The probe lives in `wsl.probe_engine_gpu`; `EngineGpuInfo.provider`/`runtime`
    carry the live EP + onnxruntime version.
  - **Re-setup must uninstall first.** `onnxruntime`, `onnxruntime-gpu`, and
    `onnxruntime-rocm` all ship the **same importable `onnxruntime` module** — installing
    a second one over the first leaves a broken mix. Both the WSL and native installers
    `pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-rocm` before (re)installing
    the right variant. Don't drop that step.
  - **Dev/CI-on-demand scripts** (none in the wheel or the unit suite):
    `scripts/onnx_parity.py` (the parity gate — proves ONNX matches essentia-TF; needs the
    real models + both runtimes), `scripts/convert_heads_to_onnx.py` (the one-off tf2onnx
    head conversion), and `scripts/build_onnx_model_bundle.py` (historical — assembled the
    never-needed `models-onnx-v1` bundle). **The hosting gate is GONE:** the converted
    heads (~5 MB) ship **bundled** in `vibechek/onnx_assets/` (PyInstaller datas + the
    wheel), and the one-click `setup_onnx_engine` stages them + fetches only the official
    EffNet backbone from essentia. Flipping the ONNX default now only awaits cross-vendor
    GPU validation on real AMD/Apple hardware.
- **Every test must import-cleanly on a `[dev]`-only install.** CI installs *only* the
  `[dev]` extra — no essentia, onnxruntime, or soundfile. A test that imports a heavy dep
  at module top level breaks the *whole* collection (not just that test). Gate heavy deps
  with `pytest.importorskip`, `@skipif`, or fakes/mocks. This bit us during beta.8 and is
  the reason `numpy` is in `[dev]`.
- **CDJ-export URI → path rule (`cdj_export.location_to_path`).** Rekordbox stores track
  locations as forward-slash `file://localhost/...` URIs (Windows: `/C:/Users/...`). Build
  the host `Path` straight from the percent-decoded forward-slash string — **never route it
  through `PureWindowsPath`**, which converts to backslashes and collapses into a single
  mangled `PosixPath` segment on Linux/macOS (that was the original cross-platform bug).
  `location_to_path` strips the leading slash on a drive URI and returns a concrete `Path`.

## CI gotchas

- **`tsc --noEmit` + `npm run build` are enforcing.** They were advisory once and hid a
  broken build (the `ui/src/lib` gitignore incident). Keep them enforcing.
- **`.gitignore` `lib/` foot-gun.** A bare `lib/` pattern (meant for Python) also matched
  `ui/src/lib/`, so generated frontend files never got committed and fresh clones failed
  `tsc`. It's anchored to `/lib/` now — keep it anchored.
- **Bundle targets are pinned** in `tauri.conf.json` (`nsis`, `deb`, `appimage`, `dmg`,
  `app`). MSI rejects non-numeric pre-release versions; RPM needs `rpmbuild` (absent on the
  runners). Don't re-add them without handling those.
- **Code signing is opt-in.** The `Configure code signing (opt-in)` step in `release.yml`
  exports cert env vars only when the secret is non-empty. This is mandatory: Tauri 2's
  bundler reads `APPLE_CERTIFICATE` via `std::env::var()` → `Ok("")` for a defined-but-empty
  var, so an unset GitHub secret (which injects `""`) would otherwise make it try
  `security import` on empty data and hard-fail the macOS bundle. Never put `APPLE_*` /
  `WINDOWS_*` secrets directly in the build step's `env:`.
- **README stats freshness check.** CI fails if `README.md`'s stats line is stale relative
  to the code. Run `python scripts/update_readme_stats.py` after adding tests/RPCs/modules.

## Debugging WSL (Windows)

- `wsl_status` / `doctor` RPCs report what's installed where. `wsl_status` /
  `engine_gpu_status` / `preflight` take an `engine` param and probe the matching venv
  (`venv` for `essentia_tf`, `venv-onnx` for `onnx`) — pass the engine the GUI is set to.
- Per-distro probe reads `__version__` from `site-packages/vibechek-*.dist-info`.
- To inspect by hand: `wsl -e bash -lic 'cd "$HOME"; ~/.vibechek/venv/bin/vibechek --version'`
  (the TF engine). For the ONNX engine swap `venv` → `venv-onnx`.
- Distro reset wipes `~/.local/share` — the ML models live there and will need re-download.

## Where things are stored (per user, via platformdirs)

| What | Location key |
|---|---|
| Config | `<config_dir>/Vibechek/config.json` |
| Recent libraries | `<config_dir>/Vibechek/library_state.json` |
| Backup history | `<config_dir>/Vibechek/backup_history.json` |
| ML models (~800 MB) | `<data_dir>/Vibechek/models/` |
| Auto-saved analyses | `<data_dir>/Vibechek/analyses/` |
| Operation journals | `<data_dir>/Vibechek/journals/` |
| Logs | `<data_dir>/Vibechek/logs/vibechek.log` |
