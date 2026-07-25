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

The git tag form is `vMAJOR.MINOR.PATCH-beta` (e.g. `v0.6.1-beta`); PEP 440 maps the
`-beta` suffix to `b0`. Pre-1.0 follows standard SemVer: bump **PATCH** for
backwards-compatible fixes (`0.6.0-beta` → `0.6.1-beta`) and **MINOR** for features
or breaking changes (`0.6.1-beta` → `0.7.0-beta`). Every `0.x` tag is a beta; the
old `-beta.N` counter is retired. See [RELEASING.md](RELEASING.md) for the full rule.

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

- **Windows default is the native engine; WSL is the fallback.** Since v0.6.3-beta
  `config._DEFAULT_INFERENCE_ENGINE = "native"` on Windows, and the desktop installer
  bundles the DSP-only native essentia wheel into the PyInstaller sidecar
  (`packaging/build-windows.bat`), so a fresh desktop install analyzes fully in-process
  — no WSL. `preflight.essentia_serves_engine()` routes `analyze_via="native"` when the
  bundled essentia imports; when it doesn't (the lean CLI zip, a pip install) or the user
  picks `essentia_tf`/`onnx`, preflight **falls back to WSL** — `analyze` routes through
  `vibechek` in a WSL venv (`~/.vibechek/venv/`). Paths translate `C:\…` ↔ `/mnt/c/…` and
  that translation lives **only** in `vibechek/wsl.py` — the frontend never sees `/mnt/c`.
  (The opt-in CLAP / online-lookup genre engines still route through WSL on Windows;
  the backend refuses their setup on the native engine.)
- **Engine → venv goes through `config.engine_venv_subdir` ONLY.** `onnx` AND `native`
  both run the ONNX stack (native is the same backbone/heads in-process) and map to
  `venv-onnx`; `essentia_tf` maps to `venv` — the two essentia builds can't share a venv
  (both ship the `essentia` module). Every component that picks a venv for an engine
  (preflight, the analyze dispatch, the WSL install/upgrade/self-heal scripts, the GUI
  status probe) must call this function rather than hand-roll
  `"venv-onnx" if engine == "onnx" else "venv"` — that hand-rolled version predates
  `native` and is exactly how preflight validated `venv-onnx` while install/dispatch
  targeted `venv`, dead-ending a native→WSL fallback install ("not ready" after a
  10-minute setup) and crashing analyze after preflight said READY (fixed at 0.7.0; the
  mapping is now the single source of truth, so don't reintroduce a second one).
- **Version-drift guard = auto-upgrade in place, not refusal.** On the WSL fallback, when
  the WSL install's `__version__` is strictly *older* than the sidecar's, the analyzer
  auto-upgrades the WSL venv in place on the next analyze (engine-aware — it targets the
  venv the analyze uses — one-time, with a progress step) rather than aborting; a *newer*
  WSL venv is left alone; a same-version "no such option" click error (code drift without a
  version bump) triggers one in-place update + retry; it hard-errors only if the automatic
  update itself fails (`analyzer.py` ~L2470, `upgrade_vibechek_in_wsl`). The pip source is
  pinned to *this build's release tag* (`config.vibechek_pip_source()` →
  `git+…/Vibechek.git@v<version>`), not `main` HEAD, so the auto-update converges the WSL
  install onto exactly the sidecar's version instead of pulling unreviewed newer code.
  Since 0.8.0, `wsl.ensure_engine_runtime` extends this same block: it runs on **every**
  WSL-dispatched analyze (not just when a version bump is detected), verifies the venv's
  ML stack actually imports (essentia/onnxruntime), reinstalls in place on failure (reuses
  `install_vibechek_in_wsl`), and for `essentia_tf` restores CUDA-11 libs a WSL reinstall
  wipes (`install_cuda_libs_in_wsl`, "Restoring GPU libraries…"). `VIBECHEK_NO_AUTOHEAL`
  disables the repair (detection still runs, so a broken stack still reports an honest
  reason instead of a raw crash). `ok=False` only for a fatal, unrepairable problem; a
  failed GPU-lib restore is non-fatal (`gpu_heal_failed`, CPU fallback still runs).
- **Worker sizing goes through `resources.compute_worker_budget`, never inline math.**
  It is the ONE model both the analyzer's real dispatch and the `worker_budget` RPC (the
  Settings slider's max) call — they used to be two separate hand-rolled calculations that
  could (and did) disagree. It sizes per-worker cost by engine × genre classifier (CLAP
  ≈4.5 GB vs Discogs ≈0.8 GB), measures the RIGHT RAM pool (the WSL VM's memory via
  `wsl.wsl_vm_memory_mb` for WSL-routed engines, not the host total — the WSL VM shares
  one physical-RAM pool with Windows+GUI+browser, so a Linux-sized reserve starves the
  host), can express "nothing fits" (`max_workers=0` → the caller refuses instead of
  launching one worker that gets OOM-killed silently), floors the GPU cap to 0 not 1 (a
  near-empty card falls through to CPU instead of dispatching a doomed single GPU
  worker), and gates GPU workers on a real registration probe (`gpu_registrable`) rather
  than free VRAM alone. Don't reintroduce ad-hoc `ram // per_worker_mb` math anywhere
  else — route it through this function so the slider and the run stay in agreement.
- **`onnxruntime-gpu`'s `<1.27` pin must move WITH the `nvidia-*-cu12` wheel set.** The
  pin lives in `wsl.ONNXRUNTIME_GPU_SPEC = "onnxruntime-gpu<1.27"` (shared by the WSL
  bootstrap and `native_install.py` so both installers stay in lockstep) because
  onnxruntime 1.27.0 dropped CUDA-12 support and made the default wheel CUDA-13-only —
  an unpinned `pip install onnxruntime-gpu` on a fresh GPU setup then hard-crashes on
  import (`libcudart.so.13: cannot open shared object file`) against the CUDA-12 runtime
  we bundle. Only bump this ceiling together with a move to the `nvidia-*-cu13` wheel
  set — bumping one without the other reproduces the exact crash this pin fixed. Note
  `pyproject.toml`'s `[onnx-gpu]` extra (the manual `pip install vibechek[onnx-gpu]`
  path) is unpinned (`onnxruntime-gpu>=1.19`) — it isn't wired to `ONNXRUNTIME_GPU_SPEC`,
  so a manual install can still hit this; the managed installers are the ones that
  matter for the desktop app.
- **Report warning fields ride the report dict, not the dataclass.** `persist_error`,
  `priors_warning`, `model_degradation_warning`, `genre_fallback_warning`,
  `runtime_healed`, `runtime_heal_warning`, and `run_meta` are set directly on the
  JSON-RPC report dict (`rpc.py`/`analyzer.py`) at transport time — a deliberate
  transport-level shortcut so ad-hoc diagnostic fields don't force a TS-codegen regen.
  This is different from `MLResult`/`DuplicateSummary` fields
  (`ml_genre_classifier`, `ml_degraded_heads`, `fpcalc_available`, `phases_run`), which
  ARE real dataclass fields and DO need `scripts/generate_ts_types.py` re-run when added
  — don't confuse the two patterns, and don't add a new dataclass field expecting it to
  reach the frontend without regenerating.
- **`logs/run_history.jsonl` is capped at 50, rewritten whole each append.**
  `logging_setup.append_run_summary` reads the file, appends, truncates to the last
  `_RUN_HISTORY_CAP` (50) entries, and does an atomic tmp-file replace rather than
  open-append — analyze runs are serialized in the sidecar so there's no concurrent
  writer to race. It's best-effort by design: every write failure is swallowed to the
  log so a diagnostics write can never fail the analyze the user just waited on. `doctor`
  reads it back for the "last analyze run" section.
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
  TF-free engine (`AnalysisConfig.inference_engine = "onnx"`; opt-in — the platform
  default is `native` on Windows / `essentia_tf` on macOS/Linux).
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
  `doctor` is engine-aware too: it probes readiness for the config's *saved* engine (not
  a hardcoded `essentia_tf`) and its "last analyze run" section reads
  `logs/run_history.jsonl`.
- Per-distro probe reads `__version__` from `site-packages/vibechek-*.dist-info`.
- To inspect by hand: `wsl -e bash -lic 'cd "$HOME"; ~/.vibechek/venv/bin/vibechek --version'`
  (the TF engine). For the ONNX engine swap `venv` → `venv-onnx`.
- A stack-import failure self-heals on the next analyze via `wsl.ensure_engine_runtime`
  (reinstalls the venv's ML stack in place; restores CUDA-11 libs for `essentia_tf`). Set
  `VIBECHEK_NO_AUTOHEAL=1` to see the raw broken state instead of letting it auto-repair
  while debugging — detection still runs and reports the real error either way.
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
| Run history (last 50 analyzes) | `<data_dir>/Vibechek/logs/run_history.jsonl` |
